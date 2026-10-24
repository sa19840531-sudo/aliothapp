"""H5 Explorer Work 租约、执行和后继提交测试。"""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from importlib import import_module
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from services.api.app.agents.explorer import (
    ExplorerAgentOutputError,
    ExplorerModelUnavailableError,
)
from services.api.app.application.bounded_exploration_scheduler import (
    BOUNDED_EXPLORATION_STATE_KEY,
    BoundedExplorationState,
)
from services.api.app.application.capture_coordination import (
    CaptureCoordinationError,
    CaptureCoordinationResult,
    CaptureCoordinationStage,
)
from services.api.app.application.exploration_dispatch_guard import (
    bounded_dispatch_terminal,
)
from services.api.app.application.exploration_work_queue import (
    CommittedExplorerWorkSuccessor,
    ExplorerWorkLease,
    ExplorerWorkStatus,
)
from services.api.app.application.explorer_turn import ExplorerTurnResult
from services.api.app.application.h5_exploration_cancellation import (
    H5CancellationCheckpoint,
)
from services.api.app.application.h5_exploration_recovery import (
    H5BrowserRecoveryAttemptFacts,
    H5BrowserRecoveryFacts,
    H5FailureStage,
    H5RecoveryOutcome,
    build_h5_recovery_session_id,
)
from services.api.app.application.h5_exploration_workflow import (
    H5ExplorationResult,
    _with_task_frontier,
)
from services.api.app.application.h5_page_type_assessment_adapter import (
    H5PageTypeAssessmentAdapterResult,
)
from services.api.app.domain.exploration import (
    CandidateAction,
    ExplorationBoundaries,
    ExplorationBudget,
    ExplorationTerminal,
    ExplorationTerminalStatus,
    FrontierSnapshot,
    SideEffectPolicy,
    SurfaceObservation,
    build_exploration_work,
)
from services.api.app.domain.exploration.h5_frontier import (
    H5_TASK_FRONTIER_METADATA_KEY,
    H5TaskFrontierSnapshot,
)
from services.api.app.domain.interaction import (
    ActionIntent,
    CaptureDirective,
    CaptureKind,
    CaptureProfile,
    CaptureRef,
    InteractionOperation,
    InteractionResult,
    InteractionResultStatus,
    InteractionRiskLevel,
    SurfaceKind,
)


def _turn_module():
    return import_module("services.api.app.application.h5_exploration_turn")


def _work(
    *,
    surface_kind: SurfaceKind = SurfaceKind.WEB,
    capture_id: str = "capture-1",
    parent_work_id: str | None = None,
):
    now = datetime.now(UTC)
    candidate = CandidateAction(
        candidate_id="candidate-profile",
        semantic_target="我的",
        action_type=InteractionOperation.CLICK,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=90,
        navigation_depth=1,
        expected_surface_kind=surface_kind,
    )
    frontier = FrontierSnapshot(
        snapshot_id=f"frontier:{capture_id}",
        observation=SurfaceObservation(
            capture_id=capture_id,
            surface_kind=surface_kind,
            location=(
                "https://m.example.test/home"
                if surface_kind is SurfaceKind.WEB
                else "app://example/home"
            ),
            state_version=f"state:{capture_id}",
            observed_at=now,
            metadata=({"parent_work_id": parent_work_id} if parent_work_id is not None else {}),
        ),
        candidates=(candidate,),
    )
    return build_exploration_work(
        task_id="task-h5",
        session_id="h5:run-h5",
        branch_id=f"h5:{capture_id}",
        objective="探索 H5 业务页面",
        surface_kind=surface_kind,
        source_capture_id=capture_id,
        frontier_snapshot_id=frontier.snapshot_id,
        frontier=frontier,
        boundaries=ExplorationBoundaries(
            allowed_hosts=("m.example.test",),
            allowed_operations=(InteractionOperation.CLICK,),
            max_navigation_depth=3,
            side_effect_policy=SideEffectPolicy.READ_ONLY,
        ),
        budget=ExplorationBudget(
            max_actions=3,
            remaining_actions=3,
            max_depth=3,
            remaining_duration_ms=30_000,
        ),
        deadline=now + timedelta(minutes=1),
    )


def test_h5_dispatch_guard_does_not_reject_sibling_by_queue_retry_count() -> None:
    """H5 派发复核必须沿用候选级重试口径，不能恢复队列级连坐。"""
    work = _work()
    state = replace(
        BoundedExplorationState.create(max_actions=work.budget.max_actions),
        retry_attempts={"discovery": 2},
    )
    work = replace(
        work,
        frontier=replace(
            work.frontier,
            observation=replace(
                work.frontier.observation,
                metadata={
                    BOUNDED_EXPLORATION_STATE_KEY: state.to_dict(),
                    H5_TASK_FRONTIER_METADATA_KEY: {"schema_version": "test-only"},
                },
            ),
        ),
    )
    candidate = work.frontier.candidates[0]
    decision = ActionIntent(
        task_id=work.task_id,
        session_id=work.session_id,
        intent_id="intent-h5-sibling",
        idempotency_key="intent-h5-sibling",
        surface_kind=SurfaceKind.WEB,
        source_capture_id=work.source_capture_id,
        action_type=candidate.action_type,
        semantic_target=candidate.semantic_target,
        parameters={"candidate_id": candidate.candidate_id},
        purpose="继续尚未尝试的 H5 兄弟入口",
        risk_level=candidate.risk_level,
        capture_profile=CaptureProfile(
            profile_id="h5-dispatch-guard-test",
            before=CaptureDirective(
                role="before_action",
                artifact_types=("screenshot",),
            ),
            after=CaptureDirective(
                role="after_action",
                artifact_types=("screenshot",),
            ),
        ),
        deadline=work.deadline,
    )

    assert (
        bounded_dispatch_terminal(
            work=work,
            decision=decision,
            now=datetime.now(UTC),
        )
        is None
    )


@pytest.mark.parametrize("knowledge_source", ["contextual", None])
@pytest.mark.parametrize("current_target_matches", [True, False])
@pytest.mark.parametrize("recovery_pending", [True, False])
def test_global_bottom_navigation_reexecution_accepts_contextual_contract(
    knowledge_source, current_target_matches, recovery_pending,
) -> None:
    """受信上下文合同在当前底栏仍可见时允许直接重执行。"""
    module = _turn_module()
    candidate = CandidateAction(
        candidate_id="candidate-contextual-navigation",
        semantic_target=(
            "div.van-tabbar:visible > div.van-tabbar-item:nth-of-type(2):visible"
        ),
        action_type=InteractionOperation.CLICK,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=95,
        navigation_depth=1,
        expected_surface_kind=SurfaceKind.WEB,
        metadata={
            "target_keywords": ["bottom_navigation", "二次元", "content_list"],
            "expected_next_page_type": "content_list",
            "navigation_knowledge_source": knowledge_source,
            "navigation_knowledge_confidence": 1.0,
            "action_parameters": {
                "target_mode": "bottom_navigation",
                "navigation_index": 2,
                "navigation_label": "二次元",
            },
        },
    )
    work = _work()
    frontier = replace(
        work.frontier,
        observation=replace(
            work.frontier.observation,
            page_type="home",
            page_state="stable",
            metadata={
                "h5_frontier_source_recovery_pending": recovery_pending,
                "global_bottom_navigation_indices": (2,),
                "global_bottom_navigation_targets": [{
                    "semantic_target": candidate.semantic_target,
                    "action_parameters": {
                        **candidate.metadata["action_parameters"],
                        "navigation_label": "二次元" if current_target_matches else "其他栏目",
                    },
                }],
            },
        ),
        candidates=(candidate,),
    )
    work = replace(work, frontier_snapshot_id=frontier.snapshot_id, frontier=frontier)
    entry = SimpleNamespace(
        candidate=candidate,
        source_location=work.frontier.observation.location,
    )

    assert module._can_execute_global_bottom_navigation(work=work, entry=entry) is (
        current_target_matches or (knowledge_source is not None and not recovery_pending)
    )
    snapshot = H5TaskFrontierSnapshot.create(max_pages=None).merge(
        source_capture_id=work.source_capture_id,
        source_state_version="old-state", source_location=entry.source_location,
        navigation_depth=0, candidates=(candidate,),
    )
    snapshot = snapshot.select_entry(snapshot.pending_entries[0].entry_id)
    bound_frontier = _with_task_frontier(frontier=frontier, snapshot=snapshot)
    bound_work = replace(
        work, frontier=bound_frontier, frontier_snapshot_id=bound_frontier.snapshot_id,
    )
    assert (module._selected_entry_requiring_recovery(bound_work) is None) is (
        current_target_matches or (knowledge_source is not None and not recovery_pending)
    )


def test_global_bottom_navigation_reexecution_rejects_page_tab_candidate() -> None:
    """页内栏目不得按旧 Vant 底栏合同进入全局底栏快速路径。"""
    module = _turn_module()
    candidate = CandidateAction(
        candidate_id="candidate-page-tab-navigation",
        semantic_target="internal:h5_page_tab:2",
        action_type=InteractionOperation.CLICK,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=95,
        navigation_depth=1,
        expected_surface_kind=SurfaceKind.WEB,
        metadata={
            "target_keywords": ["page_tab_navigation", "国产", "home"],
            "expected_next_page_type": "home",
            "navigation_knowledge_source": "contextual",
            "navigation_knowledge_confidence": 1.0,
            "action_parameters": {
                "target_mode": "page_tab_navigation",
                "navigation_index": 2,
                "navigation_label": "国产",
                "interaction_pattern_signature": "a" * 64,
            },
        },
    )
    work = _work()
    frontier = replace(
        work.frontier,
        observation=replace(
            work.frontier.observation,
            page_type="home",
            page_state="stable",
            metadata={"global_bottom_navigation_indices": (2,)},
        ),
        candidates=(candidate,),
    )
    work = replace(work, frontier_snapshot_id=frontier.snapshot_id, frontier=frontier)
    entry = SimpleNamespace(
        candidate=candidate,
        source_location=work.frontier.observation.location,
    )

    assert module._can_execute_global_bottom_navigation(work=work, entry=entry) is False


class _Queue:
    def __init__(self, work) -> None:
        self.work = work
        self.transitions = []
        self.renewals = []
        self.successors = []
        self.successor_wakeups = []
        self.payload_updates = []

    async def claim(self, *, command, lease_token):
        return ExplorerWorkLease(
            work=self.work,
            status=ExplorerWorkStatus.LEASED,
            lease_token=lease_token,
            attempt_count=1,
            row_version=4,
        )

    async def transition(self, *, command):
        self.transitions.append(command)
        return command.row_version + 1

    async def renew(self, *, command):
        self.renewals.append(command)

    async def commit_successor(self, *, command, emit_wakeup: bool = True):
        self.successors.append(command)
        self.successor_wakeups.append(emit_wakeup)
        return CommittedExplorerWorkSuccessor(
            parent_row_version=command.row_version + 1,
            successor_work_id=command.successor.work_id,
            successor_created=True,
        )

    async def update_payload(self, *, command):
        self.payload_updates.append(command)
        return command.row_version + 1


class _ClaimOnceQueue(_Queue):
    def __init__(self, work) -> None:
        super().__init__(work)
        self.claims = 0

    async def claim(self, *, command, lease_token):
        self.claims += 1
        if self.claims > 1:
            return None
        return await super().claim(command=command, lease_token=lease_token)


class _CancellationQueue(_Queue):
    def __init__(self, work) -> None:
        super().__init__(work)
        self.cancelled_work_types: set[str] = set()

    async def cancel_nonterminal(self, *, task_id, reason, work_types, h5_only):
        assert task_id == self.work.task_id
        assert reason
        assert tuple(item.value for item in work_types) == ("exploration",)
        assert h5_only is True
        self.cancelled_work_types.add("exploration")
        return 2


class _RequestedCancellation:
    async def is_cancel_requested(self, *, task_id, agent_run_id):
        assert task_id == "task-h5"
        assert agent_run_id == "run-h5"
        return True


class _DeferredCancellation:
    def __init__(self) -> None:
        self.requested = False

    async def is_cancel_requested(self, *, task_id, agent_run_id):
        assert task_id == "task-h5"
        assert agent_run_id == "run-h5"
        return self.requested


class _Executor:
    def __init__(self, turn) -> None:
        self.turn = turn

    async def execute(self, *, work, agent_run_id):
        assert work.work_id == self.turn.work_id
        assert agent_run_id == "run-h5"
        return self.turn


class _CancelDuringCaptureExecutor:
    def __init__(self, *, cancellation: _DeferredCancellation) -> None:
        self._cancellation = cancellation

    async def execute(self, *, work, agent_run_id):
        assert work.task_id == "task-h5"
        assert agent_run_id == "run-h5"
        self._cancellation.requested = True
        raise CaptureCoordinationError(
            stage=CaptureCoordinationStage.BEFORE_CAPTURE,
            error_code="before_capture_failed",
            message="取消与动作前采集同时发生",
        )


class _BrowserLossExecutor:
    def __init__(self, *, stage: CaptureCoordinationStage) -> None:
        self.stage = stage
        self.calls = 0

    async def execute(self, *, work, agent_run_id):
        del work, agent_run_id
        self.calls += 1
        raise CaptureCoordinationError(
            stage=self.stage,
            error_code="browser_session_lost",
            message="浏览器进程已丢失",
            cause_type="TargetClosedError",
        )


class _BrowserLossRecovery:
    def __init__(self, *, outcome: H5RecoveryOutcome) -> None:
        self.outcome = outcome
        self.calls = []

    async def recover(self, *, work, agent_run_id, error):
        self.calls.append((work, agent_run_id, error))
        successor = self.outcome.recovery_work
        if successor is None:
            return self.outcome
        stage = H5FailureStage(error.stage.value)
        target_session_id = build_h5_recovery_session_id(
            agent_run_id=agent_run_id,
            work_id=work.work_id,
            stage=stage,
            recovery_attempt_count=1,
        )
        facts = H5BrowserRecoveryFacts.create(
            task_id=work.task_id,
            agent_run_id=agent_run_id,
            failed_work_id=work.work_id,
            source_session_id=work.session_id,
            target_session_id=target_session_id,
            failure_stage=stage,
            recovery_attempt_count=1,
            original_source_capture_id=work.source_capture_id,
            rebaseline_before_capture_id="capture-recovery-before",
            rebaseline_after_capture_id=successor.source_capture_id,
            failed_action_execution_id=(
                None if stage is H5FailureStage.BEFORE_CAPTURE else "action-unknown"
            ),
            restoration_action_execution_id="action-recovery-navigation",
            inventory_status=(None if stage is H5FailureStage.BEFORE_CAPTURE else "insufficient"),
            source_state_version=work.frontier.observation.state_version,
            target_state_version=successor.frontier.observation.state_version,
            equivalent=True,
            reason_code=None,
        )
        frontier = replace(
            successor.frontier,
            observation=replace(
                successor.frontier.observation,
                metadata={
                    **dict(successor.frontier.observation.metadata),
                    "h5_browser_recovery": facts.to_dict(),
                },
            ),
        )
        return replace(
            self.outcome,
            recovery_work=replace(
                successor,
                session_id=target_session_id,
                frontier=frontier,
                frontier_snapshot_id=frontier.snapshot_id,
            ),
        )


class _FailingExecutor:
    def __init__(self, error: Exception) -> None:
        self.error = error

    async def execute(self, *, work, agent_run_id):
        del work, agent_run_id
        raise self.error


class _Continuation:
    def __init__(self, successor) -> None:
        self.successor = successor
        self.calls = []

    async def prepare_after_action(self, command):
        self.calls.append(command)
        return H5ExplorationResult(
            detection_result={"page_capture_id": self.successor.source_capture_id},
            page_type_assessment=H5PageTypeAssessmentAdapterResult(execution_status="skipped"),
            work=self.successor,
        )

    async def prepare_after_action_failure(self, command):
        self.calls.append(command)
        frontier = replace(
            self.successor.frontier,
            observation=replace(
                self.successor.frontier.observation,
                metadata={
                    **dict(self.successor.frontier.observation.metadata),
                    "h5_action_reobservation": {
                        "schema_version": "h5-action-reobservation.v1",
                        "task_id": command.prior_work.task_id,
                        "session_id": command.prior_work.session_id,
                        "failed_work_id": command.prior_work.work_id,
                        "before_capture_id": command.before_capture.capture_id,
                        "error_code": command.error_code,
                        "retryable": command.retryable,
                    },
                },
            ),
        )
        successor = replace(
            self.successor,
            frontier=frontier,
            frontier_snapshot_id=frontier.snapshot_id,
        )
        return H5ExplorationResult(
            detection_result={"page_capture_id": successor.source_capture_id},
            page_type_assessment=H5PageTypeAssessmentAdapterResult(execution_status="skipped"),
            work=successor,
        )


def _terminal_turn(work) -> ExplorerTurnResult:
    return ExplorerTurnResult(
        work_id=work.work_id,
        task_id=work.task_id,
        decision=ExplorationTerminal(
            work_id=work.work_id,
            task_id=work.task_id,
            status=ExplorationTerminalStatus.COMPLETED,
            reason="当前分支 Frontier 已清空",
        ),
        coordination=None,
    )


@pytest.mark.asyncio
async def test_h5_runner_consumes_web_terminal_without_marking_task_directly() -> None:
    module = _turn_module()
    work = _work()
    queue = _Queue(work)
    runner = module.H5ExplorationTurnRunner(
        queue=queue,
        turn_executor=_Executor(_terminal_turn(work)),
        continuation=_Continuation(_work(capture_id="capture-2")),
        lease_owner="h5-explorer-1",
    )

    outcome = await runner.run_one(task_id="task-h5", agent_run_id="run-h5")

    assert outcome.claimed is True
    assert outcome.final_status is ExplorerWorkStatus.SUCCEEDED, queue.transitions[-1].error_message
    assert outcome.turn.is_terminal is True
    assert [item.target_status for item in queue.transitions] == [
        ExplorerWorkStatus.RUNNING,
        ExplorerWorkStatus.SUCCEEDED,
    ]
    assert queue.successors == []


