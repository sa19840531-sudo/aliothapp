"""LLM 成本报表聚合。"""

import json
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from services.api.app.application.run_costs import estimate_llm_run_cost
from services.api.app.core.config import LLMSettings


@dataclass(frozen=True)
class CostRunItem:
    """单次 Agent Run 成本明细。"""

    run_id: str
    task_id: str | None
    task_name: str | None
    task_status: str | None
    agent_type: str
    agent_name: str
    status: str
    provider: str
    model: str
    currency: str
    ai_calls: int
    usage_missing_count: int
    input_tokens: int
    output_tokens: int
    cache_hit_tokens: int
    billable_input_tokens: int
    total_tokens: int
    input_cost: float
    output_cost: float
    cache_hit_cost: float
    total_cost: float
    pricing_source: str
    pricing_configured: bool
    applied_pricing_tiers: tuple[str, ...]
    pricing_breakdown: tuple[dict[str, Any], ...]
    started_at: datetime | None
    finished_at: datetime | None
    created_at: datetime


@dataclass(frozen=True)
class CostTaskItem:
    """单个任务聚合后的成本明细。"""

    task_id: str | None
    task_name: str | None
    status: str
    currency: str
    run_count: int
    latest_run_id: str | None
    latest_run_status: str
    latest_run_created_at: datetime
    provider: str
    model: str
    ai_calls: int
    usage_missing_count: int
    input_tokens: int
    output_tokens: int
    cache_hit_tokens: int
    billable_input_tokens: int
    total_tokens: int
    input_cost: float
    output_cost: float
    cache_hit_cost: float
    total_cost: float
    pricing_source: str
    applied_pricing_tiers: tuple[str, ...]
    pricing_breakdown: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class CostBucket:
    """成本报表分组聚合。"""

    key: str
    run_count: int
    ai_calls: int
    usage_missing_count: int
    total_tokens: int
    total_cost: float


@dataclass(frozen=True)
class CostUsageEventItem:
    """单次 LLM 调用流水成本明细。"""

    event_id: str
    scope_type: str
    task_id: str | None
    task_name: str | None
    agent_run_id: str | None
    agent_type: str | None
    agent_name: str | None
    run_status: str | None
    source: str
    operation: str | None
    provider: str
    model: str
    api_style: str
    usage_available: bool
    ai_calls: int
    input_tokens: int
    output_tokens: int
    cache_hit_tokens: int
    billable_input_tokens: int
    total_tokens: int
    input_cost: float
    output_cost: float
    cache_hit_cost: float
    total_cost: float
    currency: str
    pricing_source: str
    pricing_unit: str
    pricing_configured: bool
    pricing_tier: str | None
    input_price_per_million_tokens: float
    output_price_per_million_tokens: float
    cache_hit_price_per_million_tokens: float
    calculation_formula: str
    created_at: datetime


@dataclass(frozen=True)
class CostUsageEventReport:
    """全量 LLM 调用流水成本报表。"""

    currency: str
    days: int
    generated_at: datetime
    event_count: int
    task_event_count: int
    debug_event_count: int
    unbound_event_count: int
    ai_calls: int
    input_tokens: int
    output_tokens: int
    cache_hit_tokens: int
    billable_input_tokens: int
    total_tokens: int
    input_cost: float
    output_cost: float
    cache_hit_cost: float
    total_cost: float
    usage_missing_count: int
    by_scope: tuple[CostBucket, ...]
    by_source: tuple[CostBucket, ...]
    by_model: tuple[CostBucket, ...]
    recent_events: tuple[CostUsageEventItem, ...]


@dataclass(frozen=True)
class CostReport:
    """LLM 成本报表。"""

    currency: str
    days: int
    generated_at: datetime
    run_count: int
    completed_run_count: int
    failed_run_count: int
    ai_calls: int
    usage_missing_count: int
    input_tokens: int
    output_tokens: int
    cache_hit_tokens: int
    billable_input_tokens: int
    total_tokens: int
    input_cost: float
    output_cost: float
    cache_hit_cost: float
    total_cost: float
    average_run_cost: float
    average_call_cost: float
    by_day: tuple[CostBucket, ...]
    by_model: tuple[CostBucket, ...]
    by_status: tuple[CostBucket, ...]
    task_items: tuple[CostTaskItem, ...]
    recent_runs: tuple[CostRunItem, ...]


