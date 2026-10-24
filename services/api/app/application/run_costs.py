"""Agent Run 成本估算。"""

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal


@dataclass(frozen=True)
class LLMPricingTier:
    """LLM 单次调用的上下文价格档。"""

    min_context_tokens: int
    max_context_tokens: int | None
    input_price_per_million_tokens: float
    output_price_per_million_tokens: float
    cache_hit_price_per_million_tokens: float

    @property
    def label(self) -> str:
        if self.max_context_tokens is None:
            return f">={self.min_context_tokens}"
        return f"{self.min_context_tokens}-{self.max_context_tokens}"


@dataclass(frozen=True)
class LLMCostEstimate:
    """单次 Agent Run 的 LLM 成本估算结果。"""

    provider: str
    model: str
    currency: str
    input_tokens: int
    output_tokens: int
    cache_hit_tokens: int
    billable_input_tokens: int
    input_cost: float
    output_cost: float
    cache_hit_cost: float
    total_cost: float
    pricing_unit: str
    pricing_source: str
    pricing_configured: bool
    applied_pricing_tiers: tuple[str, ...]
    pricing_tier: str
    context_tokens: int
    input_price_per_million_tokens: float
    output_price_per_million_tokens: float
    cache_hit_price_per_million_tokens: float
    calculation_formula: str
    pricing_breakdown: tuple[dict[str, int | float | str | bool], ...]


def estimate_llm_run_cost(
    *,
    input_tokens: int | None,
    output_tokens: int | None,
    provider: str,
    model: str,
    currency: str,
    input_price_per_million_tokens: float,
    output_price_per_million_tokens: float,
    cache_hit_tokens: int | None = None,
    cache_hit_price_per_million_tokens: float = 0.0,
    long_context_threshold_tokens: int | None = None,
    long_context_input_price_per_million_tokens: float = 0.0,
    long_context_output_price_per_million_tokens: float = 0.0,
    long_context_cache_hit_price_per_million_tokens: float = 0.0,
) -> LLMCostEstimate | None:
    """按当前配置估算 Run 级 LLM 成本；无 token 数据时不返回估算。

    历史 Run 只有聚合 token，无法知道每次调用的真实上下文档位，因此这里按聚合
    输入 token 做兜底估算。新 Run 应在运行时逐调用计费后直接使用持久化成本。
    """
    return estimate_llm_call_cost(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_hit_tokens=cache_hit_tokens,
        provider=provider,
        model=model,
        currency=currency,
        input_price_per_million_tokens=input_price_per_million_tokens,
        output_price_per_million_tokens=output_price_per_million_tokens,
        cache_hit_price_per_million_tokens=cache_hit_price_per_million_tokens,
        long_context_threshold_tokens=long_context_threshold_tokens,
        long_context_input_price_per_million_tokens=long_context_input_price_per_million_tokens,
        long_context_output_price_per_million_tokens=long_context_output_price_per_million_tokens,
        long_context_cache_hit_price_per_million_tokens=(
            long_context_cache_hit_price_per_million_tokens
        ),
        pricing_source="runtime_config_aggregate",
    )


