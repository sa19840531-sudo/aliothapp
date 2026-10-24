"""验证 H5 动作跨 deadline 时的有界终态与未过期后继对照。"""

from dataclasses import replace
from datetime import datetime, timedelta

import pytest

from services.api.app.application import h5_exploration_turn as turn_module
from services.api.app.application.app_exploration_lease import AppExplorationLeaseLostError
from services.api.app.application.bounded_exploration_scheduler import (
    BoundedExplorationState,
)
from services.api.app.application.capture_coordination import (
    CaptureCoordinationError,
    CaptureCoordinationResult,
    CaptureCoordinationStage,
)
from services.api.app.application.exploration_work_queue import ExplorerWorkStatus
from services.api.app.application.explorer_turn import ExplorerTurnResult
from services.api.app.application.h5_exploration_cancellation import H5CancellationCheckpoint
from services.api.app.application.h5_exploration_recovery import (
    H5BrowserRecoveryAttemptFacts,
    H5FailureStage,
    H5RecoveryOutcome,
)
from services.api.app.application.h5_exploration_turn import H5ExplorationTurnRunner
from services.api.app.domain.exploration import ExplorationTerminalStatus
from services.api.app.domain.interaction import InteractionResult, InteractionResultStatus
from services.api.app.infrastructure.db import explorer_work_repository as repository_module
from services.api.tests.test_h5_exploration_turn import (
    _action_intent,
    _BrowserLossExecutor,
    _BrowserLossRecovery,
    _CancellationQueue,
    _capture_for_turn,
    _Continuation,
    _Executor,
    _FailingExecutor,
    _Queue,
    _terminal_turn,
    _work,
)


def _action_turn(work):
    """构造带 before/after Capture 的成功动作轮。"""
    before = _capture_for_turn(work, "capture-before")
    after = _capture_for_turn(work, "capture-after")
    intent = _action_intent(work)
    return ExplorerTurnResult(
        work_id=work.work_id,
        task_id=work.task_id,
        decision=intent,
        coordination=CaptureCoordinationResult(
            before_capture=before,
            interaction=InteractionResult(
                task_id=work.task_id,
                session_id=work.session_id,
                intent_id=intent.intent_id,
                action_execution_id="action-after",
                source_capture_id=before.capture_id,
                status=InteractionResultStatus.SUCCEEDED,
                result_capture_id=after.capture_id,
            ),
            after_capture=after,
        ),
    )


def _with_bounded_state(work, *, deadline: datetime):
    """为结算摘要提供非零的真实调度状态。"""
    state = BoundedExplorationState.create(
        max_actions=work.budget.max_actions,
        max_duration_ms=30_000,
        deadline=deadline,
    )
    state = replace(
        state,
        consumed_actions={"required": 1},
        no_progress_count=2,
    )
    metadata = state.with_metadata(work.frontier.observation.metadata)
    frontier = replace(
        work.frontier,
        observation=replace(work.frontier.observation, metadata=metadata),
    )
    return replace(work, frontier=frontier, frontier_snapshot_id=frontier.snapshot_id)


class _NoDatabaseRepository(repository_module.PostgresExplorerWorkRepository):
    async def _get_pool(self):
        raise AssertionError("截止回归不得连接数据库")


class _RealPrecheckQueue(_Queue):
    """让过期后继走真实仓储的 deadline 前置校验，但禁止数据库连接。"""

    def __init__(self, work) -> None:
        super().__init__(work)
        self.commit_attempts = 0
        self.repository = _NoDatabaseRepository(pool=None)

    async def commit_successor(self, *, command, emit_wakeup=True):
        self.commit_attempts += 1
        return await self.repository.commit_successor(
            command=command,
            emit_wakeup=emit_wakeup,
        )


class _MutableClock:
    def __init__(self, value: datetime) -> None:
        self.value = value


