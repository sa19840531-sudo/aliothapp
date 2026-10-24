"""同一 Agent Run 内跨 Main Agent 与业务 Tool 的 LLM 用量汇总。"""

import asyncio
import logging
import os
from collections.abc import Iterator, Mapping
from contextlib import contextmanager, suppress
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

from services.api.app.agents.runtime.models import UsageSnapshot
from services.api.app.application.run_costs import LLMCostEstimate, estimate_llm_call_cost
from services.api.app.core.config import get_settings

logger = logging.getLogger(__name__)


@dataclass
class RunLLMUsageMeter:
    """累计一次正式运行中所有 LLM 客户端请求的实际用量。"""

    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_hit_tokens: int | None = None
    billable_input_tokens: int | None = None
    total_tokens: int | None = None
    ai_calls: int = 0
    input_cost: float | None = None
    output_cost: float | None = None
    cache_hit_cost: float | None = None
    total_cost: float | None = None
    cost_currency: str | None = None
    pricing_source: str | None = None
    pricing_configured: bool | None = None
    applied_pricing_tiers: set[str] = field(default_factory=set)
    provider: str | None = None
    model: str | None = None
    pricing_breakdown: dict[str, dict[str, Any]] = field(default_factory=dict)
    audit_tasks: set[asyncio.Task[None]] = field(default_factory=set, repr=False)

    def record(
        self,
        usage_details: Mapping[str, Any],
    ) -> tuple[dict[str, int | None], LLMCostEstimate | None]:
        """记录一次已完成的非流式模型请求。"""
        self.ai_calls += 1
        normalized = normalize_llm_usage_tokens(usage_details)
        input_tokens = normalized["input_tokens"]
        output_tokens = normalized["output_tokens"]
        total_tokens = normalized["total_tokens"]
        cache_hit_tokens = normalized["cache_hit_tokens"]
        self.input_tokens = _add_optional(
            self.input_tokens,
            input_tokens,
        )
        self.output_tokens = _add_optional(
            self.output_tokens,
            output_tokens,
        )
        self.cache_hit_tokens = _add_optional(
            self.cache_hit_tokens,
            cache_hit_tokens,
        )
        estimate = _estimate_usage_cost(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_hit_tokens=cache_hit_tokens,
        )
        if estimate is not None:
            self.billable_input_tokens = _add_optional(
                self.billable_input_tokens,
                estimate.billable_input_tokens,
            )
        self.total_tokens = _add_optional(
            self.total_tokens,
            total_tokens,
        )
        self._record_cost(estimate)
        return normalized, estimate

    def track_audit_task(self, task: asyncio.Task[None]) -> None:
        """跟踪当前 Run 内异步写入的 LLM 用量流水。"""

        self.audit_tasks.add(task)

    async def flush_audit_events(self, *, timeout_seconds: float = 2.0) -> None:
        """Run 收尾前尽力等待当前 Run 的用量流水完成落库。"""

        await _await_usage_event_tasks(tuple(self.audit_tasks), timeout_seconds=timeout_seconds)

    def snapshot(self) -> UsageSnapshot | None:
        """在至少发生一次模型请求时生成可持久化快照。"""
        if self.ai_calls == 0:
            return None
        return UsageSnapshot(
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            cache_hit_tokens=self.cache_hit_tokens,
            billable_input_tokens=self.billable_input_tokens,
            total_tokens=self.total_tokens,
            ai_calls=self.ai_calls,
            input_cost=self.input_cost,
            output_cost=self.output_cost,
            cache_hit_cost=self.cache_hit_cost,
            total_cost=self.total_cost,
            cost_currency=self.cost_currency,
            pricing_source=self.pricing_source,
            pricing_configured=self.pricing_configured,
            applied_pricing_tiers=tuple(sorted(self.applied_pricing_tiers)),
            pricing_breakdown=tuple(
                self.pricing_breakdown[key]
                for key in sorted(self.pricing_breakdown)
            ),
            provider=self.provider,
            model=self.model,
        )

    def _record_cost(self, estimate: LLMCostEstimate | None) -> None:
        if estimate is None:
            return
        self.input_cost = _add_money(self.input_cost, estimate.input_cost)
        self.output_cost = _add_money(self.output_cost, estimate.output_cost)
        self.cache_hit_cost = _add_money(self.cache_hit_cost, estimate.cache_hit_cost)
        self.total_cost = _add_money(self.total_cost, estimate.total_cost)
        self.cost_currency = estimate.currency
        self.pricing_source = estimate.pricing_source
        self.pricing_configured = estimate.pricing_configured
        self.provider = estimate.provider
        self.model = estimate.model
        self.applied_pricing_tiers.update(estimate.applied_pricing_tiers)
        for item in estimate.pricing_breakdown:
            self._merge_pricing_breakdown(dict(item))

    def _merge_pricing_breakdown(self, item: dict[str, Any]) -> None:
        key = "|".join(
            str(item.get(field) or "")
            for field in (
                "pricing_tier",
                "pricing_source",
                "input_price_per_million_tokens",
                "output_price_per_million_tokens",
                "cache_hit_price_per_million_tokens",
            )
        )
        current = self.pricing_breakdown.get(key)
        if current is None:
            current = dict(item)
            current["ai_calls"] = 0
            for field_name in (
                "context_tokens",
                "input_tokens",
                "billable_input_tokens",
                "output_tokens",
                "cache_hit_tokens",
            ):
                current[field_name] = 0
            for field_name in (
                "input_cost",
                "output_cost",
                "cache_hit_cost",
                "total_cost",
            ):
                current[field_name] = 0.0
            self.pricing_breakdown[key] = current
        current["ai_calls"] = _nonnegative_int(current.get("ai_calls")) + 1
        for field_name in (
            "context_tokens",
            "input_tokens",
            "billable_input_tokens",
            "output_tokens",
            "cache_hit_tokens",
        ):
            current[field_name] = _nonnegative_int(current.get(field_name)) + _nonnegative_int(
                item.get(field_name)
            )
        for field_name in (
            "input_cost",
            "output_cost",
            "cache_hit_cost",
            "total_cost",
        ):
            current[field_name] = (
                _add_money(float(current.get(field_name) or 0), item.get(field_name))
                or 0
            )


