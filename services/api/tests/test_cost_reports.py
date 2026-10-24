from datetime import UTC, datetime

from services.api.app.application.cost_reports import (
    build_cost_report,
    build_usage_event_report,
)
from services.api.app.core.config import LLMSettings


def test_cost_report_reestimates_unconfigured_zero_cost_with_current_prices() -> None:
    report = build_cost_report(
        [
            {
                "run_id": "run-1",
                "task_id": "task-1",
                "task_name": "历史零成本任务",
                "status": "done",
                "provider": "custom",
                "model": "grok-4.5",
                "ai_calls": 1,
                "input_tokens": 50_000,
                "output_tokens": 10_526,
                "cache_hit_tokens": 0,
                "total_tokens": 60_526,
                "input_cost": 0,
                "output_cost": 0,
                "cache_hit_cost": 0,
                "total_cost": 0,
                "cost_currency": "CNY",
                "pricing_source": "runtime_config_per_call",
                "pricing_configured": False,
                "started_at": datetime(2026, 8, 18, 10, 0, tzinfo=UTC),
                "finished_at": datetime(2026, 8, 18, 10, 1, tzinfo=UTC),
                "created_at": datetime(2026, 8, 18, 9, 59, tzinfo=UTC),
            }
        ],
        llm_settings=LLMSettings(
            provider="custom",
            model="grok-4.5",
            cost_currency="CNY",
            input_price_per_million_tokens=1,
            output_price_per_million_tokens=3,
            cache_hit_price_per_million_tokens=0.25,
            long_context_threshold_tokens=200_000,
            long_context_input_price_per_million_tokens=2,
            long_context_output_price_per_million_tokens=6,
            long_context_cache_hit_price_per_million_tokens=0.5,
        ),
        days=30,
        recent_run_limit=50,
        generated_at=datetime(2026, 8, 18, 10, 2, tzinfo=UTC),
    )

    item = report.recent_runs[0]
    assert item.input_cost == 0.05
    assert item.output_cost == 0.031578
    assert item.total_cost == 0.081578
    assert item.billable_input_tokens == 50_000
    assert item.pricing_source == "runtime_config_aggregate"
    assert item.pricing_configured is True
    assert item.applied_pricing_tiers == ("0-200000",)
    assert report.total_cost == 0.081578


