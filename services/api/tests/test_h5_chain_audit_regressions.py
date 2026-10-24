"""源码审计的最小行为复现，不冒充当前站点或历史任务重跑。"""

import asyncio
from dataclasses import fields, replace
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from services.api.app.application.h5_targeted_recapture import (
    H5TargetedRecaptureContext,
    H5TargetedRecaptureDisposition,
    H5TargetedRecapturePlanService,
)
from services.api.app.domain.inspection import (
    EvidenceRequiredEquivalence,
    RestorationStrategy,
    build_evidence_request,
)
from services.api.tests.test_h5_recapture_execution_limits import _consumer
from services.api.tests.test_h5_targeted_recapture import NOW, _request


@pytest.mark.parametrize("equivalence,change,allowed", [
    (EvidenceRequiredEquivalence.LOGICAL_EQUIVALENT, "state", True),
    (EvidenceRequiredEquivalence.SAME_SURFACE_CONTINUATION, "state", False),
    (EvidenceRequiredEquivalence.LOGICAL_EQUIVALENT, "document", False),
    (EvidenceRequiredEquivalence.LOGICAL_EQUIVALENT, "identity", False),
    (EvidenceRequiredEquivalence.LOGICAL_EQUIVALENT, "url", False),
    (EvidenceRequiredEquivalence.LOGICAL_EQUIVALENT, "missing_document", False),
])
async def test_same_document_logical_contract_does_not_require_identical_state(equivalence, change,
                                                                            allowed):
    original = _request()
    request = build_evidence_request(**{
        **{item.name: getattr(original, item.name) for item in fields(original)
           if item.name not in {"request_id", "requirement_hash", "protocol_version"}},
        "required_equivalence": equivalence,
    })
    context = H5TargetedRecaptureContext(
        task_id="task-h5", agent_run_id="run-h5", finalization_epoch=4,
        session_id="h5:run-h5", current_capture_id="next-observation",
        target_identity="logical:home", current_identity="logical:other" if change == "identity"
        else "logical:home", target_url="https://example.test/",
        current_url="https://example.test/#other" if change == "url" else "https://example.test/",
        target_document_identity="doc", current_document_identity=(
            None if change == "missing_document" else "new" if change == "document" else "doc"),
        target_state_fingerprint="a" * 64, current_state_fingerprint="b" * 64,
    )
    graph = SimpleNamespace(get_recovery_route=AsyncMock())
    result = await H5TargetedRecapturePlanService(
        contexts=SimpleNamespace(read=AsyncMock(return_value=context)), page_graph=graph,
        clock=lambda: NOW,
    ).resolve(request=request, agent_run_id="run-h5", finalization_epoch=4)
    assert result.executable is allowed
    if allowed:
        assert result.plan.strategy is RestorationStrategy.ORIGINAL_SURFACE_PIN
        assert result.plan.source_grade == original.minimum_source_grade
        assert not result.plan.normalization_step_ids
    graph.get_recovery_route.assert_not_awaited()


@pytest.mark.parametrize("late", [False, True])
async def test_late_resumed_publication_is_not_reported_as_success(late):
    now = NOW

    async def resume(**kwargs):
        nonlocal now
        if late:
            now += timedelta(seconds=2)
        return object()

    runtime = SimpleNamespace(resume_equivalent_publication=resume, resolve_plan=AsyncMock(),
                              restore_and_publish=AsyncMock())
    consumer, requests, kwargs = _consumer(runtime, clock=lambda: now, budget_ms=1000)
    result = await consumer.consume_next(**kwargs)
    assert result.disposition is (H5TargetedRecaptureDisposition.UNFULFILLABLE if late
                                  else H5TargetedRecaptureDisposition.PROBE_PUBLISHED)
    runtime.resolve_plan.assert_not_awaited()
    runtime.restore_and_publish.assert_not_awaited()
    assert requests.settle_lease.await_count == int(late)


async def test_recovery_preparation_is_cancelled_and_drained_at_frozen_deadline():
    stopped = asyncio.Event()

    async def resume(**kwargs):
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    runtime = SimpleNamespace(resume_equivalent_publication=resume, resolve_plan=AsyncMock())
    consumer, _, kwargs = _consumer(runtime, clock=lambda: NOW, budget_ms=20)
    result = await asyncio.wait_for(consumer.consume_next(**kwargs), timeout=0.3)
    assert result.reason_code == "H5_RECAPTURE_DEADLINE_EXHAUSTED"
    assert stopped.is_set()
    runtime.resolve_plan.assert_not_awaited()


async def test_request_lease_loss_stops_preparation_before_any_followup(monkeypatch):
    from services.api.app.application import app_exploration_lease

    started, stopped = asyncio.Event(), asyncio.Event()

    async def resume(**kwargs):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    async def pulse(_):
        await started.wait()

    async def renew(**kwargs):
        return not started.is_set()

    monkeypatch.setattr(app_exploration_lease, "_sleep", pulse)
    runtime = SimpleNamespace(resume_equivalent_publication=resume, resolve_plan=AsyncMock())
    consumer, requests, kwargs = _consumer(runtime, clock=lambda: NOW)
    requests.renew_h5_lease = AsyncMock(side_effect=renew)
    result = await asyncio.wait_for(consumer.consume_next(**kwargs), timeout=0.3)
    assert result.disposition is H5TargetedRecaptureDisposition.LEASE_REJECTED
    assert stopped.is_set()
    requests.settle_lease.assert_not_awaited()
    requests.release_lease.assert_not_awaited()
    runtime.resolve_plan.assert_not_awaited()


