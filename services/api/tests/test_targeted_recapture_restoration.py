"""定向补采恢复、等价判断和 Probe 发布顺序测试。"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from services.api.app.application.evidence_requests import EvidenceRequestLeaseFence
from services.api.app.application.restoration import new_running_restoration_attempt
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
    build_restoration_plan,
)
from services.api.app.domain.interaction import SurfaceKind

NOW = datetime(2026, 9, 8, 15, tzinfo=UTC)


class RecordingCoordinator:
    def __init__(self, *, attempt, events: list[str]) -> None:  # noqa: ANN001
        self.attempt = attempt
        self.events = events
        self.calls = []

    async def restore(self, **kwargs):  # noqa: ANN003
        self.events.append("restore")
        self.calls.append(kwargs)
        return self.attempt


class RecordingPublisher:
    def __init__(self, *, events: list[str], result=None, error=None) -> None:  # noqa: ANN001
        self.events = events
        self.result = result
        self.error = error
        self.calls = []

    async def publish(self, **kwargs):  # noqa: ANN003
        self.events.append("publish")
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.result


def _facts():
    request = build_evidence_request(
        work_item_id="inspection-work-1",
        attempt_id="inspection-attempt-1",
        task_id="task-1",
        rule_id="rule-1",
        rule_code="rule_1",
        target_capture_id="capture-original",
        reason_code="TRUSTED_EVIDENCE_INVALID",
        evidence_goal="补采详情页截图",
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
        normalization_step_ids=("transition-1",),
        deadline=NOW + timedelta(minutes=5),
    )
    original = _snapshot(capture_id="capture-original", captured_at=NOW)
    restored = _snapshot(
        capture_id="capture-restored",
        captured_at=NOW + timedelta(seconds=1),
    )
    running = new_running_restoration_attempt(
        plan=plan,
        attempt_no=1,
        started_at=NOW,
    )
    assessment = StateEquivalenceGate(clock=lambda: NOW + timedelta(seconds=2)).assess(
        request=request,
        plan=plan,
        restoration_attempt_id=running.attempt_id,
        original=original,
        restored=restored,
    )
    equivalent = replace(
        running,
        status=RestorationAttemptStatus.EQUIVALENT,
        restored_capture_id=restored.capture_id,
        assessment=assessment,
        action_execution_ids=("action-restore-1",),
        finished_at=NOW + timedelta(seconds=2),
    )
    return request, plan, original, equivalent


def _snapshot(*, capture_id: str, captured_at: datetime):
    return StateInvariantSnapshot(
        task_id="task-1",
        capture_id=capture_id,
        session_id="session-1",
        surface_kind=SurfaceKind.ANDROID,
        environment={"app_build": "2026.9.8"},
        business={"screen_id": "detail"},
        semantic={"page_state": "stable"},
        structure={"state_version": "v1"},
        captured_at=captured_at,
    )


def _budget():
    return ExplorationBudget(
        max_actions=1,
        remaining_actions=1,
        max_depth=0,
        remaining_duration_ms=30_000,
    )


async def test_equivalent_restoration_publishes_probe_after_restore() -> None:
    request, plan, original, attempt = _facts()
    events: list[str] = []
    coordinator = RecordingCoordinator(attempt=attempt, events=events)
    publisher = RecordingPublisher(events=events, result="probe-work")
    service = TargetedRecaptureRestorationService(
        coordinator=coordinator,
        probe_publisher=publisher,
    )

    outcome = await service.restore_and_publish(
        request=request,
        plan=plan,
        original_snapshot=original,
        budget=_budget(),
        attempt_no=1,
    )

    assert events == ["restore", "publish"]
    assert outcome.restoration_attempt is attempt
    assert outcome.probe_work == "probe-work"
    assert publisher.calls[0]["restoration_attempt"] is attempt


async def test_equivalent_restoration_forwards_request_lease() -> None:
    request, plan, original, attempt = _facts()
    fence = EvidenceRequestLeaseFence(
        request_id=request.request_id,
        task_id=request.task_id,
        finalization_epoch=3,
        lease_owner="worker-1:app-recapture",
        lease_token="00000000-0000-0000-0000-000000000008",
        row_version=2,
    )
    publisher = RecordingPublisher(events=[], result="probe-work")
    service = TargetedRecaptureRestorationService(
        coordinator=RecordingCoordinator(attempt=attempt, events=[]),
        probe_publisher=publisher,
    )

    await service.restore_and_publish(
        request=request,
        plan=plan,
        original_snapshot=original,
        budget=_budget(),
        attempt_no=1,
        request_lease=fence,
    )

    assert publisher.calls[0]["request_lease"] is fence


async def test_failed_restoration_does_not_publish_probe() -> None:
    request, plan, original, _ = _facts()
    failed = replace(
        new_running_restoration_attempt(
            plan=plan,
            attempt_no=1,
            started_at=NOW,
        ),
        status=RestorationAttemptStatus.FAILED,
        error_code="RECOVERY_SOURCE_STATE_DRIFTED",
        finished_at=NOW + timedelta(seconds=2),
    )
    events: list[str] = []
    publisher = RecordingPublisher(events=events)
    service = TargetedRecaptureRestorationService(
        coordinator=RecordingCoordinator(attempt=failed, events=events),
        probe_publisher=publisher,
    )

    outcome = await service.restore_and_publish(
        request=request,
        plan=plan,
        original_snapshot=original,
        budget=_budget(),
        attempt_no=1,
    )

    assert events == ["restore"]
    assert outcome.restoration_attempt is failed
    assert outcome.probe_work is None
    assert publisher.calls == []


async def test_probe_publication_failure_preserves_completed_restoration() -> None:
    request, plan, original, attempt = _facts()
    events: list[str] = []
    publisher = RecordingPublisher(
        events=events,
        error=RuntimeError("publisher unavailable"),
    )
    service = TargetedRecaptureRestorationService(
        coordinator=RecordingCoordinator(attempt=attempt, events=events),
        probe_publisher=publisher,
    )

    with pytest.raises(RuntimeError, match="publisher unavailable"):
        await service.restore_and_publish(
            request=request,
            plan=plan,
            original_snapshot=original,
            budget=_budget(),
            attempt_no=1,
        )

    assert events == ["restore", "publish"]
    assert attempt.status is RestorationAttemptStatus.EQUIVALENT


async def test_existing_equivalent_attempt_publishes_without_restoration() -> None:
    request, _, _, attempt = _facts()
    events: list[str] = []
    coordinator = RecordingCoordinator(attempt=attempt, events=events)
    publisher = RecordingPublisher(events=events, result="probe-work")
    service = TargetedRecaptureRestorationService(
        coordinator=coordinator,
        probe_publisher=publisher,
    )

    outcome = await service.publish_equivalent(
        request=request,
        restoration_attempt=attempt,
        budget=_budget(),
    )

    assert events == ["publish"]
    assert coordinator.calls == []
    assert outcome.restoration_attempt is attempt
    assert outcome.probe_work == "probe-work"
