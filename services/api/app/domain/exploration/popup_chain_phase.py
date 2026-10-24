"""连续弹窗阶段机的纯领域合同。"""

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

POPUP_CHAIN_PHASE_PROTOCOL_VERSION = "popup-chain-phase.v1"
# 弹窗广告（Android）：满 1 张截图证据就必须点底部导航栏最右侧「<」。
POPUP_AD_CLOUD_BACK_AFTER_EVIDENCE_FRAMES = 1
_DEFAULT_POPUP_CLOUD_BACK_SEMANTIC_TARGET = "底部导航栏「<」关闭弹窗"


class PopupChainPhaseValidationError(ValueError):
    """连续弹窗阶段机输入或输出不满足稳定合同。"""


class PopupChainDecisionStatus(StrEnum):
    """连续弹窗阶段机的判定状态。"""

    READY_TO_CLOSE = "ready_to_close"
    VERIFYING_CLOSE = "verifying_close"
    COMPLETED = "completed"
    BLOCKED = "blocked"


@dataclass(frozen=True, kw_only=True)
class PopupChainPhaseContext:
    """一次连续弹窗阶段的可恢复上下文。"""

    task_id: str
    session_id: str
    phase_run_id: str
    closed_popup_count: int = 0
    reobserve_count: int = 0
    same_popup_attempt_count: int = 0
    max_popup_layers: int = 30
    max_reobservations: int = 6
    max_same_popup_attempts: int = 2

    def __post_init__(self) -> None:
        for field_name in ("task_id", "session_id", "phase_run_id"):
            _require_text(getattr(self, field_name), field_name)
        _require_non_negative_int(self.closed_popup_count, "closed_popup_count")
        _require_non_negative_int(self.reobserve_count, "reobserve_count")
        _require_non_negative_int(self.same_popup_attempt_count, "same_popup_attempt_count")
        _require_positive_int(self.max_popup_layers, "max_popup_layers")
        _require_positive_int(self.max_reobservations, "max_reobservations")
        _require_positive_int(self.max_same_popup_attempts, "max_same_popup_attempts")


@dataclass(frozen=True, kw_only=True)
class PopupChainObservation:
    """Interaction Runtime 采集后供 W18 阶段机消费的当前页面事实。"""

    task_id: str
    session_id: str
    source_capture_id: str
    surface_kind: SurfaceKind
    observed_at: datetime
    page_type: str | None = None
    page_state: str | None = None
    popup_visible: bool = False
    top_popup_signature: str | None = None
    close_control_visible: bool = False
    close_control_enabled: bool = False
    close_semantic_target: str | None = None
    stable_page_reached: bool = False
    evidence_artifact_ids: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        for field_name in ("task_id", "session_id", "source_capture_id"):
            _require_text(getattr(self, field_name), field_name)
        if not isinstance(self.surface_kind, SurfaceKind):
            raise PopupChainPhaseValidationError("surface_kind 不受支持")
        try:
            require_aware_datetime(self.observed_at, "observed_at")
        except ProtocolContractError as exc:
            raise PopupChainPhaseValidationError(str(exc)) from exc
        if self.page_type is not None:
            _require_text(self.page_type, "page_type")
        if self.page_state is not None:
            _require_text(self.page_state, "page_state")
        if self.top_popup_signature is not None:
            _require_text(self.top_popup_signature, "top_popup_signature")
        if self.close_semantic_target is not None:
            try:
                validate_semantic_target(self.close_semantic_target, "close_semantic_target")
            except InteractionValidationError as exc:
                raise PopupChainPhaseValidationError(str(exc)) from exc
        for field_name in (
            "popup_visible",
            "close_control_visible",
            "close_control_enabled",
            "stable_page_reached",
        ):
            if not isinstance(getattr(self, field_name), bool):
                raise PopupChainPhaseValidationError(f"{field_name} 必须为布尔值")
        # Vision 常把底层业务页标成 stable，同时仍带 popup_visible／overlay。
        # 有遮挡时优先清障：降级 stable，禁止抛错炸掉整场探索。
        if self.stable_page_reached and (
            self.popup_visible
            or self.top_popup_signature is not None
            or (self.page_state or "").casefold() in {"popup", "modal", "dialog", "overlay"}
        ):
            object.__setattr__(self, "stable_page_reached", False)
        artifacts = _unique_text_tuple(self.evidence_artifact_ids, "evidence_artifact_ids")
        object.__setattr__(self, "evidence_artifact_ids", artifacts)

    @property
    def is_popup_surface(self) -> bool:
        return (
            self.popup_visible
            or (self.page_state or "").casefold() in {"popup", "modal", "dialog", "overlay"}
            or self.top_popup_signature is not None
        )