def build_cost_report(
    rows: Sequence[Mapping[str, Any]],
    *,
    llm_settings: LLMSettings,
    days: int,
    recent_run_limit: int,
    generated_at: datetime,
) -> CostReport:
    """把 Run 查询结果聚合为前端可直接展示的成本报表。"""
    pairs = tuple(
        (_row_to_item(row, llm_settings=llm_settings), row)
        for row in rows
    )
    items = tuple(item for item, _row in pairs)
    task_items = _aggregate_task_items(pairs)
    run_count = len(items)
    completed_run_count = sum(1 for item in items if item.status in {"completed", "done"})
    failed_run_count = sum(1 for item in items if item.status in {"failed", "error"})
    accounting_items = task_items
    ai_calls = sum(item.ai_calls for item in accounting_items)
    usage_missing_count = sum(item.usage_missing_count for item in accounting_items)
    input_tokens = sum(item.input_tokens for item in accounting_items)
    output_tokens = sum(item.output_tokens for item in accounting_items)
    cache_hit_tokens = sum(item.cache_hit_tokens for item in accounting_items)
    billable_input_tokens = sum(item.billable_input_tokens for item in accounting_items)
    total_tokens = sum(item.total_tokens for item in accounting_items)
    input_cost = _sum_cost(item.input_cost for item in accounting_items)
    output_cost = _sum_cost(item.output_cost for item in accounting_items)
    cache_hit_cost = _sum_cost(item.cache_hit_cost for item in accounting_items)
    total_cost = _sum_cost(item.total_cost for item in accounting_items)
    return CostReport(
        currency=(
            accounting_items[0].currency
            if accounting_items
            else llm_settings.cost_currency
        ).strip().upper()
        or "CNY",
        days=days,
        generated_at=generated_at,
        run_count=run_count,
        completed_run_count=completed_run_count,
        failed_run_count=failed_run_count,
        ai_calls=ai_calls,
        usage_missing_count=usage_missing_count,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_hit_tokens=cache_hit_tokens,
        billable_input_tokens=billable_input_tokens,
        total_tokens=total_tokens,
        input_cost=input_cost,
        output_cost=output_cost,
        cache_hit_cost=cache_hit_cost,
        total_cost=total_cost,
        average_run_cost=_safe_div(total_cost, run_count),
        average_call_cost=_safe_div(total_cost, ai_calls),
        by_day=_bucket(accounting_items, key_fn=lambda item: _day_key(_item_created_at(item))),
        by_model=_bucket(accounting_items, key_fn=lambda item: f"{item.provider}/{item.model}"),
        by_status=_bucket(accounting_items, key_fn=lambda item: item.status),
        task_items=task_items[:recent_run_limit],
        recent_runs=items[:recent_run_limit],
    )


def build_usage_event_report(
    rows: Sequence[Mapping[str, Any]],
    *,
    llm_settings: LLMSettings,
    days: int,
    recent_event_limit: int,
    generated_at: datetime,
) -> CostUsageEventReport:
    """把 LLM 调用流水聚合为全量成本审计报表。"""
    items = tuple(_row_to_event_item(row, llm_settings=llm_settings) for row in rows)
    event_count = len(items)
    ai_calls = sum(item.ai_calls for item in items)
    input_tokens = sum(item.input_tokens for item in items)
    output_tokens = sum(item.output_tokens for item in items)
    cache_hit_tokens = sum(item.cache_hit_tokens for item in items)
    billable_input_tokens = sum(item.billable_input_tokens for item in items)
    total_tokens = sum(item.total_tokens for item in items)
    input_cost = _sum_cost(item.input_cost for item in items)
    output_cost = _sum_cost(item.output_cost for item in items)
    cache_hit_cost = _sum_cost(item.cache_hit_cost for item in items)
    total_cost = _sum_cost(item.total_cost for item in items)
    return CostUsageEventReport(
        currency=(items[0].currency if items else llm_settings.cost_currency).strip().upper()
        or "CNY",
        days=days,
        generated_at=generated_at,
        event_count=event_count,
        task_event_count=sum(1 for item in items if item.scope_type == "task_run"),
        debug_event_count=sum(1 for item in items if item.scope_type == "debug"),
        unbound_event_count=sum(1 for item in items if item.scope_type == "unbound"),
        ai_calls=ai_calls,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_hit_tokens=cache_hit_tokens,
        billable_input_tokens=billable_input_tokens,
        total_tokens=total_tokens,
        input_cost=input_cost,
        output_cost=output_cost,
        cache_hit_cost=cache_hit_cost,
        total_cost=total_cost,
        usage_missing_count=sum(1 for item in items if not item.usage_available),
        by_scope=_bucket(items, key_fn=lambda item: item.scope_type),
        by_source=_bucket(items, key_fn=lambda item: item.source),
        by_model=_bucket(items, key_fn=lambda item: f"{item.provider}/{item.model}"),
        recent_events=items[:recent_event_limit],
    )