@pytest.mark.asyncio
async def test_h5_runner_accepts_completed_empty_task_frontier_with_recovery() -> None:
    module = _turn_module()
    work = _work()
    snapshot = H5TaskFrontierSnapshot.create(max_pages=None)
    metadata = {
        **dict(work.frontier.observation.metadata),
        "h5_task_frontier": snapshot.to_dict(),
        "h5_task_frontier_selected_entry_id": None,
        "objective_satisfied": True,
    }
    frontier = replace(
        work.frontier,
        snapshot_id=snapshot.snapshot_id,
        observation=replace(work.frontier.observation, metadata=metadata),
        candidates=(),
    )
    work = replace(work, frontier_snapshot_id=frontier.snapshot_id, frontier=frontier)

    class Recovery:
        async def restore(self, *, work, agent_run_id, entry):
            raise AssertionError("终止态空 Frontier 不应触发页面恢复")

    queue = _Queue(work)
    runner = module.H5ExplorationTurnRunner(
        queue=queue,
        turn_executor=_Executor(_terminal_turn(work)),
        continuation=_Continuation(_work(capture_id="capture-2")),
        recovery=Recovery(),
        lease_owner="h5-explorer-1",
    )

    outcome = await runner.run_one(task_id="task-h5", agent_run_id="run-h5")

    assert outcome.final_status is ExplorerWorkStatus.SUCCEEDED, queue.transitions[-1].error_message
    assert [item.target_status for item in queue.transitions] == [
        ExplorerWorkStatus.RUNNING,
        ExplorerWorkStatus.SUCCEEDED,
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("new_capture", [False, True])
async def test_h5_runner_settles_stale_sibling_after_objective_is_satisfied(new_capture) -> None:
    """规则覆盖已满足后，残留 sibling 仍必须恢复执行或明确结算。"""
    from services.api.app.application.h5_exploration_workflow import _with_task_frontier

    module = _turn_module()
    work = _work()
    snapshot = H5TaskFrontierSnapshot.create(max_pages=None).merge(
        source_capture_id="capture-home",
        source_state_version="state:home",
        source_location="https://m.example.test/home",
        navigation_depth=0,
        candidates=work.frontier.candidates,
    )
    snapshot = snapshot.select_entry(snapshot.pending_entries[0].entry_id)
    frontier = _with_task_frontier(
        frontier=replace(
            work.frontier,
            observation=replace(
                work.frontier.observation,
                capture_id="capture-detail",
                state_version="state:detail",
                location="https://m.example.test/detail/1",
            ),
        ),
        snapshot=snapshot,
    )
    frontier = replace(
        frontier,
        observation=replace(
            frontier.observation,
            metadata={
                **dict(frontier.observation.metadata),
                "objective_satisfied": True,
            },
        ),
    )
    work = replace(
        work,
        source_capture_id="capture-detail",
        frontier_snapshot_id=frontier.snapshot_id,
        frontier=frontier,
    )

    recovery_calls = 0

    class Recovery:
        async def restore(self, *, work, agent_run_id, entry):
            nonlocal recovery_calls
            recovery_calls += 1
            if new_capture:
                work = replace(work, source_capture_id="capture-restored", frontier=replace(
                    work.frontier, observation=replace(
                        work.frontier.observation, capture_id="capture-restored",
                        location=entry.source_location,
                        state_version=entry.source_state_version,
                    ),
                ))
            return module.H5FrontierRecoveryWorkResult(work=work, restored=True)

    queue = _Queue(work)
    executor = _Executor(_terminal_turn(work))
    executor.execute = AsyncMock(wraps=executor.execute)
    outcome = await module.H5ExplorationTurnRunner(
        queue=queue,
        turn_executor=executor,
        continuation=_Continuation(_work(capture_id="capture-after")),
        recovery=Recovery(),
        lease_owner="h5-explorer-1",
    ).run_one(task_id=work.task_id, agent_run_id="run-h5")

    assert recovery_calls == 1
    assert len(queue.payload_updates) == 1
    if new_capture:
        assert outcome.final_status is ExplorerWorkStatus.RETRY_WAIT
        assert outcome.restored_source is True
        assert outcome.result_capture_id == "capture-restored"
        executor.execute.assert_not_awaited()
        assert queue.transitions[-1].retry_delay_seconds == 0
        assert queue.transitions[-1].reset_attempt_count is True
        assert queue.payload_updates[0].work.budget == work.budget
        assert queue.payload_updates[0].work.deadline == work.deadline
    else:
        assert outcome.final_status is ExplorerWorkStatus.SUCCEEDED
        assert queue.transitions[-1].target_status is ExplorerWorkStatus.SUCCEEDED


@pytest.mark.asyncio
async def test_h5_runner_cancels_exploration_before_claim() -> None:
    """若移除 claim 前检查，已取消任务仍会取得并执行新的 Web Work。"""

    module = _turn_module()
    work = _work()
    queue = _CancellationQueue(work)
    runner = module.H5ExplorationTurnRunner(
        queue=queue,
        turn_executor=_Executor(_terminal_turn(work)),
        continuation=_Continuation(_work(capture_id="capture-2")),
        cancellation=H5CancellationCheckpoint(_RequestedCancellation()),
        lease_owner="h5-explorer-1",
    )

    outcome = await runner.run_one(task_id="task-h5", agent_run_id="run-h5")

    assert outcome.claimed is False
    assert outcome.final_status is ExplorerWorkStatus.CANCELLED
    assert outcome.error_code == "WORKFLOW_CANCELED"
    assert queue.transitions == []
    assert queue.cancelled_work_types == {"exploration"}


@pytest.mark.asyncio
async def test_h5_runner_prefers_cancel_over_concurrent_before_capture_failure() -> None:
    """取消已生效时，动作前采集错误不得进入故障恢复或污染失败统计。"""

    module = _turn_module()
    work = _work()
    queue = _CancellationQueue(work)
    cancellation = _DeferredCancellation()
    runner = module.H5ExplorationTurnRunner(
        queue=queue,
        turn_executor=_CancelDuringCaptureExecutor(cancellation=cancellation),
        continuation=_Continuation(_work(capture_id="capture-2")),
        cancellation=H5CancellationCheckpoint(cancellation),
        lease_owner="h5-explorer-1",
    )

    outcome = await runner.run_one(task_id=work.task_id, agent_run_id="run-h5")

    assert outcome.final_status is ExplorerWorkStatus.CANCELLED
    assert outcome.error_code == "WORKFLOW_CANCELED"
    assert queue.cancelled_work_types == {"exploration"}
    assert [transition.target_status for transition in queue.transitions] == [
        ExplorerWorkStatus.RUNNING
    ]


@pytest.mark.asyncio
async def test_h5_runner_rejects_non_web_work() -> None:
    module = _turn_module()
    work = _work(surface_kind=SurfaceKind.ANDROID)
    queue = _Queue(work)
    runner = module.H5ExplorationTurnRunner(
        queue=queue,
        turn_executor=_Executor(_terminal_turn(work)),
        continuation=_Continuation(_work(capture_id="capture-2")),
        lease_owner="h5-explorer-1",
    )

    outcome = await runner.run_one(task_id="task-h5", agent_run_id="run-h5")

    assert outcome.final_status is ExplorerWorkStatus.FAILED
    assert outcome.error_code == "H5_EXPLORATION_TURN_FAILED"
    assert queue.transitions[-1].target_status is ExplorerWorkStatus.FAILED


@pytest.mark.asyncio
async def test_h5_runner_commits_action_successor_in_parent_lease() -> None:
    module = _turn_module()
    work = _work()
    successor = _work(capture_id="capture-2", parent_work_id=work.work_id)
    before = CaptureRef(
        task_id=work.task_id,
        session_id=work.session_id,
        capture_id="capture-before",
        surface_kind=SurfaceKind.WEB,
        capture_kind=CaptureKind.PAGE,
        role="before_action",
        parent_capture_id=work.source_capture_id,
        action_execution_id="action-1",
    )
    after = CaptureRef(
        task_id=work.task_id,
        session_id=work.session_id,
        capture_id="capture-2",
        surface_kind=SurfaceKind.WEB,
        capture_kind=CaptureKind.PAGE,
        role="after_action",
        parent_capture_id=before.capture_id,
        action_execution_id="action-1",
    )
    intent = _action_intent(work)
    turn = ExplorerTurnResult(
        work_id=work.work_id,
        task_id=work.task_id,
        decision=intent,
        coordination=CaptureCoordinationResult(
            before_capture=before,
            interaction=InteractionResult(
                task_id=work.task_id,
                session_id=work.session_id,
                intent_id=intent.intent_id,
                action_execution_id="action-1",
                source_capture_id=before.capture_id,
                status=InteractionResultStatus.SUCCEEDED,
                result_capture_id=after.capture_id,
            ),
            after_capture=after,
        ),
    )
    queue = _Queue(work)
    continuation = _Continuation(successor)
    runner = module.H5ExplorationTurnRunner(
        queue=queue,
        turn_executor=_Executor(turn),
        continuation=continuation,
        lease_owner="h5-explorer-1",
        emit_wakeup=False,
    )

    outcome = await runner.run_one(task_id="task-h5", agent_run_id="run-h5")

    assert outcome.final_status is ExplorerWorkStatus.SUCCEEDED, queue.transitions[-1].error_message
    assert outcome.result_capture_id == "capture-2"
    assert outcome.successor_work_id == successor.work_id
    assert len(queue.successors) == 1
    assert queue.successors[0].parent_work_id == work.work_id
    assert queue.successors[0].successor.work_id == successor.work_id
    assert queue.successor_wakeups == [False]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error_code",
    [
        "STARTUP_AD_DISMISSAL_NOT_FOUND",
        "STARTUP_AD_DISMISSAL_NOT_OBSERVED",
        "POPUP_DISMISSAL_NOT_FOUND",
        "POPUP_DISMISSAL_NOT_OBSERVED",
        "PROFILE_CREDENTIAL_CONFIRM_NOT_FOUND",
        "POPUP_MODAL_SCOPE_MISMATCH",
        "TOPMOST_OBSTRUCTION_STILL_VISIBLE",
        "CLICK_TARGET_OBSTRUCTED",
        "LOGICAL_SOURCE_STATE_STALE",
        "LOGICAL_TARGET_NOT_UNIQUE",
        "LOGICAL_TARGET_HIT_TEST_FAILED",
        "VISUAL_SOURCE_STATE_STALE",
        "VISUAL_SOURCE_PAGE_STALE",
        "VISUAL_TARGET_NOT_AT_COORDINATE",
        "SEMANTIC_TARGET_NOT_FOUND",
        "LIST_DETAIL_TARGET_NOT_FOUND",
        "VERIFIED_CARD_TARGET_STALE",
        "PROFILE_SETTINGS_TARGET_NOT_FOUND",
        "PROFILE_PAYMENT_TARGET_NOT_FOUND",
        "PROFILE_ACTIVITY_TARGET_NOT_FOUND",
        "SETTINGS_PAGE_BACK_TARGET_NOT_FOUND",
        "PROFILE_FAVORITES_PAGE_BACK_TARGET_NOT_FOUND",
        "VISUAL_SOURCE_STATE_UNAVAILABLE",
        "SURFACE_DRIVER_FAILED",
        "ACTION_DEADLINE_EXCEEDED",
        "VISUAL_TARGET_REQUIRES_STRUCTURAL_ANCHOR",
    ],
)
async def test_retryable_obstructed_action_reobserves_before_capture_as_successor(
    error_code: str,
) -> None:
    """可重观察且未产生副作用的动作失败不得重放或终止 Run。"""
    module = _turn_module()
    work = _work()
    before = CaptureRef(
        task_id=work.task_id,
        session_id=work.session_id,
        capture_id="capture-obstructed",
        surface_kind=SurfaceKind.WEB,
        capture_kind=CaptureKind.PAGE,
        role="before_action",
        parent_capture_id=work.source_capture_id,
        action_execution_id="action-startup",
    )
    interaction = InteractionResult(
        task_id=work.task_id,
        session_id=work.session_id,
        intent_id="intent-startup",
        action_execution_id="action-startup",
        source_capture_id=before.capture_id,
        status=InteractionResultStatus.BLOCKED,
        error_code=error_code,
        error_message="当前安全动作目标不可执行",
        retryable=True,
    )
    error = CaptureCoordinationError(
        stage=CaptureCoordinationStage.ACTION,
        error_code=error_code,
        message="动作执行未完成",
        before_capture=before,
        interaction=interaction,
    )
    successor = _work(capture_id=before.capture_id, parent_work_id=work.work_id)
    queue = _Queue(work)
    continuation = _Continuation(successor)
    runner = module.H5ExplorationTurnRunner(
        queue=queue,
        turn_executor=_FailingExecutor(error),
        continuation=continuation,
        lease_owner="h5-explorer-1",
        emit_wakeup=False,
    )

    outcome = await runner.run_one(task_id="task-h5", agent_run_id="run-h5")

    assert outcome.final_status is ExplorerWorkStatus.SUPERSEDED, queue.transitions[
        -1
    ].error_message
    assert outcome.result_capture_id == before.capture_id
    assert outcome.successor_work_id == successor.work_id
    assert len(continuation.calls) == 1
    assert len(queue.successors) == 1
    assert queue.successors[0].parent_target_status is ExplorerWorkStatus.SUPERSEDED
    assert queue.transitions == [queue.transitions[0]]
    assert queue.transitions[0].target_status is ExplorerWorkStatus.RUNNING


@pytest.mark.parametrize("fault", ["effect", "after_capture", "wrong_action", "not_retryable"])
def test_logical_hit_failure_reobservation_requires_pre_action_proof(fault):
    work = _work()
    before = _capture_for_turn(work, "before-hit")
    before = replace(before, role="before_action", action_execution_id="hit-action")
    interaction = InteractionResult(
        task_id=work.task_id, session_id=work.session_id, intent_id="hit-intent",
        action_execution_id="other" if fault == "wrong_action" else "hit-action",
        source_capture_id=before.capture_id,
        status=(
            InteractionResultStatus.SUCCEEDED
            if fault == "effect" else InteractionResultStatus.BLOCKED
        ),
        result_capture_id="after" if fault == "effect" else None,
        error_code=None if fault == "effect" else "LOGICAL_TARGET_HIT_TEST_FAILED",
        retryable=fault not in {"effect", "not_retryable"},
    )
    exc = CaptureCoordinationError(
        stage=CaptureCoordinationStage.ACTION, error_code="LOGICAL_TARGET_HIT_TEST_FAILED",
        message="命中校验失败", before_capture=before, interaction=interaction,
        after_capture=_capture_for_turn(work, "after") if fault == "after_capture" else None,
    )
    assert _turn_module()._is_action_reobservation(exc) is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error_code",
    [
        "SURFACE_DRIVER_FAILED",
        "ACTION_REQUEST_REJECTED",
    ],
)
async def test_nonretryable_reobservation_code_still_fails_run(
    error_code: str,
) -> None:
    """白名单错误若未自标可重试，不得走重观察，仍按失败终结。"""
    module = _turn_module()
    work = _work()
    before = CaptureRef(
        task_id=work.task_id,
        session_id=work.session_id,
        capture_id="capture-driver-failed",
        surface_kind=SurfaceKind.WEB,
        capture_kind=CaptureKind.PAGE,
        role="before_action",
        parent_capture_id=work.source_capture_id,
        action_execution_id="action-driver",
    )
    interaction = InteractionResult(
        task_id=work.task_id,
        session_id=work.session_id,
        intent_id="intent-driver",
        action_execution_id="action-driver",
        source_capture_id=before.capture_id,
        status=InteractionResultStatus.FAILED,
        error_code=error_code,
        error_message="动作目标无法安全执行",
        retryable=False,
    )
    error = CaptureCoordinationError(
        stage=CaptureCoordinationStage.ACTION,
        error_code=error_code,
        message="动作执行未完成",
        before_capture=before,
        interaction=interaction,
    )
    queue = _Queue(work)
    continuation = _Continuation(
        _work(capture_id=before.capture_id, parent_work_id=work.work_id)
    )
    runner = module.H5ExplorationTurnRunner(
        queue=queue,
        turn_executor=_FailingExecutor(error),
        continuation=continuation,
        lease_owner="h5-explorer-1",
        emit_wakeup=False,
    )

    outcome = await runner.run_one(task_id="task-h5", agent_run_id="run-h5")

    assert outcome.final_status is ExplorerWorkStatus.FAILED
    assert outcome.error_code == error_code
    assert continuation.calls == []
    assert queue.successors == []
    assert queue.transitions[-1].target_status is ExplorerWorkStatus.FAILED


def _wrapped_llm_error(status_code: int) -> Exception:
    """模拟 agent_framework ChatClientException：openai 异常在 args 里。"""

    class _FakeOpenAIStatusError(Exception):
        pass

    _FakeOpenAIStatusError.__module__ = "openai"
    upstream = _FakeOpenAIStatusError(f"Error code: {status_code}")
    upstream.status_code = status_code
    return RuntimeError("client service failed to complete the prompt", upstream)


def test_transient_llm_failure_classification() -> None:
    module = _turn_module()

    assert module._is_transient_llm_failure(_wrapped_llm_error(503)) is True
    assert module._is_transient_llm_failure(_wrapped_llm_error(429)) is True
    assert module._is_transient_llm_failure(_wrapped_llm_error(400)) is False
    assert module._is_transient_llm_failure(RuntimeError("普通失败")) is False

    class _FakeConnectionError(Exception):
        pass

    _FakeConnectionError.__name__ = "APIConnectionError"
    _FakeConnectionError.__module__ = "openai"
    chained = RuntimeError("outer")
    chained.__cause__ = _FakeConnectionError("connect reset")
    assert module._is_transient_llm_failure(chained) is True


@pytest.mark.asyncio
async def test_transient_llm_failure_parks_work_as_retry_wait() -> None:
    """LLM 网关 5xx 抖动必须停到 RETRY_WAIT 续跑，不得直接终结探索 Run。"""
    module = _turn_module()
    work = _work()
    queue = _Queue(work)
    runner = module.H5ExplorationTurnRunner(
        queue=queue,
        turn_executor=_FailingExecutor(_wrapped_llm_error(503)),
        continuation=_Continuation(_work(capture_id="capture-2")),
        lease_owner="h5-explorer-1",
        emit_wakeup=False,
    )

    outcome = await runner.run_one(task_id="task-h5", agent_run_id="run-h5")

    assert outcome.final_status is ExplorerWorkStatus.RETRY_WAIT
    failure = queue.transitions[-1]
    assert failure.target_status is ExplorerWorkStatus.RETRY_WAIT
    assert failure.retry_delay_seconds == 20