def estimate_llm_call_cost(
    *,
    input_tokens: int | None,
    output_tokens: int | None,
    provider: str,
    model: str,
    currency: str,
    input_price_per_million_tokens: float,
    output_price_per_million_tokens: float,
    cache_hit_tokens: int | None = None,
    cache_hit_price_per_million_tokens: float = 0.0,
    long_context_threshold_tokens: int | None = None,
    long_context_input_price_per_million_tokens: float = 0.0,
    long_context_output_price_per_million_tokens: float = 0.0,
    long_context_cache_hit_price_per_million_tokens: float = 0.0,
    pricing_source: str = "runtime_config",
) -> LLMCostEstimate | None:
    """按单次 LLM 调用的输入上下文选择价格档并估算成本。"""
    if input_tokens is None and output_tokens is None and cache_hit_tokens is None:
        return None

    safe_input_tokens = _nonnegative_int(input_tokens)
    safe_output_tokens = _nonnegative_int(output_tokens)
    safe_cache_hit_tokens = _nonnegative_int(cache_hit_tokens)
    billable_input_tokens = max(0, safe_input_tokens - safe_cache_hit_tokens)
    tier = _select_pricing_tier(
        context_tokens=safe_input_tokens,
        tiers=_build_pricing_tiers(
            input_price_per_million_tokens=input_price_per_million_tokens,
            output_price_per_million_tokens=output_price_per_million_tokens,
            cache_hit_price_per_million_tokens=cache_hit_price_per_million_tokens,
            long_context_threshold_tokens=long_context_threshold_tokens,
            long_context_input_price_per_million_tokens=(
                long_context_input_price_per_million_tokens
            ),
            long_context_output_price_per_million_tokens=(
                long_context_output_price_per_million_tokens
            ),
            long_context_cache_hit_price_per_million_tokens=(
                long_context_cache_hit_price_per_million_tokens
            ),
        ),
    )
    input_cost = _token_cost(
        billable_input_tokens,
        _nonnegative_decimal(tier.input_price_per_million_tokens),
    )
    output_cost = _token_cost(
        safe_output_tokens,
        _nonnegative_decimal(tier.output_price_per_million_tokens),
    )
    cache_hit_cost = _token_cost(
        safe_cache_hit_tokens,
        _nonnegative_decimal(tier.cache_hit_price_per_million_tokens),
    )
    total_cost = input_cost + output_cost + cache_hit_cost
    pricing_configured = any(
        _nonnegative_decimal(price) > 0
        for price in (
            tier.input_price_per_million_tokens,
            tier.output_price_per_million_tokens,
            tier.cache_hit_price_per_million_tokens,
        )
    )

    return LLMCostEstimate(
        provider=provider,
        model=model,
        currency=currency.strip().upper() or "CNY",
        input_tokens=safe_input_tokens,
        output_tokens=safe_output_tokens,
        cache_hit_tokens=safe_cache_hit_tokens,
        billable_input_tokens=billable_input_tokens,
        input_cost=_decimal_to_float(input_cost),
        output_cost=_decimal_to_float(output_cost),
        cache_hit_cost=_decimal_to_float(cache_hit_cost),
        total_cost=_decimal_to_float(total_cost),
        pricing_unit="1m_tokens",
        pricing_source=pricing_source,
        pricing_configured=pricing_configured,
        applied_pricing_tiers=(tier.label,),
        pricing_tier=tier.label,
        context_tokens=safe_input_tokens,
        input_price_per_million_tokens=_decimal_to_float(
            _nonnegative_decimal(tier.input_price_per_million_tokens)
        ),
        output_price_per_million_tokens=_decimal_to_float(
            _nonnegative_decimal(tier.output_price_per_million_tokens)
        ),
        cache_hit_price_per_million_tokens=_decimal_to_float(
            _nonnegative_decimal(tier.cache_hit_price_per_million_tokens)
        ),
        calculation_formula=(
            "(billable_input_tokens * input_price_per_million_tokens "
            "+ output_tokens * output_price_per_million_tokens "
            "+ cache_hit_tokens * cache_hit_price_per_million_tokens) / 1000000"
        ),
        pricing_breakdown=(
            {
                "pricing_tier": tier.label,
                "context_tokens": safe_input_tokens,
                "input_tokens": safe_input_tokens,
                "billable_input_tokens": billable_input_tokens,
                "output_tokens": safe_output_tokens,
                "cache_hit_tokens": safe_cache_hit_tokens,
                "input_price_per_million_tokens": _decimal_to_float(
                    _nonnegative_decimal(tier.input_price_per_million_tokens)
                ),
                "output_price_per_million_tokens": _decimal_to_float(
                    _nonnegative_decimal(tier.output_price_per_million_tokens)
                ),
                "cache_hit_price_per_million_tokens": _decimal_to_float(
                    _nonnegative_decimal(tier.cache_hit_price_per_million_tokens)
                ),
                "input_cost": _decimal_to_float(input_cost),
                "output_cost": _decimal_to_float(output_cost),
                "cache_hit_cost": _decimal_to_float(cache_hit_cost),
                "total_cost": _decimal_to_float(total_cost),
                "pricing_unit": "1m_tokens",
                "pricing_source": pricing_source,
                "pricing_configured": pricing_configured,
                "calculation_formula": (
                    "(billable_input_tokens * input_price_per_million_tokens "
                    "+ output_tokens * output_price_per_million_tokens "
                    "+ cache_hit_tokens * cache_hit_price_per_million_tokens) "
                    "/ 1000000"
                ),
            },
        ),
    )


def _nonnegative_int(value: int | None) -> int:
    if value is None or value < 0:
        return 0
    return value


def _nonnegative_decimal(value: float) -> Decimal:
    decimal_value = Decimal(str(value))
    if decimal_value < 0:
        return Decimal("0")
    return decimal_value


def _build_pricing_tiers(
    *,
    input_price_per_million_tokens: float,
    output_price_per_million_tokens: float,
    cache_hit_price_per_million_tokens: float,
    long_context_threshold_tokens: int | None,
    long_context_input_price_per_million_tokens: float,
    long_context_output_price_per_million_tokens: float,
    long_context_cache_hit_price_per_million_tokens: float,
) -> tuple[LLMPricingTier, ...]:
    base_tier = LLMPricingTier(
        min_context_tokens=0,
        max_context_tokens=long_context_threshold_tokens,
        input_price_per_million_tokens=input_price_per_million_tokens,
        output_price_per_million_tokens=output_price_per_million_tokens,
        cache_hit_price_per_million_tokens=cache_hit_price_per_million_tokens,
    )
    if long_context_threshold_tokens is None or long_context_threshold_tokens <= 0:
        return (base_tier,)
    return (
        base_tier,
        LLMPricingTier(
            min_context_tokens=long_context_threshold_tokens,
            max_context_tokens=None,
            input_price_per_million_tokens=long_context_input_price_per_million_tokens,
            output_price_per_million_tokens=long_context_output_price_per_million_tokens,
            cache_hit_price_per_million_tokens=long_context_cache_hit_price_per_million_tokens,
        ),
    )


def _select_pricing_tier(
    *,
    context_tokens: int,
    tiers: tuple[LLMPricingTier, ...],
) -> LLMPricingTier:
    for tier in tiers:
        if context_tokens < tier.min_context_tokens:
            continue
        if tier.max_context_tokens is None or context_tokens < tier.max_context_tokens:
            return tier
    return tiers[-1]


def _token_cost(tokens: int, price_per_million_tokens: Decimal) -> Decimal:
    return (Decimal(tokens) * price_per_million_tokens / Decimal(1_000_000)).quantize(
        Decimal("0.00000001"),
        rounding=ROUND_HALF_UP,
    )


def _decimal_to_float(value: Decimal) -> float:
    return float(value)
