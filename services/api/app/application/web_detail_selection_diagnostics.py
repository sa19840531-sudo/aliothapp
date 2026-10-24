"""Web 详情选择的有界失败元数据；不保留模型正文或异常原文。"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass

from pydantic import ValidationError

from services.api.app.application.web_assessment_errors import (
    WebAssessmentPermissionDeniedError,
)
from services.api.app.application.web_assessment_evidence import WebModelReasonRejected

_PHASE_CODES = {
    "candidate_input": "WEB_DETAIL_CANDIDATE_INPUT_INVALID",
    "evidence_load": "WEB_DETAIL_EVIDENCE_LOAD_INVALID",
    "request_prepare": "WEB_DETAIL_REQUEST_PREPARE_INVALID",
    "evidence_message": "WEB_DETAIL_EVIDENCE_MESSAGE_INVALID",
    "model_call": "WEB_DETAIL_MODEL_CALL_INVALID",
    "model_permission_denied": "WEB_DETAIL_SELECTION_MODEL_PERMISSION_DENIED",
    "response_parse": "WEB_DETAIL_RESPONSE_SCHEMA_INVALID",
    "candidate_membership": "WEB_DETAIL_CANDIDATE_OUTSIDE_CLOSED_SET",
    "reason_sanitize": "WEB_DETAIL_SELECTION_REASON_REJECTED",
    "unknown": "WEB_DETAIL_SELECTION_INVALID",
}
_VERSION = "web-detail-selection-diagnostic.v1"
_ID_PATTERN = re.compile(r"[A-Za-z0-9_.:-]{1,128}\Z")
_DIGEST_PATTERN = re.compile(r"sha256:[a-f0-9]{64}\Z")


@dataclass
class WebDetailSelectionDiagnosticContext:
    """仅计本层 MAF.run 尝试，不计 SDK 内部 HTTP 重试，不控制动作。"""

    phase: str = "candidate_input"
    attempt_count: int = 0


class WebDetailSelectionDiagnosticError(ValueError):
    """保持原 ValueError 收口范围，仅附加固定枚举诊断。"""

    def __init__(self, context: WebDetailSelectionDiagnosticContext, error: Exception):
        self.phase = context.phase
        self.attempt_count = context.attempt_count
        self.exception_type = _exception_type(error)
        self.reason_rejection_category = (
            error.category if isinstance(error, WebModelReasonRejected) else None
        )
        super().__init__(_PHASE_CODES.get(self.phase, _PHASE_CODES["unknown"]))


@contextmanager
def detail_selection_diagnostic_scope():
    """网络、取消、身份和其他运行时异常仍原样传播。"""
    context = WebDetailSelectionDiagnosticContext()
    try:
        yield context
    except (TypeError, ValueError) as error:
        raise WebDetailSelectionDiagnosticError(context, error) from error


def _exception_type(error: Exception) -> str:
    if isinstance(error, ValidationError):
        return "ValidationError"
    return "TypeError" if isinstance(error, TypeError) else "ValueError"


def detail_selection_failure_diagnostic(
    error: Exception,
    *,
    source_capture_id: str,
    screenshot_artifact_id: str,
    dom_artifact_id: str,
    candidate_ids: tuple[str, ...],
) -> dict[str, object]:
    """证据身份由导航器提供，候选仅保存数量与集合摘要。"""
    known = isinstance(error, WebDetailSelectionDiagnosticError)
    denied = (
        isinstance(error, WebAssessmentPermissionDeniedError)
        and error.assessment_kind == "web_detail_selection"
    )
    phase = "model_permission_denied" if denied else error.phase if known else "unknown"
    payload = {
        "schema_version": _VERSION,
        "phase": phase,
        "reason_code": _PHASE_CODES.get(phase, _PHASE_CODES["unknown"]),
        "exception_type": (
            "PermissionDeniedError" if denied else error.exception_type if known
            else _exception_type(error)
        ),
        "attempt_count": 1 if denied else error.attempt_count if known else None,
        "reason_rejection_category": error.reason_rejection_category if known else None,
        "source_capture_id": source_capture_id,
        "screenshot_artifact_id": screenshot_artifact_id,
        "dom_artifact_id": dom_artifact_id,
        "candidate_count": len(candidate_ids),
        "candidate_set_digest": "sha256:" + hashlib.sha256(
            json.dumps(sorted(set(candidate_ids)), ensure_ascii=True, separators=(",", ":"))
            .encode("utf-8")
        ).hexdigest(),
    }
    return bounded_detail_selection_diagnostic(payload)


def bounded_detail_selection_diagnostic(value: object) -> dict[str, object]:
    """入账边界再次投影白名单，拒绝任意模型、异常与验证输入字段。"""
    if not isinstance(value, Mapping) or not value:
        return {}
    phase = value.get("phase")
    if not isinstance(phase, str) or phase not in _PHASE_CODES:
        phase = "unknown"
    exception_type = value.get("exception_type")
    if exception_type not in (
        "TypeError", "ValueError", "ValidationError", "PermissionDeniedError",
    ):
        exception_type = "ValueError"
    attempts = value.get("attempt_count")
    if type(attempts) is not int or not 0 <= attempts <= 2:
        attempts = None
    count = value.get("candidate_count")
    if type(count) is not int or not 0 <= count <= 100_000:
        count = None
    digest = value.get("candidate_set_digest")
    if not isinstance(digest, str) or _DIGEST_PATTERN.fullmatch(digest) is None:
        digest = None
    result = {
        "schema_version": _VERSION,
        "phase": phase,
        "reason_code": _PHASE_CODES[phase],
        "exception_type": exception_type,
        "attempt_count": attempts,
        "candidate_count": count,
        "candidate_set_digest": digest,
    }
    category = value.get("reason_rejection_category")
    if phase == "reason_sanitize" and category in {
        "empty_reason", "credential_content", "high_entropy_content", "excessive_encoding",
    }:
        result["reason_rejection_category"] = category
    for key in ("source_capture_id", "screenshot_artifact_id", "dom_artifact_id"):
        item = value.get(key)
        result[key] = item if isinstance(item, str) and _ID_PATTERN.fullmatch(item) else None
    return result
