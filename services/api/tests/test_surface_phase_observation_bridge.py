"""Surface 阶段观测桥接测试。"""

from datetime import UTC, datetime

import pytest

from services.api.app.application.surface_phase_observation_bridge import (
    build_surface_phase_observation,
)
from services.api.app.domain.exploration import (
    AUTH_GATE_PHASE_PROTOCOL_VERSION,
    POPUP_CHAIN_PHASE_PROTOCOL_VERSION,
    STARTUP_AD_COUNTDOWN_PHASE_PROTOCOL_VERSION,
    SYSTEM_DIALOG_PHASE_PROTOCOL_VERSION,
    AuthGateObservation,
    PopupChainObservation,
    StartupAdCountdownObservation,
    SurfacePhaseRunSnapshot,
    SurfacePhaseRunStatus,
    SystemDialogActionPolicy,
    SystemDialogKind,
    SystemDialogObservation,
)
from services.api.app.domain.interaction import SurfaceKind

_NOW = datetime(2026, 8, 10, 19, 0, tzinfo=UTC)


def test_startup_ad_detection_builds_countdown_observation() -> None:
    result = build_surface_phase_observation(
        run=_run(
            phase_key="startup_ad_countdown",
            phase_protocol_version=STARTUP_AD_COUNTDOWN_PHASE_PROTOCOL_VERSION,
            context_payload={"countdown_observation_count": 2, "max_close_attempts": 4},
        ),
        page_detection_result={
            "page_capture_id": "capture-startup",
            "page_type": "startup_ad",
            "page_state": "countdown",
            "countdown_remaining_ms": 3_000,
            "close_control_visible": True,
            "close_control_enabled": False,
            "evidence_artifact_ids": ["artifact-startup"],
        },
        observed_at=_NOW,
    )

    assert result.phase_key == "startup_ad_countdown"
    assert result.context.countdown_observation_count == 2
    assert result.context.max_close_attempts == 4
    assert isinstance(result.observation, StartupAdCountdownObservation)
    assert result.observation.ad_visible is True
    assert result.observation.countdown_remaining_ms == 3_000
    assert result.observation.evidence_artifact_ids == ("artifact-startup",)


def test_popup_detection_builds_popup_observation_with_semantic_target() -> None:
    result = build_surface_phase_observation(
        run=_run(
            phase_key="popup_chain",
            phase_protocol_version=POPUP_CHAIN_PHASE_PROTOCOL_VERSION,
            context_payload={"closed_popup_count": 1, "same_popup_attempt_count": 1},
        ),
        page_detection_result={
            "page_capture_id": "capture-popup",
            "page_type": "home",
            "page_state": "popup",
            "close_control_visible": True,
            "close_control_enabled": True,
            "candidate_actions": [
                {
                    "action": "click",
                    "target": "关闭弹窗按钮",
                    "target_keywords": ["关闭"],
                    "purpose": "dismiss popup",
                }
            ],
            "evidence_artifact_ids": ["artifact-popup"],
        },
        observed_at=_NOW,
    )

    assert result.context.closed_popup_count == 1
    assert result.context.same_popup_attempt_count == 1
    assert isinstance(result.observation, PopupChainObservation)
    assert result.observation.popup_visible is True
    assert result.observation.close_semantic_target == "关闭弹窗按钮"
    assert result.observation.stable_page_reached is False


def test_auth_detection_builds_requires_auth_observation() -> None:
    result = build_surface_phase_observation(
        run=_run(
            phase_key="auth_gate",
            phase_protocol_version=AUTH_GATE_PHASE_PROTOCOL_VERSION,
            context_payload={"dismiss_attempt_count": 1},
        ),
        page_detection_result={
            "page_capture_id": "capture-login",
            "page_type": "profile",
            "page_state": "login_required",
            "risk_flags": ["authentication_required"],
            "login_form_visible": True,
            "evidence_artifact_ids": ["artifact-login"],
        },
        observed_at=_NOW,
    )

    assert result.context.dismiss_attempt_count == 1
    assert isinstance(result.observation, AuthGateObservation)
    assert result.observation.auth_gate_visible is True
    assert result.observation.requires_auth is True
    assert result.observation.login_form_visible is True


