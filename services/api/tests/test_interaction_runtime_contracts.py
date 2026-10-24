"""InteractionResult 动作事实与效果验证正交合同。"""

from services.api.app.domain.interaction import (
    InteractionExecutionOutcome,
    InteractionResult,
    InteractionResultStatus,
    InteractionVerificationOutcome,
)


def test_failed_action_can_attach_after_capture_without_becoming_success() -> None:
    failed = InteractionResult(
        task_id="task-1",
        session_id="session-1",
        intent_id="intent-1",
        action_execution_id="action-1",
        source_capture_id="capture-before",
        status=InteractionResultStatus.FAILED,
        error_code="driver_failed",
    )

    failed_with_after = failed.attach_capture_preserving_execution("capture-after")

    assert failed_with_after.status is InteractionResultStatus.FAILED
    assert failed_with_after.result_capture_id == "capture-after"
    assert failed_with_after.execution_outcome is InteractionExecutionOutcome.FAILED
    assert (
        failed_with_after.verification_outcome
        is InteractionVerificationOutcome.UNVERIFIED
    )


def test_pending_action_has_succeeded_execution_and_pending_verification() -> None:
    pending = InteractionResult(
        task_id="task-1",
        session_id="session-1",
        intent_id="intent-1",
        action_execution_id="action-1",
        source_capture_id="capture-before",
        status=InteractionResultStatus.EVIDENCE_PENDING,
    )

    assert pending.execution_outcome is InteractionExecutionOutcome.SUCCEEDED
    assert pending.verification_outcome is InteractionVerificationOutcome.PENDING


def test_legacy_v1_result_infers_missing_truth_fields() -> None:
    restored = InteractionResult.from_dict(
        {
            "protocol_version": "interaction-result.v1",
            "task_id": "task-1",
            "session_id": "session-1",
            "intent_id": "intent-1",
            "action_execution_id": "action-1",
            "source_capture_id": "capture-before",
            "status": "blocked",
            "error_code": "policy_rejected",
        }
    )

    assert restored.execution_outcome is InteractionExecutionOutcome.REJECTED
    assert restored.verification_outcome is InteractionVerificationOutcome.UNVERIFIED
    assert restored.to_dict()["execution_outcome"] == "rejected"
    assert restored.to_dict()["verification_outcome"] == "unverified"


def test_legacy_success_does_not_invent_effect_confirmation() -> None:
    restored = InteractionResult.from_dict(
        {
            "protocol_version": "interaction-result.v1",
            "task_id": "task-1",
            "session_id": "session-1",
            "intent_id": "intent-1",
            "action_execution_id": "action-1",
            "source_capture_id": "capture-before",
            "status": "succeeded",
            "result_capture_id": "capture-after",
        }
    )

    assert restored.execution_outcome is InteractionExecutionOutcome.SUCCEEDED
    assert restored.verification_outcome is InteractionVerificationOutcome.UNVERIFIED
