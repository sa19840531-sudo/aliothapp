"""W17-W20 阶段机动作交接合同测试。"""

from datetime import UTC, datetime, timedelta

import pytest

from services.api.app.application.surface_phase_action_handoff import (
    SurfacePhaseActionHandoffError,
    SurfacePhaseActionHandoffKind,
    build_surface_phase_action_handoff,
)
from services.api.app.domain.exploration import (
    POPUP_CHAIN_PHASE_PROTOCOL_VERSION,
    STARTUP_AD_COUNTDOWN_PHASE_PROTOCOL_VERSION,
    PopupChainObservation,
    PopupChainPhaseContext,
    StartupAdCountdownObservation,
    StartupAdCountdownPhaseContext,
    SurfacePhaseRunSnapshot,
    SurfacePhaseRunStatus,
    evaluate_popup_chain_phase,
    evaluate_startup_ad_countdown_phase,
)
from services.api.app.domain.interaction import InteractionOperation, SurfaceKind

_NOW = datetime(2026, 8, 10, 18, 0, tzinfo=UTC)


def _run(
    *,
    phase_key: str = "startup_ad_countdown",
    phase_protocol_version: str = STARTUP_AD_COUNTDOWN_PHASE_PROTOCOL_VERSION,
) -> SurfacePhaseRunSnapshot:
    return SurfacePhaseRunSnapshot(
        phase_run_id=f"phase-{phase_key}-1",
        task_id="task-1",
        session_id="session-1",
        surface_kind=SurfaceKind.WEB,
        phase_key=phase_key,
        phase_protocol_version=phase_protocol_version,
        idempotency_key="idem-1",
        source_capture_id="capture-1",
        status=SurfacePhaseRunStatus.RUNNING,
        command_sequence=1,
        finalization_epoch=0,
        row_version=0,
        started_at=_NOW,
    )


def test_close_action_is_handed_off_as_action_intent() -> None:
    run = _run()
    decision = evaluate_startup_ad_countdown_phase(
        context=StartupAdCountdownPhaseContext(
            task_id="task-1",
            session_id="session-1",
            phase_run_id=run.phase_run_id,
        ),
        observation=StartupAdCountdownObservation(
            task_id="task-1",
            session_id="session-1",
            source_capture_id="capture-1",
            surface_kind=SurfaceKind.WEB,
            observed_at=_NOW,
            ad_visible=True,
            close_control_visible=True,
            close_control_enabled=True,
            close_semantic_target="启动广告关闭按钮",
        ),
    )

    handoff = build_surface_phase_action_handoff(
        run=run,
        decision=decision,
        deadline=_NOW + timedelta(seconds=30),
    )

    assert handoff.kind is SurfacePhaseActionHandoffKind.ACTION
    assert handoff.action_intent is not None
    assert handoff.timed_capture is None
    intent = handoff.action_intent
    assert intent.action_type is InteractionOperation.CLICK
    assert intent.semantic_target == "启动广告关闭按钮"
    assert intent.source_capture_id == "capture-1"
    assert intent.capture_profile.before.role == "startup_ad_before_close"
    assert intent.capture_profile.after.role == "startup_ad_after_close"
    assert intent.capture_profile.before.artifact_types == (
        "screenshot",
        "ui_tree",
        "ui_snapshot",
        "visible_text",
    )
    assert intent.capture_profile.after.artifact_types == (
        "screenshot",
        "ui_tree",
        "ui_snapshot",
        "visible_text",
        "action_trace",
    )
    assert intent.parameters["phase_run_id"] == run.phase_run_id
    assert intent.parameters["phase_key"] == "startup_ad_countdown"
    assert intent.parameters["decision_id"] == decision.decision_id


def test_waiting_countdown_returns_timed_reobservation_handoff() -> None:
    run = _run()
    decision = evaluate_startup_ad_countdown_phase(
        context=StartupAdCountdownPhaseContext(
            task_id="task-1",
            session_id="session-1",
            phase_run_id=run.phase_run_id,
        ),
        observation=StartupAdCountdownObservation(
            task_id="task-1",
            session_id="session-1",
            source_capture_id="capture-1",
            surface_kind=SurfaceKind.WEB,
            observed_at=_NOW,
            ad_visible=True,
            countdown_remaining_ms=2_000,
            evidence_artifact_ids=("artifact-1",),
        ),
    )

    handoff = build_surface_phase_action_handoff(
        run=run,
        decision=decision,
        deadline=_NOW + timedelta(seconds=30),
    )

    assert handoff.kind is SurfacePhaseActionHandoffKind.TIMED_CAPTURE
    assert handoff.timed_capture is not None
    assert handoff.timed_capture.phase_key == "startup_ad_countdown"
    assert handoff.timed_capture.delay_ms == 1_000
    assert handoff.timed_capture.directive.role == "startup_ad_reobserve"
    assert handoff.timed_capture.directive.artifact_types == (
        "screenshot",
        "ui_tree",
        "ui_snapshot",
        "visible_text",
        "temporal_trace",
    )


def test_terminal_decision_cannot_be_handed_off() -> None:
    run = _run(
        phase_key="popup_chain",
        phase_protocol_version=POPUP_CHAIN_PHASE_PROTOCOL_VERSION,
    )
    decision = evaluate_popup_chain_phase(
        context=PopupChainPhaseContext(
            task_id="task-1",
            session_id="session-1",
            phase_run_id=run.phase_run_id,
        ),
        observation=PopupChainObservation(
            task_id="task-1",
            session_id="session-1",
            source_capture_id="capture-1",
            surface_kind=SurfaceKind.WEB,
            observed_at=_NOW,
            page_state="stable",
            popup_visible=False,
        ),
    )

    with pytest.raises(SurfacePhaseActionHandoffError, match="没有可交接动作"):
        build_surface_phase_action_handoff(
            run=run,
            decision=decision,
            deadline=_NOW + timedelta(seconds=30),
        )


def test_action_handoff_requires_before_and_after_capture_roles() -> None:
    run = _run()
    decision = evaluate_startup_ad_countdown_phase(
        context=StartupAdCountdownPhaseContext(
            task_id="task-1",
            session_id="session-1",
            phase_run_id=run.phase_run_id,
        ),
        observation=StartupAdCountdownObservation(
            task_id="task-1",
            session_id="session-1",
            source_capture_id="capture-1",
            surface_kind=SurfaceKind.WEB,
            observed_at=_NOW,
            ad_visible=True,
            close_control_visible=True,
            close_control_enabled=True,
            close_semantic_target="启动广告关闭按钮",
        ),
    )
    assert decision.next_action is not None
    patched_action = type(decision.next_action)(
        action_id=decision.next_action.action_id,
        action_type=decision.next_action.action_type,
        source_capture_id=decision.next_action.source_capture_id,
        purpose=decision.next_action.purpose,
        semantic_target=decision.next_action.semantic_target,
        delay_ms=decision.next_action.delay_ms,
        capture_roles=("only_before",),
    )
    patched_decision = type(decision)(
        decision_id=decision.decision_id,
        phase_run_id=decision.phase_run_id,
        status=decision.status,
        reason_code=decision.reason_code,
        source_capture_id=decision.source_capture_id,
        next_action=patched_action,
        evidence_artifact_ids=decision.evidence_artifact_ids,
    )

    with pytest.raises(SurfacePhaseActionHandoffError, match="Capture role"):
        build_surface_phase_action_handoff(
            run=run,
            decision=patched_decision,
            deadline=_NOW + timedelta(seconds=30),
        )