class _DeadlineCrossingExecutor(_Executor):
    def __init__(self, turn, clock: _MutableClock, after_deadline: timedelta) -> None:
        super().__init__(turn)
        self._clock = clock
        self._after_deadline = after_deadline

    async def execute(self, *, work, agent_run_id):
        assert self._clock.value < work.deadline
        result = await super().execute(work=work, agent_run_id=agent_run_id)
        self._clock.value = work.deadline + self._after_deadline
        return result


class _DeadlineCrossingContinuation(_Continuation):
    def __init__(self, successor, clock: _MutableClock, after_deadline: timedelta) -> None:
        super().__init__(successor)
        self._clock = clock
        self._after_deadline = after_deadline

    async def prepare_after_action(self, command):
        result = await super().prepare_after_action(command)
        self._clock.value = command.prior_work.deadline + self._after_deadline
        return result

    async def prepare_after_action_failure(self, command):
        result = await super().prepare_after_action_failure(command)
        self._clock.value = command.prior_work.deadline + self._after_deadline
        return result


class _DeadlineCrossingBrowserLossRecovery(_BrowserLossRecovery):
    def __init__(
        self,
        *,
        outcome: H5RecoveryOutcome,
        clock: _MutableClock,
        after_deadline: timedelta,
    ) -> None:
        super().__init__(outcome=outcome)
        self._clock = clock
        self._after_deadline = after_deadline

    async def recover(self, *, work, agent_run_id, error, attempt_reserved=False):
        del attempt_reserved
        result = await super().recover(
            work=work,
            agent_run_id=agent_run_id,
            error=error,
        )
        assert result.recovery_work is not None
        self._clock.value = result.recovery_work.deadline + self._after_deadline
        return result


class _TypedDeadlineRaceQueue(_Queue):
    def __init__(self, work, clock: _MutableClock) -> None:
        super().__init__(work)
        self.clock = clock
        self.commit_attempts = 0

    async def commit_successor(self, *, command, emit_wakeup=True):
        del emit_wakeup
        self.commit_attempts += 1
        self.clock.value = command.successor.deadline
        raise repository_module.ExplorerWorkDeadlineExceededError(
            "后继提交期间已超过 deadline，必须重新校验租约后结算"
        )


class _NonDeadlinePersistenceFailureQueue(_Queue):
    async def commit_successor(self, *, command, emit_wakeup=True):
        del command, emit_wakeup
        raise repository_module.ExplorerWorkPersistenceError("父 Work row_version 已变化")


class _LeaseLostQueue(_Queue):
    async def commit_successor(self, *, command, emit_wakeup=True):
        del command, emit_wakeup
        raise AppExplorationLeaseLostError("Explorer Work 租约已失效")


class _CancelAfterContinuation:
    def __init__(self) -> None:
        self.calls = 0

    async def is_cancel_requested(self, *, task_id, agent_run_id):
        assert task_id == "task-h5"
        assert agent_run_id == "run-h5"
        self.calls += 1
        return self.calls >= 5


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "after_deadline",
    [timedelta(0), timedelta(milliseconds=1)],
    ids=["exact_deadline", "past_deadline"],
)
async def test_h5_action_terminal_crossing_deadline_preserves_coordination_and_capture(
    monkeypatch,
    after_deadline: timedelta,
) -> None:
    prior = _work()
    terminal = turn_module.ExplorationTerminal(
        work_id=prior.work_id,
        task_id=prior.task_id,
        status=ExplorationTerminalStatus.COMPLETED,
        reason="动作后的 Frontier 已完成",
    )
    turn = replace(_action_turn(prior), decision=terminal)
    original_terminal = turn.decision
    clock = _MutableClock(prior.deadline - timedelta(milliseconds=1))

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            del tz
            return clock.value

    monkeypatch.setattr(turn_module, "datetime", Clock)
    queue = _Queue(prior)
    runner = H5ExplorationTurnRunner(
        queue=queue,
        turn_executor=_DeadlineCrossingExecutor(turn, clock, after_deadline),
        continuation=_Continuation(_work(capture_id="unused")),
        lease_owner="h5-explorer-1",
        emit_wakeup=False,
    )

    outcome = await runner.run_one(task_id=prior.task_id, agent_run_id="run-h5")

    transition = queue.transitions[-1]
    assert outcome.final_status is ExplorerWorkStatus.BUDGET_EXHAUSTED
    assert outcome.turn is not turn
    assert outcome.turn.coordination is turn.coordination
    assert outcome.result_capture_id == "capture-after"
    assert turn.decision is original_terminal
    assert original_terminal.status is ExplorationTerminalStatus.COMPLETED
    assert transition.terminal is outcome.turn.decision
    assert transition.settlement_work is None
    assert transition.terminal.coverage_summary["result_capture_id"] == "capture-after"
    assert transition.terminal.coverage_summary["consumed_action_count"] == 1


