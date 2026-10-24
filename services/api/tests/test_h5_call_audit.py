"""固定预算审计须包含各来源重试，缺用量不能报完整费用或假零。"""

import pytest


def event(**kwargs):
    return dict(
        source="h5_shortflow_agent",
        operation="h5-player-workflow",
        model="controlled",
        provider="controlled",
        physical_request=True,
        ai_calls=1,
        usage_available=True,
        pricing_configured=True,
        total_tokens=120,
        total_cost="0.25",
        cost_currency="CNY",
        **kwargs,
    )


def test_all_sources_and_retries_count_as_physical_calls_not_work_versions():
    from services.api.app.application.h5_call_audit import summarize_h5_calls

    rows = [event(), event() | {"source": "page_detection", "ai_calls": 2, "total_tokens": 240}]
    result = summarize_h5_calls(rows, budget={"max_calls": 200, "admitted_calls": 4})
    assert result["recorded_physical_calls"] == 3
    assert result["admitted_without_usage_event"] == 1
    assert result["total_tokens"] is None
    assert result["known_tokens"] == 360
    assert result["total_cost_cny"] is None
    assert result["known_cost_cny"] == "0.50"
    assert result["calls_by_source"][0]["physical_calls"] == 1


@pytest.mark.parametrize("fault", ["usage", "pricing", "usd", "missing_cost"])
def test_partial_usage_never_becomes_complete_cost(fault):
    from services.api.app.application.h5_call_audit import summarize_h5_calls

    bad = event()
    if fault == "usage":
        bad["usage_available"] = False
    elif fault == "pricing":
        bad["pricing_configured"] = False
    elif fault == "usd":
        bad["cost_currency"] = "USD"
    else:
        bad["total_cost"] = None
    result = summarize_h5_calls([event(), bad], budget={"max_calls": 200, "admitted_calls": 2})
    assert result["total_cost_cny"] is None
    assert result["known_cost_cny"] == "0.25"
    assert result["cost_complete"] is False
    assert result["recorded_physical_calls"] == 2
    if fault == "usage":
        assert result["total_tokens"] is None
        assert result["usage_unavailable_calls"] == 1


def test_queued_missing_budget_is_not_budget_exhaustion_or_zero_cost():
    from services.api.app.application.h5_call_audit import summarize_h5_calls

    result = summarize_h5_calls([], budget=None)
    assert result["budget_status"] == "not_initialized"
    assert result["total_cost_cny"] is None
    assert result["known_cost_cny"] is None
    assert result["cost_complete"] is False


def test_closed_complete_calls_have_exact_cny_without_exchange_conversion():
    from services.api.app.application.h5_call_audit import summarize_h5_calls

    result = summarize_h5_calls([event()], budget={"max_calls": 200, "admitted_calls": 1})
    assert result["total_cost_cny"] == "0.25"
    assert result["cost_complete"] is True


def test_legacy_event_without_physical_request_proof_is_not_complete_physical_accounting():
    from services.api.app.application.h5_call_audit import summarize_h5_calls

    result = summarize_h5_calls(
        [event() | {"physical_request": None}], budget={"max_calls": 200, "admitted_calls": 1}
    )
    assert result["recorded_physical_calls"] == 0
    assert result["recorded_usage_calls"] == 1
    assert result["physical_request_unverified_calls"] == 1
    assert result["cost_complete"] is False
    assert result["total_tokens"] is None
    assert result["known_tokens"] == 120
