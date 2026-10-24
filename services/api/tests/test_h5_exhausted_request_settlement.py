"""本轮 128/204 的预算耗尽残留请求：逐租约结算，不重放、不覆盖有效发布。"""

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
from services.api.app.application.h5_llm_budget import H5LLMBudgetExhausted
from services.api.app.application.h5_targeted_recapture import (
    H5TargetedRecaptureConsumer,
    H5TargetedRecaptureDisposition,
)
from services.api.app.application.supplemental_evidence_policy import (
    build_lab_supplemental_evidence_policy,
)
from services.api.app.domain.exploration import ExplorationBudget
from services.api.tests.test_h5_targeted_recapture import NOW, _request, _snapshot_request


def consumer_case(request, *, resumed=None, exhausted=True):
    requests = SimpleNamespace(
        claim_next=AsyncMock(
            return_value=EvidenceRequestClaimResult(
                disposition=EvidenceRequestClaimDisposition.CLAIMED,
                lease=EvidenceRequestProcessingLease(
                    request=request,
                    lease_owner="worker",
                    lease_token="token",
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
    runtime = SimpleNamespace(
        resume_equivalent_publication=AsyncMock(return_value=resumed),
        resolve_plan=AsyncMock(),
        restore_and_publish=AsyncMock(),
    )
    budget_reader = SimpleNamespace(is_exhausted=AsyncMock(return_value=exhausted))
    consumer = H5TargetedRecaptureConsumer(
        requests=requests,
        runtime_factory=SimpleNamespace(build=lambda **kwargs: runtime),
        policy=build_lab_supplemental_evidence_policy(),
        lease_owner="worker",
        clock=lambda: NOW,
        budget_reader=budget_reader,
    )
    return consumer, requests, runtime, budget_reader


async def consume(consumer):
    return await consumer.consume_next(
        task_id="task-h5",
        agent_run_id="run-h5",
        finalization_epoch=4,
        budget=ExplorationBudget(
            max_actions=80, remaining_actions=20, max_depth=5, remaining_duration_ms=60000
        ),
        deadline=NOW + timedelta(minutes=1),
        include_snapshot=True,
    )


@pytest.mark.parametrize("snapshot", [False, True])
async def test_exhausted_budget_settles_each_owned_request_before_new_restoration(snapshot):
    consumer, requests, runtime, reader = consumer_case(
        _snapshot_request() if snapshot else _request(),
    )
    await consume(consumer)
    command = requests.settle_lease.await_args.kwargs["command"]
    assert command.target_status is EvidenceRequestStatus.UNFULFILLABLE
    assert command.resolution_reason == "h5_llm_call_budget_exhausted"
    assert command.lease_token == "token" and command.row_version == 2
    reader.is_exhausted.assert_awaited_once_with(
        task_id="task-h5",
        agent_run_id="run-h5",
        finalization_epoch=4,
    )
    requests.release_lease.assert_not_awaited()
    runtime.resolve_plan.assert_not_awaited()
    runtime.restore_and_publish.assert_not_awaited()


async def test_budget_exhaustion_does_not_discard_already_verified_publication():
    consumer, requests, runtime, reader = consumer_case(_request(), resumed=object())
    result = await consume(consumer)
    assert result.disposition is H5TargetedRecaptureDisposition.PROBE_PUBLISHED
    requests.settle_lease.assert_not_awaited()
    reader.is_exhausted.assert_not_awaited()
    runtime.resolve_plan.assert_not_awaited()


async def test_wrapped_budget_refusal_is_terminal_not_three_retries():
    consumer, requests, runtime, _ = consumer_case(_request(), exhausted=False)
    try:
        raise H5LLMBudgetExhausted()
    except H5LLMBudgetExhausted as cause:
        error = RuntimeError("SDK connection wrapper")
        error.__cause__ = cause
    runtime.resolve_plan.side_effect = error
    await consume(consumer)
    command = requests.settle_lease.await_args.kwargs["command"]
    assert command.resolution_reason == "h5_llm_call_budget_exhausted"
    requests.release_lease.assert_not_awaited()


async def test_budget_read_failure_never_claims_budget_exhaustion():
    consumer, requests, runtime, reader = consumer_case(_request())
    reader.is_exhausted.side_effect = ConnectionError("budget read unavailable")
    await consume(consumer)
    requests.settle_lease.assert_not_awaited()
    requests.release_lease.assert_awaited_once()
    runtime.restore_and_publish.assert_not_awaited()


async def test_exhausted_snapshot_cannot_be_deferred_as_a_recoverable_request():
    consumer, _, runtime, _ = consumer_case(_snapshot_request())
    assert not await consumer.can_defer_snapshot_requests(
        requests=(_snapshot_request(),),
        agent_run_id="run-h5",
        finalization_epoch=4,
    )
    runtime.resolve_plan.assert_not_awaited()