def _row_to_item(row: Mapping[str, Any], *, llm_settings: LLMSettings) -> CostRunItem:
    provider = _string(row.get("provider")) or llm_settings.provider
    model = _string(row.get("model")) or llm_settings.model
    currency = (_string(row.get("cost_currency")) or llm_settings.cost_currency).strip().upper()
    input_tokens = _int(row.get("input_tokens"))
    output_tokens = _int(row.get("output_tokens"))
    cache_hit_tokens = _int(row.get("cache_hit_tokens"))
    billable_input_tokens = _int(row.get("billable_input_tokens"))
    total_tokens = _int(row.get("total_tokens"))
    persisted_pricing_breakdown = _list_dict_values(row.get("pricing_breakdown"))
    persisted_pricing_tiers = _list_text_values(row.get("applied_pricing_tiers"))
    persisted_total_cost = _float_or_none(row.get("total_cost"))
    persisted_pricing_configured = bool(row.get("pricing_configured"))
    should_estimate = persisted_total_cost is None or (
        not persisted_pricing_configured
        and (input_tokens > 0 or output_tokens > 0 or cache_hit_tokens > 0)
    )
    if should_estimate:
        estimate = estimate_llm_run_cost(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_hit_tokens=cache_hit_tokens,
            provider=provider,
            model=model,
            currency=currency,
            input_price_per_million_tokens=llm_settings.input_price_per_million_tokens,
            output_price_per_million_tokens=llm_settings.output_price_per_million_tokens,
            cache_hit_price_per_million_tokens=llm_settings.cache_hit_price_per_million_tokens,
            long_context_threshold_tokens=llm_settings.long_context_threshold_tokens,
            long_context_input_price_per_million_tokens=(
                llm_settings.long_context_input_price_per_million_tokens
            ),
            long_context_output_price_per_million_tokens=(
                llm_settings.long_context_output_price_per_million_tokens
            ),
            long_context_cache_hit_price_per_million_tokens=(
                llm_settings.long_context_cache_hit_price_per_million_tokens
            ),
        )
        input_cost = estimate.input_cost if estimate else 0.0
        output_cost = estimate.output_cost if estimate else 0.0
        cache_hit_cost = estimate.cache_hit_cost if estimate else 0.0
        total_cost = estimate.total_cost if estimate else 0.0
        pricing_source = estimate.pricing_source if estimate else "runtime_config_aggregate"
        pricing_configured = bool(estimate and estimate.pricing_configured)
        applied_pricing_tiers = estimate.applied_pricing_tiers if estimate else ()
        pricing_breakdown = estimate.pricing_breakdown if estimate else ()
        billable_input_tokens = (
            estimate.billable_input_tokens
            if estimate
            else max(0, input_tokens - cache_hit_tokens)
        )
    else:
        input_cost = _float(row.get("input_cost"))
        output_cost = _float(row.get("output_cost"))
        cache_hit_cost = _float(row.get("cache_hit_cost"))
        total_cost = persisted_total_cost
        pricing_source = _string(row.get("pricing_source")) or "runtime_config_per_call"
        pricing_configured = persisted_pricing_configured
        applied_pricing_tiers = persisted_pricing_tiers
        pricing_breakdown = persisted_pricing_breakdown
        if billable_input_tokens == 0 and input_tokens > 0:
            billable_input_tokens = max(0, input_tokens - cache_hit_tokens)
    return CostRunItem(
        run_id=str(row["run_id"]),
        task_id=str(row["task_id"]) if row.get("task_id") else None,
        task_name=_string(row.get("task_name")),
        task_status=_string(row.get("task_status")),
        agent_type=_string(row.get("agent_type")) or "",
        agent_name=_string(row.get("agent_name")) or "",
        status=str(row.get("status") or "unknown"),
        provider=provider,
        model=model,
        currency=currency or "CNY",
        ai_calls=_int(row.get("ai_calls")),
        usage_missing_count=_int(row.get("run_usage_missing_count")),
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_hit_tokens=cache_hit_tokens,
        billable_input_tokens=billable_input_tokens,
        total_tokens=total_tokens,
        input_cost=input_cost,
        output_cost=output_cost,
        cache_hit_cost=cache_hit_cost,
        total_cost=total_cost,
        pricing_source=pricing_source,
        pricing_configured=pricing_configured,
        applied_pricing_tiers=applied_pricing_tiers,
        pricing_breakdown=pricing_breakdown,
        started_at=row.get("started_at"),
        finished_at=row.get("finished_at"),
        created_at=row["created_at"],
    )