def test_cost_report_aggregates_multiple_runs_by_task() -> None:
    report = build_cost_report(
        [
            {
                "run_id": "consumer-run",
                "task_id": "task-1",
                "task_name": "测试JHA-204",
                "task_status": "running",
                "status": "done",
                "provider": "custom",
                "model": "grok-4.5",
                "ai_calls": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "cache_hit_tokens": 0,
                "total_tokens": 0,
                "input_cost": None,
                "output_cost": None,
                "cache_hit_cost": None,
                "total_cost": None,
                "cost_currency": "CNY",
                "pricing_source": None,
                "pricing_configured": None,
                "started_at": datetime(2026, 8, 25, 14, 38, tzinfo=UTC),
                "finished_at": datetime(2026, 8, 25, 14, 40, tzinfo=UTC),
                "created_at": datetime(2026, 8, 25, 14, 38, tzinfo=UTC),
            },
            {
                "run_id": "main-run",
                "task_id": "task-1",
                "task_name": "测试JHA-204",
                "task_status": "running",
                "status": "failed",
                "provider": "custom",
                "model": "grok-4.5",
                "ai_calls": 85,
                "input_tokens": 878_904,
                "output_tokens": 46_647,
                "cache_hit_tokens": 0,
                "total_tokens": 925_551,
                "input_cost": 0.7031232,
                "output_cost": 0.1119528,
                "cache_hit_cost": 0,
                "total_cost": 0.815076,
                "cost_currency": "CNY",
                "pricing_source": "runtime_config_per_call",
                "pricing_configured": True,
                "started_at": datetime(2026, 8, 25, 14, 18, tzinfo=UTC),
                "finished_at": datetime(2026, 8, 25, 14, 38, tzinfo=UTC),
                "created_at": datetime(2026, 8, 25, 14, 18, tzinfo=UTC),
            },
        ],
        llm_settings=LLMSettings(
            provider="custom",
            model="grok-4.5",
            cost_currency="CNY",
            input_price_per_million_tokens=0.8,
            output_price_per_million_tokens=2.4,
            cache_hit_price_per_million_tokens=0.2,
            long_context_threshold_tokens=200_000,
            long_context_input_price_per_million_tokens=1.6,
            long_context_output_price_per_million_tokens=4.8,
            long_context_cache_hit_price_per_million_tokens=0.4,
        ),
        days=30,
        recent_run_limit=50,
        generated_at=datetime(2026, 8, 25, 14, 42, tzinfo=UTC),
    )

    assert len(report.task_items) == 1
    task_item = report.task_items[0]
    assert task_item.task_id == "task-1"
    assert task_item.run_count == 2
    assert task_item.status == "running"
    assert task_item.latest_run_id == "consumer-run"
    assert task_item.latest_run_status == "done"
    assert task_item.ai_calls == 85
    assert task_item.total_tokens == 925_551
    assert task_item.total_cost == 0.815076
    assert task_item.billable_input_tokens == 878_904

    by_day = {bucket.key: bucket for bucket in report.by_day}
    day_bucket = by_day["2026-08-25"]
    assert day_bucket.run_count == 2
    assert day_bucket.ai_calls == 85
    assert day_bucket.total_tokens == 925_551
    assert day_bucket.total_cost == 0.815076

    by_model = {bucket.key: bucket for bucket in report.by_model}
    model_bucket = by_model["custom/grok-4.5"]
    assert model_bucket.run_count == 2
    assert model_bucket.ai_calls == 85
    assert model_bucket.total_cost == 0.815076


