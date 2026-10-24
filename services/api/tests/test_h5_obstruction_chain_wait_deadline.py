"""下一层弹窗等待计入采集耗时，且不丢弃在途观察到的真实阻断。"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from playwright.async_api import async_playwright

from services.api.app.application.h5_modal_close import H5_MODAL_CLOSE_SELECTOR, h5_modal_close
from services.api.app.infrastructure.browser import page_capture as module


class Clock:
    def __init__(self):
        self.now = 100.0
        self.waits = []

    async def wait_for_timeout(self, milliseconds):
        self.waits.append(milliseconds)
        self.now += milliseconds / 1000


async def wait_for_next(page, *, timeout_ms=5000):
    return await module._wait_for_next_obstruction_target(
        page=page, selectors=("text=关闭",), timeout_ms=timeout_ms,
        prefer_safe_outside=True, h5_session=True,
    )


async def test_slow_snapshots_consume_one_fixed_deadline_without_a_final_extra_snapshot(
    monkeypatch,
):
    clock = Clock()
    starts = []
    absent = {"dialog_present": False}

    async def resolve(**kwargs):
        starts.append(clock.now)
        clock.now += 2.0
        return absent

    monkeypatch.setattr(module, "time", SimpleNamespace(perf_counter=lambda: clock.now))
    monkeypatch.setattr(module, "_resolve_first_popup_dismissal_target", resolve)

    result = await wait_for_next(clock)

    assert result is absent
    assert starts == pytest.approx([100.6, 102.85])
    assert clock.waits == pytest.approx([600, 250, 150])
    assert clock.now == pytest.approx(105.0)


async def test_settle_cannot_extend_a_short_deadline_or_start_an_expired_snapshot(monkeypatch):
    clock = Clock()
    calls = []

    async def resolve(**kwargs):
        calls.append(clock.now)
        return {"dialog_present": False}

    monkeypatch.setattr(module, "time", SimpleNamespace(perf_counter=lambda: clock.now))
    monkeypatch.setattr(module, "_resolve_first_popup_dismissal_target", resolve)

    assert await wait_for_next(clock, timeout_ms=100) == {
        "dialog_present": True, "kind": "unproven",
    }
    assert not calls
    assert clock.waits == pytest.approx([100])
    assert clock.now == pytest.approx(100.1)


@pytest.mark.parametrize("timeout_ms", [0, -1])
@pytest.mark.parametrize("supports_wait", [False, True])
async def test_expired_window_never_starts_a_snapshot(monkeypatch, timeout_ms, supports_wait):
    clock = Clock()
    calls = []

    async def resolve(**kwargs):
        calls.append(clock.now)
        return {"dialog_present": False}

    monkeypatch.setattr(module, "time", SimpleNamespace(perf_counter=lambda: clock.now))
    monkeypatch.setattr(module, "_resolve_first_popup_dismissal_target", resolve)
    page = clock if supports_wait else SimpleNamespace()

    assert await wait_for_next(page, timeout_ms=timeout_ms) == {
        "dialog_present": True, "kind": "unproven",
    }
    assert not calls
    assert not clock.waits


async def test_chain_with_short_remaining_deadline_keeps_unobserved_layer_blocking(monkeypatch):
    clock = Clock()
    resolve = AsyncMock(return_value={"dialog_present": False})
    trigger = AsyncMock()
    monkeypatch.setattr(module, "time", SimpleNamespace(perf_counter=lambda: clock.now))
    monkeypatch.setattr(module, "MAX_OBSTRUCTION_CHAIN_DURATION_MS", 100)
    monkeypatch.setattr(module, "_resolve_first_popup_dismissal_target", resolve)
    monkeypatch.setattr(module, "_run_single_trigger", trigger)
    request = module.PageCaptureRequest(
        session_id="h5:short-deadline", mode=module.PageCaptureMode.SINGLE_TRIGGER,
        trigger_action=module.PageTriggerAction.CLICK, selector="text=关闭",
        clear_obstructions=True, capture_screenshot=False,
        h5_popup_dismissal_strategy="safe_outside_then_close",
    )
    trace = [{"action": "click", "dismissal_verified": True}]

    metrics = await module._clear_remaining_obstructions(
        page=clock, context=SimpleNamespace(), request=request, action_trace=trace,
        obstruction_frames=[], timeout_ms=5000, timeout_error_type=TimeoutError,
    )

    assert metrics["remaining_obstruction"] is True
    assert metrics["outcome"] == "llm_fallback_required"
    assert metrics["llm_fallback_required"] is True
    assert metrics["dismissal_count"] == 1
    resolve.assert_not_awaited()
    trigger.assert_not_awaited()
    assert clock.waits == pytest.approx([100])
    with pytest.raises(module.BlockedPageCaptureActionError) as error:
        module._require_h5_obstruction_chain_cleared(
            request=request, metrics=metrics, action_trace=trace,
        )
    assert error.value.error_code == "H5_POPUP_CHAIN_REMAINS_BLOCKED"


@pytest.mark.parametrize("kind", ["close_control", "unproven"])
async def test_inflight_blocking_snapshot_keeps_its_identity_after_deadline(monkeypatch, kind):
    clock = Clock()
    calls = []
    target = {
        "dialog_present": True, "kind": kind, "selector": "#next-close",
        "dialog_selector": "#next-panel", "dialog_node_token": "next-node-token",
        "dialog_signature": "next-panel-signature", "signed_selector": "next-signed-selector",
    }

    async def resolve(**kwargs):
        calls.append(clock.now)
        clock.now += 2.0 if len(calls) == 1 else 2.5
        return {"dialog_present": False} if len(calls) == 1 else target

    monkeypatch.setattr(module, "time", SimpleNamespace(perf_counter=lambda: clock.now))
    monkeypatch.setattr(module, "_resolve_first_popup_dismissal_target", resolve)

    assert await wait_for_next(clock) is target
    assert calls == pytest.approx([100.6, 102.85])
    assert clock.now == pytest.approx(105.35)
    assert clock.waits == [600, 250]


async def test_next_layer_inside_window_returns_the_original_valid_identity(monkeypatch):
    clock = Clock()
    calls = []
    target = {"dialog_present": True, "kind": "close_control",
              "dialog_node_token": "new-layer-token", "signed_selector": "new-layer-signature"}

    async def resolve(**kwargs):
        assert kwargs["prefer_safe_outside"] is True
        assert kwargs["h5_session"] is True
        calls.append(clock.now)
        clock.now += 0.1
        return {"dialog_present": False} if len(calls) == 1 else target

    monkeypatch.setattr(module, "time", SimpleNamespace(perf_counter=lambda: clock.now))
    monkeypatch.setattr(module, "_resolve_first_popup_dismissal_target", resolve)

    assert await wait_for_next(clock, timeout_ms=2000) is target
    assert len(calls) == 2
    assert clock.now == pytest.approx(101.05)


@pytest.mark.parametrize("phase", ["settle", "snapshot", "poll"])
async def test_cancellation_propagates_without_extra_snapshot_or_wait(monkeypatch, phase):
    clock = Clock()
    entered = asyncio.Event()
    calls = []

    async def block():
        entered.set()
        await asyncio.Event().wait()

    async def sleep(milliseconds):
        clock.waits.append(milliseconds)
        if phase == "settle" or (phase == "poll" and len(clock.waits) == 2):
            await block()
        clock.now += milliseconds / 1000

    async def resolve(**kwargs):
        calls.append(clock.now)
        if phase == "snapshot":
            await block()
        return {"dialog_present": False}

    monkeypatch.setattr(module, "time", SimpleNamespace(perf_counter=lambda: clock.now))
    monkeypatch.setattr(module, "_resolve_first_popup_dismissal_target", resolve)
    task = asyncio.create_task(wait_for_next(SimpleNamespace(wait_for_timeout=sleep)))
    try:
        await asyncio.wait_for(entered.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert len(calls) == (0 if phase == "settle" else 1)
        assert len(clock.waits) == (2 if phase == "poll" else 1)
    finally:
        if not task.done():
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task


async def test_slow_async_snapshot_uses_elapsed_time_not_only_sleep(monkeypatch):
    starts = []
    loop = asyncio.get_running_loop()
    started = loop.time()

    async def sleep(milliseconds):
        await asyncio.sleep(milliseconds / 1000)

    async def resolve(**kwargs):
        starts.append(loop.time() - started)
        await asyncio.sleep(0.05)
        return {"dialog_present": False}

    monkeypatch.setattr(module, "OBSTRUCTION_CHAIN_LAYER_SETTLE_MS", 5)
    monkeypatch.setattr(module, "OBSTRUCTION_CHAIN_NEXT_LAYER_POLL_MS", 5)
    monkeypatch.setattr(module, "_resolve_first_popup_dismissal_target", resolve)

    assert await wait_for_next(SimpleNamespace(wait_for_timeout=sleep), timeout_ms=80) == {
        "dialog_present": False,
    }
    assert 1 <= len(starts) <= 2
    assert all(offset < 0.08 for offset in starts)


async def test_real_delayed_next_layer_returns_a_current_signed_identity():
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            page = await browser.new_page(viewport={"width": 390, "height": 844})
            await page.set_content("""
                <main><h1>产品首页</h1></main>
                <script>setTimeout(() => {
                  document.body.insertAdjacentHTML('beforeend',
                    '<div id="next-panel" role="dialog" style="position:fixed;inset:0;z-index:999">'
                    + '<button id="next-close" type="button">关闭</button></div>');
                }, 750)</script>
            """)
            target = await wait_for_next(page, timeout_ms=1500)
            assert target is not None and target["dialog_present"] is True
            assert target["kind"] == "close_control"
            assert target["dialog_selector"] == "#next-panel"
            assert target["dialog_node_token"] and target["dialog_signature"]
            proof = h5_modal_close(await module._collect_ui_snapshot(
                page, surface_session_id="h5:next-modal",
            ))
            assert proof is not None
            assert target["signed_selector"] == (
                f"{H5_MODAL_CLOSE_SELECTOR}:{proof.obstruction_signature}"
            )
        finally:
            await browser.close()
