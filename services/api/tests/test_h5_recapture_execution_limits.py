"""H5 借鉴跨端资源回收及补采执行边界的回归。"""

import asyncio
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from services.api.app.application.evidence_requests import (
    EvidenceRequestClaimDisposition,
    EvidenceRequestClaimResult,
    EvidenceRequestLeaseReleaseDisposition,
    EvidenceRequestLeaseReleaseResult,
    EvidenceRequestProcessingLease,
    EvidenceRequestStatus,
)
from services.api.app.application.h5_exploration_workflow import H5SurfaceBootstrapper
from services.api.app.application.h5_targeted_recapture import (
    H5TargetedRecaptureConsumer,
    H5TargetedRecaptureContext,
    H5TargetedRecaptureDisposition,
    H5TargetedRecapturePlanService,
    H5TargetedRecaptureRuntime,
)
from services.api.app.application.supplemental_evidence_policy import (
    build_h5_supplemental_evidence_policy,
)
from services.api.app.domain.exploration import ExplorationBudget
from services.api.app.domain.inspection import RestorationAttemptStatus, RestorationStrategy
from services.api.app.domain.interaction import SurfaceKind, SurfaceSessionRef
from services.api.tests.test_h5_closed_session_recapture import _closed_context
from services.api.tests.test_h5_exploration_workflow import _command
from services.api.tests.test_h5_targeted_recapture import NOW, _request, _snapshot_request


@pytest.mark.parametrize("cancelled", [False, True])
@pytest.mark.parametrize("cleanup_failed", [False, True])
async def test_bootstrap_registration_failure_closes_own_browser_and_preserves_error(
    cancelled,
    cleanup_failed,
):
    failure = asyncio.CancelledError() if cancelled else ConnectionError("登记失败")
    session = SurfaceSessionRef(
        task_id="task-h5",
        session_id="h5:run-h5",
        surface_kind=SurfaceKind.WEB,
        primary_surface_ref="web-primary:h5:run-h5",
    )
    driver = SimpleNamespace(
        start_session=AsyncMock(return_value=session),
        close_session=AsyncMock(side_effect=RuntimeError("关闭失败") if cleanup_failed else None),
    )
    coordinator = SimpleNamespace(coordinate=AsyncMock())
    bootstrapper = H5SurfaceBootstrapper(
        driver=driver,
        surface_sessions=SimpleNamespace(register_active=AsyncMock(side_effect=failure)),
        capture_coordinator=coordinator,
    )

    with pytest.raises(type(failure)) as caught:
        await bootstrapper.start(_command())

    assert caught.value is failure
    driver.close_session.assert_awaited_once_with(
        session=session,
        reason="surface_session_registration_failed",
    )
    coordinator.coordinate.assert_not_awaited()
    if cleanup_failed:
        assert any("关闭失败" in note for note in failure.__notes__)


async def test_bootstrap_start_failure_does_not_close_another_session():
    driver = SimpleNamespace(
        start_session=AsyncMock(side_effect=ConnectionError("启动失败")),
        close_session=AsyncMock(),
    )
    sessions = SimpleNamespace(register_active=AsyncMock())
    with pytest.raises(ConnectionError):
        await H5SurfaceBootstrapper(
            driver=driver,
            surface_sessions=sessions,
            capture_coordinator=object(),
        ).start(_command())
    driver.close_session.assert_not_awaited()
    sessions.register_active.assert_not_awaited()


def _consumer(runtime, *, clock, budget_ms=10_000):
    requests = SimpleNamespace(
        claim_next=AsyncMock(
            return_value=EvidenceRequestClaimResult(
                disposition=EvidenceRequestClaimDisposition.CLAIMED,
                lease=EvidenceRequestProcessingLease(
                    request=_request(),
                    lease_owner="worker-h5",
                    lease_token="lease-h5",
                    finalization_epoch=4,
                    row_version=2,
                    attempt_count=1,
                    max_attempts=3,
                ),
            )
        ),
        settle_lease=AsyncMock(),
        release_lease=AsyncMock(
            return_value=EvidenceRequestLeaseReleaseResult(
                disposition=EvidenceRequestLeaseReleaseDisposition.RETRY_SCHEDULED,
                row_version=3,
            )
        ),
    )
    consumer = H5TargetedRecaptureConsumer(
        requests=requests,
        runtime_factory=SimpleNamespace(build=lambda **_: runtime),
        policy=build_h5_supplemental_evidence_policy(enabled=True, h5_v2_mode="on"),
        lease_owner="worker-h5",
        clock=clock,
    )
    kwargs = dict(
        task_id="task-h5",
        agent_run_id="run-h5",
        finalization_epoch=4,
        budget=ExplorationBudget(
            max_actions=5, remaining_actions=5, max_depth=4, remaining_duration_ms=budget_ms
        ),
        deadline=NOW + timedelta(minutes=1),
    )
    return consumer, requests, kwargs


