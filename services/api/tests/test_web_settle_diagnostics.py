"""真实 Web 等待循环必须保留稳定窗口重置原因，诊断不得改变准入。"""

from types import SimpleNamespace

import pytest

from services.api.app.application.web_readiness_contracts import WebReadinessProfile
from services.api.app.infrastructure.browser import page_capture as capture


@pytest.mark.asyncio
async def test_waiter_retains_reset_history_after_signals_recover(monkeypatch):
    tick = 0

    async def readiness(page):
        nonlocal tick
        tick += 1
        return {
            "core_present": tick != 2,
            "visible_loader": False,
            "transition_active": False,
            "necessary_media_ready": True,
            "dom_quiet_ms": 1200,
        }

    async def structure(page):
        return {
            "structure_digest": "first" if tick < 4 else "second",
            "document_time_origin_ms": 1000 if tick < 5 else 2000,
        }

    async def monitor(page, **kwargs):
        return 0

    async def wait(**kwargs):
        return 0

    monkeypatch.setattr(capture, "_read_page_readiness", readiness)
    monkeypatch.setattr(capture, "_read_web_structure", structure)
    monkeypatch.setattr(capture, "_install_page_state_monitor", monitor)
    monkeypatch.setattr(capture, "_wait_for_page_state_change", wait)
    monkeypatch.setattr(capture.time, "perf_counter", lambda: tick / 100 + 1)
    diagnostics = {}
    result = await capture._wait_for_web_page_settle(
        page=SimpleNamespace(url="https://example.test/list"),
        network_inflight={},
        max_wait_ms=120,
        diagnostics=diagnostics,
        profile=WebReadinessProfile(minimum_window_ms=20),
    )
    assert result is True
    events = diagnostics["window_reset_events"]
    assert [event["reasons"] for event in events] == [
        ["core_present_not_ready"],
        ["structure_changed"],
        ["document_epoch_changed"],
    ]
    assert [event["sample_index"] for event in events] == [2, 4, 5]
    assert all(event["observed_at_ms"] > 0 for event in events)
    assert diagnostics["window_reset_count"] == 3
    assert diagnostics["window_reset_events_truncated"] is False


def test_reset_history_is_bounded_and_keeps_total_count():
    from services.api.app.infrastructure.browser.web_settle_diagnostics import record_window_reset

    diagnostics = {}
    for index in range(70):
        record_window_reset(
            diagnostics,
            previous=("url", 1000, "a"),
            current=("url", 1000, "b"),
            reasons=[],
            sample_index=index + 1,
            observed_at_ms=index * 10,
            previous_stable_count=1,
        )
    assert diagnostics["window_reset_count"] == 70
    assert len(diagnostics["window_reset_events"]) == 64
    assert diagnostics["window_reset_events_truncated"] is True


@pytest.mark.parametrize("fault,minimum_samples,expected", [
    ("healthy", 3, True),
    ("healthy", 4, True),
    ("late_quiet", 3, False),
    ("structure_churn", 3, False),
    ("loader", 3, False),
    ("pending_script", 3, False),
    ("slow_observer", 3, False),
])
async def test_two_second_capture_window_keeps_quorum_without_extending_deadline(
    monkeypatch, fault, minimum_samples, expected,
):
    # 真实采集在截图前注入稳定样式，首次样本的DOM静默时间从零重新累计。
    clock = [10.0]
    intervals = []

    async def monitor(*args, **kwargs):
        return 0

    async def readiness(page):
        if fault == "slow_observer":
            clock[0] += 2.1
        return {
            "core_present": True,
            "necessary_media_ready": True,
            "visible_loader": fault == "loader",
            "transition_active": False,
            "dom_quiet_ms": max(0, (clock[0] - 10) * 1000 - (800 if fault == "late_quiet" else 0)),
        }

    async def structure(page):
        return {"structure_digest": str(clock[0]) if fault == "structure_churn" else "same",
                "document_time_origin_ms": 1000}

    async def wait(**kwargs):
        intervals.append(kwargs["timeout_ms"])
        clock[0] += max(1, kwargs["timeout_ms"]) / 1000
        return 0

    monkeypatch.setattr(capture.time, "perf_counter", lambda: clock[0])
    monkeypatch.setattr(capture, "_install_page_state_monitor", monitor)
    monkeypatch.setattr(capture, "_read_page_readiness", readiness)
    monkeypatch.setattr(capture, "_read_web_structure", structure)
    monkeypatch.setattr(capture, "_wait_for_page_state_change", wait)
    diagnostics = {}
    pending = ({1: {"url": "https://example.test/app.js", "resource_type": "script"}}
               if fault == "pending_script" else {})
    result = await capture._wait_for_web_page_settle(
        page=SimpleNamespace(url="https://example.test/"), network_inflight=pending,
        max_wait_ms=2000, diagnostics=diagnostics,
        profile=WebReadinessProfile(minimum_samples=minimum_samples),
    )
    assert result is expected, diagnostics
    assert diagnostics["quorum_met"] is expected
    assert diagnostics["timed_out"] is (not expected)
    assert all(value <= 1000 for value in intervals)
    if expected:
        assert diagnostics["stable_sample_count"] >= minimum_samples
        assert (
            diagnostics["sample_window_ended_ms"] - diagnostics["sample_window_started_ms"] >= 600
        )
        assert clock[0] <= 12
