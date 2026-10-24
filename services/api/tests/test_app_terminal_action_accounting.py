"""App 终态收尾时的动作预算计数测试。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from services.api.app.application import app_exploration_turn as runner_module
from services.api.app.application.app_exploration_turn import (
    AppExplorationTurnOutcome,
    AppExplorationTurnRunner,
)
from services.api.app.application.app_runtime_composition import (
    _AppActionBudgetGuard,
    _outcome_consumed_action,
)
from services.api.app.application.exploration_work_queue import (
    ExplorerWorkDeadlineExceededError,
    ExplorerWorkPersistenceError,
    ExplorerWorkStatus,
)
from services.api.tests.test_app_exploration_turn import (
    MemoryWorkQueue,
    RecordingContinuation,
    StaticTurnExecutor,
    _turn_result,
    _work,
)


def _outcome_for_turn(*, turn, final_status: ExplorerWorkStatus = ExplorerWorkStatus.SUCCEEDED):
    return AppExplorationTurnOutcome(
        claimed=True,
        final_status=final_status,
        turn=turn,
    )


@pytest.mark.parametrize(
    ("turn", "expected"),
    (
        (
            SimpleNamespace(is_terminal=True, coordination=None, observation_capture=None),
            False,
        ),
        (
            SimpleNamespace(is_terminal=True, coordination=object(), observation_capture=None),
            True,
        ),
        (
            SimpleNamespace(is_terminal=False, coordination=None, observation_capture=object()),
            False,
        ),
        (
            SimpleNamespace(is_terminal=False, coordination=object(), observation_capture=None),
            True,
        ),
    ),
    ids=("pure_terminal", "terminal_with_coordination", "timed_observation", "action_receipt"),
)
def test_outcome_consumed_action_uses_coordination_before_terminal(
    turn: SimpleNamespace,
    expected: bool,
) -> None:
    """动作协调回执优先于终态标记，纯观察和纯终态不得虚增。"""

    assert _outcome_consumed_action(_outcome_for_turn(turn=turn)) is expected


@pytest.mark.parametrize("successor_work_id", (None, "successor-1"))
def test_outcome_consumed_action_without_turn_keeps_successor_fallback(
    successor_work_id: str | None,
) -> None:
    outcome = AppExplorationTurnOutcome(
        claimed=True,
        final_status=ExplorerWorkStatus.SUCCEEDED,
        turn=None,
        successor_work_id=successor_work_id,
    )

    assert _outcome_consumed_action(outcome) is (successor_work_id is not None)


def test_retry_wait_receipt_does_not_consume_action_budget() -> None:
    guard = _AppActionBudgetGuard(max_actions=2)
    outcome = _outcome_for_turn(
        turn=SimpleNamespace(is_terminal=False, coordination=object(), observation_capture=None),
        final_status=ExplorerWorkStatus.RETRY_WAIT,
    )

    guard.record(outcome)

    assert guard.consumed_actions == 0
    assert guard.terminal_confirmation_claimed is False


class _MutableClock:
    def __init__(self, value: datetime) -> None:
        self.value = value


class _TypedDeadlineRaceQueue(MemoryWorkQueue):
    """模拟数据库在提交后继时发现 deadline 竞态。"""

    def __init__(self, work, clock: _MutableClock) -> None:
        super().__init__(work)
        self.clock = clock
        self.commit_attempts = 0

    async def commit_successor(self, *, command, emit_wakeup: bool = True):
        del emit_wakeup
        self.commit_attempts += 1
        self.clock.value = command.successor.deadline + timedelta(milliseconds=1)
        raise ExplorerWorkDeadlineExceededError(
            "后继提交期间已超过 deadline，必须在父 Work 上结算"
        )


class _DeadlineTextPersistenceFailureQueue(MemoryWorkQueue):
    """普通持久化故障即使消息包含 deadline 也不能被误判为截止竞态。"""

    def __init__(self, work, clock: _MutableClock) -> None:
        super().__init__(work)
        self.clock = clock
        self.commit_attempts = 0

    async def commit_successor(self, *, command, emit_wakeup: bool = True):
        del emit_wakeup
        self.commit_attempts += 1
        self.clock.value = command.successor.deadline + timedelta(milliseconds=1)
        raise ExplorerWorkPersistenceError(
            "普通持久化失败：数据库消息包含 deadline，但不是截止异常"
        )


@pytest.mark.asyncio
async def test_typed_deadline_commit_race_counts_settled_action_once(monkeypatch) -> None:
    """后继提交的 typed deadline 竞态使用后继预算，不能重复加一。"""

    work = _work()
    clock = _MutableClock(work.deadline - timedelta(milliseconds=1))

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            del tz
            return clock.value

    monkeypatch.setattr(runner_module, "datetime", Clock)
    queue = _TypedDeadlineRaceQueue(work, clock)
    original_turn = _turn_result(work)
    runner = AppExplorationTurnRunner(
        queue=queue,
        turn_executor=StaticTurnExecutor(original_turn),
        continuation=RecordingContinuation(),
        lease_owner="app-explorer-1",
    )

    outcome = await runner.run_one(task_id=work.task_id, agent_run_id="run-1")

    assert outcome.final_status is ExplorerWorkStatus.BUDGET_EXHAUSTED
    assert outcome.successor_work_id is None
    assert queue.commit_attempts == 1
    assert queue.successor_commits == []
    assert outcome.turn is not None
    assert outcome.turn.decision.coverage_summary["consumed_action_count"] == 1
    assert outcome.turn.decision.coverage_summary["result_capture_id"] == "capture-after"
    transition = queue.transitions[-1]
    assert transition.target_status is ExplorerWorkStatus.BUDGET_EXHAUSTED
    assert transition.terminal is not None
    assert transition.terminal.work_id == work.work_id
    assert transition.terminal.coverage_summary["consumed_action_count"] == 1


@pytest.mark.asyncio
async def test_persistence_error_with_deadline_text_is_not_swallowed_as_budget(
    monkeypatch,
) -> None:
    """只有专用 deadline 异常可收口，普通错误必须保留 FAILED 诊断。"""

    work = _work()
    clock = _MutableClock(work.deadline - timedelta(milliseconds=1))

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            del tz
            return clock.value

    monkeypatch.setattr(runner_module, "datetime", Clock)
    queue = _DeadlineTextPersistenceFailureQueue(work, clock)
    continuation = RecordingContinuation()
    runner = AppExplorationTurnRunner(
        queue=queue,
        turn_executor=StaticTurnExecutor(_turn_result(work)),
        continuation=continuation,
        lease_owner="app-explorer-1",
    )

    outcome = await runner.run_one(task_id=work.task_id, agent_run_id="run-1")

    assert outcome.final_status is ExplorerWorkStatus.FAILED
    assert outcome.final_status is not ExplorerWorkStatus.BUDGET_EXHAUSTED
    assert outcome.error_code == "APP_EXPLORATION_TURN_FAILED"
    assert queue.commit_attempts == 1
    assert queue.successor_commits == []
    assert len(continuation.commands) == 1
    transition = queue.transitions[-1]
    assert transition.target_status is ExplorerWorkStatus.FAILED
    assert transition.error_message is not None
    assert "普通持久化失败" in transition.error_message
    assert "deadline" in transition.error_message


@pytest.mark.asyncio
async def test_cancellation_during_deadline_commit_race_prevents_budget_terminal(
    monkeypatch,
) -> None:
    """提交截止竞态期间到达的取消，不能被预算终态覆盖。"""
    work = _work()
    clock = _MutableClock(work.deadline - timedelta(milliseconds=1))

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock.value

    monkeypatch.setattr(runner_module, "datetime", Clock)
    queue = _TypedDeadlineRaceQueue(work, clock)

    class Cancellation:
        async def is_cancel_requested(self, *, task_id, agent_run_id):
            return queue.commit_attempts > 0

    runner = AppExplorationTurnRunner(
        queue=queue,
        turn_executor=StaticTurnExecutor(_turn_result(work)),
        continuation=RecordingContinuation(),
        lease_owner="app-explorer-1",
        cancellation=Cancellation(),
    )
    outcome = await runner.run_one(task_id=work.task_id, agent_run_id="run-1")

    assert queue.commit_attempts == 1
    assert outcome.final_status is ExplorerWorkStatus.CANCELLED
    assert outcome.successor_work_id is None
    assert not any(
        item.target_status is ExplorerWorkStatus.BUDGET_EXHAUSTED
        for item in queue.transitions
    )


@pytest.mark.asyncio
async def test_completed_action_counts_when_deadline_crosses_before_successor_commit(
    monkeypatch,
) -> None:
    """动作在 deadline 前开始、After 返回后跨截止时仍须计入已消费动作。"""

    work = _work()
    original_turn = _turn_result(work)
    queue = MemoryWorkQueue(work)
    continuation = RecordingContinuation()
    clock_now = datetime.now(UTC)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            del tz
            return clock_now

    class DeadlineCrossingExecutor(StaticTurnExecutor):
        async def execute(self, *, work, agent_run_id):
            nonlocal clock_now
            assert clock_now < work.deadline
            result = await super().execute(work=work, agent_run_id=agent_run_id)
            clock_now = work.deadline + timedelta(milliseconds=1)
            return result

    monkeypatch.setattr(runner_module, "datetime", Clock)
    executor = DeadlineCrossingExecutor(original_turn)
    runner = AppExplorationTurnRunner(
        queue=queue,
        turn_executor=executor,
        continuation=continuation,
        lease_owner="app-explorer-1",
    )

    outcome = await runner.run_one(task_id=work.task_id, agent_run_id="run-1")

    assert len(executor.calls) == 1
    assert outcome.final_status is ExplorerWorkStatus.BUDGET_EXHAUSTED
    assert outcome.result_capture_id == "capture-after"
    assert outcome.turn is not None
    assert outcome.turn.coordination == original_turn.coordination
    assert outcome.turn.decision.coverage_summary["consumed_action_count"] == 1
    assert outcome.turn.decision.coverage_summary["result_capture_id"] == "capture-after"
    assert queue.transitions[-1].terminal == outcome.turn.decision
    assert continuation.commands == []
    assert queue.successor_commits == []

    guard = _AppActionBudgetGuard(max_actions=work.budget.max_actions)
    guard.record(outcome)
    assert guard.consumed_actions == 1
