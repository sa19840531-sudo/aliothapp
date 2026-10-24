"""Web V2 Capture 进入页面图和正式证据包的统一准入策略。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

from services.api.app.application.web_readiness_contracts import (
    DEFAULT_WEB_READINESS_PROFILE,
    WebEvidenceRole,
    WebReadinessProfile,
    WebReadinessStatus,
    read_web_readiness_assessment,
)


class WebEvidenceFactKind(StrEnum):
    """Capture 在 Web 证据链中的事实类型。"""

    BUSINESS_PAGE = "business_page"
    OBSTRUCTION_LAYER = "obstruction_layer"
    EXTERNAL_AD_LANDING = "external_ad_landing"
    DIAGNOSTIC_OBSERVATION = "diagnostic_observation"
    REJECTED = "rejected"


@dataclass(frozen=True, kw_only=True)
class WebCaptureEvidenceAdmission:
    """同一份判定同时供持久化、页面图、分类器和报告消费。"""

    kind: WebEvidenceFactKind
    evidence_role: WebEvidenceRole | None
    readiness_status: WebReadinessStatus | None
    can_project_business_node: bool
    can_enter_formal_bundle: bool
    reason_codes: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind.value,
            "evidence_role": (self.evidence_role.value if self.evidence_role is not None else None),
            "readiness_status": (
                self.readiness_status.value if self.readiness_status is not None else None
            ),
            "can_project_business_node": self.can_project_business_node,
            "can_enter_formal_bundle": self.can_enter_formal_bundle,
            "reason_codes": list(self.reason_codes),
        }


def assess_web_capture_evidence_admission(
    pipeline_metrics: Mapping[str, object],
    *,
    capture_role: str,
    is_overlay: bool = False,
    is_external_ad: bool = False,
    diagnostic: bool = False,
    profile: WebReadinessProfile = DEFAULT_WEB_READINESS_PROFILE,
) -> WebCaptureEvidenceAdmission:
    """根据持久化就绪事实分流，不用动作退出码或页型替代证据。"""

    if is_external_ad:
        return _admission(WebEvidenceFactKind.EXTERNAL_AD_LANDING, "external_ad")
    if is_overlay:
        return _admission(WebEvidenceFactKind.OBSTRUCTION_LAYER, "obstruction_layer")
    if diagnostic:
        return _admission(WebEvidenceFactKind.DIAGNOSTIC_OBSERVATION, "diagnostic")
    assessment = read_web_readiness_assessment(pipeline_metrics, profile=profile)
    if assessment is None:
        return _admission(WebEvidenceFactKind.REJECTED, "readiness_missing")
    if not assessment.can_serve_evidence:
        return WebCaptureEvidenceAdmission(
            kind=WebEvidenceFactKind.REJECTED,
            evidence_role=assessment.role,
            readiness_status=assessment.status,
            can_project_business_node=False,
            can_enter_formal_bundle=False,
            reason_codes=assessment.blocking_reasons or ("readiness_not_stable",),
        )
    formal_role = assessment.role in {
        WebEvidenceRole.CLASSIFICATION,
        WebEvidenceRole.INSPECTION,
        WebEvidenceRole.REPORT_PREVIEW,
    }
    formal_capture_role = str(capture_role or "").strip() in {
        "after_action",
        "primary",
    }
    if not formal_role or not formal_capture_role:
        return WebCaptureEvidenceAdmission(
            kind=WebEvidenceFactKind.DIAGNOSTIC_OBSERVATION,
            evidence_role=assessment.role,
            readiness_status=assessment.status,
            can_project_business_node=False,
            can_enter_formal_bundle=False,
            reason_codes=("evidence_role_not_formal",),
        )
    return WebCaptureEvidenceAdmission(
        kind=WebEvidenceFactKind.BUSINESS_PAGE,
        evidence_role=assessment.role,
        readiness_status=assessment.status,
        can_project_business_node=True,
        can_enter_formal_bundle=True,
        reason_codes=(),
    )


def web_capture_admission_from_mapping(
    value: object,
) -> WebCaptureEvidenceAdmission | None:
    """严格读取已持久化准入结论；非法或旧数据返回 None。"""

    if not isinstance(value, Mapping):
        return None
    try:
        reasons = value.get("reason_codes")
        if not isinstance(reasons, list | tuple) or not all(
            isinstance(item, str) and item.strip() for item in reasons
        ):
            return None
        role_value = value.get("evidence_role")
        status_value = value.get("readiness_status")
        project = value.get("can_project_business_node")
        formal = value.get("can_enter_formal_bundle")
        if not isinstance(project, bool) or not isinstance(formal, bool):
            return None
        return WebCaptureEvidenceAdmission(
            kind=WebEvidenceFactKind(str(value.get("kind") or "")),
            evidence_role=(WebEvidenceRole(str(role_value)) if role_value else None),
            readiness_status=(WebReadinessStatus(str(status_value)) if status_value else None),
            can_project_business_node=project,
            can_enter_formal_bundle=formal,
            reason_codes=tuple(item.strip() for item in reasons),
        )
    except (TypeError, ValueError):
        return None


def _admission(kind: WebEvidenceFactKind, reason: str) -> WebCaptureEvidenceAdmission:
    return WebCaptureEvidenceAdmission(
        kind=kind,
        evidence_role=None,
        readiness_status=None,
        can_project_business_node=False,
        can_enter_formal_bundle=False,
        reason_codes=(reason,),
    )


__all__ = [
    "WebCaptureEvidenceAdmission",
    "WebEvidenceFactKind",
    "assess_web_capture_evidence_admission",
    "web_capture_admission_from_mapping",
]
