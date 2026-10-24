"""Web 专属的目标重绑定、有限滚动和动作到达合同。"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum

from services.api.app.application.web_logical_page import (
    canonical_web_logical_page_url,
)
from services.api.app.domain.interaction import ActionIntent, SurfaceKind


class WebInteractionPostcondition(StrEnum):
    """一次 Web 动作必须独立验证的业务后置条件。"""

    TARGET_VISIBLE = "target_visible"
    DETAIL_ARRIVAL = "detail_arrival"
    MENU_ARRIVAL = "menu_arrival"
    OBSTRUCTION_DISMISSED = "obstruction_dismissed"


class WebInteractionEvidenceMode(StrEnum):
    """自然证据与诊断证据必须隔离。"""

    NATURAL = "natural"
    DIAGNOSTIC = "diagnostic"


class WebTargetBindingStatus(StrEnum):
    """每次新 Capture 上重新绑定目标后的闭集结果。"""

    READY = "ready"
    SCROLL_REQUIRED = "scroll_required"
    TARGET_MISSING = "target_missing"
    TARGET_AMBIGUOUS = "target_ambiguous"
    TARGET_BLOCKED = "target_blocked"
    TARGET_STALE = "target_stale"
    SCROLL_STALLED = "scroll_stalled"
    SCROLL_BOUNDARY_REACHED = "scroll_boundary_reached"
    SCROLL_LIMIT_REACHED = "scroll_limit_reached"


@dataclass(frozen=True, kw_only=True)
class WebInteractionPlan:
    """由 Web 编排器冻结、Driver 不得自行扩展的单动作计划。"""

    intent: ActionIntent
    source_capture_id: str
    source_epoch: str
    target_ref: str
    max_scroll_steps: int
    deadline_seconds: float
    postcondition: WebInteractionPostcondition

    def __post_init__(self) -> None:
        if not isinstance(self.intent, ActionIntent):
            raise TypeError("intent 必须为 ActionIntent")
        if self.intent.surface_kind is not SurfaceKind.WEB:
            raise ValueError("WebInteractionPlan 只允许 Web intent")
        for field_name in ("source_capture_id", "source_epoch", "target_ref"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field_name} 不能为空")
            object.__setattr__(self, field_name, value.strip())
        if self.intent.source_capture_id != self.source_capture_id:
            raise ValueError("intent 必须绑定 WebInteractionPlan 的 source_capture_id")
        if (
            isinstance(self.max_scroll_steps, bool)
            or not isinstance(self.max_scroll_steps, int)
            or not 0 <= self.max_scroll_steps <= 20
        ):
            raise ValueError("max_scroll_steps 必须在 0 到 20 之间")
        if (
            isinstance(self.deadline_seconds, bool)
            or not isinstance(self.deadline_seconds, int | float)
            or not math.isfinite(float(self.deadline_seconds))
            or not 0 < float(self.deadline_seconds) <= 300
        ):
            raise ValueError("deadline_seconds 必须在 0 到 300 秒之间")
        if not isinstance(self.postcondition, WebInteractionPostcondition):
            raise ValueError("postcondition 不受支持")
        object.__setattr__(self, "deadline_seconds", float(self.deadline_seconds))


@dataclass(frozen=True, kw_only=True)
class WebTargetObservation:
    """某次 Capture 上重新解析目标和滚动位置得到的事实。"""

    capture_id: str
    source_epoch: str
    matching_target_count: int
    target_visible: bool
    target_enabled: bool
    target_hit_test_passed: bool
    scroll_offset: float
    previous_scroll_offset: float | None
    at_scroll_boundary: bool
    readiness_accepted: bool = False
    identity_verified: bool = False
    business_layer_visible: bool = False
    evidence_mode: WebInteractionEvidenceMode = WebInteractionEvidenceMode.NATURAL
    content_identity: str | None = None
    url: str | None = None

    def __post_init__(self) -> None:
        for field_name in ("capture_id", "source_epoch"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field_name} 不能为空")
        if (
            isinstance(self.matching_target_count, bool)
            or not isinstance(self.matching_target_count, int)
            or self.matching_target_count < 0
        ):
            raise ValueError("matching_target_count 必须为非负整数")
        for field_name in (
            "target_visible",
            "target_enabled",
            "target_hit_test_passed",
            "at_scroll_boundary",
        ):
            if not isinstance(getattr(self, field_name), bool):
                raise ValueError(f"{field_name} 必须为布尔值")
        for field_name in ("scroll_offset", "previous_scroll_offset"):
            value = getattr(self, field_name)
            if value is not None and (
                isinstance(value, bool)
                or not isinstance(value, int | float)
                or not math.isfinite(float(value))
                or value < 0
            ):
                raise ValueError(f"{field_name} 必须为非负有限数或 None")


@dataclass(frozen=True, kw_only=True)
class WebTargetBindingResult:
    status: WebTargetBindingStatus
    rebound_capture_id: str
    rebound_epoch: str
    click_authorized: bool
    failure_reason: str | None = None


@dataclass(frozen=True, kw_only=True)
class WebInteractionOutcome:
    """动作接收、效果、后置条件与可信到达四层独立事实。"""

    command_accepted: bool
    effect_observed: bool
    postcondition_satisfied: bool
    transition_arrived: bool
    after_capture_id: str | None
    failure_reason: str | None
    evidence_mode: WebInteractionEvidenceMode = WebInteractionEvidenceMode.NATURAL

    def __post_init__(self) -> None:
        for field_name in (
            "command_accepted",
            "effect_observed",
            "postcondition_satisfied",
            "transition_arrived",
        ):
            if not isinstance(getattr(self, field_name), bool):
                raise ValueError(f"{field_name} 必须为布尔值")
        if self.after_capture_id is not None and (
            not isinstance(self.after_capture_id, str) or not self.after_capture_id.strip()
        ):
            raise ValueError("after_capture_id 必须为非空字符串或 None")
        if self.failure_reason is not None and (
            not isinstance(self.failure_reason, str) or not self.failure_reason.strip()
        ):
            raise ValueError("failure_reason 必须为非空字符串或 None")
        if not isinstance(self.evidence_mode, WebInteractionEvidenceMode):
            raise ValueError("evidence_mode 不受支持")
        if self.transition_arrived and not (
            self.command_accepted
            and self.effect_observed
            and self.postcondition_satisfied
            and self.after_capture_id
            and self.evidence_mode is WebInteractionEvidenceMode.NATURAL
        ):
            raise ValueError("transition_arrived 缺少完整自然动作事实")


def assess_web_target_binding(
    plan: WebInteractionPlan,
    observation: WebTargetObservation,
    *,
    completed_scroll_steps: int,
) -> WebTargetBindingResult:
    """只允许新 Capture 上唯一、可见、可用且命中的目标进入点击。"""

    if not isinstance(plan, WebInteractionPlan):
        raise TypeError("plan 必须为 WebInteractionPlan")
    if not isinstance(observation, WebTargetObservation):
        raise TypeError("observation 必须为 WebTargetObservation")
    if (
        isinstance(completed_scroll_steps, bool)
        or not isinstance(completed_scroll_steps, int)
        or completed_scroll_steps < 0
    ):
        raise ValueError("completed_scroll_steps 必须为非负整数")

    def result(
        status: WebTargetBindingStatus,
        reason: str | None,
    ) -> WebTargetBindingResult:
        return WebTargetBindingResult(
            status=status,
            rebound_capture_id=observation.capture_id,
            rebound_epoch=observation.source_epoch,
            click_authorized=status is WebTargetBindingStatus.READY,
            failure_reason=reason,
        )

    if completed_scroll_steps > 0 and (
        observation.capture_id == plan.source_capture_id
        or observation.source_epoch == plan.source_epoch
    ):
        return result(WebTargetBindingStatus.TARGET_STALE, "target_not_rebound")
    if observation.matching_target_count == 0:
        return result(WebTargetBindingStatus.TARGET_MISSING, "target_missing")
    if observation.matching_target_count > 1:
        return result(WebTargetBindingStatus.TARGET_AMBIGUOUS, "target_ambiguous")
    if observation.target_visible:
        if observation.target_enabled and observation.target_hit_test_passed:
            return result(WebTargetBindingStatus.READY, None)
        return result(WebTargetBindingStatus.TARGET_BLOCKED, "target_not_actionable")
    if observation.at_scroll_boundary:
        return result(
            WebTargetBindingStatus.SCROLL_BOUNDARY_REACHED,
            "scroll_boundary_reached",
        )
    if completed_scroll_steps >= plan.max_scroll_steps:
        return result(WebTargetBindingStatus.SCROLL_LIMIT_REACHED, "scroll_limit_reached")
    if (
        observation.previous_scroll_offset is not None
        and observation.scroll_offset == observation.previous_scroll_offset
    ):
        return result(WebTargetBindingStatus.SCROLL_STALLED, "scroll_did_not_move")
    return result(WebTargetBindingStatus.SCROLL_REQUIRED, None)


def assess_web_interaction_outcome(
    *,
    plan: WebInteractionPlan,
    command_accepted: bool,
    before_url: str,
    after_url: str,
    before_content_identity: str | None,
    after_content_identity: str | None,
    postcondition_satisfied: bool,
    after_capture_id: str | None,
    evidence_mode: WebInteractionEvidenceMode = WebInteractionEvidenceMode.NATURAL,
    readiness_accepted: bool = False,
    identity_verified: bool = False,
    business_layer_visible: bool = False,
    effect_observed: bool | None = None,
) -> WebInteractionOutcome:
    """根据独立前后事实结算动作，命令回执不能单独证明到达。"""

    if not isinstance(plan, WebInteractionPlan):
        raise TypeError("plan 必须为 WebInteractionPlan")
    if not isinstance(command_accepted, bool) or not isinstance(postcondition_satisfied, bool):
        raise ValueError("动作布尔事实类型错误")
    if any(
        not isinstance(value, bool)
        for value in (
            readiness_accepted,
            identity_verified,
            business_layer_visible,
        )
    ) or (effect_observed is not None and not isinstance(effect_observed, bool)):
        raise ValueError("动作信任事实类型错误")
    before_identity = canonical_web_logical_page_url(before_url)
    after_identity = canonical_web_logical_page_url(after_url)
    content_changed = bool(
        before_content_identity
        and after_content_identity
        and before_content_identity != after_content_identity
    )
    if effect_observed is None:
        effect_observed = before_identity != after_identity or content_changed
    failure_reason = None
    if not command_accepted:
        failure_reason = "command_not_accepted"
    elif not after_capture_id:
        failure_reason = "after_capture_missing"
    elif not effect_observed:
        failure_reason = "effect_not_observed"
    elif not postcondition_satisfied:
        failure_reason = "postcondition_not_satisfied"
    elif evidence_mode is WebInteractionEvidenceMode.DIAGNOSTIC:
        failure_reason = "diagnostic_evidence_not_trusted"
    elif not readiness_accepted:
        failure_reason = "readiness_not_accepted"
    elif not identity_verified:
        failure_reason = "identity_not_verified"
    elif not business_layer_visible:
        failure_reason = "business_layer_not_visible"
    return WebInteractionOutcome(
        command_accepted=command_accepted,
        effect_observed=effect_observed,
        postcondition_satisfied=postcondition_satisfied,
        transition_arrived=failure_reason is None,
        after_capture_id=after_capture_id,
        failure_reason=failure_reason,
        evidence_mode=evidence_mode,
    )


__all__ = [
    "WebInteractionEvidenceMode",
    "WebInteractionOutcome",
    "WebInteractionPlan",
    "WebInteractionPostcondition",
    "WebTargetBindingResult",
    "WebTargetBindingStatus",
    "WebTargetObservation",
    "assess_web_interaction_outcome",
    "assess_web_target_binding",
]
