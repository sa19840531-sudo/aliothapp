"""App 视觉识别结果到页面类型评估合同的适配器。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from services.api.app.domain.exploration import AppVisualAnalysis
from services.api.app.domain.exploration.page_type_assessment import (
    PageTypeAssessmentStatus,
    build_page_type_assessment,
)
from services.api.app.domain.page.registry import (
    PageTypeRegistry,
    get_default_page_type_registry,
)
from services.api.app.domain.protocol import require_text

APP_PAGE_TYPE_ASSESSMENT_ADAPTER_VERSION = "app-page-type-assessment-adapter.v1"

AppPageTypeAssessmentAdapterStatus = Literal["accepted", "blocked", "skipped"]

_AUTHENTICATION_REQUIRED_STATES = frozenset(
    {
        "auth_gate",
        "auth_required",
        "authentication_required",
        "login",
        "login_blocked",
        "login_required",
        "login_wall",
        "logged_out",
        "requires_auth",
    }
)
_SAFETY_BLOCKED_STATES = frozenset(
    {
        "browser_permission",
        "permission_dialog",
        "system_dialog",
    }
)
_UNSTABLE_STATES = frozenset(
    {
        "blocked",
        "countdown",
        "dialog",
        "line_loading",
        "loading",
        "modal",
        "overlay",
        "popup",
        "startup_ad",
        "transitional",
        "unknown",
    }
)
_CONFIDENCE_FIELDS = (
    "visual_page_type_confidence",
    "page_type_confidence",
    "confidence",
)


@dataclass(frozen=True, kw_only=True)
class AppPageTypeAssessmentAdapterResult:
    """App 视觉识别到 PageTypeAssessment 的转换结果。"""

    execution_status: AppPageTypeAssessmentAdapterStatus
    page_type_assessment: dict[str, Any] | None = None
    error_code: str | None = None
    message: str | None = None
    retryable: bool = False
    protocol_version: str = APP_PAGE_TYPE_ASSESSMENT_ADAPTER_VERSION

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "protocol_version": self.protocol_version,
            "execution_status": self.execution_status,
            "retryable": self.retryable,
        }
        if self.page_type_assessment is not None:
            payload["page_type_assessment"] = self.page_type_assessment
        if self.error_code is not None:
            payload["error_code"] = self.error_code
        if self.message is not None:
            payload["message"] = self.message
        return payload


def adapt_app_visual_analysis_to_assessment(
    *,
    task_id: str,
    agent_run_id: str,
    analysis: AppVisualAnalysis,
    assessed_at: datetime | None = None,
    min_covered_confidence: float = 0.7,
    registry: PageTypeRegistry | None = None,
) -> AppPageTypeAssessmentAdapterResult:
    """把 App Screenshot Understanding 输出转换为单页类型评估 payload。"""

    if not isinstance(analysis, AppVisualAnalysis):
        raise TypeError("analysis 必须为 AppVisualAnalysis")
    resolved_registry = registry or get_default_page_type_registry()
    try:
        normalized_task_id = require_text(task_id, "task_id")
        normalized_run_id = require_text(agent_run_id, "agent_run_id")
        page_capture_id = require_text(analysis.capture_id, "capture_id")
    except ValueError as exc:
        return _blocked("APP_PAGE_TYPE_ASSESSMENT_CONTEXT_INVALID", str(exc), retryable=True)

    page_type = _text(analysis.visual_page_type)
    if page_type is None:
        return _blocked(
            "APP_PAGE_TYPE_ASSESSMENT_PAGE_TYPE_REQUIRED",
            "App 页面类型评估必须先获得 visual_page_type。",
            retryable=True,
        )
    if page_type not in resolved_registry.all_codes:
        return _blocked(
            "APP_PAGE_TYPE_ASSESSMENT_UNKNOWN_PAGE_TYPE",
            f"visual_page_type 不属于当前 Catalog：{page_type}",
            retryable=True,
        )
    if page_type not in resolved_registry.coverage_required_codes:
        return AppPageTypeAssessmentAdapterResult(
            execution_status="skipped",
            message=f"{page_type} 不属于业务覆盖页面类型，跳过 PageTypeAssessment。",
        )

    evidence_artifact_ids = _evidence_artifact_ids(analysis)
    if not evidence_artifact_ids:
        return _blocked(
            "APP_PAGE_TYPE_ASSESSMENT_EVIDENCE_REQUIRED",
            "App 页面类型评估必须引用截图或其他证据制品。",
            retryable=True,
        )

    page_state = _text(analysis.page_state) or "unknown"
    confidence = _page_type_confidence(analysis.surface_facts)
    status = _assessment_status(
        page_state=page_state,
        confidence=confidence,
        min_covered_confidence=min_covered_confidence,
    )
    reason_code = _reason_code(
        status=status,
        page_state=page_state,
        confidence=confidence,
        min_covered_confidence=min_covered_confidence,
    )
    assessment = build_page_type_assessment(
        task_id=normalized_task_id,
        agent_run_id=normalized_run_id,
        page_type=page_type,
        status=status,
        reason_code=reason_code,
        page_capture_ids=(page_capture_id,),
        evidence_artifact_ids=evidence_artifact_ids,
        page_states=(page_state,),
        assessed_at=assessed_at or datetime.now(UTC),
        registry=resolved_registry,
    )
    payload = assessment.to_tool_payload()
    payload["page_states"] = list(assessment.page_states)
    return AppPageTypeAssessmentAdapterResult(
        execution_status="accepted",
        page_type_assessment=payload,
    )


def _assessment_status(
    *,
    page_state: str,
    confidence: float,
    min_covered_confidence: float,
) -> PageTypeAssessmentStatus:
    normalized_state = page_state.strip().casefold()
    if normalized_state in _AUTHENTICATION_REQUIRED_STATES:
        return PageTypeAssessmentStatus.REQUIRES_AUTH
    if normalized_state in _SAFETY_BLOCKED_STATES:
        return PageTypeAssessmentStatus.BLOCKED_BY_SAFETY
    if (
        normalized_state in _UNSTABLE_STATES
        or confidence < min_covered_confidence
    ):
        return PageTypeAssessmentStatus.EVIDENCE_INSUFFICIENT
    return PageTypeAssessmentStatus.COVERED


def _reason_code(
    *,
    status: PageTypeAssessmentStatus,
    page_state: str,
    confidence: float,
    min_covered_confidence: float,
) -> str:
    if status is PageTypeAssessmentStatus.REQUIRES_AUTH:
        return "app_visual_requires_auth"
    if status is PageTypeAssessmentStatus.BLOCKED_BY_SAFETY:
        return "app_visual_blocked_by_safety"
    if page_state.strip().casefold() in _UNSTABLE_STATES:
        return "app_visual_page_state_unstable"
    if confidence < min_covered_confidence:
        return "app_visual_page_type_low_confidence"
    return "app_visual_page_type_covered"


def _evidence_artifact_ids(analysis: AppVisualAnalysis) -> tuple[str, ...]:
    ids: list[str] = []
    for value in (
        analysis.screenshot_artifact_id,
        *_string_list(analysis.surface_facts.get("evidence_artifact_ids")),
    ):
        normalized = _text(value)
        if normalized is not None and normalized not in ids:
            ids.append(normalized)
    return tuple(ids)


def _page_type_confidence(surface_facts: Mapping[str, Any]) -> float:
    for field_name in _CONFIDENCE_FIELDS:
        value = surface_facts.get(field_name)
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)):
            numeric = float(value)
            if 0 <= numeric <= 1:
                return numeric
    return 1.0


def _blocked(
    error_code: str,
    message: str,
    *,
    retryable: bool,
) -> AppPageTypeAssessmentAdapterResult:
    return AppPageTypeAssessmentAdapterResult(
        execution_status="blocked",
        error_code=error_code,
        message=message,
        retryable=retryable,
    )


def _text(value: Any) -> str | None:
    if isinstance(value, str):
        normalized = value.strip()
        return normalized or None
    return None


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, Sequence) or isinstance(value, str):
        return []
    result: list[str] = []
    for item in value:
        text = _text(item)
        if text is not None and text not in result:
            result.append(text)
    return result
