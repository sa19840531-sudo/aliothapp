"""App Evidence Index 的 PostgreSQL 幂等构建与原子封存仓储。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime
from typing import Any

from services.api.app.application.app_exploration_evidence import (
    PersistedAppEvidenceIndex,
)
from services.api.app.domain.evidence import (
    AppEvidenceIndex,
    AppEvidenceIndexStatus,
)
from services.api.app.infrastructure.db.pool import get_postgres_pool


class AppEvidenceIndexPersistenceError(RuntimeError):
    """Evidence Index 与已有 Run、Capture 或 Manifest 事实冲突。"""


class PostgresAppEvidenceIndexRepository:
    """以事务和行锁保证 Index 唯一、可重放且封存后不可变。"""

    def __init__(self, *, database_url: str | None = None, pool: Any | None = None) -> None:
        self._database_url = database_url
        self._pool = pool

    @asynccontextmanager
    async def closure_lock(self, *, run_id: str):
        """跨 Worker 串行化同一 App Run 的完整 Closure。"""

        if not isinstance(run_id, str) or not run_id.strip():
            raise ValueError("run_id 必须为非空字符串")
        pool = await self._get_pool()
        lock_key = f"app-exploration-closure:{run_id}"
        async with pool.acquire() as connection:
            await connection.execute(
                "SELECT pg_advisory_lock(hashtext($1))",
                lock_key,
            )
            try:
                yield
            finally:
                await asyncio.shield(
                    connection.execute(
                        "SELECT pg_advisory_unlock(hashtext($1))",
                        lock_key,
                    )
                )

    async def create(self, *, index: AppEvidenceIndex) -> PersistedAppEvidenceIndex:
        if not isinstance(index, AppEvidenceIndex):
            raise TypeError("index 必须为 AppEvidenceIndex")
        if index.status is not AppEvidenceIndexStatus.BUILDING:
            raise ValueError("create 只接受 building Evidence Index")
        pool = await self._get_pool()
        async with pool.acquire() as connection, connection.transaction():
            await connection.execute(
                "SELECT pg_advisory_xact_lock(hashtext($1))",
                f"app-evidence-index:{index.run_id}",
            )
            row = await connection.fetchrow(
                """
                INSERT INTO qc_app_evidence_index AS existing (
                    index_id, task_id, agent_run_id, build_id,
                    surface_session_id, result_context_sha256,
                    protocol_version, status,
                    evidence_gaps, created_at
                )
                VALUES (
                    $1, $2::uuid, $3::uuid, $4,
                    $5, $6, $7, 'building', '[]'::jsonb, $8
                )
                ON CONFLICT (agent_run_id) DO UPDATE
                SET index_id = existing.index_id
                WHERE existing.index_id = EXCLUDED.index_id
                  AND existing.task_id = EXCLUDED.task_id
                  AND existing.build_id = EXCLUDED.build_id
                  AND existing.surface_session_id = EXCLUDED.surface_session_id
                  AND (
                    existing.result_context_sha256
                        IS NOT DISTINCT FROM EXCLUDED.result_context_sha256
                    OR (
                        existing.status = 'building'
                        AND existing.result_context_sha256 IS NULL
                    )
                  )
                  AND existing.protocol_version = EXCLUDED.protocol_version
                RETURNING
                    existing.index_id, existing.created_at,
                    (xmax = 0) AS created
                """,
                index.index_id,
                index.task_id,
                index.run_id,
                index.build_id,
                index.surface_session_id,
                index.result_context_sha256,
                index.protocol_version,
                index.created_at,
            )
            if row is None or str(row["index_id"]) != index.index_id:
                raise AppEvidenceIndexPersistenceError(
                    "agent_run_id 已绑定不同 Evidence Index 内容"
                )
            persisted_index = index
            if row["created_at"] != index.created_at:
                persisted_index = replace(index, created_at=row["created_at"])
            await connection.execute(
                """
                UPDATE qc_app_evidence_index
                SET result_context_sha256 = $2
                WHERE index_id = $1
                  AND status = 'building'
                  AND result_context_sha256 IS NULL
                """,
                index.index_id,
                index.result_context_sha256,
            )
            await connection.execute(
                """
                DELETE FROM qc_app_evidence_index_entry
                WHERE index_id = $1
                """,
                index.index_id,
            )
            for entry in index.entries:
                inserted = await connection.fetchrow(
                    """
                    INSERT INTO qc_app_evidence_index_entry AS existing (
                        index_id, task_id, sequence_no, capture_id,
                        evidence_manifest_id, evidence_manifest_version,
                        semantic_role, surface_phase, exploration_work_id,
                        action_execution_id, candidate_id, page_identity,
                        captured_at, metadata
                    )
                    VALUES (
                        $1, $2::uuid, $3, $4::uuid,
                        $5, $6, $7, $8, $9,
                        $10, $11, $12, $13, $14::jsonb
                    )
                    ON CONFLICT (index_id, sequence_no) DO UPDATE
                    SET sequence_no = existing.sequence_no
                    WHERE existing.capture_id = EXCLUDED.capture_id
                      AND existing.evidence_manifest_id = EXCLUDED.evidence_manifest_id
                      AND existing.evidence_manifest_version = EXCLUDED.evidence_manifest_version
                      AND existing.semantic_role = EXCLUDED.semantic_role
                      AND existing.surface_phase IS NOT DISTINCT FROM EXCLUDED.surface_phase
                      AND existing.exploration_work_id
                            IS NOT DISTINCT FROM EXCLUDED.exploration_work_id
                      AND existing.action_execution_id
                            IS NOT DISTINCT FROM EXCLUDED.action_execution_id
                      AND existing.candidate_id IS NOT DISTINCT FROM EXCLUDED.candidate_id
                      AND existing.page_identity IS NOT DISTINCT FROM EXCLUDED.page_identity
                      AND existing.captured_at = EXCLUDED.captured_at
                      AND existing.metadata = EXCLUDED.metadata
                    RETURNING existing.sequence_no
                    """,
                    index.index_id,
                    index.task_id,
                    entry.sequence_no,
                    entry.capture_id,
                    entry.evidence_manifest_id,
                    entry.evidence_manifest_version,
                    entry.semantic_role.value,
                    entry.surface_phase,
                    entry.exploration_work_id,
                    entry.action_execution_id,
                    entry.candidate_id,
                    entry.page_identity,
                    entry.captured_at,
                    _json_dumps(entry.to_dict()["metadata"]),
                )
                if inserted is None:
                    raise AppEvidenceIndexPersistenceError(
                        f"sequence_no={entry.sequence_no} 已被不同证据占用"
                    )
        return PersistedAppEvidenceIndex(
            index=persisted_index,
            created=bool(row["created"]),
        )

    async def seal(self, *, index: AppEvidenceIndex) -> PersistedAppEvidenceIndex:
        if not isinstance(index, AppEvidenceIndex):
            raise TypeError("index 必须为 AppEvidenceIndex")
        if index.status is not AppEvidenceIndexStatus.SEALED:
            raise ValueError("seal 只接受 sealed Evidence Index")
        pool = await self._get_pool()
        async with pool.acquire() as connection, connection.transaction():
            await connection.execute(
                "SELECT pg_advisory_xact_lock(hashtext($1))",
                f"app-evidence-index:{index.run_id}",
            )
            row = await connection.fetchrow(
                """
                WITH locked AS (
                    SELECT index_id
                    FROM qc_app_evidence_index
                    WHERE index_id = $1
                    FOR UPDATE
                )
                UPDATE qc_app_evidence_index AS target
                SET status = 'sealed',
                    outcome = $2,
                    terminal_reason_code = $3,
                    content_sha256 = $4,
                    evidence_gaps = $5::jsonb,
                    sealed_at = CASE
                        WHEN target.status = 'sealed' THEN target.sealed_at
                        ELSE $6
                    END,
                    updated_at = now()
                FROM locked
                WHERE target.index_id = locked.index_id
                  AND target.result_context_sha256 IS NOT DISTINCT FROM $7
                  AND (
                    target.status = 'building'
                    OR (
                        target.status = 'sealed'
                        AND target.outcome = $2
                        AND target.terminal_reason_code = $3
                        AND target.content_sha256 = $4
                        AND target.evidence_gaps = $5::jsonb
                    )
                  )
                RETURNING
                    target.index_id, target.content_sha256,
                    target.sealed_at, target.created_at
                """,
                index.index_id,
                index.outcome.value if index.outcome is not None else None,
                index.terminal_reason_code,
                index.content_sha256,
                _json_dumps([item.to_dict() for item in index.evidence_gaps]),
                index.sealed_at,
                index.result_context_sha256,
            )
            if (
                row is None
                or str(row["index_id"]) != index.index_id
                or str(row["content_sha256"]) != index.content_sha256
            ):
                raise AppEvidenceIndexPersistenceError(
                    "Evidence Index 已由不同内容封存或封存条件不满足"
                )
        persisted_index = index
        if row["sealed_at"] != index.sealed_at or row["created_at"] != index.created_at:
            persisted_index = replace(
                index,
                sealed_at=row["sealed_at"],
                created_at=row["created_at"],
            )
        return PersistedAppEvidenceIndex(index=persisted_index, created=False)

    async def get_by_run_id(self, *, run_id: str) -> AppEvidenceIndex | None:
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            return await read_app_evidence_index_on_connection(connection, run_id=run_id)

    async def _get_pool(self) -> Any:
        if self._pool is not None:
            return self._pool
        return await get_postgres_pool(self._database_url)


async def read_app_evidence_index_on_connection(
    connection: Any, *, run_id: str,
) -> AppEvidenceIndex | None:
    """复用调用方只读事务重算 Index，避免门禁事务跨连接自等。"""

    header = await connection.fetchrow(
        """
        SELECT
            index_id, task_id::text AS task_id,
            agent_run_id::text AS run_id, build_id,
            surface_session_id, protocol_version, status,
            result_context_sha256,
            outcome, terminal_reason_code, content_sha256,
            evidence_gaps, sealed_at, created_at
        FROM qc_app_evidence_index
        WHERE agent_run_id = $1::uuid
        """,
        run_id,
    )
    if header is None:
        return None
    rows = await connection.fetch(
        """
        SELECT
            sequence_no, capture_id::text AS capture_id,
            evidence_manifest_id, evidence_manifest_version,
            semantic_role, surface_phase, exploration_work_id,
            action_execution_id, candidate_id, page_identity,
            captured_at, metadata
        FROM qc_app_evidence_index_entry
        WHERE index_id = $1
        ORDER BY sequence_no
        """,
        str(header["index_id"]),
    )
    payload = {
        "protocol_version": str(header["protocol_version"]),
        "index_id": str(header["index_id"]),
        "task_id": str(header["task_id"]),
        "run_id": str(header["run_id"]),
        "build_id": str(header["build_id"]),
        "surface_session_id": str(header["surface_session_id"]),
        "result_context_sha256": header["result_context_sha256"],
        "status": str(header["status"]),
        "outcome": header["outcome"],
        "terminal_reason_code": header["terminal_reason_code"],
        "content_sha256": header["content_sha256"],
        "evidence_gaps": _json_value(header["evidence_gaps"]),
        "entries": [_entry_payload(row) for row in rows],
        "sealed_at": _datetime_payload(header["sealed_at"]),
        "created_at": _datetime_payload(header["created_at"]),
    }
    return AppEvidenceIndex.from_dict(payload)


def _entry_payload(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "protocol_version": "app-evidence-index-entry.v1",
        "sequence_no": row["sequence_no"],
        "capture_id": str(row["capture_id"]),
        "evidence_manifest_id": str(row["evidence_manifest_id"]),
        "evidence_manifest_version": row["evidence_manifest_version"],
        "semantic_role": str(row["semantic_role"]),
        "surface_phase": row["surface_phase"],
        "exploration_work_id": row["exploration_work_id"],
        "action_execution_id": row["action_execution_id"],
        "candidate_id": row["candidate_id"],
        "page_identity": row["page_identity"],
        "captured_at": _datetime_payload(row["captured_at"]),
        "metadata": _json_value(row["metadata"]),
    }


def _json_value(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


def _datetime_payload(value: Any) -> str | None:
    """将 asyncpg 返回的时间值适配为领域协议要求的 ISO8601 字符串。"""

    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, str):
        return value
    raise TypeError("App Evidence Index 时间字段必须为 datetime、ISO8601 字符串或 None")


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
