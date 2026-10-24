"""补采接入原探索账本的跨层边界回归。"""

from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from services.api.app.application.app_runtime_composition import (
    _build_supplemental_evidence_budget,
)
from services.api.app.application.app_supplemental_evidence_drain import (
    AppSupplementalEvidenceDrainError,
    AppSupplementalEvidenceDrainService,
    AppSupplementalEvidenceExhaustionSettlement,
    _consume_actions,
)
from services.api.app.application.app_targeted_recapture_consumer import (
    AppTargetedRecaptureConsumerOutcome,
    AppTargetedRecaptureDisposition,
)
from services.api.app.application.exploration_work_queue import ExplorerWorkStatus
from services.api.app.application.supplemental_evidence_policy import SupplementalEvidencePolicy
from services.api.app.domain.exploration import ExplorationBudget
from services.api.tests.test_app_supplemental_evidence_drain import (
    NOW,
    _inspection_round,
    _supplemental_state,
)


def _split_budget() -> ExplorationBudget:
    return ExplorationBudget(
        max_actions=20,
        remaining_actions=15,
        max_depth=5,
        remaining_duration_ms=9000,
        max_business_actions=12,
        remaining_business_actions=10,
        max_obstruction_actions=8,
        remaining_obstruction_actions=5,
        business_duration_floor_ms=3000,
        business_duration_floor_applied=1,
    )


def test_supplemental_conversion_preserves_buckets_and_applied_time_floor() -> None:
    result = _build_supplemental_evidence_budget(
        max_actions=20,
        consumed_actions=5,
        max_depth=5,
        deadline=NOW + timedelta(seconds=20),
        now=NOW,
        persisted_budget=_split_budget(),
    )
    assert result == _split_budget()


def test_supplemental_conversion_caps_each_bucket_without_creating_actions() -> None:
    result = _build_supplemental_evidence_budget(
        max_actions=20,
        consumed_actions=13,
        max_depth=3,
        deadline=NOW + timedelta(seconds=2),
        now=NOW,
        persisted_budget=_split_budget(),
    )
    assert result.remaining_actions == 7
    assert result.remaining_business_actions <= 10
    assert result.remaining_obstruction_actions <= 5
    assert result.max_obstruction_actions == 8
    assert result.remaining_duration_ms == 2000
    assert result.max_depth == 3


def test_recapture_business_actions_cannot_borrow_obstruction_balance() -> None:
    budget = replace(_split_budget(), remaining_actions=5, remaining_business_actions=0)
    with pytest.raises(AppSupplementalEvidenceDrainError, match="业务"):
        _consume_actions(budget, count=1)


@pytest.mark.parametrize("waiting_stage", ["inspection", "recapture"])
async def test_drain_rechecks_deadline_after_each_async_stage(waiting_stage: str) -> None:
    clock = [NOW]

    async def inspection(**_kwargs):
        if waiting_stage == "inspection":
            clock[0] += timedelta(seconds=2)
        return _inspection_round(drained=True)

    async def recapture(**_kwargs):
        if waiting_stage == "recapture":
            clock[0] += timedelta(seconds=2)
        return AppTargetedRecaptureConsumerOutcome(
            disposition=AppTargetedRecaptureDisposition.EMPTY,
        )

    consumer = SimpleNamespace(consume_next=AsyncMock(side_effect=recapture))
    probe = SimpleNamespace(run_one_turn=AsyncMock(return_value=SimpleNamespace(claimed=False)))
    service = AppSupplementalEvidenceDrainService(
        inspection=SimpleNamespace(drain_round=inspection),
        recapture=consumer,
        probe_runtime=probe,
        probe_completion=SimpleNamespace(recover_next=AsyncMock(return_value=None)),
        inspection_wakeup=SimpleNamespace(reopen_waiting_evidence=AsyncMock(return_value=0)),
        state_reader=SimpleNamespace(
            load=AsyncMock(return_value=_supplemental_state(drained=False))
        ),
        policy=SupplementalEvidencePolicy(enabled=True, runtime_ready=True),
        now=lambda: clock[0],
        max_rounds=1,
    )
    with pytest.raises(AppSupplementalEvidenceDrainError, match="截止时间"):
        await service.drain(
            task_id="task-1",
            agent_run_id="run-1",
            finalization_epoch=3,
            budget=_split_budget(),
            deadline=NOW + timedelta(seconds=1),
        )
    probe.run_one_turn.assert_not_awaited()
    assert consumer.consume_next.await_count == (waiting_stage == "recapture")