@dataclass(frozen=True)
class LLMUsageScope:
    """非任务调用的 LLM 成本归属上下文。"""

    scope_type: str | None = None
    source: str | None = None
    operation: str | None = None


_current_meter: ContextVar[RunLLMUsageMeter | None] = ContextVar(
    "alioth_agent_run_llm_usage_meter",
    default=None,
)
_current_usage_scope: ContextVar[LLMUsageScope | None] = ContextVar(
    "alioth_llm_usage_scope",
    default=None,
)
_pending_usage_event_tasks: set[asyncio.Task[None]] = set()


@contextmanager
def bind_run_llm_usage_meter() -> Iterator[RunLLMUsageMeter]:
    """为当前异步上下文及其子任务绑定新的 Run 级计量器。"""
    meter = RunLLMUsageMeter()
    token = _current_meter.set(meter)
    try:
        yield meter
    finally:
        _current_meter.reset(token)


@contextmanager
def bind_llm_usage_scope(
    *,
    scope_type: str | None = None,
    source: str | None = None,
    operation: str | None = None,
) -> Iterator[None]:
    """为当前上下文绑定非任务 LLM 调用归属。"""
    token = _current_usage_scope.set(
        LLMUsageScope(scope_type=scope_type, source=source, operation=operation)
    )
    try:
        yield
    finally:
        _current_usage_scope.reset(token)


def get_current_llm_usage_meter() -> RunLLMUsageMeter | None:
    """返回当前异步上下文中已经绑定的 Run 级计量器。"""

    return _current_meter.get()