@dataclass(frozen=True, kw_only=True)
class PopupChainNextAction:
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
            raise PopupChainPhaseValidationError("action_type 不受支持")
        if self.semantic_target is not None:
            try:
                validate_semantic_target(self.semantic_target, "semantic_target")
            except InteractionValidationError as exc:
                raise PopupChainPhaseValidationError(str(exc)) from exc
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
class PopupChainDecision:
    """W18 阶段机对当前 Capture 的收口或下一步动作决策。"""

    decision_id: str
    phase_run_id: str
    status: PopupChainDecisionStatus
    reason_code: str
    source_capture_id: str
    next_action: PopupChainNextAction | None = None
    evidence_artifact_ids: tuple[str, ...] = field(default_factory=tuple)
    protocol_version: str = POPUP_CHAIN_PHASE_PROTOCOL_VERSION
    unclosable_protocol: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        for field_name in ("decision_id", "phase_run_id", "reason_code", "source_capture_id"):
            _require_text(getattr(self, field_name), field_name)
        if self.protocol_version != POPUP_CHAIN_PHASE_PROTOCOL_VERSION:
            raise PopupChainPhaseValidationError("PopupChainDecision 版本不受支持")
        if not isinstance(self.status, PopupChainDecisionStatus):
            raise PopupChainPhaseValidationError("status 不受支持")
        if self.next_action is not None and not isinstance(
            self.next_action,
            PopupChainNextAction,
        ):
            raise PopupChainPhaseValidationError("next_action 类型不正确")
        if self.status in {
            PopupChainDecisionStatus.READY_TO_CLOSE,
            PopupChainDecisionStatus.VERIFYING_CLOSE,
        } and self.next_action is None:
            raise PopupChainPhaseValidationError("非终态阶段必须给出 next_action")
        if self.status in {
            PopupChainDecisionStatus.COMPLETED,
            PopupChainDecisionStatus.BLOCKED,
        } and self.next_action is not None:
            raise PopupChainPhaseValidationError("终态阶段不能继续给出 next_action")
        if self.unclosable_protocol is not None and not isinstance(
            self.unclosable_protocol, dict
        ):
            raise PopupChainPhaseValidationError("unclosable_protocol 必须为对象")
        artifacts = _unique_text_tuple(self.evidence_artifact_ids, "evidence_artifact_ids")
        object.__setattr__(self, "evidence_artifact_ids", artifacts)

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "protocol_version": self.protocol_version,
            "decision_id": self.decision_id,
            "phase_run_id": self.phase_run_id,
            "status": self.status.value,
            "reason_code": self.reason_code,
            "source_capture_id": self.source_capture_id,
            "next_action": self.next_action.to_dict() if self.next_action else None,
            "evidence_artifact_ids": list(self.evidence_artifact_ids),
        }
        if self.unclosable_protocol is not None:
            payload["unclosable_protocol"] = dict(self.unclosable_protocol)
        return payload


def evaluate_popup_chain_phase(
    *,
    context: PopupChainPhaseContext,
    observation: PopupChainObservation,
) -> PopupChainDecision:
    """根据当前页面事实输出连续弹窗阶段的下一步动作或终态。"""

    if context.task_id != observation.task_id or context.session_id != observation.session_id:
        raise PopupChainPhaseValidationError("阶段上下文与页面观测不属于同一任务会话")

    if observation.stable_page_reached and not observation.is_popup_surface:
        return _decision(
            context=context,
            observation=observation,
            status=PopupChainDecisionStatus.COMPLETED,
            reason_code=(
                "popup_chain_cleared"
                if context.closed_popup_count > 0
                else "popup_chain_not_present"
            ),
        )

    if not observation.is_popup_surface:
        return _decision(
            context=context,
            observation=observation,
            status=PopupChainDecisionStatus.COMPLETED,
            reason_code="popup_chain_not_present",
        )

    if context.closed_popup_count >= context.max_popup_layers:
        return _blocked(
            context=context,
            observation=observation,
            reason_code="popup_layer_limit_exceeded",
        )

    if context.same_popup_attempt_count >= context.max_same_popup_attempts:
        return _blocked(
            context=context,
            observation=observation,
            reason_code="popup_close_no_progress",
        )

    evidence_frames = _popup_ad_evidence_frame_count(
        context=context,
        observation=observation,
    )
    # A／公告：有文案或可见关闭控件时优先点（cascade A/B），不再强行 cloud_back_twice。
    if (
        observation.close_control_visible
        and observation.close_control_enabled
        and _is_notice_ack_close_target(observation.close_semantic_target)
    ):
        return _decision(
            context=context,
            observation=observation,
            status=PopupChainDecisionStatus.READY_TO_CLOSE,
            reason_code="notice_ack_ready",
            next_action=_close_action(context=context, observation=observation),
        )

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
            status=PopupChainDecisionStatus.READY_TO_CLOSE,
            reason_code="close_control_ready",
            next_action=_close_action(context=context, observation=observation),
        )

    # 无文案关闭控件：先重采绑定像素／几何下方圆 X（cascade B），
    # 勿首帧直接 D 档连点「<」导致永远点不到 X。
    if (
        observation.surface_kind is SurfaceKind.ANDROID
        and evidence_frames >= POPUP_AD_CLOUD_BACK_AFTER_EVIDENCE_FRAMES
        and context.reobserve_count >= 1
    ):
        return _decision(
            context=context,
            observation=observation,
            status=PopupChainDecisionStatus.READY_TO_CLOSE,
            reason_code="popup_one_frame_cloud_back_twice_ready",
            next_action=_cloud_back_close_action(
                context=context,
                observation=observation,
            ),
        )

    if context.reobserve_count >= context.max_reobservations:
        return _blocked(
            context=context,
            observation=observation,
            reason_code="popup_reobserve_limit_exceeded",
        )

    return _decision(
        context=context,
        observation=observation,
        status=PopupChainDecisionStatus.VERIFYING_CLOSE,
        reason_code="popup_reobserve_required",
        next_action=_timed_capture_action(context=context, observation=observation),
    )