def test_cost_report_uses_task_usage_ledger_for_task_costs() -> None:
    report = build_cost_report(
        [
            {
                "run_id": "consumer-run",
                "task_id": "task-1",
                "task_name": "CloudFront H5 验收",
                "task_status": "canceled",
                "status": "canceled",
                "provider": "custom",
                "model": "grok-4.5",
                "ai_calls": 7,
                "input_tokens": 121_694,
                "output_tokens": 13_763,
                "cache_hit_tokens": 65_664,
                "billable_input_tokens": 56_030,
                "total_tokens": 135_457,
                "input_cost": 0.044824,
                "output_cost": 0.0330312,
                "cache_hit_cost": 0.0131328,
                "total_cost": 0.090988,
                "cost_currency": "CNY",
                "pricing_source": "runtime_config_per_call",
                "pricing_configured": True,
                "applied_pricing_tiers": ["0-200000"],
                "pricing_breakdown": [
                    {
                        "pricing_tier": "0-200000",
                        "pricing_source": "runtime_config_per_call",
                        "input_price_per_million_tokens": 0.8,
                        "output_price_per_million_tokens": 2.4,
                        "cache_hit_price_per_million_tokens": 0.2,
                        "ai_calls": 7,
                        "input_tokens": 121_694,
                        "billable_input_tokens": 56_030,
                        "output_tokens": 13_763,
                        "cache_hit_tokens": 65_664,
                        "total_cost": 0.090988,
                    }
                ],
                "task_usage_event_count": 12,
                "task_usage_missing_count": 2,
                "task_usage_ai_calls": 12,
                "task_usage_input_tokens": 247_722,
                "task_usage_output_tokens": 17_476,
                "task_usage_cache_hit_tokens": 67_072,
                "task_usage_billable_input_tokens": 180_650,
                "task_usage_total_tokens": 265_198,
                "task_usage_input_cost": 0.14452,
                "task_usage_output_cost": 0.0419424,
                "task_usage_cache_hit_cost": 0.0134144,
                "task_usage_total_cost": 0.1998768,
                "task_usage_cost_currencies": ["CNY"],
                "task_usage_providers": ["custom"],
                "task_usage_models": ["grok-4.5"],
                "task_usage_pricing_sources": ["runtime_config_per_call"],
                "task_usage_applied_pricing_tiers": ["0-200000"],
                "task_usage_pricing_breakdown": [
                    {
                        "pricing_tier": "0-200000",
                        "pricing_source": "runtime_config_per_call",
                        "input_price_per_million_tokens": 0.8,
                        "output_price_per_million_tokens": 2.4,
                        "cache_hit_price_per_million_tokens": 0.2,
                        "ai_calls": 7,
                        "input_tokens": 121_694,
                        "billable_input_tokens": 56_030,
                        "output_tokens": 13_763,
                        "cache_hit_tokens": 65_664,
                        "input_cost": 0.044824,
                        "output_cost": 0.0330312,
                        "cache_hit_cost": 0.0131328,
                        "total_cost": 0.090988,
                    },
                    {
                        "pricing_tier": "0-200000",
                        "pricing_source": "runtime_config_per_call",
                        "input_price_per_million_tokens": 0.8,
                        "output_price_per_million_tokens": 2.4,
                        "cache_hit_price_per_million_tokens": 0.2,
                        "ai_calls": 5,
                        "input_tokens": 126_028,
                        "billable_input_tokens": 124_620,
                        "output_tokens": 3_713,
                        "cache_hit_tokens": 1_408,
                        "input_cost": 0.099696,
                        "output_cost": 0.0089112,
                        "cache_hit_cost": 0.0002816,
                        "total_cost": 0.1088888,
                    },
                ],
                "started_at": datetime(2026, 8, 28, 16, 6, tzinfo=UTC),
                "finished_at": datetime(2026, 8, 28, 16, 17, tzinfo=UTC),
                "created_at": datetime(2026, 8, 28, 16, 6, tzinfo=UTC),
            }
        ],
        llm_settings=LLMSettings(
            provider="custom",
            model="grok-4.5",
            cost_currency="CNY",
            input_price_per_million_tokens=0.8,
            output_price_per_million_tokens=2.4,
            cache_hit_price_per_million_tokens=0.2,
            long_context_threshold_tokens=200_000,
            long_context_input_price_per_million_tokens=1.6,
            long_context_output_price_per_million_tokens=4.8,
            long_context_cache_hit_price_per_million_tokens=0.4,
        ),
        days=30,
        recent_run_limit=50,
        generated_at=datetime(2026, 8, 28, 16, 18, tzinfo=UTC),
    )

    assert report.recent_runs[0].ai_calls == 7
    assert report.recent_runs[0].total_cost == 0.090988

    task_item = report.task_items[0]
    assert task_item.ai_calls == 12
    assert task_item.usage_missing_count == 2
    assert task_item.total_tokens == 265_198
    assert task_item.total_cost == 0.1998768
    assert task_item.pricing_breakdown[0]["ai_calls"] == 12

    assert report.ai_calls == 12
    assert report.usage_missing_count == 2
    assert report.total_tokens == 265_198
    assert report.total_cost == 0.1998768

    by_day = {bucket.key: bucket for bucket in report.by_day}
    assert by_day["2026-08-28"].run_count == 1
    assert by_day["2026-08-28"].usage_missing_count == 2
    assert by_day["2026-08-28"].total_cost == 0.1998768


