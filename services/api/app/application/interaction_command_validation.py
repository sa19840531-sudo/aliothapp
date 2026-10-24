"""Capture Coordinator 的统一动作请求校验。

领域策略负责回答“这类命令是否允许”，本模块负责把该策略接到应用层的
``CaptureCoordinationCommand``，并把拒绝转换为稳定、可供 API/Agent 消费的
错误。它不持有 Driver、浏览器句柄或设备租约。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Protocol

from services.api.app.application.capture_coordination import CaptureCoordinationCommand
from services.api.app.domain.interaction.command_policy import (
    CommandPolicy,
    CommandPolicyError,
)
from services.api.app.domain.interaction.enums import SurfaceKind


class InteractionCommandValidationError(ValueError):
    """应用层动作请求未通过统一校验。"""

    def __init__(
        self,
        message: str,
        *,
        error_code: str,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.retryable = bool(retryable)


@dataclass(frozen=True, kw_only=True)
class TrustedCommandScope:
    """由任务/运行事实源返回的不可变命令范围。"""

    task_id: str
    session_id: str
    surface_kind: SurfaceKind
    policy: CommandPolicy
    finalization_epoch: int | None = None

    def __post_init__(self) -> None:
        for field_name in ("task_id", "session_id"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip() or value != value.strip():
                raise ValueError(f"{field_name} 必须为无首尾空白的非空字符串")
        if not isinstance(self.surface_kind, SurfaceKind):
            raise TypeError("surface_kind 必须为 SurfaceKind")
        if not isinstance(self.policy, CommandPolicy):
            raise TypeError("policy 必须为 CommandPolicy")
        if self.finalization_epoch is not None and (
            isinstance(self.finalization_epoch, bool)
            or not isinstance(self.finalization_epoch, int)
            or self.finalization_epoch < 0
        ):
            raise ValueError("finalization_epoch 必须为非负整数")


class TrustedTaskCommandScopeReader(Protocol):
    """读取服务端锁定的任务/会话命令范围，不能从 Agent 参数推导。"""

    async def read_command_scope(
        self,
        *,
        task_id: str,
        session_id: str,
        surface_kind: SurfaceKind,
    ) -> TrustedCommandScope | None: ...


@dataclass(frozen=True, kw_only=True)
class InteractionCommandValidator:
    """在动作进入 Session Gate 前执行的统一校验器。"""

    policy: CommandPolicy = field(default_factory=CommandPolicy.default)
    scope_reader: TrustedTaskCommandScopeReader | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.policy, CommandPolicy):
            raise TypeError("policy 必须为 CommandPolicy")

    def validate(self, command: CaptureCoordinationCommand) -> None:
        """同步校验仅适用于静态策略；配置范围读取器时必须走异步入口。"""

        if self.scope_reader is not None:
            raise InteractionCommandValidationError(
                "命令范围需要从任务事实源异步读取",
                error_code="COMMAND_SCOPE_READER_ASYNC_REQUIRED",
            )
        self._validate_with_policy(command, self.policy)

    async def validate_for(
        self,
        command: CaptureCoordinationCommand,
        *,
        expected_finalization_epoch: int | None = None,
    ) -> None:
        """优先读取服务端范围，再校验本轮动作；读取失败时失败关闭。"""

        policy = await self._read_policy_for(
            command,
            expected_finalization_epoch=expected_finalization_epoch,
        )
        self._validate_with_policy(command, policy)

    async def validate_resolved_target_for(
        self,
        command: CaptureCoordinationCommand,
        *,
        trusted_target_texts: Sequence[str | None] = (),
        target_trust: str | None = None,
        target_role: str | None = None,
        expected_finalization_epoch: int | None = None,
    ) -> None:
        """在目标解析完成后、Driver 调用前重新读取范围并复核副作用。"""

        policy = await self._read_policy_for(
            command,
            expected_finalization_epoch=expected_finalization_epoch,
        )
        # 目标语义是在动作执行前最后才解析出来的；因此这里不仅要复核
        # 目标风险，还要用同一份新鲜策略重新校验完整意图。策略可能在
        # 首次 Coordinator 校验后收紧 allowed_operations、风险等级或能力
        # 开关，不能让后置校验只看目标文本而绕过撤销。
        self._validate_with_policy(command, policy)
        try:
            policy.validate_resolved_target(
                command.intent,
                trusted_target_texts=trusted_target_texts,
                target_trust=target_trust,
                target_role=target_role,
            )
        except CommandPolicyError as exc:
            raise InteractionCommandValidationError(
                str(exc),
                error_code=exc.error_code,
                retryable=exc.retryable,
            ) from exc

    async def validate_runtime_location_for(
        self,
        command: CaptureCoordinationCommand,
        *,
        url: str | None = None,
        package_name: str | None = None,
        expected_finalization_epoch: int | None = None,
    ) -> None:
        """在 Driver 写入前复核实际运行时来源仍在服务端范围内。"""

        policy = await self._read_policy_for(
            command,
            expected_finalization_epoch=expected_finalization_epoch,
        )
        # 实际来源校验发生在 Driver 观察/执行边界，必须同时重放完整意图
        # 校验，覆盖策略在前一次检查后发生的动作、风险和 capability 撤销。
        self._validate_with_policy(command, policy)
        try:
            policy.validate_runtime_location(
                surface_kind=command.intent.surface_kind,
                url=url,
                package_name=package_name,
            )
        except CommandPolicyError as exc:
            raise InteractionCommandValidationError(
                str(exc),
                error_code=exc.error_code,
                retryable=exc.retryable,
            ) from exc

    async def _read_policy_for(
        self,
        command: CaptureCoordinationCommand,
        *,
        expected_finalization_epoch: int | None,
    ) -> CommandPolicy:
        if expected_finalization_epoch is not None and (
            isinstance(expected_finalization_epoch, bool)
            or not isinstance(expected_finalization_epoch, int)
            or expected_finalization_epoch < 0
        ):
            raise InteractionCommandValidationError(
                "命令租约的 Finalization Epoch 无效",
                error_code="COMMAND_SCOPE_EPOCH_INVALID",
            )
        if not isinstance(command, CaptureCoordinationCommand):
            raise InteractionCommandValidationError(
                "动作协调请求类型无效",
                error_code="COMMAND_REQUEST_INVALID",
            )
        if self.scope_reader is None:
            return self.policy
        try:
            scope = await self.scope_reader.read_command_scope(
                task_id=command.intent.task_id,
                session_id=command.intent.session_id,
                surface_kind=command.intent.surface_kind,
            )
        except Exception as exc:  # noqa: BLE001 - 范围读取失败必须稳定失败关闭
            raise InteractionCommandValidationError(
                "无法读取当前任务的命令范围",
                error_code="COMMAND_SCOPE_UNAVAILABLE",
                retryable=True,
            ) from exc
        if scope is None:
            raise InteractionCommandValidationError(
                "当前任务没有可用的命令范围",
                error_code="COMMAND_SCOPE_UNAVAILABLE",
                retryable=False,
            )
        if (
            scope.task_id != command.intent.task_id
            or scope.session_id != command.intent.session_id
            or scope.surface_kind is not command.intent.surface_kind
        ):
            raise InteractionCommandValidationError(
                "命令范围与动作任务或会话不一致",
                error_code="COMMAND_SCOPE_MISMATCH",
            )
        if (
            expected_finalization_epoch is not None
            and scope.finalization_epoch is None
        ):
            raise InteractionCommandValidationError(
                "命令范围没有证明当前 Session 租约的 Finalization Epoch",
                error_code="COMMAND_SCOPE_EPOCH_UNAVAILABLE",
                retryable=True,
            )
        if (
            expected_finalization_epoch is not None
            and scope.finalization_epoch != expected_finalization_epoch
        ):
            raise InteractionCommandValidationError(
                "命令范围与当前 Session 租约的 Finalization Epoch 不一致",
                error_code="COMMAND_SCOPE_EPOCH_MISMATCH",
            )
        return scope.policy

    @staticmethod
    def _validate_with_policy(
        command: CaptureCoordinationCommand,
        policy: CommandPolicy,
    ) -> None:
        if not isinstance(command, CaptureCoordinationCommand):
            raise InteractionCommandValidationError(
                "动作协调请求类型无效",
                error_code="COMMAND_REQUEST_INVALID",
            )
        try:
            policy.validate(command.intent)
        except CommandPolicyError as exc:
            raise InteractionCommandValidationError(
                str(exc),
                error_code=exc.error_code,
                retryable=exc.retryable,
            ) from exc


def validate_interaction_command(
    command: CaptureCoordinationCommand,
    *,
    policy: CommandPolicy | None = None,
) -> None:
    """便于非类调用方复用同一校验逻辑。"""

    InteractionCommandValidator(policy=policy or CommandPolicy.default()).validate(command)


async def validate_interaction_command_async(
    command: CaptureCoordinationCommand,
    *,
    policy: CommandPolicy | None = None,
    scope_reader: TrustedTaskCommandScopeReader | None = None,
) -> None:
    """供异步应用入口使用的统一校验辅助函数。"""

    await InteractionCommandValidator(
        policy=policy or CommandPolicy.default(),
        scope_reader=scope_reader,
    ).validate_for(command)


__all__ = [
    "InteractionCommandValidationError",
    "InteractionCommandValidator",
    "TrustedCommandScope",
    "TrustedTaskCommandScopeReader",
    "validate_interaction_command",
    "validate_interaction_command_async",
]
