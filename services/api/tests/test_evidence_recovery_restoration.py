"""页面图逐边恢复到 RestorationCoordinator 端口的适配测试。"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from services.api.app.application.evidence_recovery_replay import (
    EvidenceRecoveryReplayError,
    EvidenceRecoveryReplayExecutor,
    EvidenceRecoveryReplayOutcome,
    RecoveryObservation,
)
from services.api.app.application.evidence_recovery_restoration import (
    CaptureBackedRecoverySnapshotReader,
    EvidenceRecoveryRestorationExecutor,
)
from services.api.app.application.restoration import (
    RestorationCoordinator,
    RestorationExecutionError,
    RestorationExecutionRequest,
    new_running_restoration_attempt,
)
from services.api.app.application.state_equivalence import StateEquivalenceGate
from services.api.app.application.targeted_recapture_restoration import (
    TargetedRecaptureRestorationService,
)
from services.api.app.domain.exploration import ExplorationBudget
from services.api.app.domain.inspection import (
    EvidenceReplayPolicy,
    EvidenceRequiredEquivalence,
    EvidenceSessionDependency,
    EvidenceSideEffectPolicy,
    EvidenceSourceGrade,
    RestorationAttemptStatus,
    RestorationStrategy,
    StateInvariantSnapshot,
    build_evidence_request,
    build_restoration_attempt_id,
    build_restoration_plan,
)
from services.api.app.domain.interaction import CaptureKind, CaptureRef, SurfaceKind

NOW = datetime(2026, 9, 8, 14, tzinfo=UTC)


class RecordingReplay:
    def __init__(self, result=None, error=None) -> None:  # noqa: ANN001
        self.result = result
        self.error = error
        self.calls = []

    async def execute(self, **kwargs):  # noqa: ANN003
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.result


class RecordingSnapshots:
    def __init__(self, snapshot) -> None:  # noqa: ANN001
        self.snapshot = snapshot
        self.calls = []

    async def read(self, **kwargs):  # noqa: ANN003
        self.calls.append(kwargs)
        return self.snapshot


class RecordingRestorationAttempts:
    def __init__(self) -> None:
        self.started = []
        self.completed = []

    async def start(self, *, plan, attempt_no):
        attempt = new_running_restoration_attempt(
            plan=plan,
            attempt_no=attempt_no,
            started_at=NOW,
        )
        self.started.append(attempt)
        return attempt

    async def complete(self, *, attempt, expected_row_version):
        assert expected_row_version == attempt.row_version
        completed = replace(attempt, row_version=attempt.row_version + 1)
        self.completed.append(completed)
        return completed


class RecordingProbePublisher:
    def __init__(self) -> None:
        self.calls = []

    async def publish(self, **kwargs):
        self.calls.append(kwargs)
        return "probe-work"


def _execution_request():
    request = build_evidence_request(
        work_item_id="inspection-work-1",
        attempt_id="inspection-attempt-1",
        task_id="task-1",
        rule_id="rule-1",
        rule_code="rule_1",
        target_capture_id="capture-original",
        reason_code="TRUSTED_EVIDENCE_INVALID",
        evidence_goal="补采详情页证据",
        required_artifact_types=("screenshot",),
        session_dependency=EvidenceSessionDependency.ACTIVE_SESSION,
        replay_policy=EvidenceReplayPolicy.SAME_CONTEXT_ALLOWED,
        required_equivalence=EvidenceRequiredEquivalence.LOGICAL_EQUIVALENT,
        side_effect_policy=EvidenceSideEffectPolicy.READ_ONLY,
        target_invariants={"business": {"screen_id": "detail"}},
        minimum_source_grade=EvidenceSourceGrade.B,
        expires_at=NOW + timedelta(minutes=10),
    )
    plan = build_restoration_plan(
        request=request,
        source_session_id="session-1",
        target_session_id="session-1",
        surface_kind=SurfaceKind.ANDROID,
        strategy=RestorationStrategy.SAME_APP_SESSION,
        replay_execution_plan_id="page-graph-route:route-1",
        normalization_step_ids=("transition-1", "transition-2"),
        deadline=NOW + timedelta(minutes=5),
    )
    return RestorationExecutionRequest(
        request=request,
        plan=plan,
        restoration_attempt_id=build_restoration_attempt_id(
            plan_id=plan.plan_id,
            attempt_no=1,
        ),
    )


def _pin_execution_request() -> RestorationExecutionRequest:
    base = _execution_request()
    plan = build_restoration_plan(
        request=base.request,
        source_session_id="session-1",
        target_session_id="session-1",
        surface_kind=SurfaceKind.ANDROID,
        strategy=RestorationStrategy.ORIGINAL_SURFACE_PIN,
        deadline=NOW + timedelta(minutes=5),
    )
    return RestorationExecutionRequest(
        request=base.request,
        plan=plan,
        restoration_attempt_id=build_restoration_attempt_id(
            plan_id=plan.plan_id,
            attempt_no=1,
        ),
    )


def _snapshot(
    *,
    task_id: str = "task-1",
    session_id: str = "session-1",
    capture_id: str = "capture-restored",
    screen_id: str = "detail",
    captured_at: datetime | None = None,
):
    return StateInvariantSnapshot(
        task_id=task_id,
        capture_id=capture_id,
        session_id=session_id,
        surface_kind=SurfaceKind.ANDROID,
        environment={"app_build": "2026.9.8"},
        business={"screen_id": screen_id},
        semantic={"page_state": "stable"},
        structure={"state_version": "v2"},
        captured_at=captured_at or NOW + timedelta(seconds=3),
    )


async def test_restore_returns_fresh_snapshot_and_replay_receipts() -> None:
    request = _execution_request()
    replay = RecordingReplay(
        EvidenceRecoveryReplayOutcome(
            arrived=True,
            result_capture_id="capture-restored",
            action_execution_ids=("action-1", "action-2"),
            transition_ids=("transition-1", "transition-2"),
        )
    )
    snapshots = RecordingSnapshots(_snapshot())
    executor = EvidenceRecoveryRestorationExecutor(
        replay=replay,
        snapshots=snapshots,
        agent_run_id="run-1",
        finalization_epoch=2,
    )

    result = await executor.restore(request)

    assert result.restored_snapshot.capture_id == "capture-restored"
    assert result.action_execution_ids == ("action-1", "action-2")
    assert replay.calls == [
        {
            "plan": request.plan,
            "agent_run_id": "run-1",
            "finalization_epoch": 2,
        }
    ]
    assert snapshots.calls == [
        {
            "task_id": "task-1",
            "session_id": "session-1",
            "surface_kind": SurfaceKind.ANDROID,
            "capture_id": "capture-restored",
        }
    ]


async def test_restore_maps_replay_failure_without_reading_snapshot() -> None:
    request = _execution_request()
    replay = RecordingReplay(
        error=EvidenceRecoveryReplayError(
            reason_code="RECOVERY_SOURCE_STATE_DRIFTED",
            message="当前页面已漂移",
            transition_id="transition-1",
        )
    )
    snapshots = RecordingSnapshots(_snapshot())
    executor = EvidenceRecoveryRestorationExecutor(
        replay=replay,
        snapshots=snapshots,
        agent_run_id="run-1",
        finalization_epoch=2,
    )

    with pytest.raises(RestorationExecutionError) as error:
        await executor.restore(request)

    assert error.value.error_code == "RECOVERY_SOURCE_STATE_DRIFTED"
    assert error.value.unfulfillable is False
    assert snapshots.calls == []


async def test_restore_rejects_snapshot_identity_drift() -> None:
    request = _execution_request()
    replay = RecordingReplay(
        EvidenceRecoveryReplayOutcome(
            arrived=True,
            result_capture_id="capture-restored",
            action_execution_ids=("action-1",),
            transition_ids=("transition-1", "transition-2"),
        )
    )
    executor = EvidenceRecoveryRestorationExecutor(
        replay=replay,
        snapshots=RecordingSnapshots(_snapshot(session_id="session-other")),
        agent_run_id="run-1",
        finalization_epoch=2,
    )

    with pytest.raises(RestorationExecutionError) as error:
        await executor.restore(request)

    assert error.value.error_code == "RESTORED_CAPTURE_IDENTITY_MISMATCH"
    assert error.value.unfulfillable is True


@pytest.mark.asyncio
async def test_pin_replay_runs_snapshot_equivalence_before_probe_publication() -> None:
    request = _pin_execution_request()
    original = _snapshot(capture_id="capture-original", captured_at=NOW)

    class PinRuntime:
        def __init__(self) -> None:
            self.observations = []

        async def observe(self, **kwargs):
            self.observations.append(kwargs)
            return RecoveryObservation(
                capture_id="capture-current",
                page_identity="wrong-page",
                state_fingerprint="wrong-state",
                observed_at=NOW + timedelta(seconds=1),
            )

    runtime = PinRuntime()
    replay = EvidenceRecoveryReplayExecutor(
        repository=object(),
        runtime=runtime,
        now=lambda: NOW,
    )
    snapshots = RecordingSnapshots(
        _snapshot(capture_id="capture-current", screen_id="other")
    )
    executor = EvidenceRecoveryRestorationExecutor(
        replay=replay,
        snapshots=snapshots,
        agent_run_id="run-1",
        finalization_epoch=2,
    )
    attempts = RecordingRestorationAttempts()
    coordinator = RestorationCoordinator(
        repository=attempts,
        executor=executor,
        equivalence_gate=StateEquivalenceGate(clock=lambda: NOW + timedelta(seconds=4)),
        clock=lambda: NOW + timedelta(seconds=4),
    )
    publisher = RecordingProbePublisher()
    service = TargetedRecaptureRestorationService(
        coordinator=coordinator,
        probe_publisher=publisher,
    )

    outcome = await service.restore_and_publish(
        request=request.request,
        plan=request.plan,
        original_snapshot=original,
        budget=ExplorationBudget(
            max_actions=1,
            remaining_actions=1,
            max_depth=0,
            remaining_duration_ms=30_000,
        ),
        attempt_no=1,
    )

    assert outcome.restoration_attempt.status is RestorationAttemptStatus.DIVERGENT
    assert outcome.restoration_attempt.assessment is not None
    assert outcome.restoration_attempt.assessment.passed is False
    assert outcome.probe_work is None
    assert publisher.calls == []
    assert runtime.observations[0]["plan"] is request.plan
    assert snapshots.calls[0]["capture_id"] == "capture-current"


async def test_capture_backed_reader_reuses_verified_capture_snapshot_reader() -> None:
    capture = CaptureRef(
        capture_id="capture-restored",
        task_id="task-1",
        session_id="session-1",
        surface_kind=SurfaceKind.ANDROID,
        capture_kind=CaptureKind.APP,
        role="after_action",
        artifact_ids=("artifact-1",),
        parent_capture_id="capture-before",
        action_execution_id="action-1",
    )

    class Captures:
        async def get_verified_capture(self, *, capture_id: str):
            assert capture_id == capture.capture_id
            return SimpleNamespace(capture=capture)

    class Snapshots:
        async def read(self, *, capture: CaptureRef):
            assert capture is not None
            return _snapshot()

    reader = CaptureBackedRecoverySnapshotReader(
        captures=Captures(),
        snapshots=Snapshots(),
    )

    snapshot = await reader.read(
        task_id="task-1",
        session_id="session-1",
        surface_kind=SurfaceKind.ANDROID,
        capture_id="capture-restored",
    )

    assert snapshot.capture_id == "capture-restored"