def test_system_dialog_detection_builds_policy_observation() -> None:
    result = build_surface_phase_observation(
        run=_run(
            phase_key="system_dialog",
            phase_protocol_version=SYSTEM_DIALOG_PHASE_PROTOCOL_VERSION,
            context_payload={"handled_dialog_count": 2},
        ),
        page_detection_result={
            "page_capture_id": "capture-dialog",
            "page_state": "permission_dialog",
            "dialog_kind": "browser_permission",
            "action_policy": "deny",
            "dialog_visible": True,
            "action_control_visible": True,
            "action_control_enabled": True,
            "action_semantic_target": "拒绝权限按钮",
            "evidence_artifact_ids": ["artifact-dialog"],
        },
        observed_at=_NOW,
    )

    assert result.context.handled_dialog_count == 2
    assert isinstance(result.observation, SystemDialogObservation)
    assert result.observation.dialog_kind is SystemDialogKind.BROWSER_PERMISSION
    assert result.observation.action_policy is SystemDialogActionPolicy.DENY
    assert result.observation.action_semantic_target == "拒绝权限按钮"


def test_stable_business_page_marks_surface_stable() -> None:
    result = build_surface_phase_observation(
        run=_run(
            phase_key="popup_chain",
            phase_protocol_version=POPUP_CHAIN_PHASE_PROTOCOL_VERSION,
        ),
        page_detection_result={
            "page_capture_id": "capture-home",
            "page_type": "home",
            "page_state": "stable",
            "evidence_artifact_ids": ["artifact-home"],
        },
        observed_at=_NOW,
    )

    assert isinstance(result.observation, PopupChainObservation)
    assert result.observation.stable_page_reached is True
    assert result.observation.popup_visible is False


def test_stable_business_page_with_popup_visible_is_not_stable_reached() -> None:
    """底层 home+stable 叠加 popup_visible 时，桥接必须降级 stable，继续弹层清障。"""

    result = build_surface_phase_observation(
        run=_run(
            phase_key="popup_chain",
            phase_protocol_version=POPUP_CHAIN_PHASE_PROTOCOL_VERSION,
        ),
        page_detection_result={
            "page_capture_id": "capture-home-overlay",
            "page_type": "home",
            "page_state": "stable",
            "popup_visible": True,
            "top_popup_signature": "overlay:promo",
            "close_control_visible": True,
            "close_control_enabled": True,
            "close_semantic_target": "关闭弹层右上X",
            "evidence_artifact_ids": ["artifact-home-overlay"],
        },
        observed_at=_NOW,
    )

    assert isinstance(result.observation, PopupChainObservation)
    assert result.observation.popup_visible is True
    assert result.observation.stable_page_reached is False
    assert result.observation.close_semantic_target == "关闭弹层右上X"


def test_unknown_phase_or_mismatched_protocol_fails_closed() -> None:
    with pytest.raises(ValueError, match="不支持的 Surface 阶段"):
        build_surface_phase_observation(
            run=_run(phase_key="unknown", phase_protocol_version="unknown.v1"),
            page_detection_result={"page_capture_id": "capture-1"},
            observed_at=_NOW,
        )

    with pytest.raises(ValueError, match="协议版本"):
        build_surface_phase_observation(
            run=_run(
                phase_key="popup_chain",
                phase_protocol_version=STARTUP_AD_COUNTDOWN_PHASE_PROTOCOL_VERSION,
            ),
            page_detection_result={"page_capture_id": "capture-1"},
            observed_at=_NOW,
        )


def _run(
    *,
    phase_key: str,
    phase_protocol_version: str,
    context_payload: dict[str, object] | None = None,
) -> SurfacePhaseRunSnapshot:
    return SurfacePhaseRunSnapshot(
        phase_run_id=f"phase-{phase_key}",
        task_id="task-1",
        session_id="session-1",
        surface_kind=SurfaceKind.WEB,
        phase_key=phase_key,
        phase_protocol_version=phase_protocol_version,
        idempotency_key=f"idem-{phase_key}",
        source_capture_id="capture-source",
        status=SurfacePhaseRunStatus.RUNNING,
        command_sequence=1,
        finalization_epoch=0,
        row_version=1,
        context_payload=context_payload or {},
        started_at=_NOW,
    )