@pytest.mark.asyncio
async def test_explorer_output_error_parks_work_as_retry_wait() -> None:
    """模型选错候选等输出异常属瞬时故障，必须 RETRY_WAIT 续跑，不得终结探索 Run。"""
    module = _turn_module()
    work = _work()
    queue = _Queue(work)
    runner = module.H5ExplorationTurnRunner(
        queue=queue,
        turn_executor=_FailingExecutor(
            ExplorerAgentOutputError("Explorer 选择了不属于可执行 Frontier 的 candidate_id")
        ),
        continuation=_Continuation(_work(capture_id="capture-2")),
        lease_owner="h5-explorer-1",
        emit_wakeup=False,
    )

    outcome = await runner.run_one(task_id="task-h5", agent_run_id="run-h5")

    assert outcome.final_status is ExplorerWorkStatus.RETRY_WAIT
    assert outcome.error_code == "H5_EXPLORER_OUTPUT_INVALID"
    failure = queue.transitions[-1]
    assert failure.target_status is ExplorerWorkStatus.RETRY_WAIT
    assert failure.error_code == "H5_EXPLORER_OUTPUT_INVALID"


@pytest.mark.asyncio
async def test_model_unavailable_parks_work_as_retry_wait() -> None:
    """网关不可用（含挂死兜底超时转化）属瞬时故障，必须 RETRY_WAIT 续跑。"""
    module = _turn_module()
    work = _work()
    queue = _Queue(work)
    runner = module.H5ExplorationTurnRunner(
        queue=queue,
        turn_executor=_FailingExecutor(ExplorerModelUnavailableError("网关断连")),
        continuation=_Continuation(_work(capture_id="capture-2")),
        lease_owner="h5-explorer-1",
        emit_wakeup=False,
    )

    outcome = await runner.run_one(task_id="task-h5", agent_run_id="run-h5")

    assert outcome.final_status is ExplorerWorkStatus.RETRY_WAIT
    assert outcome.error_code == "H5_EXPLORER_MODEL_UNAVAILABLE"
    failure = queue.transitions[-1]
    assert failure.target_status is ExplorerWorkStatus.RETRY_WAIT
    assert failure.error_code == "H5_EXPLORER_MODEL_UNAVAILABLE"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error_code",
    [
        "SEMANTIC_TARGET_AMBIGUOUS",
        "LOGICAL_TARGET_NOT_UNIQUE",
        "SEEK_NATIVE_CONTROLS_UNAVAILABLE",
        "BROWSER_HISTORY_EMPTY",
        "BOTTOM_NAVIGATION_TRANSITION_NOT_OBSERVED",
        "FLUTTER_BOTTOM_NAVIGATION_NO_EFFECT",
        "COMMAND_TARGET_RISK_UNDERRATED",
        "COMMAND_TARGET_SEMANTICS_UNTRUSTED",
        "H5_POPUP_DISMISSAL_INTENT_INVALID",
    ],
)
async def test_nonretryable_safe_action_failure_terminalizes_candidate_and_continues(
    error_code: str,
) -> None:
    """无副作用的终态动作失败只终止当前候选，不得升级为完整 H5 探索失败。"""
    module = _turn_module()
    work = _work()
    before = CaptureRef(
        task_id=work.task_id,
        session_id=work.session_id,
        capture_id="capture-ambiguous",
        surface_kind=SurfaceKind.WEB,
        capture_kind=CaptureKind.PAGE,
        role="before_action",
        parent_capture_id=work.source_capture_id,
        action_execution_id="action-ambiguous",
    )
    interaction = InteractionResult(
        task_id=work.task_id,
        session_id=work.session_id,
        intent_id="intent-ambiguous",
        action_execution_id="action-ambiguous",
        source_capture_id=before.capture_id,
        status=InteractionResultStatus.BLOCKED,
        error_code=error_code,
        error_message="当前安全动作目标不可执行",
        retryable=False,
    )
    error = CaptureCoordinationError(
        stage=CaptureCoordinationStage.ACTION,
        error_code=error_code,
        message="动作执行未完成",
        before_capture=before,
        interaction=interaction,
    )
    successor = _work(capture_id=before.capture_id, parent_work_id=work.work_id)
    queue = _Queue(work)
    continuation = _Continuation(successor)
    runner = module.H5ExplorationTurnRunner(
        queue=queue,
        turn_executor=_FailingExecutor(error),
        continuation=continuation,
        lease_owner="h5-explorer-1",
        emit_wakeup=False,
    )

    outcome = await runner.run_one(task_id="task-h5", agent_run_id="run-h5")

    assert outcome.final_status is ExplorerWorkStatus.SUPERSEDED
    assert outcome.error_code == error_code
    assert outcome.successor_work_id == successor.work_id
    assert len(continuation.calls) == 1
    assert continuation.calls[0].retryable is False
    assert queue.successors[0].parent_target_status is ExplorerWorkStatus.SUPERSEDED


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "stage",
    [CaptureCoordinationStage.ACTION, CaptureCoordinationStage.AFTER_CAPTURE],
)
async def test_browser_loss_unknown_action_never_replays_and_atomically_supersedes(
    stage: CaptureCoordinationStage,
) -> None:
    module = _turn_module()
    work = _work()
    successor = _work(capture_id="capture-recovery", parent_work_id=work.work_id)
    executor = _BrowserLossExecutor(stage=stage)
    recovery = _BrowserLossRecovery(
        outcome=H5RecoveryOutcome(
            retry_original_action=False,
            inventory_status="insufficient",
            supersede_failed_work=True,
            rebaseline_steps=("capture", "detect", "assess"),
            recovery_work=successor,
            recovery_attempt_count=1,
        )
    )
    queue = _Queue(work)
    runner = module.H5ExplorationTurnRunner(
        queue=queue,
        turn_executor=executor,
        continuation=_Continuation(successor),
        browser_loss_recovery=recovery,
        lease_owner="h5-explorer-1",
        emit_wakeup=False,
    )

    outcome = await runner.run_one(task_id="task-h5", agent_run_id="run-h5")

    assert executor.calls == 1
    assert len(recovery.calls) == 1
    assert outcome.final_status is ExplorerWorkStatus.SUPERSEDED, queue.transitions[
        -1
    ].error_message
    assert outcome.successor_work_id == successor.work_id
    assert len(queue.successors) == 1
    assert queue.successors[0].parent_target_status is ExplorerWorkStatus.SUPERSEDED
    assert queue.successors[0].row_version == 5
    assert queue.payload_updates == []
    assert queue.successor_wakeups == [False]


@pytest.mark.asyncio
async def test_unknown_timeout_action_rebaselines_session_without_replay() -> None:
    """超时后页面状态未知，必须作废旧 Session 并走 unknown-action 恢复链。"""

    module = _turn_module()
    error_code = "ACTION_TIMED_OUT"
    work = _work()
    before = CaptureRef(
        task_id=work.task_id,
        session_id=work.session_id,
        capture_id="capture-before-timeout",
        surface_kind=SurfaceKind.WEB,
        capture_kind=CaptureKind.PAGE,
        role="before_action",
        parent_capture_id=work.source_capture_id,
        action_execution_id="action-timeout",
    )
    interaction = InteractionResult(
        task_id=work.task_id,
        session_id=work.session_id,
        intent_id="intent-timeout",
        action_execution_id="action-timeout",
        source_capture_id=before.capture_id,
        status=InteractionResultStatus.TIMED_OUT,
        error_code=error_code,
        error_message="动作结果未知",
        retryable=True,
    )
    error = CaptureCoordinationError(
        stage=CaptureCoordinationStage.ACTION,
        error_code=error_code,
        message="动作执行未完成",
        before_capture=before,
        interaction=interaction,
    )
    successor = _work(capture_id="capture-timeout-recovery", parent_work_id=work.work_id)
    recovery = _BrowserLossRecovery(
        outcome=H5RecoveryOutcome(
            retry_original_action=False,
            inventory_status="insufficient",
            supersede_failed_work=True,
            rebaseline_steps=("capture", "detect", "assess"),
            recovery_work=successor,
            recovery_attempt_count=1,
        )
    )
    queue = _Queue(work)
    runner = module.H5ExplorationTurnRunner(
        queue=queue,
        turn_executor=_FailingExecutor(error),
        continuation=_Continuation(successor),
        browser_loss_recovery=recovery,
        lease_owner="h5-explorer-1",
        emit_wakeup=False,
    )

    outcome = await runner.run_one(task_id="task-h5", agent_run_id="run-h5")

    assert outcome.final_status is ExplorerWorkStatus.SUPERSEDED
    assert outcome.successor_work_id == successor.work_id
    assert len(recovery.calls) == 1
    assert recovery.calls[0][2].error_code == error_code
    assert queue.successors[0].parent_target_status is ExplorerWorkStatus.SUPERSEDED


@pytest.mark.asyncio
async def test_unknown_action_is_not_replayed_when_another_worker_reconsumes_task() -> None:
    module = _turn_module()
    work = _work()
    successor = _work(capture_id="capture-recovery", parent_work_id=work.work_id)
    executor = _BrowserLossExecutor(stage=CaptureCoordinationStage.ACTION)
    recovery = _BrowserLossRecovery(
        outcome=H5RecoveryOutcome(
            retry_original_action=False,
            inventory_status="insufficient",
            supersede_failed_work=True,
            rebaseline_steps=("capture", "detect", "assess"),
            recovery_work=successor,
            recovery_attempt_count=1,
        )
    )
    queue = _ClaimOnceQueue(work)
    runner = module.H5ExplorationTurnRunner(
        queue=queue,
        turn_executor=executor,
        continuation=_Continuation(successor),
        browser_loss_recovery=recovery,
        lease_owner="h5-explorer-1",
        emit_wakeup=False,
    )

    first = await runner.run_one(task_id="task-h5", agent_run_id="run-h5")
    replay = await runner.run_one(task_id="task-h5", agent_run_id="run-h5")

    assert first.final_status is ExplorerWorkStatus.SUPERSEDED
    assert replay.claimed is False
    assert executor.calls == 1
    assert len(queue.successors) == 1


@pytest.mark.asyncio
async def test_before_capture_browser_loss_atomically_publishes_single_retry_work() -> None:
    module = _turn_module()
    work = _work()

    class FailOnceThenTerminal:
        def __init__(self) -> None:
            self.calls = 0

        async def execute(self, *, work, agent_run_id):
            del agent_run_id
            self.calls += 1
            if self.calls == 1:
                raise CaptureCoordinationError(
                    stage=CaptureCoordinationStage.BEFORE_CAPTURE,
                    error_code="browser_session_lost",
                    message="浏览器进程已丢失",
                    cause_type="TargetClosedError",
                )
            return _terminal_turn(work)

    retry_work = _work(capture_id="capture-recovery", parent_work_id=work.work_id)
    executor = FailOnceThenTerminal()
    recovery = _BrowserLossRecovery(
        outcome=H5RecoveryOutcome(
            retry_original_action=True,
            inventory_status=None,
            supersede_failed_work=True,
            rebaseline_steps=("capture", "detect", "assess"),
            recovery_work=retry_work,
            recovery_attempt_count=1,
        )
    )
    queue = _Queue(work)
    runner = module.H5ExplorationTurnRunner(
        queue=queue,
        turn_executor=executor,
        continuation=_Continuation(retry_work),
        browser_loss_recovery=recovery,
        lease_owner="h5-explorer-1",
        emit_wakeup=False,
    )

    outcome = await runner.run_one(task_id="task-h5", agent_run_id="run-h5")

    assert outcome.final_status is ExplorerWorkStatus.SUPERSEDED, queue.transitions[
        -1
    ].error_message
    assert outcome.successor_work_id == retry_work.work_id
    assert executor.calls == 1
    assert len(recovery.calls) == 1
    assert queue.payload_updates == []
    assert len(queue.successors) == 1
    assert queue.successors[0].parent_target_status is ExplorerWorkStatus.SUPERSEDED
    assert queue.successors[0].successor.session_id.startswith(f"{retry_work.session_id}:recovery:")
    assert queue.successor_wakeups == [False]


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", [None, "task", "run", "session", "work", "deadline", "hash"])
async def test_persisted_completed_recovery_replay_suppresses_successor_wakeup(fault) -> None:
    """若 completed attempt 重放分支漏传策略，会重新产生无人消费唤醒。"""

    module = _turn_module()
    work = _work()
    completed_attempt = H5BrowserRecoveryAttemptFacts.create(
        task_id=work.task_id,
        agent_run_id="run-h5",
        source_session_id=work.session_id,
        failed_work_id=work.work_id,
        source_state_version=work.frontier.observation.state_version,
        frontier_snapshot_id=work.frontier_snapshot_id,
        failure_stage=H5FailureStage.ACTION,
        recovery_attempt_count=1,
        status="completed",
        error_code="browser_session_lost",
        action_execution_id="action-unknown",
        before_capture_id=work.source_capture_id,
        after_capture_id=None,
        interaction_status="executing",
        interaction_result_capture_id=None,
        started_at=work.frontier.observation.observed_at.isoformat(),
        deadline=work.deadline.isoformat(),
        remaining_duration_ms=work.budget.remaining_duration_ms,
        action_cost=2,
        remaining_actions_after_reservation=work.budget.remaining_actions - 2,
        last_safe_location=work.frontier.observation.location,
        reason_code="h5_browser_recovery_side_effect_failed",
    )
    values = {**completed_attempt._values(), "failure_stage": H5FailureStage.ACTION}
    if fault == "task":
        values["task_id"] = "another-task"
    elif fault == "run":
        values.update(agent_run_id="another-run", source_session_id="h5:another-run")
    elif fault == "session":
        values["source_session_id"] = "h5:run-h5:recovery:" + "0" * 20
    elif fault == "work":
        values["failed_work_id"] = "another-work"
    elif fault == "deadline":
        values["deadline"] = (work.deadline + timedelta(seconds=1)).isoformat()
    completed_attempt = H5BrowserRecoveryAttemptFacts.create(**values)
    payload = completed_attempt.to_dict()
    if fault == "hash":
        payload["payload_hash"] = "forged"
    replay_work = replace(
        work,
        frontier=replace(
            work.frontier,
            observation=replace(
                work.frontier.observation,
                metadata={
                    **dict(work.frontier.observation.metadata),
                    "h5_browser_recovery_attempt": payload,
                },
            ),
        ),
    )
    started_attempt = H5BrowserRecoveryAttemptFacts.create(
        **{
            **completed_attempt._values(),
            "failure_stage": H5FailureStage.ACTION,
            "recovery_attempt_count": 2,
            "status": "started",
            "reason_code": None,
        }
    )
    reserved_work = replace(
        replay_work,
        frontier=replace(
            replay_work.frontier,
            observation=replace(
                replay_work.frontier.observation,
                metadata={
                    **dict(replay_work.frontier.observation.metadata),
                    "h5_browser_recovery_attempt": started_attempt.to_dict(),
                },
            ),
        ),
    )
    successor = _work(capture_id="capture-recovery", parent_work_id=work.work_id)

    class PersistedRecovery(_BrowserLossRecovery):
        async def reserve_attempt(self, *, work, agent_run_id, error):
            del work, agent_run_id, error
            return SimpleNamespace(work=reserved_work, reserved=True)

        async def recover(
            self,
            *,
            work,
            agent_run_id,
            error,
            attempt_reserved=False,
        ):
            del attempt_reserved
            return await super().recover(
                work=work,
                agent_run_id=agent_run_id,
                error=error,
            )

    recovery = PersistedRecovery(
        outcome=H5RecoveryOutcome(
            retry_original_action=False,
            inventory_status="insufficient",
            supersede_failed_work=True,
            rebaseline_steps=("capture", "detect", "assess"),
            recovery_work=successor,
            recovery_attempt_count=2,
        )
    )
    queue = _Queue(replay_work)
    synchronize = AsyncMock(side_effect=ValueError("旧现场已 lost，不得先同步"))
    outcome = await module.H5ExplorationTurnRunner(
        queue=queue,
        turn_executor=_Executor(_terminal_turn(replay_work)),
        continuation=_Continuation(successor),
        browser_loss_recovery=recovery,
        recovery=SimpleNamespace(synchronize=synchronize),
        lease_owner="h5-explorer-1",
        emit_wakeup=False,
    ).run_one(task_id=work.task_id, agent_run_id="run-h5")

    synchronize.assert_not_called()
    if fault:
        assert outcome.final_status is ExplorerWorkStatus.FAILED
        assert queue.successors == []
        assert recovery.calls == []
        return
    assert outcome.final_status is ExplorerWorkStatus.SUPERSEDED, queue.transitions[
        -1
    ].error_message
    assert len(queue.successors) == 1
    assert queue.successor_wakeups == [False]
    synchronize.assert_not_called()


