"""H5 页面识别结果到页面类型评估合同的适配器。"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from services.api.app.application.h5_safety_interstitial import (
    H5_SAFETY_INTERSTITIAL_FLAGS,
    H5_SAFETY_INTERSTITIAL_STATES,
)
from services.api.app.domain.exploration.h5_logical_surface import (
    CONFIRMED_LOGICAL_RENDER_PROFILES,
    LOGICAL_SURFACE_PROTOCOL_VERSION,
)
from services.api.app.domain.exploration.h5_page_state import (
    h5_page_state_supports_popup_dismissal,
)
from services.api.app.domain.exploration.page_type_assessment import (
    PageTypeAssessmentStatus,
    build_page_type_assessment,
)
from services.api.app.domain.page.registry import (
    PageTypeRegistry,
    get_default_page_type_registry,
)
from services.api.app.domain.protocol import require_text

H5_PAGE_TYPE_ASSESSMENT_ADAPTER_VERSION = "h5-page-type-assessment-adapter.v1"

H5PageTypeAssessmentAdapterStatus = Literal["accepted", "blocked", "skipped"]

_AUTHENTICATION_REQUIRED_STATES = frozenset(
    {
        "auth_required",
        "authentication_required",
        "login",
        "login_blocked",
        "login_required",
        "login_wall",
        "logged_out",
    }
)
_AUTHENTICATION_REQUIRED_FLAGS = frozenset(
    {
        "authentication_required",
        "login_required",
    }
)
_POPUP_DISMISSAL_TARGET = "关闭当前弹窗"
_POPUP_DISMISSAL_REQUIRED_KEYWORDS = frozenset({"popup_dismissal", "close_control"})
_OBSTRUCTION_SIGNATURE_PATTERN = re.compile(r"^h5-obstruction:[0-9a-f]{64}$")
_SAFETY_BLOCKED_STATES = H5_SAFETY_INTERSTITIAL_STATES
_SAFETY_BLOCKED_FLAGS = H5_SAFETY_INTERSTITIAL_FLAGS


@dataclass(frozen=True, kw_only=True)
class H5PageTypeAssessmentAdapterResult:
    """H5 页面识别到 PageTypeAssessment 的转换结果。"""

    execution_status: H5PageTypeAssessmentAdapterStatus
    page_type_assessment: dict[str, Any] | None = None
    error_code: str | None = None
    message: str | None = None
    retryable: bool = False
    protocol_version: str = H5_PAGE_TYPE_ASSESSMENT_ADAPTER_VERSION

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


@dataclass(frozen=True, kw_only=True)
class H5AfterVisualNavigationAssessment:
    """视觉动作后的四个独立覆盖信任域。"""

    coverage_allowed: bool
    reason_code: str | None = None


def assess_after_visual_navigation(
    *,
    logical_transition_proof: str,
    detection_status: str,
    page_type_confidence: float,
    identity_bound: bool,
) -> H5AfterVisualNavigationAssessment:
    """视觉置信度不参与；只消费 after 的转换、页型与身份事实。"""
    if detection_status != "recognized":
        return H5AfterVisualNavigationAssessment(
            coverage_allowed=False,
            reason_code="h5_page_detection_not_recognized",
        )
    if _float(page_type_confidence) < 0.7:
        return H5AfterVisualNavigationAssessment(
            coverage_allowed=False,
            reason_code="h5_page_detection_low_confidence",
        )
    if not identity_bound:
        return H5AfterVisualNavigationAssessment(
            coverage_allowed=False,
            reason_code="h5_logical_identity_unbound",
        )
    if logical_transition_proof != "trusted":
        return H5AfterVisualNavigationAssessment(
            coverage_allowed=False,
            reason_code="h5_logical_transition_untrusted",
        )
    return H5AfterVisualNavigationAssessment(coverage_allowed=True)


def adapt_h5_page_detection_to_assessment(
    *,
    task_id: str,
    agent_run_id: str,
    detection_result: Mapping[str, Any],
    assessed_at: datetime | None = None,
    min_covered_confidence: float = 0.7,
    registry: PageTypeRegistry | None = None,
) -> H5PageTypeAssessmentAdapterResult:
    """把 page_detection_tool 的 H5 输出转换为单页类型评估 payload。"""
    resolved_registry = registry or get_default_page_type_registry()
    try:
        normalized_task_id = require_text(task_id, "task_id")
        normalized_run_id = require_text(agent_run_id, "agent_run_id")
        page_capture_id = _text(detection_result.get("page_capture_id"))
    except ValueError as exc:
        return _blocked("PAGE_TYPE_ASSESSMENT_CONTEXT_INVALID", str(exc), retryable=True)

    if page_capture_id is None:
        return _blocked(
            "PAGE_TYPE_ASSESSMENT_CAPTURE_REQUIRED",
            "H5 页面类型评估必须引用 page_capture_id。",
            retryable=True,
        )

    risk_flags = _string_list(detection_result.get("risk_flags"))
    page_state = _text(detection_result.get("page_state")) or "unknown"
    page_type = _text(detection_result.get("page_type"))
    trusted_obstruction_dismissal_available = h5_trusted_obstruction_dismissal_available(
        page_state=page_state,
        candidate_actions=detection_result.get("candidate_actions"),
    )
    if page_type is None:
        if h5_safety_blocked(page_state=page_state, risk_flags=risk_flags):
            return _blocked(
                "PAGE_TYPE_ASSESSMENT_BLOCKED_BY_SAFETY",
                "H5 页面被安全/反爬中间页阻断，未获得可信业务 page_type。",
                retryable=False,
            )
        if trusted_obstruction_dismissal_available:
            return H5PageTypeAssessmentAdapterResult(
                execution_status="skipped",
                message="当前业务页面被可信弹窗遮挡，关闭弹窗并重新采集后再评估 page_type。",
            )
        if (
            _text(detection_result.get("catalog_disposition")) == "skipped"
            and _text(detection_result.get("catalog_skip_reason")) == "uncatalogued_page_type"
        ):
            return H5PageTypeAssessmentAdapterResult(
                execution_status="skipped",
                message="当前页面类型不在 Catalog 中，跳过 PageTypeAssessment。",
            )
        return _blocked(
            "PAGE_TYPE_ASSESSMENT_PAGE_TYPE_REQUIRED",
            "H5 页面类型评估必须先获得 page_type。",
            retryable=True,
        )

    if page_type not in resolved_registry.all_codes:
        return _blocked(
            "PAGE_TYPE_ASSESSMENT_UNKNOWN_PAGE_TYPE",
            f"page_type 不属于当前 Catalog：{page_type}",
            retryable=True,
        )
    if page_type not in resolved_registry.coverage_required_codes:
        return H5PageTypeAssessmentAdapterResult(
            execution_status="skipped",
            message=f"{page_type} 不属于业务覆盖页面类型，跳过 PageTypeAssessment。",
        )

    evidence_artifact_ids = _string_list(detection_result.get("evidence_artifact_ids"))
    if not evidence_artifact_ids:
        return _blocked(
            "PAGE_TYPE_ASSESSMENT_EVIDENCE_REQUIRED",
            "H5 页面类型评估必须引用 page_detection_tool 返回的 evidence_artifact_ids。",
            retryable=True,
        )

    logical_gate_reason = _logical_coverage_gate_reason(
        detection_result=detection_result,
        page_type=page_type,
    )
    status = _assessment_status(
        detection_status=_text(detection_result.get("detection_status")),
        page_state=page_state,
        confidence=_float(detection_result.get("confidence")),
        risk_flags=risk_flags,
        trusted_obstruction_dismissal_available=(trusted_obstruction_dismissal_available),
        min_covered_confidence=min_covered_confidence,
        logical_gate_reason=logical_gate_reason,
    )
    reason_code = _reason_code(
        status=status,
        detection_status=_text(detection_result.get("detection_status")),
        confidence=_float(detection_result.get("confidence")),
        min_covered_confidence=min_covered_confidence,
        logical_gate_reason=logical_gate_reason,
    )
    assessment = build_page_type_assessment(
        task_id=normalized_task_id,
        agent_run_id=normalized_run_id,
        page_type=page_type,
        status=status,
        reason_code=reason_code,
        page_capture_ids=(page_capture_id,),
        evidence_artifact_ids=tuple(evidence_artifact_ids),
        page_states=(page_state,),
        assessed_at=assessed_at or datetime.now(UTC),
        registry=resolved_registry,
    )
    payload = assessment.to_tool_payload()
    payload["page_states"] = list(assessment.page_states)
    return H5PageTypeAssessmentAdapterResult(
        execution_status="accepted",
        page_type_assessment=payload,
    )


def _assessment_status(
    *,
    detection_status: str | None,
    page_state: str,
    confidence: float,
    risk_flags: list[str],
    trusted_obstruction_dismissal_available: bool,
    min_covered_confidence: float,
    logical_gate_reason: str | None,
) -> PageTypeAssessmentStatus:
    if h5_authentication_required(
        page_state=page_state,
        risk_flags=risk_flags,
        trusted_obstruction_dismissal_available=(trusted_obstruction_dismissal_available),
    ):
        return PageTypeAssessmentStatus.REQUIRES_AUTH
    if h5_safety_blocked(page_state=page_state, risk_flags=risk_flags):
        return PageTypeAssessmentStatus.BLOCKED_BY_SAFETY
    if detection_status != "recognized" or confidence < min_covered_confidence:
        return PageTypeAssessmentStatus.EVIDENCE_INSUFFICIENT
    if logical_gate_reason is not None:
        return PageTypeAssessmentStatus.EVIDENCE_INSUFFICIENT
    return PageTypeAssessmentStatus.COVERED


def h5_authentication_required(
    *,
    page_state: str,
    risk_flags: Sequence[str],
    trusted_obstruction_dismissal_available: bool = False,
) -> bool:
    """统一识别 H5 页面是否被认证门禁阻断。"""
    normalized_state = page_state.strip().casefold()
    normalized_flags = {flag.strip().casefold() for flag in risk_flags}
    if normalized_state in _AUTHENTICATION_REQUIRED_STATES:
        return True
    if not (normalized_flags & _AUTHENTICATION_REQUIRED_FLAGS):
        return False
    return not (
        trusted_obstruction_dismissal_available
        and h5_page_state_supports_popup_dismissal(normalized_state)
    )


def h5_trusted_obstruction_dismissal_available(
    *,
    page_state: str,
    candidate_actions: object,
) -> bool:
    """判断当前遮挡态是否存在平台可验签的低风险弹窗关闭候选。"""
    if not h5_page_state_supports_popup_dismissal(page_state):
        return False
    if not isinstance(candidate_actions, Sequence) or isinstance(
        candidate_actions,
        str | bytes,
    ):
        return False
    for raw in candidate_actions:
        if not isinstance(raw, Mapping):
            continue
        if (_text(raw.get("action")) or "").casefold() != "click":
            continue
        if _text(raw.get("target")) != _POPUP_DISMISSAL_TARGET:
            continue
        if (_text(raw.get("risk_level")) or "").casefold() != "low":
            continue
        if raw.get("navigation_depth") != 0:
            continue
        keywords = {item.casefold() for item in _string_list(raw.get("target_keywords"))}
        if not _POPUP_DISMISSAL_REQUIRED_KEYWORDS.issubset(keywords):
            continue
        signature = _text(raw.get("obstruction_signature"))
        if signature is None and isinstance(raw.get("action_parameters"), Mapping):
            signature = _text(raw["action_parameters"].get("obstruction_signature"))
        if signature is None:
            continue
        if _OBSTRUCTION_SIGNATURE_PATTERN.fullmatch(signature) is None:
            continue
        return True
    return False


def h5_safety_blocked(
    *,
    page_state: str,
    risk_flags: Sequence[str],
) -> bool:
    """统一识别 H5 页面是否被安全、风控或反爬中间页阻断。"""
    normalized_state = page_state.strip().casefold()
    normalized_flags = {flag.strip().casefold() for flag in risk_flags}
    return bool(
        normalized_state in _SAFETY_BLOCKED_STATES or normalized_flags & _SAFETY_BLOCKED_FLAGS
    )


def _reason_code(
    *,
    status: PageTypeAssessmentStatus,
    detection_status: str | None,
    confidence: float,
    min_covered_confidence: float,
    logical_gate_reason: str | None,
) -> str:
    if status is PageTypeAssessmentStatus.REQUIRES_AUTH:
        return "h5_page_requires_auth"
    if status is PageTypeAssessmentStatus.BLOCKED_BY_SAFETY:
        return "h5_page_blocked_by_safety"
    if detection_status != "recognized":
        return "h5_page_detection_not_recognized"
    if confidence < min_covered_confidence:
        return "h5_page_detection_low_confidence"
    if logical_gate_reason is not None:
        return logical_gate_reason
    return "h5_page_detection_covered"


def _logical_coverage_gate_reason(
    *,
    detection_result: Mapping[str, Any],
    page_type: str,
) -> str | None:
    """仅在非 Shadow 的确认逻辑渲染类型上启用联合覆盖门禁。"""
    surface = detection_result.get("logical_surface")
    if not isinstance(surface, Mapping):
        return None
    if (
        surface.get("protocol_version") != LOGICAL_SURFACE_PROTOCOL_VERSION
        or surface.get("shadow") is True
    ):
        return None
    profile = surface.get("render_profile")
    if not isinstance(profile, Mapping):
        return None
    if (
        str(profile.get("kind") or "") not in CONFIRMED_LOGICAL_RENDER_PROFILES
        or profile.get("status") != "confirmed"
    ):
        return None
    identity = _text(surface.get("identity_fingerprint"))
    binding = surface.get("page_type_binding")
    identity_bound = bool(
        identity is not None
        and isinstance(binding, Mapping)
        and _text(binding.get("identity_fingerprint")) == identity
        and _text(binding.get("page_type")) == page_type
    )
    visual_assessment = assess_after_visual_navigation(
        logical_transition_proof=(_text(surface.get("logical_transition_proof")) or "insufficient"),
        detection_status=_text(detection_result.get("detection_status")) or "",
        page_type_confidence=_float(detection_result.get("confidence")),
        identity_bound=identity_bound,
    )
    if not visual_assessment.coverage_allowed:
        return visual_assessment.reason_code
    assert isinstance(binding, Mapping)
    if binding.get("coverage_allowed") is not True:
        return "h5_logical_binding_rejected"
    return None


def _blocked(
    error_code: str,
    message: str,
    *,
    retryable: bool,
) -> H5PageTypeAssessmentAdapterResult:
    return H5PageTypeAssessmentAdapterResult(
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


def _float(value: Any) -> float:
    if isinstance(value, bool):
        return 0.0
    if isinstance(value, int | float):
        return max(0.0, min(1.0, float(value)))
    return 0.0


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, Sequence) or isinstance(value, str | bytes):
        return []
    items: list[str] = []
    for item in value:
        normalized = _text(item)
        if normalized is not None and normalized not in items:
            items.append(normalized)
    return items
