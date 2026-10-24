"""W17-W20 Surface 阶段机运行时接线测试。"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import pytest

from services.api.app.application.surface_phase_runtime import (
    SurfacePhaseRuntime,
    SurfacePhaseRuntimeError,
)
from services.api.app.domain.exploration import (
    AUTH_GATE_PHASE_PROTOCOL_VERSION,
    POPUP_CHAIN_PHASE_PROTOCOL_VERSION,
    STARTUP_AD_COUNTDOWN_PHASE_PROTOCOL_VERSION,
    SYSTEM_DIALOG_PHASE_PROTOCOL_VERSION,
    AuthGateObservation,
    AuthGatePhaseContext,
    PopupChainObservation,
    PopupChainPhaseContext,
    StartupAdCountdownObservation,
    StartupAdCountdownPhaseContext,
    SurfacePhaseRunSnapshot,
    SurfacePhaseRunStatus,
    SurfacePhaseStepSnapshot,
    SurfacePhaseStepStatus,
    SystemDialogActionPolicy,
    SystemDialogKind,
    SystemDialogObservation,
    SystemDialogPhaseContext,
)
from services.api.app.domain.interaction import SurfaceKind

_NOW = datetime(2026, 8, 10, 16, 0, tzinfo=UTC)


class FakeSurfacePhaseRepository:
    def __init__(self, *, first_step_index: int = 1) -> None:
        self.steps: list[dict[str, Any]] = []
        self.finishes: list[dict[str, Any]] = []
        self.first_step_index = first_step_index

    async def record_step(self, **kwargs: Any) -> SurfacePhaseStepSnapshot:
        self.steps.append(kwargs)
        step_index = self.first_step_index + len(self.steps) - 1
        return SurfacePhaseStepSnapshot(
            phase_step_id=f"step-{len(self.steps)}",
            phase_run_id=kwargs["phase_run_id"],
            task_id="task-1",
            session_id="session-1",
            step_key=kwargs["step_key"],
            step_index=step_index,
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
                phase_key="startup_ad_countdown",
                phase_protocol_version=STARTUP_AD_COUNTDOWN_PHASE_PROTOCOL_VERSION,
            ),
            phase_run_id=kwargs["phase_run_id"],
            status=kwargs["status"],
            row_version=kwargs["expected_row_version"] + 1,
            terminal_reason_code=kwargs["terminal_reason_code"],
            finished_at=_NOW,
        )


def _run(
    *,
    phase_key: str,
    phase_protocol_version: str,
    source_capture_id: str = "capture-1",
    step_count: int = 0,
    row_version: int | None = None,
) -> SurfacePhaseRunSnapshot:
    return SurfacePhaseRunSnapshot(
        phase_run_id=f"phase-{phase_key}-1",
        task_id="task-1",
        session_id="session-1",
        surface_kind=SurfaceKind.WEB,
        phase_key=phase_key,
        phase_protocol_version=phase_protocol_version,
        idempotency_key="idem-1",
        source_capture_id=source_capture_id,
        status=SurfacePhaseRunStatus.RUNNING,
        command_sequence=1,
        finalization_epoch=0,
        row_version=step_count if row_version is None else row_version,
        step_count=step_count,
        started_at=_NOW,
    )


def _runtime(
    *,
    first_step_index: int = 1,
) -> tuple[SurfacePhaseRuntime, FakeSurfacePhaseRepository]:
    repository = FakeSurfacePhaseRepository(first_step_index=first_step_index)
    return SurfacePhaseRuntime(repository=repository), repository


@pytest.mark.asyncio
async def test_startup_ad_ready_to_close_records_action_without_finishing() -> None:
    runtime, repository = _runtime()
    run = _run(
        phase_key="startup_ad_countdown",
        phase_protocol_version=STARTUP_AD_COUNTDOWN_PHASE_PROTOCOL_VERSION,
    )

    result = await runtime.evaluate_and_record(
        run=run,
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
            page_state="startup_ad",
            ad_visible=True,
            close_control_visible=True,
            close_control_enabled=True,
            close_semantic_target="启动广告关闭按钮",
            evidence_artifact_ids=("artifact-1",),
        ),
    )

    assert result.step.status is SurfacePhaseStepStatus.ACTION_PLANNED
    assert result.finished_run is None
    assert repository.finishes == []
    assert repository.steps[0]["next_action_payload"]["action_type"] == "click"
    assert repository.steps[0]["evidence_artifact_ids"] == ("artifact-1",)


@pytest.mark.asyncio
async def test_popup_chain_completed_records_terminal_step_and_finishes_run() -> None:
    runtime, repository = _runtime()
    run = _run(
        phase_key="popup_chain",
        phase_protocol_version=POPUP_CHAIN_PHASE_PROTOCOL_VERSION,
    )

    result = await runtime.evaluate_and_record(
        run=run,
        context=PopupChainPhaseContext(
            task_id="task-1",
            session_id="session-1",
            phase_run_id=run.phase_run_id,
            closed_popup_count=2,
        ),
        observation=PopupChainObservation(
            task_id="task-1",
            session_id="session-1",
            source_capture_id="capture-1",
            surface_kind=SurfaceKind.WEB,
            observed_at=_NOW,
            page_state="stable",
            popup_visible=False,
            stable_page_reached=True,
            evidence_artifact_ids=("artifact-stable",),
        ),
    )

    assert result.step.status is SurfacePhaseStepStatus.COMPLETED
    assert result.finished_run is not None
    assert result.finished_run.status is SurfacePhaseRunStatus.COMPLETED
    assert repository.finishes[0]["terminal_reason_code"] == "popup_chain_cleared"
    assert repository.finishes[0]["expected_row_version"] == 1


@pytest.mark.asyncio
async def test_terminal_step_uses_run_version_after_idempotent_replay_drift() -> None:
    runtime, repository = _runtime(first_step_index=2)
    run = _run(
        phase_key="popup_chain",
        phase_protocol_version=POPUP_CHAIN_PHASE_PROTOCOL_VERSION,
        step_count=1,
        row_version=2,
    )

    result = await runtime.evaluate_and_record(
        run=run,
        context=PopupChainPhaseContext(
            task_id="task-1",
            session_id="session-1",
            phase_run_id=run.phase_run_id,
            closed_popup_count=0,
        ),
        observation=PopupChainObservation(
            task_id="task-1",
            session_id="session-1",
            source_capture_id="capture-2",
            surface_kind=SurfaceKind.WEB,
            observed_at=_NOW,
            page_state="stable",
            popup_visible=False,
            stable_page_reached=True,
            evidence_artifact_ids=("artifact-stable",),
        ),
    )

    assert result.finished_run is not None
    assert repository.finishes[0]["expected_row_version"] == 3


@pytest.mark.asyncio
async def test_replayed_terminal_step_keeps_current_run_version() -> None:
    runtime, repository = _runtime(first_step_index=1)
    run = _run(
        phase_key="popup_chain",
        phase_protocol_version=POPUP_CHAIN_PHASE_PROTOCOL_VERSION,
        step_count=1,
        row_version=2,
    )

    result = await runtime.evaluate_and_record(
        run=run,
        context=PopupChainPhaseContext(
            task_id="task-1",
            session_id="session-1",
            phase_run_id=run.phase_run_id,
            closed_popup_count=0,
        ),
        observation=PopupChainObservation(
            task_id="task-1",
            session_id="session-1",
            source_capture_id="capture-2",
            surface_kind=SurfaceKind.WEB,
            observed_at=_NOW,
            page_state="stable",
            popup_visible=False,
            stable_page_reached=True,
            evidence_artifact_ids=("artifact-stable",),
        ),
    )

    assert result.finished_run is not None
    assert repository.finishes[0]["expected_row_version"] == 2


@pytest.mark.asyncio
async def test_auth_gate_requires_auth_blocks_phase_run() -> None:
    runtime, repository = _runtime()
    run = _run(
        phase_key="auth_gate",
        phase_protocol_version=AUTH_GATE_PHASE_PROTOCOL_VERSION,
    )

    result = await runtime.evaluate_and_record(
        run=run,
        context=AuthGatePhaseContext(
            task_id="task-1",
            session_id="session-1",
            phase_run_id=run.phase_run_id,
        ),
        observation=AuthGateObservation(
            task_id="task-1",
            session_id="session-1",
            source_capture_id="capture-1",
            surface_kind=SurfaceKind.WEB,
            observed_at=_NOW,
            login_form_visible=True,
            requires_auth=True,
            evidence_artifact_ids=("artifact-auth",),
        ),
    )

    assert result.step.status is SurfacePhaseStepStatus.BLOCKED
    assert result.finished_run is not None
    assert result.finished_run.status is SurfacePhaseRunStatus.BLOCKED
    assert repository.finishes[0]["terminal_reason_code"] == "credentials_required"


@pytest.mark.asyncio
async def test_system_dialog_ready_to_handle_records_action() -> None:
    runtime, repository = _runtime()
    run = _run(
        phase_key="system_dialog",
        phase_protocol_version=SYSTEM_DIALOG_PHASE_PROTOCOL_VERSION,
    )

    result = await runtime.evaluate_and_record(
        run=run,
        context=SystemDialogPhaseContext(
            task_id="task-1",
            session_id="session-1",
            phase_run_id=run.phase_run_id,
        ),
        observation=SystemDialogObservation(
            task_id="task-1",
            session_id="session-1",
            source_capture_id="capture-1",
            surface_kind=SurfaceKind.WEB,
            observed_at=_NOW,
            dialog_visible=True,
            dialog_kind=SystemDialogKind.BROWSER_PERMISSION,
            action_policy=SystemDialogActionPolicy.DENY,
            action_control_visible=True,
            action_control_enabled=True,
            action_semantic_target="拒绝权限按钮",
            evidence_artifact_ids=("artifact-dialog",),
        ),
    )

    assert result.step.status is SurfacePhaseStepStatus.ACTION_PLANNED
    assert result.finished_run is None
    assert repository.steps[0]["decision_status"] == "ready_to_handle"
    assert repository.steps[0]["next_action_payload"]["semantic_target"] == "拒绝权限按钮"


@pytest.mark.asyncio
async def test_rejects_protocol_mismatch_before_writing_step() -> None:
    runtime, repository = _runtime()
    run = _run(
        phase_key="popup_chain",
        phase_protocol_version=STARTUP_AD_COUNTDOWN_PHASE_PROTOCOL_VERSION,
    )

    with pytest.raises(SurfacePhaseRuntimeError, match="协议版本"):
        await runtime.evaluate_and_record(
            run=run,
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

    assert repository.steps == []