@pytest.mark.parametrize("phase", ["resume", "plan"])
async def test_recapture_does_not_restore_after_preparation_exhausts_duration(phase):
    now = NOW

    async def advance_time(**_):
        nonlocal now
        now = NOW + timedelta(seconds=2)
        return (
            None
            if phase == "resume"
            else SimpleNamespace(
                plan=SimpleNamespace(
                    deadline=NOW + timedelta(seconds=10),
                    normalization_step_ids=(),
                    strategy=RestorationStrategy.ORIGINAL_SURFACE_PIN,
                )
            )
        )

    runtime = SimpleNamespace(
        resume_equivalent_publication=AsyncMock(
            side_effect=advance_time if phase == "resume" else None,
            return_value=None,
        ),
        resolve_plan=AsyncMock(side_effect=advance_time if phase == "plan" else None),
        restore_and_publish=AsyncMock(
            return_value=SimpleNamespace(
                restoration_attempt=SimpleNamespace(status=RestorationAttemptStatus.EQUIVALENT),
                probe_work=object(),
            )
        ),
    )
    consumer, requests, kwargs = _consumer(runtime, clock=lambda: now, budget_ms=1_000)
    outcome = await consumer.consume_next(**kwargs)
    assert outcome.disposition is H5TargetedRecaptureDisposition.UNFULFILLABLE
    assert outcome.reason_code == "H5_RECAPTURE_DEADLINE_EXHAUSTED"
    runtime.restore_and_publish.assert_not_awaited()
    requests.release_lease.assert_not_awaited()
    command = requests.settle_lease.await_args.kwargs["command"]
    assert command.target_status is EvidenceRequestStatus.UNFULFILLABLE
    if phase == "resume":
        runtime.resolve_plan.assert_not_awaited()


@pytest.mark.parametrize("closed", [False, True])
async def test_plan_caps_deadline_before_remembering_new_session_identity(closed):
    context = (
        _closed_context()
        if closed
        else H5TargetedRecaptureContext(
            task_id="task-h5",
            agent_run_id="run-h5",
            finalization_epoch=4,
            session_id="h5:run-h5",
            current_capture_id="capture-target",
        )
    )
    recovery = SimpleNamespace(remember_new_session_plan=AsyncMock())
    planning = H5TargetedRecapturePlanService(
        contexts=SimpleNamespace(read=AsyncMock(return_value=context)),
        clock=lambda: NOW,
        new_session_runtime=recovery if closed else None,
    )
    runtime = H5TargetedRecaptureRuntime(
        captures=object(),
        snapshots=object(),
        planning=planning,
        restoration_attempts=object(),
        restoration=object(),
        agent_run_id="run-h5",
        finalization_epoch=4,
    )
    deadline = NOW + timedelta(seconds=4)
    result = await runtime.resolve_plan(
        request=_snapshot_request() if closed else _request(),
        deadline=deadline,
    )
    assert result.plan.deadline == deadline
    if closed:
        assert recovery.remember_new_session_plan.await_args.kwargs["plan"] is result.plan
        assert result.plan.strategy is RestorationStrategy.NEW_SESSION_REPLAY


@pytest.mark.parametrize("closed", [False, True])
async def test_recapture_reserves_route_and_new_session_entry_before_probe(closed):
    now = NOW
    plan = SimpleNamespace(
        deadline=NOW + timedelta(seconds=10),
        normalization_step_ids=("edge-1", "edge-2"),
        strategy=(
            RestorationStrategy.NEW_SESSION_REPLAY
            if closed
            else RestorationStrategy.SAME_CONTEXT_NEW_SURFACE
        ),
    )

    async def resolve(**kwargs):
        nonlocal now
        assert kwargs["deadline"] == NOW + timedelta(seconds=10)
        now += timedelta(seconds=2)
        return SimpleNamespace(plan=plan)

    runtime = SimpleNamespace(
        resume_equivalent_publication=AsyncMock(return_value=None),
        resolve_plan=AsyncMock(side_effect=resolve),
        restore_and_publish=AsyncMock(
            return_value=SimpleNamespace(
                restoration_attempt=SimpleNamespace(status=RestorationAttemptStatus.EQUIVALENT),
                probe_work=object(),
            )
        ),
    )
    consumer, _, kwargs = _consumer(runtime, clock=lambda: now)
    result = await consumer.consume_next(**kwargs)
    assert result.disposition is H5TargetedRecaptureDisposition.PROBE_PUBLISHED
    passed = runtime.restore_and_publish.await_args.kwargs["budget"]
    assert passed.resolved_remaining_business_actions == (2 if closed else 3)
    assert passed.remaining_duration_ms == 8_000


