"""Capture Coordinator 的应用层请求、端口与结构化错误。"""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol

from services.api.app.application.surface_sessions import SessionCommandPermit
from services.api.app.domain.exploration import EvidenceExitGateDecision
from services.api.app.domain.interaction import (
    ActionIntent,
    CaptureDirective,
    CaptureRef,
    InteractionResult,
    SurfaceKind,
)


class CapturePhase(StrEnum):
    """Coordinator 发起采集时的审计阶段。"""

    BEFORE_ACTION = "before_action"
    AFTER_ACTION = "after_action"
    OBSERVATION = "observation"


class CaptureCoordinationStage(StrEnum):
    """失败发生的位置。"""

    BEFORE_CAPTURE = "before_capture"
    ACTION = "action"
    AFTER_CAPTURE = "after_capture"


@dataclass(frozen=True, kw_only=True)
class CaptureCoordinationCommand:
    """协调一次 Action Intent 所需的内部 Run 身份。"""

    agent_run_id: str
    intent: ActionIntent
    allow_draining: bool = False

    def __post_init__(self) -> None:
        _require_text(self.agent_run_id, "agent_run_id")
        if not isinstance(self.intent, ActionIntent):
            raise ValueError("intent 必须为 ActionIntent")
        if not isinstance(self.allow_draining, bool):
            raise ValueError("allow_draining 必须为布尔值")


