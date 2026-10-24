"""LLM 用量流水聚合测试。"""

from services.api.app.infrastructure.db.llm_usage_event_repository import (
    _aggregate_usage_rows,
)


def test_aggregate_usage_rows_builds_run_billable_payload() -> None:
    rows = [
        {
            "source": "page_detection",
            "operation": "detect_page",
            "provider": "custom",
            "model": "grok-4.5",
            "usage_available": True,
            "ai_calls": 1,
            "input_tokens": 100,
            "output_tokens": 20,
            "cache_hit_tokens": 10,
            "billable_input_tokens": 90,
            "total_tokens": 120,
            "input_cost": 0.000072,
            "output_cost": 0.000048,
            "cache_hit_cost": 0.000002,
            "total_cost": 0.000122,
            "cost_currency": "CNY",
            "pricing_source": "runtime_config_per_call",
            "pricing_unit": "1m_tokens",
            "pricing_configured": True,
            "pricing_tier": "0-200000",
            "input_price_per_million_tokens": 0.8,
            "output_price_per_million_tokens": 2.4,
            "cache_hit_price_per_million_tokens": 0.2,
            "calculation": {
                "formula": "unit-cost",
                "pricing_breakdown": [
                    {
                        "pricing_tier": "0-200000",
                        "context_tokens": 100,
                        "input_tokens": 100,
                        "billable_input_tokens": 90,
                        "output_tokens": 20,
                        "cache_hit_tokens": 10,
                        "input_price_per_million_tokens": 0.8,
                        "output_price_per_million_tokens": 2.4,
                        "cache_hit_price_per_million_tokens": 0.2,
                        "input_cost": 0.000072,
                        "output_cost": 0.000048,
                        "cache_hit_cost": 0.000002,
                        "total_cost": 0.000122,
                        "pricing_unit": "1m_tokens",
                        "pricing_source": "runtime_config_per_call",
                        "pricing_configured": True,
                        "calculation_formula": "unit-cost",
                    }
                ],
            },
        },
        {
            "source": "semantic_rule_judge",
            "operation": "rule_judge",
            "provider": "custom",
            "model": "grok-4.5",
            "usage_available": True,
            "ai_calls": 1,
            "input_tokens": 300,
            "output_tokens": 40,
            "cache_hit_tokens": 0,
            "billable_input_tokens": 300,
            "total_tokens": 340,
            "input_cost": 0.00024,
            "output_cost": 0.000096,
            "cache_hit_cost": 0.0,
            "total_cost": 0.000336,
            "cost_currency": "CNY",
            "pricing_source": "runtime_config_per_call",
            "pricing_unit": "1m_tokens",
            "pricing_configured": True,
            "pricing_tier": "0-200000",
            "input_price_per_million_tokens": 0.8,
            "output_price_per_million_tokens": 2.4,
            "cache_hit_price_per_million_tokens": 0.2,
            "calculation": "{}",
        },
    ]

    payload = _aggregate_usage_rows(rows)

    assert payload is not None
    assert payload["usage_event_count"] == 2
    assert payload["ai_calls"] == 2
    assert payload["input_tokens"] == 400
    assert payload["output_tokens"] == 60
    assert payload["cache_hit_tokens"] == 10
    assert payload["billable_input_tokens"] == 390
    assert payload["total_tokens"] == 460
    assert payload["total_cost"] == 0.000458
    assert payload["cost_currency"] == "CNY"
    assert payload["provider"] == "custom"
    assert payload["model"] == "grok-4.5"
    assert payload["applied_pricing_tiers"] == ["0-200000"]
    assert payload["pricing_breakdown"] == [
        {
            "pricing_tier": "0-200000",
            "context_tokens": 400,
            "input_tokens": 400,
            "billable_input_tokens": 390,
            "output_tokens": 60,
            "cache_hit_tokens": 10,
            "input_price_per_million_tokens": 0.8,
            "output_price_per_million_tokens": 2.4,
            "cache_hit_price_per_million_tokens": 0.2,
            "input_cost": 0.000312,
            "output_cost": 0.000144,
            "cache_hit_cost": 0.000002,
            "total_cost": 0.000458,
            "pricing_unit": "1m_tokens",
            "pricing_source": "runtime_config_per_call",
            "pricing_configured": True,
            "calculation_formula": "unit-cost",
            "ai_calls": 2,
        }
    ]
