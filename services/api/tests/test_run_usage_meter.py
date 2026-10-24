"""Agent Run 级 LLM 用量汇总测试。"""

import asyncio

from services.api.app.agents.runtime.usage_meter import (
    bind_run_llm_usage_meter,
    record_current_llm_usage,
    record_current_openai_compatible_response_usage,
)
from services.api.app.core.config import get_settings


async def test_usage_meter_aggregates_child_task_llm_calls() -> None:
    async def record_child_call() -> None:
        record_current_llm_usage(
            {
                "input_token_count": 7,
                "output_token_count": 3,
                "total_token_count": 10,
            }
        )

    with bind_run_llm_usage_meter() as meter:
        record_current_llm_usage(
            {
                "input_token_count": 5,
                "output_token_count": 2,
                "total_token_count": 7,
            }
        )
        await asyncio.create_task(record_child_call())
        snapshot = meter.snapshot()

    assert snapshot is not None
    assert snapshot.ai_calls == 2
    assert snapshot.input_tokens == 12
    assert snapshot.output_tokens == 5
    assert snapshot.total_tokens == 17


async def test_usage_meter_normalizes_openai_compatible_usage_and_costs(monkeypatch) -> None:
    monkeypatch.setenv("ALIOTH_LLM_COST_CURRENCY", "CNY")
    monkeypatch.setenv("ALIOTH_LLM_INPUT_PRICE_PER_MILLION_TOKENS", "1")
    monkeypatch.setenv("ALIOTH_LLM_OUTPUT_PRICE_PER_MILLION_TOKENS", "3")
    monkeypatch.setenv("ALIOTH_LLM_CACHE_HIT_PRICE_PER_MILLION_TOKENS", "0.25")
    monkeypatch.setenv("ALIOTH_LLM_LONG_CONTEXT_THRESHOLD_TOKENS", "200000")
    get_settings.cache_clear()

    with bind_run_llm_usage_meter() as meter:
        record_current_llm_usage(
            {
                "usage": {
                    "prompt_tokens": "1000",
                    "completion_tokens": 200,
                    "total_tokens": 1200,
                    "prompt_tokens_details": {"cached_tokens": 100},
                }
            }
        )
        snapshot = meter.snapshot()

    assert snapshot is not None
    assert snapshot.input_tokens == 1000
    assert snapshot.output_tokens == 200
    assert snapshot.cache_hit_tokens == 100
    assert snapshot.billable_input_tokens == 900
    assert snapshot.total_tokens == 1200
    assert snapshot.ai_calls == 1
    assert snapshot.cost_currency == "CNY"
    assert snapshot.input_cost == 0.0009
    assert snapshot.output_cost == 0.0006
    assert snapshot.cache_hit_cost == 0.000025
    assert snapshot.total_cost == 0.001525
    assert snapshot.pricing_configured is True
    assert snapshot.applied_pricing_tiers == ("0-200000",)
    assert snapshot.pricing_breakdown == (
        {
            "pricing_tier": "0-200000",
            "context_tokens": 1000,
            "input_tokens": 1000,
            "billable_input_tokens": 900,
            "output_tokens": 200,
            "cache_hit_tokens": 100,
            "input_price_per_million_tokens": 1.0,
            "output_price_per_million_tokens": 3.0,
            "cache_hit_price_per_million_tokens": 0.25,
            "input_cost": 0.0009,
            "output_cost": 0.0006,
            "cache_hit_cost": 0.000025,
            "total_cost": 0.001525,
            "pricing_unit": "1m_tokens",
            "pricing_source": "runtime_config_per_call",
            "pricing_configured": True,
            "calculation_formula": (
                "(billable_input_tokens * input_price_per_million_tokens "
                "+ output_tokens * output_price_per_million_tokens "
                "+ cache_hit_tokens * cache_hit_price_per_million_tokens) "
                "/ 1000000"
            ),
            "ai_calls": 1,
        },
    )


async def test_usage_meter_records_openai_compatible_response_usage() -> None:
    response = type(
        "Response",
        (),
        {
            "usage": {
                "prompt_tokens": 30,
                "completion_tokens": 11,
                "total_tokens": 41,
            }
        },
    )()

    with bind_run_llm_usage_meter() as meter:
        record_current_openai_compatible_response_usage(response)
        snapshot = meter.snapshot()

    assert snapshot is not None
    assert snapshot.ai_calls == 1
    assert snapshot.input_tokens == 30
    assert snapshot.output_tokens == 11
    assert snapshot.total_tokens == 41


async def test_usage_meter_counts_openai_compatible_call_without_usage() -> None:
    response = object()

    with bind_run_llm_usage_meter() as meter:
        record_current_openai_compatible_response_usage(response)
        snapshot = meter.snapshot()

    assert snapshot is not None
    assert snapshot.ai_calls == 1
    assert snapshot.total_tokens is None
    assert snapshot.total_cost is None