@dataclass(frozen=True, kw_only=True)
class CaptureWriteRequest:
    """Capture Writer 可消费、但不暴露 Driver 的采集请求。"""

    task_id: str
    agent_run_id: str
    session_id: str
    surface_kind: SurfaceKind
    intent_id: str
    phase: CapturePhase
    directive: CaptureDirective
    parent_capture_id: str | None
    action_execution_id: str | None
    session_command_sequence: int | None
    finalization_epoch: int | None
    deadline: datetime
    # 排期第 13 列：App Action Capture 写回必须携带 Session Lease 身份；Web 可忽略。
    lease_owner: str | None = field(default=None, repr=False, compare=False)
    lease_token: str | None = field(default=None, repr=False, compare=False)
    observation_context: Mapping[str, str] = field(default_factory=dict)
    # 入口动作的 before 采集：导航尚未发生，Surface 上没有任何页面可观察，
    # 而交接门禁要求 before/after 成对留证。置真时允许 Writer 先建空白 Surface。
    # 仅 Web 首跳使用；App 冷启动由 Bootstrap 产出首帧，无需此通道。
    bootstrap_blank_surface: bool = False
    # 新恢复会话的初始帧不是丢失来源的复用帧，须携带完整受信导航意图。
    recovery_bootstrap_intent: ActionIntent | None = field(default=None, repr=False)
    # 仅 Web V2 显式使用；None 保持 H5/App 既有采集语义。
    web_capture_stage: str | None = None
    viewport_width: int | None = None
    viewport_height: int | None = None
    screen_width: int | None = None
    screen_height: int | None = None
    device_scale_factor: float | None = None
    is_mobile: bool | None = None
    has_touch: bool | None = None
    color_scheme: str | None = None

    def __post_init__(self) -> None:
        _require_text(self.task_id, "task_id")
        _require_text(self.agent_run_id, "agent_run_id")
        _require_text(self.session_id, "session_id")
        if not isinstance(self.surface_kind, SurfaceKind):
            raise ValueError("surface_kind 必须为 SurfaceKind")
        _require_text(self.intent_id, "intent_id")
        if self.recovery_bootstrap_intent is not None:
            from services.api.app.application.evidence_exit_gate import (
                trusted_h5_browser_recovery_identity,
            )
            from services.api.app.domain.interaction import build_action_execution_id

            intent = self.recovery_bootstrap_intent
            if not isinstance(intent, ActionIntent):
                raise ValueError("新恢复会话初始采集必须携带完整ActionIntent")
            identity = trusted_h5_browser_recovery_identity(intent)
            if not (
                identity is not None
                and identity[0] == self.agent_run_id
                and intent.task_id == self.task_id
                and intent.session_id == self.session_id
                and intent.intent_id == self.intent_id
                and intent.deadline == self.deadline
                and self.surface_kind is SurfaceKind.WEB
                and self.phase is CapturePhase.BEFORE_ACTION
                and self.directive == intent.capture_profile.before
                and self.parent_capture_id is None
                and self.bootstrap_blank_surface is False
                and self.action_execution_id == build_action_execution_id(intent)
            ):
                raise ValueError("新恢复会话初始采集授权与受信导航不一致")
        if not isinstance(self.phase, CapturePhase):
            raise ValueError("phase 必须为 CapturePhase")
        if not isinstance(self.directive, CaptureDirective):
            raise ValueError("directive 必须为 CaptureDirective")
        if self.parent_capture_id is not None:
            _require_text(self.parent_capture_id, "parent_capture_id")
        if self.action_execution_id is not None:
            _require_text(self.action_execution_id, "action_execution_id")
        for field_name in ("session_command_sequence", "finalization_epoch"):
            value = getattr(self, field_name)
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value < 0
            ):
                raise ValueError(f"{field_name} 必须为非负整数")
        if self.session_command_sequence == 0:
            raise ValueError("session_command_sequence 必须为正整数")
        if (self.session_command_sequence is None) != (self.finalization_epoch is None):
            raise ValueError("Session 命令序列与 Finalization Epoch 必须同时提供")
        if (self.lease_owner is None) != (self.lease_token is None):
            raise ValueError("lease_owner 与 lease_token 必须同时提供或同时省略")
        if self.lease_owner is not None:
            _require_text(self.lease_owner, "lease_owner")
            _require_text(self.lease_token, "lease_token")
        if not isinstance(self.observation_context, Mapping):
            raise ValueError("observation_context 必须为对象")
        for key, value in self.observation_context.items():
            _require_text(key, "observation_context.key")
            _require_text(value, f"observation_context.{key}")
        object.__setattr__(self, "observation_context", dict(self.observation_context))
        if self.web_capture_stage not in {
            None,
            "web_entry_probe",
            "web_page_probe",
            "web_page_ready_baseline",
            "web_primary_content_evidence",
        }:
            raise ValueError("web_capture_stage 不受支持")
        if not isinstance(self.deadline, datetime) or self.deadline.utcoffset() is None:
            raise ValueError("deadline 必须是带时区的 datetime")
        for field_name in (
            "viewport_width",
            "viewport_height",
            "screen_width",
            "screen_height",
        ):
            value = getattr(self, field_name)
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value <= 0
            ):
                raise ValueError(f"{field_name} 必须为正整数")
        if self.device_scale_factor is not None and (
            isinstance(self.device_scale_factor, bool)
            or not isinstance(self.device_scale_factor, (int, float))
            or not 0.1 <= float(self.device_scale_factor) <= 4.0
        ):
            raise ValueError("device_scale_factor 必须在 0.1 到 4.0 之间")
        for field_name in ("is_mobile", "has_touch"):
            value = getattr(self, field_name)
            if value is not None and not isinstance(value, bool):
                raise ValueError(f"{field_name} 必须为布尔值")
        if self.color_scheme is not None and self.color_scheme not in {
            "light",
            "dark",
            "no-preference",
        }:
            raise ValueError("color_scheme 不受支持")


