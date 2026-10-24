"""E23：历史无清单采集回填的 PostgreSQL 仓储。"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from services.api.app.application.evidence_manifest_backfill import (
    BackfillArtifactRow,
    BackfillCaptureRow,
)
from services.api.app.domain.evidence import EvidenceManifestReference
from services.api.app.infrastructure.db.evidence_manifest_repository import (
    EvidenceManifestPersistenceError,
    PostgresEvidenceManifestRepository,
)
from services.api.app.infrastructure.db.pool import get_postgres_pool


class PostgresEvidenceManifestBackfillRepository:
    """以稳定游标扫描无清单采集，并复用生产写入路径补清单。"""

    def __init__(
        self,
        *,
        database_url: str | None = None,
        pool: Any | None = None,
        manifests: PostgresEvidenceManifestRepository | None = None,
    ) -> None:
        self._database_url = database_url
        self._pool = pool
        self._manifests = manifests or PostgresEvidenceManifestRepository(
            database_url=database_url,
            pool=pool,
        )

    async def scan_captures_without_manifest(
        self,
        *,
        before_captured_at: datetime | None,
        after_captured_at: datetime | None,
        after_capture_id: str | None,
        limit: int,
    ) -> list[BackfillCaptureRow]:
        if (after_captured_at is None) != (after_capture_id is None):
            raise ValueError("回填游标时间与 Capture ID 必须同时提供")
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            rows = await connection.fetch(
                """
                SELECT
                    capture.id::text AS capture_id,
                    capture.task_id::text AS task_id,
                    capture.agent_run_id::text AS agent_run_id,
                    capture.capture_role,
                    capture.captured_at,
                    capture.surface_session_id,
                    COALESCE(
                        jsonb_agg(
                            jsonb_build_object(
                                'artifact_id', artifact.id::text,
                                'artifact_type', artifact.artifact_type,
                                'storage_path', artifact.storage_path,
                                'checksum', artifact.checksum,
                                'size_bytes', artifact.size_bytes,
                                'content_type', artifact.content_type
                            )
                            ORDER BY artifact.created_at, artifact.id
                        ) FILTER (WHERE artifact.id IS NOT NULL),
                        '[]'::jsonb
                    ) AS artifacts
                FROM qc_capture AS capture
                LEFT JOIN qc_evidence_artifact AS artifact
                    ON artifact.page_capture_id = capture.id
                   AND artifact.task_id = capture.task_id
                   AND artifact.source IN ('baseline', 'rule_supplement', 'app_runtime')
                WHERE capture.capture_status = 'captured'
                  AND capture.captured_at IS NOT NULL
                  AND ($1::timestamptz IS NULL OR capture.captured_at < $1)
                  AND (
                    $2::timestamptz IS NULL
                    OR (capture.captured_at, capture.id) > ($2, $3::uuid)
                  )
                  AND NOT EXISTS (
                    SELECT 1
                    FROM qc_evidence_manifest AS manifest
                    WHERE manifest.capture_id = capture.id
                  )
                GROUP BY
                    capture.id,
                    capture.task_id,
                    capture.agent_run_id,
                    capture.capture_role,
                    capture.captured_at,
                    capture.surface_session_id
                ORDER BY capture.captured_at, capture.id
                LIMIT $4
                """,
                before_captured_at,
                after_captured_at,
                after_capture_id,
                limit,
            )
        return [_parse_capture(row) for row in rows]

    async def add_manifest(self, *, manifest: EvidenceManifestReference) -> bool:
        """复用生产清单写入路径；版本已存在时按未创建返回。"""
        try:
            persisted = await self._manifests.add(manifest=manifest)
        except EvidenceManifestPersistenceError:
            # 并发或已有同版本清单：回填不覆盖既有事实
            return False
        return persisted.created

    async def _get_pool(self) -> Any:
        if self._pool is not None:
            return self._pool
        return await get_postgres_pool(self._database_url)


def _parse_capture(row: Mapping[str, Any]) -> BackfillCaptureRow:
    return BackfillCaptureRow(
        task_id=str(row["task_id"]),
        capture_id=str(row["capture_id"]),
        agent_run_id=str(row["agent_run_id"] or ""),
        capture_role=str(row["capture_role"]),
        captured_at=row["captured_at"],
        surface_session_id=(
            str(row["surface_session_id"])
            if row["surface_session_id"] is not None
            else None
        ),
        artifacts=tuple(_parse_artifact(item) for item in _as_list(row["artifacts"])),
    )


def _parse_artifact(value: Mapping[str, Any]) -> BackfillArtifactRow:
    return BackfillArtifactRow(
        artifact_id=str(value["artifact_id"]),
        artifact_type=str(value["artifact_type"]),
        storage_path=str(value["storage_path"] or ""),
        checksum=str(value["checksum"] or ""),
        size_bytes=int(value["size_bytes"] or 0),
        content_type=(
            str(value["content_type"]) if value.get("content_type") is not None else None
        ),
    )


def _as_list(value: Any) -> list[Mapping[str, Any]]:
    if isinstance(value, str):
        value = json.loads(value)
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("artifacts 聚合结果必须为数组")
    return value
