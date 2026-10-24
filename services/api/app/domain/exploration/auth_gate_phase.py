"""认证门禁阶段机的纯领域合同。"""

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

AUTH_GATE_PHASE_PROTOCOL_VERSION = "auth-gate-phase.v1"


class AuthGatePhaseValidationError(ValueError):
    """认证门禁阶段机输入或输出不满足稳定合同。"""


class AuthGateDecisionStatus(StrEnum):
    """认证门禁阶段机的判定状态。"""

    READY_TO_DISMISS = "ready_to_dismiss"
    VERIFYING_DISMISS = "verifying_dismiss"
    DISMISSED = "dismissed"
    REQUIRES_AUTH = "requires_auth"
    NOT_PRESENT = "not_present"
    BLOCKED = "blocked"


_TERMINAL_STATUSES = frozenset(
    {
        AuthGateDecisionStatus.DISMISSED,
        AuthGateDecisionStatus.REQUIRES_AUTH,
        AuthGateDecisionStatus.NOT_PRESENT,
        AuthGateDecisionStatus.BLOCKED,
    }
)


@dataclass(frozen=True, kw_only=True)
class AuthGatePhaseContext:
    """一次认证门禁阶段的可恢复上下文。"""

    task_id: str
    session_id: str
    phase_run_id: str
    dismiss_attempt_count: int = 0
    reobserve_count: int = 0
    max_dismiss_attempts: int = 2
    max_reobservations: int = 4

    def __post_init__(self) -> None:
        for field_name in ("task_id", "session_id", "phase_run_id"):
            _require_text(getattr(self, field_name), field_name)
        _require_non_negative_int(self.dismiss_attempt_count, "dismiss_attempt_count")
        _require_non_negative_int(self.reobserve_count, "reobserve_count")
        _require_positive_int(self.max_dismiss_attempts, "max_dismiss_attempts")
        _require_positive_int(self.max_reobservations, "max_reobservations")


@dataclass(frozen=True, kw_only=True)
class AuthGateObservation:
    """Interaction Runtime 采集后供 W19 阶段机消费的当前页面事实。"""

    task_id: str
    session_id: str
    source_capture_id: str
    surface_kind: SurfaceKind
    observed_at: datetime
    page_type: str | None = None
    page_state: str | None = None
    auth_gate_visible: bool = False
    login_form_visible: bool = False
    requires_auth: bool = False
    dismiss_control_visible: bool = False
    dismiss_control_enabled: bool = False
    dismiss_semantic_target: str | None = None
    guest_entry_visible: bool = False
    guest_entry_enabled: bool = False
    guest_entry_semantic_target: str | None = None
    stable_page_reached: bool = False
    evidence_artifact_ids: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        for field_name in ("task_id", "session_id", "source_capture_id"):
            _require_text(getattr(self, field_name), field_name)
        if not isinstance(self.surface_kind, SurfaceKind):
            raise AuthGatePhaseValidationError("surface_kind 不受支持")
        try:
            require_aware_datetime(self.observed_at, "observed_at")
        except ProtocolContractError as exc:
            raise AuthGatePhaseValidationError(str(exc)) from exc
        for field_name in ("page_type", "page_state"):
            value = getattr(self, field_name)
            if value is not None:
                _require_text(value, field_name)
        for field_name in (
            "auth_gate_visible",
            "login_form_visible",
            "requires_auth",
            "dismiss_control_visible",
            "dismiss_control_enabled",
            "guest_entry_visible",
            "guest_entry_enabled",
            "stable_page_reached",
        ):
            if not isinstance(getattr(self, field_name), bool):
                raise AuthGatePhaseValidationError(f"{field_name} 必须为布尔值")
        for field_name in ("dismiss_semantic_target", "guest_entry_semantic_target"):
            value = getattr(self, field_name)
            if value is not None:
                try:
                    validate_semantic_target(value, field_name)
                except InteractionValidationError as exc:
                    raise AuthGatePhaseValidationError(str(exc)) from exc
        # 底层页型稳定但认证门禁仍在：优先清障，不得炸探索。
        if self.stable_page_reached and (
            self.auth_gate_visible
            or self.login_form_visible
            or self.requires_auth
            or (self.page_state or "").casefold()
            in {"auth_gate", "login_required", "requires_auth"}
        ):
            object.__setattr__(self, "stable_page_reached", False)
        artifacts = _unique_text_tuple(self.evidence_artifact_ids, "evidence_artifact_ids")
        object.__setattr__(self, "evidence_artifact_ids", artifacts)

    @property
    def is_auth_gate_surface(self) -> bool:
        return (
            self.auth_gate_visible
            or self.login_form_visible
            or self.requires_auth
            or self.page_state in {"auth_gate", "login_required", "requires_auth"}
        )


