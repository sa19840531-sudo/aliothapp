"""Web 密封候选过期测试的真实协调失败夹具。"""

from __future__ import annotations

from dataclasses import replace

from services.api.app.application.capture_coordination import (
    CaptureCoordinationError,
    CaptureCoordinationStage,
)
from services.api.app.domain.interaction import (
    ActionIntent,
    CaptureKind,
    CaptureRef,
    InteractionResult,
    InteractionResultStatus,
    SurfaceKind,
    build_action_execution_id,
)


def stale_web_candidate_error(
    intent: ActionIntent,
    *,
    mutation: str = "valid",
) -> CaptureCoordinationError:
    """构造协调器在动作前解析密封候选过期时的完整失败合同。"""

    action_execution_id = build_action_execution_id(intent)
    before_capture: object = CaptureRef(
        task_id=intent.task_id,
        session_id=intent.session_id,
        capture_id="capture-fresh",
        surface_kind=SurfaceKind.WEB,
        capture_kind=CaptureKind.PAGE,
        role="before_action",
        artifact_ids=("capture-fresh:screenshot", "capture-fresh:dom"),
        parent_capture_id=intent.source_capture_id,
        action_execution_id=action_execution_id,
    )
    interaction = InteractionResult(
        task_id=intent.task_id,
        session_id=intent.session_id,
        intent_id=intent.intent_id,
        action_execution_id=action_execution_id,
        source_capture_id="capture-fresh",
        status=InteractionResultStatus.BLOCKED,
        error_code="WEB_SEALED_CANDIDATE_STALE",
        error_message="Web 密封候选已漂移或不属于源 Capture",
        retryable=True,
    )
    stage = CaptureCoordinationStage.ACTION
    after_capture = None

    if mutation == "wrong_stage":
        stage = CaptureCoordinationStage.BEFORE_CAPTURE
    elif mutation == "fake_before_capture":
        before_capture = type(
            "FakeCaptureRef",
            (),
            {"capture_id": "capture-fresh"},
        )()
    elif mutation == "wrong_before_task":
        before_capture = replace(before_capture, task_id="other-task")
    elif mutation == "wrong_before_session":
        before_capture = replace(before_capture, session_id="web:other-run")
    elif mutation == "wrong_before_surface":
        before_capture = replace(
            before_capture,
            surface_kind=SurfaceKind.ANDROID,
            capture_kind=CaptureKind.APP,
        )
    elif mutation == "wrong_before_role":
        before_capture = replace(before_capture, role="after_action")
    elif mutation == "wrong_before_parent":
        before_capture = replace(before_capture, parent_capture_id="capture-other")
    elif mutation == "wrong_before_action":
        before_capture = replace(before_capture, action_execution_id="action-other")
    elif mutation == "missing_interaction":
        interaction = None
    elif mutation == "wrong_interaction_task":
        interaction = replace(interaction, task_id="other-task")
    elif mutation == "wrong_interaction_session":
        interaction = replace(interaction, session_id="web:other-run")
    elif mutation == "wrong_interaction_intent":
        interaction = replace(interaction, intent_id="intent-other")
    elif mutation == "wrong_interaction_action":
        interaction = replace(interaction, action_execution_id="action-other")
    elif mutation == "wrong_interaction_source":
        interaction = replace(interaction, source_capture_id="capture-other")
    elif mutation == "wrong_interaction_status":
        interaction = replace(
            interaction,
            status=InteractionResultStatus.FAILED,
            execution_outcome=None,
            verification_outcome=None,
        )
    elif mutation == "wrong_interaction_error":
        interaction = replace(interaction, error_code="WEB_OTHER_FAILURE")
    elif mutation == "interaction_has_result_capture":
        interaction = replace(interaction, result_capture_id="capture-after")
    elif mutation == "has_after_capture":
        after_capture = CaptureRef(
            task_id=intent.task_id,
            session_id=intent.session_id,
            capture_id="capture-after",
            surface_kind=SurfaceKind.WEB,
            capture_kind=CaptureKind.PAGE,
            role="after_action",
            artifact_ids=("capture-after:screenshot",),
            parent_capture_id="capture-fresh",
            action_execution_id=action_execution_id,
        )
    elif mutation != "valid":
        raise ValueError(f"未知测试变体: {mutation}")

    return CaptureCoordinationError(
        stage=stage,
        error_code="WEB_SEALED_CANDIDATE_STALE",
        message="动作执行未完成",
        before_capture=before_capture,  # type: ignore[arg-type]
        interaction=interaction,
        after_capture=after_capture,
    )


def retryable_web_action_timeout_error(
    intent: ActionIntent,
) -> CaptureCoordinationError:
    """构造动作结果未知但可通过只读复检恢复的 Web 超时合同。"""

    action_execution_id = build_action_execution_id(intent)
    before_capture = CaptureRef(
        task_id=intent.task_id,
        session_id=intent.session_id,
        capture_id="capture-timeout-before",
        surface_kind=SurfaceKind.WEB,
        capture_kind=CaptureKind.PAGE,
        role="before_action",
        artifact_ids=(
            "capture-timeout-before:screenshot",
            "capture-timeout-before:dom",
        ),
        parent_capture_id=intent.source_capture_id,
        action_execution_id=action_execution_id,
    )
    interaction = InteractionResult(
        task_id=intent.task_id,
        session_id=intent.session_id,
        intent_id=intent.intent_id,
        action_execution_id=action_execution_id,
        source_capture_id=before_capture.capture_id,
        status=InteractionResultStatus.TIMED_OUT,
        error_code="ACTION_TIMED_OUT",
        error_message="浏览器动作超时",
        retryable=True,
    )
    return CaptureCoordinationError(
        stage=CaptureCoordinationStage.ACTION,
        error_code="ACTION_TIMED_OUT",
        message="动作执行未完成",
        before_capture=before_capture,
        interaction=interaction,
        retryable=True,
    )


STRICT_STALE_MUTATIONS = (
    "wrong_stage",
    "fake_before_capture",
    "wrong_before_task",
    "wrong_before_session",
    "wrong_before_surface",
    "wrong_before_role",
    "wrong_before_parent",
    "wrong_before_action",
    "missing_interaction",
    "wrong_interaction_task",
    "wrong_interaction_session",
    "wrong_interaction_intent",
    "wrong_interaction_action",
    "wrong_interaction_source",
    "wrong_interaction_status",
    "wrong_interaction_error",
    "interaction_has_result_capture",
    "has_after_capture",
)