@pytest.mark.asyncio
async def test_browser_recovery_attempt_is_persisted_before_reopen_can_fail() -> None:
    module = _turn_module()
    work = _work()
    attempt = H5BrowserRecoveryAttemptFacts.create(
        task_id=work.task_id,
        agent_run_id="run-h5",
        source_session_id=work.session_id,
        failed_work_id=work.work_id,
        source_state_version=work.frontier.observation.state_version,
        frontier_snapshot_id=work.frontier_snapshot_id,
        failure_stage=H5FailureStage.BEFORE_CAPTURE,
        recovery_attempt_count=1,
        status="started",
        error_code="browser_session_lost",
        action_execution_id=None,
        before_capture_id=None,
        after_capture_id=None,
        interaction_status=None,
        interaction_result_capture_id=None,
        started_at=work.frontier.observation.observed_at.isoformat(),
        deadline=work.deadline.isoformat(),
        remaining_duration_ms=work.budget.remaining_duration_ms,
        action_cost=1,
        remaining_actions_after_reservation=work.budget.remaining_actions - 1,
        last_safe_location=work.frontier.observation.location,
        reason_code=None,
    )
    reserved = replace(
        work,
        frontier=replace(
            work.frontier,
            observation=replace(
                work.frontier.observation,
                metadata={
                    **dict(work.frontier.observation.metadata),
                    "h5_browser_recovery_attempt": attempt.to_dict(),
                },
            ),
        ),
        budget=replace(
            work.budget,
            remaining_actions=work.budget.remaining_actions - 1,
            remaining_business_actions=(
                work.budget.resolved_remaining_business_actions - 1
            ),
        ),
    )
    events = []

    class Recovery:
        async def reserve_attempt(self, *, work, agent_run_id, error):
            del work, agent_run_id, error
            events.append("reserve")
            return SimpleNamespace(work=reserved, reserved=True)

        async def recover(self, *, work, agent_run_id, error, attempt_reserved=False):
            del work, agent_run_id, error
            assert attempt_reserved is True
            events.append("reopen")
            raise RuntimeError("browser reopen failed")

    queue = _Queue(work)
    original_update = queue.update_payload

    async def update_payload(*, command):
        events.append("persist")
        return await original_update(command=command)

    queue.update_payload = update_payload
    runner = module.H5ExplorationTurnRunner(
        queue=queue,
        turn_executor=_BrowserLossExecutor(stage=CaptureCoordinationStage.BEFORE_CAPTURE),
        continuation=_Continuation(work),
        browser_loss_recovery=Recovery(),
        lease_owner="h5-explorer-1",
    )

    outcome = await runner.run_one(task_id="task-h5", agent_run_id="run-h5")

    assert events == ["reserve", "persist", "reopen", "persist"]
    assert queue.payload_updates[0].work == reserved
    completed = H5BrowserRecoveryAttemptFacts.from_dict(
        queue.payload_updates[1].work.frontier.observation.metadata["h5_browser_recovery_attempt"]
    )
    assert completed.status == "completed"
    assert completed.reason_code == "h5_browser_recovery_side_effect_failed"
    assert outcome.final_status is ExplorerWorkStatus.RETRY_WAIT
    assert queue.transitions[-1].row_version == 7


@pytest.mark.asyncio
async def test_sibling_on_ancestor_uses_back_and_rebaseline_before_execution() -> None:
    """若跳过 rebaseline，恢复后的 sibling 可能针对陈旧页面执行。"""

    recovery = import_module("services.api.app.application.h5_exploration_recovery")
    candidate = CandidateAction(
        candidate_id="candidate-cart",
        semantic_target="购物车",
        action_type=InteractionOperation.CLICK,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=80,
        navigation_depth=1,
        expected_surface_kind=SurfaceKind.WEB,
    )
    detection = {
        "page_capture_id": "capture-rebaseline",
        "page_url": "https://m.example.test/home",
        "detection_status": "recognized",
        "page_type": "home",
        "page_state": "stable",
        "confidence": 0.9,
        "matched_catalog_signals": {"ocr_keywords": ["首页"]},
        "candidate_actions": [],
        "risk_flags": [],
        "evidence_artifact_ids": ["artifact:home"],
    }
    from services.api.app.application.h5_exploration_observation import build_h5_frontier

    source_state = build_h5_frontier(
        detection_result=detection,
        observed_at=datetime.now(UTC),
        navigation_depth=0,
    ).observation.state_version
    snapshot = H5TaskFrontierSnapshot.create(max_pages=None).merge(
        source_capture_id="capture-home",
        source_state_version=source_state,
        source_location="https://m.example.test/home",
        navigation_depth=0,
        candidates=(candidate,),
    )
    entry = snapshot.pending_entries[0]

    class Browser:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def is_history_ancestor(self, *, source_location, current_location):
            return True

        async def back(self):
            self.calls.append("back")
            return "https://m.example.test/home"

        async def navigate(self, destination_url):
            self.calls.append("navigate")
            return destination_url

        async def capture(self):
            self.calls.append("capture")
            return {"capture_id": "capture-rebaseline"}

        async def detect(self, *, capture):
            self.calls.append("detect")
            return detection

        async def assess(self, *, capture, detection):
            self.calls.append("assess")
            return {
                "execution_status": "accepted",
                "page_type_assessment": {
                    "page_type": "home",
                    "status": "covered",
                    "reason_code": "h5_page_detection_covered",
                    "page_capture_ids": ["capture-rebaseline"],
                    "evidence_artifact_ids": ["artifact:home"],
                    "page_states": ["stable"],
                },
            }

    browser = Browser()
    result = await recovery.restore_h5_frontier_source(
        entry=entry,
        current_location="https://m.example.test/product/1",
        browser=browser,
    )

    assert browser.calls == ["back", "capture", "detect", "assess"]
    assert result.restored is True
    assert result.entry_id == entry.entry_id


@pytest.mark.asyncio
async def test_second_sibling_restoration_attempt_falls_back_to_controlled_navigate() -> None:
    """首次 back 校验失败后，第二次必须用受控 navigate 恢复来源页。"""

    recovery = import_module("services.api.app.application.h5_exploration_recovery")
    candidate = CandidateAction(
        candidate_id="candidate-settings",
        semantic_target="设置",
        action_type=InteractionOperation.CLICK,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=80,
        navigation_depth=1,
        expected_surface_kind=SurfaceKind.WEB,
    )
    detection = {
        "page_capture_id": "capture-profile-rebaseline",
        "page_url": "https://m.example.test/mine",
        "detection_status": "recognized",
        "page_type": "profile",
        "page_state": "stable",
        "confidence": 0.9,
        "matched_catalog_signals": {"ocr_keywords": ["我的"]},
        "candidate_actions": [],
        "risk_flags": [],
        "evidence_artifact_ids": ["artifact:profile"],
    }
    from services.api.app.application.h5_exploration_observation import build_h5_frontier

    source_state = build_h5_frontier(
        detection_result=detection,
        observed_at=datetime.now(UTC),
        navigation_depth=0,
    ).observation.state_version
    entry = (
        H5TaskFrontierSnapshot.create(max_pages=None)
        .merge(
            source_capture_id="capture-profile",
            source_state_version=source_state,
            source_location="https://m.example.test/mine",
            navigation_depth=0,
            candidates=(candidate,),
        )
        .pending_entries[0]
    )

    class Browser:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def is_history_ancestor(self, *, source_location, current_location):
            return True

        async def back(self):
            raise AssertionError("第二次恢复不得继续 back")

        async def navigate(self, destination_url):
            self.calls.append("navigate")
            return destination_url

        async def capture(self):
            self.calls.append("capture")
            return {"capture_id": "capture-profile-rebaseline"}

        async def detect(self, *, capture):
            self.calls.append("detect")
            return detection

        async def assess(self, *, capture, detection):
            self.calls.append("assess")
            return {
                "execution_status": "accepted",
                "page_type_assessment": {
                    "page_type": "profile",
                    "status": "covered",
                    "reason_code": "h5_page_detection_covered",
                    "page_capture_ids": ["capture-profile-rebaseline"],
                    "evidence_artifact_ids": ["artifact:profile"],
                    "page_states": ["stable"],
                },
            }

    browser = Browser()
    result = await recovery.restore_h5_frontier_source(
        entry=entry,
        current_location="https://m.example.test/signin",
        browser=browser,
        attempt_count=1,
    )

    assert result.restored is True
    assert result.route == "navigate"
    assert browser.calls == ["navigate", "capture", "detect", "assess"]


@pytest.mark.asyncio
async def test_sibling_restoration_dismisses_retriggered_startup_ad_before_rebaseline() -> None:
    """受控导航重触发开屏广告时，应清障后再校验原来源页。"""

    recovery = import_module("services.api.app.application.h5_exploration_recovery")
    candidate = CandidateAction(
        candidate_id="candidate-square",
        semantic_target="广场",
        action_type=InteractionOperation.CLICK,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=90,
        navigation_depth=1,
        expected_surface_kind=SurfaceKind.WEB,
        metadata={
            "source_page_type": "home",
            "expected_next_page_type": "post_list",
        },
    )
    home_detection = {
        "page_capture_id": "capture-home-rebaseline",
        "page_url": "https://m.example.test/",
        "detection_status": "recognized",
        "page_type": "home",
        "page_state": "stable",
        "confidence": 0.9,
        "matched_catalog_signals": {"ocr_keywords": ["首页"]},
        "candidate_actions": [],
        "risk_flags": [],
        "evidence_artifact_ids": ["artifact:home"],
    }
    startup_ad_detection = {
        "page_capture_id": "capture-startup-ad",
        "page_url": "https://m.example.test/",
        "detection_status": "recognized",
        "page_type": "startup_ad",
        "page_state": "countdown",
        "confidence": 0.95,
        "matched_catalog_signals": {"ocr_keywords": ["跳过"]},
        "candidate_actions": [],
        "risk_flags": [],
        "evidence_artifact_ids": ["artifact:startup-ad"],
    }
    from services.api.app.application.h5_exploration_observation import build_h5_frontier

    source_state = build_h5_frontier(
        detection_result=home_detection,
        observed_at=datetime.now(UTC),
        navigation_depth=0,
    ).observation.state_version
    entry = (
        H5TaskFrontierSnapshot.create(max_pages=None)
        .merge(
            source_capture_id="capture-home",
            source_state_version=source_state,
            source_location="https://m.example.test/",
            navigation_depth=0,
            candidates=(candidate,),
        )
        .pending_entries[0]
    )

    class Browser:
        def __init__(self) -> None:
            self.calls: list[str] = []
            self.dismissed = False

        async def is_history_ancestor(self, *, source_location, current_location):
            return False

        async def back(self):
            raise AssertionError("本场景必须使用受控导航")

        async def navigate(self, destination_url):
            self.calls.append("navigate")
            return destination_url

        async def capture(self):
            self.calls.append("capture")
            return {"capture_id": "capture-startup-ad"}

        async def detect(self, *, capture):
            self.calls.append("detect")
            return home_detection if self.dismissed else startup_ad_detection

        async def assess(self, *, capture, detection):
            self.calls.append(f"assess:{detection['page_type']}")
            page_type = detection["page_type"]
            return {
                "execution_status": "accepted",
                "page_type_assessment": {
                    "page_type": page_type,
                    "status": "covered",
                    "reason_code": "h5_page_detection_covered",
                    "page_capture_ids": [detection["page_capture_id"]],
                    "evidence_artifact_ids": list(detection["evidence_artifact_ids"]),
                    "page_states": [detection["page_state"]],
                },
            }

        async def dismiss_startup_ad(self, *, capture, detection):
            self.calls.append("dismiss_startup_ad")
            self.dismissed = True
            return {"capture_id": "capture-home-rebaseline"}

    browser = Browser()
    result = await recovery.restore_h5_frontier_source(
        entry=entry,
        current_location="https://m.example.test/movie-player/1",
        browser=browser,
    )

    assert result.restored is True
    assert result.capture == {"capture_id": "capture-home-rebaseline"}
    assert browser.calls == [
        "navigate",
        "capture",
        "detect",
        "assess:startup_ad",
        "dismiss_startup_ad",
        "detect",
        "assess:home",
    ]


@pytest.mark.asyncio
async def test_restoration_exhaustion_returns_stable_failure_reason() -> None:
    """若恢复次数无上限，失效 sibling 会造成无限循环。"""

    recovery = import_module("services.api.app.application.h5_exploration_recovery")
    candidate = CandidateAction(
        candidate_id="candidate-cart",
        semantic_target="购物车",
        action_type=InteractionOperation.CLICK,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=80,
        navigation_depth=1,
        expected_surface_kind=SurfaceKind.WEB,
    )
    snapshot = H5TaskFrontierSnapshot.create(max_pages=None).merge(
        source_capture_id="capture-home",
        source_state_version="state:home",
        source_location="https://m.example.test/home",
        navigation_depth=0,
        candidates=(candidate,),
    )
    entry = snapshot.pending_entries[0]

    class Browser:
        async def is_history_ancestor(self, *, source_location, current_location):
            return False

        async def back(self):
            raise AssertionError("恢复次数耗尽时不能继续操作浏览器")

        async def navigate(self, destination_url):
            raise AssertionError("恢复次数耗尽时不能继续操作浏览器")

        async def capture(self):
            raise AssertionError("恢复次数耗尽时不能继续操作浏览器")

        async def detect(self, *, capture):
            raise AssertionError("恢复次数耗尽时不能继续操作浏览器")

        async def assess(self, *, capture, detection):
            raise AssertionError("恢复次数耗尽时不能继续操作浏览器")

    result = await recovery.restore_h5_frontier_source(
        entry=entry,
        current_location="https://m.example.test/product/1",
        browser=Browser(),
        attempt_count=2,
    )

    assert result.restored is False
    assert result.reason_code == "h5_frontier_restore_exhausted"


@pytest.mark.asyncio
async def test_restoration_exhaustion_marks_selected_sibling_failed_in_snapshot() -> None:
    """若耗尽恢复后仍保留 selected sibling，恢复 Work 会无限重复提交。"""

    recovery = import_module("services.api.app.application.h5_exploration_recovery")
    candidate = CandidateAction(
        candidate_id="candidate-cart",
        semantic_target="购物车",
        action_type=InteractionOperation.CLICK,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=80,
        navigation_depth=1,
        expected_surface_kind=SurfaceKind.WEB,
    )
    pending = H5TaskFrontierSnapshot.create(max_pages=None).merge(
        source_capture_id="capture-home",
        source_state_version="state:home",
        source_location="https://m.example.test/home",
        navigation_depth=0,
        candidates=(candidate,),
    )
    selected = pending.select_entry(pending.pending_entries[0].entry_id)
    attempted = selected.record_restoration_attempt(selected.entries[0].entry_id)
    exhausted = attempted.record_restoration_attempt(attempted.entries[0].entry_id)

    result = recovery.H5RestorationResult(
        entry_id=exhausted.entries[0].entry_id,
        restored=False,
        restoration_attempt_count=2,
        reason_code="h5_frontier_restore_exhausted",
    )
    failed = recovery.apply_h5_frontier_restoration_result(
        snapshot=exhausted,
        result=result,
    )

    assert failed.entries[0].status.value == "failed"
    assert failed.entries[0].last_failure_reason == "h5_frontier_restore_exhausted"


@pytest.mark.asyncio
async def test_restoration_rejects_same_url_when_rebaseline_state_differs() -> None:
    """若只比较 URL，登录页或弹窗会被误认作来源页面。"""
    from services.api.app.application.h5_exploration_observation import build_h5_frontier

    recovery = import_module("services.api.app.application.h5_exploration_recovery")
    candidate = CandidateAction(
        candidate_id="candidate-cart",
        semantic_target="购物车",
        action_type=InteractionOperation.CLICK,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=80,
        navigation_depth=1,
        expected_surface_kind=SurfaceKind.WEB,
    )
    expected_detection = {
        "page_capture_id": "capture-home",
        "page_url": "https://m.example.test/home",
        "page_type": "home",
        "page_state": "stable",
        "matched_catalog_signals": {"ocr_keywords": ["首页"]},
        "risk_flags": [],
        "candidate_actions": [],
    }
    expected_state = build_h5_frontier(
        detection_result=expected_detection,
        observed_at=datetime.now(UTC),
        navigation_depth=0,
    ).observation.state_version
    snapshot = H5TaskFrontierSnapshot.create(max_pages=None).merge(
        source_capture_id="capture-home",
        source_state_version=expected_state,
        source_location="https://m.example.test/home",
        navigation_depth=0,
        candidates=(candidate,),
    )
    entry = snapshot.pending_entries[0]

    class Browser:
        async def is_history_ancestor(self, *, source_location, current_location):
            return True

        async def back(self):
            return "https://m.example.test/home"

        async def navigate(self, destination_url):
            raise AssertionError("祖先来源应优先 back")

        async def capture(self):
            return {"capture_id": "capture-login"}

        async def detect(self, *, capture):
            return {
                **expected_detection,
                "page_capture_id": capture["capture_id"],
                "page_state": "login",
                "risk_flags": ["authentication_required"],
            }

        async def assess(self, *, capture, detection):
            return SimpleNamespace(
                execution_status="accepted",
                page_type_assessment={
                    "page_type": "home",
                    "status": "covered",
                    "reason_code": "h5_page_detection_covered",
                    "page_capture_ids": ["capture-login"],
                    "evidence_artifact_ids": ["artifact:home"],
                    "page_states": ["login"],
                },
            )

    result = await recovery.restore_h5_frontier_source(
        entry=entry,
        current_location="https://m.example.test/product/1",
        browser=Browser(),
    )

    assert result.restored is False
    assert result.reason_code == "h5_frontier_restore_state_mismatch"


@pytest.mark.asyncio
async def test_back_restoration_accepts_stable_dynamic_parent_with_signed_type() -> None:
    """若父页业务状态变化后仍要求旧版本完全相等，会误触发 navigate 并重放开屏广告。"""
    from services.api.app.application.h5_exploration_observation import build_h5_frontier

    recovery = import_module("services.api.app.application.h5_exploration_recovery")
    candidate = CandidateAction(
        candidate_id="candidate-settings",
        semantic_target="设置",
        action_type=InteractionOperation.CLICK,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=80,
        navigation_depth=1,
        expected_surface_kind=SurfaceKind.WEB,
        metadata={"source_page_type": "profile"},
    )
    source_detection = {
        "page_capture_id": "capture-profile-before-signin",
        "page_url": "https://m.example.test/mine",
        "detection_status": "recognized",
        "page_type": "profile",
        "page_state": "stable",
        "confidence": 0.9,
        "matched_catalog_signals": {"ocr_keywords": ["我的", "去签到"]},
        "candidate_actions": [],
        "risk_flags": [],
        "evidence_artifact_ids": ["artifact:profile-before-signin"],
    }
    source_state = build_h5_frontier(
        detection_result=source_detection,
        observed_at=datetime.now(UTC),
        navigation_depth=0,
    ).observation.state_version
    entry = (
        H5TaskFrontierSnapshot.create(max_pages=None)
        .merge(
            source_capture_id="capture-profile-before-signin",
            source_state_version=source_state,
            source_location="https://m.example.test/mine",
            navigation_depth=0,
            candidates=(candidate,),
        )
        .pending_entries[0]
    )

    class Browser:
        async def is_history_ancestor(self, *, source_location, current_location):
            return True

        async def back(self):
            return "https://m.example.test/mine"

        async def navigate(self, destination_url):
            raise AssertionError("动态父页只能由首次 back 等价恢复，不能受控 navigate")

        async def capture(self):
            return {"capture_id": "capture-profile-after-signin"}

        async def detect(self, *, capture):
            return {
                **source_detection,
                "page_capture_id": capture["capture_id"],
                "matched_catalog_signals": {"ocr_keywords": ["我的", "已签到"]},
                "evidence_artifact_ids": ["artifact:profile-after-signin"],
            }

        async def assess(self, *, capture, detection):
            return SimpleNamespace(
                execution_status="accepted",
                page_type_assessment={
                    "page_type": "profile",
                    "status": "covered",
                    "reason_code": "h5_page_detection_covered",
                    "page_capture_ids": [capture["capture_id"]],
                    "evidence_artifact_ids": ["artifact:profile-after-signin"],
                    "page_states": ["stable"],
                },
            )

    result = await recovery.restore_h5_frontier_source(
        entry=entry,
        current_location="https://m.example.test/signin",
        browser=Browser(),
    )

    assert result.restored is True
    assert result.route == "back"


