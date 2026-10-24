"""规则结论复用的只读查询：当前页面身份、规则合同、同一任务里可沿用的正式结论。"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from services.api.app.application.rule_result_reuse import (
    CurrentCaptureFacts,
    ReusableExecutionCandidate,
)
from services.api.app.infrastructure.db.pool import get_postgres_pool


class PostgresRuleReuseRepository:
    """只读；不写任何表，复用判定本身走正式写回链路。"""

    def __init__(self, *, pool: Any | None = None, database_url: str | None = None) -> None:
        self._pool = pool
        self._database_url = database_url

    async def load_capture_facts(self, *, page_capture_id: str) -> CurrentCaptureFacts | None:
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                SELECT
                    capture.id::text AS page_capture_id,
                    COALESCE(page.url, '') AS url,
                    COALESCE(page.page_type, '') AS page_type,
                    COALESCE(page.page_state, '') AS page_state,
                    COALESCE(capture.surface_kind, '') AS surface_kind,
                    capture.surface_session_id
                FROM qc_capture AS capture
                LEFT JOIN qc_page_capture AS page ON page.id = capture.id
                WHERE capture.id = $1::uuid
                """,
                page_capture_id,
            )
        if row is None:
            return None
        return CurrentCaptureFacts(
            page_capture_id=str(row["page_capture_id"]),
            url=str(row["url"] or ""),
            page_type=str(row["page_type"] or ""),
            page_state=str(row["page_state"] or ""),
            surface_kind=str(row["surface_kind"] or ""),
            surface_session_id=(
                str(row["surface_session_id"]) if row["surface_session_id"] else None
            ),
        )

    async def load_rule_contracts(
        self, *, rule_ids: Sequence[str]
    ) -> Mapping[str, Mapping[str, Any]]:
        if not rule_ids:
            return {}
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            rows = await connection.fetch(
                """
                SELECT id::text AS rule_id, input_contract
                FROM qc_rule
                WHERE id = ANY($1::uuid[])
                """,
                list(rule_ids),
            )
        return {str(row["rule_id"]): _json_object(row["input_contract"]) for row in rows}

    async def list_reusable_executions(
        self, *, task_id: str, rule_ids: Sequence[str]
    ) -> Sequence[ReusableExecutionCandidate]:
        if not rule_ids:
            return ()
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            rows = await connection.fetch(
                """
                SELECT
                    execution.id::text AS execution_id,
                    execution.rule_id::text AS rule_id,
                    rule.rule_code,
                    execution.execution_status,
                    execution.page_capture_id::text AS page_capture_id,
                    execution.agent_run_id::text AS agent_run_id,
                    COALESCE(page.url, '') AS page_url,
                    COALESCE(page.page_type, '') AS page_type,
                    COALESCE(capture.surface_kind, '') AS surface_kind,
                    member.contract_hash,
                    execution.result_summary,
                    execution.problem_desc,
                    execution.confidence,
                    execution.judge_output,
                    execution.created_at
                FROM qc_rule_execution AS execution
                INNER JOIN qc_rule AS rule ON rule.id = execution.rule_id
                LEFT JOIN qc_capture AS capture ON capture.id = execution.page_capture_id
                LEFT JOIN qc_page_capture AS page ON page.id = execution.page_capture_id
                LEFT JOIN LATERAL (
                    SELECT member.contract_hash
                    FROM qc_inspection_work_item_rule AS member
                    WHERE member.rule_execution_id = execution.id
                    ORDER BY member.updated_at DESC
                    LIMIT 1
                ) AS member ON TRUE
                WHERE execution.task_id = $1::uuid
                  AND execution.rule_id = ANY($2::uuid[])
                  AND execution.page_capture_id IS NOT NULL
                  AND execution.execution_status IN ('pass', 'fail', 'warning')
                  AND NOT (execution.judge_output ? 'reuse')
                ORDER BY execution.created_at ASC, execution.id ASC
                """,
                task_id,
                list(rule_ids),
            )
        return tuple(
            ReusableExecutionCandidate(
                execution_id=str(row["execution_id"]),
                rule_id=str(row["rule_id"]),
                rule_code=str(row["rule_code"]),
                execution_status=str(row["execution_status"]),
                page_capture_id=str(row["page_capture_id"]),
                page_url=str(row["page_url"] or ""),
                page_type=str(row["page_type"] or ""),
                surface_kind=str(row["surface_kind"] or ""),
                agent_run_id=str(row["agent_run_id"]) if row["agent_run_id"] else None,
                contract_hash=str(row["contract_hash"]) if row["contract_hash"] else None,
                result_summary=row["result_summary"],
                problem_desc=row["problem_desc"],
                confidence=float(row["confidence"]) if row["confidence"] is not None else None,
                judge_output=_json_object(row["judge_output"]),
                created_at=row["created_at"],
            )
            for row in rows
        )

    async def _get_pool(self) -> Any:
        if self._pool is not None:
            return self._pool
        return await get_postgres_pool(self._database_url)


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return dict(parsed) if isinstance(parsed, dict) else {}
    return {}


__all__ = ["PostgresRuleReuseRepository"]