@pytest.mark.asyncio
async def test_h5_pure_terminal_after_deadline_keeps_completed_observation_semantics(
    monkeypatch,
) -> None:
    prior = _work()
    turn = _terminal_turn(prior)
    original_terminal = turn.decision
    clock = _MutableClock(prior.deadline - timedelta(milliseconds=1))

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            del tz
            return clock.value

    monkeypatch.setattr(turn_module, "datetime", Clock)
    queue = _Queue(prior)
    runner = H5ExplorationTurnRunner(
        queue=queue,
        turn_executor=_DeadlineCrossingExecutor(turn, clock, timedelta(1)),
        continuation=_Continuation(_work(capture_id="unused")),
        lease_owner="h5-explorer-1",
        emit_wakeup=False,
    )

    outcome = await runner.run_one(task_id=prior.task_id, agent_run_id="run-h5")

    assert outcome.final_status is ExplorerWorkStatus.SUCCEEDED
    assert outcome.turn is turn
    assert outcome.turn.decision is original_terminal
    assert original_terminal.status is ExplorationTerminalStatus.COMPLETED
    assert queue.transitions[-1].terminal is original_terminal
    assert original_terminal.coverage_summary == {}


@pytest.mark.asyncio
async def test_h5_severe_action_terminal_preserves_reason_and_accounts_result(
    monkeypatch,
) -> None:
    prior = _work()
    terminal = turn_module.ExplorationTerminal(
        work_id=prior.work_id,
        task_id=prior.task_id,
        status=ExplorationTerminalStatus.BOUNDED,
        reason="证据不足，不能继续",
        outcome="bounded",
        reason_code="evidence_insufficient",
        coverage_summary={"evidence_grade": "insufficient"},
    )
    turn = replace(_action_turn(prior), decision=terminal)
    clock = _MutableClock(prior.deadline - timedelta(milliseconds=1))

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            del tz
            return clock.value

    monkeypatch.setattr(turn_module, "datetime", Clock)
    queue = _Queue(prior)
    outcome = await H5ExplorationTurnRunner(
        queue=queue,
        turn_executor=_DeadlineCrossingExecutor(turn, clock, timedelta(1)),
        continuation=_Continuation(_work(capture_id="unused")),
        lease_owner="h5-explorer-1",
        emit_wakeup=False,
    ).run_one(task_id=prior.task_id, agent_run_id="run-h5")

    persisted = queue.transitions[-1].terminal
    assert outcome.final_status is ExplorerWorkStatus.BOUNDED
    assert persisted is not None
    assert persisted.status is ExplorationTerminalStatus.BOUNDED
    assert persisted.reason == "证据不足，不能继续"
    assert persisted.outcome == "bounded"
    assert persisted.reason_code == "evidence_insufficient"
    assert persisted.coverage_summary["evidence_grade"] == "insufficient"
    assert persisted.coverage_summary["result_capture_id"] == "capture-after"
    assert persisted.coverage_summary["consumed_action_count"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stage", "retry_original_action", "action_cost"),
    [
        (CaptureCoordinationStage.BEFORE_CAPTURE, True, 1),
        (CaptureCoordinationStage.ACTION, False, 2),
    ],
    ids=["before_capture_retry", "unknown_action_recovery"],
)
async def test_h5_browser_recovery_cross_deadline_settles_parent_without_cross_session_successor(
    monkeypatch,
    stage: CaptureCoordinationStage,
    retry_original_action: bool,
    action_cost: int,
) -> None:
    prior = _work()
    recovered = _work(
        capture_id=f"capture-recovery-{stage.value}",
        parent_work_id=prior.work_id,
    )
    recovered = replace(
        recovered,
        deadline=prior.deadline,
        budget=replace(
            recovered.budget,
            remaining_actions=recovered.budget.max_actions - action_cost,
            remaining_business_actions=recovered.budget.max_actions - action_cost,
            remaining_duration_ms=max(30_000 - action_cost * 1_000, 0),
        ),
    )
    recovered = _with_bounded_state(recovered, deadline=prior.deadline)
    clock = _MutableClock(prior.deadline - timedelta(milliseconds=1))

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            del tz
            return clock.value

    monkeypatch.setattr(turn_module, "datetime", Clock)
    monkeypatch.setattr(repository_module, "datetime", Clock)
    recovery = _DeadlineCrossingBrowserLossRecovery(
        outcome=H5RecoveryOutcome(
            retry_original_action=retry_original_action,
            inventory_status=(
                None
                if stage is CaptureCoordinationStage.BEFORE_CAPTURE
                else "insufficient"
            ),
            supersede_failed_work=True,
            rebaseline_steps=("capture", "detect", "assess"),
            recovery_work=recovered,
            recovery_attempt_count=1,
        ),
        clock=clock,
        after_deadline=timedelta(1),
    )
    queue = _RealPrecheckQueue(prior)
    outcome = await H5ExplorationTurnRunner(
        queue=queue,
        turn_executor=_BrowserLossExecutor(stage=stage),
        continuation=_Continuation(recovered),
        browser_loss_recovery=recovery,
        lease_owner="h5-explorer-1",
        emit_wakeup=False,
    ).run_one(task_id=prior.task_id, agent_run_id="run-h5")

    transition = queue.transitions[-1]
    assert outcome.final_status is ExplorerWorkStatus.BUDGET_EXHAUSTED
    assert outcome.result_capture_id == recovered.source_capture_id
    assert outcome.successor_work_id is None
    assert queue.successors == []
    assert queue.commit_attempts == 0
    assert transition.target_status is ExplorerWorkStatus.BUDGET_EXHAUSTED
    assert transition.settlement_work is None
    assert transition.terminal is not None
    summary = transition.terminal.coverage_summary
    assert summary["result_capture_id"] == recovered.source_capture_id
    assert summary["settled_budget"] == recovered.budget.to_dict()
    assert summary["consumed_action_count"] == action_cost
    assert summary["bounded_exploration_scheduler"]["no_progress_count"] == 2