def test_cost_report_does_not_duplicate_task_usage_ledger_across_runs() -> None:
    rows = []
    for index, run_id in enumerate(("run-a", "run-b")):
        rows.append(
            {
                "run_id": run_id,
                "task_id": "task-1",
                "task_name": "多 Run 任务",
                "task_status": "done",
                "status": "completed",
                "provider": "custom",
                "model": "grok-4.5",
                "ai_calls": 1,
                "input_tokens": 100,
                "output_tokens": 20,
                "cache_hit_tokens": 0,
                "total_tokens": 120,
                "total_cost": 0.000128,
                "cost_currency": "CNY",
                "pricing_configured": True,
                "task_usage_event_count": 3,
                "task_usage_missing_count": 1,
                "task_usage_ai_calls": 3,
                "task_usage_input_tokens": 300,
                "task_usage_output_tokens": 60,
                "task_usage_cache_hit_tokens": 0,
                "task_usage_billable_input_tokens": 300,
                "task_usage_total_tokens": 360,
                "task_usage_input_cost": 0.00024,
                "task_usage_output_cost": 0.000144,
                "task_usage_cache_hit_cost": 0,
                "task_usage_total_cost": 0.000384,
                "task_usage_cost_currencies": ["CNY"],
                "task_usage_providers": ["custom"],
                "task_usage_models": ["grok-4.5"],
                "task_usage_pricing_sources": ["runtime_config_per_call"],
                "task_usage_applied_pricing_tiers": ["0-200000"],
                "task_usage_pricing_breakdown": [],
                "created_at": datetime(2026, 8, 28, 16, index, tzinfo=UTC),
            }
        )

    report = build_cost_report(
        rows,
        llm_settings=LLMSettings(
            provider="custom",
            model="grok-4.5",
            cost_currency="CNY",
        ),
        days=30,
        recent_run_limit=50,
        generated_at=datetime(2026, 8, 28, 16, 2, tzinfo=UTC),
    )

    assert report.run_count == 2
    assert len(report.task_items) == 1
    assert report.task_items[0].run_count == 2
    assert report.task_items[0].ai_calls == 3
    assert report.task_items[0].usage_missing_count == 1
    assert report.task_items[0].total_cost == 0.000384
    assert report.ai_calls == 3
    assert report.usage_missing_count == 1
    assert report.total_cost == 0.000384


def test_cost_report_marks_failed_provider_attempt_as_unknown_cost_lower_bound() -> None:
    report = build_cost_report(
        [
            {
                "run_id": "failed-run",
                "task_id": "task-1",
                "task_name": "供应商失败任务",
                "task_status": "failed",
                "status": "failed",
                "provider": "custom",
                "model": "grok-4.5",
                "ai_calls": 1,
                "run_usage_missing_count": 1,
                "input_tokens": 0,
                "output_tokens": 0,
                "cache_hit_tokens": 0,
                "billable_input_tokens": 0,
                "total_tokens": 0,
                "input_cost": 0,
                "output_cost": 0,
                "cache_hit_cost": 0,
                "total_cost": 0,
                "cost_currency": "CNY",
                "pricing_source": "runtime_config_per_call",
                "pricing_configured": True,
                "task_usage_event_count": 1,
                "task_usage_missing_count": 1,
                "task_usage_ai_calls": 1,
                "task_usage_input_tokens": 0,
                "task_usage_output_tokens": 0,
                "task_usage_cache_hit_tokens": 0,
                "task_usage_billable_input_tokens": 0,
                "task_usage_total_tokens": 0,
                "task_usage_input_cost": 0,
                "task_usage_output_cost": 0,
                "task_usage_cache_hit_cost": 0,
                "task_usage_total_cost": 0,
                "task_usage_cost_currencies": ["CNY"],
                "task_usage_providers": ["custom"],
                "task_usage_models": ["grok-4.5"],
                "task_usage_pricing_sources": ["runtime_config_per_call"],
                "task_usage_applied_pricing_tiers": [],
                "task_usage_pricing_breakdown": [],
                "created_at": datetime(2026, 9, 1, 10, 0, tzinfo=UTC),
            }
        ],
        llm_settings=LLMSettings(
            provider="custom",
            model="grok-4.5",
            cost_currency="CNY",
        ),
        days=30,
        recent_run_limit=50,
        generated_at=datetime(2026, 9, 1, 10, 1, tzinfo=UTC),
    )

    assert report.ai_calls == 1
    assert report.usage_missing_count == 1
    assert report.total_tokens == 0
    assert report.total_cost == 0
    assert report.task_items[0].usage_missing_count == 1
    assert report.recent_runs[0].usage_missing_count == 1
    assert report.by_status[0].usage_missing_count == 1