async def test_probe_initial_renewal_hang_stops_before_request_or_model():
    from services.api.app.application.h5_probe_work import H5ProbeWorkRunner
    from services.api.app.application.supplemental_evidence_policy import (
        build_lab_supplemental_evidence_policy,
    )
    from services.api.tests.test_h5_probe_work import NOW as PROBE_NOW
    from services.api.tests.test_h5_probe_work import MemoryQueue, _work

    work = _work()
    work = replace(work, budget=replace(work.budget, remaining_duration_ms=20))
    queue = MemoryQueue(work)
    stopped = asyncio.Event()

    async def renew(**kwargs):
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    queue.renew = renew
    executor, consumption, completion = (SimpleNamespace(**{key: AsyncMock()}) for key in (
        "execute", "ensure_running", "complete",
    ))
    runner = H5ProbeWorkRunner(
        queue=queue, turn_executor=executor, request_consumption=consumption,
        completion=completion, lease_owner="worker", now=lambda: PROBE_NOW,
        policy=build_lab_supplemental_evidence_policy(),
    )
    result = await asyncio.wait_for(runner.run_one(task_id=work.task_id, agent_run_id="run-1"), 0.3)
    assert result.error_code == "H5_PROBE_DEADLINE_EXHAUSTED"
    assert stopped.is_set()
    executor.execute.assert_not_awaited()
    consumption.ensure_running.assert_not_awaited()
    completion.complete.assert_not_awaited()


@pytest.mark.parametrize("late", [False, True, "duration"])
async def test_probe_stage_checks_clock_after_synchronous_late_return(late):
    from services.api.app.application.h5_probe_work import H5ProbeWorkError, H5ProbeWorkRunner
    from services.api.tests.test_h5_probe_work import NOW as PROBE_NOW
    from services.api.tests.test_h5_probe_work import _work

    now = PROBE_NOW
    work = replace(_work(), deadline=now + timedelta(seconds=300 if late == "duration" else 1))

    async def value():
        nonlocal now
        if late:
            now += timedelta(seconds=31 if late == "duration" else 2)
        return "observed"

    async def run(task):
        return await task

    runner = H5ProbeWorkRunner(
        queue=object(), turn_executor=object(), request_consumption=object(), completion=object(),
        lease_owner="worker", now=lambda: now,
    )
    guard = SimpleNamespace(run=run)
    if late:
        with pytest.raises(H5ProbeWorkError, match="截止"):
            await runner._run_before_deadline(guard, work, value())
    else:
        assert await runner._run_before_deadline(guard, work, value()) == "observed"
async def test_initial_request_renewal_is_also_bounded_before_external_operations():
    stopped = asyncio.Event()

    async def renew(**kwargs):
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    runtime = SimpleNamespace(resume_equivalent_publication=AsyncMock())
    consumer, requests, kwargs = _consumer(runtime, clock=lambda: NOW, budget_ms=20)
    requests.renew_h5_lease = renew
    result = await asyncio.wait_for(consumer.consume_next(**kwargs), timeout=0.3)
    assert result.reason_code == "H5_RECAPTURE_DEADLINE_EXHAUSTED"
    assert stopped.is_set()
    runtime.resume_equivalent_publication.assert_not_awaited()


async def test_hung_renewal_cannot_keep_old_executor_running_until_main_deadline(monkeypatch):
    from services.api.app.application import app_exploration_lease

    started, query_stopped, operation_stopped = (asyncio.Event() for _ in range(3))

    async def resume(**kwargs):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            operation_stopped.set()

    async def renew(**kwargs):
        if not started.is_set():
            return True
        try:
            await asyncio.Event().wait()
        finally:
            query_stopped.set()

    async def pulse(_):
        await started.wait()

    monkeypatch.setattr(app_exploration_lease, "_sleep", pulse)
    monkeypatch.setattr(app_exploration_lease._RenewingLeaseGuard, "_remaining_lease_seconds",
                        lambda _: 1.04 if started.is_set() else 10)
    runtime = SimpleNamespace(resume_equivalent_publication=resume, resolve_plan=AsyncMock())
    consumer, requests, kwargs = _consumer(runtime, clock=lambda: NOW)
    requests.renew_h5_lease = renew
    result = await asyncio.wait_for(consumer.consume_next(**kwargs), timeout=0.3)
    assert result.disposition is H5TargetedRecaptureDisposition.LEASE_REJECTED
    assert query_stopped.is_set() and operation_stopped.is_set()
    requests.settle_lease.assert_not_awaited()
    requests.release_lease.assert_not_awaited()


async def test_short_valid_request_lease_is_not_rejected_by_cleanup_margin():
    runtime = SimpleNamespace(resume_equivalent_publication=AsyncMock(return_value=object()))
    consumer, requests, kwargs = _consumer(runtime, clock=lambda: NOW)
    consumer._lease_seconds = 1
    requests.renew_h5_lease = AsyncMock(return_value=True)
    result = await consumer.consume_next(**kwargs)
    assert result.disposition is H5TargetedRecaptureDisposition.PROBE_PUBLISHED
    requests.renew_h5_lease.assert_awaited()
