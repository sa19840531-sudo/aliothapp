"""吞取消与正常退出分开验证，不以清理超时冒充物理排空。"""

import asyncio
from types import SimpleNamespace

import pytest

from services.api.app.application import h5_recapture_lease as leases
from services.api.app.application.capture_coordinator import InProcessSessionCommandGate
from services.api.app.application.h5_session_journey import H5SessionJourneyGate


@pytest.mark.parametrize("ignore_cancel", [True, False])
async def test_unresponsive_operation_returns_explicit_pending_and_quarantines_session(
    monkeypatch, ignore_cancel,
):
    monkeypatch.setattr(leases, "H5_CANCEL_DRAIN_TIMEOUT_SECONDS", 0.03, raising=False)
    gate = H5SessionJourneyGate(delegate=InProcessSessionCommandGate())
    started, swallowed, finish = (asyncio.Event() for _ in range(3))

    async def physical():
        started.set()
        try:
            await finish.wait()
        except asyncio.CancelledError:
            if not ignore_cancel:
                raise
            swallowed.set()
            await finish.wait()

    async def owner():
        guard = leases.H5BoundedLeaseGuard(
            queue=SimpleNamespace(), command=SimpleNamespace(lease_seconds=10),
            renew_interval_seconds=1, lost_error_type=RuntimeError,
            lost_message="失效", task_name="unresponsive-test",
        )
        async with gate.hold_journey(task_id="task", session_id="h5:pending"):
            await guard.run(physical())

    running = asyncio.create_task(owner())
    await started.wait()
    running.cancel()
    try:
        done, _ = await asyncio.wait({running}, timeout=0.2)
        assert done, "吞取消不能让上层无限卡在异步清理"
        if ignore_cancel:
            assert swallowed.is_set()
            with pytest.raises(RuntimeError) as raised:
                await running
            assert raised.value.error_code == "H5_PHYSICAL_OPERATION_NOT_DRAINED"
            with pytest.raises(RuntimeError, match="未排空|未确认"):
                async with gate.hold(task_id="task", session_id="h5:pending"):
                    pytest.fail("未排空会话不能取得后续动作")
        else:
            with pytest.raises(asyncio.CancelledError):
                await running
            async with gate.hold(task_id="task", session_id="h5:pending"):
                pass
    finally:
        finish.set()
        with pytest.raises((asyncio.CancelledError, RuntimeError)):
            await running
        await asyncio.sleep(0)


@pytest.mark.parametrize("kind", ["fallback", "playwright_slot"])
async def test_browser_timeout_owns_unresponsive_inner_operation(monkeypatch, kind):
    from services.api.app.infrastructure.browser.page_capture import (
        PageCaptureMode,
        PageCaptureRequest,
        PlaywrightPageCaptureBrowser,
        capture_page_with_timeout,
    )

    monkeypatch.setattr(leases, "H5_CANCEL_DRAIN_TIMEOUT_SECONDS", 0.03, raising=False)
    started, swallowed, finish = (asyncio.Event() for _ in range(3))

    async def operation(*_):
        started.set()
        try:
            await finish.wait()
        except asyncio.CancelledError:
            swallowed.set()
            await finish.wait()

    browser = SimpleNamespace(capture=operation)
    if kind == "playwright_slot":
        browser = PlaywrightPageCaptureBrowser()
        monkeypatch.setattr(browser, "_capture_with_slot", operation)
    running = asyncio.create_task(capture_page_with_timeout(
        request=PageCaptureRequest(mode=PageCaptureMode.OBSERVE, session_id="h5:physical"),
        browser=browser, timeout_seconds=0.01,
    ))
    try:
        await started.wait()
        done, _ = await asyncio.wait({running}, timeout=0.2)
        assert done, "页面级wait_for吞取消时也必须有界返回未排空"
        assert swallowed.is_set()
        with pytest.raises(RuntimeError) as raised:
            await running
        assert raised.value.error_code == "H5_PHYSICAL_OPERATION_NOT_DRAINED"
    finally:
        finish.set()
        try:
            await running
        except (RuntimeError, TimeoutError):
            pass


@pytest.mark.parametrize("pending", [True, False])
async def test_persistence_checks_journey_before_writing_artifacts(pending):
    from services.api.app.application.h5_session_journey import quarantine_h5_journeys
    from services.api.app.application.page_capture_persistence import PageCapturePersistenceService
    from services.api.tests.test_page_capture_persistence import (
        FakeEvidenceArtifactStore,
        FakePageCaptureRepository,
        _capture_result,
    )

    repository, store = FakePageCaptureRepository(), FakeEvidenceArtifactStore()
    persistence = PageCapturePersistenceService(repository=repository, artifact_store=store)
    gate = H5SessionJourneyGate(delegate=InProcessSessionCommandGate())
    async def execute():
        async with gate.hold_journey(task_id="task", session_id="h5:write"):
            if pending:
                quarantine_h5_journeys("H5_PHYSICAL_OPERATION_NOT_DRAINED")
            await persistence.persist(task_id="task", agent_run_id="run", result=_capture_result(),
                surface_session_id="h5:write", session_command_sequence=1, finalization_epoch=0)

    if pending:
        with pytest.raises(RuntimeError, match="未确认"):
            await execute()
        assert repository.calls == [] and store.calls == []
    else:
        await execute()
        assert len(repository.calls) == 1 and store.calls


async def test_expired_browser_window_never_starts_a_physical_operation():
    calls = []

    async def physical():
        calls.append("browser-or-model")

    with pytest.raises(TimeoutError):
        await leases.run_h5_physical_operation(physical(), timeout_seconds=0)
    assert calls == []
