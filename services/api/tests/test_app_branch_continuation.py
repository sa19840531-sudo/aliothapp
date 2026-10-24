"""App 分支无进展恢复的单轮派发回归。"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from services.api.app.application.app_branch_continuation import (
    app_branch_no_progress_terminal,
    resume_app_frontier_branch,
)
from services.api.app.application.app_exploration_turn import AppExplorationTurnRunner
from services.api.app.application.app_frontier_restoration import (
    APP_FRONTIER_RESTORE_ATTEMPTS_KEY,
    APP_FRONTIER_RESTORE_MAX_ATTEMPTS,
)
from services.api.app.application.app_global_frontier import AppGlobalFrontierService
from services.api.app.application.bounded_exploration_scheduler import (
    BOUNDED_EXPLORATION_STATE_KEY,
    BoundedExplorationState,
)
from services.api.app.application.capture_profiles import CaptureProfilePolicy
from services.api.app.application.exploration_dispatch_guard import (
    bounded_dispatch_terminal,
)
from services.api.app.application.exploration_work_queue import ExplorerWorkStatus
from services.api.app.application.explorer_turn import ExplorerTurnResult
from services.api.app.domain.exploration import (
    AppFrontierStatus,
    CandidateAction,
    ExplorationTerminal,
    ExplorationTerminalStatus,
)
from services.api.app.domain.interaction import (
    ActionIntent,
    InteractionOperation,
    InteractionRiskLevel,
    SurfaceKind,
)
from services.api.tests.test_app_exploration_turn import (
    MemoryAppFrontierQueue,
    MemoryWorkQueue,
    RecordingContinuation,
    SplitTurnExecutor,
    _global_frontier_work,
)


def _scheduler_state(work, **overrides: object) -> dict[str, object]:
    """构造真实 scheduler 账本，并保留用于断言的非无进展计数。"""

    state = BoundedExplorationState.create(max_actions=work.budget.max_actions)
    values = {
        "consumed_actions": {"required": 1},
        "consumed_duration_ms": {"required": 120},
        "retry_attempts": {"required": 1},
        "no_progress_count": state.policy.no_progress_limit,
        "repeat_count": 1,
        "page_repeat_count": 1,
        "popup_repeat_count": 1,
        "return_repeat_count": 1,
    }
    values.update(overrides)
    state = replace(state, **values)
    return state.to_dict()


def _work_with_scheduler_state(**state_overrides: object):
    work = _global_frontier_work()
    metadata = {
        **dict(work.frontier.observation.metadata),
        "covered_page_types": ["home"],
        "covered_milestones": ["surface:stable"],
        "covered_business_page_count": 1,
        "coverage_marker": "preserve-me",
        BOUNDED_EXPLORATION_STATE_KEY: _scheduler_state(
            work,
            **state_overrides,
        ),
    }
    return replace(
        work,
        frontier=replace(
            work.frontier,
            observation=replace(work.frontier.observation, metadata=metadata),
        ),
    )


def _frontier_action(work, *, restored: bool) -> ActionIntent:
    candidate = work.frontier.candidates[0]
    parameters: dict[str, object] = {"candidate_id": candidate.candidate_id}
    if restored:
        parameters["app_frontier_item_id"] = "frontier-item-restored"
    return ActionIntent(
        task_id=work.task_id,
        session_id=work.session_id,
        intent_id="intent-branch-continuation",
        idempotency_key="intent-branch-continuation",
        surface_kind=work.surface_kind,
        source_capture_id=work.source_capture_id,
        action_type=candidate.action_type,
        semantic_target=candidate.semantic_target,
        parameters=parameters,
        purpose="恢复任务级 Frontier 分支",
        risk_level=candidate.risk_level,
        capture_profile=CaptureProfilePolicy.for_surface(
            work.surface_kind,
            candidate.action_type,
        ),
        deadline=work.deadline,
    )


class _DispatchCheckingExecutor(SplitTurnExecutor):
    """在真实 runner 执行器边界调用派发闸门，拒绝测试替身绕过它。"""

    def __init__(self) -> None:
        super().__init__(selected_candidate_id="candidate-a")
        self.dispatch_results = []
        self.dispatch_work_states = []

    async def execute_decision(self, *, work, agent_run_id, decision):
        self.dispatch_work_states.append(work)
        bounded = bounded_dispatch_terminal(
            work=work,
            decision=decision,
            now=datetime.now(UTC),
        )
        self.dispatch_results.append(bounded)
        if bounded is not None:
            return ExplorerTurnResult(
                work_id=work.work_id,
                task_id=work.task_id,
                decision=bounded,
                coordination=None,
            )
        if isinstance(decision, ExplorationTerminal):
            return ExplorerTurnResult(
                work_id=work.work_id,
                task_id=work.task_id,
                decision=decision,
                coordination=None,
            )
        return await super().execute_decision(
            work=work,
            agent_run_id=agent_run_id,
            decision=decision,
        )


@pytest.mark.asyncio
async def test_run_one_restores_no_progress_branch_and_dispatches_action() -> None:
    """真实 run_one 恢复 sibling 后应能通过派发闸门并提交后继。"""

    work = _work_with_scheduler_state()
    prior_state = work.frontier.observation.metadata[BOUNDED_EXPLORATION_STATE_KEY]
    queue = MemoryWorkQueue(work)
    frontier_queue = MemoryAppFrontierQueue()
    executor = _DispatchCheckingExecutor()
    continuation = RecordingContinuation(
        page_type="content_detail",
        emit_action_observation=True,
    )
    runner = AppExplorationTurnRunner(
        queue=queue,
        turn_executor=executor,
        continuation=continuation,
        lease_owner="app-explorer-1",
        global_frontier=AppGlobalFrontierService(repository=frontier_queue),
        frontier_queue=frontier_queue,
    )

    outcome = await runner.run_one(task_id=work.task_id, agent_run_id="run-1")

    assert outcome.final_status is ExplorerWorkStatus.SUCCEEDED
    assert outcome.successor_work_id == continuation.successor_work_id
    assert len(executor.dispatch_results) == 1
    assert executor.dispatch_results == [None]
    assert isinstance(outcome.turn.decision, ActionIntent)
    assert outcome.turn.decision.parameters.get("app_frontier_item_id")
    assert len(queue.successor_commits) == 1

    settlement = queue.successor_commits[0].app_frontier_settlement
    assert settlement is not None
    # RecordingContinuation 已补齐导航结算四件套与身份变化，到达应记 VISITED。
    assert settlement.target_status is AppFrontierStatus.VISITED
    assert settlement.result_capture_id == "capture-after"
    assert settlement.error_code is None

    successor = queue.successor_commits[0].successor
    successor_metadata = successor.frontier.observation.metadata
    successor_state = successor_metadata[BOUNDED_EXPLORATION_STATE_KEY]
    assert successor_metadata["coverage_marker"] == "preserve-me"
    assert successor_metadata["covered_page_types"] == ("home",)
    assert successor_metadata["covered_business_page_count"] == 1
    assert successor_state["no_progress_count"] == 0
    for key in (
        "consumed_actions",
        "consumed_duration_ms",
        "retry_attempts",
        "repeat_count",
        "page_repeat_count",
        "popup_repeat_count",
        "return_repeat_count",
    ):
        assert successor_state[key] == prior_state[key], key
    assert successor_state["max_actions"] == prior_state["max_actions"]
    assert successor.budget.max_actions == work.budget.max_actions
    assert successor.budget.remaining_actions == work.budget.remaining_actions - 1


@pytest.mark.asyncio
async def test_run_one_without_pending_frontier_keeps_evidence_insufficient_branch() -> None:
    """没有可恢复 sibling 时保留 evidence_insufficient，不伪造 reset。"""

    work = _work_with_scheduler_state()
    queue = MemoryWorkQueue(work)
    frontier_queue = MemoryAppFrontierQueue()
    global_frontier = AppGlobalFrontierService(repository=frontier_queue)
    registered = await global_frontier.register_work_candidates(
        work=work,
        agent_run_id="run-1",
    )
    for item in registered:
        frontier_queue.statuses[item.frontier_item_id] = AppFrontierStatus.VISITED

    executor = _DispatchCheckingExecutor()
    runner = AppExplorationTurnRunner(
        queue=queue,
        turn_executor=executor,
        continuation=RecordingContinuation(),
        lease_owner="app-explorer-1",
        global_frontier=global_frontier,
        frontier_queue=frontier_queue,
    )

    outcome = await runner.run_one(task_id=work.task_id, agent_run_id="run-1")

    assert outcome.final_status is ExplorerWorkStatus.NO_PROGRESS
    assert outcome.successor_work_id is None
    assert outcome.turn is not None
    assert outcome.turn.decision.outcome == "evidence_insufficient"
    assert outcome.turn.decision.reason_code == "app_global_frontier_exhausted_without_completion"
    assert queue.successor_commits == []
    assert executor.dispatch_work_states
    terminal_state = executor.dispatch_work_states[0].frontier.observation.metadata[
        BOUNDED_EXPLORATION_STATE_KEY
    ]
    assert terminal_state["no_progress_count"] == 3


@pytest.mark.parametrize(
    "stop_kind",
    (
        "action_budget",
        "duration_budget",
        "deadline",
        "retry_budget",
        "repeat_limit",
        "page_repeat_limit",
        "popup_loop_limit",
        "return_loop_limit",
    ),
)
@pytest.mark.parametrize("no_progress_count", (0, 3))
def test_hard_scheduler_stops_are_not_reset_by_branch_continuation(
    stop_kind: str,
    no_progress_count: int,
) -> None:
    """动作、时间和循环硬停止不是可恢复的 no_progress 分支。"""

    work = _work_with_scheduler_state()
    base_state = BoundedExplorationState.from_dict(
        work.frontier.observation.metadata[BOUNDED_EXPLORATION_STATE_KEY],
        default_max_actions=work.budget.max_actions,
    )
    overrides: dict[str, object] = {}
    if stop_kind == "action_budget":
        overrides["consumed_actions"] = {"required": work.budget.max_actions}
    elif stop_kind == "duration_budget":
        overrides["max_duration_ms"] = 120
        overrides["consumed_duration_ms"] = {"required": 120}
    elif stop_kind == "deadline":
        overrides["deadline"] = datetime.now(UTC) - timedelta(seconds=1)
    elif stop_kind == "retry_budget":
        overrides["retry_attempts"] = {
            "required": base_state.policy.retry_limit,
            "evidence_gap": base_state.policy.retry_limit,
            "discovery": base_state.policy.retry_limit,
        }
    elif stop_kind == "repeat_limit":
        overrides["repeat_count"] = base_state.policy.repeat_limit
    elif stop_kind == "page_repeat_limit":
        overrides["page_repeat_count"] = base_state.policy.repeat_limit
    elif stop_kind == "popup_loop_limit":
        overrides["popup_repeat_count"] = base_state.policy.repeat_limit
    else:
        overrides["return_repeat_count"] = base_state.policy.repeat_limit
    work = _work_with_scheduler_state(
        no_progress_count=no_progress_count,
        **overrides,
    )

    terminal = app_branch_no_progress_terminal(work)
    decision = _frontier_action(work, restored=True)
    restored = resume_app_frontier_branch(work, decision)
    bounded = bounded_dispatch_terminal(
        work=work,
        decision=decision,
        now=datetime.now(UTC),
    )

    assert terminal is None
    assert restored is work
    assert bounded is not None
    expected_reason = {
        "action_budget": "bounded_exploration_action_budget_exhausted",
        "duration_budget": "bounded_exploration_duration_budget_exhausted",
        "deadline": "bounded_exploration_deadline_expired",
        "retry_budget": "bounded_exploration_retry_budget_exhausted",
        "repeat_limit": "bounded_exploration_repeat_limit",
        "page_repeat_limit": "bounded_exploration_repeated_page_limit",
        "popup_loop_limit": "bounded_exploration_popup_loop_limit",
        "return_loop_limit": "bounded_exploration_return_loop_limit",
    }[stop_kind]
    if stop_kind == "repeat_limit" and no_progress_count == 3:
        expected_reason = "bounded_exploration_no_progress_limit"
    assert bounded.reason_code == expected_reason
    assert (
        work.frontier.observation.metadata[BOUNDED_EXPLORATION_STATE_KEY]["no_progress_count"]
        == no_progress_count
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "restore_attempt_count",
    (APP_FRONTIER_RESTORE_MAX_ATTEMPTS - 1, APP_FRONTIER_RESTORE_MAX_ATTEMPTS),
)
async def test_restore_only_respects_item_attempt_window(
    restore_attempt_count: int,
) -> None:
    """restore_only 达到 item 上限后收口，窗口内可派发且不清零次数。"""

    source = _work_with_scheduler_state()
    frontier_queue = MemoryAppFrontierQueue()
    global_frontier = AppGlobalFrontierService(repository=frontier_queue)
    registered = await global_frontier.register_work_candidates(
        work=source,
        agent_run_id="run-1",
    )
    first = next(item for item in registered if item.candidate.candidate_id == "candidate-a")
    system_back = CandidateAction(
        candidate_id="candidate-system-back",
        semantic_target="系统返回",
        action_type=InteractionOperation.BACK,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=10,
        navigation_depth=0,
        expected_surface_kind=SurfaceKind.ANDROID,
        metadata={
            "candidate_id": "candidate-system-back",
            "purpose": "返回上一页",
            "expected_state_hint": "back",
            "frontier_scope": "local",
        },
    )
    attempts = {first.frontier_item_id: restore_attempt_count}
    work = replace(
        source,
        frontier=replace(
            source.frontier,
            observation=replace(
                source.frontier.observation,
                metadata={
                    **dict(source.frontier.observation.metadata),
                    "business_page_identity": "page:video-list",
                    APP_FRONTIER_RESTORE_ATTEMPTS_KEY: attempts,
                },
            ),
            candidates=(system_back,),
        ),
    )
    runner = AppExplorationTurnRunner(
        queue=MemoryWorkQueue(work),
        turn_executor=SplitTurnExecutor(selected_candidate_id="candidate-system-back"),
        continuation=RecordingContinuation(),
        lease_owner="app-explorer-1",
        global_frontier=global_frontier,
        frontier_queue=frontier_queue,
    )
    proposed = ExplorationTerminal(
        work_id=work.work_id,
        task_id=work.task_id,
        status=ExplorationTerminalStatus.NO_PROGRESS,
        reason="当前页候选耗尽",
    )

    decision, assigned = await runner._restore_pending_frontier(
        work=work,
        agent_run_id="run-1",
        proposed_terminal=proposed,
    )

    assert isinstance(decision, ActionIntent)
    assert decision.parameters.get("app_frontier_restore_only") is True
    assert assigned is None
    if restore_attempt_count == APP_FRONTIER_RESTORE_MAX_ATTEMPTS:
        assert frontier_queue.statuses[first.frontier_item_id] is AppFrontierStatus.SUPERSEDED
        superseded = next(
            command
            for command in frontier_queue.transitions
            if command.frontier_item_id == first.frontier_item_id
            and command.target_status is AppFrontierStatus.SUPERSEDED
        )
        assert superseded.error_code == "APP_FRONTIER_RESTORE_EXHAUSTED"
        assert decision.parameters.get("app_frontier_item_id") != first.frontier_item_id
        return

    assert frontier_queue.statuses[first.frontier_item_id] is AppFrontierStatus.PENDING
    assert decision.parameters.get("app_frontier_item_id") == first.frontier_item_id
    resumed = resume_app_frontier_branch(work, decision)
    assert resumed is not work
    assert resumed.frontier.observation.metadata[APP_FRONTIER_RESTORE_ATTEMPTS_KEY] == attempts
    assert (
        resumed.frontier.observation.metadata[BOUNDED_EXPLORATION_STATE_KEY]["no_progress_count"]
        == 0
    )
    assert (
        bounded_dispatch_terminal(
            work=resumed,
            decision=decision,
            now=datetime.now(UTC),
        )
        is None
    )


def test_action_without_frontier_restore_does_not_reset_no_progress_branch() -> None:
    """普通 Action 没有真实 Frontier 恢复标识时必须继续被派发闸门拦截。"""

    work = _work_with_scheduler_state()
    decision = _frontier_action(work, restored=False)

    terminal = app_branch_no_progress_terminal(work)
    restored = resume_app_frontier_branch(work, decision)
    bounded = bounded_dispatch_terminal(
        work=work,
        decision=decision,
        now=datetime.now(UTC),
    )

    assert terminal is not None
    assert terminal.reason_code == "bounded_exploration_no_progress_limit"
    assert restored is work
    assert bounded is not None
    assert bounded.reason_code == "bounded_exploration_no_progress_limit"
    assert (
        work.frontier.observation.metadata[BOUNDED_EXPLORATION_STATE_KEY]["no_progress_count"] == 3
    )
