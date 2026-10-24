"""PostgreSQL Manifest Read 影子对账仓储。"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from services.api.app.application.manifest_read_cutover import (
    ShadowReconciliationSummary,
    summarize_comparisons,
)
from services.api.app.application.manifest_read_shadow import (
    ArtifactIdentity,
    ShadowComparisonOutcome,
    ShadowReadComparison,
)
from services.api.app.infrastructure.db.pool import get_postgres_pool


class PostgresManifestShadowComparisonRepository:
    """持久化影子对账明细，供跨进程 authoritative 切主决议。"""

    def __init__(self, *, database_url: str | None = None, pool: Any | None = None) -> None:
        self._database_url = database_url
        self._pool = pool

    async def save_comparison(self, comparison: ShadowReadComparison) -> str:
        pool = self._pool or await get_postgres_pool(self._database_url)
        payload = comparison.to_audit_payload()
        async with pool.acquire() as connection:
            value = await connection.fetchval(
                """
                INSERT INTO qc_manifest_shadow_comparison (
                    task_id, capture_id, outcome, compared_at,
                    legacy_artifact_count, manifest_artifact_count, difference_count,
                    missing_in_manifest, unexpected_in_manifest, mismatched_size,
                    reason, payload
                )
                VALUES (
                    $1::uuid, $2, $3, $4::timestamptz,
                    $5, $6, $7,
                    $8::jsonb, $9::jsonb, $10::jsonb,
                    $11, $12::jsonb
                )
                ON CONFLICT (task_id, capture_id) DO UPDATE SET
                    outcome = EXCLUDED.outcome,
                    compared_at = EXCLUDED.compared_at,
                    legacy_artifact_count = EXCLUDED.legacy_artifact_count,
                    manifest_artifact_count = EXCLUDED.manifest_artifact_count,
                    difference_count = EXCLUDED.difference_count,
                    missing_in_manifest = EXCLUDED.missing_in_manifest,
                    unexpected_in_manifest = EXCLUDED.unexpected_in_manifest,
                    mismatched_size = EXCLUDED.mismatched_size,
                    reason = EXCLUDED.reason,
                    payload = EXCLUDED.payload
                RETURNING id::text
                """,
                comparison.task_id,
                comparison.capture_id,
                comparison.outcome.value,
                comparison.compared_at,
                comparison.legacy_artifact_count,
                comparison.manifest_artifact_count,
                comparison.difference_count,
                json.dumps(
                    [item.to_audit_payload() for item in comparison.missing_in_manifest],
                    ensure_ascii=False,
                ),
                json.dumps(
                    [
                        item.to_audit_payload()
                        for item in comparison.unexpected_in_manifest
                    ],
                    ensure_ascii=False,
                ),
                json.dumps(
                    [item.to_audit_payload() for item in comparison.mismatched_size],
                    ensure_ascii=False,
                ),
                comparison.reason,
                json.dumps(payload, ensure_ascii=False),
            )
        return str(value)

    async def load_reconciliation_summary(
        self,
        *,
        limit: int = 10_000,
    ) -> ShadowReconciliationSummary:
        """读取持久化对账明细并汇总为切主凭证。"""
        comparisons = await self.list_comparisons(limit=limit)
        return summarize_comparisons(comparisons)

    async def list_comparisons(
        self,
        *,
        limit: int = 10_000,
    ) -> list[ShadowReadComparison]:
        pool = self._pool or await get_postgres_pool(self._database_url)
        async with pool.acquire() as connection:
            rows = await connection.fetch(
                """
                SELECT
                    task_id::text AS task_id,
                    capture_id,
                    outcome,
                    compared_at,
                    legacy_artifact_count,
                    manifest_artifact_count,
                    missing_in_manifest::text AS missing_in_manifest,
                    unexpected_in_manifest::text AS unexpected_in_manifest,
                    mismatched_size::text AS mismatched_size,
                    reason
                FROM qc_manifest_shadow_comparison
                ORDER BY compared_at DESC, created_at DESC
                LIMIT $1
                """,
                min(max(limit, 1), 10_000),
            )
        return [_row_to_comparison(row) for row in rows]


def _row_to_comparison(row: Any) -> ShadowReadComparison:
    compared_at = row["compared_at"]
    if isinstance(compared_at, datetime) and compared_at.tzinfo is None:
        from datetime import UTC

        compared_at = compared_at.replace(tzinfo=UTC)
    return ShadowReadComparison(
        task_id=str(row["task_id"]),
        capture_id=str(row["capture_id"]),
        outcome=ShadowComparisonOutcome(str(row["outcome"])),
        compared_at=compared_at,
        legacy_artifact_count=int(row["legacy_artifact_count"]),
        manifest_artifact_count=int(row["manifest_artifact_count"]),
        missing_in_manifest=_identities(row["missing_in_manifest"]),
        unexpected_in_manifest=_identities(row["unexpected_in_manifest"]),
        mismatched_size=_identities(row["mismatched_size"]),
        reason=row["reason"],
    )


def _identities(raw: Any) -> tuple[ArtifactIdentity, ...]:
    if isinstance(raw, str):
        raw = json.loads(raw)
    if not isinstance(raw, list):
        return ()
    items: list[ArtifactIdentity] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        artifact_id = item.get("artifact_id")
        artifact_type = item.get("artifact_type")
        size_bytes = item.get("size_bytes")
        if (
            isinstance(artifact_id, str)
            and isinstance(artifact_type, str)
            and isinstance(size_bytes, int)
        ):
            items.append(
                ArtifactIdentity(
                    artifact_id=artifact_id,
                    artifact_type=artifact_type,
                    size_bytes=size_bytes,
                )
            )
    return tuple(items)