@pytest.mark.asyncio
async def test_navigate_restoration_accepts_stable_dynamic_source_with_signed_type() -> None:
    """精确导航回来源 URL 后，动态内容变化不应让已清障的稳定同页型恢复失败。"""
    from services.api.app.application.h5_exploration_observation import build_h5_frontier

    recovery = import_module("services.api.app.application.h5_exploration_recovery")
    candidate = CandidateAction(
        candidate_id="candidate-save-qr",
        semantic_target="保存二维码",
        action_type=InteractionOperation.CLICK,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=60,
        navigation_depth=1,
        expected_surface_kind=SurfaceKind.WEB,
        metadata={"source_page_type": "home"},
    )
    source_detection = {
        "page_capture_id": "capture-home-before-refresh",
        "page_url": "https://m.example.test/",
        "detection_status": "recognized",
        "page_type": "home",
        "page_state": "stable",
        "confidence": 0.95,
        "matched_catalog_signals": {"ocr_keywords": ["首页", "旧推荐"]},
        "candidate_actions": [],
        "risk_flags": [],
        "evidence_artifact_ids": ["artifact:home-before-refresh"],
    }
    source_state = build_h5_frontier(
        detection_result=source_detection,
        observed_at=datetime.now(UTC),
        navigation_depth=0,
    ).observation.state_version
    entry = (
        H5TaskFrontierSnapshot.create(max_pages=None)
        .merge(
            source_capture_id="capture-home-before-refresh",
            source_state_version=source_state,
            source_location="https://m.example.test/",
            navigation_depth=0,
            candidates=(candidate,),
        )
        .pending_entries[0]
    )

    class Browser:
        async def is_history_ancestor(self, *, source_location, current_location):
            return False

        async def back(self):
            raise AssertionError("非历史祖先应使用精确导航")

        async def navigate(self, destination_url):
            assert destination_url == "https://m.example.test/"
            return destination_url

        async def capture(self):
            return {"capture_id": "capture-home-after-refresh"}

        async def detect(self, *, capture):
            return {
                **source_detection,
                "page_capture_id": capture["capture_id"],
                "matched_catalog_signals": {"ocr_keywords": ["首页", "新推荐"]},
                "evidence_artifact_ids": ["artifact:home-after-refresh"],
            }

        async def assess(self, *, capture, detection):
            return SimpleNamespace(
                execution_status="accepted",
                page_type_assessment={
                    "page_type": "home",
                    "status": "covered",
                    "reason_code": "h5_page_detection_covered",
                    "page_capture_ids": [capture["capture_id"]],
                    "evidence_artifact_ids": ["artifact:home-after-refresh"],
                    "page_states": ["stable"],
                },
            )

    result = await recovery.restore_h5_frontier_source(
        entry=entry,
        current_location="https://m.example.test/movie",
        browser=Browser(),
    )

    assert result.restored is True
    assert result.route == "navigate"


@pytest.mark.asyncio
async def test_global_navigation_restoration_accepts_fresh_intermediate_history_parent() -> None:
    """未知子页只能 back 到重新证明同一底栏入口的同源中间父页。"""
    from services.api.app.application.h5_exploration_observation import build_h5_frontier

    recovery = import_module("services.api.app.application.h5_exploration_recovery")
    raw_candidate = {
        "action": "click",
        "target": (
            "div.van-tabbar:visible > "
            "div.van-tabbar-item:nth-of-type(6):visible"
        ),
        "risk_level": "low",
        "navigation_depth": 1,
        "expected_next_page_type": "profile",
        "target_keywords": ["bottom_navigation", "我的", "profile"],
        "navigation_knowledge_source": "static",
        "navigation_knowledge_confidence": 0.95,
    }
    source_detection = {
        "page_capture_id": "capture-home",
        "page_url": "https://m.example.test/",
        "detection_status": "recognized",
        "page_type": "home",
        "page_state": "stable",
        "confidence": 0.95,
        "matched_catalog_signals": {"ocr_keywords": ["首页"]},
        "candidate_actions": [raw_candidate],
        "risk_flags": [],
        "evidence_artifact_ids": ["artifact:home"],
    }
    source_frontier = build_h5_frontier(
        detection_result=source_detection,
        observed_at=datetime.now(UTC),
        navigation_depth=0,
    )
    entry = (
        H5TaskFrontierSnapshot.create(max_pages=None)
        .merge(
            source_capture_id="capture-home",
            source_state_version=source_frontier.observation.state_version,
            source_location="https://m.example.test/",
            navigation_depth=0,
            candidates=(source_frontier.candidates[0],),
        )
        .pending_entries[0]
    )
    parent_detection = {
        **source_detection,
        "page_capture_id": "capture-ai-parent",
        "page_url": "https://m.example.test/ai",
        "page_type": "content_list",
        "matched_catalog_signals": {"ocr_keywords": ["AI换脸"]},
        "evidence_artifact_ids": ["artifact:ai-parent"],
    }

    class Browser:
        async def is_history_ancestor(self, *, source_location, current_location):
            return True

        async def back(self):
            return "https://m.example.test/ai"

        async def navigate(self, destination_url):
            raise AssertionError("可信中间历史父页必须优先 back")

        async def capture(self):
            return {"capture_id": "capture-ai-parent"}

        async def detect(self, *, capture):
            return parent_detection

        async def assess(self, *, capture, detection):
            return {
                "execution_status": "accepted",
                "page_type_assessment": {
                    "page_type": "content_list",
                    "status": "covered",
                    "reason_code": "h5_page_detection_covered",
                    "page_capture_ids": [capture["capture_id"]],
                    "evidence_artifact_ids": ["artifact:ai-parent"],
                    "page_states": ["stable"],
                },
            }

    result = await recovery.restore_h5_frontier_source(
        entry=entry,
        current_location="https://m.example.test/aiRecord",
        browser=Browser(),
    )

    assert result.restored is True
    assert result.route == "back"


@pytest.mark.asyncio
async def test_global_navigation_restoration_rejects_unproved_intermediate_parent() -> None:
    """中间父页未重新签发目标底栏索引时不得替代原来源状态。"""
    from services.api.app.application.h5_exploration_observation import build_h5_frontier

    recovery = import_module("services.api.app.application.h5_exploration_recovery")
    candidate = CandidateAction(
        candidate_id="candidate-profile",
        semantic_target=(
            "div.van-tabbar:visible > "
            "div.van-tabbar-item:nth-of-type(6):visible"
        ),
        action_type=InteractionOperation.CLICK,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=95,
        navigation_depth=1,
        expected_surface_kind=SurfaceKind.WEB,
        metadata={
            "target_keywords": ["bottom_navigation", "我的", "profile"],
            "expected_next_page_type": "profile",
            "navigation_knowledge_source": "static",
            "navigation_knowledge_confidence": 0.95,
            "action_parameters": {
                "target_mode": "bottom_navigation",
                "navigation_index": 6,
                "navigation_label": "我的",
            },
        },
    )
    entry = (
        H5TaskFrontierSnapshot.create(max_pages=None)
        .merge(
            source_capture_id="capture-home",
            source_state_version="state:home",
            source_location="https://m.example.test/",
            navigation_depth=0,
            candidates=(candidate,),
        )
        .pending_entries[0]
    )
    parent_detection = {
        "page_capture_id": "capture-ai-parent",
        "page_url": "https://m.example.test/ai",
        "detection_status": "recognized",
        "page_type": "content_list",
        "page_state": "stable",
        "confidence": 0.95,
        "matched_catalog_signals": {"ocr_keywords": ["AI换脸"]},
        "candidate_actions": [],
        "risk_flags": [],
        "evidence_artifact_ids": ["artifact:ai-parent"],
    }
    assert build_h5_frontier(
        detection_result=parent_detection,
        observed_at=datetime.now(UTC),
        navigation_depth=0,
    ).observation.metadata["global_bottom_navigation_indices"] == ()

    class Browser:
        async def is_history_ancestor(self, *, source_location, current_location):
            return True

        async def back(self):
            return "https://m.example.test/ai"

        async def navigate(self, destination_url):
            raise AssertionError("首次应使用 back")

        async def capture(self):
            return {"capture_id": "capture-ai-parent"}

        async def detect(self, *, capture):
            return parent_detection

        async def assess(self, *, capture, detection):
            return {
                "execution_status": "accepted",
                "page_type_assessment": {
                    "page_type": "content_list",
                    "status": "covered",
                    "reason_code": "h5_page_detection_covered",
                    "page_capture_ids": [capture["capture_id"]],
                    "evidence_artifact_ids": ["artifact:ai-parent"],
                    "page_states": ["stable"],
                },
            }

    result = await recovery.restore_h5_frontier_source(
        entry=entry,
        current_location="https://m.example.test/aiRecord",
        browser=Browser(),
    )

    assert result.restored is False
    assert result.reason_code == "h5_frontier_restore_state_mismatch"


@pytest.mark.asyncio
async def test_restoration_rejects_blocked_assessment_before_exposing_sibling() -> None:
    """若 assessment blocked 仍放行，认证墙会在恢复后触发真实业务动作。"""
    recovery = import_module("services.api.app.application.h5_exploration_recovery")
    candidate = CandidateAction(
        candidate_id="candidate-cart",
        semantic_target="购物车",
        action_type=InteractionOperation.CLICK,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=80,
        navigation_depth=1,
        expected_surface_kind=SurfaceKind.WEB,
    )
    snapshot = H5TaskFrontierSnapshot.create(max_pages=None).merge(
        source_capture_id="capture-home",
        source_state_version="state:home",
        source_location="https://m.example.test/home",
        navigation_depth=0,
        candidates=(candidate,),
    )

    class Browser:
        async def is_history_ancestor(self, *, source_location, current_location):
            return False

        async def back(self):
            raise AssertionError("非祖先来源不应 back")

        async def navigate(self, destination_url):
            return destination_url

        async def capture(self):
            return {"capture_id": "capture-home"}

        async def detect(self, *, capture):
            return {"page_capture_id": capture["capture_id"]}

        async def assess(self, *, capture, detection):
            return SimpleNamespace(execution_status="blocked")

    result = await recovery.restore_h5_frontier_source(
        entry=snapshot.pending_entries[0],
        current_location="https://m.example.test/product/1",
        browser=Browser(),
    )

    assert result.restored is False
    assert result.reason_code == "h5_frontier_restore_assessment_rejected"


@pytest.mark.asyncio
async def test_restoration_rejects_accepted_assessment_with_auth_page_status() -> None:
    """accepted 只表示适配完成，认证页仍不能作为 sibling 的可执行来源。"""
    recovery = import_module("services.api.app.application.h5_exploration_recovery")
    candidate = CandidateAction(
        candidate_id="candidate-cart",
        semantic_target="购物车",
        action_type=InteractionOperation.CLICK,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=80,
        navigation_depth=1,
        expected_surface_kind=SurfaceKind.WEB,
    )
    snapshot = H5TaskFrontierSnapshot.create(max_pages=None).merge(
        source_capture_id="capture-home",
        source_state_version="state:home",
        source_location="https://m.example.test/home",
        navigation_depth=0,
        candidates=(candidate,),
    )

    class Browser:
        async def is_history_ancestor(self, *, source_location, current_location):
            return False

        async def back(self):
            raise AssertionError("非祖先来源不应 back")

        async def navigate(self, destination_url):
            return destination_url

        async def capture(self):
            return {"capture_id": "capture-home"}

        async def detect(self, *, capture):
            return {"page_capture_id": capture["capture_id"]}

        async def assess(self, *, capture, detection):
            return SimpleNamespace(
                execution_status="accepted",
                page_type_assessment={"status": "requires_auth"},
            )

    result = await recovery.restore_h5_frontier_source(
        entry=snapshot.pending_entries[0],
        current_location="https://m.example.test/product/1",
        browser=Browser(),
    )

    assert result.restored is False
    assert result.reason_code == "h5_frontier_restore_assessment_rejected"


@pytest.mark.asyncio
async def test_restoration_rejects_accepted_assessment_without_payload() -> None:
    """适配结果缺少可审计页面状态时不得把来源页交给 sibling。"""
    recovery = import_module("services.api.app.application.h5_exploration_recovery")
    candidate = CandidateAction(
        candidate_id="candidate-cart",
        semantic_target="购物车",
        action_type=InteractionOperation.CLICK,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=80,
        navigation_depth=1,
        expected_surface_kind=SurfaceKind.WEB,
    )
    snapshot = H5TaskFrontierSnapshot.create(max_pages=None).merge(
        source_capture_id="capture-home",
        source_state_version="state:home",
        source_location="https://m.example.test/home",
        navigation_depth=0,
        candidates=(candidate,),
    )

    class Browser:
        async def is_history_ancestor(self, *, source_location, current_location):
            return False

        async def navigate(self, destination_url):
            return destination_url

        async def capture(self):
            return {"capture_id": "capture-home"}

        async def detect(self, *, capture):
            return {"page_capture_id": capture["capture_id"]}

        async def assess(self, *, capture, detection):
            return SimpleNamespace(execution_status="accepted")

    result = await recovery.restore_h5_frontier_source(
        entry=snapshot.pending_entries[0],
        current_location="https://m.example.test/product/1",
        browser=Browser(),
    )

    assert result.restored is False
    assert result.reason_code == "h5_frontier_restore_assessment_rejected"


@pytest.mark.asyncio
async def test_restoration_rejects_accepted_covered_payload_without_audit_references() -> None:
    """伪造的 covered 状态没有页面、证据和状态引用时不得放行。"""
    recovery = import_module("services.api.app.application.h5_exploration_recovery")
    candidate = CandidateAction(
        candidate_id="candidate-cart",
        semantic_target="购物车",
        action_type=InteractionOperation.CLICK,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=80,
        navigation_depth=1,
        expected_surface_kind=SurfaceKind.WEB,
    )
    snapshot = H5TaskFrontierSnapshot.create(max_pages=None).merge(
        source_capture_id="capture-home",
        source_state_version="state:home",
        source_location="https://m.example.test/home",
        navigation_depth=0,
        candidates=(candidate,),
    )

    class Browser:
        async def is_history_ancestor(self, *, source_location, current_location):
            return False

        async def navigate(self, destination_url):
            return destination_url

        async def capture(self):
            return {"capture_id": "capture-home"}

        async def detect(self, *, capture):
            return {"page_capture_id": capture["capture_id"]}

        async def assess(self, *, capture, detection):
            return SimpleNamespace(
                execution_status="accepted",
                page_type_assessment={"status": "covered"},
            )

    result = await recovery.restore_h5_frontier_source(
        entry=snapshot.pending_entries[0],
        current_location="https://m.example.test/product/1",
        browser=Browser(),
    )

    assert result.restored is False
    assert result.reason_code == "h5_frontier_restore_assessment_rejected"


@pytest.mark.asyncio
async def test_restoration_rejects_covered_popup_rebaseline() -> None:
    """即使评估为 covered，弹窗 overlay 也不是可执行 sibling 的稳定来源。"""
    from services.api.app.application.h5_exploration_observation import build_h5_frontier

    recovery = import_module("services.api.app.application.h5_exploration_recovery")
    candidate = CandidateAction(
        candidate_id="candidate-cart",
        semantic_target="购物车",
        action_type=InteractionOperation.CLICK,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=80,
        navigation_depth=1,
        expected_surface_kind=SurfaceKind.WEB,
    )
    popup_detection = {
        "page_capture_id": "capture-popup",
        "page_url": "https://m.example.test/home",
        "detection_status": "recognized",
        "page_type": "home",
        "page_state": "popup",
        "confidence": 0.9,
        "matched_catalog_signals": {"ocr_keywords": ["首页"]},
        "candidate_actions": [],
        "risk_flags": [],
        "evidence_artifact_ids": ["artifact:popup"],
    }
    source_state = build_h5_frontier(
        detection_result=popup_detection,
        observed_at=datetime.now(UTC),
        navigation_depth=0,
    ).observation.state_version
    snapshot = H5TaskFrontierSnapshot.create(max_pages=None).merge(
        source_capture_id="capture-home",
        source_state_version=source_state,
        source_location="https://m.example.test/home",
        navigation_depth=0,
        candidates=(candidate,),
    )

    class Browser:
        async def is_history_ancestor(self, *, source_location, current_location):
            return False

        async def navigate(self, destination_url):
            return destination_url

        async def capture(self):
            return {"capture_id": "capture-popup"}

        async def detect(self, *, capture):
            return popup_detection

        async def assess(self, *, capture, detection):
            return SimpleNamespace(
                execution_status="accepted",
                page_type_assessment={
                    "page_type": "home",
                    "status": "covered",
                    "reason_code": "h5_page_detection_covered",
                    "page_capture_ids": ["capture-popup"],
                    "evidence_artifact_ids": ["artifact:popup"],
                    "page_states": ["popup"],
                },
            )

    result = await recovery.restore_h5_frontier_source(
        entry=snapshot.pending_entries[0],
        current_location="https://m.example.test/product/1",
        browser=Browser(),
    )

    assert result.restored is False
    assert result.reason_code == "h5_frontier_restore_rebaseline_blocked"