@pytest.mark.asyncio
async def test_h5_resumed_browser_recovery_cross_deadline_settles_original_parent(
    monkeypatch,
) -> None:
    prior = _work()
    started_attempt = H5BrowserRecoveryAttemptFacts.create(
        task_id=prior.task_id,
        agent_run_id="run-h5",
        source_session_id=prior.session_id,
        failed_work_id=prior.work_id,
        source_state_version=prior.frontier.observation.state_version,
        frontier_snapshot_id=prior.frontier_snapshot_id,
        failure_stage=H5FailureStage.ACTION,
        recovery_attempt_count=1,
        status="started",
        error_code="browser_session_lost",
        action_execution_id="action-unknown",
        before_capture_id=prior.source_capture_id,
        after_capture_id=None,
        interaction_status="executing",
        interaction_result_capture_id=None,
        started_at=prior.frontier.observation.observed_at.isoformat(),
        deadline=prior.deadline.isoformat(),
        remaining_duration_ms=prior.budget.remaining_duration_ms,
        action_cost=2,
        remaining_actions_after_reservation=prior.budget.remaining_actions - 2,
        last_safe_location=prior.frontier.observation.location,
        reason_code=None,
    )
    started_frontier = replace(
        prior.frontier,
        observation=replace(
            prior.frontier.observation,
            metadata={
                **dict(prior.frontier.observation.metadata),
                "h5_browser_recovery_attempt": started_attempt.to_dict(),
            },
        ),
    )
    started_work = replace(prior, frontier=started_frontier)
    recovered = _work(capture_id="capture-resumed-recovery", parent_work_id=prior.work_id)
    recovered = replace(
        recovered,
        deadline=prior.deadline,
        budget=replace(
            recovered.budget,
            remaining_actions=1,
            remaining_business_actions=1,
            remaining_duration_ms=28_000,
        ),
    )
    recovered = _with_bounded_state(recovered, deadline=prior.deadline)
    clock = _MutableClock(prior.deadline - timedelta(milliseconds=1))

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            del tz
            return clock.value

    monkeypatch.setattr(turn_module, "datetime", Clock)
    monkeypatch.setattr(repository_module, "datetime", Clock)
    recovery = _DeadlineCrossingBrowserLossRecovery(
        outcome=H5RecoveryOutcome(
            retry_original_action=False,
            inventory_status="insufficient",
            supersede_failed_work=True,
            rebaseline_steps=("capture", "detect", "assess"),
            recovery_work=recovered,
            recovery_attempt_count=2,
        ),
        clock=clock,
        after_deadline=timedelta(1),
    )
    queue = _RealPrecheckQueue(started_work)
    outcome = await H5ExplorationTurnRunner(
        queue=queue,
        turn_executor=_BrowserLossExecutor(stage=CaptureCoordinationStage.ACTION),
        continuation=_Continuation(recovered),
        browser_loss_recovery=recovery,
        lease_owner="h5-explorer-1",
        emit_wakeup=False,
    ).run_one(task_id=prior.task_id, agent_run_id="run-h5")

    transition = queue.transitions[-1]
    assert outcome.final_status is ExplorerWorkStatus.BUDGET_EXHAUSTED
    assert outcome.result_capture_id == recovered.source_capture_id
    assert outcome.successor_work_id is None
    assert queue.successors == []
    assert queue.commit_attempts == 0
    assert transition.target_status is ExplorerWorkStatus.BUDGET_EXHAUSTED
    assert transition.settlement_work is None
    assert transition.terminal is not None
    assert transition.terminal.coverage_summary["consumed_action_count"] == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "after_deadline",
    [timedelta(0), timedelta(milliseconds=1)],
    ids=["exact_deadline", "past_deadline"],
)
async def test_h5_action_ending_at_or_after_deadline_closes_as_budget(
    monkeypatch,
    after_deadline: timedelta,
) -> None:
    prior = _work()
    successor = _work(capture_id="capture-after", parent_work_id=prior.work_id)
    successor = replace(
        successor,
        deadline=prior.deadline,
        budget=replace(
            successor.budget,
            remaining_actions=2,
            remaining_duration_ms=29_000,
            remaining_business_actions=2,
        ),
    )
    successor = _with_bounded_state(successor, deadline=prior.deadline)
    clock = _MutableClock(prior.deadline - timedelta(milliseconds=1))

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            del tz
            return clock.value

    monkeypatch.setattr(repository_module, "datetime", Clock)
    monkeypatch.setattr(turn_module, "datetime", Clock)
    turn = _action_turn(prior)
    original_decision = turn.decision
    queue = _RealPrecheckQueue(prior)
    runner = H5ExplorationTurnRunner(
        queue=queue,
        turn_executor=_DeadlineCrossingExecutor(turn, clock, after_deadline),
        continuation=_Continuation(successor),
        lease_owner="h5-explorer-1",
        emit_wakeup=False,
    )

    outcome = await runner.run_one(task_id=prior.task_id, agent_run_id="run-h5")

    assert outcome.final_status is ExplorerWorkStatus.BUDGET_EXHAUSTED
    transition = queue.transitions[-1]
    assert outcome.turn is not turn
    assert outcome.turn.coordination is turn.coordination
    assert outcome.turn.decision is transition.terminal
    assert turn.decision is original_decision
    assert turn.is_terminal is False
    assert outcome.result_capture_id == "capture-after"
    assert outcome.successor_work_id is None
    assert queue.successors == []
    assert transition.target_status is ExplorerWorkStatus.BUDGET_EXHAUSTED
    assert transition.terminal is not None
    assert transition.terminal.status is ExplorationTerminalStatus.BUDGET_EXHAUSTED
    assert transition.settlement_work is successor
    summary = transition.terminal.coverage_summary
    assert summary["result_capture_id"] == successor.source_capture_id
    assert summary["settled_budget"] == successor.budget.to_dict()
    assert summary["consumed_action_count"] == 1
    bounded_state = summary["bounded_exploration_scheduler"]
    assert bounded_state["no_progress_count"] == 2
    assert bounded_state["consumed_actions"]["required"] == 1
    assert queue.commit_attempts == 0


