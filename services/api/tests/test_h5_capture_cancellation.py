"""H5 补采截止时，动作账本不能遗留非终态。"""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime

import pytest

from services.api.app.application.capture_coordinator import CaptureCoordinator, CapturePhase
from services.api.app.domain.interaction import ActionExecutionStatus
from services.api.tests.test_capture_coordinator import (
    RecordingActionExecutionRepository,
    RecordingCaptureWriter,
    RecordingEvidenceExitGate,
    RecordingInteractionExecutor,
    _h5_recovery_command,
)


class CancellationLedger(RecordingActionExecutionRepository):
    async def fail_before_action(self, **kwargs):
        assert kwargs["expected_row_version"] == self.state.row_version
        self.state = replace(
            self.state,
            status=ActionExecutionStatus.FAILED,
            error_code=kwargs["error_code"],
            row_version=self.state.row_version + 1,
            action_finished_at=datetime.now(UTC),
        )
        return self.state

    async def fail_evidence(self, **kwargs):
        interaction = kwargs["interaction"]
        assert kwargs["expected_row_version"] == self.state.row_version
        self.state = replace(
            self.state,
            status=ActionExecutionStatus.EVIDENCE_FAILED,
            result_payload=interaction.to_dict(),
            error_code=interaction.error_code,
            row_version=self.state.row_version + 1,
            evidence_finished_at=datetime.now(UTC),
        )
        return self.state


@pytest.mark.parametrize("phase", ["before", "action", "after"])
@pytest.mark.parametrize("session_prefix", ["h5", "web-v2"])
async def test_h5_cancellation_settles_only_its_own_action(phase, session_prefix):
    events = []
    ledger = CancellationLedger(events)

    class Writer(RecordingCaptureWriter):
        async def capture(self, request):
            if (phase == "before" and request.phase is CapturePhase.BEFORE_ACTION) or (
                phase == "after" and request.phase is CapturePhase.AFTER_ACTION
            ):
                raise asyncio.CancelledError
            return await super().capture(request)

    class Executor(RecordingInteractionExecutor):
        async def execute(self, request):
            if phase == "action":
                raise asyncio.CancelledError
            return await super().execute(request)

    command = _h5_recovery_command()
    command = replace(
        command,
        intent=replace(
            command.intent,
            session_id=f"{session_prefix}:run-1",
        ),
    )
    coordinator = CaptureCoordinator(
        capture_writer=Writer(events),
        interaction_executor=Executor(events),
        action_execution_repository=ledger,
        evidence_exit_gate=RecordingEvidenceExitGate(),
    )

    with pytest.raises(asyncio.CancelledError):
        await coordinator.coordinate(command)

    if session_prefix == "h5":
        expected = (
            ActionExecutionStatus.EVIDENCE_FAILED
            if phase == "after"
            else ActionExecutionStatus.FAILED
        )
        assert ledger.state.status is expected
        assert ledger.state.error_code.startswith("H5_ACTION_")
        assert ledger.state.result_capture_id is None
    else:
        expected = {
            "before": ActionExecutionStatus.PLANNED,
            "action": ActionExecutionStatus.EXECUTING,
            "after": ActionExecutionStatus.EVIDENCE_PENDING,
        }[phase]
        assert ledger.state.status is expected
    assert len([event for event in events if event == "execute"]) <= 1