@dataclass(frozen=True, kw_only=True)
class ExecuteInteractionRequest:
    """Interaction Executor 的单动作执行请求。"""

    agent_run_id: str
    intent: ActionIntent
    source_capture_id: str
    action_execution_id: str | None = None
    # Coordinator 从 Session 租约带入的版本；Driver 前的二次校验必须继续
    # 绑定同一 Epoch，避免校验后 Session 被关闭/重开而仍执行旧动作。
    finalization_epoch: int | None = None
    # 下游 Official/ADB 服务只验证 Coordinator 已持有的外层租约，不得在
    # 同一动作内重新 acquire；这些字段只存在于进程内执行上下文，不进入
    # state_summary、普通日志或持久化页面事实。
    command_sequence: int | None = None
    lease_owner: str | None = field(default=None, repr=False)
    lease_token: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        _require_text(self.agent_run_id, "agent_run_id")
        if not isinstance(self.intent, ActionIntent):
            raise ValueError("intent 必须为 ActionIntent")
        _require_text(self.source_capture_id, "source_capture_id")
        if self.action_execution_id is not None:
            _require_text(self.action_execution_id, "action_execution_id")
        if self.finalization_epoch is not None and (
            isinstance(self.finalization_epoch, bool)
            or not isinstance(self.finalization_epoch, int)
            or self.finalization_epoch < 0
        ):
            raise ValueError("finalization_epoch 必须为非负整数")
        if self.command_sequence is not None and (
            isinstance(self.command_sequence, bool)
            or not isinstance(self.command_sequence, int)
            or self.command_sequence <= 0
        ):
            raise ValueError("command_sequence 必须为正整数")
        # 历史 Web 调用已经携带 finalization_epoch，但没有底层租约字段；保留
        # 该兼容形状。只要开始携带 command_sequence，就必须同时提供 epoch；
        # 正式 App Coordinator 会传完整四元组，由下游入口再强制检查。
        if self.command_sequence is not None and self.finalization_epoch is None:
            raise ValueError("command_sequence 必须同时提供 finalization_epoch")
        if (self.lease_owner is None) != (self.lease_token is None):
            raise ValueError("lease_owner 与 lease_token 必须同时提供或同时省略")
        if self.lease_owner is not None:
            _require_text(self.lease_owner, "lease_owner")
            _require_text(self.lease_token, "lease_token")


@dataclass(frozen=True, kw_only=True)
class CaptureCoordinationResult:
    """动作前证据、真实执行结果和动作后证据组成的审计结果。"""

    before_capture: CaptureRef
    interaction: InteractionResult
    after_capture: CaptureRef
    exit_gate_decision: EvidenceExitGateDecision | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "before_capture": self.before_capture.to_dict(),
            "interaction": self.interaction.to_dict(),
            "after_capture": self.after_capture.to_dict(),
            "exit_gate_decision": (
                self.exit_gate_decision.to_dict() if self.exit_gate_decision is not None else None
            ),
        }


class CaptureWriter(Protocol):
    """负责从当前会话采集并提交不可变证据的端口。"""

    async def capture(self, request: CaptureWriteRequest) -> CaptureRef: ...


class InteractionExecutor(Protocol):
    """唯一允许把结构化意图交给平台 Driver 的端口。"""

    async def execute(self, request: ExecuteInteractionRequest) -> InteractionResult: ...


class SessionCommandGate(Protocol):
    """串行化同一 Surface Session 的完整动作原子边界。"""

    def hold(
        self,
        *,
        task_id: str,
        session_id: str,
        surface_kind: SurfaceKind | None = None,
        allow_draining: bool = False,
    ) -> AbstractAsyncContextManager[SessionCommandPermit]: ...

    async def release_session(self, *, task_id: str, session_id: str) -> None: ...

    async def cancel_session(
        self,
        *,
        task_id: str,
        session_id: str,
        reason: str = "canceled",
    ) -> None: ...


class CaptureCoordinationError(RuntimeError):
    """携带已完成审计引用的结构化协调失败。"""

    def __init__(
        self,
        *,
        stage: CaptureCoordinationStage,
        error_code: str,
        message: str,
        cause_type: str | None = None,
        before_capture: CaptureRef | None = None,
        interaction: InteractionResult | None = None,
        after_capture: CaptureRef | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.stage = stage
        self.error_code = _require_text(error_code, "error_code")
        self.cause_type = cause_type
        self.before_capture = before_capture
        self.interaction = interaction
        self.after_capture = after_capture
        # 动作后 Capture 的 Official 502／TIMEOUT 等暂态故障必须可重试，禁止误终态。
        self.retryable = bool(retryable)

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage.value,
            "error_code": self.error_code,
            "message": str(self),
            "cause_type": self.cause_type,
            "retryable": self.retryable,
            "before_capture": (
                self.before_capture.to_dict() if self.before_capture is not None else None
            ),
            "interaction": (self.interaction.to_dict() if self.interaction is not None else None),
            "after_capture": (
                self.after_capture.to_dict() if self.after_capture is not None else None
            ),
        }


def _require_text(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError(f"{field_name} 必须为无首尾空白的非空字符串")
    return value