@pytest.mark.parametrize("pending", [True, False])
async def test_zero_business_budget_still_settles_but_never_dispatches_probe(pending: bool) -> None:
    probe = SimpleNamespace(run_one_turn=AsyncMock(return_value=SimpleNamespace(claimed=False)))
    completion = SimpleNamespace(recover_next=AsyncMock(return_value=None))
    service = AppSupplementalEvidenceDrainService(
        inspection=SimpleNamespace(
            drain_round=AsyncMock(return_value=_inspection_round(drained=True))
        ),
        recapture=SimpleNamespace(
            consume_next=AsyncMock(
                return_value=AppTargetedRecaptureConsumerOutcome(
                    disposition=AppTargetedRecaptureDisposition.EMPTY,
                )
            )
        ),
        probe_runtime=probe,
        probe_completion=completion,
        inspection_wakeup=SimpleNamespace(reopen_waiting_evidence=AsyncMock(return_value=0)),
        state_reader=SimpleNamespace(
            load=AsyncMock(return_value=_supplemental_state(drained=not pending))
        ),
        policy=SupplementalEvidencePolicy(enabled=True, runtime_ready=True),
        now=lambda: NOW,
        max_rounds=1,
    )
    budget = replace(_split_budget(), remaining_actions=5, remaining_business_actions=0)
    call = service.drain(
        task_id="task-1",
        agent_run_id="run-1",
        finalization_epoch=3,
        budget=budget,
        deadline=NOW + timedelta(seconds=10),
    )
    if pending:
        with pytest.raises(AppSupplementalEvidenceDrainError, match="业务"):
            await call
    else:
        result = await call
        assert result.remaining_budget == budget
    probe.run_one_turn.assert_not_awaited()
    completion.recover_next.assert_awaited_once()


async def test_final_drain_result_accounts_for_time_spent_in_readonly_settlement() -> None:
    clock = [NOW]

    async def inspection(**_kwargs):
        clock[0] += timedelta(seconds=2)
        return _inspection_round(drained=True)

    service = AppSupplementalEvidenceDrainService(
        inspection=SimpleNamespace(drain_round=inspection),
        recapture=SimpleNamespace(
            consume_next=AsyncMock(
                return_value=AppTargetedRecaptureConsumerOutcome(
                    disposition=AppTargetedRecaptureDisposition.EMPTY,
                )
            )
        ),
        probe_runtime=SimpleNamespace(
            run_one_turn=AsyncMock(return_value=SimpleNamespace(claimed=False))
        ),
        probe_completion=SimpleNamespace(recover_next=AsyncMock(return_value=None)),
        inspection_wakeup=SimpleNamespace(reopen_waiting_evidence=AsyncMock(return_value=0)),
        state_reader=SimpleNamespace(
            load=AsyncMock(return_value=_supplemental_state(drained=True))
        ),
        policy=SupplementalEvidencePolicy(enabled=True, runtime_ready=True),
        now=lambda: clock[0],
    )
    result = await service.drain(
        task_id="task-1",
        agent_run_id="run-1",
        finalization_epoch=3,
        budget=_split_budget(),
        deadline=NOW + timedelta(seconds=20),
    )
    assert result.remaining_budget.remaining_duration_ms == 7000
    assert result.remaining_budget.remaining_actions == 15


