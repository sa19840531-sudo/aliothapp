"""V2 页面恢复、状态等价和补采来源等级测试。"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from services.api.app.application.restoration import (
    RestorationCoordinator,
    RestorationExecutionRequest,
    RestorationExecutionResult,
    new_running_restoration_attempt,
)
from services.api.app.application.state_equivalence import StateEquivalenceGate
from services.api.app.domain.inspection import (
    EvidenceReplayPolicy,
    EvidenceRequiredEquivalence,
    EvidenceSessionDependency,
    EvidenceSideEffectPolicy,
    EvidenceSourceGrade,
    RestorationAttempt,
    RestorationAttemptStatus,
    RestorationStrategy,
    RestorationValidationError,
    StateEquivalenceAssessment,
    StateInvariantSnapshot,
    build_evidence_request,
    build_restoration_plan,
)
from services.api.app.domain.interaction import SurfaceKind

NOW = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)


def _request(
    *,
    replay_policy: EvidenceReplayPolicy = EvidenceReplayPolicy.SAME_SURFACE_ONLY,
    required_equivalence: EvidenceRequiredEquivalence = (
        EvidenceRequiredEquivalence.SAME_SURFACE_CONTINUATION
    ),
    minimum_source_grade: EvidenceSourceGrade = EvidenceSourceGrade.A1,
):
    return build_evidence_request(
        work_item_id="inspection-work-1",
        attempt_id="inspection-attempt-1",
        task_id="task-1",
        rule_id="rule-1",
        rule_code="alioth_1",
        target_capture_id="capture-original",
        reason_code="TRUSTED_EVIDENCE_INVALID",
        evidence_goal="补采动作后证据",
        required_artifact_types=("screenshot", "action_trace"),
        session_dependency=EvidenceSessionDependency.ACTIVE_SESSION,
        replay_policy=replay_policy,
        required_equivalence=required_equivalence,
        side_effect_policy=EvidenceSideEffectPolicy.READ_ONLY,
        target_invariants={
            "environment": {"app_build": "2026.8.3"},
            "business": {"route": "/product/42"},
            "semantic": {"overlay": "none"},
            "structure": {"state_version": "v7"},
        },
        minimum_source_grade=minimum_source_grade,
        expires_at=NOW + timedelta(hours=1),
    )


def _snapshot(
    *,
    capture_id: str,
    session_id: str = "session-1",
    captured_at: datetime = NOW,
    route: str = "/product/42",
) -> StateInvariantSnapshot:
    return StateInvariantSnapshot(
        task_id="task-1",
        capture_id=capture_id,
        session_id=session_id,
        surface_kind=SurfaceKind.WEB,
        environment={"app_build": "2026.8.3"},
        business={"route": route},
        semantic={"overlay": "none"},
        structure={"state_version": "v7"},
        captured_at=captured_at,
    )


def _plan(*, request=None, strategy=RestorationStrategy.ORIGINAL_SURFACE_PIN):
    actual_request = request or _request()
    return build_restoration_plan(
        request=actual_request,
        source_session_id="session-1",
        target_session_id=(
            "session-2" if strategy is RestorationStrategy.NEW_SESSION_REPLAY else "session-1"
        ),
        surface_kind=SurfaceKind.WEB,
        strategy=strategy,
        replay_execution_plan_id=(
            "journey-plan-1"
            if strategy
            in {
                RestorationStrategy.SAME_CONTEXT_NEW_SURFACE,
                RestorationStrategy.SAME_APP_SESSION,
                RestorationStrategy.NEW_SESSION_REPLAY,
            }
            else None
        ),
        deadline=NOW + timedelta(minutes=30),
    )


def test_original_surface_pin_is_not_treated_as_replay() -> None:
    request = _request(replay_policy=EvidenceReplayPolicy.FORBIDDEN)

    plan = _plan(request=request)

    assert plan.source_grade is EvidenceSourceGrade.A1
    assert plan == type(plan).from_dict(plan.to_dict())
    with pytest.raises(RestorationValidationError, match="禁止重放"):
        _plan(
            request=request,
            strategy=RestorationStrategy.SAME_CONTEXT_NEW_SURFACE,
        )


def test_original_atomic_request_cannot_be_reconstructed() -> None:
    request = _request(
        replay_policy=EvidenceReplayPolicy.NEW_SESSION_ALLOWED,
        required_equivalence=EvidenceRequiredEquivalence.ORIGINAL_ATOMIC,
        minimum_source_grade=EvidenceSourceGrade.A0,
    )

    with pytest.raises(RestorationValidationError, match="original_atomic"):
        _plan(request=request)


def test_state_equivalence_requires_all_four_invariant_groups_and_grade() -> None:
    request = _request()
    plan = _plan(request=request)
    original = _snapshot(capture_id=request.target_capture_id)
    restored = _snapshot(
        capture_id="capture-restored",
        captured_at=NOW + timedelta(minutes=1),
    )

    assessment = StateEquivalenceGate(clock=lambda: NOW + timedelta(minutes=2)).assess(
        request=request,
        plan=plan,
        restoration_attempt_id="restoration-attempt-1",
        original=original,
        restored=restored,
    )

    assert assessment.passed is True
    assert {item.dimension.value for item in assessment.comparisons} == {
        "environment",
        "business",
        "semantic",
        "structure",
    }
    assert StateEquivalenceAssessment.from_dict(assessment.to_dict()) == assessment
    assert StateInvariantSnapshot.from_dict(restored.to_dict()) == restored

    divergent = StateEquivalenceGate(clock=lambda: NOW + timedelta(minutes=2)).assess(
        request=request,
        plan=plan,
        restoration_attempt_id="restoration-attempt-2",
        original=original,
        restored=_snapshot(
            capture_id="capture-divergent",
            captured_at=NOW + timedelta(minutes=1),
            route="/product/99",
        ),
    )
    assert divergent.passed is False
    assert any(item.key == "route" and item.matched is False for item in divergent.comparisons)


def test_state_equivalence_rejects_stale_or_reused_capture() -> None:
    request = _request()
    plan = _plan(request=request)
    original = _snapshot(capture_id=request.target_capture_id)
    gate = StateEquivalenceGate(clock=lambda: NOW)

    with pytest.raises(RestorationValidationError, match="新的 Capture"):
        gate.assess(
            request=request,
            plan=plan,
            restoration_attempt_id="restoration-attempt-1",
            original=original,
            restored=original,
        )
    with pytest.raises(RestorationValidationError, match="截止时间"):
        gate.assess(
            request=request,
            plan=plan,
            restoration_attempt_id="restoration-attempt-1",
            original=original,
            restored=_snapshot(
                capture_id="capture-late",
                captured_at=NOW + timedelta(minutes=31),
            ),
        )


class RecordingRestorationRepository:
    def __init__(self) -> None:
        self.completed: list[RestorationAttempt] = []

    async def start(self, *, plan, attempt_no):
        return new_running_restoration_attempt(
            plan=plan,
            attempt_no=attempt_no,
            started_at=NOW,
        )

    async def complete(self, *, attempt, expected_row_version):
        assert expected_row_version == 0
        persisted = replace(attempt, row_version=1)
        self.completed.append(persisted)
        return persisted


class RecordingRestorationExecutor:
    def __init__(self, result: RestorationExecutionResult) -> None:
        self.result = result
        self.requests: list[RestorationExecutionRequest] = []

    async def restore(self, request: RestorationExecutionRequest):
        self.requests.append(request)
        return self.result


async def test_restoration_coordinator_persists_equivalence_terminal() -> None:
    request = _request()
    plan = _plan(request=request)
    repository = RecordingRestorationRepository()
    executor = RecordingRestorationExecutor(
        RestorationExecutionResult(
            restored_snapshot=_snapshot(
                capture_id="capture-restored",
                captured_at=NOW + timedelta(minutes=1),
            ),
            action_execution_ids=("action-restore-1",),
        )
    )
    coordinator = RestorationCoordinator(
        repository=repository,
        executor=executor,
        equivalence_gate=StateEquivalenceGate(clock=lambda: NOW + timedelta(minutes=2)),
        clock=lambda: NOW + timedelta(minutes=2),
    )

    result = await coordinator.restore(
        request=request,
        plan=plan,
        attempt_no=1,
        original_snapshot=_snapshot(capture_id=request.target_capture_id),
    )

    assert result.status is RestorationAttemptStatus.EQUIVALENT
    assert result.row_version == 1
    assert result.assessment is not None and result.assessment.passed
    assert result.action_execution_ids == ("action-restore-1",)
    assert RestorationAttempt.from_dict(result.to_dict()) == result
    assert len(executor.requests) == 1


async def test_unfulfillable_plan_never_calls_surface_executor() -> None:
    request = _request()
    plan = _plan(request=request, strategy=RestorationStrategy.UNFULFILLABLE)
    repository = RecordingRestorationRepository()
    executor = RecordingRestorationExecutor(
        RestorationExecutionResult(
            restored_snapshot=_snapshot(
                capture_id="capture-unused",
                captured_at=NOW + timedelta(minutes=1),
            )
        )
    )
    coordinator = RestorationCoordinator(
        repository=repository,
        executor=executor,
        equivalence_gate=StateEquivalenceGate(clock=lambda: NOW),
        clock=lambda: NOW + timedelta(minutes=1),
    )

    result = await coordinator.restore(
        request=request,
        plan=plan,
        attempt_no=1,
        original_snapshot=_snapshot(capture_id=request.target_capture_id),
    )

    assert result.status is RestorationAttemptStatus.UNFULFILLABLE
    assert result.error_code == "RESTORATION_UNFULFILLABLE"
    assert executor.requests == []
