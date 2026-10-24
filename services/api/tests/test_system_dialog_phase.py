"""系统弹窗阶段机领域测试。"""

from datetime import UTC, datetime

import pytest

from services.api.app.domain.exploration import (
    SystemDialogActionPolicy,
    SystemDialogDecisionStatus,
    SystemDialogKind,
    SystemDialogObservation,
    SystemDialogPhaseContext,
    SystemDialogPhaseValidationError,
    evaluate_system_dialog_phase,
)
from services.api.app.domain.interaction import InteractionOperation, SurfaceKind

_NOW = datetime(2026, 8, 10, 15, 0, tzinfo=UTC)


def _context(**overrides: object) -> SystemDialogPhaseContext:
    payload = {
        "task_id": "task-1",
        "session_id": "session-1",
        "phase_run_id": "phase-system-dialog-1",
    } | overrides
    return SystemDialogPhaseContext(**payload)


def _observation(**overrides: object) -> SystemDialogObservation:
    payload = {
        "task_id": "task-1",
        "session_id": "session-1",
        "source_capture_id": "capture-system-dialog-1",
        "surface_kind": SurfaceKind.ANDROID,
        "observed_at": _NOW,
        "dialog_visible": True,
        "dialog_kind": SystemDialogKind.PERMISSION,
        "action_policy": SystemDialogActionPolicy.ALLOW,
        "page_state": "permission_dialog",
        "evidence_artifact_ids": ("artifact-system-dialog-1",),
    } | overrides
    return SystemDialogObservation(**payload)


def test_permission_dialog_allow_action_outputs_tap_with_capture_roles() -> None:
    decision = evaluate_system_dialog_phase(
        context=_context(),
        observation=_observation(
            action_control_visible=True,
            action_control_enabled=True,
            action_semantic_target="系统权限允许按钮",
        ),
    )

    assert decision.status is SystemDialogDecisionStatus.READY_TO_HANDLE
    assert decision.reason_code == "allow_action_ready"
    assert decision.next_action is not None
    assert decision.next_action.action_type is InteractionOperation.TAP
    assert decision.next_action.semantic_target == "系统权限允许按钮"
    assert decision.next_action.capture_roles == (
        "system_dialog_before_action",
        "system_dialog_after_action",
    )


def test_web_browser_permission_outputs_click_action() -> None:
    decision = evaluate_system_dialog_phase(
        context=_context(),
        observation=_observation(
            surface_kind=SurfaceKind.WEB,
            dialog_kind=SystemDialogKind.BROWSER_PERMISSION,
            action_policy=SystemDialogActionPolicy.DENY,
            action_control_visible=True,
            action_control_enabled=True,
            action_semantic_target="浏览器权限拒绝按钮",
        ),
    )

    assert decision.next_action is not None
    assert decision.next_action.action_type is InteractionOperation.CLICK
    assert decision.reason_code == "deny_action_ready"


def test_manual_review_policy_blocks_without_action() -> None:
    decision = evaluate_system_dialog_phase(
        context=_context(),
        observation=_observation(action_policy=SystemDialogActionPolicy.MANUAL_REVIEW),
    )

    assert decision.status is SystemDialogDecisionStatus.BLOCKED
    assert decision.reason_code == "system_dialog_requires_manual_review"
    assert decision.next_action is None


def test_missing_action_target_blocks_without_guessing_locator() -> None:
    decision = evaluate_system_dialog_phase(
        context=_context(),
        observation=_observation(
            action_control_visible=True,
            action_control_enabled=True,
        ),
    )

    assert decision.status is SystemDialogDecisionStatus.BLOCKED
    assert decision.reason_code == "system_dialog_action_target_missing"


def test_after_action_stable_page_closes_as_handled() -> None:
    decision = evaluate_system_dialog_phase(
        context=_context(handled_dialog_count=1),
        observation=_observation(
            source_capture_id="capture-stable-1",
            dialog_visible=False,
            page_state="stable",
            stable_page_reached=True,
            evidence_artifact_ids=("artifact-stable-1",),
        ),
    )

    assert decision.status is SystemDialogDecisionStatus.HANDLED
    assert decision.reason_code == "system_dialog_handled"
    assert decision.next_action is None
    assert decision.evidence_artifact_ids == ("artifact-stable-1",)


def test_no_system_dialog_surface_closes_as_not_present() -> None:
    decision = evaluate_system_dialog_phase(
        context=_context(),
        observation=_observation(
            dialog_visible=False,
            page_state="stable",
        ),
    )

    assert decision.status is SystemDialogDecisionStatus.NOT_PRESENT
    assert decision.reason_code == "system_dialog_not_present"
    assert decision.next_action is None


def test_dialog_limit_blocks_without_infinite_handling() -> None:
    decision = evaluate_system_dialog_phase(
        context=_context(handled_dialog_count=6, max_system_dialogs=6),
        observation=_observation(),
    )

    assert decision.status is SystemDialogDecisionStatus.BLOCKED
    assert decision.reason_code == "system_dialog_limit_exceeded"


def test_unavailable_action_reobserves_before_blocking() -> None:
    decision = evaluate_system_dialog_phase(
        context=_context(reobserve_count=1),
        observation=_observation(),
    )

    assert decision.status is SystemDialogDecisionStatus.VERIFYING_HANDLE
    assert decision.reason_code == "system_dialog_reobserve_required"
    assert decision.next_action is not None
    assert decision.next_action.action_type is InteractionOperation.TIMED_CAPTURE
    assert decision.next_action.capture_roles == ("system_dialog_reobserve",)


def test_reobserve_limit_blocks_when_action_never_becomes_available() -> None:
    decision = evaluate_system_dialog_phase(
        context=_context(reobserve_count=4, max_reobservations=4),
        observation=_observation(),
    )

    assert decision.status is SystemDialogDecisionStatus.BLOCKED
    assert decision.reason_code == "system_dialog_reobserve_limit_exceeded"


def test_context_and_observation_must_belong_to_same_session() -> None:
    with pytest.raises(SystemDialogPhaseValidationError, match="不属于同一任务会话"):
        evaluate_system_dialog_phase(
            context=_context(session_id="session-other"),
            observation=_observation(),
        )


def test_semantic_target_rejects_platform_locator() -> None:
    with pytest.raises(SystemDialogPhaseValidationError, match="不能是平台定位器"):
        _observation(action_semantic_target="id=allow")