@pytest.mark.asyncio
async def test_h5_unexpired_successor_remains_successful() -> None:
    prior = _work()
    successor = replace(
        _work(capture_id="capture-after", parent_work_id=prior.work_id),
        deadline=prior.deadline,
    )
    turn = _action_turn(prior)
    queue = _Queue(prior)
    runner = H5ExplorationTurnRunner(
        queue=queue,
        turn_executor=_Executor(turn),
        continuation=_Continuation(successor),
        lease_owner="h5-explorer-1",
        emit_wakeup=False,
    )

    outcome = await runner.run_one(task_id=prior.task_id, agent_run_id="run-h5")

    assert outcome.final_status is ExplorerWorkStatus.SUCCEEDED
    assert outcome.result_capture_id == "capture-after"
    assert outcome.successor_work_id == successor.work_id
    assert len(queue.successors) == 1
    assert queue.transitions[-1].target_status is not ExplorerWorkStatus.BUDGET_EXHAUSTED


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "after_deadline",
    [timedelta(0), timedelta(milliseconds=1)],
    ids=["exact_deadline", "past_deadline"],
)
async def test_h5_continuation_ending_at_or_after_deadline_settles_result(
    monkeypatch,
    after_deadline: timedelta,
) -> None:
    prior = _work()
    successor = _work(capture_id="capture-after", parent_work_id=prior.work_id)
    successor = replace(
        successor,
        deadline=prior.deadline,
        budget=replace(
            successor.budget,
            remaining_actions=2,
            remaining_business_actions=2,
            remaining_duration_ms=29_000,
        ),
    )
    successor = _with_bounded_state(successor, deadline=prior.deadline)
    clock = _MutableClock(prior.deadline - timedelta(milliseconds=1))

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            del tz
            return clock.value

    monkeypatch.setattr(repository_module, "datetime", Clock)
    monkeypatch.setattr(turn_module, "datetime", Clock)
    turn = _action_turn(prior)
    original_decision = turn.decision
    queue = _RealPrecheckQueue(prior)
    runner = H5ExplorationTurnRunner(
        queue=queue,
        turn_executor=_Executor(turn),
        continuation=_DeadlineCrossingContinuation(
            successor,
            clock,
            after_deadline,
        ),
        lease_owner="h5-explorer-1",
        emit_wakeup=False,
    )

    outcome = await runner.run_one(task_id=prior.task_id, agent_run_id="run-h5")

    transition = queue.transitions[-1]
    assert outcome.final_status is ExplorerWorkStatus.BUDGET_EXHAUSTED
    assert outcome.turn is not turn
    assert outcome.turn.coordination is turn.coordination
    assert outcome.turn.decision is transition.terminal
    assert turn.decision is original_decision
    assert turn.is_terminal is False
    assert outcome.result_capture_id == "capture-after"
    assert outcome.successor_work_id is None
    assert queue.successors == []
    assert transition.target_status is ExplorerWorkStatus.BUDGET_EXHAUSTED
    assert transition.terminal is not None
    assert transition.terminal.status is ExplorationTerminalStatus.BUDGET_EXHAUSTED
    assert transition.settlement_work is successor
    summary = transition.terminal.coverage_summary
    assert summary["result_capture_id"] == successor.source_capture_id
    assert summary["settled_budget"] == successor.budget.to_dict()
    assert summary["consumed_action_count"] == 1
    assert summary["bounded_exploration_scheduler"]["no_progress_count"] == 2
    assert queue.commit_attempts == 0


