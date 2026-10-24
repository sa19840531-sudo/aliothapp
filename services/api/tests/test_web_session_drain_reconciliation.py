"""正式收口把排空阻塞保留为可恢复等待，不提前推进或丢弃活浏览器。"""

import logging

import pytest

from services.api.app.application.surface_sessions import SessionDrainPendingError
from services.api.app.application.web_formal_session_closure import DurableWebFormalSessionCloser
from services.api.app.application.web_inspection_reconciliation import ReconcileWebInspectionCommand
from services.api.tests.test_surface_session_close_readiness import ready_snapshot
from services.api.tests.test_web_inspection_reconciliation import (
    RUN_ID,
    TASK_ID,
    StateReader,
    _service,
    _state,
)


def pending_error(*, task_id=TASK_ID, session_id=f"web:{RUN_ID}"):
    return SessionDrainPendingError(
        task_id=task_id,
        readiness=ready_snapshot(
            session_id=session_id, ready=False, pending_manifests=2,
        ),
    )


async def test_reconciliation_waits_then_retries_same_live_runtime(caplog):
    events = []
    runtime_calls = 0

    class Runtime:
        async def close_session(self, *, task_id, session_id):
            nonlocal runtime_calls
            assert (task_id, session_id) == (TASK_ID, f"web:{RUN_ID}")
            runtime_calls += 1
            if runtime_calls == 1:
                events.append("session_drain_pending")
                raise pending_error()
            events.append("close_browser")

    def forbidden_fallback():
        raise AssertionError("排空等待不能丢弃持有真实浏览器的 Runtime")

    closer = DurableWebFormalSessionCloser(runtime_factory=forbidden_fallback)
    closer.bind_live_runtime(session_id=f"web:{RUN_ID}", runtime=Runtime())
    service, snapshots, executions, _ = _service(
        StateReader(_state(task_finalization_state="exploration_draining")), events=events,
    )
    service._session_closer = closer
    command = ReconcileWebInspectionCommand(
        task_id=TASK_ID, agent_run_id=RUN_ID, finalization_epoch=4,
    )
    with caplog.at_level(logging.INFO):
        waiting = await service.reconcile(command)
    assert waiting.status == "waiting_for_session_drain"
    assert events == [
        "finalize_manifests", "publish_pending", "drain_active_session", "session_drain_pending",
    ]
    assert snapshots.snapshots == []
    assert executions.commands == []
    assert TASK_ID in caplog.text and f"web:{RUN_ID}" in caplog.text
    assert '"pending_manifests": 2' in caplog.text

    events.clear()
    resumed = await service.reconcile(command)
    assert resumed.status == "waiting_for_run_terminal"
    assert runtime_calls == 2
    assert events == [
        "finalize_manifests", "publish_pending", "drain_active_session",
        "close_browser", "begin_session_draining",
    ]
    assert snapshots.snapshots == []
    assert executions.commands == []


@pytest.mark.parametrize("error", [
    RuntimeError("driver_close_failed"),
    RuntimeError("会话状态或版本已变化"),
    pending_error(task_id="other-task"),
    pending_error(session_id="web:other-run"),
])
async def test_reconciliation_does_not_hide_driver_version_or_identity_errors(error):
    events = []

    class Closer:
        async def close(self, **kwargs):
            raise error

    service, snapshots, executions, _ = _service(
        StateReader(_state(task_finalization_state="exploration_draining")), events=events,
    )
    service._session_closer = Closer()
    with pytest.raises(type(error)) as caught:
        await service.reconcile(ReconcileWebInspectionCommand(
            task_id=TASK_ID, agent_run_id=RUN_ID, finalization_epoch=4,
        ))
    assert caught.value is error
    assert events == ["finalize_manifests", "publish_pending", "drain_active_session"]
    assert snapshots.snapshots == []
    assert executions.commands == []
