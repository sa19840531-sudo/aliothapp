"""根任务状态的 PostgreSQL 乐观并发仓储。"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any

from services.api.app.application.task_status import TransitionTaskStatusCommand
from services.api.app.domain.task.status import (
    TaskStatus,
    TaskStatusSnapshot,
    validate_task_status_transition,
)
from services.api.app.infrastructure.db.pool import get_postgres_pool


class PostgresTaskStatusRepository:
    """统一读取和迁移 `qc_task.status`。"""

    def __init__(self, *, database_url: str | None = None, pool: Any | None = None) -> None:
        self._database_url = database_url
        self._pool = pool

    async def get(self, *, task_id: str) -> TaskStatusSnapshot | None:
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            return await get_task_status_in_transaction(
                connection,
                task_id=task_id,
                for_update=False,
            )

    async def transition(
        self,
        *,
        command: TransitionTaskStatusCommand,
    ) -> TaskStatusSnapshot | None:
        pool = await self._get_pool()
        async with pool.acquire() as connection, connection.transaction():
            return await transition_task_status_in_transaction(
                connection,
                command=command,
                allowed_source_statuses=(command.expected_status,),
            )

    async def _get_pool(self) -> Any:
        if self._pool is not None:
            return self._pool
        return await get_postgres_pool(self._database_url)


async def get_task_status_in_transaction(
    connection: Any,
    *,
    task_id: str,
    for_update: bool = True,
) -> TaskStatusSnapshot | None:
    """读取任务状态；调用方可在既有事务内锁定同一任务。"""
    suffix = " FOR UPDATE" if for_update else ""
    row = await connection.fetchrow(
        f"""
        SELECT
            id::text AS task_id,
            status,
            row_version,
            finalization_state
        FROM qc_task
        WHERE id = $1::uuid{suffix}
        """,
        task_id,
    )
    return _snapshot(row)


async def transition_task_status_in_transaction(
    connection: Any,
    *,
    command: TransitionTaskStatusCommand,
    allowed_source_statuses: Sequence[TaskStatus],
) -> TaskStatusSnapshot | None:
    """在调用方事务内以状态、版本和 sealed 防线推进任务。"""
    current = await get_task_status_in_transaction(
        connection,
        task_id=command.task_id,
        for_update=True,
    )
    if current is None:
        return None
    return await _transition_locked_task_status(
        connection,
        current=current,
        command=command,
        allowed_source_statuses=allowed_source_statuses,
    )


async def transition_current_task_status_in_transaction(
    connection: Any,
    *,
    task_id: str,
    target_status: TaskStatus,
    allowed_source_statuses: Sequence[TaskStatus],
    occurred_at: datetime,
    last_error: str | None = None,
    update_last_error: bool = False,
    mark_locked: bool = False,
) -> TaskStatusSnapshot | None:
    """锁定当前版本后推进状态，供已有数据库事务逐步迁移到统一入口。"""
    allowed = _normalize_allowed_source_statuses(allowed_source_statuses)
    current = await get_task_status_in_transaction(
        connection,
        task_id=task_id,
        for_update=True,
    )
    if current is None or current.status not in allowed:
        return None
    return await _transition_locked_task_status(
        connection,
        current=current,
        command=TransitionTaskStatusCommand(
            task_id=task_id,
            expected_status=current.status,
            target_status=target_status,
            expected_row_version=current.row_version,
            occurred_at=occurred_at,
            last_error=last_error,
            update_last_error=update_last_error,
            mark_locked=mark_locked,
        ),
        allowed_source_statuses=allowed,
    )


async def _transition_locked_task_status(
    connection: Any,
    *,
    current: TaskStatusSnapshot,
    command: TransitionTaskStatusCommand,
    allowed_source_statuses: Sequence[TaskStatus],
) -> TaskStatusSnapshot | None:
    """在已经锁定的任务快照上执行一次条件更新。"""
    allowed = _normalize_allowed_source_statuses(allowed_source_statuses)
    if current.status not in allowed:
        return None
    if current.status is not command.expected_status:
        return None
    if current.row_version != command.expected_row_version:
        return None
    validate_task_status_transition(
        current_status=current.status,
        target_status=command.target_status,
        finalization_state=current.finalization_state,
    )
    if current.status is command.target_status:
        return current
    # asyncpg 对同一参数既写入 varchar 列又与 text 字面量比较时会报
    # AmbiguousParameterError(text versus character varying)；显式转型消歧。
    row = await connection.fetchrow(
        """
        UPDATE qc_task
        SET
            status = $4::varchar,
            started_at = CASE
                WHEN $4::varchar = 'running' THEN COALESCE(started_at, $5::timestamptz)
                ELSE started_at
            END,
            locked_at = CASE
                WHEN $6::boolean THEN COALESCE(locked_at, $5::timestamptz)
                ELSE locked_at
            END,
            finished_at = CASE
                WHEN $4::varchar IN ('review', 'done', 'failed', 'canceled')
                THEN COALESCE(finished_at, $5::timestamptz)
                ELSE finished_at
            END,
            last_error = CASE
                WHEN $7::boolean THEN $8::text
                ELSE last_error
            END,
            updated_at = now()
        WHERE id = $1::uuid
          AND status = $2::varchar
          AND row_version = $3::bigint
          AND finalization_state <> 'sealed'
        RETURNING
            id::text AS task_id,
            status,
            row_version,
            finalization_state
        """,
        command.task_id,
        command.expected_status.value,
        command.expected_row_version,
        command.target_status.value,
        command.occurred_at,
        command.mark_locked,
        command.update_last_error,
        command.last_error,
    )
    return _snapshot(row)


def _normalize_allowed_source_statuses(
    allowed_source_statuses: Sequence[TaskStatus],
) -> tuple[TaskStatus, ...]:
    allowed = tuple(allowed_source_statuses)
    if not allowed or any(not isinstance(item, TaskStatus) for item in allowed):
        raise ValueError("allowed_source_statuses 必须为非空 TaskStatus 数组")
    if len(set(allowed)) != len(allowed):
        raise ValueError("allowed_source_statuses 不能重复")
    return allowed


def _snapshot(row: Any) -> TaskStatusSnapshot | None:
    if row is None:
        return None
    return TaskStatusSnapshot(
        task_id=str(row["task_id"]),
        status=TaskStatus(str(row["status"])),
        row_version=int(row["row_version"]),
        finalization_state=str(row["finalization_state"]),
    )
