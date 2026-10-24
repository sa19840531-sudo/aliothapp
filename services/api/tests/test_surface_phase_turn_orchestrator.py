"""W17-W20 Surface 阶段单轮编排测试。"""

from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import pytest

from services.api.app.application.surface_phase_action_handoff import (
    SurfacePhaseActionHandoffKind,
)
from services.api.app.application.surface_phase_turn_orchestrator import (
    SurfacePhaseTurnOrchestrator,
)
from services.api.app.domain.exploration import (
    POPUP_CHAIN_PHASE_PROTOCOL_VERSION,
    STARTUP_AD_COUNTDOWN_PHASE_PROTOCOL_VERSION,
    SurfacePhaseRunSnapshot,
    SurfacePhaseRunStatus,
    SurfacePhaseStepSnapshot,
)
from services.api.app.domain.interaction import SurfaceKind

_NOW = datetime(2026, 8, 10, 20, 0, tzinfo=UTC)


class _Repository:
    def __init__(self) -> None:
        self.steps: list[dict[str, Any]] = []
        self.finishes: list[dict[str, Any]] = []

    async def record_step(self, **kwargs: Any) -> SurfacePhaseStepSnapshot:
        self.steps.append(kwargs)
        return SurfacePhaseStepSnapshot(
            phase_step_id=f"step-{len(self.steps)}",
            phase_run_id=kwargs["phase_run_id"],
            task_id="task-1",
            session_id="session-1",
            step_key=kwargs["step_key"],
            step_index=len(self.steps),
            status=kwargs["status"],
            decision_status=kwargs["decision_status"],
            reason_code=kwargs["reason_code"],
            source_capture_id=kwargs["source_capture_id"],
            decision_payload=kwargs["decision_payload"],
            evidence_artifact_ids=tuple(kwargs["evidence_artifact_ids"]),
            next_action_payload=kwargs["next_action_payload"],
            action_execution_id=kwargs.get("action_execution_id"),
            result_capture_id=kwargs.get("result_capture_id"),
            created_at=_NOW,
        )

    async def finish_run(self, **kwargs: Any) -> SurfacePhaseRunSnapshot:
        self.finishes.append(kwargs)
        return replace(
            _run(
                phase_key="popup_chain",
                phase_protocol_version=POPUP_CHAIN_PHASE_PROTOCOL_VERSION,
            ),
            phase_run_id=kwargs["phase_run_id"],
            status=kwargs["status"],
            row_version=kwargs["expected_row_version"] + 1,
            terminal_reason_code=kwargs["terminal_reason_code"],
            finished_at=_NOW,
        )


@pytest.mark.asyncio
async def test_turn_orchestrator_returns_action_handoff_for_ready_close() -> None:
    repository = _Repository()
    run = _run(
        phase_key="startup_ad_countdown",
        phase_protocol_version=STARTUP_AD_COUNTDOWN_PHASE_PROTOCOL_VERSION,
    )

    result = await SurfacePhaseTurnOrchestrator(repository=repository).run_turn(
        run=run,
        page_detection_result={
            "page_capture_id": "capture-1",
            "page_type": "startup_ad",
            "page_state": "countdown",
            "close_control_visible": True,
            "close_control_enabled": True,
            "close_semantic_target": "启动广告关闭按钮",
            "evidence_artifact_ids": ["artifact-startup"],
        },
        observed_at=_NOW,
        action_deadline=_NOW,
    )

    assert result.is_terminal is False
    assert result.needs_interaction is True
    assert result.action_handoff is not None
    assert result.action_handoff.kind is SurfacePhaseActionHandoffKind.ACTION
    assert result.action_handoff.action_intent is not None
    assert result.action_handoff.action_intent.semantic_target == "启动广告关闭按钮"
    assert repository.steps[0]["decision_status"] == "ready_to_close"
    assert repository.finishes == []


@pytest.mark.asyncio
async def test_turn_orchestrator_returns_timed_capture_for_countdown_wait() -> None:
    repository = _Repository()
    run = _run(
        phase_key="startup_ad_countdown",
        phase_protocol_version=STARTUP_AD_COUNTDOWN_PHASE_PROTOCOL_VERSION,
    )

    result = await SurfacePhaseTurnOrchestrator(repository=repository).run_turn(
        run=run,
        page_detection_result={
            "page_capture_id": "capture-1",
            "page_type": "startup_ad",
            "page_state": "countdown",
            "countdown_remaining_ms": 2_500,
            "evidence_artifact_ids": ["artifact-startup"],
        },
        observed_at=_NOW,
        action_deadline=_NOW,
    )

    assert result.is_terminal is False
    assert result.action_handoff is not None
    assert result.action_handoff.kind is SurfacePhaseActionHandoffKind.TIMED_CAPTURE
    assert result.action_handoff.timed_capture is not None
    assert result.action_handoff.timed_capture.delay_ms == 1_000
    assert repository.steps[0]["decision_status"] == "waiting_countdown"
    assert repository.finishes == []


@pytest.mark.asyncio
async def test_turn_orchestrator_finishes_terminal_phase_without_handoff() -> None:
    repository = _Repository()
    run = _run(
        phase_key="popup_chain",
        phase_protocol_version=POPUP_CHAIN_PHASE_PROTOCOL_VERSION,
        context_payload={"closed_popup_count": 1},
    )

    result = await SurfacePhaseTurnOrchestrator(repository=repository).run_turn(
        run=run,
        page_detection_result={
            "page_capture_id": "capture-1",
            "page_type": "home",
            "page_state": "stable",
            "evidence_artifact_ids": ["artifact-home"],
        },
        observed_at=_NOW,
        action_deadline=_NOW,
    )

    assert result.is_terminal is True
    assert result.needs_interaction is False
    assert result.action_handoff is None
    assert result.turn_result.finished_run is not None
    assert result.turn_result.finished_run.status is SurfacePhaseRunStatus.COMPLETED
    assert repository.finishes[0]["terminal_reason_code"] == "popup_chain_cleared"


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
        source_capture_id="capture-1",
        status=SurfacePhaseRunStatus.RUNNING,
        command_sequence=1,
        finalization_epoch=0,
        row_version=1,
        context_payload=context_payload or {},
        started_at=_NOW,
    )
