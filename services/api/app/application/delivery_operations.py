"""规则发布投递事故的统一查询与受控处置合同。

本模块只覆盖 H5/App 主证据形成的 Publication、Outbox、Inbox 和规则工作项。
补采事件不属于本合同，仓储实现也必须以显式消息类型白名单过滤。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol


class DeliveryPlatform(StrEnum):
    """支持受控处置的页面探索平台。"""

    H5 = "h5"
    APP = "app"


class DeliveryOperationAction(StrEnum):
    """运维人员可执行的显式动作。"""

    RECONCILE = "reconcile"
    REQUEUE = "requeue"
    MARK_NEEDS_REVIEW = "mark_needs_review"


@dataclass(frozen=True, kw_only=True)
class DeliveryOperationAudit:
    """一次不可变的运维处置事实。"""

    operation_id: str
    action: DeliveryOperationAction
    operator_user_id: str
    reason: str
    result: str
    before_state: dict[str, Any]
    after_state: dict[str, Any]
    created_at: datetime


@dataclass(frozen=True, kw_only=True)
class DeliveryIncident:
    """Publication 维度的投递与规则工作统一投影。"""

    platform: DeliveryPlatform
    publication_id: str
    task_id: str
    status: str
    row_version: int
    finalization_epoch: int
    ruleset_id: str
    ruleset_version: int
    ruleset_snapshot_hash: str | None
    source_identity: dict[str, Any]
    outbox_pending_count: int
    outbox_dead_letter_count: int
    inbox_pending_count: int
    inbox_dead_letter_count: int
    linked_work_item_count: int
    linked_work_active_count: int
    linked_work_failed_count: int
    failure_code: str | None
    failure_message: str | None
    allowed_actions: tuple[DeliveryOperationAction, ...] = ()
    operations: tuple[DeliveryOperationAudit, ...] = ()
    updated_at: datetime | None = None


@dataclass(frozen=True, kw_only=True)
class DeliveryOperationCommand:
    """带并发版本围栏和操作理由的处置命令。"""

    platform: DeliveryPlatform
    publication_id: str
    action: DeliveryOperationAction
    expected_row_version: int
    operator_user_id: str
    reason: str

    def __post_init__(self) -> None:
        if self.expected_row_version <= 0:
            raise ValueError("expected_row_version 必须为正整数")
        for field_name in ("publication_id", "operator_user_id", "reason"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip() or value != value.strip():
                raise ValueError(f"{field_name} 必须为无首尾空白的非空字符串")
        if len(self.reason) > 500:
            raise ValueError("reason 不能超过 500 字符")


@dataclass(frozen=True, kw_only=True)
class DeliveryOperationResult:
    """一次已审计处置的结果。"""

    operation_id: str
    action: DeliveryOperationAction
    platform: DeliveryPlatform
    publication_id: str
    task_id: str
    result: str
    affected_counts: dict[str, int] = field(default_factory=dict)
    incident: DeliveryIncident


class DeliveryIncidentNotFoundError(RuntimeError):
    """Publication 不存在。"""


class DeliveryOperationConflictError(RuntimeError):
    """来源身份漂移、版本冲突或动作已不再适用。"""


class DeliveryOperationRepository(Protocol):
    """统一投递事故仓储端口。"""

    async def list_incidents(
        self,
        *,
        task_id: str,
        limit: int,
    ) -> list[DeliveryIncident]: ...

    async def execute(
        self,
        command: DeliveryOperationCommand,
    ) -> DeliveryOperationResult: ...


class DeliveryOperationService:
    """收敛输入约束，具体身份复核与事务由仓储完成。"""

    def __init__(self, repository: DeliveryOperationRepository) -> None:
        self._repository = repository

    async def list_incidents(
        self,
        *,
        task_id: str,
        limit: int = 100,
    ) -> list[DeliveryIncident]:
        normalized_task_id = task_id.strip()
        if not normalized_task_id:
            raise ValueError("task_id 不能为空")
        if limit <= 0 or limit > 200:
            raise ValueError("limit 必须在 1 到 200 之间")
        return await self._repository.list_incidents(
            task_id=normalized_task_id,
            limit=limit,
        )

    async def execute(
        self,
        *,
        platform: DeliveryPlatform,
        publication_id: str,
        action: DeliveryOperationAction,
        expected_row_version: int,
        operator_user_id: str,
        reason: str,
    ) -> DeliveryOperationResult:
        return await self._repository.execute(
            DeliveryOperationCommand(
                platform=platform,
                publication_id=publication_id.strip(),
                action=action,
                expected_row_version=expected_row_version,
                operator_user_id=operator_user_id.strip(),
                reason=reason.strip(),
            )
        )