@pytest.mark.asyncio
async def test_h5_typed_deadline_commit_race_settles_without_publishing_successor(
    monkeypatch,
) -> None:
    prior = _work()
    successor = replace(
        _work(capture_id="capture-after", parent_work_id=prior.work_id),
        deadline=prior.deadline,
    )
    clock = _MutableClock(prior.deadline - timedelta(milliseconds=1))

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            del tz
            return clock.value

    monkeypatch.setattr(repository_module, "datetime", Clock)
    monkeypatch.setattr(turn_module, "datetime", Clock)
    turn = _action_turn(prior)
    queue = _TypedDeadlineRaceQueue(prior, clock)
    runner = H5ExplorationTurnRunner(
        queue=queue,
        turn_executor=_Executor(turn),
        continuation=_Continuation(successor),
        lease_owner="h5-explorer-1",
        emit_wakeup=False,
    )

    outcome = await runner.run_one(task_id=prior.task_id, agent_run_id="run-h5")

    transition = queue.transitions[-1]
    assert outcome.final_status is ExplorerWorkStatus.BUDGET_EXHAUSTED
    assert outcome.turn is not turn
    assert outcome.turn.coordination is turn.coordination
    assert outcome.turn.decision is transition.terminal
    assert outcome.result_capture_id == "capture-after"
    assert outcome.successor_work_id is None
    assert queue.successors == []
    assert transition.target_status is ExplorerWorkStatus.BUDGET_EXHAUSTED
    assert transition.settlement_work is successor
    assert queue.commit_attempts == 1


