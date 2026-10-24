"""Web 专属的页面就绪与证据角色领域合同。"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum


@dataclass(frozen=True, kw_only=True)
class WebReadinessProfile:
    """仅由可信 Runtime 注入的 Web 采样合同；不接受页面或模型覆盖。"""

    version: str = "web-structure.v1"
    network_required: bool = True
    critical_resource_types: tuple[str, ...] = ("document", "script")
    critical_url_prefixes: tuple[str, ...] = ()
    same_origin_only: bool = True
    minimum_dom_quiet_ms: float = 600.0
    minimum_samples: int = 3
    minimum_window_ms: float = 600.0
    maximum_sample_age_ms: float = 2000.0
    # DOM 与 UI Snapshot 体积较大时，原子制品采集本身可能稳定地超过 5 秒。
    # 仍由采集前后 URL、文档 epoch、结构摘要和本上限共同证明同帧一致性。
    maximum_capture_ms: float = 15000.0

    def __post_init__(self) -> None:
        if not isinstance(self.version, str) or not self.version.strip():
            raise ValueError("profile version 必须为非空字符串")
        if type(self.network_required) is not bool or type(self.same_origin_only) is not bool:
            raise ValueError("网络策略必须为布尔值")
        if type(self.minimum_samples) is not int or self.minimum_samples < 2:
            raise ValueError("稳定窗口至少需要两个样本")
        for name in (
            "minimum_dom_quiet_ms",
            "minimum_window_ms",
            "maximum_sample_age_ms",
            "maximum_capture_ms",
        ):
            if not _finite_number(getattr(self, name)) or getattr(self, name) <= 0:
                raise ValueError(f"{name} 必须为有限正数")
        for name in ("critical_resource_types", "critical_url_prefixes"):
            value = getattr(self, name)
            if not isinstance(value, tuple) or not all(isinstance(x, str) and x for x in value):
                raise ValueError(f"{name} 必须为非空字符串元组")
        if self.network_required and not self.critical_resource_types:
            raise ValueError("必需网络必须声明关键资源类型")


def _finite_number(value: object) -> bool:
    return type(value) in (int, float) and math.isfinite(value)


DEFAULT_WEB_READINESS_PROFILE = WebReadinessProfile()


class WebReadinessStatus(StrEnum):
    """一次 Web 页面观察用于正式证据前的稳定性结论。"""

    STABLE = "stable"
    LOADING = "loading"
    TRANSITIONAL = "transitional"
    TIMEOUT = "timeout"
    DEGRADED = "degraded"
    IDENTITY_DRIFT = "identity_drift"
    ARTIFACT_SKEW = "artifact_skew"
    CAPTURE_FAILED = "capture_failed"
    POPUP_BLOCKED = "popup_blocked"
    HUMAN_VERIFICATION_BLOCKED = "human_verification_blocked"


class WebEvidenceRole(StrEnum):
    """Web Capture 在探索质检链路中承担的证据职责。"""

    ENTRY_OBSERVATION = "entry_observation"
    CLASSIFICATION = "classification"
    INSPECTION = "inspection"
    INTERACTION_BEFORE = "interaction_before"
    INTERACTION_AFTER = "interaction_after"
    REPORT_PREVIEW = "report_preview"


class WebNetworkCapability(StrEnum):
    """当前 Web 运行面是否能够观察关键网络请求。"""

    OBSERVABLE = "observable"
    UNKNOWN = "unknown"
    NOT_REQUIRED = "not_required"


@dataclass(frozen=True, kw_only=True)
class WebReadinessSignals:
    """由 Web 捕获层提供、供纯判定器消费的事实。"""

    identity_before: str | None
    identity_after: str | None
    artifacts_same_epoch: bool
    capture_failed: bool
    timed_out: bool
    core_present: bool | None
    visible_loader: bool | None
    transition_active: bool | None
    necessary_media_ready: bool | None
    dom_quiet_ms: float | None
    minimum_dom_quiet_ms: float
    network_capability: WebNetworkCapability
    network_required: bool
    critical_network_pending: int | None
    settle_complete: bool | None = None
    quorum_met: bool | None = None
    capture_consistent: bool | None = None
    profile_version: str = ""
    human_verification_present: bool | None = None
    media_degradation_proven: bool = False
    inspection_ready: bool = False
    blocking_loader: bool | None = None
    blank_document: bool = False
    structure_stability_proven: bool = False

    def __post_init__(self) -> None:
        for field_name in (
            "artifacts_same_epoch",
            "capture_failed",
            "timed_out",
            "network_required",
            "media_degradation_proven",
            "inspection_ready",
            "blank_document",
            "structure_stability_proven",
        ):
            if not isinstance(getattr(self, field_name), bool):
                raise ValueError(f"{field_name} 必须为布尔值")
        for field_name in (
            "core_present",
            "visible_loader",
            "transition_active",
            "necessary_media_ready",
            "settle_complete",
            "quorum_met",
            "capture_consistent",
            "human_verification_present",
            "blocking_loader",
        ):
            value = getattr(self, field_name)
            if value is not None and not isinstance(value, bool):
                raise ValueError(f"{field_name} 必须为布尔值或 None")
        if not isinstance(self.profile_version, str):
            raise ValueError("profile_version 必须为字符串")
        for field_name in ("identity_before", "identity_after"):
            value = getattr(self, field_name)
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise ValueError(f"{field_name} 必须为非空字符串或 None")
            if isinstance(value, str):
                object.__setattr__(self, field_name, value.strip())
        if isinstance(self.dom_quiet_ms, bool) or (
            self.dom_quiet_ms is not None
            and (not _finite_number(self.dom_quiet_ms) or self.dom_quiet_ms < 0)
        ):
            raise ValueError("dom_quiet_ms 必须为非负数或 None")
        if (
            isinstance(self.minimum_dom_quiet_ms, bool)
            or not _finite_number(self.minimum_dom_quiet_ms)
            or self.minimum_dom_quiet_ms < 0
        ):
            raise ValueError("minimum_dom_quiet_ms 必须为非负数")
        if isinstance(self.critical_network_pending, bool) or (
            self.critical_network_pending is not None
            and (
                not isinstance(self.critical_network_pending, int)
                or self.critical_network_pending < 0
            )
        ):
            raise ValueError("critical_network_pending 必须为非负整数或 None")
        if not isinstance(self.network_capability, WebNetworkCapability):
            raise ValueError("network_capability 必须为 WebNetworkCapability")
        object.__setattr__(
            self,
            "minimum_dom_quiet_ms",
            float(self.minimum_dom_quiet_ms),
        )
        if self.dom_quiet_ms is not None:
            object.__setattr__(self, "dom_quiet_ms", float(self.dom_quiet_ms))


@dataclass(frozen=True, kw_only=True)
class WebSettleAssessment:
    """Web 就绪纯判定结果，不包含任何浏览器副作用。"""

    role: WebEvidenceRole
    status: WebReadinessStatus
    blocking_reasons: tuple[str, ...]
    dynamic_reasons: tuple[str, ...] = ()
    signals: WebReadinessSignals | None = None

    @property
    def can_serve_evidence(self) -> bool:
        """仅稳定帧可以承担正式页面分类或质检证据。"""

        return self.status is WebReadinessStatus.STABLE


def assess_web_readiness(
    signals: WebReadinessSignals,
    *,
    role: WebEvidenceRole,
) -> WebSettleAssessment:
    """按失败关闭顺序评估 Web Capture，固定等待永远不是单独通过条件。"""

    if not isinstance(signals, WebReadinessSignals):
        raise TypeError("signals 必须为 WebReadinessSignals")
    if not isinstance(role, WebEvidenceRole):
        raise TypeError("role 必须为 WebEvidenceRole")

    if signals.capture_failed:
        return _assessment(
            role,
            WebReadinessStatus.CAPTURE_FAILED,
            "blank_document" if signals.blank_document else "capture_failed",
            signals=signals,
        )
    if signals.identity_before is None or signals.identity_after is None:
        return _assessment(
            role,
            WebReadinessStatus.CAPTURE_FAILED,
            "identity_missing",
            signals=signals,
        )
    if signals.identity_before != signals.identity_after:
        return _assessment(
            role,
            WebReadinessStatus.IDENTITY_DRIFT,
            "identity_changed_during_capture",
            signals=signals,
        )
    if not signals.artifacts_same_epoch:
        return _assessment(
            role,
            WebReadinessStatus.ARTIFACT_SKEW,
            "artifacts_cross_epoch",
            signals=signals,
        )
    if signals.human_verification_present is True:
        return _assessment(
            role,
            WebReadinessStatus.HUMAN_VERIFICATION_BLOCKED,
            "human_verification_blocked",
            signals=signals,
        )
    if (
        signals.media_degradation_proven
        and signals.inspection_ready
        and signals.core_present is True
        and signals.capture_consistent is True
        and signals.blocking_loader is False
        and signals.transition_active is False
        and signals.dom_quiet_ms is not None
        and signals.dom_quiet_ms >= signals.minimum_dom_quiet_ms
        and (
            not signals.network_required
            or (
                signals.network_capability is WebNetworkCapability.OBSERVABLE
                and signals.critical_network_pending == 0
            )
        )
    ):
        # 明确失败的非主体媒体只签发降级审计，不进入正式分类或质检。
        return _assessment(
            role, WebReadinessStatus.DEGRADED, "noncritical_media_terminal_failure", signals=signals
        )
    if signals.timed_out:
        return _assessment(
            role,
            WebReadinessStatus.TIMEOUT,
            "readiness_deadline_exceeded",
            signals=signals,
        )
    if signals.capture_consistent is False:
        return _assessment(
            role, WebReadinessStatus.ARTIFACT_SKEW, "capture_inconsistent", signals=signals
        )
    if signals.transition_active is True:
        return _assessment(
            role,
            WebReadinessStatus.TRANSITIONAL,
            "transition_active",
            signals=signals,
        )
    if (
        signals.network_required
        and signals.network_capability is not WebNetworkCapability.OBSERVABLE
    ):
        return _assessment(
            role,
            WebReadinessStatus.CAPTURE_FAILED,
            "required_network_unobservable",
            signals=signals,
        )

    missing_signals: list[str] = []
    if not signals.profile_version.strip():
        missing_signals.append("profile_version_unknown")
    for field_name in (
        "core_present",
        "visible_loader",
        "transition_active",
        "necessary_media_ready",
        "dom_quiet_ms",
        "settle_complete",
        "quorum_met",
        "capture_consistent",
    ):
        if getattr(signals, field_name) is None:
            missing_signals.append(f"{field_name}_unknown")
    if (
        signals.network_required
        and signals.network_capability is WebNetworkCapability.OBSERVABLE
        and signals.critical_network_pending is None
    ):
        missing_signals.append("critical_network_pending_unknown")
    if missing_signals:
        return WebSettleAssessment(
            role=role,
            status=WebReadinessStatus.CAPTURE_FAILED,
            blocking_reasons=tuple(missing_signals),
            signals=signals,
        )

    loading_reasons: list[str] = []
    if signals.settle_complete is not True:
        loading_reasons.append("settle_incomplete")
    if signals.quorum_met is not True:
        loading_reasons.append("stable_sample_quorum_not_met")
    if signals.core_present is not True:
        loading_reasons.append("core_not_ready")
    if signals.visible_loader is True:
        loading_reasons.append("visible_loader")
    if signals.necessary_media_ready is not True:
        loading_reasons.append("necessary_media_not_ready")
    if (
        signals.dom_quiet_ms is not None
        and signals.dom_quiet_ms < signals.minimum_dom_quiet_ms
        and not signals.structure_stability_proven
    ):
        loading_reasons.append("dom_not_quiet")
    if (
        signals.network_required
        and signals.network_capability is WebNetworkCapability.OBSERVABLE
        and signals.critical_network_pending is not None
        and signals.critical_network_pending > 0
    ):
        loading_reasons.append("critical_network_pending")
    if loading_reasons:
        return WebSettleAssessment(
            role=role,
            status=WebReadinessStatus.LOADING,
            blocking_reasons=tuple(loading_reasons),
            signals=signals,
        )

    dynamic_reasons = (
        ("network_capability_unknown",)
        if signals.network_capability is WebNetworkCapability.UNKNOWN
        else ()
    )
    return WebSettleAssessment(
        role=role,
        status=WebReadinessStatus.STABLE,
        blocking_reasons=(),
        dynamic_reasons=dynamic_reasons,
        signals=signals,
    )


def read_web_readiness_assessment(
    pipeline_metrics: Mapping[str, object],
    *,
    profile: WebReadinessProfile = DEFAULT_WEB_READINESS_PROFILE,
) -> WebSettleAssessment | None:
    """从 Web Capture 持久化指标恢复就绪结论；旧 Capture 返回 None。"""

    raw = pipeline_metrics.get("web_readiness")
    if not isinstance(raw, Mapping):
        return None
    role = WebEvidenceRole.ENTRY_OBSERVATION
    try:
        role = WebEvidenceRole(str(raw.get("role") or ""))
        network_capability = WebNetworkCapability(str(raw.get("network_capability") or ""))
        signals = WebReadinessSignals(
            identity_before=_optional_text(raw.get("identity_before")),
            identity_after=_optional_text(raw.get("identity_after")),
            artifacts_same_epoch=raw.get("artifacts_same_epoch"),
            capture_failed=raw.get("capture_failed"),
            timed_out=raw.get("timed_out"),
            core_present=raw.get("core_present"),
            visible_loader=raw.get("visible_loader"),
            transition_active=raw.get("transition_active"),
            necessary_media_ready=raw.get("necessary_media_ready"),
            dom_quiet_ms=raw.get("dom_quiet_ms"),
            minimum_dom_quiet_ms=raw.get("minimum_dom_quiet_ms"),
            network_capability=network_capability,
            network_required=raw.get("network_required"),
            critical_network_pending=raw.get("critical_network_pending"),
            settle_complete=raw.get("settle_complete"),
            quorum_met=raw.get("quorum_met"),
            capture_consistent=raw.get("capture_consistent"),
            profile_version=raw.get("profile_version", ""),
            human_verification_present=raw.get("human_verification_present"),
            media_degradation_proven=_viewport_media_degradation_proven(raw.get("viewport_media")),
            inspection_ready=raw.get("inspection_ready") is True,
            blocking_loader=raw.get("blocking_loader"),
            blank_document=raw.get("blank_document") is True,
            # 采集期间的无关 DOM mutation 不应推翻已经实测且贯穿采集的结构窗口。
            # 不修改原始 quiet 值；URL、epoch、遮挡层及结构变化仍然拒绝准入。
            structure_stability_proven=web_stable_window_proven(raw, profile=profile),
        )
    except (TypeError, ValueError):
        return _assessment(
            role,
            WebReadinessStatus.CAPTURE_FAILED,
            "invalid_web_readiness_metadata",
        )
    assessment = assess_web_readiness(signals, role=role)
    if not assessment.can_serve_evidence:
        return assessment
    if (
        signals.profile_version != profile.version
        or signals.network_required != profile.network_required
        or signals.minimum_dom_quiet_ms != profile.minimum_dom_quiet_ms
        or raw.get("network_scope") != web_profile_network_scope(profile)
    ):
        return _assessment(
            role, WebReadinessStatus.CAPTURE_FAILED, "readiness_profile_mismatch", signals=signals
        )
    if not web_stable_window_proven(raw, profile=profile):
        return _assessment(
            role, WebReadinessStatus.ARTIFACT_SKEW, "stable_window_unproven", signals=signals
        )
    return assessment


def _viewport_media_degradation_proven(value: object) -> bool:
    """从实际解码字段复核降级，不信任单独的汇总状态字符串。"""
    if (
        not isinstance(value, Mapping)
        or value.get("schema_version") != "viewport-media.v1"
        or value.get("state") != "degraded"
        or value.get("sample_truncated") is not False
        or not isinstance(value.get("items"), list)
        or not value["items"]
    ):
        return False
    failed = False
    for item in value["items"]:
        if not isinstance(item, Mapping) or type(item.get("critical")) is not bool:
            return False
        if item.get("tag") != "img" or item.get("complete") is not True:
            return False
        width = item.get("natural_width")
        if not _finite_number(width) or width < 0:
            return False
        if width == 0:
            if item.get("critical") or item.get("resource_state") != "failed":
                return False
            failed = True
        elif item.get("resource_state") != "loaded":
            return False
    return failed


def web_profile_network_scope(profile: WebReadinessProfile) -> dict[str, object]:
    """持久化所声明的观测范围；API 规则需显式配置其请求前缀。"""

    return {
        "resource_types": list(profile.critical_resource_types),
        "url_prefixes": list(profile.critical_url_prefixes),
        "same_origin_only": profile.same_origin_only,
    }


def web_capture_consistency_proven(
    raw: Mapping[str, object],
    *,
    profile: WebReadinessProfile,
) -> bool:
    """仅复核采集边界，不用采样失败冒充截图与结构错位。"""

    consistency = raw.get("capture_consistency")
    if not isinstance(consistency, Mapping):
        return False
    before, after = consistency.get("before"), consistency.get("after")
    if not isinstance(before, Mapping) or not isinstance(after, Mapping):
        return False
    if consistency.get("status") not in {"consistent", "consistent_with_capability_gaps"}:
        return False
    if consistency.get("changed_signals") != []:
        return False
    gaps = consistency.get("unavailable_signals")
    if not isinstance(gaps, (list, tuple)) or any(x != "account_identity" for x in gaps):
        return False
    for field in (
        "page_url_sha256",
        "context_page_urls_sha256",
        "active_page_index",
        "document_time_origin_ms",
        "visibility_state",
        "top_layer_sha256",
    ):
        if field not in before or field not in after or before[field] != after[field]:
            return False
    if (
        before.get("document_observation_available") is not True
        or after.get("document_observation_available") is not True
        or not _finite_number(before.get("document_time_origin_ms"))
        or not before.get("page_url_sha256")
        or not before.get("context_page_urls_sha256")
        or type(before.get("active_page_index")) is not int
        or before["active_page_index"] < 0
        or before.get("visibility_state") != "visible"
    ):
        return False
    capture_start, capture_end = raw.get("capture_started_ms"), raw.get("capture_ended_ms")
    if not all(_finite_number(x) for x in (capture_start, capture_end)):
        return False
    structure = raw.get("structure_before")
    return bool(
        0 <= capture_start <= capture_end
        and capture_end - capture_start <= profile.maximum_capture_ms
        and isinstance(structure, str)
        and structure
        and structure == raw.get("structure_after")
    )


def web_stable_window_proven(
    raw: Mapping[str, object],
    *,
    profile: WebReadinessProfile,
) -> bool:
    """稳定准入必须同时证明连续采样与本次采集处于同一稳定窗口。"""

    if not web_capture_consistency_proven(raw, profile=profile):
        return False
    wait = raw.get("settle_diagnostics")
    if not isinstance(wait, Mapping):
        return False
    if (
        wait.get("settle_complete") is not True
        or wait.get("quorum_met") is not True
        or type(wait.get("stable_sample_count")) is not int
        or wait["stable_sample_count"] < profile.minimum_samples
    ):
        return False
    start, end = wait.get("sample_window_started_ms"), wait.get("sample_window_ended_ms")
    capture_start, capture_end = raw.get("capture_started_ms"), raw.get("capture_ended_ms")
    if not all(_finite_number(x) for x in (start, end, capture_start, capture_end)):
        return False
    if not (0 <= start <= end <= capture_start <= capture_end):
        return False
    if (
        end - start < profile.minimum_window_ms
        or capture_start - end > profile.maximum_sample_age_ms
        or capture_end - capture_start > profile.maximum_capture_ms
    ):
        return False
    structure = wait.get("structure_digest")
    before = raw["capture_consistency"]["before"]
    return bool(
        wait.get("document_time_origin_ms") == before["document_time_origin_ms"]
        and isinstance(structure, str)
        and structure
        and structure == raw.get("structure_before") == raw.get("structure_after")
    )


def _assessment(
    role: WebEvidenceRole,
    status: WebReadinessStatus,
    reason: str,
    *,
    signals: WebReadinessSignals | None = None,
) -> WebSettleAssessment:
    return WebSettleAssessment(
        role=role,
        status=status,
        blocking_reasons=(reason,),
        signals=signals,
    )


def _optional_text(value: object) -> str | None:
    text = str(value).strip() if isinstance(value, str) else ""
    return text or None
