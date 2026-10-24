"""页面类型判断记录仓储：只追加；同一页面、同一来源、同样内容再记一次是幂等的。"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

from services.api.app.application.page_type_judgements import (
    PageTypeJudgementInput,
    h5_detection_judgements,
    judgement_from_mapping,
)
from services.api.app.domain.page.type_judgement import (
    PageTypeJudgement,
    build_judgement_id,
    canonical_basis_hash,
)
from services.api.app.infrastructure.db.pool import get_postgres_pool

# 同来源上一条判断页型不同 → 这条是改判并指回上一条；内容完全相同 → 幂等不重复插入。
_INSERT_SQL = """
WITH previous AS (
    SELECT judgement_id, page_type
    FROM qc_page_type_judgement
    WHERE page_capture_id = $3::uuid AND source = $5
    ORDER BY sequence_no DESC
    LIMIT 1
),
next_seq AS (
    SELECT COALESCE(MAX(sequence_no), 0) + 1 AS sequence_no
    FROM qc_page_type_judgement
    WHERE page_capture_id = $3::uuid
)
INSERT INTO qc_page_type_judgement (
    judgement_id, task_id, agent_run_id, page_capture_id, sequence_no, source,
    page_type, page_state, confidence, catalog_version, kind, revises_judgement_id,
    basis, basis_hash, task_finalization_state
)
SELECT
    $1, $2::uuid, $4::uuid, $3::uuid, next_seq.sequence_no, $5,
    $6::varchar, $7::varchar, $8::double precision, $9::varchar,
    CASE
        WHEN previous.judgement_id IS NOT NULL AND previous.page_type IS DISTINCT FROM $6::varchar
            THEN 'revision'
        ELSE 'judgement'
    END,
    CASE
        WHEN previous.judgement_id IS NOT NULL AND previous.page_type IS DISTINCT FROM $6::varchar
            THEN previous.judgement_id
        ELSE NULL
    END,
    $10::jsonb, $11,
    (SELECT finalization_state FROM qc_task WHERE id = $2::uuid)
FROM next_seq
LEFT JOIN previous ON TRUE
ON CONFLICT (page_capture_id, source, basis_hash) DO NOTHING
RETURNING
    judgement_id, task_id::text AS task_id, agent_run_id::text AS agent_run_id,
    page_capture_id::text AS page_capture_id, sequence_no, source, page_type, page_state,
    confidence, catalog_version, kind, revises_judgement_id, basis, basis_hash,
    task_finalization_state, created_at
"""

_LIST_SQL = """
SELECT
    judgement_id, task_id::text AS task_id, agent_run_id::text AS agent_run_id,
    page_capture_id::text AS page_capture_id, sequence_no, source, page_type, page_state,
    confidence, catalog_version, kind, revises_judgement_id, basis, basis_hash,
    task_finalization_state, created_at
FROM qc_page_type_judgement
WHERE task_id = $1::uuid
  AND ($2::uuid IS NULL OR agent_run_id = $2::uuid)
ORDER BY page_capture_id, sequence_no
"""


class PostgresPageTypeJudgementRepository:
    def __init__(self, *, pool: Any | None = None, database_url: str | None = None) -> None:
        self._pool = pool
        self._database_url = database_url

    async def record(
        self, *, inputs: Sequence[PageTypeJudgementInput]
    ) -> Sequence[PageTypeJudgement]:
        if not inputs:
            return ()
        pool = await self._get_pool()
        async with pool.acquire() as connection, connection.transaction():
            return await self.record_on_connection(connection, inputs=inputs)

    async def record_on_connection(
        self, connection: Any, *, inputs: Sequence[PageTypeJudgementInput]
    ) -> Sequence[PageTypeJudgement]:
        """在调用方事务里追加判断；同页面串行化，序号不撞。"""

        recorded: list[PageTypeJudgement] = []
        for item in inputs:
            await connection.execute(
                "SELECT pg_advisory_xact_lock(hashtext($1))",
                f"page-type-judgement:{item.page_capture_id}",
            )
            basis_hash = canonical_basis_hash(
                page_type=item.page_type,
                page_state=item.page_state,
                catalog_version=item.catalog_version,
                basis=item.basis,
            )
            judgement_id = build_judgement_id(
                task_id=item.task_id,
                page_capture_id=item.page_capture_id,
                source=item.source,
                basis_hash=basis_hash,
            )
            row = await connection.fetchrow(
                _INSERT_SQL,
                judgement_id,
                item.task_id,
                item.page_capture_id,
                item.agent_run_id,
                item.source,
                item.page_type,
                item.page_state or None,
                item.confidence,
                item.catalog_version or None,
                json.dumps(dict(item.basis), ensure_ascii=False),
                basis_hash,
            )
            if row is not None:
                recorded.append(_judgement_from_row(row))
        return recorded

    async def record_detection_on_connection(self, connection: Any, **detection: Any) -> None:
        """识别写库同事务挂接：一次识别拆成结构规则与模型各自的判断。"""

        await self.record_on_connection(
            connection, inputs=h5_detection_judgements(**detection)
        )

    async def list_for_task(
        self, *, task_id: str, agent_run_id: str | None = None
    ) -> Sequence[PageTypeJudgement]:
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            rows = await connection.fetch(_LIST_SQL, task_id, agent_run_id)
        return tuple(_judgement_from_row(row) for row in rows)

    async def _get_pool(self) -> Any:
        if self._pool is not None:
            return self._pool
        return await get_postgres_pool(self._database_url)


def _judgement_from_row(row: Any) -> PageTypeJudgement:
    return judgement_from_mapping(dict(row))


__all__ = ["PostgresPageTypeJudgementRepository"]
