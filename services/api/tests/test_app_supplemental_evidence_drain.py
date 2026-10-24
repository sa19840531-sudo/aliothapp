"""App 活会话补采有界排空协调测试。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from services.api.app.application.app_live_inspection_publish import (
    AppActiveSessionDrainState,
    AppActiveSessionInspectionRoundResult,
)
from services.api.app.application.app_probe_completion import AppProbeCompletionResult
from services.api.app.application.app_supplemental_evidence_drain import (
    AppSupplementalEvidenceDrainError,
    AppSupplementalEvidenceDrainService,
    AppSupplementalEvidenceDrainState,
    AppSupplementalEvidenceExhaustionSettlement,
)
from services.api.app.application.app_targeted_recapture_consumer import (
    AppTargetedRecaptureConsumerOutcome,
    AppTargetedRecaptureDisposition,
)
from services.api.app.application.exploration_work_queue import ExplorerWorkStatus
from services.api.app.application.supplemental_evidence_policy import (
    SupplementalEvidencePolicy,
)
from services.api.app.domain.exploration import ExplorationBudget

NOW = datetime(2026, 9, 8, 20, tzinfo=UTC)


def _budget(*, remaining: int = 8) -> ExplorationBudget:
    return ExplorationBudget(
        max_actions=10,
        remaining_actions=remaining,
        max_depth=5,
        remaining_duration_ms=120_000,
        max_business_actions=10,
        remaining_business_actions=remaining,
        max_obstruction_actions=0,
        remaining_obstruction_actions=0,
    )


def _inspection_round(*, drained: bool) -> AppActiveSessionInspectionRoundResult:
    return AppActiveSessionInspectionRoundResult(
        published_work_item_count=0,
        state=AppActiveSessionDrainState(
            pending_capture_count=0,
            nonterminal_work_item_count=0 if drained else 1,
        ),
    )


def _supplemental_state(*, drained: bool) -> AppSupplementalEvidenceDrainState:
    return AppSupplementalEvidenceDrainState(
        nonterminal_request_count=0 if drained else 1,
        nonterminal_probe_count=0,
        pending_completion_count=0,
    )


async def test_drain_closes_request_probe_supplement_and_rejudge_in_two_rounds() -> None:
    inspection = SimpleNamespace(
        drain_round=AsyncMock(
            side_effect=[_inspection_round(drained=False), _inspection_round(drained=True)]
        )
    )
    recapture = SimpleNamespace(
        consume_next=AsyncMock(
            side_effect=[
                AppTargetedRecaptureConsumerOutcome(
                    disposition=AppTargetedRecaptureDisposition.PROBE_PUBLISHED,
                    request_id="request-1",
                    consumed_action_count=2,
                ),
                AppTargetedRecaptureConsumerOutcome(
                    disposition=AppTargetedRecaptureDisposition.EMPTY
                ),
            ]
        )
    )
    probe_outcome = SimpleNamespace(
        claimed=True,
        final_status=ExplorerWorkStatus.SUCCEEDED,
        turn=SimpleNamespace(coordination=SimpleNamespace()),
    )
    probe_runtime = SimpleNamespace(
        run_one_turn=AsyncMock(side_effect=[probe_outcome, SimpleNamespace(claimed=False)])
    )
    completion = SimpleNamespace(
        recover_next=AsyncMock(return_value=None),
        complete=AsyncMock(
            return_value=AppProbeCompletionResult(
                request_id="request-1",
                manifest_id="manifest-1",
                attached=True,
            )
        ),
    )
    wakeup = SimpleNamespace(reopen_waiting_evidence=AsyncMock(return_value=1))
    states = SimpleNamespace(
        load=AsyncMock(
            side_effect=[_supplemental_state(drained=True), _supplemental_state(drained=True)]
        )
    )
    service = AppSupplementalEvidenceDrainService(
        inspection=inspection,
        recapture=recapture,
        probe_runtime=probe_runtime,
        probe_completion=completion,
        inspection_wakeup=wakeup,
        state_reader=states,
        policy=SupplementalEvidencePolicy(enabled=True, runtime_ready=True),
        now=lambda: NOW,
    )

    result = await service.drain(
        task_id="task-1",
        agent_run_id="run-1",
        finalization_epoch=3,
        budget=_budget(),
        deadline=NOW + timedelta(minutes=2),
    )

    assert result.round_count == 2
    assert result.request_attempt_count == 1
    assert result.probe_turn_count == 1
    assert result.consumed_action_count == 3
    assert result.remaining_budget.remaining_actions == 5
    assert wakeup.reopen_waiting_evidence.await_count == 2
    wakeup.reopen_waiting_evidence.assert_awaited_with(task_id="task-1")
    completion.complete.assert_awaited_once_with(task_id="task-1", outcome=probe_outcome)


async def test_drain_can_finalize_after_probe_consumes_last_action() -> None:
    service = AppSupplementalEvidenceDrainService(
        inspection=SimpleNamespace(
            drain_round=AsyncMock(
                side_effect=[
                    _inspection_round(drained=False),
                    _inspection_round(drained=True),
                ]
            )
        ),
        recapture=SimpleNamespace(
            consume_next=AsyncMock(
                side_effect=[
                    AppTargetedRecaptureConsumerOutcome(
                        disposition=AppTargetedRecaptureDisposition.PROBE_PUBLISHED,
                        request_id="request-1",
                        consumed_action_count=2,
                    ),
                    AppTargetedRecaptureConsumerOutcome(
                        disposition=AppTargetedRecaptureDisposition.EMPTY
                    ),
                ]
            )
        ),
        probe_runtime=SimpleNamespace(
            run_one_turn=AsyncMock(
                side_effect=[
                    SimpleNamespace(
                        claimed=True,
                        final_status=ExplorerWorkStatus.SUCCEEDED,
                        turn=SimpleNamespace(coordination=SimpleNamespace()),
                    ),
                    SimpleNamespace(claimed=False),
                ]
            )
        ),
        probe_completion=SimpleNamespace(
            recover_next=AsyncMock(return_value=None),
            complete=AsyncMock(
                return_value=AppProbeCompletionResult(
                    request_id="request-1",
                    manifest_id="manifest-1",
                    attached=True,
                )
            ),
        ),
        inspection_wakeup=SimpleNamespace(reopen_waiting_evidence=AsyncMock(return_value=1)),
        state_reader=SimpleNamespace(
            load=AsyncMock(return_value=_supplemental_state(drained=True))
        ),
        policy=SupplementalEvidencePolicy(enabled=True, runtime_ready=True),
        now=lambda: NOW,
    )

    result = await service.drain(
        task_id="task-1",
        agent_run_id="run-1",
        finalization_epoch=3,
        budget=_budget(remaining=3),
        deadline=NOW + timedelta(minutes=2),
    )

    assert result.round_count == 2
    assert result.consumed_action_count == 3
    assert result.remaining_budget.remaining_actions == 0


async def test_drain_recovers_succeeded_probe_before_judge_round() -> None:
    events: list[str] = []

    async def recover_next(**kwargs):
        del kwargs
        events.append("recover")
        return AppProbeCompletionResult(
            request_id="request-1",
            manifest_id="manifest-1",
            attached=True,
        )

    async def reopen_waiting_evidence(**kwargs):
        del kwargs
        events.append("wakeup")
        return 1

    async def drain_round(**kwargs):
        del kwargs
        events.append("judge")
        return _inspection_round(drained=True)

    service = AppSupplementalEvidenceDrainService(
        inspection=SimpleNamespace(drain_round=drain_round),
        recapture=SimpleNamespace(
            consume_next=AsyncMock(
                return_value=AppTargetedRecaptureConsumerOutcome(
                    disposition=AppTargetedRecaptureDisposition.EMPTY
                )
            )
        ),
        probe_runtime=SimpleNamespace(
            run_one_turn=AsyncMock(return_value=SimpleNamespace(claimed=False))
        ),
        probe_completion=SimpleNamespace(recover_next=recover_next),
        inspection_wakeup=SimpleNamespace(reopen_waiting_evidence=reopen_waiting_evidence),
        state_reader=SimpleNamespace(
            load=AsyncMock(return_value=_supplemental_state(drained=True))
        ),
        policy=SupplementalEvidencePolicy(enabled=True, runtime_ready=True),
        now=lambda: NOW,
    )

    result = await service.drain(
        task_id="task-1",
        agent_run_id="run-1",
        finalization_epoch=3,
        budget=_budget(),
        deadline=NOW + timedelta(minutes=2),
    )

    assert result.recovered_completion_count == 1
    assert events == ["recover", "wakeup", "judge"]


async def test_drain_fails_before_side_effect_when_deadline_is_exhausted() -> None:
    inspection = SimpleNamespace(drain_round=AsyncMock())
    service = AppSupplementalEvidenceDrainService(
        inspection=inspection,
        recapture=SimpleNamespace(),
        probe_runtime=SimpleNamespace(),
        probe_completion=SimpleNamespace(),
        inspection_wakeup=SimpleNamespace(),
        state_reader=SimpleNamespace(),
        policy=SupplementalEvidencePolicy(enabled=True, runtime_ready=True),
        now=lambda: NOW,
    )

    with pytest.raises(AppSupplementalEvidenceDrainError, match="截止时间"):
        await service.drain(
            task_id="task-1",
            agent_run_id="run-1",
            finalization_epoch=3,
            budget=_budget(),
            deadline=NOW,
        )

    inspection.drain_round.assert_not_awaited()


async def test_drain_uses_exhaustion_settlement_when_entering_after_deadline() -> None:
    inspection = SimpleNamespace(
        drain_round=AsyncMock(
            side_effect=[_inspection_round(drained=False), _inspection_round(drained=True)]
        )
    )
    recover = AsyncMock(return_value=None)
    settle = AsyncMock(
        return_value=AppSupplementalEvidenceExhaustionSettlement(
            settled_request_count=1,
            cancelled_probe_count=1,
        )
    )
    states = SimpleNamespace(
        load=AsyncMock(
            side_effect=[
                _supplemental_state(drained=False),
                _supplemental_state(drained=True),
                _supplemental_state(drained=True),
            ]
        )
    )
    service = AppSupplementalEvidenceDrainService(
        inspection=inspection,
        recapture=SimpleNamespace(consume_next=AsyncMock()),
        probe_runtime=SimpleNamespace(run_one_turn=AsyncMock()),
        probe_completion=SimpleNamespace(recover_next=recover),
        inspection_wakeup=SimpleNamespace(reopen_waiting_evidence=AsyncMock(return_value=1)),
        state_reader=states,
        exhaustion_settlement=SimpleNamespace(settle_exhausted=settle),
        policy=SupplementalEvidencePolicy(enabled=True, runtime_ready=True),
        now=lambda: NOW,
    )

    result = await service.drain(
        task_id="task-1",
        agent_run_id="run-1",
        finalization_epoch=3,
        budget=_budget(),
        deadline=NOW,
    )

    assert result.round_count == 2
    assert recover.await_count == 2
    recover.assert_awaited_with(
        task_id="task-1", agent_run_id="run-1", finalization_epoch=3
    )
    settle.assert_awaited_once_with(
        task_id="task-1",
        agent_run_id="run-1",
        finalization_epoch=3,
        reason_code="SUPPLEMENTAL_DRAIN_DEADLINE_EXHAUSTED",
    )
    inspection.drain_round.assert_awaited()
    service._recapture.consume_next.assert_not_awaited()
    service._probe_runtime.run_one_turn.assert_not_awaited()


async def test_drain_fails_before_probe_when_budget_exhausted_mid_round() -> None:
    """recapture 扣光预算后，不得再派发 Probe 编排。"""

    probe_runtime = SimpleNamespace(run_one_turn=AsyncMock())
    service = AppSupplementalEvidenceDrainService(
        inspection=SimpleNamespace(
            drain_round=AsyncMock(
                side_effect=[
                    _inspection_round(drained=False),
                    _inspection_round(drained=True),
                ]
            )
        ),
        recapture=SimpleNamespace(
            consume_next=AsyncMock(
                side_effect=[
                    AppTargetedRecaptureConsumerOutcome(
                        disposition=AppTargetedRecaptureDisposition.PROBE_PUBLISHED,
                        request_id="request-1",
                        consumed_action_count=1,
                    ),
                    AppTargetedRecaptureConsumerOutcome(
                        disposition=AppTargetedRecaptureDisposition.EMPTY
                    ),
                ]
            )
        ),
        probe_runtime=probe_runtime,
        probe_completion=SimpleNamespace(recover_next=AsyncMock(return_value=None)),
        inspection_wakeup=SimpleNamespace(reopen_waiting_evidence=AsyncMock(return_value=0)),
        state_reader=SimpleNamespace(
            load=AsyncMock(return_value=_supplemental_state(drained=True))
        ),
        policy=SupplementalEvidencePolicy(enabled=True, runtime_ready=True),
        now=lambda: NOW,
    )

    result = await service.drain(
        task_id="task-1",
        agent_run_id="run-1",
        finalization_epoch=3,
        budget=_budget(remaining=1),
        deadline=NOW + timedelta(minutes=2),
    )

    assert result.consumed_action_count == 1
    assert result.remaining_budget.remaining_actions == 0
    probe_runtime.run_one_turn.assert_not_awaited()


async def test_drain_reopens_waiting_rule_after_request_becomes_terminal() -> None:
    inspection = SimpleNamespace(
        drain_round=AsyncMock(
            side_effect=[_inspection_round(drained=False), _inspection_round(drained=True)]
        )
    )
    recapture = SimpleNamespace(
        consume_next=AsyncMock(
            side_effect=[
                AppTargetedRecaptureConsumerOutcome(
                    disposition=AppTargetedRecaptureDisposition.UNFULFILLABLE,
                    request_id="request-1",
                    reason_code="RECAPTURE_ACTION_BUDGET_EXHAUSTED",
                ),
                AppTargetedRecaptureConsumerOutcome(
                    disposition=AppTargetedRecaptureDisposition.EMPTY
                ),
            ]
        )
    )
    wakeup = SimpleNamespace(reopen_waiting_evidence=AsyncMock(return_value=1))
    service = AppSupplementalEvidenceDrainService(
        inspection=inspection,
        recapture=recapture,
        probe_runtime=SimpleNamespace(
            run_one_turn=AsyncMock(return_value=SimpleNamespace(claimed=False))
        ),
        probe_completion=SimpleNamespace(recover_next=AsyncMock(return_value=None)),
        inspection_wakeup=wakeup,
        state_reader=SimpleNamespace(
            load=AsyncMock(return_value=_supplemental_state(drained=True))
        ),
        policy=SupplementalEvidencePolicy(enabled=True, runtime_ready=True),
        now=lambda: NOW,
    )

    result = await service.drain(
        task_id="task-1",
        agent_run_id="run-1",
        finalization_epoch=3,
        budget=_budget(),
        deadline=NOW + timedelta(minutes=2),
    )

    assert result.round_count == 2
    assert result.request_attempt_count == 1
    assert wakeup.reopen_waiting_evidence.await_count == 2
