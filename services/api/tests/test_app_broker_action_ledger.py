"""正式恢复的 Broker 拒绝和 Driver 失败须原样保留到动作账本。"""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from services.api.app.application.app_restoration_executor import AppRestorationInteractionExecutor
from services.api.app.application.capture_coordinator import CaptureCoordinator
from services.api.app.application.interaction_command_validation import InteractionCommandValidator
from services.api.app.domain.interaction import (
    ActionExecutionStatus,
    InteractionOperation,
    RawActionResult,
    SurfaceDriverError,
    SurfaceSessionRef,
)
from services.api.tests.test_capture_coordinator import (
    RecordingActionExecutionRepository,
    RecordingCaptureWriter,
    RecordingEvidenceExitGate,
    _app_recovery_command,
)


@pytest.mark.parametrize(
    ("failure", "error_code"),
    [
        ("unsupported", "EXECUTION_CONTEXT_UNSUPPORTED"),
        ("returned", "DRIVER_REJECTED"),
        ("surface", "DRIVER_TIMEOUT"),
        ("unknown", "APP_DRIVER_FAILED"),
    ],
)
async def test_broker_failure_receipt_survives_coordinator_ledger(failure, error_code):
    calls = []

    class BareDriver:
        async def execute(self, **kwargs):
            calls.append("bare")
            raise AssertionError("正式恢复不得调用裸 Driver")

    class ContextDriver(BareDriver):
        async def execute_with_context(self, *, action_execution_id, **kwargs):
            calls.append("context")
            if failure == "surface":
                raise SurfaceDriverError("内部错误", error_code="DRIVER_TIMEOUT", retryable=True)
            if failure == "unknown":
                raise RuntimeError("内部错误")
            return RawActionResult(
                action_execution_id=action_execution_id,
                succeeded=False,
                error_code="DRIVER_REJECTED",
                retryable=False,
            )

    command = _app_recovery_command()
    command = replace(
        command,
        intent=replace(
            command.intent,
            action_type=InteractionOperation.WARM_RESTORE,
            deadline=datetime.now(UTC) + timedelta(minutes=1),
        ),
    )
    intent = command.intent
    events = []
    actions = RecordingActionExecutionRepository(events)
    coordinator = CaptureCoordinator(
        capture_writer=RecordingCaptureWriter(events),
        interaction_executor=AppRestorationInteractionExecutor(
            driver=BareDriver() if failure == "unsupported" else ContextDriver(),
            surface_sessions=SimpleNamespace(),
            package_name="com.example",
            session=SurfaceSessionRef(
                task_id=intent.task_id,
                session_id=intent.session_id,
                surface_kind=intent.surface_kind,
                primary_surface_ref="app:session-1",
            ),
            command_validator=InteractionCommandValidator(),
        ),
        evidence_exit_gate=RecordingEvidenceExitGate(),
        action_execution_repository=actions,
    )

    result = await coordinator.coordinate(command)

    assert calls == ([] if failure == "unsupported" else ["context"])
    assert result.interaction.error_code == error_code
    assert actions.state.status is ActionExecutionStatus.FAILED
    assert actions.state.error_code == error_code
    receipt = actions.state.result_payload["state_summary"]["app_command_broker_receipt"]
    assert receipt == result.interaction.state_summary["app_command_broker_receipt"]
    assert receipt["action_execution_id"] == actions.state.action_execution_id
    assert receipt["result_summary"]["driver_invoked"] is (failure != "unsupported")
    assert receipt["result_summary"]["error_code"] == error_code
    assert "action:terminal_evidence" in events
