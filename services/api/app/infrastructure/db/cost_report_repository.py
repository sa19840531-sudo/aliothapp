"""LLM 成本报表查询仓储。"""

from typing import Any

from services.api.app.infrastructure.db.pool import get_postgres_pool


class PostgresCostReportRepository:
    """从 Agent Run 审计表查询成本报表基础数据。"""

    def __init__(self, *, database_url: str | None = None, pool: Any = None) -> None:
        self._database_url = database_url
        self._pool = pool

    async def list_recent_runs(self, *, days: int) -> list[dict[str, Any]]:
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            rows = await connection.fetch(
                """
                WITH recent_runs AS MATERIALIZED (
                    -- 运行上下文包含大量轨迹，计量子对象只解压提取一次。
                    SELECT id, task_id, agent_type, agent_name, status,
                           created_at, started_at, finished_at,
                           total_ai_calls, total_tokens,
                           output_context->'usage' AS usage
                    FROM qc_agent_run
                    WHERE created_at >= now() - ($1::integer * interval '1 day')
                ), task_usage_ledger AS (
                    SELECT
                        resolved_event.resolved_task_id AS task_id,
                        count(*)::integer AS task_usage_event_count,
                        count(*) FILTER (WHERE NOT resolved_event.usage_available)::integer
                            AS task_usage_missing_count,
                        COALESCE(sum(resolved_event.ai_calls), 0)::integer
                            AS task_usage_ai_calls,
                        COALESCE(sum(resolved_event.input_tokens), 0)::bigint
                            AS task_usage_input_tokens,
                        COALESCE(sum(resolved_event.output_tokens), 0)::bigint
                            AS task_usage_output_tokens,
                        COALESCE(sum(resolved_event.cache_hit_tokens), 0)::bigint
                            AS task_usage_cache_hit_tokens,
                        COALESCE(sum(resolved_event.billable_input_tokens), 0)::bigint
                            AS task_usage_billable_input_tokens,
                        COALESCE(sum(resolved_event.total_tokens), 0)::bigint
                            AS task_usage_total_tokens,
                        COALESCE(sum(resolved_event.input_cost), 0)::double precision
                            AS task_usage_input_cost,
                        COALESCE(sum(resolved_event.output_cost), 0)::double precision
                            AS task_usage_output_cost,
                        COALESCE(sum(resolved_event.cache_hit_cost), 0)::double precision
                            AS task_usage_cache_hit_cost,
                        COALESCE(sum(resolved_event.total_cost), 0)::double precision
                            AS task_usage_total_cost,
                        bool_or(resolved_event.pricing_configured)
                            AS task_usage_pricing_configured,
                        COALESCE(
                            jsonb_agg(DISTINCT resolved_event.cost_currency)
                                FILTER (
                                    WHERE resolved_event.cost_currency IS NOT NULL
                                      AND btrim(resolved_event.cost_currency) <> ''
                                ),
                            '[]'::jsonb
                        ) AS task_usage_cost_currencies,
                        COALESCE(
                            jsonb_agg(DISTINCT resolved_event.provider)
                                FILTER (
                                    WHERE resolved_event.provider IS NOT NULL
                                      AND btrim(resolved_event.provider) <> ''
                                ),
                            '[]'::jsonb
                        ) AS task_usage_providers,
                        COALESCE(
                            jsonb_agg(DISTINCT resolved_event.model)
                                FILTER (
                                    WHERE resolved_event.model IS NOT NULL
                                      AND btrim(resolved_event.model) <> ''
                                ),
                            '[]'::jsonb
                        ) AS task_usage_models,
                        COALESCE(
                            jsonb_agg(DISTINCT resolved_event.pricing_source)
                                FILTER (
                                    WHERE resolved_event.pricing_source IS NOT NULL
                                      AND btrim(resolved_event.pricing_source) <> ''
                                ),
                            '[]'::jsonb
                        ) AS task_usage_pricing_sources,
                        COALESCE(
                            jsonb_agg(DISTINCT resolved_event.pricing_tier)
                                FILTER (
                                    WHERE resolved_event.pricing_tier IS NOT NULL
                                      AND btrim(resolved_event.pricing_tier) <> ''
                                ),
                            '[]'::jsonb
                        ) AS task_usage_applied_pricing_tiers,
                        COALESCE(
                            jsonb_agg(
                                jsonb_build_object(
                                    'pricing_tier',
                                        resolved_event.pricing_tier,
                                    'context_tokens',
                                        resolved_event.input_tokens,
                                    'input_tokens',
                                        resolved_event.input_tokens,
                                    'billable_input_tokens',
                                        resolved_event.billable_input_tokens,
                                    'output_tokens',
                                        resolved_event.output_tokens,
                                    'cache_hit_tokens',
                                        resolved_event.cache_hit_tokens,
                                    'input_price_per_million_tokens',
                                        resolved_event.input_price_per_million_tokens,
                                    'output_price_per_million_tokens',
                                        resolved_event.output_price_per_million_tokens,
                                    'cache_hit_price_per_million_tokens',
                                        resolved_event.cache_hit_price_per_million_tokens,
                                    'input_cost',
                                        resolved_event.input_cost,
                                    'output_cost',
                                        resolved_event.output_cost,
                                    'cache_hit_cost',
                                        resolved_event.cache_hit_cost,
                                    'total_cost',
                                        resolved_event.total_cost,
                                    'pricing_unit',
                                        resolved_event.pricing_unit,
                                    'pricing_source',
                                        resolved_event.pricing_source,
                                    'pricing_configured',
                                        resolved_event.pricing_configured,
                                    'calculation_formula',
                                        resolved_event.calculation->>'formula',
                                    'ai_calls',
                                        resolved_event.ai_calls
                                )
                                ORDER BY resolved_event.created_at
                            )
                                FILTER (
                                    WHERE resolved_event.ai_calls > 0
                                       OR resolved_event.total_tokens > 0
                                       OR resolved_event.total_cost > 0
                                ),
                            '[]'::jsonb
                        ) AS task_usage_pricing_breakdown
                    FROM (
                        SELECT
                            COALESCE(event.task_id, event_run.task_id) AS resolved_task_id,
                            event.*
                        FROM qc_llm_usage_event AS event
                        LEFT JOIN qc_agent_run AS event_run
                            ON event_run.id = event.agent_run_id
                        WHERE event.created_at
                            >= now() - ($1::integer * interval '1 day')
                    ) AS resolved_event
                    WHERE resolved_event.resolved_task_id IS NOT NULL
                    GROUP BY resolved_event.resolved_task_id
                ),
                run_usage_ledger AS (
                    SELECT
                        event.agent_run_id,
                        count(*) FILTER (WHERE NOT event.usage_available)::integer
                            AS run_usage_missing_count
                    FROM qc_llm_usage_event AS event
                    WHERE event.created_at
                        >= now() - ($1::integer * interval '1 day')
                      AND event.agent_run_id IS NOT NULL
                    GROUP BY event.agent_run_id
                )
                SELECT
                    run.id::text AS run_id,
                    run.task_id::text AS task_id,
                    task.task_config->>'task_name' AS task_name,
                    task.status AS task_status,
                    run.agent_type,
                    run.agent_name,
                    run.status,
                    run.created_at,
                    run.started_at,
                    run.finished_at,
                    COALESCE(run.total_ai_calls, 0)::integer AS ai_calls,
                    COALESCE(run.total_tokens, 0)::integer AS total_tokens,
                    CASE
                        WHEN jsonb_typeof(
                            run.usage->'input_tokens'
                        ) = 'number'
                        THEN (
                            run.usage->>'input_tokens'
                        )::integer
                        ELSE 0
                    END AS input_tokens,
                    CASE
                        WHEN jsonb_typeof(
                            run.usage->'output_tokens'
                        ) = 'number'
                        THEN (
                            run.usage->>'output_tokens'
                        )::integer
                        ELSE 0
                    END AS output_tokens,
                    CASE
                        WHEN jsonb_typeof(
                            run.usage->'cache_hit_tokens'
                        ) = 'number'
                        THEN (
                            run.usage->>'cache_hit_tokens'
                        )::integer
                        ELSE 0
                    END AS cache_hit_tokens,
                    CASE
                        WHEN jsonb_typeof(
                            run.usage->'billable_input_tokens'
                        ) = 'number'
                        THEN (
                            run.usage->>'billable_input_tokens'
                        )::integer
                        ELSE 0
                    END AS billable_input_tokens,
                    CASE
                        WHEN jsonb_typeof(
                            run.usage->'input_cost'
                        ) = 'number'
                        THEN (
                            run.usage->>'input_cost'
                        )::double precision
                    END AS input_cost,
                    CASE
                        WHEN jsonb_typeof(
                            run.usage->'output_cost'
                        ) = 'number'
                        THEN (
                            run.usage->>'output_cost'
                        )::double precision
                    END AS output_cost,
                    CASE
                        WHEN jsonb_typeof(
                            run.usage->'cache_hit_cost'
                        ) = 'number'
                        THEN (
                            run.usage->>'cache_hit_cost'
                        )::double precision
                    END AS cache_hit_cost,
                    CASE
                        WHEN jsonb_typeof(
                            run.usage->'total_cost'
                        ) = 'number'
                        THEN (
                            run.usage->>'total_cost'
                        )::double precision
                    END AS total_cost,
                    run.usage->>'cost_currency' AS cost_currency,
                    run.usage->>'pricing_source' AS pricing_source,
                    CASE
                        WHEN jsonb_typeof(
                            run.usage->'pricing_configured'
                        ) = 'boolean'
                        THEN (
                            run.usage->>'pricing_configured'
                        )::boolean
                    END AS pricing_configured,
                    CASE
                        WHEN jsonb_typeof(
                            run.usage->'applied_pricing_tiers'
                        ) = 'array'
                        THEN run.usage->'applied_pricing_tiers'
                        ELSE '[]'::jsonb
                    END AS applied_pricing_tiers,
                    CASE
                        WHEN jsonb_typeof(
                            run.usage->'pricing_breakdown'
                        ) = 'array'
                        THEN run.usage->'pricing_breakdown'
                        ELSE '[]'::jsonb
                    END AS pricing_breakdown,
                    run.usage->>'provider' AS provider,
                    run.usage->>'model' AS model,
                    ledger.task_usage_event_count,
                    ledger.task_usage_missing_count,
                    ledger.task_usage_ai_calls,
                    ledger.task_usage_input_tokens,
                    ledger.task_usage_output_tokens,
                    ledger.task_usage_cache_hit_tokens,
                    ledger.task_usage_billable_input_tokens,
                    ledger.task_usage_total_tokens,
                    ledger.task_usage_input_cost,
                    ledger.task_usage_output_cost,
                    ledger.task_usage_cache_hit_cost,
                    ledger.task_usage_total_cost,
                    ledger.task_usage_pricing_configured,
                    ledger.task_usage_cost_currencies,
                    ledger.task_usage_providers,
                    ledger.task_usage_models,
                    ledger.task_usage_pricing_sources,
                    ledger.task_usage_applied_pricing_tiers,
                    ledger.task_usage_pricing_breakdown,
                    run_ledger.run_usage_missing_count
                FROM recent_runs AS run
                LEFT JOIN qc_task AS task ON task.id = run.task_id
                LEFT JOIN task_usage_ledger AS ledger
                    ON ledger.task_id = run.task_id
                LEFT JOIN run_usage_ledger AS run_ledger
                    ON run_ledger.agent_run_id = run.id
                WHERE run.created_at >= now() - ($1::integer * interval '1 day')
                ORDER BY run.created_at DESC
                """,
                days,
            )
        return [dict(row) for row in rows]

    async def close(self) -> None:
        if self._pool is not None and hasattr(self._pool, "close"):
            await self._pool.close()

    async def _get_pool(self) -> Any:
        if self._pool is not None:
            return self._pool
        return await get_postgres_pool(self._database_url)