@pytest.mark.parametrize("cleanup_fails", [False, True])
async def test_h5_deadline_cancels_capture_and_preserves_timeout(cleanup_fails, caplog):
    events = []

    class Ledger(CancellationLedger):
        async def fail_evidence(self, **kwargs):
            if cleanup_fails:
                raise RuntimeError("Session 租约围栏已变化")
            return await super().fail_evidence(**kwargs)

    ledger = Ledger(events)
    capture_exited = asyncio.Event()

    class Writer(RecordingCaptureWriter):
        async def capture(self, request):
            if request.phase is CapturePhase.AFTER_ACTION:
                try:
                    await asyncio.Event().wait()
                finally:
                    capture_exited.set()
            return await super().capture(request)

    gate = RecordingEvidenceExitGate()
    coordinator = CaptureCoordinator(
        capture_writer=Writer(events),
        interaction_executor=RecordingInteractionExecutor(events),
        action_execution_repository=ledger,
        evidence_exit_gate=gate,
    )
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.02):
            await coordinator.coordinate(_h5_recovery_command())
    assert capture_exited.is_set()
    assert events.count("execute") == 1
    assert gate.finalizations
    if cleanup_fails:
        assert ledger.state.status is ActionExecutionStatus.EVIDENCE_PENDING
        assert "H5 取消动作账本收尾失败" in caplog.text
    else:
        assert ledger.state.status is ActionExecutionStatus.EVIDENCE_FAILED


@pytest.mark.parametrize("session_prefix", ["h5", "web-v2"])
async def test_before_capture_timeout_then_probe_deadline_does_not_orphan_action(session_prefix):
    """真实故障顺序：前采集超时，补偿写账本时又被 Probe 截止取消。"""
    events = []
    settling = asyncio.Event()
    release = asyncio.Event()

    class Ledger(CancellationLedger):
        async def fail_before_action(self, **kwargs):
            settling.set()
            await release.wait()
            return await super().fail_before_action(**kwargs)

    class Writer(RecordingCaptureWriter):
        async def capture(self, request):
            raise TimeoutError("动作前采集超过绝对截止")

    ledger = Ledger(events)
    command = _h5_recovery_command()
    command = replace(command, intent=replace(command.intent, session_id=f"{session_prefix}:run-1"))
    coordinator = CaptureCoordinator(
        capture_writer=Writer(events), interaction_executor=RecordingInteractionExecutor(events),
        action_execution_repository=ledger, evidence_exit_gate=RecordingEvidenceExitGate(),
    )
    running = asyncio.create_task(coordinator.coordinate(command))
    await settling.wait()
    running.cancel()
    await asyncio.sleep(0)
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await running
    assert events.count("execute") == 0
    assert ledger.state.result_capture_id is None
    assert ledger.state.status is (
        ActionExecutionStatus.FAILED if session_prefix == "h5" else ActionExecutionStatus.PLANNED
    )


async def test_h5_plan_commit_canceled_before_return_is_settled_without_execution():
    events = []

    class Ledger(CancellationLedger):
        async def plan(self, **kwargs):
            await super().plan(**kwargs)
            raise asyncio.CancelledError

    ledger = Ledger(events)
    coordinator = CaptureCoordinator(
        capture_writer=RecordingCaptureWriter(events),
        interaction_executor=RecordingInteractionExecutor(events),
        action_execution_repository=ledger, evidence_exit_gate=RecordingEvidenceExitGate(),
    )
    with pytest.raises(asyncio.CancelledError):
        await coordinator.coordinate(_h5_recovery_command())
    assert ledger.state.status is ActionExecutionStatus.FAILED
    assert events.count("execute") == 0


async def test_h5_before_capture_settlement_fence_rejection_is_visible(caplog):
    events = []

    class Ledger(CancellationLedger):
        async def fail_before_action(self, **kwargs):
            raise RuntimeError("租约已转交，不能强制清账")

    class Writer(RecordingCaptureWriter):
        async def capture(self, request):
            raise TimeoutError("原始采集超时")

    ledger = Ledger(events)
    coordinator = CaptureCoordinator(
        capture_writer=Writer(events), interaction_executor=RecordingInteractionExecutor(events),
        action_execution_repository=ledger, evidence_exit_gate=RecordingEvidenceExitGate(),
    )
    with pytest.raises(Exception) as error:
        await coordinator.coordinate(_h5_recovery_command())
    assert error.value.cause_type == "TimeoutError"
    assert ledger.state.status is ActionExecutionStatus.PLANNED
    assert "账本收尾失败" in caplog.text