@pytest.mark.asyncio
async def test_h5_non_deadline_persistence_failure_still_fails_without_settlement() -> None:
    prior = _work()
    successor = replace(
        _work(capture_id="capture-after", parent_work_id=prior.work_id),
        deadline=prior.deadline,
    )
    turn = _action_turn(prior)
    queue = _NonDeadlinePersistenceFailureQueue(prior)
    runner = H5ExplorationTurnRunner(
        queue=queue,
        turn_executor=_Executor(turn),
        continuation=_Continuation(successor),
        lease_owner="h5-explorer-1",
        emit_wakeup=False,
    )

    outcome = await runner.run_one(task_id=prior.task_id, agent_run_id="run-h5")

    assert outcome.final_status is ExplorerWorkStatus.FAILED
    assert outcome.error_code == "H5_EXPLORATION_TURN_FAILED"
    assert outcome.turn is None
    assert outcome.result_capture_id is None
    assert outcome.successor_work_id is None
    assert queue.transitions[-1].target_status is ExplorerWorkStatus.FAILED
    assert queue.transitions[-1].terminal is None
    assert queue.transitions[-1].settlement_work is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "after_deadline",
    [timedelta(0), timedelta(milliseconds=1)],
    ids=["exact_deadline", "past_deadline"],
)
async def test_h5_failed_reobservation_ending_at_or_after_deadline_settles(
    monkeypatch,
    after_deadline: timedelta,
) -> None:
    prior = _work()
    before = replace(
        _capture_for_turn(prior, "capture-before"),
        role="before_action",
        action_execution_id="action-blocked",
    )
    error = CaptureCoordinationError(
        stage=CaptureCoordinationStage.ACTION,
        error_code="CLICK_TARGET_OBSTRUCTED",
        message="动作目标被遮挡",
        before_capture=before,
        interaction=InteractionResult(
            task_id=prior.task_id,
            session_id=prior.session_id,
            intent_id="intent-blocked",
            action_execution_id="action-blocked",
            source_capture_id=before.capture_id,
            status=InteractionResultStatus.BLOCKED,
            error_code="CLICK_TARGET_OBSTRUCTED",
            retryable=True,
        ),
    )
    successor = _work(capture_id=before.capture_id, parent_work_id=prior.work_id)
    successor = replace(
        successor,
        deadline=prior.deadline,
        budget=replace(
            successor.budget,
            remaining_actions=2,
            remaining_business_actions=2,
            remaining_duration_ms=29_000,
        ),
    )
    successor = _with_bounded_state(successor, deadline=prior.deadline)
    clock = _MutableClock(prior.deadline - timedelta(milliseconds=1))

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            del tz
            return clock.value

    monkeypatch.setattr(repository_module, "datetime", Clock)
    monkeypatch.setattr(turn_module, "datetime", Clock)
    queue = _RealPrecheckQueue(prior)
    runner = H5ExplorationTurnRunner(
        queue=queue,
        turn_executor=_FailingExecutor(error),
        continuation=_DeadlineCrossingContinuation(
            successor,
            clock,
            after_deadline,
        ),
        lease_owner="h5-explorer-1",
        emit_wakeup=False,
    )

    outcome = await runner.run_one(task_id=prior.task_id, agent_run_id="run-h5")

    transition = queue.transitions[-1]
    assert outcome.final_status is ExplorerWorkStatus.BUDGET_EXHAUSTED
    assert outcome.result_capture_id == before.capture_id
    assert outcome.successor_work_id is None
    assert queue.successors == []
    assert transition.target_status is ExplorerWorkStatus.BUDGET_EXHAUSTED
    assert transition.terminal is not None
    assert transition.terminal.status is ExplorationTerminalStatus.BUDGET_EXHAUSTED
    assert transition.settlement_work is not None
    assert transition.settlement_work.work_id == successor.work_id
    assert transition.settlement_work.source_capture_id == successor.source_capture_id
    assert transition.terminal.coverage_summary["result_capture_id"] == before.capture_id
    assert transition.terminal.coverage_summary["settled_budget"] == (
        transition.settlement_work.budget.to_dict()
    )
    assert transition.terminal.coverage_summary["consumed_action_count"] == 1
    assert transition.terminal.coverage_summary["bounded_exploration_scheduler"][
        "no_progress_count"
    ] == 2
    assert queue.commit_attempts == 0


