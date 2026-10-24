"""正式 App 动作取消时的账本收口与禁止重放测试。"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping

import pytest

from services.api.app.application.app_exploration_cancellation import (
    AppExplorationCanceledError,
    run_app_exploration_operation,
)
from services.api.app.application.capture_coordinator import (
    AppActionRecoveryBlockedError,
    CaptureCoordinator,
)
from services.api.app.domain.interaction import ActionExecutionStatus
from services.api.tests.test_capture_coordinator import (
    RecordingActionExecutionRepository,
    RecordingCaptureWriter,
    RecordingEvidenceExitGate,
    _app_recovery_command,
)


class _BlockingInteractionExecutor:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.requests = []

    async def execute(self, request):
        self.requests.append(request)
        self.started.set()
        await self.release.wait()


class _LeaseLostError(RuntimeError):
    pass


class _DatabaseWriteError(RuntimeError):
    pass


class _FailingCancellationLedger(RecordingActionExecutionRepository):
    def __init__(self, events, error: Exception) -> None:
        super().__init__(events)
        self.error = error
        self.record_attempts = 0

    async def record_interaction(self, **kwargs):
        del kwargs
        self.record_attempts += 1
        raise self.error


class _BlockingCancellationLedger(RecordingActionExecutionRepository):
    def __init__(self, events) -> None:
        super().__init__(events)
        self.compensation_started = asyncio.Event()
        self.compensation_cancelled = asyncio.Event()

    async def record_interaction(self, **kwargs):
        del kwargs
        self.compensation_started.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            self.compensation_cancelled.set()
            raise


class _ToggleCancellation:
    def __init__(self, *, requested: bool = False) -> None:
        self.requested = requested

    async def is_cancel_requested(self, *, task_id: str, agent_run_id: str) -> bool:
        del task_id, agent_run_id
        return self.requested


def _coordinator(*, executor, actions, writer):
    return CaptureCoordinator(
        capture_writer=writer,
        interaction_executor=executor,
        evidence_exit_gate=RecordingEvidenceExitGate(),
        action_execution_repository=actions,
    )


@pytest.mark.asyncio
async def test_formal_app_cancel_during_driver_call_closes_unknown_ledger_without_after() -> None:
    events = []
    actions = RecordingActionExecutionRepository(events)
    writer = RecordingCaptureWriter(events)
    executor = _BlockingInteractionExecutor()
    coordinator = _coordinator(executor=executor, actions=actions, writer=writer)

    running = asyncio.create_task(coordinator.coordinate(_app_recovery_command()))
    await asyncio.wait_for(executor.started.wait(), timeout=2)
    running.cancel()

    with pytest.raises(asyncio.CancelledError):
        await running

    assert actions.state is not None
    assert actions.state.status is not ActionExecutionStatus.EXECUTING
    assert actions.state.error_code == "APP_ACTION_OUTCOME_UNKNOWN_DURING_CANCELLATION"
    assert actions.state.result_capture_id is None
    payload = dict(actions.state.result_payload)
    state_summary = payload.get("state_summary")
    assert isinstance(state_summary, Mapping)
    assert state_summary["after_capture_status"] == "not_collected"
    assert state_summary["after_capture_reason"] == "workflow_canceled"
    assert len(executor.requests) == 1
    assert len(writer.requests) == 1


@pytest.mark.asyncio
async def test_cancelled_formal_app_action_cannot_replay_or_capture_on_retry() -> None:
    events = []
    actions = RecordingActionExecutionRepository(events)
    writer = RecordingCaptureWriter(events)
    executor = _BlockingInteractionExecutor()
    coordinator = _coordinator(executor=executor, actions=actions, writer=writer)
    running = asyncio.create_task(coordinator.coordinate(_app_recovery_command()))
    await asyncio.wait_for(executor.started.wait(), timeout=2)
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running

    with pytest.raises(AppActionRecoveryBlockedError):
        await coordinator.coordinate(_app_recovery_command())

    assert len(executor.requests) == 1
    assert len(writer.requests) == 1


@pytest.mark.parametrize(
    ("error_type", "error_message"),
    [
        (_LeaseLostError, "secret lease token must not be logged"),
        (_DatabaseWriteError, "secret database details must not be logged"),
    ],
)
@pytest.mark.asyncio
async def test_cancel_compensation_error_is_logged_without_swallowing_cancellation(
    caplog: pytest.LogCaptureFixture,
    error_type: type[Exception],
    error_message: str,
) -> None:
    events = []
    actions = _FailingCancellationLedger(events, error_type(error_message))
    writer = RecordingCaptureWriter(events)
    executor = _BlockingInteractionExecutor()
    coordinator = _coordinator(executor=executor, actions=actions, writer=writer)
    running = asyncio.create_task(coordinator.coordinate(_app_recovery_command()))
    await asyncio.wait_for(executor.started.wait(), timeout=2)
    action_id = actions.state.action_execution_id
    running.cancel()

    with caplog.at_level(
        logging.ERROR,
        logger="services.api.app.application.capture_coordinator",
    ):
        with pytest.raises(asyncio.CancelledError):
            await running

    assert actions.record_attempts == 1
    messages = [
        record.getMessage()
        for record in caplog.records
        if record.name == "services.api.app.application.capture_coordinator"
    ]
    assert any(action_id in message and error_type.__name__ in message for message in messages)
    assert all(error_message not in message for message in messages)
    assert len(executor.requests) == 1
    assert len(writer.requests) == 1

    with pytest.raises(AppActionRecoveryBlockedError):
        await coordinator.coordinate(_app_recovery_command())
    assert len(executor.requests) == 1
    assert len(writer.requests) == 1


@pytest.mark.asyncio
async def test_second_cancel_interrupts_inline_compensation_without_detached_work() -> None:
    events = []
    actions = _BlockingCancellationLedger(events)
    writer = RecordingCaptureWriter(events)
    executor = _BlockingInteractionExecutor()
    coordinator = _coordinator(executor=executor, actions=actions, writer=writer)
    running = asyncio.create_task(coordinator.coordinate(_app_recovery_command()))
    await asyncio.wait_for(executor.started.wait(), timeout=2)
    running.cancel()
    await asyncio.wait_for(actions.compensation_started.wait(), timeout=2)

    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running

    assert actions.compensation_cancelled.is_set()
    assert len(executor.requests) == 1
    assert len(writer.requests) == 1
    assert not any(
        not task.done() and "compensation" in task.get_name().casefold()
        for task in asyncio.all_tasks()
    )


@pytest.mark.asyncio
async def test_cancel_before_formal_app_dispatch_has_no_action_or_capture_side_effect() -> None:
    events = []
    actions = RecordingActionExecutionRepository(events)
    writer = RecordingCaptureWriter(events)
    executor = _BlockingInteractionExecutor()
    coordinator = _coordinator(executor=executor, actions=actions, writer=writer)
    cancellation = _ToggleCancellation(requested=True)

    with pytest.raises(AppExplorationCanceledError):
        await run_app_exploration_operation(
            coordinator.coordinate(_app_recovery_command()),
            cancellation=cancellation,
            task_id="task-1",
            agent_run_id="run-1",
            poll_interval_seconds=0.001,
        )

    assert actions.state is None
    assert executor.requests == []
    assert writer.requests == []
