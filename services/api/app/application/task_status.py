"""根质检任务状态迁移的应用层端口。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from services.api.app.domain.task.status import (
    TaskStatus,
    TaskStatusConflictError,
    TaskStatusSnapshot,
    TaskStatusValidationError,
    validate_task_status_transition,
)


@dataclass(frozen=True, kw_only=True)
class TransitionTaskStatusCommand:
    """以期望状态和行版本推进根任务。"""

    task_id: str
    expected_status: TaskStatus
    target_status: TaskStatus
    expected_row_version: int
    occurred_at: datetime
    last_error: str | None = None
    update_last_error: bool = False
    mark_locked: bool = False


class TaskStatusRepository(Protocol):
    """根任务状态事实的最小持久化端口。"""

    async def get(self, *, task_id: str) -> TaskStatusSnapshot | None: ...

    async def transition(
        self,
        *,
        command: TransitionTaskStatusCommand,
    ) -> TaskStatusSnapshot | None: ...


class TaskStatusService:
    """统一验证并推进 `qc_task.status`。"""

    def __init__(self, *, repository: TaskStatusRepository) -> None:
        self._repository = repository

    async def transition(
        self,
        command: TransitionTaskStatusCommand,
    ) -> TaskStatusSnapshot:
        _validate_command(command)
        current = await self._repository.get(task_id=command.task_id)
        if current is None:
            raise TaskStatusConflictError("任务不存在")
        if current.status is not command.expected_status:
            raise TaskStatusConflictError("任务状态已变化")
        if current.row_version != command.expected_row_version:
            raise TaskStatusConflictError("任务 row_version 已变化")
        validate_task_status_transition(
            current_status=current.status,
            target_status=command.target_status,
            finalization_state=current.finalization_state,
        )
        updated = await self._repository.transition(command=command)
        if updated is None:
            raise TaskStatusConflictError("任务状态、row_version 或收口状态已变化")
        return updated


def _validate_command(command: TransitionTaskStatusCommand) -> None:
    if not isinstance(command, TransitionTaskStatusCommand):
        raise TypeError("command 必须为 TransitionTaskStatusCommand")
    if (
        not isinstance(command.task_id, str)
        or not command.task_id.strip()
        or command.task_id != command.task_id.strip()
    ):
        raise TaskStatusValidationError("task_id 必须为无首尾空白的非空字符串")
    if not isinstance(command.expected_status, TaskStatus) or not isinstance(
        command.target_status,
        TaskStatus,
    ):
        raise TaskStatusValidationError("expected_status/target_status 类型不正确")
    if (
        isinstance(command.expected_row_version, bool)
        or not isinstance(command.expected_row_version, int)
        or command.expected_row_version < 0
    ):
        raise TaskStatusValidationError("expected_row_version 必须为非负整数")
    if not isinstance(command.occurred_at, datetime) or command.occurred_at.utcoffset() is None:
        raise TaskStatusValidationError("occurred_at 必须带时区")
    if command.last_error is not None and (
        not isinstance(command.last_error, str)
        or not command.last_error.strip()
        or command.last_error != command.last_error.strip()
    ):
        raise TaskStatusValidationError("last_error 必须为无首尾空白的非空字符串")
    if not isinstance(command.update_last_error, bool) or not isinstance(
        command.mark_locked,
        bool,
    ):
        raise TaskStatusValidationError("update_last_error/mark_locked 必须为布尔值")