@dataclass(frozen=True, kw_only=True)
class AuthGateNextAction:
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
            raise AuthGatePhaseValidationError("action_type 不受支持")
        if self.semantic_target is not None:
            try:
                validate_semantic_target(self.semantic_target, "semantic_target")
            except InteractionValidationError as exc:
                raise AuthGatePhaseValidationError(str(exc)) from exc
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
class AuthGateDecision:
    """W19 阶段机对当前 Capture 的收口或下一步动作决策。"""

    decision_id: str
    phase_run_id: str
    status: AuthGateDecisionStatus
    reason_code: str
    source_capture_id: str
    next_action: AuthGateNextAction | None = None
    evidence_artifact_ids: tuple[str, ...] = field(default_factory=tuple)
    protocol_version: str = AUTH_GATE_PHASE_PROTOCOL_VERSION

    def __post_init__(self) -> None:
        for field_name in ("decision_id", "phase_run_id", "reason_code", "source_capture_id"):
            _require_text(getattr(self, field_name), field_name)
        if self.protocol_version != AUTH_GATE_PHASE_PROTOCOL_VERSION:
            raise AuthGatePhaseValidationError("AuthGateDecision 版本不受支持")
        if not isinstance(self.status, AuthGateDecisionStatus):
            raise AuthGatePhaseValidationError("status 不受支持")
        if self.next_action is not None and not isinstance(self.next_action, AuthGateNextAction):
            raise AuthGatePhaseValidationError("next_action 类型不正确")
        if self.status not in _TERMINAL_STATUSES and self.next_action is None:
            raise AuthGatePhaseValidationError("非终态阶段必须给出 next_action")
        if self.status in _TERMINAL_STATUSES and self.next_action is not None:
            raise AuthGatePhaseValidationError("终态阶段不能继续给出 next_action")
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


def evaluate_auth_gate_phase(
    *,
    context: AuthGatePhaseContext,
    observation: AuthGateObservation,
) -> AuthGateDecision:
    """根据当前页面事实输出认证门禁阶段的下一步动作或终态。"""

    if context.task_id != observation.task_id or context.session_id != observation.session_id:
        raise AuthGatePhaseValidationError("阶段上下文与页面观测不属于同一任务会话")

    if observation.stable_page_reached and not observation.is_auth_gate_surface:
        return _decision(
            context=context,
            observation=observation,
            status=AuthGateDecisionStatus.DISMISSED,
            reason_code="auth_gate_dismissed",
        )

    if not observation.is_auth_gate_surface:
        return _decision(
            context=context,
            observation=observation,
            status=AuthGateDecisionStatus.NOT_PRESENT,
            reason_code="auth_gate_not_present",
        )

    if context.dismiss_attempt_count >= context.max_dismiss_attempts:
        return _decision(
            context=context,
            observation=observation,
            status=AuthGateDecisionStatus.REQUIRES_AUTH,
            reason_code="dismiss_attempt_limit_exceeded",
        )

    if (
        observation.dismiss_control_visible
        and observation.dismiss_control_enabled
        and observation.dismiss_semantic_target is None
    ):
        return _decision(
            context=context,
            observation=observation,
            status=AuthGateDecisionStatus.BLOCKED,
            reason_code="dismiss_control_target_missing",
        )
    if (
        observation.guest_entry_visible
        and observation.guest_entry_enabled
        and observation.guest_entry_semantic_target is None
    ):
        return _decision(
            context=context,
            observation=observation,
            status=AuthGateDecisionStatus.BLOCKED,
            reason_code="guest_entry_target_missing",
        )

    dismiss_target = _dismiss_target(observation)
    if dismiss_target is not None:
        return _decision(
            context=context,
            observation=observation,
            status=AuthGateDecisionStatus.READY_TO_DISMISS,
            reason_code=dismiss_target.reason_code,
            next_action=_dismiss_action(
                context=context,
                observation=observation,
                target=dismiss_target.semantic_target,
                reason_code=dismiss_target.reason_code,
            ),
        )

    if observation.requires_auth or observation.login_form_visible:
        return _decision(
            context=context,
            observation=observation,
            status=AuthGateDecisionStatus.REQUIRES_AUTH,
            reason_code="credentials_required",
        )

    if context.reobserve_count >= context.max_reobservations:
        return _decision(
            context=context,
            observation=observation,
            status=AuthGateDecisionStatus.BLOCKED,
            reason_code="auth_gate_reobserve_limit_exceeded",
        )

    return _decision(
        context=context,
        observation=observation,
        status=AuthGateDecisionStatus.VERIFYING_DISMISS,
        reason_code="auth_gate_reobserve_required",
        next_action=_timed_capture_action(context=context, observation=observation),
    )