def _is_notice_ack_close_target(semantic_target: str | None) -> bool:
    """官方公告确认钮（我知道了等），与广告圆 ×／cloud_back 拆开。"""

    try:
        from services.api.app.domain.app_product.popup_close import (
            is_notice_ack_semantic_target,
        )
    except Exception:  # noqa: BLE001
        blob = str(semantic_target or "")
        return "我知道了" in blob or "知道了" in blob
    return is_notice_ack_semantic_target(semantic_target)


def _popup_ad_evidence_frame_count(
    *,
    context: PopupChainPhaseContext,
    observation: PopupChainObservation,
) -> int:
    """当前弹窗阶段已有截图证据帧数（含本帧）。"""

    by_artifacts = len(observation.evidence_artifact_ids)
    by_counter = context.reobserve_count + 1
    return max(by_artifacts, by_counter)


def _cloud_back_close_action(
    *,
    context: PopupChainPhaseContext,
    observation: PopupChainObservation,
) -> PopupChainNextAction:
    """Android 弹窗 D 档：系统导航栏最右侧「<」连点两下（cloud_back_tap_twice）。"""

    target = _DEFAULT_POPUP_CLOUD_BACK_SEMANTIC_TARGET
    return PopupChainNextAction(
        action_id=_action_id(
            context=context,
            observation=observation,
            action_type=InteractionOperation.BACK,
            reason_code="popup_one_frame_cloud_back_twice_ready",
            semantic_target=target,
        ),
        action_type=InteractionOperation.BACK,
        source_capture_id=observation.source_capture_id,
        semantic_target=target,
        purpose=(
            "弹窗广告仍在且无文案/几何X/蒙层可用：连点两下底部导航栏最右侧「<」关窗"
            "（cloud_back_tap_twice；中间禁止截图；禁止当开屏点进入／禁止keyevent4）"
        ),
        capture_roles=("popup_before_close", "popup_after_close"),
    )


def _close_action(
    *,
    context: PopupChainPhaseContext,
    observation: PopupChainObservation,
) -> PopupChainNextAction:
    operation = (
        InteractionOperation.CLICK
        if observation.surface_kind is SurfaceKind.WEB
        else InteractionOperation.TAP
    )
    target = observation.close_semantic_target
    if target is None:
        raise PopupChainPhaseValidationError("close_semantic_target 缺失")
    return PopupChainNextAction(
        action_id=_action_id(
            context=context,
            observation=observation,
            action_type=operation,
            reason_code="close_control_ready",
            semantic_target=target,
        ),
        action_type=operation,
        source_capture_id=observation.source_capture_id,
        semantic_target=target,
        purpose="关闭当前最上层弹窗并重新采集",
        capture_roles=("popup_before_close", "popup_after_close"),
    )


def _timed_capture_action(
    *,
    context: PopupChainPhaseContext,
    observation: PopupChainObservation,
) -> PopupChainNextAction:
    return PopupChainNextAction(
        action_id=_action_id(
            context=context,
            observation=observation,
            action_type=InteractionOperation.TIMED_CAPTURE,
            reason_code="popup_reobserve_required",
            delay_ms=500,
        ),
        action_type=InteractionOperation.TIMED_CAPTURE,
        source_capture_id=observation.source_capture_id,
        purpose="等待弹窗状态变化并重新观察",
        delay_ms=500,
        capture_roles=("popup_reobserve",),
    )