def test_usage_event_report_includes_debug_and_task_costs() -> None:
    report = build_usage_event_report(
        [
            {
                "event_id": "event-task",
                "scope_type": "task_run",
                "task_id": "task-1",
                "task_name": "正式任务",
                "agent_run_id": "run-1",
                "agent_type": "main_agent",
                "agent_name": "MainAgent",
                "run_status": "completed",
                "source": "main_agent",
                "operation": "agent_response",
                "provider": "custom",
                "model": "grok-4.5",
                "api_style": "chat_completion",
                "usage_available": True,
                "ai_calls": 1,
                "input_tokens": 1000,
                "output_tokens": 200,
                "cache_hit_tokens": 100,
                "billable_input_tokens": 900,
                "total_tokens": 1200,
                "input_cost": 0.00072,
                "output_cost": 0.00048,
                "cache_hit_cost": 0.00002,
                "total_cost": 0.00122,
                "cost_currency": "CNY",
                "pricing_source": "runtime_config_per_call",
                "pricing_unit": "1m_tokens",
                "pricing_configured": True,
                "pricing_tier": "0-200000",
                "input_price_per_million_tokens": 0.8,
                "output_price_per_million_tokens": 2.4,
                "cache_hit_price_per_million_tokens": 0.2,
                "calculation": {
                    "formula": "cost formula",
                },
                "created_at": datetime(2026, 8, 26, 10, 0, tzinfo=UTC),
            },
            {
                "event_id": "event-debug",
                "scope_type": "debug",
                "task_id": None,
                "task_name": None,
                "agent_run_id": None,
                "agent_type": None,
                "agent_name": None,
                "run_status": None,
                "source": "devui_debug_agent",
                "operation": "debug_agent_response",
                "provider": "custom",
                "model": "grok-4.5",
                "api_style": "chat_completion",
                "usage_available": False,
                "ai_calls": 1,
                "input_tokens": 0,
                "output_tokens": 0,
                "cache_hit_tokens": 0,
                "billable_input_tokens": 0,
                "total_tokens": 0,
                "input_cost": 0,
                "output_cost": 0,
                "cache_hit_cost": 0,
                "total_cost": 0,
                "cost_currency": "CNY",
                "pricing_source": "runtime_config_per_call",
                "pricing_unit": "1m_tokens",
                "pricing_configured": True,
                "pricing_tier": None,
                "input_price_per_million_tokens": 0,
                "output_price_per_million_tokens": 0,
                "cache_hit_price_per_million_tokens": 0,
                "calculation": {"formula": ""},
                "created_at": datetime(2026, 8, 26, 10, 1, tzinfo=UTC),
            },
        ],
        llm_settings=LLMSettings(
            provider="custom",
            model="grok-4.5",
            cost_currency="CNY",
        ),
        days=30,
        recent_event_limit=20,
        generated_at=datetime(2026, 8, 26, 10, 2, tzinfo=UTC),
    )

    assert report.event_count == 2
    assert report.task_event_count == 1
    assert report.debug_event_count == 1
    assert report.usage_missing_count == 1
    assert report.ai_calls == 2
    assert report.billable_input_tokens == 900
    assert report.total_cost == 0.00122
    by_scope = {bucket.key: bucket for bucket in report.by_scope}
    assert by_scope["debug"].ai_calls == 1
    by_source = {bucket.key: bucket for bucket in report.by_source}
    assert by_source["devui_debug_agent"].run_count == 1
    assert report.recent_events[0].calculation_formula == "cost formula"