def record_current_llm_usage(
    usage_details: Mapping[str, Any],
    *,
    source: str | None = None,
    operation: str | None = None,
    scope_type: str | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> None:
    """由统一 LLM 客户端在每次响应后写入当前 Run 计量器。"""
    meter = _current_meter.get()
    if meter is not None:
        normalized, estimate = meter.record(usage_details)
    else:
        normalized = normalize_llm_usage_tokens(usage_details)
        estimate = _estimate_usage_cost(
            input_tokens=normalized["input_tokens"],
            output_tokens=normalized["output_tokens"],
            cache_hit_tokens=normalized["cache_hit_tokens"],
        )
    _schedule_llm_usage_event(
        usage_details=usage_details,
        normalized=normalized,
        estimate=estimate,
        source=source,
        operation=operation,
        scope_type=scope_type,
        metadata=metadata,
    )


def record_current_openai_compatible_response_usage(
    response: Any,
    *,
    source: str | None = None,
    operation: str | None = None,
    scope_type: str | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> None:
    """记录一次 OpenAI 兼容 SDK 响应；缺少 token 明细时仍累计调用次数。"""
    usage = _response_usage_mapping(response)
    record_current_llm_usage(
        usage or {},
        source=source,
        operation=operation,
        scope_type=scope_type,
        metadata=metadata,
    )


def record_current_llm_attempt_failure(
    error: BaseException,
    *,
    source: str | None = None,
    operation: str | None = None,
    scope_type: str | None = None,
    attempt_no: int | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> None:
    """记录一次已发出但供应商未返回 usage 的失败请求。"""

    failure_metadata = dict(metadata or {})
    failure_metadata.update(
        {
            "request_status": "failed",
            "usage_status": "unavailable",
            "error_type": type(error).__name__,
            "error_message": str(error)[:240],
        }
    )
    if attempt_no is not None:
        failure_metadata["attempt_no"] = attempt_no
    record_current_llm_usage(
        {},
        source=source,
        operation=operation,
        scope_type=scope_type,
        metadata=failure_metadata,
    )


def normalize_llm_usage_tokens(
    usage_details: Mapping[str, Any],
) -> dict[str, int | None]:
    """归一化不同 OpenAI 兼容端点的 token 字段名。"""

    sources = _usage_sources(usage_details)
    input_tokens = _first_int_from_sources(
        sources,
        (
            "input_token_count",
            "input_tokens",
            "prompt_tokens",
            "prompt_token_count",
        ),
    )
    output_tokens = _first_int_from_sources(
        sources,
        (
            "output_token_count",
            "output_tokens",
            "completion_tokens",
            "completion_token_count",
        ),
    )
    total_tokens = _first_int_from_sources(
        sources,
        (
            "total_token_count",
            "total_tokens",
        ),
    )
    cache_hit_tokens = _extract_cache_hit_tokens(usage_details)
    if total_tokens is None and input_tokens is not None and output_tokens is not None:
        total_tokens = input_tokens + output_tokens
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_hit_tokens": cache_hit_tokens,
        "total_tokens": total_tokens,
    }


def _add_optional(current: int | None, value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return current
    return (current or 0) + value


def _optional_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _add_money(current: float | None, value: float | None) -> float | None:
    if value is None:
        return current
    return round((current or 0.0) + value, 8)


def _estimate_usage_cost(
    *,
    input_tokens: int | None,
    output_tokens: int | None,
    cache_hit_tokens: int | None,
) -> LLMCostEstimate | None:
    settings = get_settings().llm
    return estimate_llm_call_cost(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_hit_tokens=cache_hit_tokens,
        provider=settings.provider,
        model=settings.model,
        currency=settings.cost_currency,
        input_price_per_million_tokens=settings.input_price_per_million_tokens,
        output_price_per_million_tokens=settings.output_price_per_million_tokens,
        cache_hit_price_per_million_tokens=settings.cache_hit_price_per_million_tokens,
        long_context_threshold_tokens=settings.long_context_threshold_tokens,
        long_context_input_price_per_million_tokens=(
            settings.long_context_input_price_per_million_tokens
        ),
        long_context_output_price_per_million_tokens=(
            settings.long_context_output_price_per_million_tokens
        ),
        long_context_cache_hit_price_per_million_tokens=(
            settings.long_context_cache_hit_price_per_million_tokens
        ),
        pricing_source="runtime_config_per_call",
    )


def _schedule_llm_usage_event(
    *,
    usage_details: Mapping[str, Any],
    normalized: Mapping[str, int | None],
    estimate: LLMCostEstimate | None,
    source: str | None,
    operation: str | None,
    scope_type: str | None,
    metadata: Mapping[str, Any] | None = None,
) -> None:
    if not _usage_audit_enabled():
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    payload = _build_usage_event_payload(
        usage_details=usage_details,
        normalized=normalized,
        estimate=estimate,
        source=source,
        operation=operation,
        scope_type=scope_type,
        metadata=metadata,
    )
    task = loop.create_task(_persist_llm_usage_event(payload))
    _pending_usage_event_tasks.add(task)
    meter = _current_meter.get()
    if meter is not None:
        meter.track_audit_task(task)
    task.add_done_callback(_discard_usage_event_task)
    task.add_done_callback(_log_usage_event_failure)


async def flush_pending_llm_usage_events(*, timeout_seconds: float = 2.0) -> None:
    """等待当前进程仍在写入的 LLM 用量流水，供 Run 终态聚合前调用。"""

    await _await_usage_event_tasks(
        tuple(_pending_usage_event_tasks),
        timeout_seconds=timeout_seconds,
    )


async def _await_usage_event_tasks(
    tasks: tuple[asyncio.Task[None], ...],
    *,
    timeout_seconds: float,
) -> None:
    pending_tasks = tuple(task for task in tasks if not task.done())
    if not pending_tasks:
        return
    done, pending = await asyncio.wait(pending_tasks, timeout=max(0.0, timeout_seconds))
    if pending:
        logger.warning(
            "LLM 用量调用流水仍有 %s/%s 条未完成，Run 聚合将以已落库明细为准。",
            len(pending),
            len(pending_tasks),
        )
    for task in done:
        with suppress(Exception):
            task.result()


async def _persist_llm_usage_event(payload: Mapping[str, Any]) -> None:
    from services.api.app.infrastructure.db.llm_usage_event_repository import (
        PostgresLLMUsageEventRepository,
    )

    await PostgresLLMUsageEventRepository().record(payload)


def _discard_usage_event_task(task: asyncio.Task[None]) -> None:
    _pending_usage_event_tasks.discard(task)


def _log_usage_event_failure(task: asyncio.Task[None]) -> None:
    try:
        task.result()
    except Exception:
        logger.warning("LLM 用量调用流水写入失败", exc_info=True)


def _usage_audit_enabled() -> bool:
    raw_value = os.getenv("ALIOTH_LLM_USAGE_AUDIT_ENABLED")
    if raw_value is not None:
        return raw_value.strip().lower() in {"1", "true", "yes", "on"}
    return get_settings().environment.strip().casefold() != "test"


def _build_usage_event_payload(
    *,
    usage_details: Mapping[str, Any],
    normalized: Mapping[str, int | None],
    estimate: LLMCostEstimate | None,
    source: str | None,
    operation: str | None,
    scope_type: str | None,
    metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    from services.api.app.agents.runtime.run_context import get_agent_run_context

    settings = get_settings().llm
    context = get_agent_run_context()
    usage_scope = _current_usage_scope.get()
    resolved_source = _clean_label(source) or _clean_label(
        usage_scope.source if usage_scope else None
    ) or "unknown"
    resolved_operation = _clean_label(operation) or _clean_label(
        usage_scope.operation if usage_scope else None
    )
    resolved_scope_type = _resolve_scope_type(
        explicit_scope_type=scope_type or (usage_scope.scope_type if usage_scope else None),
        source=resolved_source,
        task_id=context.task_id if context is not None else None,
        agent_run_id=context.agent_run_id if context is not None else None,
    )
    pricing_breakdown = [dict(item) for item in estimate.pricing_breakdown] if estimate else []
    input_tokens = normalized.get("input_tokens")
    output_tokens = normalized.get("output_tokens")
    cache_hit_tokens = normalized.get("cache_hit_tokens")
    total_tokens = normalized.get("total_tokens")
    return {
        "scope_type": resolved_scope_type,
        "task_id": context.task_id if context is not None else None,
        "agent_run_id": context.agent_run_id if context is not None else None,
        "source": resolved_source,
        "operation": resolved_operation,
        "provider": estimate.provider if estimate else settings.provider,
        "model": estimate.model if estimate else settings.model,
        "api_style": settings.api_style,
        "usage_available": any(
            value is not None
            for value in (input_tokens, output_tokens, cache_hit_tokens, total_tokens)
        ),
        "ai_calls": 1,
        "input_tokens": _nonnegative_int(input_tokens),
        "output_tokens": _nonnegative_int(output_tokens),
        "cache_hit_tokens": _nonnegative_int(cache_hit_tokens),
        "billable_input_tokens": (
            estimate.billable_input_tokens
            if estimate is not None
            else max(0, _nonnegative_int(input_tokens) - _nonnegative_int(cache_hit_tokens))
        ),
        "total_tokens": _nonnegative_int(total_tokens),
        "input_cost": estimate.input_cost if estimate else 0.0,
        "output_cost": estimate.output_cost if estimate else 0.0,
        "cache_hit_cost": estimate.cache_hit_cost if estimate else 0.0,
        "total_cost": estimate.total_cost if estimate else 0.0,
        "cost_currency": (estimate.currency if estimate else settings.cost_currency)
        .strip()
        .upper()
        or "CNY",
        "pricing_source": estimate.pricing_source if estimate else "runtime_config_per_call",
        "pricing_unit": estimate.pricing_unit if estimate else "1m_tokens",
        "pricing_configured": (
            estimate.pricing_configured if estimate else _pricing_configured_from_settings()
        ),
        "pricing_tier": estimate.pricing_tier if estimate else None,
        "input_price_per_million_tokens": (
            estimate.input_price_per_million_tokens if estimate else 0.0
        ),
        "output_price_per_million_tokens": (
            estimate.output_price_per_million_tokens if estimate else 0.0
        ),
        "cache_hit_price_per_million_tokens": (
            estimate.cache_hit_price_per_million_tokens if estimate else 0.0
        ),
        "calculation": {
            "formula": estimate.calculation_formula if estimate else "",
            "pricing_breakdown": pricing_breakdown,
        },
        "vendor_usage_payload": _json_safe(usage_details),
        "metadata": _json_safe(dict(metadata or {})),
    }


def _resolve_scope_type(
    *,
    explicit_scope_type: str | None,
    source: str,
    task_id: str | None,
    agent_run_id: str | None,
) -> str:
    normalized = _clean_label(explicit_scope_type)
    if normalized in {"task_run", "agent_run", "debug", "unbound"}:
        return normalized
    if task_id:
        return "task_run"
    if source.startswith("devui") or "debug" in source:
        return "debug"
    if agent_run_id:
        return "agent_run"
    return "unbound"


def _pricing_configured_from_settings() -> bool:
    settings = get_settings().llm
    return any(
        price > 0
        for price in (
            settings.input_price_per_million_tokens,
            settings.output_price_per_million_tokens,
            settings.cache_hit_price_per_million_tokens,
            settings.long_context_input_price_per_million_tokens,
            settings.long_context_output_price_per_million_tokens,
            settings.long_context_cache_hit_price_per_million_tokens,
        )
    )


def _clean_label(value: str | None) -> str | None:
    if value is None:
        return None
    text = value.strip().lower()
    return text or None


def _nonnegative_int(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return max(0, value)


def _json_safe(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_json_safe(item) for item in value]
    if isinstance(value, str | int | float | bool) or value is None:
        return value
    return str(value)


def _response_usage_mapping(response: Any) -> Mapping[str, Any] | None:
    usage_details = getattr(response, "usage_details", None)
    if isinstance(usage_details, Mapping):
        return usage_details
    usage = getattr(response, "usage", None)
    if isinstance(usage, Mapping):
        return {"usage": dict(usage)}
    for method_name in ("model_dump", "dict"):
        method = getattr(usage, method_name, None)
        if callable(method):
            dumped = method()
            if isinstance(dumped, Mapping):
                return {"usage": dict(dumped)}
    return None


def _extract_cache_hit_tokens(usage_details: Mapping[str, Any]) -> int | None:
    sources = _usage_sources(usage_details)
    direct = _first_int_from_sources(
        sources,
        (
            "cache_hit_token_count",
            "cached_token_count",
            "cached_tokens",
            "input_cached_tokens",
            "cache_read_input_tokens",
            # Agent Framework 用 *_count 后缀，并把供应商原始键前缀化为
            # prompt/…；少这两个名字会让缓存命中恒记为 0，任何缓存优化都
            # 变成看不见的收益。
            "cache_read_input_token_count",
            "prompt/cached_tokens",
        ),
    )
    if direct is not None:
        return direct
    for source in sources:
        for nested_key in (
            "input_token_details",
            "input_tokens_details",
            "prompt_token_details",
            "prompt_tokens_details",
        ):
            nested = source.get(nested_key)
            if not isinstance(nested, Mapping):
                continue
            nested_value = _first_int(
                nested,
                (
                    "cached_tokens",
                    "cache_hit_tokens",
                    "cache_read_input_tokens",
                ),
            )
            if nested_value is not None:
                return nested_value
    return None


def _first_int(mapping: Mapping[str, Any], keys: tuple[str, ...]) -> int | None:
    for key in keys:
        value = _optional_int(mapping.get(key))
        if value is not None:
            return value
    return None


def _first_int_from_sources(
    sources: tuple[Mapping[str, Any], ...],
    keys: tuple[str, ...],
) -> int | None:
    for source in sources:
        value = _first_int(source, keys)
        if value is not None:
            return value
    return None


def _usage_sources(usage_details: Mapping[str, Any]) -> tuple[Mapping[str, Any], ...]:
    nested = usage_details.get("usage")
    if isinstance(nested, Mapping):
        return usage_details, nested
    return (usage_details,)