@dataclass(frozen=True, kw_only=True)
class _DismissTarget:
    semantic_target: str
    reason_code: str


def _dismiss_target(observation: AuthGateObservation) -> _DismissTarget | None:
    if observation.dismiss_control_visible and observation.dismiss_control_enabled:
        return _DismissTarget(
            semantic_target=str(observation.dismiss_semantic_target),
            reason_code="dismiss_control_ready",
        )
    if observation.guest_entry_visible and observation.guest_entry_enabled:
        return _DismissTarget(
            semantic_target=str(observation.guest_entry_semantic_target),
            reason_code="guest_entry_ready",
        )
    return None


def _dismiss_action(
    *,
    context: AuthGatePhaseContext,
    observation: AuthGateObservation,
    target: str,
    reason_code: str,
) -> AuthGateNextAction:
    operation = (
        InteractionOperation.CLICK
        if observation.surface_kind is SurfaceKind.WEB
        else InteractionOperation.TAP
    )
    return AuthGateNextAction(
        action_id=_action_id(
            context=context,
            observation=observation,
            action_type=operation,
            reason_code=reason_code,
            semantic_target=target,
        ),
        action_type=operation,
        source_capture_id=observation.source_capture_id,
        purpose="关闭或绕过认证门禁并重新采集",
        semantic_target=target,
        capture_roles=("auth_gate_before_dismiss", "auth_gate_after_dismiss"),
    )


def _timed_capture_action(
    *,
    context: AuthGatePhaseContext,
    observation: AuthGateObservation,
) -> AuthGateNextAction:
    return AuthGateNextAction(
        action_id=_action_id(
            context=context,
            observation=observation,
            action_type=InteractionOperation.TIMED_CAPTURE,
            reason_code="auth_gate_reobserve_required",
            delay_ms=500,
        ),
        action_type=InteractionOperation.TIMED_CAPTURE,
        source_capture_id=observation.source_capture_id,
        purpose="等待认证门禁状态变化并重新观察",
        delay_ms=500,
        capture_roles=("auth_gate_reobserve",),
    )


def _decision(
    *,
    context: AuthGatePhaseContext,
    observation: AuthGateObservation,
    status: AuthGateDecisionStatus,
    reason_code: str,
    next_action: AuthGateNextAction | None = None,
) -> AuthGateDecision:
    decision_id = "auth-gate-decision:" + stable_protocol_hash(
        {
            "phase_run_id": context.phase_run_id,
            "source_capture_id": observation.source_capture_id,
            "status": status.value,
            "reason_code": reason_code,
            "dismiss_attempt_count": context.dismiss_attempt_count,
            "reobserve_count": context.reobserve_count,
            "next_action": next_action.to_dict() if next_action else None,
        }
    )
    return AuthGateDecision(
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
    context: AuthGatePhaseContext,
    observation: AuthGateObservation,
    action_type: InteractionOperation,
    reason_code: str,
    semantic_target: str | None = None,
    delay_ms: int | None = None,
) -> str:
    return "auth-gate-action:" + stable_protocol_hash(
        {
            "phase_run_id": context.phase_run_id,
            "source_capture_id": observation.source_capture_id,
            "action_type": action_type.value,
            "reason_code": reason_code,
            "semantic_target": semantic_target,
            "delay_ms": delay_ms,
            "dismiss_attempt_count": context.dismiss_attempt_count,
            "reobserve_count": context.reobserve_count,
        }
    )


def _require_text(value: Any, field_name: str) -> str:
    try:
        return require_text(value, field_name)
    except ProtocolContractError as exc:
        raise AuthGatePhaseValidationError(str(exc)) from exc


def _require_non_negative_int(value: Any, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise AuthGatePhaseValidationError(f"{field_name} 必须为非负整数")
    return value


def _require_positive_int(value: Any, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise AuthGatePhaseValidationError(f"{field_name} 必须为正整数")
    return value


def _unique_text_tuple(values: tuple[str, ...], field_name: str) -> tuple[str, ...]:
    if not isinstance(values, tuple):
        raise AuthGatePhaseValidationError(f"{field_name} 必须为数组")
    normalized = tuple(_require_text(value, field_name) for value in values)
    if len(normalized) != len(set(normalized)):
        raise AuthGatePhaseValidationError(f"{field_name} 不能包含重复值")
    return normalized