def _row_to_event_item(
    row: Mapping[str, Any],
    *,
    llm_settings: LLMSettings,
) -> CostUsageEventItem:
    currency = (_string(row.get("cost_currency")) or llm_settings.cost_currency).strip().upper()
    calculation = _mapping_value(row.get("calculation"))
    return CostUsageEventItem(
        event_id=str(row["event_id"]),
        scope_type=_string(row.get("scope_type")) or "unbound",
        task_id=str(row["task_id"]) if row.get("task_id") else None,
        task_name=_string(row.get("task_name")),
        agent_run_id=str(row["agent_run_id"]) if row.get("agent_run_id") else None,
        agent_type=_string(row.get("agent_type")),
        agent_name=_string(row.get("agent_name")),
        run_status=_string(row.get("run_status")),
        source=_string(row.get("source")) or "unknown",
        operation=_string(row.get("operation")),
        provider=_string(row.get("provider")) or llm_settings.provider,
        model=_string(row.get("model")) or llm_settings.model,
        api_style=_string(row.get("api_style")) or llm_settings.api_style,
        usage_available=bool(row.get("usage_available")),
        ai_calls=_int(row.get("ai_calls")),
        input_tokens=_int(row.get("input_tokens")),
        output_tokens=_int(row.get("output_tokens")),
        cache_hit_tokens=_int(row.get("cache_hit_tokens")),
        billable_input_tokens=_int(row.get("billable_input_tokens")),
        total_tokens=_int(row.get("total_tokens")),
        input_cost=_float(row.get("input_cost")),
        output_cost=_float(row.get("output_cost")),
        cache_hit_cost=_float(row.get("cache_hit_cost")),
        total_cost=_float(row.get("total_cost")),
        currency=currency or "CNY",
        pricing_source=_string(row.get("pricing_source")) or "runtime_config_per_call",
        pricing_unit=_string(row.get("pricing_unit")) or "1m_tokens",
        pricing_configured=bool(row.get("pricing_configured")),
        pricing_tier=_string(row.get("pricing_tier")),
        input_price_per_million_tokens=_float(
            row.get("input_price_per_million_tokens")
        ),
        output_price_per_million_tokens=_float(
            row.get("output_price_per_million_tokens")
        ),
        cache_hit_price_per_million_tokens=_float(
            row.get("cache_hit_price_per_million_tokens")
        ),
        calculation_formula=_string(calculation.get("formula")) or "",
        created_at=row["created_at"],
    )


def _bucket(items: Sequence[Any], *, key_fn: Any) -> tuple[CostBucket, ...]:
    grouped: dict[str, list[Any]] = defaultdict(list)
    for item in items:
        grouped[key_fn(item)].append(item)
    return tuple(
        CostBucket(
            key=key,
            run_count=sum(_bucket_item_count(item) for item in values),
            ai_calls=sum(item.ai_calls for item in values),
            usage_missing_count=sum(_item_usage_missing_count(item) for item in values),
            total_tokens=sum(item.total_tokens for item in values),
            total_cost=_sum_cost(item.total_cost for item in values),
        )
        for key, values in sorted(grouped.items())
    )


def _bucket_item_count(item: Any) -> int:
    count = getattr(item, "run_count", None)
    return max(1, _int(count)) if count is not None else 1