@pytest.mark.asyncio
async def test_h5_runner_restores_selected_sibling_before_explorer_action() -> None:
    """若移除恢复端口，Explorer 会在子页直接执行祖先 sibling。"""
    from services.api.app.application.h5_exploration_workflow import _with_task_frontier

    module = _turn_module()
    work = _work()
    sibling = CandidateAction(
        candidate_id="candidate-cart",
        semantic_target="购物车",
        action_type=InteractionOperation.CLICK,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=80,
        navigation_depth=1,
        expected_surface_kind=SurfaceKind.WEB,
    )
    snapshot = (
        H5TaskFrontierSnapshot.create(max_pages=None)
        .merge(
            source_capture_id="capture-home",
            source_state_version="state:home",
            source_location="https://m.example.test/home",
            navigation_depth=0,
            candidates=(sibling,),
        )
        .select_entry(
            H5TaskFrontierSnapshot.create(max_pages=None)
            .merge(
                source_capture_id="capture-home",
                source_state_version="state:home",
                source_location="https://m.example.test/home",
                navigation_depth=0,
                candidates=(sibling,),
            )
            .pending_entries[0]
            .entry_id
        )
    )
    observation = replace(
        work.frontier.observation,
        state_version="state:product",
        location="https://m.example.test/product/1",
    )
    work = replace(work, frontier=replace(work.frontier, observation=observation))
    restored_frontier = _with_task_frontier(frontier=work.frontier, snapshot=snapshot)
    work = replace(
        work,
        frontier_snapshot_id=restored_frontier.snapshot_id,
        frontier=restored_frontier,
    )
    before = _capture_for_turn(work, "capture-before")
    after = _capture_for_turn(work, "capture-after")
    intent = _action_intent(work)
    intent = replace(intent, parameters={"candidate_id": sibling.candidate_id})
    turn = ExplorerTurnResult(
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
    calls: list[str] = []

    class Recovery:
        async def restore(self, *, work, agent_run_id, entry):
            calls.append("restore")
            updated = snapshot.record_restoration_attempt(entry.entry_id)
            return module.H5FrontierRecoveryWorkResult(
                work=replace(
                    work,
                    source_capture_id="capture-rebaseline",
                    frontier=replace(
                        work.frontier,
                        observation=replace(
                            work.frontier.observation,
                            capture_id="capture-rebaseline",
                            state_version="state:home",
                            location="https://m.example.test/home",
                            metadata={
                                **work.frontier.observation.metadata,
                                "h5_task_frontier": updated.to_dict(),
                            },
                        ),
                    ),
                ),
                restored=True,
            )

    class Executor(_Executor):
        async def execute(self, *, work, agent_run_id):
            calls.append("execute")
            assert work.source_capture_id == "capture-rebaseline"
            assert len(queue.payload_updates) == 1
            return turn

    queue = _Queue(work)
    continuation = _Continuation(_work(capture_id="capture-after", parent_work_id=work.work_id))
    runner = module.H5ExplorationTurnRunner(
        queue=queue,
        turn_executor=Executor(turn),
        continuation=continuation,
        recovery=Recovery(),
        lease_owner="h5-explorer-1",
    )

    restored = await runner.run_one(task_id=work.task_id, agent_run_id="run-h5")
    assert calls == ["restore"]
    assert restored.restored_source is True
    assert restored.final_status is ExplorerWorkStatus.RETRY_WAIT
    calls.append("checkpoint")
    queue.work = queue.payload_updates[-1].work
    await runner.run_one(task_id=work.task_id, agent_run_id="run-h5")
    assert calls == ["restore", "checkpoint", "execute"]
    assert len(queue.payload_updates) == 1
    assert queue.payload_updates[0].work.source_capture_id == "capture-rebaseline"
    assert queue.payload_updates[0].row_version == 5
    assert queue.successors[0].row_version == 5
    assert continuation.calls[0].prior_work.source_capture_id == "capture-rebaseline"


@pytest.mark.asyncio
async def test_h5_runner_persists_successful_recovery_before_terminal_transition() -> None:
    """若 terminal 前不写回 rebaseline Work，恢复审计与新来源 Capture 都会丢失。"""
    from services.api.app.application.h5_exploration_workflow import _with_task_frontier

    module = _turn_module()
    work = _work()
    candidate = work.frontier.candidates[0]
    snapshot = H5TaskFrontierSnapshot.create(max_pages=None).merge(
        source_capture_id="capture-home",
        source_state_version="state:home",
        source_location="https://m.example.test/home",
        navigation_depth=0,
        candidates=(candidate,),
    )
    snapshot = snapshot.select_entry(snapshot.pending_entries[0].entry_id)
    observation = replace(
        work.frontier.observation,
        state_version="state:child",
        location="https://m.example.test/product/1",
    )
    frontier = _with_task_frontier(
        frontier=replace(work.frontier, observation=observation),
        snapshot=snapshot,
    )
    work = replace(work, frontier_snapshot_id=frontier.snapshot_id, frontier=frontier)
    updated_snapshot = snapshot.record_restoration_attempt(snapshot.entries[0].entry_id)
    restored_observation = replace(
        work.frontier.observation,
        capture_id="capture-rebaseline",
        state_version="state:home",
        location="https://m.example.test/home",
        metadata={
            **work.frontier.observation.metadata,
            "h5_task_frontier": updated_snapshot.to_dict(),
        },
    )
    restored_frontier = replace(
        work.frontier,
        snapshot_id="frontier:capture-rebaseline",
        observation=restored_observation,
    )
    restored_work = replace(
        work,
        source_capture_id="capture-rebaseline",
        frontier_snapshot_id=restored_frontier.snapshot_id,
        frontier=restored_frontier,
    )

    class Recovery:
        async def restore(self, *, work, agent_run_id, entry):
            return module.H5FrontierRecoveryWorkResult(work=restored_work, restored=True)

    queue = _Queue(work)
    outcome = await module.H5ExplorationTurnRunner(
        queue=queue,
        turn_executor=_Executor(_terminal_turn(work)),
        continuation=_Continuation(_work(capture_id="capture-after")),
        recovery=Recovery(),
        lease_owner="h5-explorer-1",
    ).run_one(task_id=work.task_id, agent_run_id="run-h5")

    assert outcome.final_status is ExplorerWorkStatus.RETRY_WAIT
    assert outcome.restored_source is True
    assert len(queue.payload_updates) == 1
    assert queue.payload_updates[0].work.source_capture_id == "capture-rebaseline"
    assert queue.transitions[-1].row_version == 6


@pytest.mark.asyncio
@pytest.mark.parametrize(
    (
        "page_type",
        "page_state",
        "detection_status",
        "location",
        "action_reobservation_metadata",
        "requires_recovery",
    ),
    (
        (
            "home",
            "stable",
            "recognized",
            "https://m.example.test/product",
            {"global_bottom_navigation_indices": [4]},
            False,
        ),
        (
            "content_list",
            "empty",
            "recognized",
            "https://m.example.test/product",
            {"global_bottom_navigation_indices": [4]},
            False,
        ),
        (
            "video_list",
            "loading",
            "recognized",
            "https://m.example.test/movie",
            {"global_bottom_navigation_indices": [4]},
            False,
        ),
        (
            None,
            "stable",
            "insufficient",
            "https://m.example.test/ai",
            {
                "action_reobserve_error_code": "SEMANTIC_TARGET_AMBIGUOUS",
                "action_reobservation_retryable": False,
                "action_reobservation_candidate_identity": {
                    "action_type": "click",
                    "semantic_target": "AI视频换脸",
                    "expected_next_page_type": "content_list",
                },
            },
            False,
        ),
        (
            "video_detail",
            "stable",
            "recognized",
            "https://m.example.test/moviePlayer?id=1",
            {},
            True,
        ),
        (None, "stable", "insufficient", "https://m.example.test/ai", {}, True),
        (
            None,
            "stable",
            "insufficient",
            "https://other.example.test/ai",
            {
                "action_reobserve_error_code": "SEMANTIC_TARGET_AMBIGUOUS",
                "action_reobservation_retryable": False,
                "action_reobservation_candidate_identity": {
                    "action_type": "click",
                    "semantic_target": "AI视频换脸",
                    "expected_next_page_type": "content_list",
                },
            },
            True,
        ),
    ),
)
async def test_h5_runner_does_not_restore_global_bottom_navigation_source(
    page_type: str | None,
    page_state: str,
    detection_status: str,
    location: str,
    action_reobservation_metadata: dict[str, object],
    requires_recovery: bool,
) -> None:
    """底栏是当前业务页的全局能力，不得导航旧根地址并重新触发开屏广告。"""
    from services.api.app.application.h5_exploration_workflow import _with_task_frontier

    module = _turn_module()
    work = _work()
    candidate = CandidateAction(
        candidate_id="candidate-square",
        semantic_target=("div.van-tabbar:visible > div.van-tabbar-item:nth-of-type(4):visible"),
        action_type=InteractionOperation.CLICK,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=93,
        navigation_depth=1,
        expected_surface_kind=SurfaceKind.WEB,
        metadata={
            "action_parameters": {
                "target_mode": "bottom_navigation",
                "navigation_index": 4,
                "navigation_label": "广场",
            },
            "expected_next_page_type": "post_list",
            "target_keywords": ["bottom_navigation", "广场", "post_list"],
            "navigation_knowledge_source": "static",
            "navigation_knowledge_confidence": 0.95,
        },
    )
    snapshot = H5TaskFrontierSnapshot.create(max_pages=None).merge(
        source_capture_id="capture-home-root",
        source_state_version="state:home-root",
        source_location="https://m.example.test/",
        navigation_depth=0,
        candidates=(candidate,),
    )
    snapshot = snapshot.select_entry(snapshot.pending_entries[0].entry_id)
    current_observation = replace(
        work.frontier.observation,
        capture_id="capture-home-product",
        state_version="state:home-product",
        location=location,
        page_type=page_type,
        page_state=page_state,
        metadata={
            **work.frontier.observation.metadata,
            "detection_status": detection_status,
            **action_reobservation_metadata,
        },
    )
    frontier = _with_task_frontier(
        frontier=replace(work.frontier, observation=current_observation),
        snapshot=snapshot,
    )
    work = replace(
        work,
        source_capture_id=current_observation.capture_id,
        frontier_snapshot_id=frontier.snapshot_id,
        frontier=frontier,
    )
    selected_for_recovery = module._selected_entry_requiring_recovery(work)
    if requires_recovery:
        assert selected_for_recovery is not None
        return
    assert selected_for_recovery is None
    recovery_calls = 0

    class Recovery:
        async def restore(self, *, work, agent_run_id, entry):
            nonlocal recovery_calls
            recovery_calls += 1
            return module.H5FrontierRecoveryWorkResult(
                work=work,
                restored=False,
                reason_code="h5_frontier_restore_state_mismatch",
            )

    queue = _Queue(work)
    outcome = await module.H5ExplorationTurnRunner(
        queue=queue,
        turn_executor=_Executor(_terminal_turn(work)),
        continuation=_Continuation(_work(capture_id="capture-after")),
        recovery=Recovery(),
        lease_owner="h5-explorer-1",
    ).run_one(task_id=work.task_id, agent_run_id="run-h5")

    assert recovery_calls == 0
    assert outcome.final_status is ExplorerWorkStatus.SUCCEEDED
    assert len(queue.payload_updates) == 1
    assert queue.payload_updates[0].work.source_capture_id == current_observation.capture_id


@pytest.mark.asyncio
async def test_h5_runner_rebinds_logical_bottom_navigation_to_current_capture() -> None:
    """当前截图已证明同一底栏位置时，应刷新逻辑来源而不是执行过期候选。"""
    from services.api.app.application.h5_exploration_workflow import _with_task_frontier

    module = _turn_module()
    work = _work()
    old_identity = f"sha256:{'1' * 64}"
    old_state = f"sha256:{'2' * 64}"
    current_identity = f"sha256:{'3' * 64}"
    current_state = f"sha256:{'4' * 64}"
    candidate = CandidateAction(
        candidate_id="candidate-logical-square",
        semantic_target=("div.van-tabbar:visible > div.van-tabbar-item:nth-of-type(4):visible"),
        action_type=InteractionOperation.CLICK,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=93,
        navigation_depth=1,
        expected_surface_kind=SurfaceKind.WEB,
        metadata={
            "action_parameters": {
                "target_mode": "bottom_navigation",
                "navigation_index": 4,
                "navigation_label": "广场",
            },
            "expected_next_page_type": "post_list",
            "source_page_type": "home",
            "source_page_state": "stable",
            "source_identity_fingerprint": old_identity,
            "source_state_fingerprint": old_state,
            "logical_target_required": True,
            "target_keywords": ["bottom_navigation", "广场", "post_list"],
            "navigation_knowledge_source": "static",
            "navigation_knowledge_confidence": 0.95,
        },
    )
    snapshot = H5TaskFrontierSnapshot.create(max_pages=None).merge(
        source_capture_id="capture-home-root",
        source_state_version="state:home-root",
        source_location="https://m.example.test/",
        navigation_depth=0,
        candidates=(candidate,),
    )
    snapshot = snapshot.select_entry(snapshot.pending_entries[0].entry_id)
    current_observation = replace(
        work.frontier.observation,
        capture_id="capture-current",
        state_version="state:current",
        location="https://m.example.test/product",
        page_type="home",
        page_state="stable",
        overlay_signature=None,
        metadata={
            **dict(work.frontier.observation.metadata),
            "detection_status": "recognized",
            "requires_auth": False,
            "global_bottom_navigation_indices": [4],
            "logical_targets_effective": True,
            "logical_surface_identity_fingerprint": current_identity,
            "logical_surface_state_fingerprint": current_state,
        },
    )
    frontier = _with_task_frontier(
        frontier=replace(work.frontier, observation=current_observation),
        snapshot=snapshot,
    )
    work = replace(
        work,
        source_capture_id=current_observation.capture_id,
        frontier_snapshot_id=frontier.snapshot_id,
        frontier=frontier,
    )
    executed_work = None

    class Executor:
        async def execute(self, *, work, agent_run_id):
            nonlocal executed_work
            executed_work = work
            return _terminal_turn(work)

    queue = _Queue(work)
    outcome = await module.H5ExplorationTurnRunner(
        queue=queue,
        turn_executor=Executor(),
        continuation=_Continuation(_work(capture_id="capture-after")),
        lease_owner="h5-explorer-1",
    ).run_one(task_id=work.task_id, agent_run_id="run-h5")

    assert outcome.final_status is ExplorerWorkStatus.SUCCEEDED
    assert executed_work is not None
    rebound = executed_work.frontier.candidates[0]
    assert rebound.metadata["source_identity_fingerprint"] == current_identity
    assert rebound.metadata["source_state_fingerprint"] == current_state
    assert executed_work.source_capture_id == "capture-current"
    assert len(queue.payload_updates) == 1


def test_h5_runner_reuses_low_risk_root_sibling_on_same_stable_page() -> None:
    """同 URL 首页局部切换后，可信根级 sibling 应在当前 Capture 重新解析。"""
    from services.api.app.application.h5_exploration_workflow import _with_task_frontier

    module = _turn_module()
    work = _work()
    candidate = CandidateAction(
        candidate_id="candidate-profile-root-sibling",
        semantic_target="我的",
        action_type=InteractionOperation.CLICK,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=96,
        navigation_depth=0,
        expected_surface_kind=SurfaceKind.WEB,
        metadata={
            "purpose": "进入个人中心",
            "source_page_type": "home",
            "source_page_state": "stable",
            "expected_next_page_type": "profile",
            "target_keywords": ["我的"],
        },
    )
    snapshot = H5TaskFrontierSnapshot.create(max_pages=None).merge(
        source_capture_id="capture-home-before-local-switch",
        source_state_version="state:home-before-local-switch",
        source_location="https://m.example.test/",
        navigation_depth=0,
        candidates=(candidate,),
    )
    snapshot = snapshot.select_entry(snapshot.pending_entries[0].entry_id)
    current_observation = replace(
        work.frontier.observation,
        capture_id="capture-home-after-local-switch",
        state_version="state:home-after-local-switch",
        location="https://m.example.test/",
        page_type="home",
        page_state="stable",
        overlay_signature=None,
        metadata={
            **dict(work.frontier.observation.metadata),
            "detection_status": "recognized",
            "requires_auth": False,
        },
    )
    frontier = _with_task_frontier(
        frontier=replace(work.frontier, observation=current_observation),
        snapshot=snapshot,
    )
    work = replace(
        work,
        source_capture_id=current_observation.capture_id,
        frontier_snapshot_id=frontier.snapshot_id,
        frontier=frontier,
    )

    selected = snapshot.entries[0]
    assert module._can_execute_stable_same_source_page(work=work, entry=selected) is True
    assert module._selected_entry_requiring_recovery(work) is None
    untrusted = replace(
        selected,
        candidate=replace(
            candidate,
            metadata={
                **dict(candidate.metadata),
                "source_page_state": "popup",
            },
        ),
    )
    assert module._can_execute_stable_same_source_page(work=work, entry=untrusted) is False


@pytest.mark.asyncio
async def test_h5_runner_reuses_stable_same_page_after_nested_back() -> None:
    """子页 back 回到同一我的页后，支付视觉候选不得再 navigate 重触发开屏。"""
    from services.api.app.application.h5_exploration_workflow import _with_task_frontier

    module = _turn_module()
    work = _work()
    candidate = CandidateAction(
        candidate_id="candidate-payment",
        semantic_target="立即开通",
        action_type=InteractionOperation.CLICK,
        risk_level=InteractionRiskLevel.MEDIUM,
        exploration_value=96,
        navigation_depth=1,
        expected_surface_kind=SurfaceKind.WEB,
        metadata={
            "source_page_type": "profile",
            "expected_next_page_type": "payment",
            "target_keywords": [
                "profile_vip_payment_entry",
                "profile_local_action",
                "page_type:payment",
                "entry:立即开通",
                "evidence:screenshot",
            ],
            "action_parameters": {
                "target_mode": "visual_coordinate",
                "visual_entry_kind": "profile_vip_payment",
                "visual_source_capture_id": "capture-profile-original",
                "visual_source_url": "https://m.example.test/mine",
                "visual_source_artifact_id": "artifact-profile",
                "visual_source_sha256": f"sha256:{'a' * 64}",
                "screenshot_index": 1,
                "left_ratio": 0.72,
                "top_ratio": 0.24,
                "right_ratio": 0.94,
                "bottom_ratio": 0.29,
                "visual_confidence": 0.93,
            },
        },
    )
    snapshot = H5TaskFrontierSnapshot.create(max_pages=None).merge(
        source_capture_id="capture-profile-original",
        source_state_version="state:profile-original",
        source_location="https://m.example.test/mine",
        navigation_depth=1,
        candidates=(candidate,),
    )
    snapshot = snapshot.select_entry(snapshot.pending_entries[0].entry_id)
    current_observation = replace(
        work.frontier.observation,
        capture_id="capture-profile-after-back",
        state_version="state:profile-after-back",
        location="https://m.example.test/mine",
        page_type="profile",
        page_state="stable",
        overlay_signature=None,
        metadata={
            **dict(work.frontier.observation.metadata),
            "detection_status": "recognized",
            "requires_auth": False,
        },
    )
    frontier = _with_task_frontier(
        frontier=replace(work.frontier, observation=current_observation),
        snapshot=snapshot,
    )
    work = replace(
        work,
        source_capture_id=current_observation.capture_id,
        frontier_snapshot_id=frontier.snapshot_id,
        frontier=frontier,
    )
    recovery_calls = 0

    class Recovery:
        async def restore(self, *, work, agent_run_id, entry):
            nonlocal recovery_calls
            recovery_calls += 1
            return module.H5FrontierRecoveryWorkResult(
                work=work,
                restored=False,
                reason_code="h5_frontier_restore_state_mismatch",
            )

    queue = _Queue(work)
    outcome = await module.H5ExplorationTurnRunner(
        queue=queue,
        turn_executor=_Executor(_terminal_turn(work)),
        continuation=_Continuation(_work(capture_id="capture-after")),
        recovery=Recovery(),
        lease_owner="h5-explorer-1",
    ).run_one(task_id=work.task_id, agent_run_id="run-h5")

    assert recovery_calls == 0
    assert outcome.final_status is ExplorerWorkStatus.SUCCEEDED
    assert len(queue.payload_updates) == 1
    assert queue.payload_updates[0].work.source_capture_id == current_observation.capture_id


def test_h5_runner_does_not_reuse_untrusted_medium_same_page_candidate() -> None:
    """普通 medium 候选不能借支付视觉白名单绕过来源状态恢复。"""
    from services.api.app.application.h5_exploration_workflow import _with_task_frontier

    module = _turn_module()
    work = _work()
    candidate = CandidateAction(
        candidate_id="candidate-untrusted-medium",
        semantic_target="立即开通",
        action_type=InteractionOperation.CLICK,
        risk_level=InteractionRiskLevel.MEDIUM,
        exploration_value=96,
        navigation_depth=1,
        expected_surface_kind=SurfaceKind.WEB,
        metadata={
            "source_page_type": "profile",
            "expected_next_page_type": "payment",
            "target_keywords": ["payment"],
        },
    )
    snapshot = H5TaskFrontierSnapshot.create(max_pages=None).merge(
        source_capture_id="capture-profile-original",
        source_state_version="state:profile-original",
        source_location="https://m.example.test/mine",
        navigation_depth=1,
        candidates=(candidate,),
    )
    snapshot = snapshot.select_entry(snapshot.pending_entries[0].entry_id)
    selected = snapshot.entries[0]
    observation = replace(
        work.frontier.observation,
        capture_id="capture-profile-after-back",
        state_version="state:profile-after-back",
        location="https://m.example.test/mine",
        page_type="profile",
        page_state="stable",
        overlay_signature=None,
        metadata={
            **dict(work.frontier.observation.metadata),
            "detection_status": "recognized",
            "requires_auth": False,
        },
    )
    frontier = _with_task_frontier(
        frontier=replace(work.frontier, observation=observation),
        snapshot=snapshot,
    )
    work = replace(
        work,
        source_capture_id=observation.capture_id,
        frontier_snapshot_id=frontier.snapshot_id,
        frontier=frontier,
    )

    assert module._can_execute_stable_same_source_page(work=work, entry=selected) is False


def test_h5_runner_reuses_trusted_list_detail_candidate_on_same_stable_page() -> None:
    """列表动作重观察后应复用当前稳定页，禁止错误恢复 URL 重触发开屏。"""
    from services.api.app.application.h5_exploration_workflow import _with_task_frontier

    module = _turn_module()
    work = _work()
    candidate = CandidateAction(
        candidate_id="candidate-video-detail",
        semantic_target="当前列表首个内容卡片",
        action_type=InteractionOperation.CLICK,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=98,
        navigation_depth=2,
        expected_surface_kind=SurfaceKind.WEB,
        metadata={
            "source_page_type": "video_list",
            "expected_next_page_type": "video_detail",
            "target_keywords": [
                "nested_content",
                "list_detail_navigation",
                "bottom_tab:2",
            ],
            "action_parameters": {
                "target_mode": "list_detail_navigation",
                "source_page_type": "video_list",
                "expected_page_type": "video_detail",
                "list_detail_selector": ".masonry-grid .movie-item .video-cover:visible",
            },
        },
    )
    snapshot = H5TaskFrontierSnapshot.create(max_pages=None).merge(
        source_capture_id="capture-video-list-original",
        source_state_version="state:video-list-original",
        source_location="https://m.example.test/movie",
        navigation_depth=1,
        candidates=(candidate,),
    )
    snapshot = snapshot.select_entry(snapshot.pending_entries[0].entry_id)
    selected = snapshot.entries[0]
    observation = replace(
        work.frontier.observation,
        capture_id="capture-video-list-reobserved",
        state_version="state:video-list-reobserved",
        location="https://m.example.test/movie",
        page_type="video_list",
        page_state="stable",
        overlay_signature=None,
        metadata={
            **dict(work.frontier.observation.metadata),
            "detection_status": "recognized",
            "requires_auth": False,
        },
    )
    frontier = _with_task_frontier(
        frontier=replace(work.frontier, observation=observation),
        snapshot=snapshot,
    )
    work = replace(
        work,
        source_capture_id=observation.capture_id,
        frontier_snapshot_id=frontier.snapshot_id,
        frontier=frontier,
    )

    assert module._can_execute_stable_same_source_page(work=work, entry=selected) is True
    untrusted_candidate = replace(
        candidate,
        candidate_id="candidate-model-depth-two",
        metadata={
            "source_page_type": "video_list",
            "expected_next_page_type": "video_detail",
            "target_keywords": ["进入视频详情"],
        },
    )
    assert (
        module._can_execute_stable_same_source_page(
            work=work,
            entry=replace(selected, candidate=untrusted_candidate),
        )
        is False
    )


@pytest.mark.asyncio
async def test_h5_runner_persists_failed_recovery_snapshot_before_retrying() -> None:
    """若失败恢复不更新 Work payload，下次领取会丢失尝试次数并无限重试。"""
    from services.api.app.application.h5_exploration_workflow import _with_task_frontier

    module = _turn_module()
    work = _work()
    candidate = work.frontier.candidates[0]
    snapshot = H5TaskFrontierSnapshot.create(max_pages=None).merge(
        source_capture_id="capture-home",
        source_state_version="state:home",
        source_location="https://m.example.test/home",
        navigation_depth=0,
        candidates=(candidate,),
    )
    snapshot = snapshot.select_entry(snapshot.pending_entries[0].entry_id)
    frontier = _with_task_frontier(frontier=work.frontier, snapshot=snapshot)
    work = replace(work, frontier_snapshot_id=frontier.snapshot_id, frontier=frontier)
    updated_snapshot = snapshot.record_restoration_attempt(snapshot.entries[0].entry_id)
    updated_frontier = _with_task_frontier(frontier=work.frontier, snapshot=updated_snapshot)
    recovery_work = replace(
        work,
        frontier_snapshot_id=updated_frontier.snapshot_id,
        frontier=updated_frontier,
    )
    queue = _Queue(work)

    class Recovery:
        async def restore(self, *, work, agent_run_id, entry):
            return module.H5FrontierRecoveryWorkResult(
                work=recovery_work,
                restored=False,
                reason_code="h5_frontier_restore_state_mismatch",
            )

    class Executor:
        async def execute(self, *, work, agent_run_id):
            raise AssertionError("恢复失败时不得执行 Explorer candidate")

    runner = module.H5ExplorationTurnRunner(
        queue=queue,
        turn_executor=Executor(),
        continuation=_Continuation(_work(capture_id="capture-2")),
        recovery=Recovery(),
        lease_owner="h5-explorer-1",
    )

    outcome = await runner.run_one(task_id=work.task_id, agent_run_id="run-h5")

    assert outcome.final_status is ExplorerWorkStatus.RETRY_WAIT
    assert len(queue.payload_updates) == 1
    persisted = H5TaskFrontierSnapshot.from_dict(
        queue.payload_updates[0].work.frontier.observation.metadata["h5_task_frontier"]
    )
    assert persisted.entries[0].restoration_attempt_count == 1


@pytest.mark.asyncio
async def test_h5_runner_marks_second_failed_recovery_terminal_without_executing() -> None:
    """若第二次恢复耗尽仍转 retry_wait，sibling 会永久占用队列。"""
    from services.api.app.application.h5_exploration_workflow import _with_task_frontier

    module = _turn_module()
    work = _work()
    candidate = work.frontier.candidates[0]
    snapshot = H5TaskFrontierSnapshot.create(max_pages=None).merge(
        source_capture_id="capture-home",
        source_state_version="state:home",
        source_location="https://m.example.test/home",
        navigation_depth=0,
        candidates=(candidate,),
    )
    snapshot = snapshot.select_entry(snapshot.pending_entries[0].entry_id)
    snapshot = snapshot.record_restoration_attempt(snapshot.entries[0].entry_id)
    leased_frontier = _with_task_frontier(frontier=work.frontier, snapshot=snapshot)
    leased_work = replace(
        work,
        frontier_snapshot_id=leased_frontier.snapshot_id,
        frontier=leased_frontier,
    )
    snapshot = snapshot.record_restoration_attempt(snapshot.entries[0].entry_id).mark_failed(
        snapshot.entries[0].entry_id,
        reason="h5_frontier_restore_exhausted",
    )
    frontier = _with_task_frontier(frontier=work.frontier, snapshot=snapshot)
    recovery_work = replace(
        work,
        frontier_snapshot_id=frontier.snapshot_id,
        frontier=frontier,
    )
    queue = _Queue(leased_work)

    class Recovery:
        async def restore(self, *, work, agent_run_id, entry):
            return module.H5FrontierRecoveryWorkResult(
                work=recovery_work,
                restored=False,
                reason_code="h5_frontier_restore_exhausted",
            )

    class Executor:
        async def execute(self, *, work, agent_run_id):
            raise AssertionError("恢复耗尽时不得执行 Explorer candidate")

    outcome = await module.H5ExplorationTurnRunner(
        queue=queue,
        turn_executor=Executor(),
        continuation=_Continuation(_work(capture_id="capture-2")),
        recovery=Recovery(),
        lease_owner="h5-explorer-1",
    ).run_one(task_id=leased_work.task_id, agent_run_id="run-h5")

    assert outcome.final_status is ExplorerWorkStatus.BOUNDED
    assert len(queue.payload_updates) == 1
    assert queue.transitions[-1].target_status is ExplorerWorkStatus.BOUNDED
    assert queue.transitions[-1].row_version == 6
    assert queue.transitions[-1].terminal is not None
    assert queue.transitions[-1].terminal.status is ExplorationTerminalStatus.BOUNDED
    assert queue.transitions[-1].terminal.reason_code == "h5_frontier_restore_exhausted"


@pytest.mark.asyncio
async def test_exhausted_recovery_with_pending_sibling_resumes_instead_of_bounding() -> None:
    """单条目恢复耗尽不得连坐仍有 pending sibling 的探索 Run。"""
    from services.api.app.application.h5_exploration_workflow import _with_task_frontier

    module = _turn_module()
    work = _work()
    candidate = work.frontier.candidates[0]
    sibling = replace(
        candidate,
        candidate_id="candidate-sibling",
        semantic_target="立即开通",
    )
    snapshot = H5TaskFrontierSnapshot.create(max_pages=None).merge(
        source_capture_id="capture-home",
        source_state_version="state:home",
        source_location="https://m.example.test/home",
        navigation_depth=0,
        candidates=(candidate, sibling),
    )
    selected_entry = next(
        entry
        for entry in snapshot.pending_entries
        if entry.candidate.candidate_id == candidate.candidate_id
    )
    snapshot = snapshot.select_entry(selected_entry.entry_id)
    leased_frontier = _with_task_frontier(frontier=work.frontier, snapshot=snapshot)
    leased_work = replace(
        work,
        frontier_snapshot_id=leased_frontier.snapshot_id,
        frontier=leased_frontier,
    )
    exhausted_snapshot = snapshot.record_restoration_attempt(
        selected_entry.entry_id
    ).record_restoration_attempt(selected_entry.entry_id).mark_failed(
        selected_entry.entry_id,
        reason="h5_frontier_restore_exhausted",
    )
    frontier = _with_task_frontier(frontier=work.frontier, snapshot=exhausted_snapshot)
    recovery_work = replace(
        work,
        frontier_snapshot_id=frontier.snapshot_id,
        frontier=frontier,
    )
    queue = _Queue(leased_work)

    class Recovery:
        async def restore(self, *, work, agent_run_id, entry):
            return module.H5FrontierRecoveryWorkResult(
                work=recovery_work,
                restored=False,
                reason_code="h5_frontier_restore_exhausted",
            )

    class Executor:
        async def execute(self, *, work, agent_run_id):
            raise AssertionError("恢复耗尽的当轮不得执行 Explorer candidate")

    outcome = await module.H5ExplorationTurnRunner(
        queue=queue,
        turn_executor=Executor(),
        continuation=_Continuation(_work(capture_id="capture-2")),
        recovery=Recovery(),
        lease_owner="h5-explorer-1",
    ).run_one(task_id=leased_work.task_id, agent_run_id="run-h5")

    assert outcome.final_status is ExplorerWorkStatus.RETRY_WAIT
    failure = queue.transitions[-1]
    assert failure.target_status is ExplorerWorkStatus.RETRY_WAIT
    assert failure.terminal is None
    assert failure.retry_delay_seconds == 20


@pytest.mark.asyncio
async def test_exhausted_recovery_with_selected_sibling_stays_retry_wait() -> None:
    """恢复适配器已改选 sibling 时，即使没有 pending 也必须继续重领执行。"""
    from services.api.app.application.h5_exploration_workflow import _with_task_frontier

    module = _turn_module()
    work = _work()
    stale = work.frontier.candidates[0]
    sibling = replace(stale, candidate_id="candidate-sibling", semantic_target="广场")
    snapshot = H5TaskFrontierSnapshot.create(max_pages=None).merge(
        source_capture_id="capture-home",
        source_state_version="state:home",
        source_location="https://m.example.test/home",
        navigation_depth=0,
        candidates=(stale, sibling),
    )
    stale_entry = next(
        entry
        for entry in snapshot.pending_entries
        if entry.candidate.candidate_id == stale.candidate_id
    )
    snapshot = snapshot.select_entry(stale_entry.entry_id)
    snapshot = snapshot.record_restoration_attempt(stale_entry.entry_id)
    snapshot = snapshot.record_restoration_attempt(stale_entry.entry_id)
    leased_frontier = _with_task_frontier(frontier=work.frontier, snapshot=snapshot)
    leased_work = replace(
        work,
        frontier_snapshot_id=leased_frontier.snapshot_id,
        frontier=leased_frontier,
    )

    failed_snapshot = snapshot.mark_failed(
        stale_entry.entry_id,
        reason="h5_frontier_restore_exhausted",
    )
    sibling_entry = next(
        entry
        for entry in failed_snapshot.pending_entries
        if entry.candidate.candidate_id == sibling.candidate_id
    )
    reselected_snapshot = failed_snapshot.select_entry(sibling_entry.entry_id)
    reselected_frontier = _with_task_frontier(
        frontier=replace(work.frontier, candidates=(sibling,)),
        snapshot=reselected_snapshot,
    )
    recovery_work = replace(
        work,
        frontier_snapshot_id=reselected_frontier.snapshot_id,
        frontier=reselected_frontier,
    )
    queue = _Queue(leased_work)

    class Recovery:
        async def restore(self, *, work, agent_run_id, entry):
            del work, agent_run_id, entry
            return module.H5FrontierRecoveryWorkResult(
                work=recovery_work,
                restored=False,
                reason_code="h5_frontier_restore_exhausted",
                reselected_sibling=True,
            )

    class Executor:
        async def execute(self, *, work, agent_run_id):
            del work, agent_run_id
            raise AssertionError("恢复耗尽的当轮不得执行旧的 Explorer candidate")

    outcome = await module.H5ExplorationTurnRunner(
        queue=queue,
        turn_executor=Executor(),
        continuation=_Continuation(_work(capture_id="capture-2")),
        recovery=Recovery(),
        lease_owner="h5-explorer-1",
    ).run_one(task_id=leased_work.task_id, agent_run_id="run-h5")

    assert outcome.final_status is ExplorerWorkStatus.RETRY_WAIT
    assert outcome.reselected_sibling is True
    transition = queue.transitions[-1]
    assert transition.target_status is ExplorerWorkStatus.RETRY_WAIT
    assert transition.terminal is None
    assert transition.retry_delay_seconds == 20
    assert transition.reset_attempt_count is True


def _capture_for_turn(work, capture_id: str) -> CaptureRef:
    return CaptureRef(
        task_id=work.task_id,
        session_id=work.session_id,
        capture_id=capture_id,
        surface_kind=SurfaceKind.WEB,
        capture_kind=CaptureKind.PAGE,
        role="after_action",
        parent_capture_id=work.source_capture_id,
        action_execution_id="action-after",
    )


def _action_intent(work) -> ActionIntent:
    return ActionIntent(
        task_id=work.task_id,
        session_id=work.session_id,
        intent_id="intent-profile",
        idempotency_key="intent-profile",
        surface_kind=SurfaceKind.WEB,
        source_capture_id=work.source_capture_id,
        action_type=InteractionOperation.CLICK,
        semantic_target="我的",
        purpose="补齐个人中心覆盖",
        capture_profile=CaptureProfile(
            profile_id="h5-test",
            before=CaptureDirective(role="before_action", artifact_types=("screenshot",)),
            after=CaptureDirective(role="after_action", artifact_types=("screenshot",)),
        ),
        deadline=work.deadline,
        parameters={"candidate_id": "candidate-profile"},
    )


@pytest.mark.asyncio
async def test_restoration_exhaustion_reselects_pending_sibling_for_reclaim() -> None:
    """若耗尽后不改选 pending sibling，重领到的 Work 无候选可执行，
    decide_next 会以 task_frontier_not_exhausted_cleanly 把整个 Run 有界收束，
    撇下全部未探候选（实测 51鉴片 18 步、91蜜桃 24 步即收工）。"""
    from services.api.app.application.h5_exploration_workflow import _with_task_frontier
    from services.api.app.application.h5_runtime_composition import (
        H5CoordinatorFrontierRecovery,
    )
    from services.api.app.domain.exploration.h5_frontier import (
        H5_TASK_FRONTIER_METADATA_KEY,
    )

    now = datetime.now(UTC)
    stale = CandidateAction(
        candidate_id="candidate-card",
        semantic_target="当前列表首个内容卡片",
        action_type=InteractionOperation.CLICK,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=80,
        navigation_depth=1,
        expected_surface_kind=SurfaceKind.WEB,
    )
    sibling = CandidateAction(
        candidate_id="candidate-tabbar-4",
        semantic_target="div.van-tabbar:visible > div.van-tabbar-item:nth-of-type(4):visible",
        action_type=InteractionOperation.CLICK,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=70,
        navigation_depth=1,
        expected_surface_kind=SurfaceKind.WEB,
    )
    snapshot = H5TaskFrontierSnapshot.create(max_pages=None).merge(
        source_capture_id="capture-home",
        source_state_version="state:capture-1",
        source_location="https://m.example.test/home",
        navigation_depth=0,
        candidates=(stale, sibling),
        now=now,
    )
    stale_entry = next(
        entry
        for entry in snapshot.pending_entries
        if entry.candidate.candidate_id == "candidate-card"
    )
    snapshot = snapshot.select_entry(stale_entry.entry_id, selected_at=now)
    snapshot = snapshot.record_restoration_attempt(stale_entry.entry_id)
    snapshot = snapshot.record_restoration_attempt(stale_entry.entry_id)

    base = _work()
    frontier = _with_task_frontier(frontier=base.frontier, snapshot=snapshot)
    work = replace(base, frontier_snapshot_id=frontier.snapshot_id, frontier=frontier)
    selected_entry = next(
        entry for entry in snapshot.entries if entry.entry_id == stale_entry.entry_id
    )

    recovery = H5CoordinatorFrontierRecovery(
        capture_coordinator=object(),
        detector=object(),
        assessment_repository=object(),
    )
    result = await recovery.restore(
        work=work,
        agent_run_id="run-h5",
        entry=selected_entry,
    )

    assert result.restored is False
    assert result.reason_code == "h5_frontier_restore_exhausted"
    restored_snapshot = H5TaskFrontierSnapshot.from_dict(
        result.work.frontier.observation.metadata[H5_TASK_FRONTIER_METADATA_KEY]
    )
    statuses = {
        entry.candidate.candidate_id: entry.status.value for entry in restored_snapshot.entries
    }
    assert statuses["candidate-card"] == "failed"
    assert statuses["candidate-tabbar-4"] == "selected"
    # 重领后必须有可执行候选，否则 decide_next 立刻有界收束
    assert tuple(item.candidate_id for item in result.work.frontier.candidates) == (
        "candidate-tabbar-4",
    )


def _dynamic_restore_entry(*, source_page_type: str = "video_list"):
    """构造一条跨状态 sibling：来源页是内容列表，内容每次进入都会漂移。"""
    from services.api.app.application.h5_exploration_observation import build_h5_frontier

    detection = {
        "page_capture_id": "capture-list",
        "page_url": "https://m.example.test/movie",
        "page_type": source_page_type,
        "page_state": "stable",
        "detection_status": "recognized",
        "matched_catalog_signals": {"ocr_keywords": ["最新"]},
        "risk_flags": [],
        "candidate_actions": [],
    }
    state_version = build_h5_frontier(
        detection_result=detection,
        observed_at=datetime.now(UTC),
        navigation_depth=0,
    ).observation.state_version
    candidate = CandidateAction(
        candidate_id="candidate-card",
        semantic_target="当前列表首个内容卡片",
        action_type=InteractionOperation.CLICK,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=80,
        navigation_depth=1,
        expected_surface_kind=SurfaceKind.WEB,
        metadata={"source_page_type": source_page_type},
    )
    snapshot = H5TaskFrontierSnapshot.create(max_pages=None).merge(
        source_capture_id="capture-list",
        source_state_version=state_version,
        source_location="https://m.example.test/movie",
        navigation_depth=0,
        candidates=(candidate,),
    )
    return snapshot.pending_entries[0], detection


class _DriftingBrowser:
    """navigate 回到同一列表页，但素材已换（state_version 必然漂移）。"""

    def __init__(self, detection: dict, *, page_state: str = "stable") -> None:
        self._detection = detection
        self._page_state = page_state
        self.navigated = False

    async def is_history_ancestor(self, *, source_location, current_location):
        del source_location, current_location
        return False

    async def back(self):
        raise AssertionError("非历史祖先必须走 navigate")

    async def navigate(self, destination_url):
        self.navigated = True
        return destination_url

    async def capture(self):
        return {"capture_id": "capture-list-fresh"}

    async def detect(self, *, capture):
        return {
            **self._detection,
            "page_capture_id": capture["capture_id"],
            "page_state": self._page_state,
            # 内容漂移：换了一批素材关键词，state_version 随之改变
            "matched_catalog_signals": {"ocr_keywords": ["最新", "今日热播", "新上架"]},
        }

    async def assess(self, *, capture, detection):
        del detection
        return SimpleNamespace(
            execution_status="accepted",
            page_type_assessment={
                "page_type": self._detection["page_type"],
                "status": "covered",
                "reason_code": "h5_page_detection_covered",
                "page_capture_ids": [capture["capture_id"]],
                "evidence_artifact_ids": ["artifact:list"],
                "page_states": [self._page_state],
            },
        )


@pytest.mark.asyncio
async def test_navigate_restore_accepts_same_page_content_drift() -> None:
    """内容站列表每次进入都换素材；navigate 若坚持 state_version 逐字相等，
    每条 sibling 只剩一次机会（实测 R6 有 7 条因此判死）。"""
    recovery = import_module("services.api.app.application.h5_exploration_recovery")
    entry, detection = _dynamic_restore_entry()
    browser = _DriftingBrowser(detection)

    result = await recovery.restore_h5_frontier_source(
        entry=entry,
        current_location="https://m.example.test/moviePlayer",
        browser=browser,
    )

    assert browser.navigated is True
    assert result.restored is True
    assert result.route == "navigate"


@pytest.mark.asyncio
@pytest.mark.parametrize("same_identity", [True, False])
async def test_navigation_rebaseline_checks_projected_logical_identity(same_identity):
    recovery = import_module("services.api.app.application.h5_exploration_recovery")
    entry, detection = _dynamic_restore_entry()
    expected_identity = "sha256:" + "1" * 64
    actual_identity = expected_identity if same_identity else "sha256:" + "3" * 64
    entry = replace(entry, candidate=replace(entry.candidate, metadata={
        **entry.candidate.metadata, "source_identity_fingerprint": expected_identity,
    }))
    detection["logical_surface"] = {
        "protocol_version": "logical_surface.v1", "shadow": False,
        "render_profile": {"kind": "dom_spa", "status": "confirmed"},
        "identity_fingerprint": actual_identity,
        "state_fingerprint": "sha256:" + "2" * 64,
        "page_type_binding": {
            "identity_fingerprint": actual_identity, "page_type": "video_list",
        },
    }
    result = await recovery.restore_h5_frontier_source(
        entry=entry, current_location="https://m.example.test/moviePlayer",
        browser=_DriftingBrowser(detection),
    )
    assert result.restored is same_identity
    if same_identity:
        assert result.detection["logical_surface"]["identity_fingerprint"] == expected_identity
    else:
        assert result.capture is not None
        assert result.detection["logical_surface"]["identity_fingerprint"] == actual_identity


@pytest.mark.parametrize("current_identity", ["source", "other", None])
def test_same_url_same_page_type_does_not_prove_same_logical_module(current_identity) -> None:
    module = _turn_module()
    work = _work()
    candidate = CandidateAction(
        candidate_id="category-entry", semantic_target="分类", exploration_value=10,
        action_type=InteractionOperation.CLICK, risk_level=InteractionRiskLevel.LOW,
        navigation_depth=0, expected_surface_kind=SurfaceKind.WEB,
        metadata={
            "source_page_type": "home", "source_page_state": "stable",
            "source_identity_fingerprint": "source", "expected_next_page_type": "content_list",
        },
    )
    observation = replace(
        work.frontier.observation, location="https://example.test/#/home", page_type="home",
        page_state="stable", overlay_signature=None, metadata={
            "detection_status": "recognized", "requires_auth": False,
            "logical_surface_identity_fingerprint": current_identity,
        },
    )
    work = replace(work, frontier=replace(work.frontier, observation=observation))
    entry = SimpleNamespace(candidate=candidate, navigation_depth=0,
                            source_location=observation.location)
    assert module._can_execute_stable_same_source_page(work=work, entry=entry) is (
        current_identity == "source"
    )


def test_spa_hash_route_must_match_before_reusing_local_sibling() -> None:
    module = _turn_module()
    work = _work()
    candidate = replace(work.frontier.candidates[0], metadata={
        "source_page_type": "home", "source_page_state": "stable",
        "expected_next_page_type": "content_list",
    }, navigation_depth=0)
    work = replace(work, frontier=replace(work.frontier, observation=replace(
        work.frontier.observation, location="https://example.test/#/community", page_type="home",
        page_state="stable", overlay_signature=None, metadata={"detection_status": "recognized"},
    )))
    entry = SimpleNamespace(candidate=candidate, navigation_depth=0,
                            source_location="https://example.test/#/home")
    assert module._can_execute_stable_same_source_page(work=work, entry=entry) is False


@pytest.mark.asyncio
async def test_navigate_restore_accepts_only_untrusted_transition_assessment() -> None:
    """确定性来源恢复不应要求普通业务点击转场证明。"""
    recovery = import_module("services.api.app.application.h5_exploration_recovery")
    entry, detection = _dynamic_restore_entry()

    class Browser(_DriftingBrowser):
        async def assess(self, *, capture, detection):
            del detection
            return SimpleNamespace(
                execution_status="accepted",
                page_type_assessment={
                    "page_type": "video_list",
                    "status": "evidence_insufficient",
                    "reason_code": "h5_logical_transition_untrusted",
                    "page_capture_ids": [capture["capture_id"]],
                    "evidence_artifact_ids": ["artifact:list"],
                    "page_states": ["stable"],
                },
            )

    result = await recovery.restore_h5_frontier_source(
        entry=entry,
        current_location="https://m.example.test/moviePlayer",
        browser=Browser(detection),
    )

    assert result.restored is True
    assert result.route == "navigate"


@pytest.mark.asyncio
async def test_navigate_restore_rejects_other_insufficient_assessment_reason() -> None:
    """来源恢复只能豁免转场证明，其他证据不足原因仍必须失败关闭。"""
    recovery = import_module("services.api.app.application.h5_exploration_recovery")
    entry, detection = _dynamic_restore_entry()

    class Browser(_DriftingBrowser):
        async def assess(self, *, capture, detection):
            del detection
            return SimpleNamespace(
                execution_status="accepted",
                page_type_assessment={
                    "page_type": "video_list",
                    "status": "evidence_insufficient",
                    "reason_code": "h5_page_detection_evidence_insufficient",
                    "page_capture_ids": [capture["capture_id"]],
                    "evidence_artifact_ids": ["artifact:list"],
                    "page_states": ["stable"],
                },
            )

    result = await recovery.restore_h5_frontier_source(
        entry=entry,
        current_location="https://m.example.test/moviePlayer",
        browser=Browser(detection),
    )

    assert result.restored is False
    assert result.reason_code == "h5_frontier_restore_assessment_rejected"


@pytest.mark.asyncio
async def test_navigate_restore_retriggered_startup_ad_fails_closed_when_undismissable() -> None:
    """来源恢复重触发开屏且浏览器不能清障时，必须以专用原因失败关闭。"""
    recovery = import_module("services.api.app.application.h5_exploration_recovery")
    entry, detection = _dynamic_restore_entry()
    browser = _DriftingBrowser({**detection, "page_type": "startup_ad"})

    result = await recovery.restore_h5_frontier_source(
        entry=entry,
        current_location="https://m.example.test/moviePlayer",
        browser=browser,
    )

    assert result.restored is False
    assert result.reason_code == "h5_frontier_restore_startup_ad_blocked"


@pytest.mark.asyncio
async def test_navigate_restore_still_rejects_unstable_page_state() -> None:
    """弹窗/遮罩未清时不得据此执行 sibling。"""
    recovery = import_module("services.api.app.application.h5_exploration_recovery")
    entry, detection = _dynamic_restore_entry()
    browser = _DriftingBrowser(detection, page_state="popup")

    result = await recovery.restore_h5_frontier_source(
        entry=entry,
        current_location="https://m.example.test/moviePlayer",
        browser=browser,
    )

    assert result.restored is False


@pytest.mark.parametrize("after_type,restored", [("video_list", True), ("home", False)])
async def test_source_restore_clears_verified_popup_before_comparing_business_type(
    after_type: str, restored: bool,
) -> None:
    """遮罩中的页型不等于来源页型；只清障，清障后仍须核验来源业务页。"""
    recovery = import_module("services.api.app.application.h5_exploration_recovery")
    entry, detection = _dynamic_restore_entry()

    class Browser(_DriftingBrowser):
        closed = False

        async def detect(self, *, capture):
            return {
                **await super().detect(capture=capture),
                "page_type": after_type if self.closed else "home",
                "page_state": "stable" if self.closed else "popup",
            }

        async def dismiss_popup(self, *, capture, detection):
            self.closed = True
            return {"capture_id": "capture-cleared"}

    browser = Browser(detection)
    result = await recovery.restore_h5_frontier_source(
        entry=entry, current_location="https://m.example.test/moviePlayer", browser=browser,
    )
    assert browser.closed is True
    assert result.restored is restored


@pytest.mark.parametrize("override", [
    {"page_url": "https://other.example.test/"},
    {"page_type": "login"},
    {"page_type": "register"},
    {"requires_auth": True},
    {"risk_flags": ["authentication_required"]},
    {"risk_flags": {"unknown": True}},
    {"detection_status": "unknown"},
])
async def test_source_popup_clearance_rejects_untrusted_or_auth_surface(override):
    """不同落点、认证门槛或识别未知不能借来源清障自动点击。"""
    recovery = import_module("services.api.app.application.h5_exploration_recovery")
    entry, detection = _dynamic_restore_entry()
    assert recovery._is_retriggered_source_popup(
        entry=entry, detection={**detection, "page_state": "popup", **override},
    ) is False


@pytest.mark.parametrize("repeat_startup", [False, True])
async def test_source_restore_handles_popup_changing_to_startup_without_renavigation(
    repeat_startup,
):
    """清障候选失效后的新现场仍须识别下一层，不重导航也不增加开屏预算。"""
    recovery = import_module("services.api.app.application.h5_exploration_recovery")
    entry, detection = _dynamic_restore_entry()

    class Browser(_DriftingBrowser):
        phase = 0
        popup_calls = 0
        startup_calls = 0

        async def detect(self, *, capture):
            return {
                **await super().detect(capture=capture),
                "page_type": "startup_ad" if self.phase == 1 else "video_list",
                "page_state": (
                    "popup" if self.phase == 0 else "countdown" if self.phase == 1 else "stable"
                ),
            }

        async def dismiss_popup(self, *, capture, detection):
            self.popup_calls += 1
            self.phase = 1
            return {"capture_id": "capture-fresh-before-close"}

        async def dismiss_startup_ad(self, *, capture, detection):
            self.startup_calls += 1
            self.phase = 1 if repeat_startup else 2
            return {"capture_id": "capture-startup-cleared"}

    browser = Browser(detection)
    result = await recovery.restore_h5_frontier_source(
        entry=entry, current_location="https://m.example.test/moviePlayer", browser=browser,
    )
    assert browser.popup_calls == 1
    assert browser.startup_calls == 1
    assert result.restored is (not repeat_startup)
    if repeat_startup:
        assert result.reason_code == "h5_frontier_restore_startup_ad_blocked"


async def test_source_restore_changed_popup_stops_at_deadline_not_layer_count():
    """只返回新动作前现场但弹层一直存在时，不能无限重复清障。"""
    recovery = import_module("services.api.app.application.h5_exploration_recovery")
    entry, detection = _dynamic_restore_entry()

    class Browser(_DriftingBrowser):
        popup_calls = 0

        async def detect(self, *, capture):
            return {**await super().detect(capture=capture), "page_state": "popup"}

        async def dismiss_popup(self, *, capture, detection):
            self.popup_calls += 1
            return {"capture_id": f"capture-fresh-{self.popup_calls}"}

    browser = Browser(detection)
    started = datetime.now(UTC)
    result = await recovery.restore_h5_frontier_source(
        entry=entry, current_location="https://m.example.test/moviePlayer", browser=browser,
        deadline=started + timedelta(seconds=50),
        now=lambda: started + timedelta(seconds=browser.popup_calls),
    )
    assert result.restored is False
    assert result.reason_code == recovery.H5_FRONTIER_RESTORE_POPUP_DEADLINE
    assert browser.popup_calls == 50


@pytest.mark.parametrize("override", [
    {"page_url": "https://other.example.test/"}, {"detection_status": "unknown"},
])
async def test_source_restore_startup_refresh_rejects_other_location_or_unknown(override):
    recovery = import_module("services.api.app.application.h5_exploration_recovery")
    entry, detection = _dynamic_restore_entry()
    assert recovery._is_retriggered_startup_ad(
        entry=entry, detection={**detection, "page_type": "startup_ad", **override},
    ) is False