@pytest.mark.parametrize("settlement_port", [True, False])
async def test_last_probe_result_can_finish_settlement_after_deadline_without_new_action(
    settlement_port,
) -> None:
    clock = [NOW]

    async def execute_probe(**_kwargs):
        clock[0] += timedelta(seconds=2)
        return SimpleNamespace(
            claimed=True,
            final_status=ExplorerWorkStatus.SUCCEEDED,
            turn=SimpleNamespace(coordination=object()),
        )

    probe = SimpleNamespace(run_one_turn=AsyncMock(side_effect=execute_probe))
    complete = AsyncMock(return_value=object())
    inspection = AsyncMock(
        side_effect=[
            _inspection_round(drained=False),
            _inspection_round(drained=True),
        ]
    )
    service = AppSupplementalEvidenceDrainService(
        inspection=SimpleNamespace(drain_round=inspection),
        recapture=SimpleNamespace(
            consume_next=AsyncMock(
                return_value=AppTargetedRecaptureConsumerOutcome(
                    disposition=AppTargetedRecaptureDisposition.EMPTY,
                )
            )
        ),
        probe_runtime=probe,
        probe_completion=SimpleNamespace(
            recover_next=AsyncMock(return_value=None), complete=complete
        ),
        inspection_wakeup=SimpleNamespace(reopen_waiting_evidence=AsyncMock(return_value=1)),
        state_reader=SimpleNamespace(
            load=AsyncMock(return_value=_supplemental_state(drained=True))
        ),
        policy=SupplementalEvidencePolicy(enabled=True, runtime_ready=True),
        now=lambda: clock[0],
        exhaustion_settlement=(SimpleNamespace(settle_exhausted=AsyncMock())
                               if settlement_port else None),
    )
    budget = replace(_split_budget(), remaining_actions=6, remaining_business_actions=1)
    if not settlement_port:
        with pytest.raises(AppSupplementalEvidenceDrainError, match="超过任务截止时间"):
            await service.drain(
                task_id="task-1", agent_run_id="run-1", finalization_epoch=3,
                budget=budget, deadline=NOW + timedelta(seconds=1),
            )
        probe.run_one_turn.assert_awaited_once()
        complete.assert_awaited_once()
        return
    result = await service.drain(
        task_id="task-1",
        agent_run_id="run-1",
        finalization_epoch=3,
        budget=budget,
        deadline=NOW + timedelta(seconds=1),
    )
    assert result.round_count == 2
    assert result.consumed_action_count == 1
    assert result.remaining_budget.remaining_business_actions == 0
    assert result.remaining_budget.remaining_obstruction_actions == 5
    assert result.remaining_budget.remaining_duration_ms == 7000
    probe.run_one_turn.assert_awaited_once()
    complete.assert_awaited_once()
    assert inspection.await_count == 2


@pytest.mark.parametrize("boundary", ["zero_duration", "late_publication"])
async def test_exhausted_drain_settles_pending_work_without_dispatching_probe(
    boundary: str,
) -> None:
    """时长为零或发布返回已越界时，收不足后仍完成下一轮规则结算。"""
    clock = [NOW]
    settled = [False]

    async def inspect(**_kwargs):
        return _inspection_round(drained=settled[0])

    async def consume(**_kwargs):
        clock[0] += timedelta(seconds=2)
        return AppTargetedRecaptureConsumerOutcome(
            disposition=AppTargetedRecaptureDisposition.PROBE_PUBLISHED,
            request_id="request-1",
        )

    async def settle(**_kwargs):
        settled[0] = True
        clock[0] += timedelta(milliseconds=500)
        return AppSupplementalEvidenceExhaustionSettlement(
            settled_request_count=1, cancelled_probe_count=1,
        )

    async def state(**_kwargs):
        return _supplemental_state(drained=settled[0])

    consumer = AsyncMock(side_effect=consume)
    probe = AsyncMock()
    settler = AsyncMock(side_effect=settle)
    inspection = AsyncMock(side_effect=inspect)
    service = AppSupplementalEvidenceDrainService(
        inspection=SimpleNamespace(drain_round=inspection),
        recapture=SimpleNamespace(consume_next=consumer),
        probe_runtime=SimpleNamespace(run_one_turn=probe),
        probe_completion=SimpleNamespace(recover_next=AsyncMock(return_value=None)),
        inspection_wakeup=SimpleNamespace(reopen_waiting_evidence=AsyncMock(return_value=1)),
        state_reader=SimpleNamespace(load=state),
        exhaustion_settlement=SimpleNamespace(settle_exhausted=settler),
        policy=SupplementalEvidencePolicy(enabled=True, runtime_ready=True),
        now=lambda: clock[0],
    )
    budget = _split_budget()
    if boundary == "zero_duration":
        budget = replace(budget, remaining_duration_ms=0)
    deadline = NOW + timedelta(seconds=1)

    result = await service.drain(
        task_id="task-1", agent_run_id="run-1", finalization_epoch=3,
        budget=budget, deadline=deadline,
    )

    assert result.round_count == 2
    assert result.probe_turn_count == result.consumed_action_count == 0
    assert result.remaining_budget.remaining_business_actions == 10
    assert result.remaining_budget.remaining_obstruction_actions == 5
    assert result.remaining_budget.remaining_duration_ms == (
        0 if boundary == "zero_duration" else 6500
    )
    assert consumer.await_count == (boundary == "late_publication")
    if boundary == "late_publication":
        assert consumer.call_args.kwargs["deadline"] == deadline
    assert inspection.await_count == 2
    settler.assert_awaited_once_with(
        task_id="task-1", agent_run_id="run-1", finalization_epoch=3,
        reason_code=(
            "SUPPLEMENTAL_DRAIN_DURATION_EXHAUSTED"
            if boundary == "zero_duration" else "SUPPLEMENTAL_DRAIN_DEADLINE_EXHAUSTED"
        ),
    )
    probe.assert_not_awaited()
