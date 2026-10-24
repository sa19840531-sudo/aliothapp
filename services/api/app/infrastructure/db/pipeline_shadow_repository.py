"""PostgreSQL 质检影子差异摘要仓储。"""

from __future__ import annotations

import json
from typing import Any

from services.api.app.application.pipeline_shadow import ShadowComparison
from services.api.app.infrastructure.db.pool import get_postgres_pool


class PostgresPipelineShadowRepository:
    """只写结论摘要和证据 ID，禁止保存原始页面内容。"""

    def __init__(self, *, database_url: str | None = None, pool: Any | None = None) -> None:
        self._database_url = database_url
        self._pool = pool

    async def save_comparison(
        self, *, comparison: ShadowComparison, difference_type: str
    ) -> str:
        pool = self._pool or await get_postgres_pool(self._database_url)
        async with pool.acquire() as connection:
            value = await connection.fetchval(
                """
                INSERT INTO qc_pipeline_shadow_comparison (
                    task_id, agent_run_id, page_capture_id, rule_id, rule_code,
                    comparison_mode, legacy_conclusion, candidate_conclusion,
                    difference_type, legacy_duration_ms, candidate_duration_ms,
                    legacy_input_tokens, candidate_input_tokens,
                    legacy_evidence_artifact_ids, candidate_evidence_artifact_ids,
                    summary
                )
                VALUES (
                    $1::uuid, $2::uuid, $3::uuid, $4::uuid, $5, $6, $7, $8,
                    $9, $10, $11, $12, $13, $14::jsonb, $15::jsonb, $16::jsonb
                )
                RETURNING id::text
                """,
                comparison.task_id,
                comparison.agent_run_id,
                comparison.page_capture_id,
                comparison.rule_id,
                comparison.rule_code,
                comparison.comparison_mode,
                comparison.legacy_conclusion,
                comparison.candidate_conclusion,
                difference_type,
                comparison.legacy_duration_ms,
                comparison.candidate_duration_ms,
                comparison.legacy_input_tokens,
                comparison.candidate_input_tokens,
                json.dumps(list(comparison.legacy_evidence_artifact_ids)),
                json.dumps(list(comparison.candidate_evidence_artifact_ids)),
                json.dumps(comparison.summary or {}, ensure_ascii=False),
            )
        return str(value)

    async def list_gate_rows(self, *, limit: int) -> list[dict[str, Any]]:
        """读取最近判定回放摘要；严重级别通过规则表关联，不复制规则正文。"""
        pool = self._pool or await get_postgres_pool(self._database_url)
        async with pool.acquire() as connection:
            rows = await connection.fetch(
                """
                SELECT
                    comparison.rule_code,
                    rule.severity,
                    comparison.legacy_conclusion,
                    comparison.candidate_conclusion
                FROM qc_pipeline_shadow_comparison AS comparison
                LEFT JOIN qc_rule AS rule ON rule.id = comparison.rule_id
                WHERE comparison.comparison_mode = 'historical_replay'
                ORDER BY comparison.created_at DESC
                LIMIT $1
                """,
                min(max(limit, 1), 10_000),
            )
        return [dict(row) for row in rows]