def _item_usage_missing_count(item: Any) -> int:
    count = getattr(item, "usage_missing_count", None)
    if count is not None:
        return _int(count)
    return 0 if getattr(item, "usage_available", True) else 1


def _item_created_at(item: Any) -> datetime:
    created_at = getattr(item, "created_at", None)
    if isinstance(created_at, datetime):
        return created_at
    latest_run_created_at = getattr(item, "latest_run_created_at", None)
    if isinstance(latest_run_created_at, datetime):
        return latest_run_created_at
    raise TypeError("成本报表项目缺少创建时间")


def _aggregate_task_items(
    pairs: Sequence[tuple[CostRunItem, Mapping[str, Any]]],
) -> tuple[CostTaskItem, ...]:
    grouped: dict[str, list[tuple[CostRunItem, Mapping[str, Any]]]] = defaultdict(list)
    for item, row in pairs:
        key = item.task_id or f"run:{item.run_id}"
        grouped[key].append((item, row))
    return tuple(
        _task_item_from_group(values)
        for values in sorted(
            grouped.values(),
            key=lambda values: max(item.created_at for item, _row in values),
            reverse=True,
        )
    )


def _task_item_from_group(
    pairs: Sequence[tuple[CostRunItem, Mapping[str, Any]]],
) -> CostTaskItem:
    values = tuple(item for item, _row in pairs)
    rows = tuple(row for _item, row in pairs)
    latest = max(values, key=lambda item: item.created_at)
    ledger_row = _task_usage_ledger_row(rows)
    if ledger_row is not None:
        currency = (
            _aggregate_label(_list_text_values(ledger_row.get("task_usage_cost_currencies")))
            or latest.currency
        )
        return CostTaskItem(
            task_id=latest.task_id,
            task_name=latest.task_name,
            status=_task_status(values),
            currency=currency,
            run_count=len(values),
            latest_run_id=latest.run_id,
            latest_run_status=latest.status,
            latest_run_created_at=latest.created_at,
            provider=(
                _aggregate_label(_list_text_values(ledger_row.get("task_usage_providers")))
                or _aggregate_label(item.provider for item in values)
            ),
            model=(
                _aggregate_label(_list_text_values(ledger_row.get("task_usage_models")))
                or _aggregate_label(item.model for item in values)
            ),
            ai_calls=_int(ledger_row.get("task_usage_ai_calls")),
            usage_missing_count=_int(ledger_row.get("task_usage_missing_count")),
            input_tokens=_int(ledger_row.get("task_usage_input_tokens")),
            output_tokens=_int(ledger_row.get("task_usage_output_tokens")),
            cache_hit_tokens=_int(ledger_row.get("task_usage_cache_hit_tokens")),
            billable_input_tokens=_int(
                ledger_row.get("task_usage_billable_input_tokens")
            ),
            total_tokens=_int(ledger_row.get("task_usage_total_tokens")),
            input_cost=_float(ledger_row.get("task_usage_input_cost")),
            output_cost=_float(ledger_row.get("task_usage_output_cost")),
            cache_hit_cost=_float(ledger_row.get("task_usage_cache_hit_cost")),
            total_cost=_float(ledger_row.get("task_usage_total_cost")),
            pricing_source=(
                _aggregate_label(
                    _list_text_values(ledger_row.get("task_usage_pricing_sources"))
                )
                or "runtime_config_per_call"
            ),
            applied_pricing_tiers=_list_text_values(
                ledger_row.get("task_usage_applied_pricing_tiers")
            ),
            pricing_breakdown=_merge_pricing_breakdowns(
                (ledger_row.get("task_usage_pricing_breakdown"),)
            ),
        )
    return CostTaskItem(
        task_id=latest.task_id,
        task_name=latest.task_name,
        status=_task_status(values),
        currency=latest.currency,
        run_count=len(values),
        latest_run_id=latest.run_id,
        latest_run_status=latest.status,
        latest_run_created_at=latest.created_at,
        provider=_aggregate_label(item.provider for item in values),
        model=_aggregate_label(item.model for item in values),
        ai_calls=sum(item.ai_calls for item in values),
        usage_missing_count=sum(item.usage_missing_count for item in values),
        input_tokens=sum(item.input_tokens for item in values),
        output_tokens=sum(item.output_tokens for item in values),
        cache_hit_tokens=sum(item.cache_hit_tokens for item in values),
        billable_input_tokens=sum(item.billable_input_tokens for item in values),
        total_tokens=sum(item.total_tokens for item in values),
        input_cost=_sum_cost(item.input_cost for item in values),
        output_cost=_sum_cost(item.output_cost for item in values),
        cache_hit_cost=_sum_cost(item.cache_hit_cost for item in values),
        total_cost=_sum_cost(item.total_cost for item in values),
        pricing_source=_aggregate_label(item.pricing_source for item in values),
        applied_pricing_tiers=tuple(
            sorted(
                {
                    tier
                    for item in values
                    for tier in item.applied_pricing_tiers
                    if str(tier).strip()
                }
            )
        ),
        pricing_breakdown=_merge_pricing_breakdowns(
            item.pricing_breakdown for item in values
        ),
    )


