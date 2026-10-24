"""App Agent Run 执行租约与 PostgreSQL-first 恢复候选仓储。"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from services.api.app.application.app_run_recovery import (
    AcquireAppRunExecutionLeaseCommand,
    AppRunExecutionLease,
    AppRunRecoveryPersistenceError,
    AppRunRecoverySnapshot,
    ReleaseAppRunExecutionLeaseCommand,
    RenewAppRunExecutionLeaseCommand,
)
from services.api.app.infrastructure.db.pool import get_postgres_pool
from services.api.app.infrastructure.queue.state_store import (
    agent_run_from_postgres_row,
)


class PostgresAppRunRecoveryRepository:
    """以 PostgreSQL 决定同一 App Run 的唯一执行者与恢复时机。"""

    def __init__(self, *, database_url: str | None = None, pool: Any | None = None) -> None:
        self._database_url = database_url
        self._pool = pool

    async def acquire(
        self,
        command: AcquireAppRunExecutionLeaseCommand,
    ) -> AppRunExecutionLease | None:
        _validate_lease_command(command)
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                INSERT INTO qc_app_run_execution_lease AS existing (
                    agent_run_id, task_id, lease_owner, lease_token,
                    lease_expires_at, row_version, attempt_count
                )
                SELECT
                    run.id, run.task_id, $3::varchar, $4::uuid,
                    now() + make_interval(secs => $5::integer), 0, 1
                FROM qc_agent_run AS run
                INNER JOIN qc_task AS task ON task.id = run.task_id
                WHERE run.id = $1::uuid
                  AND run.task_id = $2::uuid
                  AND run.agent_type = 'main'
                  AND run.status IN ('pending', 'running')
                  AND task.target_platform = 'app'
                  AND task.status IN ('queued', 'running')
                  AND task.finalization_state <> 'sealed'
                ON CONFLICT (agent_run_id) DO UPDATE
                SET
                    lease_owner = EXCLUDED.lease_owner,
                    lease_token = EXCLUDED.lease_token,
                    lease_expires_at = EXCLUDED.lease_expires_at,
                    row_version = existing.row_version + 1,
                    attempt_count = CASE
                        WHEN existing.lease_owner = EXCLUDED.lease_owner
                         AND existing.lease_token = EXCLUDED.lease_token
                        THEN existing.attempt_count
                        ELSE existing.attempt_count + 1
                    END,
                    updated_at = now()
                WHERE existing.task_id = EXCLUDED.task_id
                  AND (
                    existing.lease_expires_at <= now()
                    OR (
                        existing.lease_owner = EXCLUDED.lease_owner
                        AND existing.lease_token = EXCLUDED.lease_token
                    )
                  )
                RETURNING
                    existing.task_id::text AS task_id,
                    existing.agent_run_id::text AS agent_run_id,
                    existing.lease_owner,
                    existing.lease_token::text AS lease_token,
                    existing.lease_expires_at,
                    existing.row_version,
                    existing.attempt_count
                """,
                command.agent_run_id,
                command.task_id,
                command.lease_owner,
                command.lease_token,
                command.lease_seconds,
            )
        return _parse_lease(row) if row is not None else None

    async def renew(
        self,
        command: RenewAppRunExecutionLeaseCommand,
    ) -> AppRunExecutionLease:
        _validate_lease_command(command)
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                UPDATE qc_app_run_execution_lease AS lease
                SET
                    lease_expires_at = now() + make_interval(secs => $5::integer),
                    row_version = lease.row_version + 1,
                    updated_at = now()
                FROM qc_agent_run AS run
                INNER JOIN qc_task AS task ON task.id = run.task_id
                WHERE lease.agent_run_id = $1::uuid
                  AND lease.task_id = $2::uuid
                  AND lease.lease_owner = $3::varchar
                  AND lease.lease_token = $4::uuid
                  AND lease.lease_expires_at > now()
                  AND run.id = lease.agent_run_id
                  AND run.task_id = lease.task_id
                  AND run.agent_type = 'main'
                  AND run.status IN (
                    'pending', 'running', 'done', 'failed', 'aborted', 'canceled'
                  )
                  AND task.target_platform = 'app'
                RETURNING
                    lease.task_id::text AS task_id,
                    lease.agent_run_id::text AS agent_run_id,
                    lease.lease_owner,
                    lease.lease_token::text AS lease_token,
                    lease.lease_expires_at,
                    lease.row_version,
                    lease.attempt_count
                """,
                command.agent_run_id,
                command.task_id,
                command.lease_owner,
                command.lease_token,
                command.lease_seconds,
            )
        if row is None:
            raise AppRunRecoveryPersistenceError("App Run 执行租约已失效")
        return _parse_lease(row)

    async def release(
        self,
        command: ReleaseAppRunExecutionLeaseCommand,
    ) -> bool:
        _validate_identity(command)
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            released = await connection.fetchval(
                """
                DELETE FROM qc_app_run_execution_lease AS lease
                WHERE lease.agent_run_id = $1::uuid
                  AND lease.task_id = $2::uuid
                  AND lease.lease_owner = $3::varchar
                  AND lease.lease_token = $4::uuid
                RETURNING true
                """,
                command.agent_run_id,
                command.task_id,
                command.lease_owner,
                command.lease_token,
            )
        return bool(released)

    async def list_recovery_snapshots(
        self,
        *,
        limit: int = 100,
    ) -> list[AppRunRecoverySnapshot]:
        if limit <= 0:
            return []
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            rows = await connection.fetch(_RECOVERY_QUERY, None, limit)
        return [_parse_recovery_snapshot(row) for row in rows]

    async def get_recovery_snapshot(
        self,
        *,
        agent_run_id: str,
    ) -> AppRunRecoverySnapshot | None:
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            rows = await connection.fetch(_RECOVERY_QUERY, agent_run_id, 1)
        return _parse_recovery_snapshot(rows[0]) if rows else None

    async def _get_pool(self) -> Any:
        if self._pool is not None:
            return self._pool
        return await get_postgres_pool(self._database_url)


_RECOVERY_QUERY = """
    WITH recovery_candidates AS (
    SELECT
        run.id::text AS run_id,
        run.task_id::text AS task_id,
        run.status,
        run.input_context,
        run.output_context,
        run.working_memory,
        run.loop_guard,
        run.stop_reason,
        run.error_message,
        run.total_steps,
        run.started_at,
        run.finished_at,
        run.created_at,
        CASE
            WHEN execution_lease.lease_expires_at > now() THEN false
            WHEN result.agent_run_id IS NOT NULL THEN true
            WHEN bootstrap.stage = 'app_ready' AND (
                surface_session.session_id IS NULL
                OR surface_session.status <> 'active'
            ) THEN true
            WHEN work.has_due_work THEN true
            WHEN work.has_unclaimable_work THEN true
            WHEN work.has_terminal_work AND NOT work.has_nonterminal_work THEN true
            WHEN work.has_any_work THEN false
            WHEN run.status = 'pending' THEN true
            WHEN bootstrap.session_id IS NULL THEN true
            WHEN bootstrap.stage IN ('failed', 'canceled') THEN false
            WHEN bootstrap.stage <> 'app_ready' THEN (
                bootstrap.workflow_lease_expires_at IS NULL
                OR bootstrap.workflow_lease_expires_at <= now()
            )
            WHEN NOT work.has_any_work THEN true
            ELSE false
        END AS recovery_ready,
        CASE
            WHEN execution_lease.lease_expires_at > now() THEN 'run_execution_lease_active'
            WHEN result.agent_run_id IS NOT NULL
            THEN 'exploration_result_waiting_runtime_recovery'
            WHEN bootstrap.stage = 'app_ready' AND (
                surface_session.session_id IS NULL
                OR surface_session.status <> 'active'
            ) THEN 'app_ready_session_unavailable_waiting_runtime_recovery'
            WHEN work.has_due_work
            THEN 'exploration_work_due_waiting_runtime_recovery'
            WHEN work.has_unclaimable_work
            THEN 'exploration_work_unclaimable_waiting_runtime_recovery'
            WHEN work.has_terminal_work AND NOT work.has_nonterminal_work
            THEN 'terminal_work_waiting_runtime_recovery'
            WHEN work.has_any_work THEN 'exploration_work_waiting'
            WHEN run.status = 'pending' THEN 'pending_run_not_dispatched'
            WHEN bootstrap.session_id IS NULL THEN 'bootstrap_not_started'
            WHEN bootstrap.stage IN ('failed', 'canceled')
            THEN 'bootstrap_terminal_waiting_runtime_recovery'
            WHEN bootstrap.stage <> 'app_ready' THEN 'bootstrap_lease_expired'
            WHEN NOT work.has_any_work THEN 'app_ready_without_exploration_work'
            ELSE 'exploration_work_waiting'
        END AS recovery_reason,
        CASE
            WHEN execution_lease.lease_expires_at > now()
            THEN execution_lease.lease_expires_at
            WHEN result.agent_run_id IS NOT NULL
            THEN now()
            WHEN bootstrap.stage = 'app_ready' AND (
                surface_session.session_id IS NULL
                OR surface_session.status <> 'active'
            ) THEN now()
            WHEN work.has_due_work
              OR work.has_unclaimable_work
              OR (work.has_terminal_work AND NOT work.has_nonterminal_work)
            THEN now()
            WHEN bootstrap.stage <> 'app_ready'
            THEN bootstrap.workflow_lease_expires_at
            ELSE work.next_work_at
        END AS next_recovery_at,
        COALESCE(execution_lease.lease_expires_at > now(), false)
            AS execution_lease_active
    FROM qc_agent_run AS run
    INNER JOIN qc_task AS task ON task.id = run.task_id
    LEFT JOIN qc_app_run_execution_lease AS execution_lease
        ON execution_lease.agent_run_id = run.id
    LEFT JOIN qc_app_bootstrap AS bootstrap
        ON bootstrap.task_id = run.task_id
       AND bootstrap.session_id = 'app:' || run.task_id::text
    LEFT JOIN qc_surface_session AS surface_session
        ON surface_session.task_id = run.task_id
       AND surface_session.session_id = bootstrap.session_id
    LEFT JOIN qc_app_exploration_result AS result
        ON result.agent_run_id = run.id
    LEFT JOIN LATERAL (
        SELECT
            count(*) > 0 AS has_any_work,
            COALESCE(bool_or(
                item.deadline > now()
                AND item.attempt_count < item.max_attempts
                AND (
                    (
                        item.status IN ('pending', 'retry_wait')
                        AND item.next_attempt_at <= now()
                    )
                    OR (
                        item.status IN ('leased', 'running')
                        AND item.lease_expires_at <= now()
                    )
                )
            ), false) AS has_due_work,
            COALESCE(bool_or(
                item.status IN ('pending', 'retry_wait', 'leased', 'running')
                AND (
                    item.deadline <= now()
                    OR item.attempt_count >= item.max_attempts
                )
            ), false) AS has_unclaimable_work,
            COALESCE(bool_or(
                item.status IN (
                    'succeeded', 'bounded', 'budget_exhausted', 'blocked',
                    'no_progress', 'failed', 'cancelled', 'superseded'
                )
            ), false) AS has_terminal_work,
            COALESCE(bool_or(
                item.status IN ('pending', 'retry_wait', 'leased', 'running')
            ), false) AS has_nonterminal_work,
            min(
                CASE
                    WHEN item.status IN ('pending', 'retry_wait')
                    THEN LEAST(item.next_attempt_at, item.deadline)
                    WHEN item.status IN ('leased', 'running')
                    THEN LEAST(item.lease_expires_at, item.deadline)
                    ELSE NULL
                END
            ) AS next_work_at
        FROM qc_v2_work_item AS item
        INNER JOIN qc_capture AS source
            ON source.id = item.source_capture_id
           AND source.agent_run_id = run.id
        WHERE item.task_id = run.task_id
          AND item.work_type = 'exploration'
          AND item.finalization_epoch = task.finalization_epoch
    ) AS work ON true
    WHERE run.status IN ('pending', 'running')
      AND run.agent_type = 'main'
      AND task.target_platform = 'app'
      AND task.status IN ('queued', 'running')
      AND task.finalization_state <> 'sealed'
      AND ($1::uuid IS NULL OR run.id = $1::uuid)
      AND (
        $1::uuid IS NOT NULL
        OR execution_lease.agent_run_id IS NULL
        OR execution_lease.lease_expires_at <= now()
      )
    )
    SELECT *
    FROM recovery_candidates
    ORDER BY
        recovery_ready DESC,
        CASE WHEN status = 'pending' THEN 0 ELSE 1 END,
        COALESCE(next_recovery_at, created_at),
        created_at,
        run_id
    LIMIT $2::integer
