"""H5 异常关闭不得留下可继续执行的会话和租约。"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from services.api.app.application.h5_runtime_composition import H5V2Runtime
from services.api.app.domain.interaction import SurfaceSessionStatus
from services.api.app.infrastructure.browser.capture_runtime import WebCaptureRuntime


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_point", ["seal", "physical_close", "release"])
async def test_h5_shutdown_failure_still_closes_driver_and_releases_lease(failure_point):
    snapshot = SimpleNamespace(
        task_id="task-h5", status=SurfaceSessionStatus.DRAINING, row_version=2,
    )
    sessions = SimpleNamespace(
        get=AsyncMock(return_value=snapshot),
        seal_exploration_for_close=AsyncMock(),
        mark_closed=AsyncMock(),
        mark_lost=AsyncMock(),
    )
    sessions.seal_exploration_for_close.return_value = SimpleNamespace(
        task_id="task-h5", status=SurfaceSessionStatus.CLOSING, row_version=3
    )
    error = RuntimeError(f"{failure_point} failed")
    browser = SimpleNamespace(close_session=AsyncMock())
    gate = SimpleNamespace(release_session=AsyncMock())
    if failure_point == "seal":
        sessions.seal_exploration_for_close.side_effect = error
    elif failure_point == "physical_close":
        browser.close_session.side_effect = error
    else:
        gate.release_session.side_effect = [error, None]
    runtime = WebCaptureRuntime(
        coordinator=object(), session_command_gate=gate, _browser=browser,
        _executor=object(), _evidence_buffer=object(), surface_sessions=sessions,
    )
    with pytest.raises(RuntimeError, match=f"{failure_point} failed"):
        await runtime.close_h5_exploration_session(task_id="task-h5", session_id="h5:run")
    browser.close_session.assert_awaited()
    gate.release_session.assert_awaited_with(task_id="task-h5", session_id="h5:run")
    if failure_point != "release":
        sessions.mark_lost.assert_awaited()


@pytest.mark.asyncio
async def test_one_recovery_session_failure_does_not_abandon_other_h5_sessions():
    close = AsyncMock(side_effect=[RuntimeError("recovery seal failed"), None])
    runtime = H5V2Runtime(
        capture_runtime=SimpleNamespace(surface_sessions=None, close_h5_exploration_session=close),
        workflow=object(), turn_runner=object(), runtime_settings=object(),
        session_lineage=SimpleNamespace(session_ids=("h5:main", "h5:recovery")),
    )
    with pytest.raises(RuntimeError, match="recovery seal failed"):
        await runtime.close(task_id="task-h5", session_id="h5:main")
    assert [call.kwargs["session_id"] for call in close.await_args_list] == [
        "h5:recovery", "h5:main",
    ]


@pytest.mark.asyncio
async def test_cancel_during_shutdown_still_releases_physical_session_and_lease():
    entered = asyncio.Event()

    async def wait_for_seal(**kwargs):
        entered.set()
        await asyncio.Event().wait()

    sessions = SimpleNamespace(
        get=AsyncMock(return_value=SimpleNamespace(
            task_id="task-h5", status=SurfaceSessionStatus.DRAINING, row_version=2,
        )),
        seal_exploration_for_close=wait_for_seal,
        mark_lost=AsyncMock(),
    )
    browser = SimpleNamespace(close_session=AsyncMock())
    gate = SimpleNamespace(release_session=AsyncMock())
    runtime = WebCaptureRuntime(
        coordinator=object(), session_command_gate=gate, _browser=browser,
        _executor=object(), _evidence_buffer=object(), surface_sessions=sessions,
    )
    task = asyncio.create_task(runtime.close_h5_exploration_session(
        task_id="task-h5", session_id="h5:run",
    ))
    await asyncio.wait_for(entered.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    browser.close_session.assert_awaited_once_with("h5:run")
    sessions.mark_lost.assert_awaited_once()
    gate.release_session.assert_awaited_once_with(task_id="task-h5", session_id="h5:run")


@pytest.mark.asyncio
async def test_h5_emergency_cleanup_does_not_touch_foreign_session():
    sessions = SimpleNamespace(get=AsyncMock(return_value=SimpleNamespace(task_id="other-task")))
    browser = SimpleNamespace(close_session=AsyncMock())
    gate = SimpleNamespace(release_session=AsyncMock())
    runtime = WebCaptureRuntime(
        coordinator=object(), session_command_gate=gate, _browser=browser,
        _executor=object(), _evidence_buffer=object(), surface_sessions=sessions,
    )
    with pytest.raises(RuntimeError, match="不属于"):
        await runtime.close_h5_exploration_session(task_id="task-h5", session_id="h5:other")
    browser.close_session.assert_not_awaited()
    gate.release_session.assert_not_awaited()