@pytest.mark.asyncio
async def test_h5_cancellation_does_not_become_budget_exhausted() -> None:
    prior = _work()
    successor = _work(capture_id="capture-after", parent_work_id=prior.work_id)
    cancellation = _CancelAfterContinuation()
    queue = _CancellationQueue(prior)
    runner = H5ExplorationTurnRunner(
        queue=queue,
        turn_executor=_Executor(_action_turn(prior)),
        continuation=_Continuation(successor),
        cancellation=H5CancellationCheckpoint(cancellation),
        lease_owner="h5-explorer-1",
        emit_wakeup=False,
    )

    outcome = await runner.run_one(task_id=prior.task_id, agent_run_id="run-h5")

    assert cancellation.calls >= 5
    assert outcome.final_status is ExplorerWorkStatus.CANCELLED
    assert outcome.final_status is not ExplorerWorkStatus.BUDGET_EXHAUSTED
    assert queue.successors == []
    assert queue.cancelled_work_types == {"exploration"}


@pytest.mark.asyncio
async def test_h5_lease_loss_does_not_become_budget_exhausted() -> None:
    prior = _work()
    successor = _work(capture_id="capture-after", parent_work_id=prior.work_id)
    queue = _LeaseLostQueue(prior)
    runner = H5ExplorationTurnRunner(
        queue=queue,
        turn_executor=_Executor(_action_turn(prior)),
        continuation=_Continuation(successor),
        lease_owner="h5-explorer-1",
        emit_wakeup=False,
    )

    with pytest.raises(AppExplorationLeaseLostError):
        await runner.run_one(task_id=prior.task_id, agent_run_id="run-h5")

    assert [item.target_status for item in queue.transitions] == [ExplorerWorkStatus.RUNNING]
    assert queue.successors == []
