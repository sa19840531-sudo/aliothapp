"""Web 密封候选动作前过期的严格恢复边界。"""

from __future__ import annotations

from typing import Protocol

from services.api.app.application.capture_coordination import (
    CaptureCoordinationError,
    CaptureCoordinationStage,
)
from services.api.app.domain.interaction import (
    ActionIntent,
    CaptureKind,
    CaptureRef,
    InteractionOperation,
    InteractionResult,
    InteractionResultStatus,
    SurfaceKind,
    build_action_execution_id,
)

_STALE_ERROR_CODE = "WEB_SEALED_CANDIDATE_STALE"
_RECOVERABLE_CAPABILITIES = frozenset(
    {"web_age_gate_confirm", "web_obstruction_dismiss"}
)
_RETRYABLE_TIMEOUT_ERROR_CODE = "ACTION_TIMED_OUT"


class WebRecoveryEvidence(Protocol):
    """动作前复检后可继续读取的 Web 证据身份。"""

    task_id: str
    agent_run_id: str
    session_id: str
    capture_id: str


def validated_web_stale_before_capture(
    error: CaptureCoordinationError,
    *,
    intent: ActionIntent,
) -> CaptureRef | None:
    """仅返回能证明点击尚未发生的同链路动作前 Capture。"""

    if (
        error.stage is not CaptureCoordinationStage.ACTION
        or error.error_code != _STALE_ERROR_CODE
        or error.after_capture is not None
        or intent.surface_kind is not SurfaceKind.WEB
        or intent.action_type is not InteractionOperation.CLICK
        or intent.parameters.get("target_mode") != "web_sealed_candidate"
        or intent.parameters.get("web_sealed_source_capture_id")
        != intent.source_capture_id
        or intent.parameters.get("web_sealed_candidate_capability")
        not in _RECOVERABLE_CAPABILITIES
    ):
        return None

    before_capture = error.before_capture
    interaction = error.interaction
    if not isinstance(before_capture, CaptureRef) or not isinstance(
        interaction, InteractionResult
    ):
        return None

    action_execution_id = build_action_execution_id(intent)
    if (
        before_capture.task_id != intent.task_id
        or before_capture.session_id != intent.session_id
        or before_capture.surface_kind is not SurfaceKind.WEB
        or before_capture.capture_kind is not CaptureKind.PAGE
        or before_capture.role != "before_action"
        or before_capture.parent_capture_id != intent.source_capture_id
        or before_capture.action_execution_id != action_execution_id
        or interaction.task_id != intent.task_id
        or interaction.session_id != intent.session_id
        or interaction.intent_id != intent.intent_id
        or interaction.action_execution_id != action_execution_id
        or interaction.source_capture_id != before_capture.capture_id
        or interaction.status is not InteractionResultStatus.BLOCKED
        or interaction.error_code != _STALE_ERROR_CODE
        or interaction.result_capture_id is not None
    ):
        return None
    return before_capture


def validated_web_retryable_timeout_before_capture(
    error: CaptureCoordinationError,
    *,
    intent: ActionIntent,
) -> CaptureRef | None:
    """严格验证结果未知的 Web 点击超时，只授权后续只读复检。"""

    if (
        error.stage is not CaptureCoordinationStage.ACTION
        or error.error_code != _RETRYABLE_TIMEOUT_ERROR_CODE
        or not error.retryable
        or error.after_capture is not None
        or intent.surface_kind is not SurfaceKind.WEB
        or intent.action_type is not InteractionOperation.CLICK
        or intent.parameters.get("target_mode") != "web_sealed_candidate"
        or intent.parameters.get("web_sealed_source_capture_id")
        != intent.source_capture_id
        or intent.parameters.get("web_sealed_candidate_capability")
        not in _RECOVERABLE_CAPABILITIES
    ):
        return None

    before_capture = error.before_capture
    interaction = error.interaction
    if not isinstance(before_capture, CaptureRef) or not isinstance(
        interaction, InteractionResult
    ):
        return None

    action_execution_id = build_action_execution_id(intent)
    if (
        before_capture.task_id != intent.task_id
        or before_capture.session_id != intent.session_id
        or before_capture.surface_kind is not SurfaceKind.WEB
        or before_capture.capture_kind is not CaptureKind.PAGE
        or before_capture.role != "before_action"
        or before_capture.parent_capture_id != intent.source_capture_id
        or before_capture.action_execution_id != action_execution_id
        or interaction.task_id != intent.task_id
        or interaction.session_id != intent.session_id
        or interaction.intent_id != intent.intent_id
        or interaction.action_execution_id != action_execution_id
        or interaction.source_capture_id != before_capture.capture_id
        or interaction.status is not InteractionResultStatus.TIMED_OUT
        or interaction.error_code != _RETRYABLE_TIMEOUT_ERROR_CODE
        or not interaction.retryable
        or interaction.result_capture_id is not None
    ):
        return None
    return before_capture


def web_recovery_evidence_matches(
    fresh: WebRecoveryEvidence,
    *,
    original: WebRecoveryEvidence,
    expected_capture_id: str,
) -> bool:
    """确认读取结果仍属于原任务、Run、会话和指定动作前 Capture。"""

    return (
        fresh.task_id == original.task_id
        and fresh.agent_run_id == original.agent_run_id
        and fresh.session_id == original.session_id
        and fresh.capture_id == expected_capture_id
    )


__all__ = [
    "validated_web_retryable_timeout_before_capture",
    "validated_web_stale_before_capture",
    "web_recovery_evidence_matches",
]