"""


def _parse_lease(row: Mapping[str, Any]) -> AppRunExecutionLease:
    return AppRunExecutionLease(
        task_id=str(row["task_id"]),
        agent_run_id=str(row["agent_run_id"]),
        lease_owner=str(row["lease_owner"]),
        lease_token=str(row["lease_token"]),
        lease_expires_at=_datetime(row["lease_expires_at"]),
        row_version=int(row["row_version"]),
        attempt_count=int(row["attempt_count"]),
    )


def _parse_recovery_snapshot(row: Mapping[str, Any]) -> AppRunRecoverySnapshot:
    return AppRunRecoverySnapshot(
        record=agent_run_from_postgres_row(row),
        ready=bool(row["recovery_ready"]),
        reason=str(row["recovery_reason"]),
        next_recovery_at=(
            _datetime(row["next_recovery_at"]) if row.get("next_recovery_at") is not None else None
        ),
        execution_lease_active=bool(row.get("execution_lease_active")),
    )


def _validate_lease_command(command: object) -> None:
    _validate_identity(command)
    lease_seconds = getattr(command, "lease_seconds", None)
    if isinstance(lease_seconds, bool) or not isinstance(lease_seconds, int):
        raise ValueError("lease_seconds 必须为正整数")
    if lease_seconds <= 0:
        raise ValueError("lease_seconds 必须为正整数")


def _validate_identity(command: object) -> None:
    for field_name in ("task_id", "agent_run_id", "lease_owner", "lease_token"):
        value = getattr(command, field_name, None)
        if not isinstance(value, str) or not value.strip() or value != value.strip():
            raise ValueError(f"{field_name} 必须为非空且无首尾空白的字符串")


def _datetime(value: object) -> datetime:
    if not isinstance(value, datetime):
        raise AppRunRecoveryPersistenceError("App Run 租约时间字段无效")
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