async def test_recapture_cannot_spend_obstruction_pool_on_route_and_probe():
    runtime = SimpleNamespace(
        resume_equivalent_publication=AsyncMock(return_value=None),
        resolve_plan=AsyncMock(
            return_value=SimpleNamespace(
                plan=SimpleNamespace(
                    deadline=NOW + timedelta(seconds=10),
                    normalization_step_ids=("edge-1", "edge-2"),
                    strategy=RestorationStrategy.SAME_CONTEXT_NEW_SURFACE,
                )
            )
        ),
        restore_and_publish=AsyncMock(),
    )
    consumer, requests, kwargs = _consumer(runtime, clock=lambda: NOW)
    kwargs["budget"] = ExplorationBudget(
        max_actions=5,
        remaining_actions=5,
        max_depth=4,
        remaining_duration_ms=10_000,
        max_business_actions=2,
        remaining_business_actions=2,
        max_obstruction_actions=3,
        remaining_obstruction_actions=3,
    )
    result = await consumer.consume_next(**kwargs)
    assert result.reason_code == "H5_RECAPTURE_ACTION_BUDGET_EXHAUSTED"
    runtime.restore_and_publish.assert_not_awaited()
    requests.settle_lease.assert_awaited_once()


async def test_context_lookup_cannot_extend_initial_plan_duration():
    now = NOW

    async def read(**_):
        nonlocal now
        now += timedelta(seconds=5)
        return H5TargetedRecaptureContext(
            task_id="task-h5",
            agent_run_id="run-h5",
            finalization_epoch=4,
            session_id="h5:run-h5",
            current_capture_id="capture-target",
        )

    result = await H5TargetedRecapturePlanService(
        contexts=SimpleNamespace(read=AsyncMock(side_effect=read)),
        max_duration=timedelta(seconds=3),
        clock=lambda: now,
    ).resolve(request=_request(), agent_run_id="run-h5", finalization_epoch=4)
    assert result.plan is None
    assert result.reason_code == "H5_RECAPTURE_DEADLINE_EXHAUSTED"


async def test_equivalent_resume_does_not_repeat_restoration_or_charge_route_again():
    runtime = SimpleNamespace(
        resume_equivalent_publication=AsyncMock(return_value=object()),
        resolve_plan=AsyncMock(),
        restore_and_publish=AsyncMock(),
    )
    consumer, requests, kwargs = _consumer(runtime, clock=lambda: NOW)
    result = await consumer.consume_next(**kwargs)
    assert result.disposition is H5TargetedRecaptureDisposition.PROBE_PUBLISHED
    runtime.resolve_plan.assert_not_awaited()
    runtime.restore_and_publish.assert_not_awaited()
    requests.settle_lease.assert_not_awaited()
    assert runtime.resume_equivalent_publication.await_args.kwargs["budget"] == kwargs["budget"]


@pytest.mark.parametrize("no_time", [False, True])
async def test_zero_recapture_budget_settles_without_building_runtime(no_time):
    runtime = SimpleNamespace(resume_equivalent_publication=AsyncMock())
    consumer, requests, kwargs = _consumer(runtime, clock=lambda: NOW)
    kwargs["budget"] = ExplorationBudget(
        max_actions=1,
        remaining_actions=1 if no_time else 0,
        max_depth=1,
        remaining_duration_ms=0 if no_time else 1_000,
    )
    result = await consumer.consume_next(**kwargs)
    assert result.reason_code == (
        "H5_RECAPTURE_DEADLINE_EXHAUSTED" if no_time else "H5_RECAPTURE_ACTION_BUDGET_EXHAUSTED"
    )
    runtime.resume_equivalent_publication.assert_not_awaited()
    requests.settle_lease.assert_awaited_once()


