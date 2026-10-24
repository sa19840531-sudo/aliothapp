"""启动广告倒计时阶段机领域测试。"""

from datetime import UTC, datetime

import pytest

from services.api.app.domain.exploration import (
    StartupAdCountdownDecisionStatus,
    StartupAdCountdownObservation,
    StartupAdCountdownPhaseContext,
    StartupAdCountdownPhaseValidationError,
    evaluate_startup_ad_countdown_phase,
)
from services.api.app.domain.interaction import InteractionOperation, SurfaceKind

_NOW = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)


def _context(**overrides: object) -> StartupAdCountdownPhaseContext:
    payload = {
        "task_id": "task-1",
        "session_id": "session-1",
        "phase_run_id": "phase-startup-ad-1",
    } | overrides
    return StartupAdCountdownPhaseContext(**payload)


def _observation(**overrides: object) -> StartupAdCountdownObservation:
    payload = {
        "task_id": "task-1",
        "session_id": "session-1",
        "source_capture_id": "capture-startup-ad-1",
        "surface_kind": SurfaceKind.WEB,
        "observed_at": _NOW,
        "page_type": "startup_ad",
        "page_state": "countdown",
        "ad_visible": True,
        "evidence_artifact_ids": ("artifact-startup-ad-1",),
    } | overrides
    return StartupAdCountdownObservation(**payload)


def test_countdown_not_finished_requests_bounded_reobservation() -> None:
    decision = evaluate_startup_ad_countdown_phase(
        context=_context(countdown_observation_count=0, max_countdown_observations=1),
        observation=_observation(countdown_remaining_ms=5_000),
    )

    assert decision.status is StartupAdCountdownDecisionStatus.WAITING_COUNTDOWN
    assert decision.reason_code == "countdown_reobserve_required"
    assert decision.next_action is not None
    assert decision.next_action.action_type is InteractionOperation.TIMED_CAPTURE
    assert decision.next_action.delay_ms == 1_000
    assert decision.next_action.capture_roles == ("startup_ad_reobserve",)


def test_countdown_with_two_evidence_frames_forces_enter_skip() -> None:
    """倒计时未结束但已有 2 帧证据：必须点进入／跳过。"""

    decision = evaluate_startup_ad_countdown_phase(
        context=_context(countdown_observation_count=1, max_countdown_observations=10),
        observation=_observation(
            countdown_remaining_ms=1_000,
            evidence_artifact_ids=("artifact-1", "artifact-2"),
        ),
    )

    assert decision.status is StartupAdCountdownDecisionStatus.READY_TO_CLOSE
    assert decision.reason_code == "startup_ad_two_frame_enter_ready"
    assert decision.next_action is not None
    assert decision.next_action.action_type is InteractionOperation.CLICK
    assert "进入" in decision.next_action.semantic_target


def test_missing_countdown_and_close_control_requests_bounded_grace_reobservation() -> None:
    decision = evaluate_startup_ad_countdown_phase(
        context=_context(countdown_observation_count=0, max_countdown_observations=1),
        observation=_observation(countdown_remaining_ms=None),
    )

    assert decision.status is StartupAdCountdownDecisionStatus.WAITING_COUNTDOWN
    assert decision.reason_code == "startup_ad_control_reobserve_required"
    assert decision.next_action is not None
    assert decision.next_action.action_type is InteractionOperation.TIMED_CAPTURE
    assert decision.next_action.delay_ms == 500


def test_missing_close_control_after_two_frames_forces_enter_skip() -> None:
    """无可见控件但已有 2 帧截图：仍须点默认进入／跳过。"""

    decision = evaluate_startup_ad_countdown_phase(
        context=_context(
            countdown_observation_count=1,
            max_countdown_observations=10,
        ),
        observation=_observation(
            countdown_remaining_ms=None,
            evidence_artifact_ids=("artifact-1", "artifact-2"),
        ),
    )

    assert decision.status is StartupAdCountdownDecisionStatus.READY_TO_CLOSE
    assert decision.reason_code == "startup_ad_two_frame_enter_ready"
    assert decision.next_action is not None
    assert decision.next_action.semantic_target == "开屏进入：进入/跳过"


def test_close_control_ready_outputs_click_action_with_before_after_capture_roles() -> None:
    decision = evaluate_startup_ad_countdown_phase(
        context=_context(),
        observation=_observation(
            countdown_remaining_ms=0,
            close_control_visible=True,
            close_control_enabled=True,
            close_semantic_target="启动广告关闭按钮",
        ),
    )

    assert decision.status is StartupAdCountdownDecisionStatus.READY_TO_CLOSE
    assert decision.reason_code == "close_control_ready"
    assert decision.next_action is not None
    assert decision.next_action.action_type is InteractionOperation.CLICK
    assert decision.next_action.semantic_target == "启动广告关闭按钮"
    assert decision.next_action.capture_roles == (
        "startup_ad_before_close",
        "startup_ad_after_close",
    )


