"""停止证明必须来自原资源；断连、协程结束与未知关闭均不能代替它。"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from services.api.app.application.capture_coordinator import InProcessSessionCommandGate
from services.api.app.application.h5_session_journey import (
    H5SessionJourneyGate,
    quarantine_h5_journeys,
)
from services.api.app.infrastructure.browser.page_capture import (
    PlaywrightPageCaptureBrowser,
    _PlaywrightSession,
)


async def test_waiter_rechecks_quarantine_after_lock_handoff():
    gate = H5SessionJourneyGate(delegate=InProcessSessionCommandGate())
    started, finish = asyncio.Event(), asyncio.Event()
    calls = []

    async def owner():
        with pytest.raises(RuntimeError):
            async with gate.hold_journey(task_id="task", session_id="h5:race"):
                started.set()
                await finish.wait()
                quarantine_h5_journeys("unknown")

    async def waiter():
        async with gate.hold_journey(task_id="task", session_id="h5:race"):
            calls.append("unsafe-action")

    running = asyncio.create_task(owner())
    await started.wait()
    queued = asyncio.create_task(waiter())
    await asyncio.sleep(0)
    finish.set()
    await running
    with pytest.raises(RuntimeError, match="未排空|未确认"):
        await queued
    assert calls == []


async def test_completed_unique_sessions_do_not_accumulate_local_locks():
    gate = H5SessionJourneyGate(delegate=InProcessSessionCommandGate())
    for index in range(1000):
        async with gate.hold_journey(task_id="task", session_id=f"h5:ended-{index}"):
            pass
    assert gate._locks == {}
    assert gate._active == {}


@pytest.mark.parametrize("session_id", ["h5:close", "web:close"])
async def test_unknown_browser_close_is_not_a_h5_stop_ack(session_id):
    browser = PlaywrightPageCaptureBrowser()
    session = _PlaywrightSession(
        playwright=SimpleNamespace(stop=AsyncMock()),
        browser=SimpleNamespace(close=AsyncMock(side_effect=RuntimeError("connection lost"))),
        context=None, page=None,
    )
    browser._sessions[session_id] = session
    if session_id.startswith("h5:"):
        with pytest.raises(RuntimeError):
            await browser.close_session(session_id)
        assert browser._sessions[session_id] is session
    else:
        await browser.close_session(session_id)
        assert session_id not in browser._sessions


@pytest.mark.parametrize("case", ["missing", "pending", "stop_failed", "valid"])
async def test_release_requires_bound_resource_and_completed_operations(case):
    from services.api.app.application.h5_session_journey import (
        bind_h5_physical_resource,
        track_h5_physical_operation,
    )
    from services.api.app.infrastructure.browser.h5_physical_stop import H5BrowserStopGuard

    physical = SimpleNamespace(
        close=AsyncMock(), is_connected=lambda: False,
    )
    guard = H5BrowserStopGuard(browser=physical, browser_pid=None)
    # 只在本用例隔离对象上提供已经核验的进程退出事实。
    guard.browser_pid = 987654321
    if case == "stop_failed":
        physical.close.side_effect = RuntimeError("close not acknowledged")
    gate = H5SessionJourneyGate(delegate=InProcessSessionCommandGate())
    finish = asyncio.Event()
    pending = asyncio.create_task(finish.wait())
    try:
        with pytest.raises(RuntimeError):
            async with gate.hold_journey(task_id="task", session_id="h5:proof"):
                if case != "missing":
                    bind_h5_physical_resource("h5:proof", guard)
                if case == "pending":
                    track_h5_physical_operation(pending)
                quarantine_h5_journeys("unknown")
        with pytest.raises((ValueError, RuntimeError)):
            await gate.resolve_quarantine(task_id="wrong-task", session_id="h5:proof")
        if case == "valid":
            proof = await gate.resolve_quarantine(task_id="task", session_id="h5:proof")
            assert proof[0]["browser_pid"] == guard.browser_pid
            async with gate.hold_journey(task_id="task", session_id="h5:proof"):
                pass
        else:
            with pytest.raises(RuntimeError):
                await gate.resolve_quarantine(task_id="task", session_id="h5:proof")
            with pytest.raises(RuntimeError):
                async with gate.hold_journey(task_id="task", session_id="h5:proof"):
                    pytest.fail("无证明不能解封")
    finally:
        finish.set()
        await pending


async def test_real_browser_stop_proof_waits_for_owned_process_exit():
    from playwright.async_api import Error, async_playwright

    from services.api.app.infrastructure.browser.h5_physical_stop import H5BrowserStopGuard

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        guard = await H5BrowserStopGuard.bind(browser, owned_local=True)
        try:
            assert guard.browser_pid > 0
            page = await browser.new_page()
            await page.set_content("<main>仅隔离浏览器</main>")
            pending = asyncio.create_task(page.evaluate("() => new Promise(() => {})"))
            await asyncio.sleep(0.03)
            proof = await guard.stop_and_prove()
            await asyncio.wait({pending}, timeout=1)
            assert pending.done()
            with pytest.raises(Error, match="closed"):
                await pending
            assert proof["method"] == "owned_browser_close_ack_and_pid_absent"
            assert not browser.is_connected()
        finally:
            await browser.close()


async def test_uncancelled_capture_keeps_physical_capacity_slot(monkeypatch):
    from services.api.app.application import h5_recapture_lease as leases
    from services.api.app.infrastructure.browser.page_capture import (
        PageCaptureMode,
        PageCaptureRequest,
    )

    monkeypatch.setattr(leases, "H5_CANCEL_DRAIN_TIMEOUT_SECONDS", 0.02)
    browser = PlaywrightPageCaptureBrowser(max_concurrent_captures=1)
    started, finish = asyncio.Event(), asyncio.Event()
    calls = []

    async def physical(request):
        calls.append(request.session_id)
        if request.session_id == "h5:unresponsive":
            started.set()
            try:
                await finish.wait()
            except asyncio.CancelledError:
                await finish.wait()

    monkeypatch.setattr(browser, "_capture_with_slot", physical)
    def request(session):
        return PageCaptureRequest(mode=PageCaptureMode.OBSERVE, session_id=session)
    with pytest.raises(RuntimeError, match="未确认"):
        await browser.capture_with_timeout(request("h5:unresponsive"), timeout_seconds=0.01)
    second = asyncio.create_task(
        browser.capture_with_timeout(request("h5:next"), timeout_seconds=1),
    )
    try:
        await asyncio.sleep(0.02)
        assert calls == ["h5:unresponsive"], "在途操作仍占用物理槽，不能偷释放并发容量"
    finally:
        finish.set()
        await asyncio.wait_for(second, 1)
    assert calls == ["h5:unresponsive", "h5:next"]


async def test_cancel_before_physical_child_starts_does_not_leak_slot(monkeypatch):
    from services.api.app.infrastructure.browser import page_capture as module

    browser = PlaywrightPageCaptureBrowser(max_concurrent_captures=1)
    calls = []

    async def physical(request):
        calls.append(request.session_id)

    async def never_started(operation, **_):
        task = asyncio.ensure_future(operation)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        raise asyncio.CancelledError

    monkeypatch.setattr(browser, "_capture_with_slot", physical)
    monkeypatch.setattr(module, "run_h5_physical_operation", never_started)
    with pytest.raises(asyncio.CancelledError):
        await browser.capture_with_timeout(
            module.PageCaptureRequest(
                mode=module.PageCaptureMode.OBSERVE, session_id="h5:not-started",
            ),
            timeout_seconds=1,
        )
    assert calls == []
    assert not browser._capture_semaphore.locked()


async def test_remote_or_shared_browser_cannot_be_bound_as_owned_stop_resource():
    from services.api.app.infrastructure.browser.h5_physical_stop import H5BrowserStopGuard

    browser = SimpleNamespace(close=AsyncMock())
    with pytest.raises(ValueError, match="独占|本地"):
        await H5BrowserStopGuard.bind(browser)
    browser.close.assert_not_awaited()


@pytest.mark.parametrize("responsive", [True, False])
async def test_stop_proof_requires_owned_driver_cleanup_ack(monkeypatch, responsive):
    from services.api.app.infrastructure.browser import h5_physical_stop as module

    monkeypatch.setattr(module, "H5_BROWSER_STOP_TIMEOUT_SECONDS", 0.03, raising=False)
    finish = asyncio.Event()

    async def driver_stop():
        if not responsive:
            await finish.wait()

    guard = module.H5BrowserStopGuard(
        browser=SimpleNamespace(close=AsyncMock(), is_connected=lambda: False),
        browser_pid=987654321, playwright=SimpleNamespace(stop=driver_stop),
    )
    try:
        if responsive:
            proof = await guard.stop_and_prove()
            assert proof["driver_stop_ack"] is True
        else:
            with pytest.raises(RuntimeError, match="未确认"):
                await guard.stop_and_prove()
            assert not guard._stop_task.done()
            finish.set()
            await guard._stop_task
            assert (await guard.stop_and_prove())["driver_stop_ack"] is True
    finally:
        finish.set()
        if guard._stop_task is not None:
            await guard._stop_task


async def test_caught_browser_timeout_cannot_resume_same_journey():
    from services.api.app.application.h5_recapture_lease import run_h5_physical_operation
    from services.api.app.application.h5_session_journey import bind_h5_physical_resource

    gate = H5SessionJourneyGate(delegate=InProcessSessionCommandGate())
    with pytest.raises(RuntimeError, match="未确认"):
        async with gate.hold_journey(task_id="task", session_id="h5:caught-timeout"):
            bind_h5_physical_resource(
                "h5:caught-timeout", SimpleNamespace(resource_id="owned-test"),
            )
            try:
                await run_h5_physical_operation(asyncio.sleep(1), timeout_seconds=0.01)
            except TimeoutError:
                pass
            async with gate.hold(task_id="task", session_id="h5:caught-timeout"):
                pytest.fail("吞掉页面超时不能恢复原浏览器旅程")


async def test_repeated_stop_observation_does_not_accumulate_pending_callbacks():
    from services.api.app.application.h5_recapture_lease import retain_h5_pending_operation

    finish = asyncio.Event()
    pending = asyncio.create_task(finish.wait())
    try:
        for _ in range(1000):
            retain_h5_pending_operation(pending)
        assert len(pending._callbacks) == 1
    finally:
        finish.set()
        await pending


async def test_frozen_deadline_does_not_claim_unproven_browser_stopped():
    from datetime import UTC, datetime, timedelta

    from services.api.app.application.h5_session_journey import (
        bind_h5_physical_resource,
        h5_session_journey,
    )

    gate = H5SessionJourneyGate(delegate=InProcessSessionCommandGate())
    with pytest.raises(TimeoutError) as raised:
        async with h5_session_journey(
            gate=gate, task_id="task", session_id="h5:deadline-proof",
            deadline=datetime.now(UTC) + timedelta(milliseconds=20),
        ):
            bind_h5_physical_resource(
                "h5:deadline-proof", SimpleNamespace(resource_id="owned-test"),
            )
            await asyncio.sleep(1)
    assert "操作已停止" not in str(raised.value)
    assert "h5:deadline-proof" in gate._quarantined


async def test_real_capture_runtime_capacity_and_proven_close_leave_no_owned_resources(
    monkeypatch, tmp_path,
):
    import json

    from services.api.app.infrastructure.browser.capture_runtime import WebCaptureRuntime
    from services.api.app.infrastructure.browser.page_capture import (
        PageCaptureMode,
        PageCaptureRequest,
    )

    adapter = PlaywrightPageCaptureBrowser(max_concurrent_captures=2)
    gate = H5SessionJourneyGate(delegate=InProcessSessionCommandGate())
    runtime = WebCaptureRuntime(
        coordinator=None, session_command_gate=gate, _browser=adapter,
        _executor=None, _evidence_buffer=None,
    )
    original = adapter._capture_with_slot
    active = peak = captures = 0
    proofs = []

    async def counted(request):
        nonlocal active, peak, captures
        active += 1
        peak = max(peak, active)
        try:
            result = await original(request)
            captures += 1
            return result
        finally:
            active -= 1

    monkeypatch.setattr(adapter, "_capture_with_slot", counted)

    async def actor(index):
        session = f"h5:real-capacity-{index}"
        try:
            for cycle in range(3):
                async with gate.hold_journey(task_id="capacity", session_id=session):
                    result = await adapter.capture_with_timeout(
                        PageCaptureRequest(
                            mode=PageCaptureMode.GOTO if cycle == 0 else PageCaptureMode.OBSERVE,
                            session_id=session,
                            url="data:text/html,<title>Owned</title><main>Controlled content</main>"
                            if cycle == 0 else None,
                            capture_screenshot=True, capture_dom=True, capture_visible_text=True,
                        ), timeout_seconds=15,
                    )
                    screenshot = next(
                        a for a in result.artifacts if a.artifact_type == "screenshot"
                    )
                    assert screenshot.content["frame_origin"]["status"] == "bracketed"
            resource = adapter._sessions[session].stop_guard
            await runtime.close_h5_exploration_session(task_id="capacity", session_id=session)
            proof = await resource.stop_and_prove()
            assert proof["driver_stop_ack"] is True
            proofs.append(proof)
        finally:
            if session in adapter._sessions:
                await adapter.close_session(session)

    await asyncio.gather(*(actor(index) for index in range(4)))
    assert peak <= 2 and captures == 12 and len(proofs) == 4
    assert not adapter._sessions and not adapter._session_locks
    assert not gate._locks and not gate._quarantined
    (tmp_path / "physical-capacity.json").write_text(
        json.dumps({"peak": peak, "captures": captures, "proofs": proofs}, indent=2),
        encoding="utf-8",
    )
