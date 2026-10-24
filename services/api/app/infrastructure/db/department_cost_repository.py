"""部门 Token 消耗与费用读模型（2.1 原型「Token 与费用」）。

用量事件按任务挂公司（qc_task.tenant_id），部门取任务配置里的产品部门；金额沿用事件落库时
按当时单价算出的 total_cost，价格版本从事件里出现过的单价组合反推，不回写历史。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from services.api.app.infrastructure.db.pool import get_postgres_pool

# 工作台把没填的部门存成 '-'，和空串一样按「未填写部门」归并。
_DEPARTMENT_EXPR = (
    "COALESCE(NULLIF(NULLIF(btrim(task.task_config->'product'->>'department_name'), ''), '-'), '未填写部门')"
)

_SCOPED_EVENTS = f"""
    SELECT
        event.*,
        task.tenant_id,
        {_DEPARTMENT_EXPR} AS department_name,
        COALESCE(NULLIF(task.task_config->>'task_name', ''), task.target_url, task.id::text) AS task_name
    FROM qc_llm_usage_event AS event
    JOIN qc_task AS task ON task.id = event.task_id
    WHERE event.created_at >= $1 AND event.created_at < $2
      AND ($3::varchar IS NULL OR event.provider = $3)
      AND ($4::uuid IS NULL OR task.tenant_id = $4::uuid)
"""


class PostgresDepartmentCostRepository:
    def __init__(self, *, database_url: str | None = None, pool: Any | None = None) -> None:
        self._database_url = database_url
        self._pool = pool

    async def month_totals(
        self, *, start: datetime, end: datetime, provider: str | None, tenant_id: str | None
    ) -> dict[str, Any]:
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                f"""
                WITH scoped AS ({_SCOPED_EVENTS})
                SELECT
                    COALESCE(SUM(input_tokens), 0)::bigint AS input_tokens,
                    COALESCE(SUM(output_tokens), 0)::bigint AS output_tokens,
                    COALESCE(SUM(cache_hit_tokens), 0)::bigint AS cache_hit_tokens,
                    COALESCE(SUM(total_tokens), 0)::bigint AS total_tokens,
                    COALESCE(SUM(ai_calls), 0)::bigint AS ai_calls,
                    COALESCE(SUM(total_cost), 0)::float8 AS total_cost,
                    COUNT(*) FILTER (WHERE NOT pricing_configured)::int AS unpriced_calls,
                    COUNT(DISTINCT task_id)::int AS task_count,
                    COALESCE(MAX(cost_currency), 'CNY') AS currency
                FROM scoped
                """,
                start,
                end,
                provider,
                tenant_id,
            )
        return dict(row or {})

    async def department_rows(
        self, *, start: datetime, end: datetime, provider: str | None, tenant_id: str | None
    ) -> list[dict[str, Any]]:
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            rows = await connection.fetch(
                f"""
                WITH scoped AS ({_SCOPED_EVENTS})
                SELECT
                    scoped.tenant_id::text AS tenant_id,
                    COALESCE(tenant.display_name, '') AS tenant_name,
                    scoped.department_name,
                    COALESCE(SUM(scoped.input_tokens), 0)::bigint AS input_tokens,
                    COALESCE(SUM(scoped.output_tokens), 0)::bigint AS output_tokens,
                    COALESCE(SUM(scoped.cache_hit_tokens), 0)::bigint AS cache_hit_tokens,
                    COALESCE(SUM(scoped.total_tokens), 0)::bigint AS total_tokens,
                    COALESCE(SUM(scoped.ai_calls), 0)::bigint AS ai_calls,
                    COALESCE(SUM(scoped.total_cost), 0)::float8 AS total_cost,
                    COUNT(DISTINCT scoped.task_id)::int AS task_count,
                    mode() WITHIN GROUP (ORDER BY scoped.provider) AS main_provider,
                    mode() WITHIN GROUP (ORDER BY scoped.model) AS main_model
                FROM scoped
                LEFT JOIN qc_tenant AS tenant ON tenant.id = scoped.tenant_id
                GROUP BY scoped.tenant_id, tenant.display_name, scoped.department_name
                ORDER BY total_cost DESC, total_tokens DESC
                """,
                start,
                end,
                provider,
                tenant_id,
            )
        return [dict(row) for row in rows]

    async def task_rows(
        self,
        *,
        start: datetime,
        end: datetime,
        provider: str | None,
        tenant_id: str | None,
        department_name: str | None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            rows = await connection.fetch(
                f"""
                WITH scoped AS ({_SCOPED_EVENTS})
                SELECT
                    scoped.task_id::text AS task_id,
                    MAX(scoped.task_name) AS task_name,
                    scoped.department_name,
                    mode() WITHIN GROUP (ORDER BY scoped.provider) AS provider,
                    mode() WITHIN GROUP (ORDER BY scoped.model) AS model,
                    COALESCE(SUM(scoped.ai_calls), 0)::bigint AS ai_calls,
                    COALESCE(SUM(scoped.input_tokens), 0)::bigint AS input_tokens,
                    COALESCE(SUM(scoped.output_tokens), 0)::bigint AS output_tokens,
                    COALESCE(SUM(scoped.total_tokens), 0)::bigint AS total_tokens,
                    COALESCE(SUM(scoped.total_cost), 0)::float8 AS total_cost,
                    MIN(scoped.created_at) AS first_call_at,
                    MAX(scoped.created_at) AS last_call_at
                FROM scoped
                WHERE ($5::varchar IS NULL OR scoped.department_name = $5)
                GROUP BY scoped.task_id, scoped.department_name
                ORDER BY total_cost DESC, total_tokens DESC
                LIMIT $6
                """,
                start,
                end,
                provider,
                tenant_id,
                department_name,
                limit,
            )
        return [dict(row) for row in rows]

    async def price_versions(self) -> list[dict[str, Any]]:
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            rows = await connection.fetch(
                """
                SELECT
                    input_price_per_million_tokens::float8 AS input_price,
                    output_price_per_million_tokens::float8 AS output_price,
                    cache_hit_price_per_million_tokens::float8 AS cache_hit_price,
                    pricing_source,
                    bool_and(pricing_configured) AS pricing_configured,
                    COALESCE(MAX(cost_currency), 'CNY') AS currency,
                    MIN(created_at) AS effective_from,
                    MAX(created_at) AS last_seen_at,
                    COUNT(*)::int AS event_count
                FROM qc_llm_usage_event
                GROUP BY 1, 2, 3, 4
                ORDER BY MIN(created_at) DESC
                """
            )
        return [dict(row) for row in rows]

    async def providers(self) -> list[str]:
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            rows = await connection.fetch(
                "SELECT DISTINCT provider FROM qc_llm_usage_event ORDER BY provider"
            )
        return [str(row["provider"]) for row in rows]

    async def _get_pool(self) -> Any:
        if self._pool is not None:
            return self._pool
        return await get_postgres_pool(self._database_url)
