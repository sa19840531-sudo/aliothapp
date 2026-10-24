"""App 探索技术失败事实的 PostgreSQL 仓储。"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from services.api.app.application.app_exploration_failures import (
    PersistedAppExplorationFailure,
)
from services.api.app.domain.evidence.app_failure import (
    AppExplorationFailureFact,
    AppExplorationFailureRecoveryState,
)
from services.api.app.domain.protocol import thaw_json_mapping
from services.api.app.infrastructure.db.pool import get_postgres_pool


class AppExplorationFailurePersistenceError(RuntimeError):
    """同一 Run 的失败事实冲突或对账版本已变化。"""


class PostgresAppExplorationFailureRepository:
    """以 agent_run_id 和 failure_sha256 保证首次技术失败不可变。"""

    def __init__(self, *, database_url: str | None = None, pool: Any | None = None) -> None:
        self._database_url = database_url
        self._pool = pool

    async def record(
        self,
        *,
        failure: AppExplorationFailureFact,
    ) -> PersistedAppExplorationFailure:
        if not isinstance(failure, AppExplorationFailureFact):
            raise TypeError("failure 必须为 AppExplorationFailureFact")
        pool = await self._get_pool()
        async with pool.acquire() as connection, connection.transaction():
            row = await connection.fetchrow(
                """
                INSERT INTO qc_app_exploration_failure AS existing (
                    failure_id, task_id, agent_run_id, surface_session_id,
                    protocol_version, phase, reason_code, error_type,
                    retryable, observed_at, failure_sha256, metadata,
                    recovery_state
                )
                SELECT
                    $1::uuid, task.id, run.id, $4,
                    $5, $6, $7, $8,
                    $9, $10, $11, $12::jsonb,
                    'pending'
                FROM qc_task AS task
                INNER JOIN qc_agent_run AS run
                  ON run.id = $3::uuid
                 AND run.task_id = task.id
                WHERE task.id = $2::uuid
                  AND task.target_platform = 'app'
                ON CONFLICT (agent_run_id) DO UPDATE
                SET agent_run_id = existing.agent_run_id
                WHERE existing.failure_sha256 = EXCLUDED.failure_sha256
                RETURNING existing.*, (xmax = 0) AS created
                """,
                failure.failure_id,
                failure.task_id,
                failure.agent_run_id,
                failure.surface_session_id,
                failure.protocol_version,
                failure.phase,
                failure.reason_code,
                failure.error_type,
                failure.retryable,
                failure.observed_at,
                failure.failure_sha256,
                _json_dumps(failure.metadata),
            )
        if row is None:
            raise AppExplorationFailurePersistenceError(
                "agent_run_id 已绑定不同的 App 技术失败事实"
            )
        persisted = _parse_failure(row)
        if persisted.failure_sha256 != failure.failure_sha256:
            raise AppExplorationFailurePersistenceError("失败事实哈希不一致")
        return PersistedAppExplorationFailure(
            failure=persisted,
            created=bool(row["created"]),
        )

    async def get_by_run_id(self, *, run_id: str) -> AppExplorationFailureFact | None:
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                "SELECT * FROM qc_app_exploration_failure WHERE agent_run_id = $1::uuid",
                run_id,
            )
        return _parse_failure(row) if row is not None else None

    async def list_pending(self, *, limit: int = 100) -> tuple[AppExplorationFailureFact, ...]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise ValueError("limit 必须在 1 到 1000 之间")
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            rows = await connection.fetch(
                """
                SELECT *
                FROM qc_app_exploration_failure
                WHERE recovery_state = 'pending'
                ORDER BY observed_at, failure_id
                LIMIT $1
                """,
                limit,
            )
        return tuple(_parse_failure(row) for row in rows)

    async def mark_reconciled(
        self,
        *,
        failure_id: str,
        failure_sha256: str,
        recovery_state: AppExplorationFailureRecoveryState,
        reconciled_at: datetime,
        sealed_index_id: str | None = None,
        result_id: str | None = None,
        session_status: str | None = None,
    ) -> AppExplorationFailureFact:
        if recovery_state is AppExplorationFailureRecoveryState.PENDING:
            raise ValueError("对账终态不能为 pending")
        pool = await self._get_pool()
        async with pool.acquire() as connection, connection.transaction():
            row = await connection.fetchrow(
                """
                UPDATE qc_app_exploration_failure
                SET recovery_state = $3,
                    sealed_index_id = $4,
                    result_id = $5::uuid,
                    session_status = $6,
                    reconciled_at = $7,
                    row_version = row_version + 1,
                    updated_at = now()
                WHERE failure_id = $1::uuid
                  AND failure_sha256 = $2
                  AND recovery_state = 'pending'
                RETURNING *
                """,
                failure_id,
                failure_sha256,
                recovery_state.value,
                sealed_index_id,
                result_id,
                session_status,
                reconciled_at,
            )
            if row is None:
                existing = await connection.fetchrow(
                    """
                    SELECT *
                    FROM qc_app_exploration_failure
                    WHERE failure_id = $1::uuid
                      AND failure_sha256 = $2
                    """,
                    failure_id,
                    failure_sha256,
                )
                if existing is not None and (
                    str(existing["recovery_state"]) == recovery_state.value
                    and _optional_text(existing["sealed_index_id"]) == sealed_index_id
                    and _optional_text(existing["result_id"]) == result_id
                    and _optional_text(existing["session_status"]) == session_status
                ):
                    row = existing
        if row is None:
            raise AppExplorationFailurePersistenceError("失败事实已由其他恢复机处理或身份冲突")
        return _parse_failure(row)

    async def _get_pool(self) -> Any:
        if self._pool is not None:
            return self._pool
        return await get_postgres_pool(self._database_url)


def _parse_failure(row: Mapping[str, Any]) -> AppExplorationFailureFact:
    return AppExplorationFailureFact(
        failure_id=str(row["failure_id"]),
        task_id=str(row["task_id"]),
        agent_run_id=str(row["agent_run_id"]),
        surface_session_id=str(row["surface_session_id"]),
        protocol_version=str(row["protocol_version"]),
        phase=str(row["phase"]),
        reason_code=str(row["reason_code"]),
        error_type=str(row["error_type"]),
        retryable=bool(row["retryable"]),
        observed_at=_datetime(row["observed_at"]),
        failure_sha256=str(row["failure_sha256"]),
        metadata=_json_value(row["metadata"]),
        recovery_state=AppExplorationFailureRecoveryState(str(row["recovery_state"])),
        sealed_index_id=(
            str(row["sealed_index_id"]) if row["sealed_index_id"] is not None else None
        ),
        result_id=str(row["result_id"]) if row["result_id"] is not None else None,
        session_status=(str(row["session_status"]) if row["session_status"] is not None else None),
        reconciled_at=(
            _datetime(row["reconciled_at"]) if row["reconciled_at"] is not None else None
        ),
    )


def _datetime(value: object) -> datetime:
    if not isinstance(value, datetime):
        raise AppExplorationFailurePersistenceError("失败事实时间字段无效")
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _json_value(value: object) -> Mapping[str, Any]:
    parsed = json.loads(value) if isinstance(value, str) else value
    if not isinstance(parsed, Mapping):
        raise AppExplorationFailurePersistenceError("失败事实 metadata 必须为对象")
    return parsed


def _json_dumps(value: Mapping[str, Any]) -> str:
    # AppExplorationFailureFact 为保证不可变性会把 metadata 深度冻结为
    # MappingProxyType/tuple；写入 JSONB 前必须还原为普通 JSON 值。
    return json.dumps(
        thaw_json_mapping(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _optional_text(value: object) -> str | None:
    return str(value) if value is not None else None
