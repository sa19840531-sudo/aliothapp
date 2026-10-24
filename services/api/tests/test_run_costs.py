from services.api.app.application.run_costs import estimate_llm_call_cost, estimate_llm_run_cost


def test_estimate_llm_call_cost_uses_rmb_short_context_tier() -> None:
    estimate = estimate_llm_call_cost(
        input_tokens=100_000,
        output_tokens=50_000,
        cache_hit_tokens=10_000,
        provider="custom",
        model="grok-4.5",
        currency="cny",
        input_price_per_million_tokens=1.0,
        output_price_per_million_tokens=3.0,
        cache_hit_price_per_million_tokens=0.25,
        long_context_threshold_tokens=200_000,
        long_context_input_price_per_million_tokens=2.0,
        long_context_output_price_per_million_tokens=6.0,
        long_context_cache_hit_price_per_million_tokens=0.5,
    )

    assert estimate is not None
    assert estimate.provider == "custom"
    assert estimate.model == "grok-4.5"
    assert estimate.currency == "CNY"
    assert estimate.input_cost == 0.09
    assert estimate.output_cost == 0.15
    assert estimate.cache_hit_cost == 0.0025
    assert estimate.total_cost == 0.2425
    assert estimate.billable_input_tokens == 90_000
    assert estimate.pricing_unit == "1m_tokens"
    assert estimate.pricing_source == "runtime_config"
    assert estimate.pricing_configured is True
    assert estimate.applied_pricing_tiers == ("0-200000",)
    assert estimate.pricing_tier == "0-200000"
    assert estimate.input_price_per_million_tokens == 1.0
    assert estimate.output_price_per_million_tokens == 3.0
    assert estimate.cache_hit_price_per_million_tokens == 0.25
    assert estimate.pricing_breakdown == (
        {
            "pricing_tier": "0-200000",
            "context_tokens": 100_000,
            "input_tokens": 100_000,
            "billable_input_tokens": 90_000,
            "output_tokens": 50_000,
            "cache_hit_tokens": 10_000,
            "input_price_per_million_tokens": 1.0,
            "output_price_per_million_tokens": 3.0,
            "cache_hit_price_per_million_tokens": 0.25,
            "input_cost": 0.09,
            "output_cost": 0.15,
            "cache_hit_cost": 0.0025,
            "total_cost": 0.2425,
            "pricing_unit": "1m_tokens",
            "pricing_source": "runtime_config",
            "pricing_configured": True,
            "calculation_formula": (
                "(billable_input_tokens * input_price_per_million_tokens "
                "+ output_tokens * output_price_per_million_tokens "
                "+ cache_hit_tokens * cache_hit_price_per_million_tokens) "
                "/ 1000000"
            ),
        },
    )


def test_estimate_llm_call_cost_uses_rmb_long_context_tier() -> None:
    estimate = estimate_llm_call_cost(
        input_tokens=200_000,
        output_tokens=10_000,
        cache_hit_tokens=10_000,
        provider="custom",
        model="grok-4.5",
        currency="CNY",
        input_price_per_million_tokens=1.0,
        output_price_per_million_tokens=3.0,
        cache_hit_price_per_million_tokens=0.25,
        long_context_threshold_tokens=200_000,
        long_context_input_price_per_million_tokens=2.0,
        long_context_output_price_per_million_tokens=6.0,
        long_context_cache_hit_price_per_million_tokens=0.5,
    )

    assert estimate is not None
    assert estimate.input_cost == 0.38
    assert estimate.output_cost == 0.06
    assert estimate.cache_hit_cost == 0.005
    assert estimate.total_cost == 0.445
    assert estimate.applied_pricing_tiers == (">=200000",)


def test_estimate_llm_run_cost_returns_none_without_token_usage() -> None:
    estimate = estimate_llm_run_cost(
        input_tokens=None,
        output_tokens=None,
        provider="custom",
        model="grok-4.5",
        currency="CNY",
        input_price_per_million_tokens=1.0,
        output_price_per_million_tokens=3.0,
    )

    assert estimate is None


def test_estimate_llm_run_cost_marks_missing_prices_as_unconfigured() -> None:
    estimate = estimate_llm_run_cost(
        input_tokens=123,
        output_tokens=456,
        provider="custom",
        model="grok-4.5",
        currency="CNY",
        input_price_per_million_tokens=0,
        output_price_per_million_tokens=0,
    )

    assert estimate is not None
    assert estimate.input_cost == 0
    assert estimate.output_cost == 0
    assert estimate.total_cost == 0
    assert estimate.pricing_configured is False
