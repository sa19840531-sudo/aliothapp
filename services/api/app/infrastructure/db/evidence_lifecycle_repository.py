"""PostgreSQL 证据生命周期仓储。"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from services.api.app.domain.evidence.lifecycle import (
    EvidenceArtifactLifecycleView,
    EvidenceLifecycleEvaluation,
    EvidenceLifecycleEventType,
    EvidenceLifecycleStatus,
)
from services.api.app.infrastructure.db.pool import get_postgres_pool


class PostgresEvidenceLifecycleRepository:
    """合法留存、状态推进与评估审计落库。"""

    def __init__(self, *, database_url: str | None = None, pool: Any | None = None) -> None:
        self._database_url = database_url
        self._pool = pool

    async def has_active_legal_hold(self, *, task_id: str) -> bool:
        pool = self._pool or await get_postgres_pool(self._database_url)
        async with pool.acquire() as connection:
            value = await connection.fetchval(
                """
                SELECT 1
                FROM qc_evidence_legal_hold
                WHERE task_id = $1::uuid
                  AND released_at IS NULL
                LIMIT 1
                """,
                task_id,
            )
        return value is not None

    async def apply_legal_hold(
        self,
        *,
        task_id: str,
        reason: str,
        held_by: str,
        held_at: datetime,
    ) -> str:
        pool = self._pool or await get_postgres_pool(self._database_url)
        async with pool.acquire() as connection, connection.transaction():
            hold_id = await connection.fetchval(
                """
                INSERT INTO qc_evidence_legal_hold (
                    task_id, reason, held_by, held_at
                )
                VALUES ($1::uuid, $2, $3, $4::timestamptz)
                RETURNING id::text
                """,
                task_id,
                reason,
                held_by,
                held_at,
            )
            await connection.execute(
                """
                INSERT INTO qc_evidence_lifecycle_event (
                    artifact_id, task_id, event_type, policy_version, actor, detail
                )
                VALUES (
                    NULL, $1::uuid, 'hold_apply', 'evidence-lifecycle.v1', $2,
                    $3::jsonb
                )
                """,
                task_id,
                held_by,
                json.dumps({"reason": reason, "hold_id": str(hold_id)}, ensure_ascii=False),
            )
        return str(hold_id)

    async def release_legal_hold(
        self,
        *,
        task_id: str,
        release_reason: str,
        released_at: datetime,
        released_by: str | None = None,
    ) -> int:
        actor = (released_by or "evidence-lifecycle").strip() or "evidence-lifecycle"
        pool = self._pool or await get_postgres_pool(self._database_url)
        async with pool.acquire() as connection, connection.transaction():
            result = await connection.execute(
                """
                UPDATE qc_evidence_legal_hold
                SET released_at = $2::timestamptz,
                    release_reason = $3
                WHERE task_id = $1::uuid
                  AND released_at IS NULL
                """,
                task_id,
                released_at,
                release_reason,
            )
            await connection.execute(
                """
                INSERT INTO qc_evidence_lifecycle_event (
                    artifact_id, task_id, event_type, policy_version, actor, detail
                )
                VALUES (
                    NULL, $1::uuid, 'hold_release', 'evidence-lifecycle.v1',
                    $2, $3::jsonb
                )
                """,
                task_id,
                actor,
                json.dumps(
                    {"release_reason": release_reason, "released_by": actor},
                    ensure_ascii=False,
                ),
            )
        # asyncpg returns "UPDATE N"
        try:
            return int(str(result).split()[-1])
        except (TypeError, ValueError, IndexError):
            return 0

    async def fetch_artifact(
        self,
        *,
        artifact_id: str,
    ) -> EvidenceArtifactLifecycleView | None:
        pool = self._pool or await get_postgres_pool(self._database_url)
        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                SELECT
                    id::text AS artifact_id,
                    task_id::text AS task_id,
                    created_at,
                    lifecycle_status,
                    storage_path
                FROM qc_evidence_artifact
                WHERE id = $1::uuid
                """,
                artifact_id,
            )
        if row is None:
            return None
        return _row_to_view(row)

    async def list_candidates(
        self,
        *,
        limit: int,
    ) -> Sequence[EvidenceArtifactLifecycleView]:
        pool = self._pool or await get_postgres_pool(self._database_url)
        async with pool.acquire() as connection:
            rows = await connection.fetch(
                """
                SELECT
                    id::text AS artifact_id,
                    task_id::text AS task_id,
                    created_at,
                    lifecycle_status,
                    storage_path
                FROM qc_evidence_artifact
                WHERE lifecycle_status <> 'deleted'
                ORDER BY created_at ASC
                LIMIT $1
                """,
                min(max(limit, 1), 10_000),
            )
        return tuple(_row_to_view(row) for row in rows)

    async def transition_status(
        self,
        *,
        artifact_id: str,
        from_status: EvidenceLifecycleStatus,
        to_status: EvidenceLifecycleStatus,
        actor: str,
        event_type: EvidenceLifecycleEventType,
        policy_version: str,
        detail: dict,
    ) -> bool:
        pool = self._pool or await get_postgres_pool(self._database_url)
        async with pool.acquire() as connection, connection.transaction():
            row = await connection.fetchrow(
                """
                UPDATE qc_evidence_artifact
                SET lifecycle_status = $3,
                    archived_at = CASE
                        WHEN $3 = 'archived' THEN COALESCE(archived_at, now())
                        ELSE archived_at
                    END,
                    deleted_at = CASE
                        WHEN $3 = 'deleted' THEN COALESCE(deleted_at, now())
                        ELSE deleted_at
                    END
                WHERE id = $1::uuid
                  AND lifecycle_status = $2
                RETURNING task_id::text AS task_id
                """,
                artifact_id,
                from_status.value,
                to_status.value,
            )
            if row is None:
                return False
            await connection.execute(
                """
                INSERT INTO qc_evidence_lifecycle_event (
                    artifact_id, task_id, event_type, policy_version, actor, detail
                )
                VALUES ($1::uuid, $2::uuid, $3, $4, $5, $6::jsonb)
                """,
                artifact_id,
                row["task_id"],
                event_type.value,
                policy_version,
                actor,
                json.dumps(detail, ensure_ascii=False),
            )
        return True

    async def record_evaluation(
        self,
        *,
        evaluation: EvidenceLifecycleEvaluation,
        actor: str,
    ) -> None:
        pool = self._pool or await get_postgres_pool(self._database_url)
        async with pool.acquire() as connection:
            await connection.execute(
                """
                INSERT INTO qc_evidence_lifecycle_event (
                    artifact_id, task_id, event_type, policy_version, actor, detail
                )
                VALUES ($1::uuid, $2::uuid, 'evaluate', $3, $4, $5::jsonb)
                """,
                evaluation.artifact_id,
                evaluation.task_id,
                evaluation.policy_version,
                actor,
                json.dumps(evaluation.to_audit_payload(), ensure_ascii=False),
            )


def _row_to_view(row: Any) -> EvidenceArtifactLifecycleView:
    created_at = row["created_at"]
    if created_at.tzinfo is None:
        from datetime import UTC

        created_at = created_at.replace(tzinfo=UTC)
    return EvidenceArtifactLifecycleView(
        artifact_id=str(row["artifact_id"]),
        task_id=str(row["task_id"]),
        created_at=created_at,
        lifecycle_status=EvidenceLifecycleStatus(str(row["lifecycle_status"])),
        storage_path=row["storage_path"],
    )
