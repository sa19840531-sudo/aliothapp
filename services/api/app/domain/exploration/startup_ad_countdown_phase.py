"""启动广告倒计时阶段机的纯领域合同。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

from services.api.app.domain.interaction import (
    InteractionOperation,
    SurfaceKind,
)
from services.api.app.domain.interaction.validation import (
    InteractionValidationError,
    validate_semantic_target,
)
from services.api.app.domain.protocol import (
    ProtocolContractError,
    require_aware_datetime,
    require_text,
    stable_protocol_hash,
)

STARTUP_AD_COUNTDOWN_PHASE_PROTOCOL_VERSION = "startup-ad-countdown-phase.v1"
_STARTUP_AD_CONTROL_GRACE_DELAY_MS = 500
# 开屏／首屏：满 2 张截图证据就必须点「进入／跳过」，不得继续干等。
STARTUP_AD_ENTER_AFTER_EVIDENCE_FRAMES = 2
_DEFAULT_STARTUP_ENTER_SEMANTIC_TARGET = "开屏进入：进入/跳过"


class StartupAdCountdownPhaseValidationError(ValueError):
    """启动广告倒计时阶段机输入或输出不满足稳定合同。"""


class StartupAdCountdownDecisionStatus(StrEnum):
    """启动广告倒计时阶段机的判定状态。"""

    WAITING_COUNTDOWN = "waiting_countdown"
    READY_TO_CLOSE = "ready_to_close"
    VERIFYING_CLOSE = "verifying_close"
    COMPLETED = "completed"
    BLOCKED = "blocked"


@dataclass(frozen=True, kw_only=True)
class StartupAdCountdownPhaseContext:
    """一次启动广告倒计时阶段的可恢复上下文。"""

    task_id: str
    session_id: str
    phase_run_id: str
    countdown_observation_count: int = 0
    close_attempt_count: int = 0
    max_countdown_observations: int = 10
    max_close_attempts: int = 3

    def __post_init__(self) -> None:
        for field_name in ("task_id", "session_id", "phase_run_id"):
            _require_text(getattr(self, field_name), field_name)
        _require_non_negative_int(
            self.countdown_observation_count,
            "countdown_observation_count",
        )
        _require_non_negative_int(self.close_attempt_count, "close_attempt_count")
        _require_positive_int(
            self.max_countdown_observations,
            "max_countdown_observations",
        )
        _require_positive_int(self.max_close_attempts, "max_close_attempts")


@dataclass(frozen=True, kw_only=True)
class StartupAdCountdownObservation:
    """Interaction Runtime 采集后供 W17 阶段机消费的当前页面事实。"""

    task_id: str
    session_id: str
    source_capture_id: str
    surface_kind: SurfaceKind
    observed_at: datetime
    page_type: str | None = None
    page_state: str | None = None
    ad_visible: bool = False
    countdown_remaining_ms: int | None = None
    close_control_visible: bool = False
    close_control_enabled: bool = False
    close_semantic_target: str | None = None
    stable_page_reached: bool = False
    evidence_artifact_ids: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        for field_name in ("task_id", "session_id", "source_capture_id"):
            _require_text(getattr(self, field_name), field_name)
        if not isinstance(self.surface_kind, SurfaceKind):
            raise StartupAdCountdownPhaseValidationError("surface_kind 不受支持")
        try:
            require_aware_datetime(self.observed_at, "observed_at")
        except ProtocolContractError as exc:
            raise StartupAdCountdownPhaseValidationError(str(exc)) from exc
        if self.page_type is not None:
            _require_text(self.page_type, "page_type")
        if self.page_state is not None:
            _require_text(self.page_state, "page_state")
        if self.countdown_remaining_ms is not None:
            _require_non_negative_int(
                self.countdown_remaining_ms,
                "countdown_remaining_ms",
            )
        for field_name in (
            "ad_visible",
            "close_control_visible",
            "close_control_enabled",
            "stable_page_reached",
        ):
            if not isinstance(getattr(self, field_name), bool):
                raise StartupAdCountdownPhaseValidationError(f"{field_name} 必须为布尔值")
        # 底层页型稳定但广告仍在：优先清障，不得炸探索。
        if self.stable_page_reached and (
            self.ad_visible
            or self.page_type == "startup_ad"
            or (self.page_state or "").casefold() in {"countdown", "startup_ad"}
        ):
            object.__setattr__(self, "stable_page_reached", False)
        if self.close_semantic_target is not None:
            try:
                validate_semantic_target(self.close_semantic_target, "close_semantic_target")
            except InteractionValidationError as exc:
                raise StartupAdCountdownPhaseValidationError(str(exc)) from exc
        artifacts = _unique_text_tuple(self.evidence_artifact_ids, "evidence_artifact_ids")
        object.__setattr__(self, "evidence_artifact_ids", artifacts)

    @property
    def is_startup_ad_surface(self) -> bool:
        return (
            self.ad_visible
            or self.page_type == "startup_ad"
            or self.page_state in {"countdown", "startup_ad"}
        )


@dataclass(frozen=True, kw_only=True)
class StartupAdCountdownNextAction:
    """阶段机建议交给 Interaction Runtime 执行的下一步动作。"""

    action_id: str
    action_type: InteractionOperation
    source_capture_id: str
    purpose: str
    semantic_target: str | None = None
    delay_ms: int | None = None
    capture_roles: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        for field_name in ("action_id", "source_capture_id", "purpose"):
            _require_text(getattr(self, field_name), field_name)
        if not isinstance(self.action_type, InteractionOperation):
            raise StartupAdCountdownPhaseValidationError("action_type 不受支持")
        if self.semantic_target is not None:
            try:
                validate_semantic_target(self.semantic_target, "semantic_target")
            except InteractionValidationError as exc:
                raise StartupAdCountdownPhaseValidationError(str(exc)) from exc
        if self.delay_ms is not None:
            _require_non_negative_int(self.delay_ms, "delay_ms")
        roles = _unique_text_tuple(self.capture_roles, "capture_roles")
        object.__setattr__(self, "capture_roles", roles)

    def to_dict(self) -> dict[str, Any]:
        return {
            "action_id": self.action_id,
            "action_type": self.action_type.value,
            "source_capture_id": self.source_capture_id,
            "purpose": self.purpose,
            "semantic_target": self.semantic_target,
            "delay_ms": self.delay_ms,
            "capture_roles": list(self.capture_roles),
        }


@dataclass(frozen=True, kw_only=True)
class StartupAdCountdownDecision:
    """W17 阶段机对当前 Capture 的收口或下一步动作决策。"""

    decision_id: str
    phase_run_id: str
    status: StartupAdCountdownDecisionStatus
    reason_code: str
    source_capture_id: str
    next_action: StartupAdCountdownNextAction | None = None
    evidence_artifact_ids: tuple[str, ...] = field(default_factory=tuple)
    protocol_version: str = STARTUP_AD_COUNTDOWN_PHASE_PROTOCOL_VERSION

    def __post_init__(self) -> None:
        for field_name in ("decision_id", "phase_run_id", "reason_code", "source_capture_id"):
            _require_text(getattr(self, field_name), field_name)
        if self.protocol_version != STARTUP_AD_COUNTDOWN_PHASE_PROTOCOL_VERSION:
            raise StartupAdCountdownPhaseValidationError("StartupAdCountdownDecision 版本不受支持")
        if not isinstance(self.status, StartupAdCountdownDecisionStatus):
            raise StartupAdCountdownPhaseValidationError("status 不受支持")
        if self.next_action is not None and not isinstance(
            self.next_action,
            StartupAdCountdownNextAction,
        ):
            raise StartupAdCountdownPhaseValidationError("next_action 类型不正确")
        if self.status in {
            StartupAdCountdownDecisionStatus.WAITING_COUNTDOWN,
            StartupAdCountdownDecisionStatus.READY_TO_CLOSE,
            StartupAdCountdownDecisionStatus.VERIFYING_CLOSE,
        } and self.next_action is None:
            raise StartupAdCountdownPhaseValidationError("非终态阶段必须给出 next_action")
        if self.status in {
            StartupAdCountdownDecisionStatus.COMPLETED,
            StartupAdCountdownDecisionStatus.BLOCKED,
        } and self.next_action is not None:
            raise StartupAdCountdownPhaseValidationError("终态阶段不能继续给出 next_action")
        artifacts = _unique_text_tuple(self.evidence_artifact_ids, "evidence_artifact_ids")
        object.__setattr__(self, "evidence_artifact_ids", artifacts)

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol_version": self.protocol_version,
            "decision_id": self.decision_id,
            "phase_run_id": self.phase_run_id,
            "status": self.status.value,
            "reason_code": self.reason_code,
            "source_capture_id": self.source_capture_id,
            "next_action": self.next_action.to_dict() if self.next_action else None,
            "evidence_artifact_ids": list(self.evidence_artifact_ids),
        }


def evaluate_startup_ad_countdown_phase(
    *,
    context: StartupAdCountdownPhaseContext,
    observation: StartupAdCountdownObservation,
) -> StartupAdCountdownDecision:
    """根据当前页面事实输出启动广告阶段的下一步动作或终态。"""

    if context.task_id != observation.task_id or context.session_id != observation.session_id:
        raise StartupAdCountdownPhaseValidationError("阶段上下文与页面观测不属于同一任务会话")

    if observation.stable_page_reached and not observation.is_startup_ad_surface:
        return _decision(
            context=context,
            observation=observation,
            status=StartupAdCountdownDecisionStatus.COMPLETED,
            reason_code=(
                "startup_ad_closed_and_stable"
                if context.close_attempt_count > 0
                else "startup_ad_not_present"
            ),
        )

    if not observation.is_startup_ad_surface:
        if _is_followup_surface_phase(observation):
            return _decision(
                context=context,
                observation=observation,
                status=StartupAdCountdownDecisionStatus.COMPLETED,
                reason_code="startup_ad_transitioned_to_followup_surface",
            )
        if context.close_attempt_count > 0 and not observation.stable_page_reached:
            if context.countdown_observation_count >= context.max_countdown_observations:
                return _blocked(
                    context=context,
                    observation=observation,
                    reason_code="stable_page_reobserve_limit_exceeded",
                )
            return _decision(
                context=context,
                observation=observation,
                status=StartupAdCountdownDecisionStatus.VERIFYING_CLOSE,
                reason_code="stable_page_reobserve_required",
                next_action=_timed_capture_action(
                    context=context,
                    observation=observation,
                    reason_code="stable_page_reobserve_required",
                    delay_ms=500,
                ),
            )
        return _decision(
            context=context,
            observation=observation,
            status=StartupAdCountdownDecisionStatus.COMPLETED,
            reason_code="startup_ad_not_present",
        )

    if context.close_attempt_count >= context.max_close_attempts:
        return _blocked(
            context=context,
            observation=observation,
            reason_code="close_attempt_limit_exceeded",
        )

    # 控件已可见：首帧即可点进入／跳过。
    if observation.close_control_visible and observation.close_control_enabled:
        if observation.close_semantic_target is None:
            return _blocked(
                context=context,
                observation=observation,
                reason_code="close_control_target_missing",
            )
        return _decision(
            context=context,
            observation=observation,
            status=StartupAdCountdownDecisionStatus.READY_TO_CLOSE,
            reason_code="close_control_ready",
            next_action=_close_action(context=context, observation=observation),
        )

    evidence_frames = _startup_ad_evidence_frame_count(
        context=context,
        observation=observation,
    )
    # 满 2 张截图证据：必须点进入／跳过（可用默认语义目标），禁止继续干等或直接 blocked。
    if evidence_frames >= STARTUP_AD_ENTER_AFTER_EVIDENCE_FRAMES:
        return _decision(
            context=context,
            observation=observation,
            status=StartupAdCountdownDecisionStatus.READY_TO_CLOSE,
            reason_code="startup_ad_two_frame_enter_ready",
            next_action=_close_action(
                context=context,
                observation=observation,
                semantic_target_override=(
                    observation.close_semantic_target
                    or _DEFAULT_STARTUP_ENTER_SEMANTIC_TARGET
                ),
                reason_code="startup_ad_two_frame_enter_ready",
            ),
        )

    if observation.countdown_remaining_ms is not None and observation.countdown_remaining_ms > 0:
        if context.countdown_observation_count >= context.max_countdown_observations:
            return _blocked(
                context=context,
                observation=observation,
                reason_code="countdown_observation_limit_exceeded",
            )
        return _decision(
            context=context,
            observation=observation,
            status=StartupAdCountdownDecisionStatus.WAITING_COUNTDOWN,
            reason_code="countdown_reobserve_required",
            next_action=_timed_capture_action(
                context=context,
                observation=observation,
                reason_code="countdown_reobserve_required",
                delay_ms=min(observation.countdown_remaining_ms, 1_000),
            ),
        )

    if context.close_attempt_count > 0:
        if context.countdown_observation_count >= context.max_countdown_observations:
            return _blocked(
                context=context,
                observation=observation,
                reason_code="startup_ad_control_grace_limit_exceeded",
            )
        return _decision(
            context=context,
            observation=observation,
            status=StartupAdCountdownDecisionStatus.VERIFYING_CLOSE,
            reason_code="stable_page_reobserve_required",
            next_action=_timed_capture_action(
                context=context,
                observation=observation,
                reason_code="stable_page_reobserve_required",
                delay_ms=500,
            ),
        )

    # 未满 2 帧：最多再采一帧，凑齐证据后下一轮必点进入／跳过。
    if context.countdown_observation_count < context.max_countdown_observations:
        return _decision(
            context=context,
            observation=observation,
            status=StartupAdCountdownDecisionStatus.WAITING_COUNTDOWN,
            reason_code="startup_ad_control_reobserve_required",
            next_action=_timed_capture_action(
                context=context,
                observation=observation,
                reason_code="startup_ad_control_reobserve_required",
                delay_ms=_STARTUP_AD_CONTROL_GRACE_DELAY_MS,
            ),
        )

    return _blocked(
        context=context,
        observation=observation,
        reason_code="startup_ad_control_grace_limit_exceeded",
    )


def _is_followup_surface_phase(observation: StartupAdCountdownObservation) -> bool:
    """广告消失后若已出现下一类表面，立即把处理权交回通用阶段路由。

    注意：全屏开屏常被 Vision 标成 overlay；overlay 单独不足以当作已离开开屏，
    否则会立刻收口并误走 popup cloud_back，系统「<」把 App 退出到桌面。
    """

    page_state = str(observation.page_state or "").strip().casefold()
    page_type = str(observation.page_type or "").strip().casefold()
    if page_state == "overlay":
        return False
    return page_state in {
        "popup",
        "modal",
        "dialog",
        "auth_gate",
        "auth_required",
        "login_required",
        "requires_auth",
        "system_dialog",
        "permission_dialog",
        "browser_permission",
    } or page_type in {
        "permission_dialog",
        "browser_permission",
    }


def _startup_ad_evidence_frame_count(
    *,
    context: StartupAdCountdownPhaseContext,
    observation: StartupAdCountdownObservation,
) -> int:
    """当前开屏阶段已有截图证据帧数（含本帧）。"""

    by_artifacts = len(observation.evidence_artifact_ids)
    # countdown_observation_count 为已完成的重采次数；+1 计入当前帧。
    by_counter = context.countdown_observation_count + 1
    return max(by_artifacts, by_counter)


def _close_action(
    *,
    context: StartupAdCountdownPhaseContext,
    observation: StartupAdCountdownObservation,
    semantic_target_override: str | None = None,
    reason_code: str = "close_control_ready",
) -> StartupAdCountdownNextAction:
    operation = (
        InteractionOperation.CLICK
        if observation.surface_kind is SurfaceKind.WEB
        else InteractionOperation.TAP
    )
    target = semantic_target_override or observation.close_semantic_target
    if target is None:
        raise StartupAdCountdownPhaseValidationError("close_semantic_target 缺失")
    return StartupAdCountdownNextAction(
        action_id=_action_id(
            context=context,
            observation=observation,
            action_type=operation,
            reason_code=reason_code,
            semantic_target=target,
        ),
        action_type=operation,
        source_capture_id=observation.source_capture_id,
        semantic_target=target,
        purpose="点开屏进入/跳过并采集进入后页面",
        capture_roles=("startup_ad_before_close", "startup_ad_after_close"),
    )


def _timed_capture_action(
    *,
    context: StartupAdCountdownPhaseContext,
    observation: StartupAdCountdownObservation,
    reason_code: str,
    delay_ms: int,
) -> StartupAdCountdownNextAction:
    return StartupAdCountdownNextAction(
        action_id=_action_id(
            context=context,
            observation=observation,
            action_type=InteractionOperation.TIMED_CAPTURE,
            reason_code=reason_code,
            delay_ms=delay_ms,
        ),
        action_type=InteractionOperation.TIMED_CAPTURE,
        source_capture_id=observation.source_capture_id,
        purpose="等待启动广告阶段变化并重新观察",
        delay_ms=delay_ms,
        capture_roles=("startup_ad_reobserve",),
    )


def _blocked(
    *,
    context: StartupAdCountdownPhaseContext,
    observation: StartupAdCountdownObservation,
    reason_code: str,
) -> StartupAdCountdownDecision:
    return _decision(
        context=context,
        observation=observation,
        status=StartupAdCountdownDecisionStatus.BLOCKED,
        reason_code=reason_code,
    )


def _decision(
    *,
    context: StartupAdCountdownPhaseContext,
    observation: StartupAdCountdownObservation,
    status: StartupAdCountdownDecisionStatus,
    reason_code: str,
    next_action: StartupAdCountdownNextAction | None = None,
) -> StartupAdCountdownDecision:
    decision_id = "startup-ad-countdown-decision:" + stable_protocol_hash(
        {
            "phase_run_id": context.phase_run_id,
            "source_capture_id": observation.source_capture_id,
            "status": status.value,
            "reason_code": reason_code,
            "countdown_observation_count": context.countdown_observation_count,
            "close_attempt_count": context.close_attempt_count,
            "next_action": next_action.to_dict() if next_action else None,
        }
    )
    return StartupAdCountdownDecision(
        decision_id=decision_id,
        phase_run_id=context.phase_run_id,
        status=status,
        reason_code=reason_code,
        source_capture_id=observation.source_capture_id,
        next_action=next_action,
        evidence_artifact_ids=observation.evidence_artifact_ids,
    )


def _action_id(
    *,
    context: StartupAdCountdownPhaseContext,
    observation: StartupAdCountdownObservation,
    action_type: InteractionOperation,
    reason_code: str,
    semantic_target: str | None = None,
    delay_ms: int | None = None,
) -> str:
    return "startup-ad-countdown-action:" + stable_protocol_hash(
        {
            "phase_run_id": context.phase_run_id,
            "source_capture_id": observation.source_capture_id,
            "action_type": action_type.value,
            "reason_code": reason_code,
            "semantic_target": semantic_target,
            "delay_ms": delay_ms,
            "countdown_observation_count": context.countdown_observation_count,
            "close_attempt_count": context.close_attempt_count,
        }
    )


def _require_text(value: Any, field_name: str) -> str:
    try:
        return require_text(value, field_name)
    except ProtocolContractError as exc:
        raise StartupAdCountdownPhaseValidationError(str(exc)) from exc


def _require_non_negative_int(value: Any, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise StartupAdCountdownPhaseValidationError(f"{field_name} 必须为非负整数")
    return value


def _require_positive_int(value: Any, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise StartupAdCountdownPhaseValidationError(f"{field_name} 必须为正整数")
    return value


def _unique_text_tuple(values: tuple[str, ...], field_name: str) -> tuple[str, ...]:
    if not isinstance(values, tuple):
        raise StartupAdCountdownPhaseValidationError(f"{field_name} 必须为数组")
    normalized = tuple(_require_text(value, field_name) for value in values)
    if len(normalized) != len(set(normalized)):
        raise StartupAdCountdownPhaseValidationError(f"{field_name} 不能包含重复值")
    return normalized
