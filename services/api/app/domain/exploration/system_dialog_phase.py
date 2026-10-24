"""系统弹窗阶段机的纯领域合同。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

from services.api.app.domain.interaction import InteractionOperation, SurfaceKind
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

SYSTEM_DIALOG_PHASE_PROTOCOL_VERSION = "system-dialog-phase.v1"


class SystemDialogPhaseValidationError(ValueError):
    """系统弹窗阶段机输入或输出不满足稳定合同。"""


class SystemDialogDecisionStatus(StrEnum):
    """系统弹窗阶段机的判定状态。"""

    READY_TO_HANDLE = "ready_to_handle"
    VERIFYING_HANDLE = "verifying_handle"
    HANDLED = "handled"
    NOT_PRESENT = "not_present"
    BLOCKED = "blocked"


class SystemDialogKind(StrEnum):
    """系统弹窗类型。"""

    PERMISSION = "permission"
    SYSTEM_ALERT = "system_alert"
    BROWSER_PERMISSION = "browser_permission"
    APP_UPDATE = "app_update"
    UNKNOWN = "unknown"


class SystemDialogActionPolicy(StrEnum):
    """系统弹窗处理策略。"""

    ALLOW = "allow"
    DENY = "deny"
    DISMISS = "dismiss"
    OPEN_SETTINGS = "open_settings"
    MANUAL_REVIEW = "manual_review"


@dataclass(frozen=True, kw_only=True)
class SystemDialogPhaseContext:
    """一次系统弹窗阶段的可恢复上下文。"""

    task_id: str
    session_id: str
    phase_run_id: str
    handled_dialog_count: int = 0
    reobserve_count: int = 0
    max_system_dialogs: int = 6
    max_reobservations: int = 4

    def __post_init__(self) -> None:
        for field_name in ("task_id", "session_id", "phase_run_id"):
            _require_text(getattr(self, field_name), field_name)
        _require_non_negative_int(self.handled_dialog_count, "handled_dialog_count")
        _require_non_negative_int(self.reobserve_count, "reobserve_count")
        _require_positive_int(self.max_system_dialogs, "max_system_dialogs")
        _require_positive_int(self.max_reobservations, "max_reobservations")


@dataclass(frozen=True, kw_only=True)
class SystemDialogObservation:
    """Interaction Runtime 采集后供 W20 阶段机消费的当前系统弹窗事实。"""

    task_id: str
    session_id: str
    source_capture_id: str
    surface_kind: SurfaceKind
    observed_at: datetime
    dialog_visible: bool = False
    dialog_kind: SystemDialogKind = SystemDialogKind.UNKNOWN
    action_policy: SystemDialogActionPolicy = SystemDialogActionPolicy.MANUAL_REVIEW
    action_control_visible: bool = False
    action_control_enabled: bool = False
    action_semantic_target: str | None = None
    stable_page_reached: bool = False
    page_state: str | None = None
    evidence_artifact_ids: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        for field_name in ("task_id", "session_id", "source_capture_id"):
            _require_text(getattr(self, field_name), field_name)
        if not isinstance(self.surface_kind, SurfaceKind):
            raise SystemDialogPhaseValidationError("surface_kind 不受支持")
        if not isinstance(self.dialog_kind, SystemDialogKind):
            raise SystemDialogPhaseValidationError("dialog_kind 不受支持")
        if not isinstance(self.action_policy, SystemDialogActionPolicy):
            raise SystemDialogPhaseValidationError("action_policy 不受支持")
        try:
            require_aware_datetime(self.observed_at, "observed_at")
        except ProtocolContractError as exc:
            raise SystemDialogPhaseValidationError(str(exc)) from exc
        if self.page_state is not None:
            _require_text(self.page_state, "page_state")
        for field_name in (
            "dialog_visible",
            "action_control_visible",
            "action_control_enabled",
            "stable_page_reached",
        ):
            if not isinstance(getattr(self, field_name), bool):
                raise SystemDialogPhaseValidationError(f"{field_name} 必须为布尔值")
        if self.action_semantic_target is not None:
            try:
                validate_semantic_target(self.action_semantic_target, "action_semantic_target")
            except InteractionValidationError as exc:
                raise SystemDialogPhaseValidationError(str(exc)) from exc
        # 底层页型稳定但系统弹窗仍在：优先清障，不得炸探索。
        if self.stable_page_reached and (
            self.dialog_visible
            or (self.page_state or "").casefold()
            in {"system_dialog", "permission_dialog", "browser_permission"}
        ):
            object.__setattr__(self, "stable_page_reached", False)
        artifacts = _unique_text_tuple(self.evidence_artifact_ids, "evidence_artifact_ids")
        object.__setattr__(self, "evidence_artifact_ids", artifacts)

    @property
    def is_system_dialog_surface(self) -> bool:
        return (
            self.dialog_visible
            or self.page_state in {"system_dialog", "permission_dialog", "browser_permission"}
        )


@dataclass(frozen=True, kw_only=True)
class SystemDialogNextAction:
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
            raise SystemDialogPhaseValidationError("action_type 不受支持")
        if self.semantic_target is not None:
            try:
                validate_semantic_target(self.semantic_target, "semantic_target")
            except InteractionValidationError as exc:
                raise SystemDialogPhaseValidationError(str(exc)) from exc
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
class SystemDialogDecision:
    """W20 阶段机对当前 Capture 的收口或下一步动作决策。"""

    decision_id: str
    phase_run_id: str
    status: SystemDialogDecisionStatus
    reason_code: str
    source_capture_id: str
    next_action: SystemDialogNextAction | None = None
    evidence_artifact_ids: tuple[str, ...] = field(default_factory=tuple)
    protocol_version: str = SYSTEM_DIALOG_PHASE_PROTOCOL_VERSION

    def __post_init__(self) -> None:
        for field_name in ("decision_id", "phase_run_id", "reason_code", "source_capture_id"):
            _require_text(getattr(self, field_name), field_name)
        if self.protocol_version != SYSTEM_DIALOG_PHASE_PROTOCOL_VERSION:
            raise SystemDialogPhaseValidationError("SystemDialogDecision 版本不受支持")
        if not isinstance(self.status, SystemDialogDecisionStatus):
            raise SystemDialogPhaseValidationError("status 不受支持")
        if self.next_action is not None and not isinstance(
            self.next_action,
            SystemDialogNextAction,
        ):
            raise SystemDialogPhaseValidationError("next_action 类型不正确")
        terminal_statuses = {
            SystemDialogDecisionStatus.HANDLED,
            SystemDialogDecisionStatus.NOT_PRESENT,
            SystemDialogDecisionStatus.BLOCKED,
        }
        if self.status not in terminal_statuses and self.next_action is None:
            raise SystemDialogPhaseValidationError("非终态阶段必须给出 next_action")
        if self.status in terminal_statuses and self.next_action is not None:
            raise SystemDialogPhaseValidationError("终态阶段不能继续给出 next_action")
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


def evaluate_system_dialog_phase(
    *,
    context: SystemDialogPhaseContext,
    observation: SystemDialogObservation,
) -> SystemDialogDecision:
    """根据当前页面事实输出系统弹窗阶段的下一步动作或终态。"""

    if context.task_id != observation.task_id or context.session_id != observation.session_id:
        raise SystemDialogPhaseValidationError("阶段上下文与页面观测不属于同一任务会话")

    if observation.stable_page_reached and not observation.is_system_dialog_surface:
        return _decision(
            context=context,
            observation=observation,
            status=SystemDialogDecisionStatus.HANDLED,
            reason_code=(
                "system_dialog_handled"
                if context.handled_dialog_count > 0
                else "system_dialog_not_present"
            ),
        )

    if not observation.is_system_dialog_surface:
        return _decision(
            context=context,
            observation=observation,
            status=SystemDialogDecisionStatus.NOT_PRESENT,
            reason_code="system_dialog_not_present",
        )

    if context.handled_dialog_count >= context.max_system_dialogs:
        return _decision(
            context=context,
            observation=observation,
            status=SystemDialogDecisionStatus.BLOCKED,
            reason_code="system_dialog_limit_exceeded",
        )

    if observation.action_policy is SystemDialogActionPolicy.MANUAL_REVIEW:
        return _decision(
            context=context,
            observation=observation,
            status=SystemDialogDecisionStatus.BLOCKED,
            reason_code="system_dialog_requires_manual_review",
        )

    if observation.action_control_visible and observation.action_control_enabled:
        if observation.action_semantic_target is None:
            return _decision(
                context=context,
                observation=observation,
                status=SystemDialogDecisionStatus.BLOCKED,
                reason_code="system_dialog_action_target_missing",
            )
        return _decision(
            context=context,
            observation=observation,
            status=SystemDialogDecisionStatus.READY_TO_HANDLE,
            reason_code=f"{observation.action_policy.value}_action_ready",
            next_action=_handle_action(context=context, observation=observation),
        )

    if context.reobserve_count >= context.max_reobservations:
        return _decision(
            context=context,
            observation=observation,
            status=SystemDialogDecisionStatus.BLOCKED,
            reason_code="system_dialog_reobserve_limit_exceeded",
        )

    return _decision(
        context=context,
        observation=observation,
        status=SystemDialogDecisionStatus.VERIFYING_HANDLE,
        reason_code="system_dialog_reobserve_required",
        next_action=_timed_capture_action(context=context, observation=observation),
    )


def _handle_action(
    *,
    context: SystemDialogPhaseContext,
    observation: SystemDialogObservation,
) -> SystemDialogNextAction:
    operation = (
        InteractionOperation.CLICK
        if observation.surface_kind is SurfaceKind.WEB
        else InteractionOperation.TAP
    )
    target = observation.action_semantic_target
    if target is None:
        raise SystemDialogPhaseValidationError("action_semantic_target 缺失")
    return SystemDialogNextAction(
        action_id=_action_id(
            context=context,
            observation=observation,
            action_type=operation,
            reason_code=f"{observation.action_policy.value}_action_ready",
            semantic_target=target,
        ),
        action_type=operation,
        source_capture_id=observation.source_capture_id,
        purpose="处理系统弹窗并重新采集",
        semantic_target=target,
        capture_roles=("system_dialog_before_action", "system_dialog_after_action"),
    )


def _timed_capture_action(
    *,
    context: SystemDialogPhaseContext,
    observation: SystemDialogObservation,
) -> SystemDialogNextAction:
    return SystemDialogNextAction(
        action_id=_action_id(
            context=context,
            observation=observation,
            action_type=InteractionOperation.TIMED_CAPTURE,
            reason_code="system_dialog_reobserve_required",
            delay_ms=500,
        ),
        action_type=InteractionOperation.TIMED_CAPTURE,
        source_capture_id=observation.source_capture_id,
        purpose="等待系统弹窗状态变化并重新观察",
        delay_ms=500,
        capture_roles=("system_dialog_reobserve",),
    )


def _decision(
    *,
    context: SystemDialogPhaseContext,
    observation: SystemDialogObservation,
    status: SystemDialogDecisionStatus,
    reason_code: str,
    next_action: SystemDialogNextAction | None = None,
) -> SystemDialogDecision:
    decision_id = "system-dialog-decision:" + stable_protocol_hash(
        {
            "phase_run_id": context.phase_run_id,
            "source_capture_id": observation.source_capture_id,
            "status": status.value,
            "reason_code": reason_code,
            "handled_dialog_count": context.handled_dialog_count,
            "reobserve_count": context.reobserve_count,
            "dialog_kind": observation.dialog_kind.value,
            "action_policy": observation.action_policy.value,
            "next_action": next_action.to_dict() if next_action else None,
        }
    )
    return SystemDialogDecision(
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
    context: SystemDialogPhaseContext,
    observation: SystemDialogObservation,
    action_type: InteractionOperation,
    reason_code: str,
    semantic_target: str | None = None,
    delay_ms: int | None = None,
) -> str:
    return "system-dialog-action:" + stable_protocol_hash(
        {
            "phase_run_id": context.phase_run_id,
            "source_capture_id": observation.source_capture_id,
            "action_type": action_type.value,
            "reason_code": reason_code,
            "semantic_target": semantic_target,
            "delay_ms": delay_ms,
            "handled_dialog_count": context.handled_dialog_count,
            "reobserve_count": context.reobserve_count,
            "dialog_kind": observation.dialog_kind.value,
            "action_policy": observation.action_policy.value,
        }
    )


def _require_text(value: Any, field_name: str) -> str:
    try:
        return require_text(value, field_name)
    except ProtocolContractError as exc:
        raise SystemDialogPhaseValidationError(str(exc)) from exc


def _require_non_negative_int(value: Any, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise SystemDialogPhaseValidationError(f"{field_name} 必须为非负整数")
    return value


def _require_positive_int(value: Any, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise SystemDialogPhaseValidationError(f"{field_name} 必须为正整数")
    return value


def _unique_text_tuple(values: tuple[str, ...], field_name: str) -> tuple[str, ...]:
    if not isinstance(values, tuple):
        raise SystemDialogPhaseValidationError(f"{field_name} 必须为数组")
    normalized = tuple(_require_text(value, field_name) for value in values)
    if len(normalized) != len(set(normalized)):
        raise SystemDialogPhaseValidationError(f"{field_name} 不能包含重复值")
    return normalized
