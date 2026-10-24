"""存量证据附件可信身份回填的 PostgreSQL 仓储。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from services.api.app.application.evidence_artifact_backfill import (
    EvidenceArtifactBackfillRow,
    EvidenceArtifactComputedIdentity,
)
from services.api.app.infrastructure.db.pool import get_postgres_pool


class PostgresEvidenceArtifactBackfillRepository:
    """以稳定游标扫描异常身份，并以乐观条件回填。"""

    def __init__(self, *, database_url: str | None = None, pool: Any | None = None) -> None:
        self._database_url = database_url
        self._pool = pool

    async def scan_candidates(
        self,
        *,
        after_created_at: datetime | None,
        after_id: str | None,
        limit: int,
    ) -> list[EvidenceArtifactBackfillRow]:
        if (after_created_at is None) != (after_id is None):
            raise ValueError("回填游标时间与附件 ID 必须同时提供")
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            rows = await connection.fetch(
                """
                SELECT
                    artifact.id::text AS artifact_id,
                    artifact.task_id::text AS task_id,
                    artifact.page_capture_id::text AS capture_id,
                    artifact.created_at,
                    artifact.storage_path,
                    artifact.checksum,
                    artifact.size_bytes
                FROM qc_evidence_artifact AS artifact
                WHERE (
                    $1::timestamptz IS NULL
                    OR (artifact.created_at, artifact.id) > ($1, $2::uuid)
                )
                  AND (
                    artifact.storage_path IS NULL
                    OR btrim(artifact.storage_path) = ''
                    OR artifact.checksum IS NULL
                    OR btrim(artifact.checksum) = ''
                    OR artifact.checksum !~ '^sha256:[0-9a-f]{64}$'
                    OR artifact.size_bytes IS NULL
                    OR artifact.size_bytes < 0
                  )
                ORDER BY artifact.created_at, artifact.id
                LIMIT $3
                """,
                after_created_at,
                after_id,
                limit,
            )
        return [
            EvidenceArtifactBackfillRow(
                artifact_id=str(row["artifact_id"]),
                created_at=row["created_at"],
                storage_path=row["storage_path"],
                checksum=row["checksum"],
                size_bytes=row["size_bytes"],
                task_id=str(row["task_id"]),
                capture_id=(
                    str(row["capture_id"]) if row["capture_id"] is not None else None
                ),
            )
            for row in rows
        ]

    async def update_identity(
        self,
        *,
        row: EvidenceArtifactBackfillRow,
        computed: EvidenceArtifactComputedIdentity,
    ) -> bool:
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            updated_id = await connection.fetchval(
                """
                UPDATE qc_evidence_artifact
                SET checksum = $2,
                    size_bytes = $3
                WHERE id = $1::uuid
                  AND storage_path IS NOT DISTINCT FROM $4
                  AND checksum IS NOT DISTINCT FROM $5
                  AND size_bytes IS NOT DISTINCT FROM $6
                RETURNING id::text
                """,
                row.artifact_id,
                computed.checksum,
                computed.size_bytes,
                row.storage_path,
                row.checksum,
                row.size_bytes,
            )
        return updated_id is not None

    async def _get_pool(self) -> Any:
        if self._pool is not None:
            return self._pool
        return await get_postgres_pool(self._database_url)
