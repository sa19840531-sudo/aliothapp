"""等价恢复完成后发布 Probe Work 的应用层合同测试。"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from services.api.app.application.evidence_requests import EvidenceRequestLeaseFence
from services.api.app.application.restoration import new_running_restoration_attempt
from services.api.app.application.restored_probe_publication import (
    RestoredProbePublicationError,
    RestoredProbePublicationService,
)
from services.api.app.application.state_equivalence import StateEquivalenceGate
from services.api.app.domain.exploration import (
    ExplorationBudget,
    build_probe_work,
)
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

NOW = datetime(2026, 9, 8, 12, tzinfo=UTC)


class RecordingOrchestrator:
    def __init__(self) -> None:
        self.commands = []

    async def route_evidence_request(self, command):  # noqa: ANN001
        self.commands.append(command)
        return build_probe_work(
            session_id=command.session_id,
            branch_id=command.branch_id,
            surface_kind=command.surface_kind,
            source_capture_id=command.source_capture_id,
            restoration_plan_hash=command.restoration_plan_hash,
            restoration_attempt_id=command.restoration_attempt_id,
            equivalence_assessment_id=command.equivalence_assessment_id,
            source_grade=command.source_grade,
            evidence_request=command.evidence_request,
            budget=command.budget,
            deadline=command.deadline,
            restoration_plan=command.restoration_plan,
        )


def _request(*, active_session: bool = False):
    return build_evidence_request(
        work_item_id="inspection-work-1",
        attempt_id="inspection-attempt-1",
        task_id="task-1",
        rule_id="rule-1",
        rule_code="rule_1",
        target_capture_id="capture-original",
        reason_code="TRUSTED_EVIDENCE_INVALID",
        evidence_goal="补采详情页截图",
        required_artifact_types=("screenshot",),
        session_dependency=(
            EvidenceSessionDependency.ACTIVE_SESSION
            if active_session
            else EvidenceSessionDependency.SNAPSHOT
        ),
        replay_policy=EvidenceReplayPolicy.NEW_SESSION_ALLOWED,
        required_equivalence=EvidenceRequiredEquivalence.LOGICAL_EQUIVALENT,
        side_effect_policy=EvidenceSideEffectPolicy.READ_ONLY,
        target_invariants={
            "environment": {"app_build": "2026.9.8"},
            "business": {"screen_id": "detail"},
            "semantic": {"page_state": "stable"},
            "structure": {"state_version": "v1"},
        },
        minimum_source_grade=EvidenceSourceGrade.C,
        expires_at=NOW + timedelta(minutes=10),
    )


def _snapshot(*, capture_id: str, session_id: str, captured_at: datetime):
    return StateInvariantSnapshot(
        task_id="task-1",
        capture_id=capture_id,
        session_id=session_id,
        surface_kind=SurfaceKind.ANDROID,
        environment={"app_build": "2026.9.8"},
        business={"screen_id": "detail"},
        semantic={"page_state": "stable"},
        structure={"state_version": "v1"},
        captured_at=captured_at,
    )


def _equivalent_attempt(*, active_session: bool = False):
    request = _request(active_session=active_session)
    target_session_id = "session-original" if active_session else "session-restored"
    plan = build_restoration_plan(
        request=request,
        source_session_id="session-original",
        target_session_id=target_session_id,
        surface_kind=SurfaceKind.ANDROID,
        strategy=(
            RestorationStrategy.SAME_APP_SESSION
            if active_session
            else RestorationStrategy.NEW_SESSION_REPLAY
        ),
        replay_execution_plan_id="page-graph-route:route-1",
        normalization_step_ids=("transition-1",),
        deadline=NOW + timedelta(minutes=5),
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
        original=_snapshot(
            capture_id=request.target_capture_id,
            session_id=plan.source_session_id,
            captured_at=NOW - timedelta(seconds=1),
        ),
        restored=_snapshot(
            capture_id="capture-restored",
            session_id=target_session_id,
            captured_at=NOW + timedelta(seconds=1),
        ),
    )
    return request, replace(
        running,
        status=RestorationAttemptStatus.EQUIVALENT,
        restored_capture_id="capture-restored",
        assessment=assessment,
        action_execution_ids=("action-restore-1",),
        finished_at=NOW + timedelta(seconds=2),
    )


async def test_publish_uses_equivalent_restored_capture_and_target_session() -> None:
    request, attempt = _equivalent_attempt()
    orchestrator = RecordingOrchestrator()
    service = RestoredProbePublicationService(
        orchestrator=orchestrator,
        clock=lambda: NOW + timedelta(seconds=3),
    )

    probe = await service.publish(
        request=request,
        restoration_attempt=attempt,
        budget=ExplorationBudget(
            max_actions=1,
            remaining_actions=1,
            max_depth=0,
            remaining_duration_ms=30_000,
        ),
    )

    command = orchestrator.commands[0]
    assert command.source_capture_id == "capture-restored"
    assert command.session_id == "session-restored"
    assert command.restoration_attempt_id == attempt.attempt_id
    assert command.equivalence_assessment_id == attempt.assessment.assessment_id
    assert command.restoration_plan is attempt.plan
    assert probe.source_capture_id == "capture-restored"
    assert probe.session_id == "session-restored"


@pytest.mark.parametrize("h5_snapshot", [False, True])
async def test_publish_passes_claim_lease_to_orchestrator(h5_snapshot) -> None:
    request, attempt = _equivalent_attempt(active_session=not h5_snapshot)
    if h5_snapshot:
        plan = build_restoration_plan(
            request=request,
            source_session_id="h5:run-1",
            target_session_id="h5:run-1",
            surface_kind=SurfaceKind.WEB,
            strategy=RestorationStrategy.ORIGINAL_SURFACE_PIN,
            deadline=NOW + timedelta(minutes=5),
        )
        running = new_running_restoration_attempt(plan=plan, attempt_no=1, started_at=NOW)
        assessment = StateEquivalenceGate(clock=lambda: NOW + timedelta(seconds=2)).assess(
            request=request,
            plan=plan,
            restoration_attempt_id=running.attempt_id,
            original=replace(
                _snapshot(
                    capture_id=request.target_capture_id,
                    session_id=plan.source_session_id,
                    captured_at=NOW - timedelta(seconds=1),
                ),
                surface_kind=SurfaceKind.WEB,
            ),
            restored=replace(
                _snapshot(
                    capture_id="capture-restored",
                    session_id=plan.target_session_id,
                    captured_at=NOW + timedelta(seconds=1),
                ),
                surface_kind=SurfaceKind.WEB,
            ),
        )
        attempt = replace(
            running,
            status=RestorationAttemptStatus.EQUIVALENT,
            restored_capture_id="capture-restored",
            assessment=assessment,
            action_execution_ids=("action-restore-1",),
            finished_at=NOW + timedelta(seconds=2),
        )
    fence = EvidenceRequestLeaseFence(
        request_id=request.request_id,
        task_id=request.task_id,
        finalization_epoch=3,
        lease_owner="worker-1:app-recapture",
        lease_token="00000000-0000-0000-0000-000000000008",
        row_version=2,
    )
    orchestrator = RecordingOrchestrator()
    service = RestoredProbePublicationService(
        orchestrator=orchestrator,
        clock=lambda: NOW + timedelta(seconds=3),
    )

    await service.publish(
        request=request,
        restoration_attempt=attempt,
        budget=ExplorationBudget(
            max_actions=1,
            remaining_actions=1,
            max_depth=0,
            remaining_duration_ms=30_000,
        ),
        request_lease=fence,
    )

    assert orchestrator.commands[0].request_lease is fence
    if h5_snapshot:
        command = orchestrator.commands[0]
        for changes in (
            {"surface_kind": SurfaceKind.ANDROID},
            {"session_id": "web:run-1"},
            {"restoration_plan": None},
            {"session_id": "h5:other-run"},
        ):
            with pytest.raises(ValueError, match="active_session"):
                replace(command, **changes)


async def test_publish_rejects_non_equivalent_restoration() -> None:
    request, attempt = _equivalent_attempt()
    orchestrator = RecordingOrchestrator()
    service = RestoredProbePublicationService(orchestrator=orchestrator, clock=lambda: NOW)
    failed = replace(
        new_running_restoration_attempt(
            plan=attempt.plan,
            attempt_no=1,
            started_at=NOW,
        ),
        status=RestorationAttemptStatus.FAILED,
        error_code="RESTORATION_FAILED",
        finished_at=NOW + timedelta(seconds=2),
    )

    with pytest.raises(RestoredProbePublicationError) as error:
        await service.publish(
            request=request,
            restoration_attempt=failed,
            budget=ExplorationBudget(
                max_actions=1,
                remaining_actions=1,
                max_depth=0,
                remaining_duration_ms=30_000,
            ),
        )

    assert error.value.reason_code == "RESTORATION_NOT_EQUIVALENT"
    assert orchestrator.commands == []


async def test_publish_rejects_expired_restoration_without_side_effect() -> None:
    request, attempt = _equivalent_attempt()
    orchestrator = RecordingOrchestrator()
    service = RestoredProbePublicationService(
        orchestrator=orchestrator,
        clock=lambda: attempt.plan.deadline,
    )

    with pytest.raises(RestoredProbePublicationError) as error:
        await service.publish(
            request=request,
            restoration_attempt=attempt,
            budget=ExplorationBudget(
                max_actions=1,
                remaining_actions=1,
                max_depth=0,
                remaining_duration_ms=30_000,
            ),
        )

    assert error.value.reason_code == "RESTORATION_RESULT_EXPIRED"
    assert orchestrator.commands == []


async def test_publish_rejects_request_identity_drift_without_side_effect() -> None:
    _, attempt = _equivalent_attempt()
    other_request = build_evidence_request(
        work_item_id="inspection-work-2",
        attempt_id="inspection-attempt-2",
        task_id="task-1",
        rule_id="rule-2",
        rule_code="rule_2",
        target_capture_id="capture-original",
        reason_code="TRUSTED_EVIDENCE_INVALID",
        evidence_goal="补采另一个规则证据",
        required_artifact_types=("screenshot",),
        session_dependency=EvidenceSessionDependency.SNAPSHOT,
        replay_policy=EvidenceReplayPolicy.NEW_SESSION_ALLOWED,
        required_equivalence=EvidenceRequiredEquivalence.LOGICAL_EQUIVALENT,
        side_effect_policy=EvidenceSideEffectPolicy.READ_ONLY,
        target_invariants={"business": {"screen_id": "detail"}},
        minimum_source_grade=EvidenceSourceGrade.C,
        expires_at=NOW + timedelta(minutes=10),
    )
    orchestrator = RecordingOrchestrator()
    service = RestoredProbePublicationService(orchestrator=orchestrator, clock=lambda: NOW)

    with pytest.raises(RestoredProbePublicationError) as error:
        await service.publish(
            request=other_request,
            restoration_attempt=attempt,
            budget=ExplorationBudget(
                max_actions=1,
                remaining_actions=1,
                max_depth=0,
                remaining_duration_ms=30_000,
            ),
        )

    assert error.value.reason_code == "RESTORATION_RESULT_IDENTITY_MISMATCH"
    assert orchestrator.commands == []
