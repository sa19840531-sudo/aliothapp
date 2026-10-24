"""LLM 调用流水审计仓储。"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from services.api.app.application.run_usage import merge_usage_payloads
from services.api.app.infrastructure.db.pool import get_postgres_pool


class PostgresLLMUsageEventRepository:
    """持久化所有 OpenAI-compatible LLM 调用的计量流水。"""

    def __init__(self, *, database_url: str | None = None, pool: Any = None) -> None:
        self._database_url = database_url
        self._pool = pool

    async def record(self, payload: Mapping[str, Any]) -> None:
        """写入一次 LLM 调用计量流水。"""
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            await connection.execute(
                """
                INSERT INTO qc_llm_usage_event (
                    scope_type,
                    task_id,
                    agent_run_id,
                    source,
                    operation,
                    provider,
                    model,
                    api_style,
                    usage_available,
                    ai_calls,
                    input_tokens,
                    output_tokens,
                    cache_hit_tokens,
                    billable_input_tokens,
                    total_tokens,
                    input_cost,
                    output_cost,
                    cache_hit_cost,
                    total_cost,
                    cost_currency,
                    pricing_source,
                    pricing_unit,
                    pricing_configured,
                    pricing_tier,
                    input_price_per_million_tokens,
                    output_price_per_million_tokens,
                    cache_hit_price_per_million_tokens,
                    calculation,
                    vendor_usage_payload,
                    metadata
                )
                VALUES (
                    $1,
                    $2::uuid,
                    $3::uuid,
                    $4,
                    $5,
                    $6,
                    $7,
                    $8,
                    $9,
                    $10,
                    $11,
                    $12,
                    $13,
                    $14,
                    $15,
                    $16,
                    $17,
                    $18,
                    $19,
                    $20,
                    $21,
                    $22,
                    $23,
                    $24,
                    $25,
                    $26,
                    $27,
                    $28::jsonb,
                    $29::jsonb,
                    $30::jsonb
                )
                """,
                _text(payload.get("scope_type")) or "unbound",
                _text(payload.get("task_id")),
                _text(payload.get("agent_run_id")),
                _text(payload.get("source")) or "unknown",
                _text(payload.get("operation")),
                _text(payload.get("provider")) or "unknown",
                _text(payload.get("model")) or "unknown",
                _text(payload.get("api_style")) or "unknown",
                bool(payload.get("usage_available")),
                _nonnegative_int(payload.get("ai_calls")),
                _nonnegative_int(payload.get("input_tokens")),
                _nonnegative_int(payload.get("output_tokens")),
                _nonnegative_int(payload.get("cache_hit_tokens")),
                _nonnegative_int(payload.get("billable_input_tokens")),
                _nonnegative_int(payload.get("total_tokens")),
                _nonnegative_float(payload.get("input_cost")),
                _nonnegative_float(payload.get("output_cost")),
                _nonnegative_float(payload.get("cache_hit_cost")),
                _nonnegative_float(payload.get("total_cost")),
                (_text(payload.get("cost_currency")) or "CNY").upper(),
                _text(payload.get("pricing_source")) or "runtime_config_per_call",
                _text(payload.get("pricing_unit")) or "1m_tokens",
                bool(payload.get("pricing_configured")),
                _text(payload.get("pricing_tier")),
                _nonnegative_float(payload.get("input_price_per_million_tokens")),
                _nonnegative_float(payload.get("output_price_per_million_tokens")),
                _nonnegative_float(payload.get("cache_hit_price_per_million_tokens")),
                json.dumps(payload.get("calculation") or {}, ensure_ascii=False, default=str),
                json.dumps(
                    payload.get("vendor_usage_payload") or {},
                    ensure_ascii=False,
                    default=str,
                ),
                json.dumps(payload.get("metadata") or {}, ensure_ascii=False, default=str),
            )

    async def list_recent_events(self, *, days: int) -> list[dict[str, Any]]:
        """查询最近 N 天的 LLM 调用计量流水。"""
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            rows = await connection.fetch(
                """
                SELECT
                    event.id::text AS event_id,
                    event.scope_type,
                    event.task_id::text AS task_id,
                    task.task_config->>'task_name' AS task_name,
                    event.agent_run_id::text AS agent_run_id,
                    run.agent_type,
                    run.agent_name,
                    run.status AS run_status,
                    event.source,
                    event.operation,
                    event.provider,
                    event.model,
                    event.api_style,
                    event.usage_available,
                    event.ai_calls,
                    event.input_tokens,
                    event.output_tokens,
                    event.cache_hit_tokens,
                    event.billable_input_tokens,
                    event.total_tokens,
                    event.input_cost::double precision AS input_cost,
                    event.output_cost::double precision AS output_cost,
                    event.cache_hit_cost::double precision AS cache_hit_cost,
                    event.total_cost::double precision AS total_cost,
                    event.cost_currency,
                    event.pricing_source,
                    event.pricing_unit,
                    event.pricing_configured,
                    event.pricing_tier,
                    event.input_price_per_million_tokens::double precision
                        AS input_price_per_million_tokens,
                    event.output_price_per_million_tokens::double precision
                        AS output_price_per_million_tokens,
                    event.cache_hit_price_per_million_tokens::double precision
                        AS cache_hit_price_per_million_tokens,
                    event.calculation,
                    event.metadata,
                    event.created_at
                FROM qc_llm_usage_event AS event
                LEFT JOIN qc_task AS task ON task.id = event.task_id
                LEFT JOIN qc_agent_run AS run ON run.id = event.agent_run_id
                WHERE event.created_at >= now() - ($1::integer * interval '1 day')
                ORDER BY event.created_at DESC
                """,
                days,
            )
        return [dict(row) for row in rows]

    async def aggregate_for_agent_run(self, *, agent_run_id: str) -> dict[str, Any] | None:
        """按 Run 聚合已落库的 LLM 调用流水。"""

        pool = await self._get_pool()
        async with pool.acquire() as connection:
            rows = await connection.fetch(
                """
                SELECT
                    source,
                    operation,
                    provider,
                    model,
                    usage_available,
                    ai_calls,
                    input_tokens,
                    output_tokens,
                    cache_hit_tokens,
                    billable_input_tokens,
                    total_tokens,
                    input_cost::double precision AS input_cost,
                    output_cost::double precision AS output_cost,
                    cache_hit_cost::double precision AS cache_hit_cost,
                    total_cost::double precision AS total_cost,
                    cost_currency,
                    pricing_source,
                    pricing_unit,
                    pricing_configured,
                    pricing_tier,
                    input_price_per_million_tokens::double precision
                        AS input_price_per_million_tokens,
                    output_price_per_million_tokens::double precision
                        AS output_price_per_million_tokens,
                    cache_hit_price_per_million_tokens::double precision
                        AS cache_hit_price_per_million_tokens,
                    calculation,
                    created_at
                FROM qc_llm_usage_event
                WHERE agent_run_id = $1::uuid
                ORDER BY created_at, id
                """,
                agent_run_id,
            )
        return _aggregate_usage_rows(rows)

    async def usage_totals_for_agent_run(self, *, agent_run_id: str) -> tuple[int, int]:
        """读取同一 Run 跨消费者已落库的调用次数和 Token 用量。"""
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                SELECT COALESCE(SUM(ai_calls), 0)::bigint AS ai_calls,
                       COALESCE(SUM(total_tokens), 0)::bigint AS total_tokens
                FROM qc_llm_usage_event
                WHERE agent_run_id = $1::uuid
                """,
                agent_run_id,
            )
        return int(row["ai_calls"]), int(row["total_tokens"])

    async def usage_totals_for_task(self, *, task_id: str) -> tuple[int, int]:
        """H5 预算计入同 Task 的探索、逻辑消费与补采 Run，不借用别的任务流水。"""
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                SELECT COALESCE(SUM(ai_calls), 0)::bigint AS ai_calls,
                       COALESCE(SUM(total_tokens), 0)::bigint AS total_tokens
                FROM qc_llm_usage_event
                WHERE task_id = $1::uuid
                """,
                task_id,
            )
        return int(row["ai_calls"]), int(row["total_tokens"])

    async def close(self) -> None:
        if self._pool is not None and hasattr(self._pool, "close"):
            await self._pool.close()

    async def _get_pool(self) -> Any:
        if self._pool is not None:
            return self._pool
        return await get_postgres_pool(self._database_url)


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _nonnegative_int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return max(0, value)
    if isinstance(value, str) and value.strip().isdigit():
        return max(0, int(value.strip()))
    return 0


def _nonnegative_float(value: Any) -> float:
    if isinstance(value, bool):
        return 0.0
    if isinstance(value, int | float):
        return max(0.0, float(value))
    if isinstance(value, str):
        try:
            return max(0.0, float(value.strip()))
        except ValueError:
            return 0.0
    return 0.0


def _aggregate_usage_rows(rows: list[Mapping[str, Any]]) -> dict[str, Any] | None:
    if not rows:
        return None
    merged: dict[str, Any] = {}
    for row in rows:
        merged = merge_usage_payloads(merged, _usage_payload_from_row(row))
    merged["usage_event_count"] = len(rows)
    return merged


def _usage_payload_from_row(row: Mapping[str, Any]) -> dict[str, Any]:
    pricing_tier = _text(row.get("pricing_tier"))
    calculation = _json_object(row.get("calculation"))
    breakdown = [
        _enrich_pricing_breakdown_item(item, row=row, calculation=calculation)
        for item in _pricing_breakdown_from_calculation(calculation)
    ] or [_pricing_breakdown_from_row(row, calculation=calculation)]
    return {
        "input_tokens": _nonnegative_int(row.get("input_tokens")),
        "output_tokens": _nonnegative_int(row.get("output_tokens")),
        "cache_hit_tokens": _nonnegative_int(row.get("cache_hit_tokens")),
        "billable_input_tokens": _nonnegative_int(row.get("billable_input_tokens")),
        "total_tokens": _nonnegative_int(row.get("total_tokens")),
        "ai_calls": _nonnegative_int(row.get("ai_calls")),
        "input_cost": _nonnegative_float(row.get("input_cost")),
        "output_cost": _nonnegative_float(row.get("output_cost")),
        "cache_hit_cost": _nonnegative_float(row.get("cache_hit_cost")),
        "total_cost": _nonnegative_float(row.get("total_cost")),
        "cost_currency": _text(row.get("cost_currency")) or "CNY",
        "pricing_source": _text(row.get("pricing_source")) or "runtime_config_per_call",
        "pricing_configured": bool(row.get("pricing_configured")),
        "applied_pricing_tiers": [pricing_tier] if pricing_tier else [],
        "pricing_breakdown": breakdown,
        "provider": _text(row.get("provider")) or "unknown",
        "model": _text(row.get("model")) or "unknown",
    }


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        if isinstance(parsed, Mapping):
            return dict(parsed)
    return {}


def _pricing_breakdown_from_calculation(
    calculation: Mapping[str, Any],
) -> list[dict[str, Any]]:
    breakdown = calculation.get("pricing_breakdown")
    if not isinstance(breakdown, list):
        return []
    return [dict(item) for item in breakdown if isinstance(item, Mapping)]


def _pricing_breakdown_from_row(
    row: Mapping[str, Any],
    *,
    calculation: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "pricing_tier": _text(row.get("pricing_tier")),
        "context_tokens": _nonnegative_int(row.get("input_tokens")),
        "input_tokens": _nonnegative_int(row.get("input_tokens")),
        "billable_input_tokens": _nonnegative_int(row.get("billable_input_tokens")),
        "output_tokens": _nonnegative_int(row.get("output_tokens")),
        "cache_hit_tokens": _nonnegative_int(row.get("cache_hit_tokens")),
        "input_price_per_million_tokens": _nonnegative_float(
            row.get("input_price_per_million_tokens")
        ),
        "output_price_per_million_tokens": _nonnegative_float(
            row.get("output_price_per_million_tokens")
        ),
        "cache_hit_price_per_million_tokens": _nonnegative_float(
            row.get("cache_hit_price_per_million_tokens")
        ),
        "input_cost": _nonnegative_float(row.get("input_cost")),
        "output_cost": _nonnegative_float(row.get("output_cost")),
        "cache_hit_cost": _nonnegative_float(row.get("cache_hit_cost")),
        "total_cost": _nonnegative_float(row.get("total_cost")),
        "pricing_unit": _text(row.get("pricing_unit")) or "1m_tokens",
        "pricing_source": _text(row.get("pricing_source")) or "runtime_config_per_call",
        "pricing_configured": bool(row.get("pricing_configured")),
        "calculation_formula": _text(calculation.get("formula")) or "",
        "ai_calls": _nonnegative_int(row.get("ai_calls")),
    }


def _enrich_pricing_breakdown_item(
    item: Mapping[str, Any],
    *,
    row: Mapping[str, Any],
    calculation: Mapping[str, Any],
) -> dict[str, Any]:
    enriched = dict(item)
    fallback = _pricing_breakdown_from_row(row, calculation=calculation)
    for key, value in fallback.items():
        enriched.setdefault(key, value)
    return enriched