def _task_usage_ledger_row(
    rows: Sequence[Mapping[str, Any]],
) -> Mapping[str, Any] | None:
    for row in rows:
        if _int(row.get("task_usage_event_count")) > 0:
            return row
    return None


def _task_status(values: Sequence[CostRunItem]) -> str:
    task_statuses = [status for item in values if (status := item.task_status)]
    if task_statuses:
        return max(values, key=lambda item: item.created_at).task_status or task_statuses[0]
    if any(item.status in {"running", "queued"} for item in values):
        return "running"
    if any(item.status in {"failed", "error"} for item in values):
        return "failed"
    if any(item.status in {"canceled", "cancelled"} for item in values):
        return "canceled"
    if all(item.status in {"completed", "done"} for item in values):
        return "done"
    return max(values, key=lambda item: item.created_at).status


def _aggregate_label(values: Any) -> str:
    unique = sorted({str(value) for value in values if str(value).strip()})
    if not unique:
        return ""
    return unique[0] if len(unique) == 1 else "mixed"


def _day_key(value: datetime) -> str:
    day: date = value.date()
    return day.isoformat()


def _sum_cost(values: Any) -> float:
    return round(sum(float(value or 0.0) for value in values), 8)


def _safe_div(value: float, count: int) -> float:
    if count <= 0:
        return 0.0
    return round(value / count, 8)


def _int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return max(0, value)
    return 0


def _float(value: Any) -> float:
    parsed = _float_or_none(value)
    return parsed or 0.0


def _float_or_none(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return max(0.0, float(value))
    return None


def _string(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _list_text_values(value: Any) -> tuple[str, ...]:
    if isinstance(value, str) and value.strip().startswith("["):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return ()
    if isinstance(value, str):
        return (value,)
    if isinstance(value, list | tuple | set):
        return tuple(str(item).strip() for item in value if str(item).strip())
    return ()


def _list_dict_values(value: Any) -> tuple[dict[str, Any], ...]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return ()
    if not isinstance(value, list | tuple):
        return ()
    return tuple(dict(item) for item in value if isinstance(item, Mapping))


def _mapping_value(value: Any) -> Mapping[str, Any]:
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, Mapping) else {}
    return value if isinstance(value, Mapping) else {}


def _merge_pricing_breakdowns(values: Any) -> tuple[dict[str, Any], ...]:
    grouped: dict[str, dict[str, Any]] = {}
    for breakdown in values:
        for item in _list_dict_values(breakdown):
            key = "|".join(
                _string(item.get(field)) or ""
                for field in (
                    "pricing_tier",
                    "pricing_source",
                    "input_price_per_million_tokens",
                    "output_price_per_million_tokens",
                    "cache_hit_price_per_million_tokens",
                )
            )
            current = grouped.get(key)
            if current is None:
                current = dict(item)
                grouped[key] = current
                continue
            for field in (
                "ai_calls",
                "context_tokens",
                "input_tokens",
                "billable_input_tokens",
                "output_tokens",
                "cache_hit_tokens",
            ):
                current[field] = _int(current.get(field)) + _int(item.get(field))
            for field in (
                "input_cost",
                "output_cost",
                "cache_hit_cost",
                "total_cost",
            ):
                current[field] = round(
                    _float(current.get(field)) + _float(item.get(field)),
                    8,
                )
    return tuple(grouped[key] for key in sorted(grouped))
