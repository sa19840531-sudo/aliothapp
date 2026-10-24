"""共享派发不扩预算，现场推进或失败即停止领取兄弟请求。"""

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from services.api.app.application.evidence_requests import (
    EvidenceRequestClaimDisposition,
    EvidenceRequestClaimResult,
    EvidenceRequestProcessingLease,
    EvidenceRequestStatus,
)
from services.api.app.application.h5_targeted_recapture import (
    H5TargetedRecaptureConsumer,
    H5TargetedRecaptureDisposition,
    H5TargetedRecaptureDrainService,
    H5TargetedRecaptureOutcome,
)
from services.api.app.application.supplemental_evidence_policy import (
    build_lab_supplemental_evidence_policy,
)
from services.api.app.domain.exploration import ExplorationBudget
from services.api.tests.test_h5_recapture_coalescing import request


@pytest.mark.parametrize(
    "current,budget,failed,expected",
    [
        (True, 3, False, 3),
        (True, 1, False, 1),
        (False, 3, False, 1),
        (True, 3, True, 2),
    ],
)
async def test_dispatch_reserves_existing_budget_and_stops_on_source_change(
    current, budget, failed, expected
):
    first = request()
    siblings = tuple(request(rule=f"another-{index}") for index in range(4))
    calls, scope = [], []

    async def consume(**kwargs):
        assert scope == ["inside"]
        calls.append(kwargs)
        return H5TargetedRecaptureOutcome(
            disposition=(
                H5TargetedRecaptureDisposition.RETRY_SCHEDULED
                if failed and len(calls) == 2
                else H5TargetedRecaptureDisposition.PROBE_PUBLISHED
            ),
            request_id=first.request_id if len(calls) == 1 else kwargs["h5_request_id"],
            request=first,
        )

    @contextmanager
    def sharing_scope():
        scope.append("inside")
        try:
            yield
        finally:
            scope.clear()

    sharing = SimpleNamespace(
        share_observations=sharing_scope,
        has_shared_observation=lambda **_: True,
        shared_observation_is_current=AsyncMock(return_value=current),
    )
    consumer = SimpleNamespace(consume_next=consume)
    reader = SimpleNamespace(
        read_h5_epoch=AsyncMock(return_value=4),
        list_compatible_observation_requests=AsyncMock(return_value=siblings),
    )
    drain = H5TargetedRecaptureDrainService(
        consumer=consumer,
        scope_reader=reader,
        observation_sharing=sharing,
        budget=ExplorationBudget(
            max_actions=budget, remaining_actions=budget, max_depth=0, remaining_duration_ms=30_000
        ),
    )
    assert await drain.drain_one(
        task_id=first.task_id,
        agent_run_id="run-h5",
        deadline=datetime.now(UTC) + timedelta(minutes=1),
    )
    assert len(calls) == expected
    assert scope == []
    for index, call in enumerate(calls[1:]):
        assert call["h5_request_id"] == siblings[index].request_id
        assert call["budget"].remaining_actions == budget - index - 1


async def test_unchanged_gap_is_evidence_insufficient_not_passed_and_skips_browser():
    item = request()
    now = datetime.now(UTC)
    lease = EvidenceRequestProcessingLease(
        request=item,
        finalization_epoch=4,
        lease_owner="worker",
        lease_token="token",
        row_version=2,
        attempt_count=1,
        max_attempts=3,
    )
    requests = SimpleNamespace(
        claim_next=AsyncMock(
            return_value=EvidenceRequestClaimResult(
                disposition=EvidenceRequestClaimDisposition.CLAIMED, lease=lease
            )
        ),
        settle_lease=AsyncMock(),
    )
    factory = SimpleNamespace(build=AsyncMock())
    consumer = H5TargetedRecaptureConsumer(
        requests=requests,
        runtime_factory=factory,
        lease_owner="worker",
        clock=lambda: now,
        policy=build_lab_supplemental_evidence_policy(),
        gap_history=SimpleNamespace(
            has_unchanged_gap=AsyncMock(return_value="failed-prior-request")
        ),
    )
    outcome = await consumer.consume_next(
        task_id=item.task_id,
        agent_run_id="run-h5",
        finalization_epoch=4,
        budget=ExplorationBudget(
            max_actions=1, remaining_actions=1, max_depth=0, remaining_duration_ms=30_000
        ),
        deadline=now + timedelta(minutes=1),
    )
    assert outcome.disposition is H5TargetedRecaptureDisposition.UNFULFILLABLE
    command = requests.settle_lease.await_args.kwargs["command"]
    assert command.target_status is EvidenceRequestStatus.UNFULFILLABLE
    assert command.resolution_reason == "H5_RECAPTURE_UNCHANGED_GAP:failed-prior-request"
    factory.build.assert_not_called()
