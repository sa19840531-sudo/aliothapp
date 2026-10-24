"""App 探索 Result Intent 的 PostgreSQL 仓储。"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from services.api.app.application.app_exploration_reconciliation import (
    AppExplorationResultIntent,
    PersistedAppExplorationResultIntent,
)
from services.api.app.domain.protocol import thaw_json_mapping
from services.api.app.infrastructure.db.pool import get_postgres_pool


class AppExplorationResultIntentPersistenceError(RuntimeError):
    """同一 Run 已绑定不同 Result 恢复上下文。"""


class PostgresAppExplorationResultIntentRepository:
    """在 Evidence Index 封存前幂等保存 Result 的最小恢复事实。"""

    def __init__(self, *, database_url: str | None = None, pool: Any | None = None) -> None:
        self._database_url = database_url
        self._pool = pool

    async def record(
        self,
        *,
        intent: AppExplorationResultIntent,
    ) -> PersistedAppExplorationResultIntent:
        if not isinstance(intent, AppExplorationResultIntent):
            raise TypeError("intent 必须为 AppExplorationResultIntent")
        pool = await self._get_pool()
        async with pool.acquire() as connection, connection.transaction():
            row = await connection.fetchrow(
                """
                INSERT INTO qc_app_exploration_result_intent AS existing (
                    agent_run_id, task_id, surface_session_id,
                    result_context_sha256, coverage_summary,
                    started_at, created_at
                )
                VALUES ($1::uuid, $2::uuid, $3, $4, $5::jsonb, $6, $7)
                ON CONFLICT (agent_run_id) DO UPDATE
                SET agent_run_id = existing.agent_run_id
                WHERE existing.task_id = EXCLUDED.task_id
                  AND existing.surface_session_id = EXCLUDED.surface_session_id
                  AND existing.result_context_sha256 = EXCLUDED.result_context_sha256
                  AND existing.coverage_summary = EXCLUDED.coverage_summary
                  AND existing.started_at = EXCLUDED.started_at
                RETURNING existing.*, (xmax = 0) AS created
                """,
                intent.agent_run_id,
                intent.task_id,
                intent.surface_session_id,
                intent.result_context_sha256,
                _json_dumps(intent.coverage_summary),
                intent.started_at,
                intent.created_at,
            )
        if row is None:
            raise AppExplorationResultIntentPersistenceError(
                "agent_run_id 已绑定不同 Result Intent"
            )
        return PersistedAppExplorationResultIntent(
            intent=_parse_intent(row),
            created=bool(row["created"]),
        )

    async def get_by_run_id(self, *, run_id: str) -> AppExplorationResultIntent | None:
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                SELECT *
                FROM qc_app_exploration_result_intent
                WHERE agent_run_id = $1::uuid
                """,
                run_id,
            )
        return _parse_intent(row) if row is not None else None

    async def list_orphaned(
        self,
        *,
        limit: int = 100,
    ) -> tuple[AppExplorationResultIntent, ...]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise ValueError("limit 必须在 1 到 1000 之间")
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            rows = await connection.fetch(
                """
                SELECT intent.*
                FROM qc_app_exploration_result_intent AS intent
                INNER JOIN qc_app_evidence_index AS evidence_index
                  ON evidence_index.agent_run_id = intent.agent_run_id
                 AND evidence_index.task_id = intent.task_id
                 AND evidence_index.surface_session_id = intent.surface_session_id
                 AND evidence_index.result_context_sha256 = intent.result_context_sha256
                 AND evidence_index.status = 'sealed'
                LEFT JOIN qc_app_exploration_result AS result
                  ON result.agent_run_id = intent.agent_run_id
                WHERE result.agent_run_id IS NULL
                ORDER BY evidence_index.sealed_at, intent.agent_run_id
                LIMIT $1
                """,
                limit,
            )
        return tuple(_parse_intent(row) for row in rows)

    async def _get_pool(self) -> Any:
        if self._pool is not None:
            return self._pool
        return await get_postgres_pool(self._database_url)


def _parse_intent(row: Mapping[str, Any]) -> AppExplorationResultIntent:
    return AppExplorationResultIntent(
        task_id=str(row["task_id"]),
        agent_run_id=str(row["agent_run_id"]),
        surface_session_id=str(row["surface_session_id"]),
        result_context_sha256=str(row["result_context_sha256"]),
        coverage_summary=_json_value(row["coverage_summary"]),
        started_at=_datetime(row["started_at"]),
        created_at=_datetime(row["created_at"]),
    )


def _datetime(value: object) -> datetime:
    if not isinstance(value, datetime):
        raise AppExplorationResultIntentPersistenceError("Result Intent 时间字段无效")
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _json_value(value: object) -> Mapping[str, Any]:
    parsed = json.loads(value) if isinstance(value, str) else value
    if not isinstance(parsed, Mapping):
        raise AppExplorationResultIntentPersistenceError("coverage_summary 必须为对象")
    return parsed


def _json_dumps(value: Mapping[str, Any]) -> str:
    return json.dumps(
        thaw_json_mapping(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