def _blocked(
    *,
    context: PopupChainPhaseContext,
    observation: PopupChainObservation,
    reason_code: str,
) -> PopupChainDecision:
    return _decision(
        context=context,
        observation=observation,
        status=PopupChainDecisionStatus.BLOCKED,
        reason_code=reason_code,
        unclosable_protocol=build_popup_unclosable_protocol(
            context=context,
            observation=observation,
            reason_code=reason_code,
        ),
    )


def build_popup_unclosable_protocol(
    *,
    context: PopupChainPhaseContext,
    observation: PopupChainObservation,
    reason_code: str,
) -> dict[str, Any]:
    """关不掉时补全失败协议：成对 before/after 角色 + 嵌套广告墙审计。"""

    return {
        "protocol_version": "app-popup-unclosable.v1",
        "reason_code": reason_code,
        "source_capture_id": observation.source_capture_id,
        "top_popup_signature": observation.top_popup_signature,
        "closed_popup_count": context.closed_popup_count,
        "max_popup_layers": context.max_popup_layers,
        "same_popup_attempt_count": context.same_popup_attempt_count,
        "reobserve_count": context.reobserve_count,
        "evidence_capture_roles": {
            "before": "popup_before_close",
            "primary": "popup_after_close",
        },
        "nested_ad_queue": True,
        "close_succeeded": False,
        "evidence_artifact_ids": list(observation.evidence_artifact_ids),
    }


def _decision(
    *,
    context: PopupChainPhaseContext,
    observation: PopupChainObservation,
    status: PopupChainDecisionStatus,
    reason_code: str,
    next_action: PopupChainNextAction | None = None,
    unclosable_protocol: dict[str, Any] | None = None,
) -> PopupChainDecision:
    decision_id = "popup-chain-decision:" + stable_protocol_hash(
        {
            "phase_run_id": context.phase_run_id,
            "source_capture_id": observation.source_capture_id,
            "status": status.value,
            "reason_code": reason_code,
            "closed_popup_count": context.closed_popup_count,
            "reobserve_count": context.reobserve_count,
            "same_popup_attempt_count": context.same_popup_attempt_count,
            "top_popup_signature": observation.top_popup_signature,
            "next_action": next_action.to_dict() if next_action else None,
            "unclosable_protocol": unclosable_protocol,
        }
    )
    return PopupChainDecision(
        decision_id=decision_id,
        phase_run_id=context.phase_run_id,
        status=status,
        reason_code=reason_code,
        source_capture_id=observation.source_capture_id,
        next_action=next_action,
        evidence_artifact_ids=observation.evidence_artifact_ids,
        unclosable_protocol=unclosable_protocol,
    )


def _action_id(
    *,
    context: PopupChainPhaseContext,
    observation: PopupChainObservation,
    action_type: InteractionOperation,
    reason_code: str,
    semantic_target: str | None = None,
    delay_ms: int | None = None,
) -> str:
    return "popup-chain-action:" + stable_protocol_hash(
        {
            "phase_run_id": context.phase_run_id,
            "source_capture_id": observation.source_capture_id,
            "action_type": action_type.value,
            "reason_code": reason_code,
            "semantic_target": semantic_target,
            "delay_ms": delay_ms,
            "closed_popup_count": context.closed_popup_count,
            "reobserve_count": context.reobserve_count,
            "same_popup_attempt_count": context.same_popup_attempt_count,
            "top_popup_signature": observation.top_popup_signature,
        }
    )


def _require_text(value: Any, field_name: str) -> str:
    try:
        return require_text(value, field_name)
    except ProtocolContractError as exc:
        raise PopupChainPhaseValidationError(str(exc)) from exc


def _require_non_negative_int(value: Any, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise PopupChainPhaseValidationError(f"{field_name} 必须为非负整数")
    return value


def _require_positive_int(value: Any, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise PopupChainPhaseValidationError(f"{field_name} 必须为正整数")
    return value


def _unique_text_tuple(values: tuple[str, ...], field_name: str) -> tuple[str, ...]:
    if not isinstance(values, tuple):
        raise PopupChainPhaseValidationError(f"{field_name} 必须为数组")
    normalized = tuple(_require_text(value, field_name) for value in values)
    if len(normalized) != len(set(normalized)):
        raise PopupChainPhaseValidationError(f"{field_name} 不能包含重复值")
    return normalized
