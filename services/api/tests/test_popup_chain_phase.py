"""连续弹窗阶段机领域测试。"""

from datetime import UTC, datetime

import pytest

from services.api.app.domain.exploration import (
    PopupChainDecisionStatus,
    PopupChainObservation,
    PopupChainPhaseContext,
    PopupChainPhaseValidationError,
    evaluate_popup_chain_phase,
)
from services.api.app.domain.interaction import InteractionOperation, SurfaceKind

_NOW = datetime(2026, 8, 10, 13, 0, tzinfo=UTC)


def _context(**overrides: object) -> PopupChainPhaseContext:
    payload = {
        "task_id": "task-1",
        "session_id": "session-1",
        "phase_run_id": "phase-popup-chain-1",
    } | overrides
    return PopupChainPhaseContext(**payload)


def _observation(**overrides: object) -> PopupChainObservation:
    payload = {
        "task_id": "task-1",
        "session_id": "session-1",
        "source_capture_id": "capture-popup-1",
        "surface_kind": SurfaceKind.WEB,
        "observed_at": _NOW,
        "page_state": "popup",
        "popup_visible": True,
        "top_popup_signature": "popup-layer-1",
        "evidence_artifact_ids": ("artifact-popup-1",),
    } | overrides
    return PopupChainObservation(**payload)


def test_visible_popup_with_close_control_outputs_click_action() -> None:
    decision = evaluate_popup_chain_phase(
        context=_context(closed_popup_count=1),
        observation=_observation(
            close_control_visible=True,
            close_control_enabled=True,
            close_semantic_target="弹窗关闭按钮",
        ),
    )

    assert decision.status is PopupChainDecisionStatus.READY_TO_CLOSE
    assert decision.reason_code == "close_control_ready"
    assert decision.next_action is not None
    assert decision.next_action.action_type is InteractionOperation.CLICK
    assert decision.next_action.semantic_target == "弹窗关闭按钮"
    assert decision.next_action.capture_roles == ("popup_before_close", "popup_after_close")


def test_android_popup_without_close_control_reobserves_before_cloud_back() -> None:
    """无关闭控件时先重采绑下方圆 X，禁止首帧直接 D 档连点「<」。"""

    decision = evaluate_popup_chain_phase(
        context=_context(),
        observation=_observation(
            surface_kind=SurfaceKind.ANDROID,
            close_control_visible=False,
            close_control_enabled=False,
            evidence_artifact_ids=("artifact-popup-1",),
        ),
    )

    assert decision.status is PopupChainDecisionStatus.VERIFYING_CLOSE
    assert decision.reason_code == "popup_reobserve_required"
    assert decision.next_action is not None
    assert decision.next_action.action_type is InteractionOperation.TIMED_CAPTURE


def test_android_popup_after_reobserve_falls_back_to_cloud_back_twice() -> None:
    """重采后仍无关闭控件：D 档连点两下底栏「<」。"""

    decision = evaluate_popup_chain_phase(
        context=_context(reobserve_count=1),
        observation=_observation(
            surface_kind=SurfaceKind.ANDROID,
            close_control_visible=False,
            close_control_enabled=False,
            evidence_artifact_ids=("artifact-popup-1", "artifact-popup-2"),
        ),
    )

    assert decision.status is PopupChainDecisionStatus.READY_TO_CLOSE
    assert decision.reason_code == "popup_one_frame_cloud_back_twice_ready"
    assert decision.next_action is not None
    assert decision.next_action.action_type is InteractionOperation.BACK
    assert "底部导航栏" in decision.next_action.semantic_target
    assert "cloud_back_tap_twice" in decision.next_action.purpose
    assert "连点两下" in decision.next_action.purpose


def test_android_popup_prefers_close_control_when_x_visible() -> None:
    decision = evaluate_popup_chain_phase(
        context=_context(),
        observation=_observation(
            surface_kind=SurfaceKind.ANDROID,
            close_control_visible=True,
            close_control_enabled=True,
            close_semantic_target="弹窗关闭按钮",
            evidence_artifact_ids=("artifact-popup-1",),
        ),
    )

    assert decision.reason_code == "close_control_ready"
    assert decision.next_action is not None
    assert decision.next_action.action_type is InteractionOperation.TAP
    assert decision.next_action.semantic_target == "弹窗关闭按钮"


def test_android_notice_ack_taps_wozhidaole_not_cloud_back() -> None:
    """官方公告有「我知道了」：点确认文案，不连点底栏「<」。"""

    decision = evaluate_popup_chain_phase(
        context=_context(),
        observation=_observation(
            surface_kind=SurfaceKind.ANDROID,
            close_control_visible=True,
            close_control_enabled=True,
            close_semantic_target="我知道了",
            evidence_artifact_ids=("artifact-popup-1",),
        ),
    )

    assert decision.status is PopupChainDecisionStatus.READY_TO_CLOSE
    assert decision.reason_code == "notice_ack_ready"
    assert decision.next_action is not None
    assert decision.next_action.action_type is InteractionOperation.TAP
    assert decision.next_action.semantic_target == "我知道了"


def test_missing_close_target_blocks_instead_of_guessing_locator() -> None:
    decision = evaluate_popup_chain_phase(
        context=_context(),
        observation=_observation(
            close_control_visible=True,
            close_control_enabled=True,
        ),
    )

    assert decision.status is PopupChainDecisionStatus.BLOCKED
    assert decision.reason_code == "close_control_target_missing"
    assert decision.next_action is None


