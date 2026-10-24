"""认证门禁阶段机领域测试。"""

from datetime import UTC, datetime

import pytest

from services.api.app.domain.exploration import (
    AuthGateDecisionStatus,
    AuthGateObservation,
    AuthGatePhaseContext,
    AuthGatePhaseValidationError,
    evaluate_auth_gate_phase,
)
from services.api.app.domain.interaction import InteractionOperation, SurfaceKind

_NOW = datetime(2026, 8, 10, 14, 0, tzinfo=UTC)


def _context(**overrides: object) -> AuthGatePhaseContext:
    payload = {
        "task_id": "task-1",
        "session_id": "session-1",
        "phase_run_id": "phase-auth-gate-1",
    } | overrides
    return AuthGatePhaseContext(**payload)


def _observation(**overrides: object) -> AuthGateObservation:
    payload = {
        "task_id": "task-1",
        "session_id": "session-1",
        "source_capture_id": "capture-auth-gate-1",
        "surface_kind": SurfaceKind.WEB,
        "observed_at": _NOW,
        "page_state": "auth_gate",
        "auth_gate_visible": True,
        "login_form_visible": True,
        "requires_auth": True,
        "evidence_artifact_ids": ("artifact-auth-gate-1",),
    } | overrides
    return AuthGateObservation(**payload)


def test_dismiss_control_ready_outputs_click_action() -> None:
    decision = evaluate_auth_gate_phase(
        context=_context(),
        observation=_observation(
            login_form_visible=False,
            requires_auth=False,
            dismiss_control_visible=True,
            dismiss_control_enabled=True,
            dismiss_semantic_target="登录弹窗关闭按钮",
        ),
    )

    assert decision.status is AuthGateDecisionStatus.READY_TO_DISMISS
    assert decision.reason_code == "dismiss_control_ready"
    assert decision.next_action is not None
    assert decision.next_action.action_type is InteractionOperation.CLICK
    assert decision.next_action.semantic_target == "登录弹窗关闭按钮"
    assert decision.next_action.capture_roles == (
        "auth_gate_before_dismiss",
        "auth_gate_after_dismiss",
    )


def test_android_dismiss_control_outputs_tap_action() -> None:
    decision = evaluate_auth_gate_phase(
        context=_context(),
        observation=_observation(
            surface_kind=SurfaceKind.ANDROID,
            login_form_visible=False,
            requires_auth=False,
            dismiss_control_visible=True,
            dismiss_control_enabled=True,
            dismiss_semantic_target="登录弹窗关闭按钮",
        ),
    )

    assert decision.next_action is not None
    assert decision.next_action.action_type is InteractionOperation.TAP


def test_guest_entry_is_treated_as_dismiss_action() -> None:
    decision = evaluate_auth_gate_phase(
        context=_context(),
        observation=_observation(
            login_form_visible=False,
            requires_auth=False,
            guest_entry_visible=True,
            guest_entry_enabled=True,
            guest_entry_semantic_target="游客模式入口",
        ),
    )

    assert decision.status is AuthGateDecisionStatus.READY_TO_DISMISS
    assert decision.reason_code == "guest_entry_ready"
    assert decision.next_action is not None
    assert decision.next_action.semantic_target == "游客模式入口"


def test_login_form_without_dismiss_path_requires_auth() -> None:
    decision = evaluate_auth_gate_phase(
        context=_context(),
        observation=_observation(),
    )

    assert decision.status is AuthGateDecisionStatus.REQUIRES_AUTH
    assert decision.reason_code == "credentials_required"
    assert decision.next_action is None


def test_after_dismiss_stable_page_closes_as_dismissed() -> None:
    decision = evaluate_auth_gate_phase(
        context=_context(dismiss_attempt_count=1),
        observation=_observation(
            source_capture_id="capture-stable-1",
            page_state="stable",
            auth_gate_visible=False,
            login_form_visible=False,
            requires_auth=False,
            stable_page_reached=True,
            evidence_artifact_ids=("artifact-stable-1",),
        ),
    )

    assert decision.status is AuthGateDecisionStatus.DISMISSED
    assert decision.reason_code == "auth_gate_dismissed"
    assert decision.next_action is None
    assert decision.evidence_artifact_ids == ("artifact-stable-1",)


def test_no_auth_gate_surface_closes_as_not_present() -> None:
    decision = evaluate_auth_gate_phase(
        context=_context(),
        observation=_observation(
            page_state="stable",
            auth_gate_visible=False,
            login_form_visible=False,
            requires_auth=False,
        ),
    )

    assert decision.status is AuthGateDecisionStatus.NOT_PRESENT
    assert decision.reason_code == "auth_gate_not_present"
    assert decision.next_action is None


def test_missing_dismiss_target_blocks_without_guessing_locator() -> None:
    decision = evaluate_auth_gate_phase(
        context=_context(),
        observation=_observation(
            login_form_visible=False,
            requires_auth=False,
            dismiss_control_visible=True,
            dismiss_control_enabled=True,
        ),
    )

    assert decision.status is AuthGateDecisionStatus.BLOCKED
    assert decision.reason_code == "dismiss_control_target_missing"


def test_dismiss_attempt_limit_requires_auth() -> None:
    decision = evaluate_auth_gate_phase(
        context=_context(dismiss_attempt_count=2, max_dismiss_attempts=2),
        observation=_observation(),
    )

    assert decision.status is AuthGateDecisionStatus.REQUIRES_AUTH
    assert decision.reason_code == "dismiss_attempt_limit_exceeded"


def test_reobserve_limit_blocks_unknown_auth_gate_state() -> None:
    decision = evaluate_auth_gate_phase(
        context=_context(reobserve_count=4, max_reobservations=4),
        observation=_observation(
            login_form_visible=False,
            requires_auth=False,
        ),
    )

    assert decision.status is AuthGateDecisionStatus.BLOCKED
    assert decision.reason_code == "auth_gate_reobserve_limit_exceeded"


def test_unknown_auth_gate_state_reobserves_before_blocking() -> None:
    decision = evaluate_auth_gate_phase(
        context=_context(reobserve_count=1),
        observation=_observation(
            login_form_visible=False,
            requires_auth=False,
        ),
    )

    assert decision.status is AuthGateDecisionStatus.VERIFYING_DISMISS
    assert decision.reason_code == "auth_gate_reobserve_required"
    assert decision.next_action is not None
    assert decision.next_action.action_type is InteractionOperation.TIMED_CAPTURE
    assert decision.next_action.capture_roles == ("auth_gate_reobserve",)


def test_context_and_observation_must_belong_to_same_session() -> None:
    with pytest.raises(AuthGatePhaseValidationError, match="不属于同一任务会话"):
        evaluate_auth_gate_phase(
            context=_context(session_id="session-other"),
            observation=_observation(),
        )


def test_semantic_target_rejects_platform_locator() -> None:
    with pytest.raises(AuthGatePhaseValidationError, match="不能是平台定位器"):
        _observation(dismiss_semantic_target=".close")