async def test_invalid_deadline_is_rejected_before_claiming_request():
    consumer, requests, kwargs = _consumer(object(), clock=lambda: NOW)
    kwargs["deadline"] = NOW.replace(tzinfo=None)
    with pytest.raises(ValueError):
        await consumer.consume_next(**kwargs)
    requests.claim_next.assert_not_awaited()


async def test_request_expiring_during_planning_keeps_expiry_reason():
    now = NOW

    async def resolve(**_):
        nonlocal now
        now += timedelta(minutes=11)
        return SimpleNamespace(plan=None, reason_code="EVIDENCE_REQUEST_EXPIRED", retryable=False)

    runtime = SimpleNamespace(
        resume_equivalent_publication=AsyncMock(return_value=None),
        resolve_plan=AsyncMock(side_effect=resolve),
        restore_and_publish=AsyncMock(),
    )
    consumer, requests, kwargs = _consumer(runtime, clock=lambda: now)
    result = await consumer.consume_next(**kwargs)
    assert result.disposition is H5TargetedRecaptureDisposition.EXPIRED
    assert result.reason_code == "EVIDENCE_REQUEST_EXPIRED"
    runtime.restore_and_publish.assert_not_awaited()
    command = requests.settle_lease.await_args.kwargs["command"]
    assert command.target_status is EvidenceRequestStatus.EXPIRED


async def test_closed_session_deadline_is_rechecked_after_context_read():
    now = NOW

    async def read(**_):
        nonlocal now
        now += timedelta(seconds=2)
        return _closed_context()

    recovery = SimpleNamespace(
        deadline=NOW + timedelta(seconds=1),
        remember_new_session_plan=AsyncMock(),
    )
    result = await H5TargetedRecapturePlanService(
        contexts=SimpleNamespace(read=AsyncMock(side_effect=read)),
        new_session_runtime=recovery,
        clock=lambda: now,
    ).resolve(request=_snapshot_request(), agent_run_id="run-h5", finalization_epoch=4)
    assert result.plan is None
    assert result.reason_code == "H5_NEW_SESSION_DEADLINE_EXHAUSTED"
    recovery.remember_new_session_plan.assert_not_awaited()


@pytest.mark.parametrize("operation", ["release", "settle"])
@pytest.mark.parametrize("fence_rejected", [True, False])
async def test_recapture_lease_rejection_does_not_abort_main_or_claim_progress(
    operation, fence_rejected,
):
    from services.api.app.application.h5_targeted_recapture import (
        H5TargetedRecaptureDrainService,
    )
    from services.api.app.infrastructure.db.evidence_request_repository import (
        EvidenceRequestPersistenceError,
    )

    runtime = SimpleNamespace(
        resume_equivalent_publication=AsyncMock(return_value=None),
        resolve_plan=AsyncMock(return_value=SimpleNamespace(
            plan=None, reason_code="H5_TARGET_GRAPH_STATE_UNAVAILABLE",
            retryable=operation == "release",
        )),
        restore_and_publish=AsyncMock(),
    )
    consumer, requests, kwargs = _consumer(runtime, clock=lambda: NOW)
    error = (
        EvidenceRequestPersistenceError("Evidence Request 任务执行域、状态、版本或租约已变化")
        if fence_rejected else ConnectionError("数据库连接失败")
    )
    write = requests.release_lease if operation == "release" else requests.settle_lease
    write.side_effect = error
    if not fence_rejected:
        requests.release_lease.side_effect = error
        with pytest.raises(ConnectionError):
            await consumer.consume_next(**kwargs)
        return

    result = await consumer.consume_next(**kwargs)
    assert result.disposition.value == "lease_rejected"
    assert result.reason_code == "H5_RECAPTURE_LEASE_REJECTED"
    assert result.request_id == _request().request_id
    assert write.await_count == 1
    runtime.restore_and_publish.assert_not_awaited()

    wakeup = SimpleNamespace(reopen_waiting_evidence=AsyncMock())
    drain = H5TargetedRecaptureDrainService(
        consumer=SimpleNamespace(consume_next=AsyncMock(return_value=result)),
        scope_reader=SimpleNamespace(read_h5_epoch=AsyncMock(return_value=4)),
        budget=kwargs["budget"], inspection_wakeup=wakeup,
    )
    assert not await drain.drain_one(
        task_id="task-h5", agent_run_id="run-h5", deadline=kwargs["deadline"],
    )
    wakeup.reopen_waiting_evidence.assert_not_awaited()