def test_android_close_control_outputs_tap_action() -> None:
    decision = evaluate_startup_ad_countdown_phase(
        context=_context(),
        observation=_observation(
            surface_kind=SurfaceKind.ANDROID,
            countdown_remaining_ms=0,
            close_control_visible=True,
            close_control_enabled=True,
            close_semantic_target="启动广告关闭按钮",
        ),
    )

    assert decision.next_action is not None
    assert decision.next_action.action_type is InteractionOperation.TAP


def test_close_control_without_semantic_target_blocks() -> None:
    decision = evaluate_startup_ad_countdown_phase(
        context=_context(),
        observation=_observation(
            countdown_remaining_ms=0,
            close_control_visible=True,
            close_control_enabled=True,
        ),
    )

    assert decision.status is StartupAdCountdownDecisionStatus.BLOCKED
    assert decision.reason_code == "close_control_target_missing"


def test_after_close_requires_stable_page_before_completed() -> None:
    decision = evaluate_startup_ad_countdown_phase(
        context=_context(close_attempt_count=1),
        observation=_observation(
            source_capture_id="capture-after-close-1",
            page_type="home",
            page_state="stable",
            ad_visible=False,
            stable_page_reached=True,
            evidence_artifact_ids=("artifact-after-close-1",),
        ),
    )

    assert decision.status is StartupAdCountdownDecisionStatus.COMPLETED
    assert decision.reason_code == "startup_ad_closed_and_stable"
    assert decision.next_action is None
    assert decision.evidence_artifact_ids == ("artifact-after-close-1",)


def test_after_close_without_stable_page_requests_reobservation() -> None:
    decision = evaluate_startup_ad_countdown_phase(
        context=_context(close_attempt_count=1),
        observation=_observation(
            source_capture_id="capture-after-close-1",
            page_type="startup_ad",
            page_state="loading",
            ad_visible=False,
            stable_page_reached=False,
        ),
    )

    assert decision.status is StartupAdCountdownDecisionStatus.VERIFYING_CLOSE
    assert decision.reason_code == "stable_page_reobserve_required"
    assert decision.next_action is not None
    assert decision.next_action.action_type is InteractionOperation.TIMED_CAPTURE
    assert decision.next_action.delay_ms == 500


def test_after_close_popup_completes_startup_phase_for_generic_popup_router() -> None:
    decision = evaluate_startup_ad_countdown_phase(
        context=_context(close_attempt_count=1),
        observation=_observation(
            source_capture_id="capture-home-popup-1",
            page_type="home",
            page_state="popup",
            ad_visible=False,
            stable_page_reached=False,
        ),
    )

    assert decision.status is StartupAdCountdownDecisionStatus.COMPLETED
    assert decision.reason_code == "startup_ad_transitioned_to_followup_surface"
    assert decision.next_action is None


def test_after_close_unknown_surface_blocks_after_screenshot_budget() -> None:
    decision = evaluate_startup_ad_countdown_phase(
        context=_context(
            close_attempt_count=1,
            countdown_observation_count=1,
            max_countdown_observations=1,
        ),
        observation=_observation(
            source_capture_id="capture-unknown-after-close-1",
            page_type="unknown",
            page_state="unknown",
            ad_visible=False,
            stable_page_reached=False,
        ),
    )

    assert decision.status is StartupAdCountdownDecisionStatus.BLOCKED
    assert decision.reason_code == "stable_page_reobserve_limit_exceeded"
    assert decision.next_action is None


def test_close_attempt_limit_blocks_remaining_ad() -> None:
    decision = evaluate_startup_ad_countdown_phase(
        context=_context(close_attempt_count=3, max_close_attempts=3),
        observation=_observation(countdown_remaining_ms=0),
    )

    assert decision.status is StartupAdCountdownDecisionStatus.BLOCKED
    assert decision.reason_code == "close_attempt_limit_exceeded"


def test_context_and_observation_must_belong_to_same_session() -> None:
    with pytest.raises(
        StartupAdCountdownPhaseValidationError,
        match="不属于同一任务会话",
    ):
        evaluate_startup_ad_countdown_phase(
            context=_context(session_id="session-other"),
            observation=_observation(),
        )


def test_semantic_target_rejects_platform_locator() -> None:
    with pytest.raises(StartupAdCountdownPhaseValidationError, match="不能是平台定位器"):
        _observation(close_semantic_target="#close")
