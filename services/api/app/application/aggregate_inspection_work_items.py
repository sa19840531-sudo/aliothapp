"""绑定 EvidenceInputSet 的任务级质检工作项协议与状态机。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol
from uuid import uuid4

from services.api.app.domain.evidence import EvidenceInputScope
from services.api.app.domain.protocol import stable_protocol_hash

AGGREGATE_INSPECTION_WORK_ITEM_PROTOCOL_VERSION = "aggregate-inspection-work-item.v1"
AGGREGATE_INSPECTION_CONTROL_MODES = frozenset({"shadow", "enforced"})


class AggregateInspectionWorkItemStatus(StrEnum):
    """任务级工作项状态；与单页工作项分表，避免 page_capture 锚点。"""

    PENDING = "pending"
    LEASED = "leased"
    RUNNING = "running"
    RETRY_WAIT = "retry_wait"
    SUCCEEDED = "succeeded"
    EVIDENCE_INSUFFICIENT = "evidence_insufficient"
    FAILED = "failed"
    CANCELLED = "cancelled"
    SUPERSEDED = "superseded"


AGGREGATE_TERMINAL_STATUSES = frozenset(
    {
        AggregateInspectionWorkItemStatus.SUCCEEDED,
        AggregateInspectionWorkItemStatus.EVIDENCE_INSUFFICIENT,
        AggregateInspectionWorkItemStatus.FAILED,
        AggregateInspectionWorkItemStatus.CANCELLED,
        AggregateInspectionWorkItemStatus.SUPERSEDED,
    }
)

_TRANSITIONS = {
    AggregateInspectionWorkItemStatus.LEASED: frozenset(
        {
            AggregateInspectionWorkItemStatus.RUNNING,
            AggregateInspectionWorkItemStatus.RETRY_WAIT,
            AggregateInspectionWorkItemStatus.FAILED,
            AggregateInspectionWorkItemStatus.CANCELLED,
            AggregateInspectionWorkItemStatus.SUPERSEDED,
        }
    ),
    AggregateInspectionWorkItemStatus.RUNNING: frozenset(
        {
            AggregateInspectionWorkItemStatus.RETRY_WAIT,
            AggregateInspectionWorkItemStatus.SUCCEEDED,
            AggregateInspectionWorkItemStatus.EVIDENCE_INSUFFICIENT,
            AggregateInspectionWorkItemStatus.FAILED,
            AggregateInspectionWorkItemStatus.CANCELLED,
            AggregateInspectionWorkItemStatus.SUPERSEDED,
        }
    ),
}


class AggregateInspectionAttemptStatus(StrEnum):
    """一次任务级 Judge 尝试的审计状态。"""

    RUNNING = "running"
    SUCCEEDED = "succeeded"
    EVIDENCE_INSUFFICIENT = "evidence_insufficient"
    RETRYABLE_ERROR = "retryable_error"
    TERMINAL_ERROR = "terminal_error"
    ABANDONED = "abandoned"


_ATTEMPT_TRANSITIONS = {
    AggregateInspectionAttemptStatus.RUNNING: frozenset(
        {
            AggregateInspectionAttemptStatus.SUCCEEDED,
            AggregateInspectionAttemptStatus.EVIDENCE_INSUFFICIENT,
            AggregateInspectionAttemptStatus.RETRYABLE_ERROR,
            AggregateInspectionAttemptStatus.TERMINAL_ERROR,
            AggregateInspectionAttemptStatus.ABANDONED,
        }
    )
}


@dataclass(frozen=True, kw_only=True)
class PublishAggregateInspectionWorkItemCommand:
    """为一个已封存 EvidenceInputSet 发布且只发布一条聚合规则。"""

    task_id: str
    evidence_input_set_id: str
    producer_agent_run_id: str
    rule_id: str
    rule_code: str
    rule_contract_hash: str
    scope: EvidenceInputScope
    scope_key: str
    finalization_epoch: int
    priority: int = 100
    max_attempts: int = 3
    control_mode: str = "enforced"
    protocol_version: str = AGGREGATE_INSPECTION_WORK_ITEM_PROTOCOL_VERSION


@dataclass(frozen=True, kw_only=True)
class PublishedAggregateInspectionWorkItem:
    """幂等发布后的任务级工作项身份。"""

    work_item_id: str
    input_hash: str
    status: AggregateInspectionWorkItemStatus
    created: bool


@dataclass(frozen=True, kw_only=True)
class ClaimAggregateInspectionWorkItemCommand:
    """按任务与消费者 Run 领取一条任务级工作项。"""

    task_id: str
    consumer_agent_run_id: str
    lease_owner: str
    lease_seconds: int


@dataclass(frozen=True, kw_only=True)
class AggregateInspectionWorkItemLease:
    """任务级工作项的受 fencing 租约执行视图。"""

    work_item_id: str
    task_id: str
    evidence_input_set_id: str
    input_hash: str
    rule_id: str
    rule_code: str
    rule_contract_hash: str
    scope: EvidenceInputScope
    scope_key: str
    finalization_epoch: int
    lease_token: str
    attempt_no: int
    row_version: int


@dataclass(frozen=True, kw_only=True)
class TransitionAggregateInspectionWorkItemCommand:
    """绑定已登记 attempt，用状态、租约和 row_version 原子推进工作项。"""

    work_item_id: str
    expected_status: AggregateInspectionWorkItemStatus
    target_status: AggregateInspectionWorkItemStatus
    row_version: int
    lease_token: str | None = None
    attempt_id: str | None = None
    next_attempt_at: datetime | None = None
    result_payload: Mapping[str, Any] | None = None
    error_code: str | None = None
    error_message: str | None = None


@dataclass(frozen=True, kw_only=True)
class RenewAggregateInspectionWorkItemLeaseCommand:
    """当前消费者在租约过期前续租。"""

    work_item_id: str
    consumer_agent_run_id: str
    lease_token: str
    row_version: int
    lease_seconds: int


@dataclass(frozen=True, kw_only=True)
class StartAggregateInspectionAttemptCommand:
    """在有效租约下登记当前 attempt_no。"""

    work_item_id: str
    consumer_agent_run_id: str
    attempt_no: int
    lease_token: str
    input_hash: str


@dataclass(frozen=True, kw_only=True)
class TransitionAggregateInspectionAttemptCommand:
    """持久化一次聚合 Judge 尝试的不可变终态输出。"""

    attempt_id: str
    expected_status: AggregateInspectionAttemptStatus
    target_status: AggregateInspectionAttemptStatus
    lease_token: str
    result_payload: Mapping[str, Any] | None = None
    error_code: str | None = None
    error_message: str | None = None


class AggregateInspectionWorkItemValidationError(ValueError):
    """任务级工作项输入或状态转换不符合协议。"""


class AggregateInspectionWorkItemConflictError(RuntimeError):
    """工作项已被其他消费者或更高 row_version 推进。"""


@dataclass(frozen=True, kw_only=True)
class AggregateInspectionWorkItemFinalization:
    """任务收口时聚合工作项的恢复与失败关闭摘要。"""

    task_id: str
    recovered_work_item_count: int
    failed_work_item_count: int
    abandoned_attempt_count: int


class AggregateInspectionWorkItemRepository(Protocol):
    """任务级工作项的 PostgreSQL 权威端口。"""

    async def publish(
        self,
        *,
        command: PublishAggregateInspectionWorkItemCommand,
    ) -> PublishedAggregateInspectionWorkItem: ...

    async def claim(
        self,
        *,
        command: ClaimAggregateInspectionWorkItemCommand,
        lease_token: str,
    ) -> AggregateInspectionWorkItemLease | None: ...

    async def transition(
        self,
        *,
        command: TransitionAggregateInspectionWorkItemCommand,
        result_hash: str | None,
    ) -> int | None: ...

    async def renew_lease(
        self,
        *,
        command: RenewAggregateInspectionWorkItemLeaseCommand,
    ) -> int | None: ...

    async def start_attempt(
        self,
        *,
        command: StartAggregateInspectionAttemptCommand,
    ) -> str: ...

    async def transition_attempt(
        self,
        *,
        command: TransitionAggregateInspectionAttemptCommand,
        result_hash: str | None,
    ) -> bool: ...

    async def has_nonterminal_work(self, *, task_id: str) -> bool: ...

    async def finalize_task_work(
        self,
        *,
        task_id: str,
        error_code: str,
        error_message: str,
    ) -> AggregateInspectionWorkItemFinalization: ...


class AggregateInspectionWorkItemService:
    """验证任务级工作项合同并委托 PostgreSQL 原子推进。"""

    def __init__(self, *, repository: AggregateInspectionWorkItemRepository) -> None:
        self._repository = repository

    async def publish(
        self,
        command: PublishAggregateInspectionWorkItemCommand,
    ) -> PublishedAggregateInspectionWorkItem:
        _validate_publish(command)
        return await self._repository.publish(command=command)

    async def claim(
        self,
        command: ClaimAggregateInspectionWorkItemCommand,
    ) -> AggregateInspectionWorkItemLease | None:
        _require_text(command.task_id, "task_id")
        _require_text(command.consumer_agent_run_id, "consumer_agent_run_id")
        _require_text(command.lease_owner, "lease_owner")
        if (
            isinstance(command.lease_seconds, bool)
            or not isinstance(command.lease_seconds, int)
            or command.lease_seconds <= 0
        ):
            raise AggregateInspectionWorkItemValidationError("lease_seconds 必须大于 0")
        return await self._repository.claim(
            command=command,
            lease_token=str(uuid4()),
        )

    async def transition(
        self,
        command: TransitionAggregateInspectionWorkItemCommand,
    ) -> int:
        allowed = _TRANSITIONS.get(command.expected_status, frozenset())
        if command.target_status not in allowed:
            raise AggregateInspectionWorkItemValidationError(
                "非法任务级工作项状态转换："
                f"{command.expected_status.value} -> {command.target_status.value}"
            )
        _require_text(command.work_item_id, "work_item_id")
        if (
            isinstance(command.row_version, bool)
            or not isinstance(command.row_version, int)
            or command.row_version < 0
        ):
            raise AggregateInspectionWorkItemValidationError("row_version 不能小于 0")
        if command.expected_status in {
            AggregateInspectionWorkItemStatus.LEASED,
            AggregateInspectionWorkItemStatus.RUNNING,
        }:
            _require_text(command.lease_token, "lease_token")
        if command.target_status not in {
            AggregateInspectionWorkItemStatus.CANCELLED,
            AggregateInspectionWorkItemStatus.SUPERSEDED,
        }:
            _require_text(command.attempt_id, "attempt_id")
        if command.target_status is AggregateInspectionWorkItemStatus.RETRY_WAIT:
            if command.next_attempt_at is None:
                raise AggregateInspectionWorkItemValidationError(
                    "retry_wait 必须提供 next_attempt_at"
                )
        elif command.next_attempt_at is not None:
            raise AggregateInspectionWorkItemValidationError(
                "只有 retry_wait 可以设置 next_attempt_at"
            )
        terminal_with_result = {
            AggregateInspectionWorkItemStatus.SUCCEEDED,
            AggregateInspectionWorkItemStatus.EVIDENCE_INSUFFICIENT,
        }
        if command.target_status in terminal_with_result:
            if not isinstance(command.result_payload, Mapping) or not command.result_payload:
                raise AggregateInspectionWorkItemValidationError(
                    "聚合判定终态必须保存非空 result_payload"
                )
            result_hash = stable_protocol_hash(dict(command.result_payload))
        else:
            if command.result_payload is not None:
                raise AggregateInspectionWorkItemValidationError(
                    "只有判定终态可以保存 result_payload"
                )
            result_hash = None
        if command.target_status is AggregateInspectionWorkItemStatus.FAILED:
            _require_text(command.error_code, "error_code")
            _require_text(command.error_message, "error_message")
        next_version = await self._repository.transition(
            command=command,
            result_hash=result_hash,
        )
        if next_version is None:
            raise AggregateInspectionWorkItemConflictError(
                "任务级工作项状态、租约或 row_version 已变化"
            )
        return next_version

    async def renew_lease(
        self,
        command: RenewAggregateInspectionWorkItemLeaseCommand,
    ) -> int:
        for field_name in (
            "work_item_id",
            "consumer_agent_run_id",
            "lease_token",
        ):
            _require_text(getattr(command, field_name), field_name)
        if (
            isinstance(command.row_version, bool)
            or not isinstance(command.row_version, int)
            or command.row_version < 0
        ):
            raise AggregateInspectionWorkItemValidationError("row_version 不能小于 0")
        if (
            isinstance(command.lease_seconds, bool)
            or not isinstance(command.lease_seconds, int)
            or command.lease_seconds <= 0
        ):
            raise AggregateInspectionWorkItemValidationError("lease_seconds 必须大于 0")
        next_version = await self._repository.renew_lease(command=command)
        if next_version is None:
            raise AggregateInspectionWorkItemConflictError(
                "任务级工作项租约已过期或 row_version 已变化"
            )
        return next_version

    async def start_attempt(
        self,
        command: StartAggregateInspectionAttemptCommand,
    ) -> str:
        for field_name in (
            "work_item_id",
            "consumer_agent_run_id",
            "lease_token",
            "input_hash",
        ):
            _require_text(getattr(command, field_name), field_name)
        if (
            isinstance(command.attempt_no, bool)
            or not isinstance(command.attempt_no, int)
            or command.attempt_no <= 0
        ):
            raise AggregateInspectionWorkItemValidationError(
                "attempt_no 必须大于 0"
            )
        return await self._repository.start_attempt(command=command)

    async def transition_attempt(
        self,
        command: TransitionAggregateInspectionAttemptCommand,
    ) -> None:
        allowed = _ATTEMPT_TRANSITIONS.get(command.expected_status, frozenset())
        if command.target_status not in allowed:
            raise AggregateInspectionWorkItemValidationError(
                "非法任务级尝试状态转换："
                f"{command.expected_status.value} -> {command.target_status.value}"
            )
        _require_text(command.attempt_id, "attempt_id")
        _require_text(command.lease_token, "lease_token")
        terminal_with_result = {
            AggregateInspectionAttemptStatus.SUCCEEDED,
            AggregateInspectionAttemptStatus.EVIDENCE_INSUFFICIENT,
        }
        if command.target_status in terminal_with_result:
            if not isinstance(command.result_payload, Mapping) or not command.result_payload:
                raise AggregateInspectionWorkItemValidationError(
                    "聚合判定尝试终态必须保存非空 result_payload"
                )
            result_hash = stable_protocol_hash(dict(command.result_payload))
        else:
            if command.result_payload is not None:
                raise AggregateInspectionWorkItemValidationError(
                    "错误或废弃尝试不能保存判定 result_payload"
                )
            result_hash = None
        if command.target_status in {
            AggregateInspectionAttemptStatus.RETRYABLE_ERROR,
            AggregateInspectionAttemptStatus.TERMINAL_ERROR,
        }:
            _require_text(command.error_code, "error_code")
            _require_text(command.error_message, "error_message")
        updated = await self._repository.transition_attempt(
            command=command,
            result_hash=result_hash,
        )
        if not updated:
            raise AggregateInspectionWorkItemConflictError(
                "任务级尝试状态或租约已变化"
            )

    async def has_nonterminal_work(self, *, task_id: str) -> bool:
        _require_text(task_id, "task_id")
        return await self._repository.has_nonterminal_work(task_id=task_id)

    async def finalize_task_work(
        self,
        *,
        task_id: str,
        error_code: str,
        error_message: str,
    ) -> AggregateInspectionWorkItemFinalization:
        _require_text(task_id, "task_id")
        _require_text(error_code, "error_code")
        _require_text(error_message, "error_message")
        return await self._repository.finalize_task_work(
            task_id=task_id,
            error_code=error_code,
            error_message=error_message,
        )


def _validate_publish(command: PublishAggregateInspectionWorkItemCommand) -> None:
    for field_name in (
        "task_id",
        "evidence_input_set_id",
        "producer_agent_run_id",
        "rule_id",
        "rule_code",
        "rule_contract_hash",
        "scope_key",
    ):
        _require_text(getattr(command, field_name), field_name)
    if command.scope not in {
        EvidenceInputScope.JOURNEY,
        EvidenceInputScope.TASK,
        EvidenceInputScope.ENVIRONMENT_MATRIX,
    }:
        raise AggregateInspectionWorkItemValidationError(
            "任务级工作项不得承载 page scope"
        )
    if (
        isinstance(command.finalization_epoch, bool)
        or not isinstance(command.finalization_epoch, int)
        or command.finalization_epoch < 0
    ):
        raise AggregateInspectionWorkItemValidationError(
            "finalization_epoch 必须为非负整数"
        )
    if (
        isinstance(command.priority, bool)
        or not isinstance(command.priority, int)
        or command.priority < 0
    ):
        raise AggregateInspectionWorkItemValidationError("priority 必须为非负整数")
    if (
        isinstance(command.max_attempts, bool)
        or not isinstance(command.max_attempts, int)
        or command.max_attempts <= 0
    ):
        raise AggregateInspectionWorkItemValidationError("max_attempts 必须大于 0")
    if command.control_mode not in AGGREGATE_INSPECTION_CONTROL_MODES:
        raise AggregateInspectionWorkItemValidationError("control_mode 不受支持")
    if command.protocol_version != AGGREGATE_INSPECTION_WORK_ITEM_PROTOCOL_VERSION:
        raise AggregateInspectionWorkItemValidationError("任务级工作项协议版本不受支持")


def _require_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise AggregateInspectionWorkItemValidationError(f"{field_name} 不能为空")
    return value