def test_after_close_android_still_cloud_backs_while_popup_remains() -> None:
    """关一层后仍有弹窗：Android 有证据帧继续点「<」，不空等重采。"""

    decision = evaluate_popup_chain_phase(
        context=_context(closed_popup_count=1, reobserve_count=1),
        observation=_observation(
            surface_kind=SurfaceKind.ANDROID,
            source_capture_id="capture-after-popup-close-1",
            popup_visible=True,
            top_popup_signature="popup-layer-2",
            close_control_visible=False,
            close_control_enabled=False,
            evidence_artifact_ids=("artifact-popup-2",),
        ),
    )

    assert decision.status is PopupChainDecisionStatus.READY_TO_CLOSE
    assert decision.reason_code == "popup_one_frame_cloud_back_twice_ready"
    assert decision.next_action is not None
    assert decision.next_action.action_type is InteractionOperation.BACK


def test_web_popup_reobserves_when_close_control_missing() -> None:
    decision = evaluate_popup_chain_phase(
        context=_context(closed_popup_count=1, reobserve_count=1),
        observation=_observation(
            surface_kind=SurfaceKind.WEB,
            source_capture_id="capture-after-popup-close-1",
            popup_visible=True,
            top_popup_signature="popup-layer-2",
            close_control_visible=False,
            close_control_enabled=False,
        ),
    )

    assert decision.status is PopupChainDecisionStatus.VERIFYING_CLOSE
    assert decision.reason_code == "popup_reobserve_required"
    assert decision.next_action is not None
    assert decision.next_action.action_type is InteractionOperation.TIMED_CAPTURE
    assert decision.next_action.capture_roles == ("popup_reobserve",)


def test_stable_page_after_popup_closes_completes_chain() -> None:
    decision = evaluate_popup_chain_phase(
        context=_context(closed_popup_count=2),
        observation=_observation(
            source_capture_id="capture-stable-1",
            page_state="stable",
            popup_visible=False,
            top_popup_signature=None,
            stable_page_reached=True,
            evidence_artifact_ids=("artifact-stable-1",),
        ),
    )

    assert decision.status is PopupChainDecisionStatus.COMPLETED
    assert decision.reason_code == "popup_chain_cleared"
    assert decision.next_action is None
    assert decision.evidence_artifact_ids == ("artifact-stable-1",)


def test_no_popup_surface_completes_without_action() -> None:
    decision = evaluate_popup_chain_phase(
        context=_context(),
        observation=_observation(
            page_state="stable",
            popup_visible=False,
            top_popup_signature=None,
        ),
    )

    assert decision.status is PopupChainDecisionStatus.COMPLETED
    assert decision.reason_code == "popup_chain_not_present"
    assert decision.next_action is None


def test_popup_layer_limit_blocks_without_infinite_clicking() -> None:
    decision = evaluate_popup_chain_phase(
        context=_context(closed_popup_count=30, max_popup_layers=30),
        observation=_observation(),
    )

    assert decision.status is PopupChainDecisionStatus.BLOCKED
    assert decision.reason_code == "popup_layer_limit_exceeded"
    assert decision.unclosable_protocol is not None
    assert decision.unclosable_protocol["close_succeeded"] is False
    assert decision.unclosable_protocol["evidence_capture_roles"]["before"] == (
        "popup_before_close"
    )


def test_reobserve_limit_blocks_when_close_control_never_becomes_available() -> None:
    decision = evaluate_popup_chain_phase(
        context=_context(reobserve_count=6, max_reobservations=6),
        observation=_observation(),
    )

    assert decision.status is PopupChainDecisionStatus.BLOCKED
    assert decision.reason_code == "popup_reobserve_limit_exceeded"


def test_same_popup_close_no_progress_blocks_after_bounded_retry() -> None:
    decision = evaluate_popup_chain_phase(
        context=_context(same_popup_attempt_count=2, max_same_popup_attempts=2),
        observation=_observation(
            close_control_visible=True,
            close_control_enabled=True,
            close_semantic_target="确认关闭公告",
        ),
    )

    assert decision.status is PopupChainDecisionStatus.BLOCKED
    assert decision.reason_code == "popup_close_no_progress"
    assert decision.next_action is None


def test_context_and_observation_must_belong_to_same_session() -> None:
    with pytest.raises(PopupChainPhaseValidationError, match="不属于同一任务会话"):
        evaluate_popup_chain_phase(
            context=_context(session_id="session-other"),
            observation=_observation(),
        )


def test_semantic_target_rejects_platform_locator() -> None:
    with pytest.raises(PopupChainPhaseValidationError, match="不能是平台定位器"):
        _observation(close_semantic_target="//button")


def test_stable_page_with_popup_visible_coerces_instead_of_raising() -> None:
    """Vision 常把底层业务页标 stable，同时仍报弹层；不得炸探索。"""

    observation = _observation(
        page_type="home",
        page_state="stable",
        popup_visible=True,
        top_popup_signature="overlay:1",
        close_control_visible=True,
        close_control_enabled=True,
        close_semantic_target="关闭弹层右上X",
        stable_page_reached=True,
    )

    assert observation.stable_page_reached is False
    assert observation.popup_visible is True
    decision = evaluate_popup_chain_phase(context=_context(), observation=observation)
    assert decision.status is PopupChainDecisionStatus.READY_TO_CLOSE
    assert decision.reason_code == "close_control_ready"


@pytest.mark.parametrize("page_state", ["Popup", "MODAL", "Dialog", "overlay"])
def test_popup_page_state_is_case_insensitive(page_state: str) -> None:
    observation = _observation(
        page_state=page_state,
        popup_visible=False,
        top_popup_signature=None,
        close_control_visible=True,
        close_control_enabled=True,
        close_semantic_target="关闭当前弹窗",
    )

    assert observation.is_popup_surface is True
    decision = evaluate_popup_chain_phase(context=_context(), observation=observation)
    assert decision.status is PopupChainDecisionStatus.READY_TO_CLOSE
