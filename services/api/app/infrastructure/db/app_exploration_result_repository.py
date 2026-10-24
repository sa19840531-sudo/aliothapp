"""App 探索结果 PostgreSQL 幂等仓储。"""

from __future__ import annotations

import json
from typing import Any

from services.api.app.application.app_exploration_results import (
    PersistedAppExplorationResult,
)
from services.api.app.domain.evidence import AppExplorationResult
from services.api.app.infrastructure.db.pool import get_postgres_pool


class AppExplorationResultPersistenceError(RuntimeError):
    """同一 Run 已绑定不同探索结果。"""


class PostgresAppExplorationResultRepository:
    """以 agent_run_id 保证探索结果唯一且可重放。"""

    def __init__(self, *, database_url: str | None = None, pool: Any | None = None) -> None:
        self._database_url = database_url
        self._pool = pool

    async def create(
        self,
        *,
        result: AppExplorationResult,
    ) -> PersistedAppExplorationResult:
        if not isinstance(result, AppExplorationResult):
            raise TypeError("result 必须为 AppExplorationResult")
        pool = await self._get_pool()
        async with pool.acquire() as connection, connection.transaction():
            row = await connection.fetchrow(
                """
                INSERT INTO qc_app_exploration_result AS existing (
                    agent_run_id, task_id, surface_session_id,
                    evidence_index_id, protocol_version, outcome,
                    terminal_reason_code, evidence_index_hash,
                    coverage_summary, evidence_gaps, started_at,
                    completed_at, result_sha256
                )
                VALUES (
                    $1::uuid, $2::uuid, $3,
                    $4, $5, $6,
                    $7, $8,
                    $9::jsonb, $10::jsonb, $11,
                    $12, $13
                )
                ON CONFLICT (agent_run_id) DO UPDATE
                SET agent_run_id = existing.agent_run_id
                WHERE existing.result_sha256 = EXCLUDED.result_sha256
                RETURNING existing.agent_run_id::text AS run_id,
                          existing.result_sha256,
                          (xmax = 0) AS created
                """,
                result.run_id,
                result.task_id,
                result.surface_session_id,
                result.evidence_index_id,
                result.protocol_version,
                result.outcome.value,
                result.terminal_reason_code,
                result.evidence_index_hash,
                _json_dumps(result.to_dict()["coverage_summary"]),
                _json_dumps(result.to_dict()["evidence_gaps"]),
                result.started_at,
                result.completed_at,
                result.result_sha256,
            )
        if (
            row is None
            or str(row["run_id"]) != result.run_id
            or str(row["result_sha256"]) != result.result_sha256
        ):
            raise AppExplorationResultPersistenceError(
                "agent_run_id 已绑定不同 App Exploration Result"
            )
        return PersistedAppExplorationResult(
            result=result,
            created=bool(row["created"]),
        )

    async def get_by_run_id(self, *, run_id: str) -> AppExplorationResult | None:
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                SELECT
                    agent_run_id::text AS run_id,
                    task_id::text AS task_id,
                    surface_session_id,
                    evidence_index_id,
                    protocol_version,
                    outcome,
                    terminal_reason_code,
                    evidence_index_hash,
                    coverage_summary,
                    evidence_gaps,
                    started_at,
                    completed_at,
                    result_sha256
                FROM qc_app_exploration_result
                WHERE agent_run_id = $1::uuid
                """,
                run_id,
            )
        if row is None:
            return None
        return AppExplorationResult.from_dict(
            {
                "protocol_version": str(row["protocol_version"]),
                "task_id": str(row["task_id"]),
                "run_id": str(row["run_id"]),
                "surface_session_id": str(row["surface_session_id"]),
                "outcome": str(row["outcome"]),
                "terminal_reason_code": str(row["terminal_reason_code"]),
                "evidence_index_id": str(row["evidence_index_id"]),
                "evidence_index_hash": str(row["evidence_index_hash"]),
                "coverage_summary": _json_value(row["coverage_summary"]),
                "evidence_gaps": _json_value(row["evidence_gaps"]),
                "started_at": _datetime_payload(row["started_at"]),
                "completed_at": _datetime_payload(row["completed_at"]),
                "result_sha256": str(row["result_sha256"]),
            }
        )

    async def _get_pool(self) -> Any:
        if self._pool is not None:
            return self._pool
        return await get_postgres_pool(self._database_url)


def _json_value(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


def _datetime_payload(value: Any) -> Any:
    return value.isoformat() if hasattr(value, "isoformat") else value


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
