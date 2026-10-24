"""App Evidence Request 租约消费者测试。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from services.api.app.application.app_targeted_recapture_consumer import (
    AppTargetedRecaptureConsumer,
    AppTargetedRecaptureDisposition,
)
from services.api.app.application.app_targeted_recapture_planning import (
    AppTargetedRecapturePlanResolution,
)
from services.api.app.application.evidence_requests import (
    EvidenceRequestClaimDisposition,
    EvidenceRequestClaimResult,
    EvidenceRequestLeaseReleaseDisposition,
    EvidenceRequestLeaseReleaseResult,
    EvidenceRequestProcessingLease,
    EvidenceRequestStatus,
)
from services.api.app.application.restored_probe_publication import (
    RestoredProbePublicationError,
)
from services.api.app.application.supplemental_evidence_policy import (
    SupplementalEvidenceNotReadyError,
    SupplementalEvidencePolicy,
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
    build_evidence_request,
    build_restoration_plan,
)
from services.api.app.domain.interaction import SurfaceKind

NOW = datetime(2026, 9, 8, 18, tzinfo=UTC)


def _request():
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
        session_dependency=EvidenceSessionDependency.ACTIVE_SESSION,
        replay_policy=EvidenceReplayPolicy.SAME_CONTEXT_ALLOWED,
        required_equivalence=EvidenceRequiredEquivalence.LOGICAL_EQUIVALENT,
        side_effect_policy=EvidenceSideEffectPolicy.READ_ONLY,
        target_invariants={"business": {"page_type": "content_detail"}},
        minimum_source_grade=EvidenceSourceGrade.B,
        expires_at=NOW + timedelta(minutes=10),
    )


def _lease() -> EvidenceRequestProcessingLease:
    return EvidenceRequestProcessingLease(
        request=_request(),
        lease_owner="worker-1:app-recapture",
        lease_token="00000000-0000-0000-0000-000000000008",
        finalization_epoch=3,
        row_version=2,
        attempt_count=2,
        max_attempts=3,
    )


def _budget(
    *,
    remaining_actions: int = 2,
    max_actions: int = 2,
    remaining_duration_ms: int = 60_000,
) -> ExplorationBudget:
    return ExplorationBudget(
        max_actions=max_actions,
        remaining_actions=remaining_actions,
        max_depth=1,
        remaining_duration_ms=remaining_duration_ms,
    )


def _consumer(*, repository, runtime, policy=None, clock=None, runtime_factory=None):  # noqa: ANN001
    return AppTargetedRecaptureConsumer(
        requests=repository,
        runtime_factory=runtime_factory or SimpleNamespace(build=lambda **_: runtime),
        policy=policy or SupplementalEvidencePolicy(enabled=True, runtime_ready=True),
        lease_owner="worker-1:app-recapture",
        lease_seconds=60,
        retry_delay_seconds=5,
        clock=clock or (lambda: NOW),
    )


async def test_consumer_resumes_probe_publication_before_planning_or_surface_access() -> None:
    lease = _lease()
    repository = SimpleNamespace(
        claim_next=AsyncMock(
            return_value=EvidenceRequestClaimResult(
                disposition=EvidenceRequestClaimDisposition.CLAIMED,
                lease=lease,
            )
        ),
        release_lease=AsyncMock(),
        settle_lease=AsyncMock(),
    )
    runtime = SimpleNamespace(
        resume_equivalent_publication=AsyncMock(
            return_value=SimpleNamespace(probe_work=SimpleNamespace(work_id="probe-1"))
        ),
        resolve_plan=AsyncMock(),
        restore_and_publish=AsyncMock(),
    )

    outcome = await _consumer(repository=repository, runtime=runtime).consume_next(
        task_id=lease.request.task_id,
        agent_run_id="run-1",
        finalization_epoch=3,
        budget=_budget(),
    )

    assert outcome.disposition is AppTargetedRecaptureDisposition.PROBE_PUBLISHED
    assert outcome.request_id == lease.request.request_id
    runtime.resume_equivalent_publication.assert_awaited_once_with(
        request=lease.request,
        budget=_budget(),
        request_lease=lease.fence,
    )
    runtime.resolve_plan.assert_not_awaited()
    runtime.restore_and_publish.assert_not_awaited()
    repository.release_lease.assert_not_awaited()
    repository.settle_lease.assert_not_awaited()


async def test_consumer_settles_after_slow_claim_without_building_runtime() -> None:
    lease = _lease()
    current_time = [NOW]

    async def claim_next(**_: object) -> EvidenceRequestClaimResult:
        current_time[0] = NOW + timedelta(seconds=1)
        return EvidenceRequestClaimResult(
            disposition=EvidenceRequestClaimDisposition.CLAIMED,
            lease=lease,
        )

    repository = SimpleNamespace(
        claim_next=claim_next,
        release_lease=AsyncMock(),
        settle_lease=AsyncMock(return_value=3),
    )
    factory = SimpleNamespace(build=Mock())
    consumer = _consumer(
        repository=repository,
        runtime=SimpleNamespace(),
        clock=lambda: current_time[0],
        runtime_factory=factory,
    )

    outcome = await consumer.consume_next(
        task_id=lease.request.task_id,
        agent_run_id="run-1",
        finalization_epoch=3,
        budget=_budget(remaining_duration_ms=1_000),
    )

    assert outcome.disposition is AppTargetedRecaptureDisposition.UNFULFILLABLE
    assert outcome.reason_code == "RECAPTURE_DEADLINE_EXHAUSTED"
    command = repository.settle_lease.await_args.kwargs["command"]
    assert command.target_status is EvidenceRequestStatus.UNFULFILLABLE
    assert command.resolution_reason == "RECAPTURE_DEADLINE_EXHAUSTED"
    factory.build.assert_not_called()


async def test_consumer_settles_after_slow_resume_without_planning() -> None:
    lease = _lease()
    current_time = [NOW]
    repository = SimpleNamespace(
        claim_next=AsyncMock(
            return_value=EvidenceRequestClaimResult(
                disposition=EvidenceRequestClaimDisposition.CLAIMED,
                lease=lease,
            )
        ),
        release_lease=AsyncMock(),
        settle_lease=AsyncMock(return_value=3),
    )

    async def resume(**_: object) -> None:
        current_time[0] = NOW + timedelta(seconds=1)
        return None

    runtime = SimpleNamespace(
        resume_equivalent_publication=resume,
        resolve_plan=AsyncMock(),
        restore_and_publish=AsyncMock(),
    )
    consumer = _consumer(
        repository=repository,
        runtime=runtime,
        clock=lambda: current_time[0],
    )

    outcome = await consumer.consume_next(
        task_id=lease.request.task_id,
        agent_run_id="run-1",
        finalization_epoch=3,
        budget=_budget(remaining_duration_ms=1_000),
    )

    assert outcome.disposition is AppTargetedRecaptureDisposition.UNFULFILLABLE
    assert outcome.reason_code == "RECAPTURE_DEADLINE_EXHAUSTED"
    runtime.resolve_plan.assert_not_awaited()
    runtime.restore_and_publish.assert_not_awaited()


async def test_consumer_settles_after_slow_plan_before_restore() -> None:
    lease = _lease()
    request = lease.request
    plan = build_restoration_plan(
        request=request,
        source_session_id="session-1",
        target_session_id="session-1",
        surface_kind=SurfaceKind.ANDROID,
        strategy=RestorationStrategy.ORIGINAL_SURFACE_PIN,
        deadline=NOW + timedelta(minutes=5),
    )
    current_time = [NOW]
    repository = SimpleNamespace(
        claim_next=AsyncMock(
            return_value=EvidenceRequestClaimResult(
                disposition=EvidenceRequestClaimDisposition.CLAIMED,
                lease=lease,
            )
        ),
        release_lease=AsyncMock(),
        settle_lease=AsyncMock(return_value=3),
    )

    async def resolve_plan(**_: object) -> AppTargetedRecapturePlanResolution:
        current_time[0] = NOW + timedelta(seconds=1)
        return AppTargetedRecapturePlanResolution(plan=plan)

    runtime = SimpleNamespace(
        resume_equivalent_publication=AsyncMock(return_value=None),
        resolve_plan=resolve_plan,
        restore_and_publish=AsyncMock(),
    )
    consumer = _consumer(
        repository=repository,
        runtime=runtime,
        clock=lambda: current_time[0],
    )

    outcome = await consumer.consume_next(
        task_id=request.task_id,
        agent_run_id="run-1",
        finalization_epoch=3,
        budget=_budget(remaining_duration_ms=1_000),
    )

    assert outcome.disposition is AppTargetedRecaptureDisposition.UNFULFILLABLE
    assert outcome.reason_code == "RECAPTURE_DEADLINE_EXHAUSTED"
    runtime.restore_and_publish.assert_not_awaited()


async def test_consumer_zero_duration_settles_without_building_runtime() -> None:
    lease = _lease()
    repository = SimpleNamespace(
        claim_next=AsyncMock(
            return_value=EvidenceRequestClaimResult(
                disposition=EvidenceRequestClaimDisposition.CLAIMED,
                lease=lease,
            )
        ),
        release_lease=AsyncMock(),
        settle_lease=AsyncMock(return_value=3),
    )
    factory = SimpleNamespace(build=Mock())
    consumer = _consumer(
        repository=repository,
        runtime=SimpleNamespace(),
        runtime_factory=factory,
    )

    outcome = await consumer.consume_next(
        task_id=lease.request.task_id,
        agent_run_id="run-1",
        finalization_epoch=3,
        budget=_budget(remaining_duration_ms=0),
    )

    assert outcome.disposition is AppTargetedRecaptureDisposition.UNFULFILLABLE
    assert outcome.reason_code == "RECAPTURE_DEADLINE_EXHAUSTED"
    factory.build.assert_not_called()


@pytest.mark.parametrize("obstruction_balance", [0, 5])
async def test_consumer_settles_request_without_publishing_when_no_action_remains(
    obstruction_balance: int,
) -> None:
    lease = _lease()
    repository = SimpleNamespace(
        claim_next=AsyncMock(
            return_value=EvidenceRequestClaimResult(
                disposition=EvidenceRequestClaimDisposition.CLAIMED,
                lease=lease,
            )
        ),
        release_lease=AsyncMock(),
        settle_lease=AsyncMock(return_value=3),
    )
    runtime = SimpleNamespace(
        resume_equivalent_publication=AsyncMock(),
        resolve_plan=AsyncMock(),
        restore_and_publish=AsyncMock(),
    )

    outcome = await _consumer(repository=repository, runtime=runtime).consume_next(
        task_id=lease.request.task_id,
        agent_run_id="run-1",
        finalization_epoch=3,
        budget=ExplorationBudget(
            max_actions=2 + obstruction_balance,
            remaining_actions=obstruction_balance,
            max_depth=2,
            remaining_duration_ms=60_000,
            max_business_actions=2,
            remaining_business_actions=0,
            max_obstruction_actions=obstruction_balance,
            remaining_obstruction_actions=obstruction_balance,
        ),
    )

    assert outcome.disposition is AppTargetedRecaptureDisposition.UNFULFILLABLE
    command = repository.settle_lease.await_args.kwargs["command"]
    assert command.resolution_reason == "RECAPTURE_ACTION_BUDGET_EXHAUSTED"
    runtime.resume_equivalent_publication.assert_not_awaited()
    runtime.resolve_plan.assert_not_awaited()
    runtime.restore_and_publish.assert_not_awaited()


async def test_consumer_restores_with_claim_attempt_when_no_equivalent_result() -> None:
    lease = _lease()
    request = lease.request
    plan = build_restoration_plan(
        request=request,
        source_session_id="session-1",
        target_session_id="session-1",
        surface_kind=SurfaceKind.ANDROID,
        strategy=RestorationStrategy.ORIGINAL_SURFACE_PIN,
        deadline=NOW + timedelta(minutes=5),
    )
    repository = SimpleNamespace(
        claim_next=AsyncMock(
            return_value=EvidenceRequestClaimResult(
                disposition=EvidenceRequestClaimDisposition.CLAIMED,
                lease=lease,
            )
        ),
        release_lease=AsyncMock(),
        settle_lease=AsyncMock(),
    )
    runtime = SimpleNamespace(
        resume_equivalent_publication=AsyncMock(return_value=None),
        resolve_plan=AsyncMock(return_value=AppTargetedRecapturePlanResolution(plan=plan)),
        restore_and_publish=AsyncMock(
            return_value=SimpleNamespace(
                restoration_attempt=SimpleNamespace(status=RestorationAttemptStatus.EQUIVALENT),
                probe_work=SimpleNamespace(work_id="probe-1"),
            )
        ),
    )

    outcome = await _consumer(repository=repository, runtime=runtime).consume_next(
        task_id=request.task_id,
        agent_run_id="run-1",
        finalization_epoch=3,
        budget=_budget(),
    )

    assert outcome.disposition is AppTargetedRecaptureDisposition.PROBE_PUBLISHED
    runtime.restore_and_publish.assert_awaited_once()
    restore_kwargs = runtime.restore_and_publish.await_args.kwargs
    assert restore_kwargs["request"] == request
    assert restore_kwargs["budget"] == _budget()
    assert restore_kwargs["attempt_no"] == lease.attempt_count
    assert restore_kwargs["request_lease"] == lease.fence
    tightened_plan = restore_kwargs["plan"]
    assert tightened_plan.deadline == NOW + timedelta(seconds=60)
    assert tightened_plan.plan_id == f"restoration-plan:{tightened_plan.plan_hash}"
    assert tightened_plan.plan_id != plan.plan_id


async def test_consumer_passes_effective_deadline_and_elapsed_budget_to_restore() -> None:
    lease = _lease()
    request = lease.request
    plan = build_restoration_plan(
        request=request,
        source_session_id="session-1",
        target_session_id="session-1",
        surface_kind=SurfaceKind.ANDROID,
        strategy=RestorationStrategy.SAME_APP_SESSION,
        replay_execution_plan_id="route-1",
        normalization_step_ids=("transition-1",),
        deadline=NOW + timedelta(minutes=5),
    )
    current_time = [NOW]
    repository = SimpleNamespace(
        claim_next=AsyncMock(
            return_value=EvidenceRequestClaimResult(
                disposition=EvidenceRequestClaimDisposition.CLAIMED,
                lease=lease,
            )
        ),
        release_lease=AsyncMock(),
        settle_lease=AsyncMock(),
    )

    async def resolve_plan(**_: object) -> AppTargetedRecapturePlanResolution:
        current_time[0] = NOW + timedelta(milliseconds=250)
        return AppTargetedRecapturePlanResolution(plan=plan)

    runtime = SimpleNamespace(
        resume_equivalent_publication=AsyncMock(return_value=None),
        resolve_plan=resolve_plan,
        restore_and_publish=AsyncMock(
            return_value=SimpleNamespace(
                restoration_attempt=SimpleNamespace(status=RestorationAttemptStatus.EQUIVALENT),
                probe_work=SimpleNamespace(work_id="probe-1"),
            )
        ),
    )
    consumer = _consumer(
        repository=repository,
        runtime=runtime,
        clock=lambda: current_time[0],
    )

    outcome = await consumer.consume_next(
        task_id=request.task_id,
        agent_run_id="run-1",
        finalization_epoch=3,
        budget=_budget(remaining_duration_ms=1_000),
        deadline=NOW + timedelta(milliseconds=800),
    )

    assert outcome.disposition is AppTargetedRecaptureDisposition.PROBE_PUBLISHED
    restore_kwargs = runtime.restore_and_publish.await_args.kwargs
    passed_plan = restore_kwargs["plan"]
    assert passed_plan.deadline == NOW + timedelta(milliseconds=800)
    assert passed_plan.replay_execution_plan_id == plan.replay_execution_plan_id
    assert passed_plan.normalization_step_ids == plan.normalization_step_ids
    assert passed_plan.plan_id == f"restoration-plan:{passed_plan.plan_hash}"
    assert passed_plan.plan_id != plan.plan_id
    assert restore_kwargs["budget"].remaining_duration_ms == 550
    assert restore_kwargs["budget"].remaining_actions == 1


async def test_consumer_allows_already_resumed_result_after_deadline() -> None:
    lease = _lease()
    current_time = [NOW]
    repository = SimpleNamespace(
        claim_next=AsyncMock(
            return_value=EvidenceRequestClaimResult(
                disposition=EvidenceRequestClaimDisposition.CLAIMED,
                lease=lease,
            )
        ),
        release_lease=AsyncMock(),
        settle_lease=AsyncMock(),
    )

    async def resume(**_: object) -> SimpleNamespace:
        current_time[0] = NOW + timedelta(seconds=1)
        return SimpleNamespace(probe_work=SimpleNamespace(work_id="probe-1"))

    runtime = SimpleNamespace(
        resume_equivalent_publication=resume,
        resolve_plan=AsyncMock(),
        restore_and_publish=AsyncMock(),
    )
    consumer = _consumer(
        repository=repository,
        runtime=runtime,
        clock=lambda: current_time[0],
    )

    outcome = await consumer.consume_next(
        task_id=lease.request.task_id,
        agent_run_id="run-1",
        finalization_epoch=3,
        budget=_budget(remaining_duration_ms=1_000),
    )

    assert outcome.disposition is AppTargetedRecaptureDisposition.PROBE_PUBLISHED
    repository.settle_lease.assert_not_awaited()
    runtime.resolve_plan.assert_not_awaited()
    runtime.restore_and_publish.assert_not_awaited()


async def test_consumer_debits_restoration_actions_before_publishing_probe() -> None:
    lease = _lease()
    request = lease.request
    plan = build_restoration_plan(
        request=request,
        source_session_id="session-1",
        target_session_id="session-1",
        surface_kind=SurfaceKind.ANDROID,
        strategy=RestorationStrategy.ORIGINAL_SURFACE_PIN,
        normalization_step_ids=("transition-1",),
        deadline=NOW + timedelta(minutes=5),
    )
    repository = SimpleNamespace(
        claim_next=AsyncMock(
            return_value=EvidenceRequestClaimResult(
                disposition=EvidenceRequestClaimDisposition.CLAIMED,
                lease=lease,
            )
        ),
        release_lease=AsyncMock(),
        settle_lease=AsyncMock(),
    )
    runtime = SimpleNamespace(
        resume_equivalent_publication=AsyncMock(return_value=None),
        resolve_plan=AsyncMock(return_value=AppTargetedRecapturePlanResolution(plan=plan)),
        restore_and_publish=AsyncMock(
            return_value=SimpleNamespace(
                restoration_attempt=SimpleNamespace(status=RestorationAttemptStatus.EQUIVALENT),
                probe_work=SimpleNamespace(work_id="probe-1"),
            )
        ),
    )
    budget = _budget(remaining_actions=3, max_actions=3)

    outcome = await _consumer(repository=repository, runtime=runtime).consume_next(
        task_id=request.task_id,
        agent_run_id="run-1",
        finalization_epoch=3,
        budget=budget,
    )

    published_budget = runtime.restore_and_publish.await_args.kwargs["budget"]
    assert published_budget.remaining_actions == 2
    assert outcome.consumed_action_count == 1


async def test_consumer_rejects_plan_when_restoration_and_probe_exceed_budget() -> None:
    lease = _lease()
    request = lease.request
    plan = build_restoration_plan(
        request=request,
        source_session_id="session-1",
        target_session_id="session-1",
        surface_kind=SurfaceKind.ANDROID,
        strategy=RestorationStrategy.ORIGINAL_SURFACE_PIN,
        normalization_step_ids=("transition-1",),
        deadline=NOW + timedelta(minutes=5),
    )
    repository = SimpleNamespace(
        claim_next=AsyncMock(
            return_value=EvidenceRequestClaimResult(
                disposition=EvidenceRequestClaimDisposition.CLAIMED,
                lease=lease,
            )
        ),
        release_lease=AsyncMock(),
        settle_lease=AsyncMock(return_value=3),
    )
    runtime = SimpleNamespace(
        resume_equivalent_publication=AsyncMock(return_value=None),
        resolve_plan=AsyncMock(return_value=AppTargetedRecapturePlanResolution(plan=plan)),
        restore_and_publish=AsyncMock(),
    )

    outcome = await _consumer(repository=repository, runtime=runtime).consume_next(
        task_id=request.task_id,
        agent_run_id="run-1",
        finalization_epoch=3,
        budget=_budget(remaining_actions=1),
    )

    assert outcome.disposition is AppTargetedRecaptureDisposition.UNFULFILLABLE
    command = repository.settle_lease.await_args.kwargs["command"]
    assert command.resolution_reason == "RECAPTURE_ACTION_BUDGET_EXHAUSTED"
    runtime.restore_and_publish.assert_not_awaited()


async def test_consumer_settles_deterministic_unfulfillable_plan() -> None:
    lease = _lease()
    request = lease.request
    plan = build_restoration_plan(
        request=request,
        source_session_id="session-1",
        target_session_id="session-1",
        surface_kind=SurfaceKind.ANDROID,
        strategy=RestorationStrategy.UNFULFILLABLE,
        deadline=NOW + timedelta(minutes=5),
    )
    repository = SimpleNamespace(
        claim_next=AsyncMock(
            return_value=EvidenceRequestClaimResult(
                disposition=EvidenceRequestClaimDisposition.CLAIMED,
                lease=lease,
            )
        ),
        release_lease=AsyncMock(),
        settle_lease=AsyncMock(return_value=3),
    )
    runtime = SimpleNamespace(
        resume_equivalent_publication=AsyncMock(return_value=None),
        resolve_plan=AsyncMock(
            return_value=AppTargetedRecapturePlanResolution(
                plan=plan,
                reason_code="TARGET_GRAPH_STATE_UNAVAILABLE",
            )
        ),
        restore_and_publish=AsyncMock(),
    )

    outcome = await _consumer(repository=repository, runtime=runtime).consume_next(
        task_id=request.task_id,
        agent_run_id="run-1",
        finalization_epoch=3,
        budget=_budget(),
    )

    assert outcome.disposition is AppTargetedRecaptureDisposition.UNFULFILLABLE
    command = repository.settle_lease.await_args.kwargs["command"]
    assert command.target_status is EvidenceRequestStatus.UNFULFILLABLE
    assert command.resolution_reason == "TARGET_GRAPH_STATE_UNAVAILABLE"
    runtime.restore_and_publish.assert_not_awaited()


async def test_consumer_releases_transient_plan_failure_for_delayed_retry() -> None:
    lease = _lease()
    repository = SimpleNamespace(
        claim_next=AsyncMock(
            return_value=EvidenceRequestClaimResult(
                disposition=EvidenceRequestClaimDisposition.CLAIMED,
                lease=lease,
            )
        ),
        release_lease=AsyncMock(
            return_value=EvidenceRequestLeaseReleaseResult(
                disposition=EvidenceRequestLeaseReleaseDisposition.RETRY_SCHEDULED,
                row_version=3,
            )
        ),
        settle_lease=AsyncMock(),
    )
    runtime = SimpleNamespace(
        resume_equivalent_publication=AsyncMock(return_value=None),
        resolve_plan=AsyncMock(
            return_value=AppTargetedRecapturePlanResolution(
                plan=None,
                reason_code="RECAPTURE_CONTEXT_UNAVAILABLE",
            )
        ),
        restore_and_publish=AsyncMock(),
    )

    outcome = await _consumer(repository=repository, runtime=runtime).consume_next(
        task_id=lease.request.task_id,
        agent_run_id="run-1",
        finalization_epoch=3,
        budget=_budget(),
    )

    assert outcome.disposition is AppTargetedRecaptureDisposition.RETRY_SCHEDULED
    command = repository.release_lease.await_args.kwargs["command"]
    assert command.next_attempt_at == NOW + timedelta(seconds=5)
    assert command.reason_code == "RECAPTURE_CONTEXT_UNAVAILABLE"
    repository.settle_lease.assert_not_awaited()


async def test_consumer_reports_retry_exhaustion_from_repository_result() -> None:
    lease = _lease()
    repository = SimpleNamespace(
        claim_next=AsyncMock(
            return_value=EvidenceRequestClaimResult(
                disposition=EvidenceRequestClaimDisposition.CLAIMED,
                lease=lease,
            )
        ),
        release_lease=AsyncMock(
            return_value=EvidenceRequestLeaseReleaseResult(
                disposition=EvidenceRequestLeaseReleaseDisposition.RETRY_EXHAUSTED,
                row_version=3,
            )
        ),
        settle_lease=AsyncMock(),
    )
    runtime = SimpleNamespace(
        resume_equivalent_publication=AsyncMock(return_value=None),
        resolve_plan=AsyncMock(side_effect=RuntimeError("database unavailable")),
    )

    outcome = await _consumer(repository=repository, runtime=runtime).consume_next(
        task_id=lease.request.task_id,
        agent_run_id="run-1",
        finalization_epoch=3,
        budget=_budget(),
    )

    assert outcome.disposition is AppTargetedRecaptureDisposition.UNFULFILLABLE
    assert outcome.reason_code == "RECAPTURE_RUNTIME_RUNTIMEERROR"


async def test_consumer_settles_nonretryable_publication_error() -> None:
    lease = _lease()
    repository = SimpleNamespace(
        claim_next=AsyncMock(
            return_value=EvidenceRequestClaimResult(
                disposition=EvidenceRequestClaimDisposition.CLAIMED,
                lease=lease,
            )
        ),
        release_lease=AsyncMock(),
        settle_lease=AsyncMock(return_value=3),
    )
    runtime = SimpleNamespace(
        resume_equivalent_publication=AsyncMock(
            side_effect=RestoredProbePublicationError(
                reason_code="RESTORATION_RESULT_EXPIRED",
                message="恢复结果已过期",
            )
        )
    )

    outcome = await _consumer(repository=repository, runtime=runtime).consume_next(
        task_id=lease.request.task_id,
        agent_run_id="run-1",
        finalization_epoch=3,
        budget=_budget(),
    )

    assert outcome.disposition is AppTargetedRecaptureDisposition.UNFULFILLABLE
    command = repository.settle_lease.await_args.kwargs["command"]
    assert command.resolution_reason == "RESTORATION_RESULT_EXPIRED"
    repository.release_lease.assert_not_awaited()


async def test_consumer_returns_empty_without_building_runtime() -> None:
    repository = SimpleNamespace(
        claim_next=AsyncMock(
            return_value=EvidenceRequestClaimResult(
                disposition=EvidenceRequestClaimDisposition.EMPTY
            )
        )
    )
    factory = SimpleNamespace(build=AsyncMock())
    consumer = AppTargetedRecaptureConsumer(
        requests=repository,
        runtime_factory=factory,
        policy=SupplementalEvidencePolicy(enabled=True, runtime_ready=True),
        lease_owner="worker-1:app-recapture",
    )

    outcome = await consumer.consume_next(
        task_id="task-1",
        agent_run_id="run-1",
        finalization_epoch=3,
        budget=_budget(),
    )

    assert outcome.disposition is AppTargetedRecaptureDisposition.EMPTY
    factory.build.assert_not_called()


async def test_consumer_checks_runtime_gate_before_claim() -> None:
    repository = SimpleNamespace(claim_next=AsyncMock())
    consumer = _consumer(
        repository=repository,
        runtime=SimpleNamespace(),
        policy=SupplementalEvidencePolicy(enabled=True, runtime_ready=False),
    )

    with pytest.raises(SupplementalEvidenceNotReadyError):
        await consumer.consume_next(
            task_id="task-1",
            agent_run_id="run-1",
            finalization_epoch=3,
            budget=_budget(),
        )

    repository.claim_next.assert_not_awaited()
