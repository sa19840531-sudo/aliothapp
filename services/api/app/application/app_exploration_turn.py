"""App Screenshot Explorer 单轮工作的租约与执行编排。"""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Protocol
from uuid import uuid4

from services.api.app.application.app_action_observation import action_observation_for_successor
from services.api.app.application.app_branch_continuation import (
    app_branch_no_progress_terminal,
    resume_app_frontier_branch,
)
from services.api.app.application.app_candidate_eligibility import (
    count_eligible_app_candidates,
    is_app_candidate_eligible,
)
from services.api.app.application.app_exploration_cancellation import (
    AppExplorationCanceledError,
    raise_if_app_exploration_canceled,
    run_app_exploration_operation,
)
from services.api.app.application.app_exploration_lease import (
    AppExplorationLeaseGuard,
    AppExplorationLeaseLostError,
    AppFrontierLeaseGuard,
)
from services.api.app.application.app_frontier_queue import (
    AppFrontierQueue,
    ClaimAppFrontierCommand,
    RenewAppFrontierLeaseCommand,
    TransitionAppFrontierCommand,
)
from services.api.app.application.app_frontier_restoration import (
    AppFrontierDeferredError,
    AppFrontierRestorationError,
    app_frontier_restore_attempts_exhausted,
    app_frontier_same_semantic_retry_allowed,
    app_frontier_same_semantic_retry_exhausted,
    bind_app_frontier_restoration_candidate,
    build_app_frontier_restore_intent,
)
from services.api.app.application.app_global_frontier import AppGlobalFrontierService
from services.api.app.application.app_metrics import (
    APP_FRONTIER_CLAIM_AGE_SECONDS,
    observe_app_metric_safely,
)
from services.api.app.application.app_screenshot_exploration import (
    AppScreenshotExplorationResult,
    ContinueAppScreenshotExplorationCommand,
    ContinueAppScreenshotObservationCommand,
)
from services.api.app.application.app_workflow import AppWorkflowCancellation
from services.api.app.application.capture_coordination import (
    CaptureCoordinationError,
    CaptureCoordinationStage,
)
from services.api.app.application.exploration_scheduler_facts import (
    ExplorationSchedulerFactsReader,
    merge_exploration_scheduler_facts,
)
from services.api.app.application.exploration_work_queue import (
    AppFrontierSettlement,
    ClaimExplorerWorkCommand,
    CommitExplorerWorkSuccessorCommand,
    ExplorerWorkDeadlineExceededError,
    ExplorerWorkQueue,
    ExplorerWorkStatus,
    ExplorerWorkType,
    RenewExplorerWorkLeaseCommand,
    TransitionExplorerWorkCommand,
)
from services.api.app.application.explorer_turn import (
    ExplorerTurnExecutor,
    ExplorerTurnResult,
)
from services.api.app.application.supplemental_evidence_policy import (
    SupplementalEvidenceOperation,
    SupplementalEvidencePolicy,
)
from services.api.app.domain.exploration import (
    AppExplorationOutcome,
    AppExplorationTerminalEvaluator,
    AppFrontierCounts,
    AppFrontierItem,
    AppFrontierStatus,
    CandidateAction,
    ExplorationTerminal,
    ExplorationTerminalStatus,
    ExplorationWork,
    ProbeWork,
)
from services.api.app.domain.inspection import EvidenceSourceGrade, RestorationPlan
from services.api.app.domain.interaction import (
    ActionIntent,
    InteractionOperation,
    InteractionResultStatus,
    SurfaceKind,
    TimedObservationIntent,
)

_GLOBAL_FRONTIER_FAIRNESS_NO_NEW_PAGE_LIMIT = 1
_ACTION_DEADLINE_MIN_HEADROOM_SECONDS = 180
_ACTION_DEADLINE_MAX_HEADROOM_SECONDS = 1200
_ACTION_DEADLINE_CLOSURE_RESERVE_SECONDS = 180


@dataclass(frozen=True, kw_only=True)
class AppExplorationTurnOutcome:
    """一次领取尝试的引用型结果。"""

    claimed: bool
    work_id: str | None = None
    final_status: ExplorerWorkStatus | None = None
    turn: ExplorerTurnResult | None = None
    result_capture_id: str | None = None
    successor_work_id: str | None = None
    error_code: str | None = None
    evidence_request_id: str | None = None
    source_capture_id: str | None = None
    work_type: str | None = None
    restoration_plan: RestorationPlan | None = None
    restoration_attempt_id: str | None = None
    equivalence_assessment_id: str | None = None
    source_grade: EvidenceSourceGrade | None = None


class AppExplorationContinuationPort(Protocol):
    """把成功动作轮的 after capture 准备为尚未发布的后继 Work。"""

    async def prepare_after_action(
        self,
        command: ContinueAppScreenshotExplorationCommand,
    ) -> AppScreenshotExplorationResult: ...

    async def prepare_after_observation(
        self,
        command: ContinueAppScreenshotObservationCommand,
    ) -> AppScreenshotExplorationResult: ...


class AppExplorationTurnRunner:
    """领取一条 App Exploration Work，并完成确定性状态写回。"""

    def __init__(
        self,
        *,
        queue: ExplorerWorkQueue,
        turn_executor: ExplorerTurnExecutor,
        continuation: AppExplorationContinuationPort,
        lease_owner: str,
        lease_seconds: int = 180,
        retry_delay_seconds: int = 5,
        cancellation: AppWorkflowCancellation | None = None,
        cancellation_poll_interval_seconds: float = 1.0,
        supplemental_evidence_policy: SupplementalEvidencePolicy | None = None,
        global_frontier: AppGlobalFrontierService | None = None,
        frontier_queue: AppFrontierQueue | None = None,
        scheduler_facts_reader: ExplorationSchedulerFactsReader | None = None,
    ) -> None:
        if not isinstance(lease_owner, str) or not lease_owner.strip():
            raise ValueError("lease_owner 必须为非空字符串")
        if lease_owner != lease_owner.strip():
            raise ValueError("lease_owner 不能包含首尾空白")
        if lease_seconds <= 0 or retry_delay_seconds < 0:
            raise ValueError("租约时长和重试延迟不合法")
        if cancellation_poll_interval_seconds <= 0:
            raise ValueError("取消轮询间隔必须大于 0")
        self._queue = queue
        self._turn_executor = turn_executor
        self._continuation = continuation
        self._lease_owner = lease_owner
        self._lease_seconds = lease_seconds
        self._retry_delay_seconds = retry_delay_seconds
        self._cancellation = cancellation
        self._cancellation_poll_interval_seconds = cancellation_poll_interval_seconds
        self._supplemental_evidence_policy = (
            supplemental_evidence_policy
            if supplemental_evidence_policy is not None
            else SupplementalEvidencePolicy()
        )
        self._global_frontier = global_frontier
        self._frontier_queue = frontier_queue
        self._scheduler_facts_reader = scheduler_facts_reader

    async def run_one(
        self,
        *,
        task_id: str,
        agent_run_id: str,
        work_type: ExplorerWorkType = ExplorerWorkType.EXPLORATION,
    ) -> AppExplorationTurnOutcome:
        for field_name, value in (("task_id", task_id), ("agent_run_id", agent_run_id)):
            if not isinstance(value, str) or not value.strip() or value != value.strip():
                raise ValueError(f"{field_name} 必须为无首尾空白的非空字符串")
        if not isinstance(work_type, ExplorerWorkType):
            raise TypeError("work_type 必须为 ExplorerWorkType")
        if work_type is ExplorerWorkType.PROBE:
            self._supplemental_evidence_policy.require_enabled(
                SupplementalEvidenceOperation.CLAIM_PROBE
            )
        try:
            await self._raise_if_canceled(task_id=task_id, agent_run_id=agent_run_id)
        except AppExplorationCanceledError:
            await self.cancel_nonterminal(task_id=task_id)
            return AppExplorationTurnOutcome(
                claimed=False,
                final_status=ExplorerWorkStatus.CANCELLED,
                error_code=AppExplorationCanceledError.error_code,
            )
        lease_token = str(uuid4())
        lease = await self._queue.claim(
            command=ClaimExplorerWorkCommand(
                work_type=work_type,
                lease_owner=self._lease_owner,
                lease_seconds=self._lease_seconds,
                task_id=task_id,
                surface_kinds=(SurfaceKind.ANDROID, SurfaceKind.IOS),
            ),
            lease_token=lease_token,
        )
        if lease is None:
            return AppExplorationTurnOutcome(claimed=False)
        running_version = await self._queue.transition(
            command=TransitionExplorerWorkCommand(
                work_id=lease.work.work_id,
                expected_status=ExplorerWorkStatus.LEASED,
                target_status=ExplorerWorkStatus.RUNNING,
                lease_token=lease.lease_token,
                row_version=lease.row_version,
            )
        )
        lease_guard = AppExplorationLeaseGuard(
            queue=self._queue,
            command=RenewExplorerWorkLeaseCommand(
                work_id=lease.work.work_id,
                lease_owner=self._lease_owner,
                lease_token=lease.lease_token,
                lease_seconds=self._lease_seconds,
            ),
            renew_interval_seconds=max(1, self._lease_seconds // 3),
        )
        assigned_item = None
        selected_frontier_item = None
        frontier_lease_guard = None
        try:
            async with lease_guard:
                _validate_app_work(lease.work)
                if isinstance(lease.work, ProbeWork):
                    self._supplemental_evidence_policy.require_enabled(
                        SupplementalEvidenceOperation.EXECUTE_PROBE
                    )
                await lease_guard.assert_healthy()
                await self._raise_if_canceled(task_id=task_id, agent_run_id=agent_run_id)
                work = lease.work
                if isinstance(work, ExplorationWork) and self._scheduler_facts_reader is not None:
                    facts = await self._scheduler_facts_reader.read_app_facts(
                        task_id=task_id,
                        agent_run_id=agent_run_id,
                        session_id=work.session_id,
                    )
                    if not isinstance(facts, Mapping):
                        raise TypeError("App 调度事实读取器必须返回 Mapping")
                    # Deferred 会重新领取同一 Work，必须刷新判定进度，不能沿用发布时的缺口。
                    work = replace(
                        work,
                        frontier=replace(
                            work.frontier,
                            observation=replace(
                                work.frontier.observation,
                                metadata=merge_exploration_scheduler_facts(
                                    work.frontier.observation.metadata,
                                    facts,
                                    surface="app",
                                ),
                            ),
                        ),
                    )
                if isinstance(work, ExplorationWork) and self._global_frontier is not None:
                    registered = await self._global_frontier.register_work_candidates(
                        work=work,
                        agent_run_id=agent_run_id,
                    )
                    work = self._global_frontier.retain_registered_candidates(
                        work=work,
                        registered=registered,
                    )
                    work = await self._global_frontier.project_counts(
                        work=work,
                        agent_run_id=agent_run_id,
                    )
                    decision = await _recover_turn_decision(
                        self._turn_executor,
                        work=work,
                    )
                    decision = _discard_stale_candidate_decision(
                        work=work,
                        recovered_decision=decision,
                    )
                    decision = _prefer_current_goal_terminal(
                        work=work,
                        recovered_decision=decision,
                    )
                    branch_terminal = app_branch_no_progress_terminal(work)
                    if (
                        branch_terminal is not None
                        and isinstance(decision, ExplorationTerminal)
                        and decision.reason_code != branch_terminal.reason_code
                    ):
                        # 已恢复的其他终态保持原有处理，不借局部计数重开其执行窗口。
                        branch_terminal = None
                    if branch_terminal is not None:
                        decision = branch_terminal
                    fairness_applied = False
                    deterministic_checked = False
                    if decision is None:
                        decision, deterministic_checked = await _decide_deterministic(
                            self._turn_executor,
                            work=work,
                        )
                    if decision is None and _can_schedule_goal_frontier_without_model(work):
                        direct_decision, assigned_item = await self._schedule_pending_frontier_once(
                            work=work,
                            agent_run_id=agent_run_id,
                            missing_goal_only=True,
                        )
                        if direct_decision is not None:
                            decision = direct_decision
                            fairness_applied = True
                    if decision is None:
                        decision = await _decide_after_deterministic(
                            self._turn_executor,
                            work=work,
                            deterministic_checked=deterministic_checked,
                        )
                    deadline_guarded = _needs_deadline_headroom_stop(
                        work=work,
                        decision=decision,
                    )
                    if deadline_guarded:
                        decision = _deadline_headroom_terminal(work)
                    if isinstance(decision, ExplorationTerminal):
                        if not deadline_guarded:
                            decision, assigned_item = await self._restore_pending_frontier(
                                work=work,
                                agent_run_id=agent_run_id,
                                proposed_terminal=decision,
                            )
                        fairness_applied = isinstance(decision, ActionIntent)
                        if fairness_applied and branch_terminal is not None:
                            work = resume_app_frontier_branch(work, decision)
                    elif _should_yield_to_global_frontier(
                        work=work,
                        decision=decision,
                    ):
                        fair_decision, assigned_item = await self._schedule_pending_frontier_once(
                            work=work,
                            agent_run_id=agent_run_id,
                        )
                        if fair_decision is not None:
                            decision = fair_decision
                            fairness_applied = True
                    if (
                        isinstance(decision, ActionIntent)
                        and not fairness_applied
                        and decision.parameters.get("app_frontier_item_id") is None
                        and decision.parameters.get("app_frontier_inject_candidate") is None
                        and decision.parameters.get("bottom_navigation_tab_seek") is not True
                        and decision.parameters.get("require_runtime_bottom_nav_rebind")
                        is not True
                        and decision.parameters.get("hjll_script") is not True
                        and decision.parameters.get("hlapp_script") is not True
                        and decision.parameters.get("hlwapp_script") is not True
                        and decision.parameters.get("product_script_orchestrated") is not True
                        and decision.parameters.get("tangguo_flow_driver") is not True
                    ):
                        # 模型选中当前帧不可执行候选时，优先改派持久化底栏 sibling，
                        # 避免派发闸门 BOUNDED → WORKFLOW_CANCELED 未巡栏。
                        candidate_id = decision.parameters.get("candidate_id")
                        selected = next(
                            (
                                item
                                for item in work.frontier.candidates
                                if item.candidate_id == candidate_id
                            ),
                            None,
                        )
                        if selected is None or not is_app_candidate_eligible(work, selected):
                            fair_decision, assigned_item = (
                                await self._schedule_pending_frontier_once(
                                    work=work,
                                    agent_run_id=agent_run_id,
                                )
                            )
                            if fair_decision is not None:
                                decision = fair_decision
                                fairness_applied = True
                    if isinstance(decision, ActionIntent) and not fairness_applied:
                        try:
                            selected = _selected_candidate_for_decision(work, decision)
                        except ValueError:
                            # 合成候选漏重建时改派 sibling，禁止整轮 FAILED 卡死首页。
                            fair_decision, assigned_item = (
                                await self._schedule_pending_frontier_once(
                                    work=work,
                                    agent_run_id=agent_run_id,
                                )
                            )
                            if fair_decision is None:
                                raise
                            decision = fair_decision
                            fairness_applied = True
                            selected = _selected_candidate_for_decision(work, decision)
                        selected_frontier_item = (
                            self._global_frontier.registered_item_for_candidate(
                                work=work,
                                candidate=selected,
                                registered=registered,
                            )
                        )
                        if selected_frontier_item is not None:
                            assigned_item = await self._claim_global_frontier_item(
                                work=work,
                                agent_run_id=agent_run_id,
                                frontier_item_id=selected_frontier_item.frontier_item_id,
                            )
                            if assigned_item is None:
                                raise AppExplorationLeaseLostError("App Frontier 当前候选不可领取")
                            selected_frontier_item = None
                    if isinstance(decision, ActionIntent):
                        # 合成 BACK 等：先并入本轮 Frontier，再 bind。
                        if not any(
                            item.candidate_id == decision.parameters.get("candidate_id")
                            for item in work.frontier.candidates
                        ):
                            try:
                                selected = _selected_candidate_for_decision(work, decision)
                            except ValueError:
                                selected = None
                            if selected is not None:
                                work = replace(
                                    work,
                                    frontier=replace(
                                        work.frontier,
                                        candidates=tuple(
                                            (*work.frontier.candidates, selected)
                                        ),
                                    ),
                                )
                        work = bind_app_frontier_restoration_candidate(
                            work=work,
                            decision=decision,
                        )
                    if assigned_item is not None:
                        frontier_lease_guard = self._frontier_lease_guard(assigned_item)
                        await frontier_lease_guard.start()
                    execution = run_app_exploration_operation(
                        self._turn_executor.execute_decision(
                            work=work,
                            agent_run_id=agent_run_id,
                            decision=decision,
                        ),
                        cancellation=self._cancellation,
                        task_id=task_id,
                        agent_run_id=agent_run_id,
                        poll_interval_seconds=self._cancellation_poll_interval_seconds,
                    )
                    if frontier_lease_guard is not None:
                        execution = frontier_lease_guard.run(execution)
                    turn = await lease_guard.run(execution)
                else:
                    recovered_decision = (
                        await _recover_turn_decision(self._turn_executor, work=work)
                        if isinstance(work, ExplorationWork)
                        else None
                    )
                    if isinstance(work, ExplorationWork):
                        recovered_decision = _prefer_current_goal_terminal(
                            work=work,
                            recovered_decision=recovered_decision,
                        )
                    operation = (
                        self._turn_executor.execute_decision(
                            work=work,
                            agent_run_id=agent_run_id,
                            decision=recovered_decision,
                        )
                        if recovered_decision is not None
                        else self._turn_executor.execute(
                            work=work,
                            agent_run_id=agent_run_id,
                        )
                    )
                    turn = await lease_guard.run(
                        run_app_exploration_operation(
                            operation,
                            cancellation=self._cancellation,
                            task_id=task_id,
                            agent_run_id=agent_run_id,
                            poll_interval_seconds=self._cancellation_poll_interval_seconds,
                        )
                    )
                await lease_guard.assert_healthy()
                package_boundary_terminal = _package_boundary_terminal(
                    work=work,
                    turn=turn,
                )
                if package_boundary_terminal is not None:
                    # 把动作轮的决策替换成显式终态，外层 Run/结果投影才能识别
                    # evidence_insufficient；coordination 仍保留动作及 after Capture 审计。
                    turn = replace(turn, decision=package_boundary_terminal)
                turn = _with_terminal_action_count(work=work, turn=turn)
                target_status, terminal = _successful_target(turn)
                result_capture_id = (
                    turn.coordination.after_capture.capture_id
                    if turn.coordination is not None
                    else (
                        turn.observation_capture.capture_id
                        if turn.observation_capture is not None
                        else None
                    )
                )
                successor_work_id = None
                # Probe 为单次补采：即使动作成功也不发布探索后继 Work。
                # Work deadline 已过时不得再 commit_successor（会抛 PersistenceError），
                # 改为确定性预算耗尽终态，避免 APP_EXPLORATION_TURN_FAILED 空转。
                deadline_passed = isinstance(
                    work, ExplorationWork
                ) and work.deadline <= datetime.now(UTC)
                if (
                    deadline_passed
                    and terminal is None
                    and not turn.is_terminal
                    and not isinstance(lease.work, ProbeWork)
                ):
                    evaluation = AppExplorationTerminalEvaluator(
                        now=lambda: datetime.now(UTC)
                    ).evaluate(
                        work,
                        eligible_candidate_count=_eligible_candidate_count(work),
                    )
                    if evaluation.terminal is None:
                        evaluation = AppExplorationTerminalEvaluator(
                            now=lambda: datetime.now(UTC)
                        ).evaluate(
                            work,
                            eligible_candidate_count=0,
                            proposed_terminal=ExplorationTerminal(
                                work_id=work.work_id,
                                task_id=work.task_id,
                                status=ExplorationTerminalStatus.BUDGET_EXHAUSTED,
                                reason="Explorer Work deadline 已到，停止提交后继。",
                            ),
                        )
                    if evaluation.terminal is None:
                        raise RuntimeError("deadline 已过但未能形成探索终态")
                    turn = replace(turn, decision=evaluation.terminal)
                    turn = _with_terminal_action_count(work=work, turn=turn)
                    target_status, terminal = _successful_target(turn)
                if (
                    terminal is None
                    and not turn.is_terminal
                    and not isinstance(lease.work, ProbeWork)
                ):
                    if isinstance(turn.decision, TimedObservationIntent):
                        continuation_call = self._continuation.prepare_after_observation(
                            ContinueAppScreenshotObservationCommand(
                                prior_work=work,
                                turn=turn,
                            )
                        )
                    else:
                        continuation_call = self._continuation.prepare_after_action(
                            ContinueAppScreenshotExplorationCommand(
                                prior_work=work,
                                turn=turn,
                            )
                        )
                    successor = await lease_guard.run(
                        run_app_exploration_operation(
                            (
                                frontier_lease_guard.run(continuation_call)
                                if frontier_lease_guard is not None
                                else continuation_call
                            ),
                            cancellation=self._cancellation,
                            task_id=task_id,
                            agent_run_id=agent_run_id,
                            poll_interval_seconds=self._cancellation_poll_interval_seconds,
                        )
                    )
                    await lease_guard.assert_healthy()
                    _validate_successor(
                        prior_work=work,
                        turn=turn,
                        successor=successor,
                    )
                    await self._raise_if_canceled(
                        task_id=task_id,
                        agent_run_id=agent_run_id,
                    )
                    if self._global_frontier is not None:
                        successor = AppScreenshotExplorationResult(
                            observation_id=successor.observation_id,
                            visual_created=successor.visual_created,
                            page_type_assessment=successor.page_type_assessment,
                            work=await self._global_frontier.project_counts(
                                work=successor.work,
                                agent_run_id=agent_run_id,
                            ),
                        )
                    if frontier_lease_guard is not None:
                        await frontier_lease_guard.assert_healthy()
                    try:
                        committed = await self._queue.commit_successor(
                            command=CommitExplorerWorkSuccessorCommand(
                                parent_work_id=lease.work.work_id,
                                task_id=work.task_id,
                                session_id=work.session_id,
                                lease_owner=self._lease_owner,
                                lease_token=lease.lease_token,
                                row_version=running_version,
                                successor=successor.work,
                                app_frontier_settlement=_frontier_settlement(
                                    work=work,
                                    agent_run_id=agent_run_id,
                                    assigned_item=assigned_item,
                                    selected_frontier_item=selected_frontier_item,
                                    result_capture_id=result_capture_id,
                                    turn=turn,
                                    successor=successor.work,
                                ),
                            )
                        )
                    except ExplorerWorkDeadlineExceededError:
                        await lease_guard.assert_healthy()
                        if frontier_lease_guard is not None:
                            await frontier_lease_guard.assert_healthy()
                        await self._raise_if_canceled(
                            task_id=task_id,
                            agent_run_id=agent_run_id,
                        )
                        evaluation = AppExplorationTerminalEvaluator(
                            now=lambda: datetime.now(UTC)
                        ).evaluate(
                            successor.work,
                            eligible_candidate_count=0,
                            proposed_terminal=ExplorationTerminal(
                                work_id=work.work_id,
                                task_id=work.task_id,
                                status=ExplorationTerminalStatus.BUDGET_EXHAUSTED,
                                reason="后继 Explorer Work 已超过 deadline，停止空转。",
                            ),
                        )
                        if evaluation.terminal is None:
                            raise
                        turn = replace(
                            turn,
                            decision=replace(evaluation.terminal, work_id=work.work_id),
                        )
                        turn = _with_terminal_action_count(
                            work=work,
                            turn=turn,
                            settled_work=successor.work,
                        )
                        target_status, terminal = _successful_target(turn)
                        await self._queue.transition(
                            command=TransitionExplorerWorkCommand(
                                work_id=lease.work.work_id,
                                expected_status=ExplorerWorkStatus.RUNNING,
                                target_status=target_status,
                                lease_token=lease.lease_token,
                                row_version=running_version,
                                terminal=terminal,
                                app_frontier_settlement=_frontier_settlement(
                                    work=work,
                                    agent_run_id=agent_run_id,
                                    assigned_item=assigned_item,
                                    selected_frontier_item=selected_frontier_item,
                                    result_capture_id=result_capture_id,
                                    turn=turn,
                                    successor=successor.work,
                                ),
                            )
                        )
                        await _stop_frontier_guard(frontier_lease_guard)
                        frontier_lease_guard = None
                    else:
                        await _stop_frontier_guard(frontier_lease_guard)
                        frontier_lease_guard = None
                        successor_work_id = committed.successor_work_id
                else:
                    await self._raise_if_canceled(
                        task_id=task_id,
                        agent_run_id=agent_run_id,
                    )
                    if frontier_lease_guard is not None:
                        await frontier_lease_guard.assert_healthy()
                    await self._queue.transition(
                        command=TransitionExplorerWorkCommand(
                            work_id=lease.work.work_id,
                            expected_status=ExplorerWorkStatus.RUNNING,
                            target_status=target_status,
                            lease_token=lease.lease_token,
                            row_version=running_version,
                            terminal=terminal,
                            error_code=(
                                turn.coordination.interaction.error_code
                                if package_boundary_terminal is not None
                                and turn.coordination is not None
                                else None
                            ),
                            error_message=(
                                turn.coordination.interaction.error_message
                                if package_boundary_terminal is not None
                                and turn.coordination is not None
                                else None
                            ),
                            app_frontier_settlement=_frontier_settlement(
                                work=work,
                                agent_run_id=agent_run_id,
                                assigned_item=assigned_item,
                                selected_frontier_item=selected_frontier_item,
                                result_capture_id=result_capture_id,
                                turn=turn,
                            ),
                        )
                    )
                    await _stop_frontier_guard(frontier_lease_guard)
                    frontier_lease_guard = None
                return AppExplorationTurnOutcome(
                    claimed=True,
                    work_id=lease.work.work_id,
                    final_status=target_status,
                    turn=turn,
                    result_capture_id=result_capture_id,
                    successor_work_id=successor_work_id,
                    error_code=(
                        turn.coordination.interaction.error_code
                        if package_boundary_terminal is not None and turn.coordination is not None
                        else None
                    ),
                    evidence_request_id=(
                        lease.work.evidence_request.request_id
                        if isinstance(lease.work, ProbeWork)
                        else None
                    ),
                    source_capture_id=getattr(lease.work, "source_capture_id", None),
                    work_type=work_type.value,
                    restoration_plan=(
                        lease.work.restoration_plan if isinstance(lease.work, ProbeWork) else None
                    ),
                    restoration_attempt_id=(
                        lease.work.restoration_attempt_id
                        if isinstance(lease.work, ProbeWork)
                        else None
                    ),
                    equivalence_assessment_id=(
                        lease.work.equivalence_assessment_id
                        if isinstance(lease.work, ProbeWork)
                        else None
                    ),
                    source_grade=(
                        lease.work.source_grade if isinstance(lease.work, ProbeWork) else None
                    ),
                )
        except AppExplorationCanceledError:
            await self.cancel_nonterminal(task_id=task_id)
            await _stop_frontier_guard(frontier_lease_guard)
            return AppExplorationTurnOutcome(
                claimed=True,
                work_id=lease.work.work_id,
                final_status=ExplorerWorkStatus.CANCELLED,
                error_code=AppExplorationCanceledError.error_code,
            )
        except AppExplorationLeaseLostError:
            await _stop_frontier_guard(frontier_lease_guard)
            raise
        except Exception as exc:  # noqa: BLE001 - 必须先归还 DB 工作租约
            import logging

            logging.getLogger(__name__).exception(
                "App Explorer 单轮执行失败 work_id=%s task_id=%s err=%s: %s",
                lease.work.work_id,
                lease.work.task_id,
                type(exc).__name__,
                exc,
            )
            retryable = _is_retryable(exc)
            target_status = (
                ExplorerWorkStatus.RETRY_WAIT if retryable else ExplorerWorkStatus.FAILED
            )
            error_code = _safe_error_code(exc)
            # Frontier 暂不可领是等待租约／重试事实，不是本条 Work 的投递失败；
            # 必须重置 attempt，否则 3 次 Deferred 会误判为 EXPLORER_WORK_ATTEMPTS_EXHAUSTED。
            frontier_deferred = isinstance(exc, AppFrontierDeferredError)
            if frontier_lease_guard is not None:
                await frontier_lease_guard.assert_healthy()
            await self._queue.transition(
                command=TransitionExplorerWorkCommand(
                    work_id=lease.work.work_id,
                    expected_status=ExplorerWorkStatus.RUNNING,
                    target_status=target_status,
                    lease_token=lease.lease_token,
                    row_version=running_version,
                    error_code=error_code,
                    error_message=f"App Explorer 单轮执行失败: {type(exc).__name__}: {exc}"[:500],
                    retry_delay_seconds=(self._retry_delay_seconds if retryable else 0),
                    reset_attempt_count=frontier_deferred and retryable,
                    app_frontier_settlement=_frontier_settlement(
                        work=lease.work,
                        agent_run_id=agent_run_id,
                        assigned_item=assigned_item,
                        selected_frontier_item=None,
                        target_status=(
                            AppFrontierStatus.RETRY_WAIT if retryable else AppFrontierStatus.FAILED
                        ),
                        result_capture_id=None,
                        error_code=error_code,
                        error_message=str(exc)[:500],
                        retry_delay_seconds=(self._retry_delay_seconds if retryable else 0),
                    ),
                )
            )
            await _stop_frontier_guard(frontier_lease_guard)
            return AppExplorationTurnOutcome(
                claimed=True,
                work_id=lease.work.work_id,
                final_status=target_status,
                error_code=error_code,
            )

    async def next_claim_at(
        self,
        *,
        task_id: str,
        work_type: ExplorerWorkType = ExplorerWorkType.EXPLORATION,
    ) -> datetime | None:
        return await self._queue.next_claim_at(task_id=task_id, work_type=work_type)

    async def expire_unclaimable(self, *, task_id: str) -> int:
        expired = int(await self._queue.expire_unclaimable(task_id=task_id))
        if self._frontier_queue is not None:
            expired += int(await self._frontier_queue.expire_unclaimable(task_id=task_id))
        return expired

    async def cancel_nonterminal(self, *, task_id: str) -> int:
        cancel_app = getattr(self._queue, "cancel_app_exploration_nonterminal", None)
        if callable(cancel_app) and self._frontier_queue is not None:
            return int(
                await cancel_app(
                    task_id=task_id,
                    reason="App 探索 Run 已终止，取消剩余未执行工作",
                )
            )
        cancel = getattr(self._queue, "cancel_nonterminal", None)
        canceled = 0
        if callable(cancel):
            canceled += int(
                await cancel(
                    task_id=task_id,
                    reason="App 探索 Run 已终止，取消剩余未执行工作",
                )
            )
        if self._frontier_queue is not None:
            canceled += int(
                await self._frontier_queue.cancel_nonterminal(
                    task_id=task_id,
                    agent_run_id=None,
                    reason="App 探索 Run 已终止，取消剩余未执行工作",
                )
            )
        return canceled

    async def _restore_pending_frontier(
        self,
        *,
        work: ExplorationWork,
        agent_run_id: str,
        proposed_terminal: ExplorationTerminal,
    ):
        if self._global_frontier is None:
            return proposed_terminal, None
        if (
            "tangguo_flow_state" in proposed_terminal.coverage_summary
            or proposed_terminal.reason == "app_state_machine_decision_missing_or_invalid"
        ):
            # 状态机的结束／阻塞不能被全局候选改写成新动作，避免回首页后再次进片或 BACK。
            return proposed_terminal, None
        if _is_goal_completion_terminal(proposed_terminal):
            return proposed_terminal, None
        from services.api.app.application.app_hjll_script import HJLL_SCRIPT_REASON_CODE
        from services.api.app.application.app_hlapp_script import (
            HLAPP_SCRIPT_BLOCKED_CODE,
            HLAPP_SCRIPT_REASON_CODE,
        )
        from services.api.app.application.app_hlwapp_script import (
            HLWAPP_SCRIPT_BLOCKED_CODE,
            HLWAPP_SCRIPT_REASON_CODE,
        )
        from services.api.app.application.app_jinguo_script import (
            JINGUO_SCRIPT_BLOCKED_CODE,
            JINGUO_SCRIPT_REASON_CODE,
        )
        from services.api.app.application.app_jmcomic_script import (
            JMCOMIC_SCRIPT_BLOCKED_CODE,
            JMCOMIC_SCRIPT_REASON_CODE,
        )
        from services.api.app.application.app_mama_script import (
            MAMA_SCRIPT_BLOCKED_CODE,
            MAMA_SCRIPT_REASON_CODE,
        )

        if proposed_terminal.reason_code in {
            HJLL_SCRIPT_REASON_CODE,
            HLAPP_SCRIPT_REASON_CODE,
            HLAPP_SCRIPT_BLOCKED_CODE,
            HLWAPP_SCRIPT_REASON_CODE,
            HLWAPP_SCRIPT_BLOCKED_CODE,
            JINGUO_SCRIPT_REASON_CODE,
            JINGUO_SCRIPT_BLOCKED_CODE,
            MAMA_SCRIPT_REASON_CODE,
            MAMA_SCRIPT_BLOCKED_CODE,
            JMCOMIC_SCRIPT_REASON_CODE,
            JMCOMIC_SCRIPT_BLOCKED_CODE,
        }:
            # 禁漫脚本最后一步就是「我的」。走完后不再改点未访底栏。
            # 规则近 3 分钟还有更新时，收口门禁自己会等停更。
            return proposed_terminal, None
        # 局部分支停滞可恢复 sibling；原 Run 预算耗尽后不得因此重开动作窗口。
        evaluation = AppExplorationTerminalEvaluator(now=lambda: datetime.now(UTC)).evaluate(
            work, eligible_candidate_count=_eligible_candidate_count(work)
        )
        if evaluation.coverage.budget_exhaustion_reason is not None:
            return evaluation.terminal or proposed_terminal, None
        if proposed_terminal.status is ExplorationTerminalStatus.BUDGET_EXHAUSTED:
            return proposed_terminal, None
        while True:
            pending = await self._global_frontier.next_pending(
                work=work,
                agent_run_id=agent_run_id,
            )
            if pending is None:
                # scheduler stop_reason／额度过滤会把仍可恢复的 sibling 滤光；
                # restore 路径必须再看一次原始 pending，否则会空转 Deferred 烧死 Work。
                pending = await self._global_frontier.next_pending_for_restore(
                    work=work,
                    agent_run_id=agent_run_id,
                )
            if pending is None:
                counts = await self._global_frontier.counts(
                    work=work,
                    agent_run_id=agent_run_id,
                )
                if counts.exhausted:
                    from services.api.app.agents.explorer.maf_agent import (
                        _deterministic_app_coverage_decision,
                        _eligible_candidates,
                    )

                    # 弹窗流程未满足：禁止因 pending=0 排空收口；强制关层或观察重采。
                    from services.api.app.application.app_exploration_flow_contract import (
                        popup_phase_blocks_frontier_exhaustion,
                    )

                    if (
                        popup_phase_blocks_frontier_exhaustion(
                            work.frontier.observation.metadata,
                        )
                        and work.budget.remaining_actions > 0
                    ):
                        eligible, _ = _eligible_candidates(work)
                        forced = _deterministic_app_coverage_decision(
                            work=work,
                            eligible=eligible,
                        )
                        if forced is not None:
                            return forced, None
                        # 确定性路径空：合成清障观察，禁止 Deferred 空转烧死 Work（197bac3c）。
                        from services.api.app.agents.explorer.maf_agent import (
                            synthetic_popup_clearance_observe_intent,
                        )

                        return (
                            synthetic_popup_clearance_observe_intent(work=work),
                            None,
                        )
                    # 上方分类未完：sibling 空时仍强制当前帧点最新→…，禁止提前收口。
                    from services.api.app.application.app_coverage_page_seek import (
                        home_top_category_tabs_incomplete,
                        playbook_bottom_tabs_hard_incomplete,
                    )

                    meta_now = work.frontier.observation.metadata
                    pkg = (
                        meta_now.get("package_name")
                        if isinstance(meta_now, Mapping)
                        else None
                    )
                    package_name = (
                        pkg.strip() if isinstance(pkg, str) and pkg.strip() else None
                    )
                    if package_name is None and work.app_goal is not None:
                        allowed = getattr(work.app_goal, "allowed_packages", ()) or ()
                        if allowed:
                            package_name = str(allowed[0]).strip() or None
                    if (
                        home_top_category_tabs_incomplete(
                            coverage_metadata=(
                                meta_now if isinstance(meta_now, Mapping) else None
                            ),
                            package_name=package_name,
                        )
                        and work.budget.remaining_actions > 0
                    ):
                        eligible, _ = _eligible_candidates(work)
                        forced = _deterministic_app_coverage_decision(
                            work=work,
                            eligible=eligible,
                        )
                        if forced is not None:
                            return forced, None
                    # 底栏硬覆盖未完：sibling 全终态时仍强制当前帧寻栏，禁止提前收口卡主页。
                    if (
                        playbook_bottom_tabs_hard_incomplete(
                            coverage_metadata=work.frontier.observation.metadata,
                        )
                        and work.budget.remaining_actions > 0
                    ):
                        eligible, _ = _eligible_candidates(work)
                        forced = _deterministic_app_coverage_decision(
                            work=work,
                            eligible=eligible,
                        )
                        if forced is not None:
                            return forced, None
                    return _evaluate_exhausted_frontier_terminal(
                        proposed_terminal,
                        work=work,
                        counts=counts,
                    ), None
                raise AppFrontierDeferredError("App Frontier 仍有租约或重试等待项")
            if await self._supersede_redundant_pending(
                work=work,
                agent_run_id=agent_run_id,
                pending=pending,
            ):
                continue
            try:
                decision = build_app_frontier_restore_intent(work=work, item=pending)
            except AppFrontierRestorationError as exc:
                lease = await self._claim_global_frontier_item(
                    work=work,
                    agent_run_id=agent_run_id,
                    frontier_item_id=pending.frontier_item_id,
                )
                if lease is None:
                    raise AppExplorationLeaseLostError("App Frontier 阻断项不可领取") from exc
                target_status = _frontier_restoration_target_status(
                    exc,
                    attempt_count=lease.attempt_count,
                    bottom_nav=_pending_is_bottom_nav(pending),
                )
                await self._transition_frontier_item(
                    lease,
                    target_status=target_status,
                    error_code=exc.error_code,
                    error_message=str(exc),
                    retry_delay_seconds=(
                        max(1, self._retry_delay_seconds)
                        if target_status is AppFrontierStatus.RETRY_WAIT
                        else 0
                    ),
                )
                continue
            if decision.parameters.get("app_frontier_restore_only") is True:
                if await self._supersede_exhausted_restore_pending(
                    work=work,
                    agent_run_id=agent_run_id,
                    pending=pending,
                ):
                    continue
                return decision, None
            lease = await self._claim_global_frontier_item(
                work=work,
                agent_run_id=agent_run_id,
                frontier_item_id=pending.frontier_item_id,
            )
            if lease is None:
                raise AppExplorationLeaseLostError("App Frontier 指派项不可领取")
            return decision, lease

    async def _schedule_pending_frontier_once(
        self,
        *,
        work: ExplorationWork,
        agent_run_id: str,
        missing_goal_only: bool = False,
    ):
        """稳定页停滞时只让渡一个全局分支，避免单轮批量阻断 sibling。"""

        if self._global_frontier is None:
            return None, None
        while True:
            pending = await (
                self._global_frontier.next_missing_goal_pending(
                    work=work,
                    agent_run_id=agent_run_id,
                )
                if missing_goal_only
                else self._global_frontier.next_pending(
                    work=work,
                    agent_run_id=agent_run_id,
                )
            )
            if pending is None:
                return None, None
            if await self._supersede_redundant_pending(
                work=work,
                agent_run_id=agent_run_id,
                pending=pending,
            ):
                continue
            # 当前帧已有底栏时让回当前候选决策；同语义待领取项仍供后续指纹绑定，
            # 不能在本轮登记后将其终结，否则当前候选随后的领取必然失败。
            if _should_skip_stale_bottom_nav_pending(work=work, pending=pending):
                return None, None
            try:
                decision = build_app_frontier_restore_intent(work=work, item=pending)
            except AppFrontierRestorationError as exc:
                lease = await self._claim_global_frontier_item(
                    work=work,
                    agent_run_id=agent_run_id,
                    frontier_item_id=pending.frontier_item_id,
                )
                if lease is None:
                    raise AppExplorationLeaseLostError(
                        "App Frontier 公平调度阻断项不可领取"
                    ) from exc
                target_status = _frontier_restoration_target_status(
                    exc,
                    attempt_count=lease.attempt_count,
                    bottom_nav=_pending_is_bottom_nav(pending),
                )
                await self._transition_frontier_item(
                    lease,
                    target_status=target_status,
                    error_code=exc.error_code,
                    error_message=str(exc),
                    retry_delay_seconds=(
                        max(1, self._retry_delay_seconds)
                        if target_status is AppFrontierStatus.RETRY_WAIT
                        else 0
                    ),
                )
                return None, None
            if decision.parameters.get("app_frontier_restore_only") is True:
                if await self._supersede_exhausted_restore_pending(
                    work=work,
                    agent_run_id=agent_run_id,
                    pending=pending,
                ):
                    continue
                return decision, None
            lease = await self._claim_global_frontier_item(
                work=work,
                agent_run_id=agent_run_id,
                frontier_item_id=pending.frontier_item_id,
            )
            if lease is None:
                raise AppExplorationLeaseLostError("App Frontier 公平调度项不可领取")
            return decision, lease

    async def _supersede_exhausted_restore_pending(
        self,
        *,
        work: ExplorationWork,
        agent_run_id: str,
        pending: AppFrontierItem,
    ) -> bool:
        """restore_only 超过上限时收口该 sibling，避免 BACK／锚点无限空转。"""

        if not app_frontier_restore_attempts_exhausted(
            work.frontier.observation.metadata,
            frontier_item_id=pending.frontier_item_id,
            bottom_nav=_pending_is_bottom_nav(pending),
        ):
            return False
        lease = await self._claim_global_frontier_item(
            work=work,
            agent_run_id=agent_run_id,
            frontier_item_id=pending.frontier_item_id,
        )
        if lease is None:
            raise AppExplorationLeaseLostError("App Frontier 恢复耗尽项不可领取")
        await self._transition_frontier_item(
            lease,
            target_status=AppFrontierStatus.SUPERSEDED,
            error_code="APP_FRONTIER_RESTORE_EXHAUSTED",
            error_message="同一 Frontier 候选的来源恢复次数已耗尽，跳过重复恢复。",
        )
        return True

    async def _supersede_redundant_pending(
        self,
        *,
        work: ExplorationWork,
        agent_run_id: str,
        pending: AppFrontierItem,
    ) -> bool:
        """已在来源页成功执行的同义候选直接收口，不再触发设备和证据采样。"""

        if self._global_frontier is None or not (
            self._global_frontier.pending_item_is_redundant(
                work=work,
                item=pending,
            )
        ):
            return False
        lease = await self._claim_global_frontier_item(
            work=work,
            agent_run_id=agent_run_id,
            frontier_item_id=pending.frontier_item_id,
        )
        if lease is None:
            raise AppExplorationLeaseLostError("App Frontier 重复候选不可领取")
        await self._transition_frontier_item(
            lease,
            target_status=AppFrontierStatus.SUPERSEDED,
            error_code="APP_FRONTIER_REDUNDANT_PAGE",
            error_message="同一来源页的同义动作已成功执行，跳过重复证据采样。",
        )
        return True

    async def _raise_if_canceled(self, *, task_id: str, agent_run_id: str) -> None:
        await raise_if_app_exploration_canceled(
            self._cancellation,
            task_id=task_id,
            agent_run_id=agent_run_id,
        )

    async def _claim_global_frontier_item(
        self,
        *,
        work: ExplorationWork,
        agent_run_id: str,
        frontier_item_id: str,
    ):
        if self._frontier_queue is None:
            return None
        lease_token = str(uuid4())
        lease = await self._frontier_queue.claim(
            command=ClaimAppFrontierCommand(
                task_id=work.task_id,
                agent_run_id=agent_run_id,
                session_id=work.session_id,
                lease_owner=self._lease_owner,
                lease_seconds=self._lease_seconds,
                frontier_item_id=frontier_item_id,
            ),
            lease_token=lease_token,
        )
        if lease is None or lease.item.frontier_item_id != frontier_item_id:
            return None
        running_version = await self._frontier_queue.transition(
            command=TransitionAppFrontierCommand(
                frontier_item_id=frontier_item_id,
                expected_status=AppFrontierStatus.LEASED,
                target_status=AppFrontierStatus.RUNNING,
                lease_token=lease.lease_token,
                row_version=lease.row_version,
            )
        )
        observe_app_metric_safely(
            APP_FRONTIER_CLAIM_AGE_SECONDS,
            max(0.0, (datetime.now(UTC) - lease.item.discovered_at).total_seconds()),
            surface_kind=lease.item.surface_kind.value,
        )
        return replace(lease, status=AppFrontierStatus.RUNNING, row_version=running_version)

    async def _transition_frontier_item(
        self,
        lease,
        *,
        target_status: AppFrontierStatus,
        result_work_id: str | None = None,
        result_capture_id: str | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
        retry_delay_seconds: int = 0,
    ) -> None:
        if self._frontier_queue is None:
            return
        await self._frontier_queue.transition(
            command=TransitionAppFrontierCommand(
                frontier_item_id=lease.item.frontier_item_id,
                expected_status=AppFrontierStatus.RUNNING,
                target_status=target_status,
                lease_token=lease.lease_token,
                row_version=lease.row_version,
                result_work_id=result_work_id,
                result_capture_id=result_capture_id,
                error_code=error_code,
                error_message=error_message,
                retry_delay_seconds=retry_delay_seconds,
            )
        )

    def _frontier_lease_guard(self, assigned_item) -> AppFrontierLeaseGuard:
        if self._frontier_queue is None:
            raise AppExplorationLeaseLostError("App Frontier 缺少续租仓储")
        return AppFrontierLeaseGuard(
            queue=self._frontier_queue,
            command=RenewAppFrontierLeaseCommand(
                frontier_item_id=assigned_item.item.frontier_item_id,
                lease_owner=self._lease_owner,
                lease_token=assigned_item.lease_token,
                lease_seconds=self._lease_seconds,
            ),
            renew_interval_seconds=max(1, self._lease_seconds // 3),
        )


def _validate_app_work(work) -> None:
    if not isinstance(work, (ExplorationWork, ProbeWork)):
        raise ValueError("App Runner 只消费 Exploration/Probe Work")
    if work.surface_kind not in {SurfaceKind.ANDROID, SurfaceKind.IOS}:
        raise ValueError("App Runner 不消费 Web Exploration Work")


def _should_yield_to_global_frontier(
    *,
    work: ExplorationWork,
    decision: ActionIntent | TimedObservationIntent,
) -> bool:
    """稳定业务页连续无新增覆盖时，让持久化 sibling 获得有界执行机会。"""

    if isinstance(decision, TimedObservationIntent):
        return False
    if (
        decision.parameters.get("hjll_script") is True
        or decision.parameters.get("hlapp_script") is True
        or decision.parameters.get("hlwapp_script") is True
        or decision.parameters.get("product_script_orchestrated") is True
        or decision.parameters.get("tangguo_flow_driver") is True
    ):
        return False
    if decision.parameters.get("surface_phase_key") is not None:
        return False
    observation = work.frontier.observation
    from services.api.app.application.app_coverage_page_seek import (
        coverage_incomplete_allows_bottom_nav_through_overlay,
        overlay_budget_allows_real_coverage_seek,
    )

    bottom_nav_incomplete = coverage_incomplete_allows_bottom_nav_through_overlay(
        coverage_metadata=observation.metadata,
    )
    if observation.page_state != "stable":
        # 弹层未满帧：禁止 pending 底栏抢走关层；否则会空点底栏假 completed。
        if not bottom_nav_incomplete:
            return False
        if not overlay_budget_allows_real_coverage_seek(
            page_type=observation.page_type,
            coverage_metadata=observation.metadata,
        ):
            return False
    if decision.parameters.get("bottom_navigation_tab_seek") is True:
        return False
    # 当前帧关层／跳过不得被全局底栏 sibling 抢占。
    # 注意：不得调用会抛错的 _selected_candidate_for_decision（合成 BACK 等可能暂不在 Frontier）。
    candidate_id = decision.parameters.get("candidate_id")
    selected = next(
        (
            candidate
            for candidate in work.frontier.candidates
            if candidate.candidate_id == candidate_id
        ),
        None,
    )
    if selected is not None:
        role = str(selected.metadata.get("control_role") or "").casefold()
        if role in {"dismiss", "skip"}:
            return False
        terms = " ".join(
            (
                str(selected.semantic_target or ""),
                str(selected.metadata.get("purpose") or ""),
            )
        ).casefold()
        if any(marker in terms for marker in ("关闭", "跳过", "skip", "close", "dismiss")):
            return False
    elif any(
        marker in str(decision.purpose or "").casefold()
        for marker in ("关闭", "跳过", "开屏", "弹层", "skip", "close")
    ):
        return False
    metadata = observation.metadata
    has_current_frame_tabs = any(
        candidate.metadata.get("bottom_navigation_tab_seek") is True
        for candidate in work.frontier.candidates
    )
    if bottom_nav_incomplete:
        # 当前帧已有底栏：禁止让渡历史 sibling（空水平槽重绑失败空转）。
        if has_current_frame_tabs:
            return False
        # 当前帧无底栏且决策是宫格／缺页：让渡给 pending 底栏 sibling。
        frontier = metadata.get("app_global_frontier")
        if not isinstance(frontier, Mapping):
            return False
        pending = frontier.get("pending")
        return isinstance(pending, int) and not isinstance(pending, bool) and pending > 0
    if _decision_targets_missing_goal_page(work=work, decision=decision):
        # 当前截图已经有直达覆盖缺口的安全动作时，不能再被 sibling 公平轮转抢占。
        return False
    no_new_page_count = metadata.get(
        "dynamic_no_new_business_page_action_count",
        0,
    )
    if (
        not isinstance(no_new_page_count, int)
        or isinstance(no_new_page_count, bool)
        or no_new_page_count < _GLOBAL_FRONTIER_FAIRNESS_NO_NEW_PAGE_LIMIT
    ):
        return False
    frontier = metadata.get("app_global_frontier")
    if not isinstance(frontier, Mapping):
        return False
    pending = frontier.get("pending")
    return isinstance(pending, int) and not isinstance(pending, bool) and pending > 0


def _decision_targets_missing_goal_page(
    *,
    work: ExplorationWork,
    decision: ActionIntent,
) -> bool:
    """识别当前决策是否直达尚未覆盖的必需页型。"""

    if work.app_goal is None:
        return False
    candidate_id = decision.parameters.get("candidate_id")
    candidate = next(
        (item for item in work.frontier.candidates if item.candidate_id == candidate_id),
        None,
    )
    if candidate is None:
        return False
    expected = candidate.metadata.get("expected_next_page_type")
    if not isinstance(expected, str) or not expected.strip():
        return False
    required = {item.casefold() for item in work.app_goal.required_page_types}
    covered = {
        item.casefold()
        for item in work.frontier.observation.metadata.get("covered_page_types", ())
        if isinstance(item, str)
    }
    return expected.strip().casefold() in required - covered


def _needs_deadline_headroom_stop(
    *,
    work: ExplorationWork,
    decision: ActionIntent | ExplorationTerminal | TimedObservationIntent,
) -> bool:
    """设备动作或重观察没有完整采集余量时，不再启动新一轮 I/O。"""

    if isinstance(decision, ExplorationTerminal):
        return False
    remaining_seconds = (work.deadline - datetime.now(UTC)).total_seconds()
    return remaining_seconds < _action_deadline_headroom_seconds(work)


def _action_deadline_headroom_seconds(work: ExplorationWork) -> float:
    """按最近视觉耗时为 after Capture、理解和最终封存预留完整窗口。"""

    metadata = work.frontier.observation.metadata
    facts = metadata.get("surface_facts")
    facts = facts if isinstance(facts, Mapping) else {}
    latency = facts.get("visual_latency")
    latency = latency if isinstance(latency, Mapping) else {}
    raw_elapsed_ms = latency.get("elapsed_ms")
    observed_seconds = 0.0
    if (
        not isinstance(raw_elapsed_ms, bool)
        and isinstance(raw_elapsed_ms, (int, float))
        and raw_elapsed_ms > 0
    ):
        observed_seconds = float(raw_elapsed_ms) / 1000.0
    dynamic_seconds = observed_seconds * 2.0 + _ACTION_DEADLINE_CLOSURE_RESERVE_SECONDS
    return min(
        _ACTION_DEADLINE_MAX_HEADROOM_SECONDS,
        max(_ACTION_DEADLINE_MIN_HEADROOM_SECONDS, dynamic_seconds),
    )


def _deadline_headroom_terminal(work: ExplorationWork) -> ExplorationTerminal:
    """按正式预算终态封存，避免末轮 Capture 超时被放大成技术失败。"""

    evaluation = AppExplorationTerminalEvaluator(now=lambda: work.deadline).evaluate(
        work,
        eligible_candidate_count=_eligible_candidate_count(work),
        proposed_terminal=ExplorationTerminal(
            work_id=work.work_id,
            task_id=work.task_id,
            status=ExplorationTerminalStatus.BUDGET_EXHAUSTED,
            reason="剩余时间不足以完成动作前后采集，按探索预算收口。",
        ),
    )
    if evaluation.terminal is None:
        raise RuntimeError("探索 deadline 已到但未形成预算终态")
    return evaluation.terminal


async def _recover_turn_decision(turn_executor, *, work: ExplorationWork):
    """无论是否启用全局 Frontier，都先恢复同一 Work 已落库的动作意图。"""

    recover_decision = getattr(turn_executor, "recover_decision", None)
    if not callable(recover_decision):
        return None
    return await recover_decision(work=work)


async def _decide_deterministic(turn_executor, *, work: ExplorationWork):
    """兼容旧 Executor；仅在其显式暴露确定性决策时调用。"""

    decide = getattr(turn_executor, "decide_deterministic", None)
    if not callable(decide):
        return None, False
    return await decide(work=work), True


async def _decide_after_deterministic(
    turn_executor,
    *,
    work: ExplorationWork,
    deterministic_checked: bool,
):
    """阶段检查后调用不重复推进阶段机的模型入口。"""

    decide = getattr(turn_executor, "decide_after_deterministic", None)
    if deterministic_checked and callable(decide):
        return await decide(work=work)
    return await turn_executor.decide(work=work)


def _can_schedule_goal_frontier_without_model(work: ExplorationWork) -> bool:
    """稳定无遮挡，或底栏硬覆盖未完时的弹层帧，仍可直达明确覆盖缺口。"""

    if work.app_goal is None:
        return False
    if (work.deadline - datetime.now(UTC)).total_seconds() < (
        _action_deadline_headroom_seconds(work)
    ):
        return False
    observation = work.frontier.observation
    page_type = str(observation.page_type or "").strip().casefold()
    if page_type in {
        "startup_ad",
        "countdown",
        "modal",
        "dialog",
        "overlay",
        "system_dialog",
        "permission_dialog",
        "auth_gate",
        "content_detail",
        "video_detail",
        "post_detail",
        "settings",
        "payment",
    }:
        return False
    metadata = observation.metadata
    from services.api.app.application.app_coverage_page_seek import (
        coverage_incomplete_allows_bottom_nav_through_overlay,
        overlay_budget_allows_real_coverage_seek,
    )

    bottom_nav_incomplete = coverage_incomplete_allows_bottom_nav_through_overlay(
        coverage_metadata=metadata,
    )
    if observation.page_state != "stable":
        # popup：仅满帧让路，或当前帧候选已含可见底栏条带时，才可直达 pending 底栏。
        if not bottom_nav_incomplete:
            return False
        has_visible_bottom = any(
            candidate.metadata.get("bottom_navigation_tab_seek") is True
            and isinstance(candidate.metadata.get("normalized_bounds"), Mapping)
            and float(
                (candidate.metadata.get("normalized_bounds") or {}).get("top") or 0.0
            )
            >= 0.88
            for candidate in work.frontier.candidates
        )
        if not (
            overlay_budget_allows_real_coverage_seek(
                page_type=observation.page_type,
                coverage_metadata=metadata,
            )
            or has_visible_bottom
        ):
            return False
        if page_type not in {"", "home", "popup"} and page_type not in {
            "content_list",
            "video_list",
            "post_list",
            "activity",
            "profile",
        }:
            return False
    facts = metadata.get("surface_facts")
    facts = facts if isinstance(facts, Mapping) else {}
    if bottom_nav_incomplete:
        # 底栏未巡检完时允许弹层帧直达；仍禁止鉴权门。
        return facts.get("requires_auth") is not True and metadata.get("requires_auth") is not True
    return not any(
        metadata.get(key) is True or facts.get(key) is True
        for key in ("popup_visible", "dialog_visible", "requires_auth")
    )


def _discard_stale_candidate_decision(
    *,
    work: ExplorationWork,
    recovered_decision: ActionIntent | ExplorationTerminal | TimedObservationIntent | None,
):
    """全局队列已过滤候选时，不重放上次失败留下的过期动作。"""

    if not isinstance(recovered_decision, ActionIntent):
        return recovered_decision
    candidate_id = recovered_decision.parameters.get("candidate_id")
    if not isinstance(candidate_id, str) or not candidate_id.strip():
        return recovered_decision
    candidate = next(
        (
            candidate
            for candidate in work.frontier.candidates
            if candidate.candidate_id == candidate_id
        ),
        None,
    )
    if candidate is not None and is_app_candidate_eligible(work, candidate):
        return recovered_decision
    return None


def _selected_candidate_for_decision(
    work: ExplorationWork,
    decision: ActionIntent,
):
    candidate_id = decision.parameters.get("candidate_id")
    selected = next(
        (item for item in work.frontier.candidates if item.candidate_id == candidate_id),
        None,
    )
    if selected is not None:
        return selected
    # 合成系统 BACK：可能尚未写入 Frontier 快照，按意图重建，避免整轮 ValueError。
    if (
        isinstance(candidate_id, str)
        and candidate_id.startswith("forced-system-back:")
        and decision.action_type is InteractionOperation.BACK
    ):
        return CandidateAction(
            candidate_id=candidate_id,
            semantic_target=str(decision.semantic_target or "系统 BACK"),
            action_type=InteractionOperation.BACK,
            risk_level=decision.risk_level,
            exploration_value=100,
            navigation_depth=0,
            expected_surface_kind=work.surface_kind,
            metadata={
                "purpose": decision.purpose,
                "frontier_scope": "local",
                "forced_detail_exit_back": True,
                "post_detail_exit_prefer_tabs": True,
            },
        )
    # 限步清障合成 OBSERVE／坐标点击：池空时须重建，禁止 TURN_FAILED→lease 清理退桌面
    # （QC 49f8f035：弹窗未关完报 bounded-dismiss-observe 不属于 Frontier）。
    if (
        decision.parameters.get("bounded_dismiss_tool") is True
        and isinstance(candidate_id, str)
        and (
            candidate_id.startswith("bounded-dismiss-observe:")
            or candidate_id.startswith("bounded-dismiss-coord:")
        )
    ):
        meta: dict[str, object] = {
            "purpose": decision.purpose,
            "frontier_scope": "local",
            "bounded_dismiss_tool": True,
            "bounded_dismiss_constrained": True,
            "require_screenshot_evidence": decision.action_type
            in {InteractionOperation.CLICK, InteractionOperation.TAP},
        }
        for key in (
            "bounded_dismiss_phase",
            "bounded_dismiss_step",
            "bounded_dismiss_max_steps",
            "bounded_dismiss_splash_steps",
            "bounded_dismiss_popup_steps",
            "bounded_dismiss_verdict",
            "expected_state_hint",
            "control_role",
            "forced_popup_clearance_observe",
        ):
            if key in decision.parameters:
                meta[key] = decision.parameters[key]
        bounds = decision.parameters.get("normalized_bounds")
        if isinstance(bounds, Mapping):
            meta["normalized_bounds"] = dict(bounds)
        if candidate_id.startswith("bounded-dismiss-observe:"):
            meta["forced_popup_clearance_observe"] = True
            meta["expected_state_hint"] = str(
                decision.parameters.get("expected_state_hint")
                or "popup_clearance_reobserve"
            )
        return CandidateAction(
            candidate_id=candidate_id,
            semantic_target=str(
                decision.semantic_target
                or (
                    "观察等待清障控件"
                    if candidate_id.startswith("bounded-dismiss-observe:")
                    else "清障坐标"
                )
            ),
            action_type=decision.action_type,
            risk_level=decision.risk_level,
            exploration_value=90
            if candidate_id.startswith("bounded-dismiss-observe:")
            else 95,
            navigation_depth=0,
            expected_surface_kind=work.surface_kind,
            metadata=meta,
        )
    # 糖果／Agent 三合同主流程：合成 tap／back／scroll 不在 Frontier 快照内，
    # 必须重建执行，禁止 ValueError→APP_EXPLORATION_TURN_FAILED→收口 HOME 退桌面
    # （QC f3fded86：弹窗清障后首个 tangguo-flow-tap 整轮失败）。
    if (
        decision.parameters.get("tangguo_flow_driver") is True
        and isinstance(candidate_id, str)
        and candidate_id.startswith("tangguo-flow-")
    ):
        meta: dict[str, object] = {
            "purpose": decision.purpose,
            "frontier_scope": "local",
            "tangguo_flow_driver": True,
            "require_screenshot_evidence": True,
        }
        bounds = decision.parameters.get("normalized_bounds")
        if isinstance(bounds, Mapping):
            meta["normalized_bounds"] = dict(bounds)
        action_parameters = decision.parameters.get("action_parameters")
        if isinstance(action_parameters, Mapping):
            meta["action_parameters"] = dict(action_parameters)
        for key in (
            "home_content_entry_seek",
            "home_top_category_tab_seek",
            "bottom_navigation_tab_seek",
            "tangguo_flow_phase",
            "tangguo_flow_tab",
            "wallet_entry_target",
            "payment_method",
            "inspect_only",
            "expand_more_pay",
            "forced_detail_exit_back",
            "comic_entry_seek",
            "comic_read_scroll",
        ):
            if key in decision.parameters:
                meta[key] = decision.parameters[key]
        return CandidateAction(
            candidate_id=candidate_id,
            semantic_target=str(decision.semantic_target or "糖果主流程步骤"),
            action_type=decision.action_type,
            risk_level=decision.risk_level,
            exploration_value=95,
            navigation_depth=0,
            expected_surface_kind=work.surface_kind,
            metadata=meta,
        )
    # F02：历史合成左上角坐标点击不再放行执行。
    if (
        isinstance(candidate_id, str)
        and candidate_id.startswith("forced-short-drama-top-left-back:")
    ):
        return None
    # 合成弹窗清障观察：队列空时续发关层，候选可能不在 Frontier 快照内。
    if (
        isinstance(candidate_id, str)
        and candidate_id.startswith("forced-popup-clearance-observe:")
        and decision.action_type is InteractionOperation.OBSERVE
    ):
        contract = decision.parameters.get("app_popup_clearance_observe")
        meta = {
            "purpose": (
                str(contract.get("purpose"))
                if isinstance(contract, Mapping) and isinstance(contract.get("purpose"), str)
                else decision.purpose
            ),
            "frontier_scope": "local",
            "expected_state_hint": "popup_clearance_reobserve",
            "forced_popup_clearance_observe": True,
        }
        if isinstance(contract, Mapping):
            meta["popup_clearance_observe_contract"] = dict(contract)
        return CandidateAction(
            candidate_id=candidate_id,
            semantic_target=str(decision.semantic_target or "观察等待弹层关闭控件"),
            action_type=InteractionOperation.OBSERVE,
            risk_level=decision.risk_level,
            exploration_value=90,
            navigation_depth=0,
            expected_surface_kind=work.surface_kind,
            metadata=meta,
        )
    # 无 X 促销：点蒙层空白处关闭。
    if (
        isinstance(candidate_id, str)
        and candidate_id.startswith("forced-popup-scrim-dismiss:")
        and decision.action_type
        in {InteractionOperation.CLICK, InteractionOperation.TAP}
    ):
        contract = decision.parameters.get("app_popup_scrim_dismiss")
        bounds = None
        if isinstance(contract, Mapping):
            bounds = contract.get("normalized_bounds")
        if not isinstance(bounds, Mapping):
            bounds = decision.parameters.get("normalized_bounds")
        meta = {
            "purpose": (
                str(contract.get("purpose"))
                if isinstance(contract, Mapping)
                and isinstance(contract.get("purpose"), str)
                else decision.purpose
            ),
            "control_role": "dismiss",
            "frontier_scope": "local",
            "expected_state_hint": "dismiss_popup_scrim",
            "forced_popup_scrim_dismiss": True,
            "require_screenshot_evidence": True,
        }
        if isinstance(bounds, Mapping):
            meta["normalized_bounds"] = dict(bounds)
        if isinstance(contract, Mapping):
            meta["popup_scrim_dismiss_contract"] = dict(contract)
        return CandidateAction(
            candidate_id=candidate_id,
            semantic_target=str(decision.semantic_target or "点击弹层蒙层空白处关闭"),
            action_type=InteractionOperation.CLICK,
            risk_level=decision.risk_level,
            exploration_value=95,
            navigation_depth=0,
            expected_surface_kind=work.surface_kind,
            metadata=meta,
        )
    # 合成像素圆 X：eligible 漏关层时从 surface_facts 重建，须允许执行。
    if (
        isinstance(candidate_id, str)
        and candidate_id.startswith("forced-pixel-popup-close:")
        and decision.action_type
        in {InteractionOperation.CLICK, InteractionOperation.TAP}
    ):
        contract = decision.parameters.get("app_popup_close_corner")
        bounds = None
        if isinstance(contract, Mapping):
            bounds = contract.get("normalized_bounds")
        if not isinstance(bounds, Mapping):
            bounds = decision.parameters.get("normalized_bounds")
        if not isinstance(bounds, Mapping):
            bounds = decision.parameters.get("bounds")
        meta = {
            "purpose": (
                str(contract.get("purpose"))
                if isinstance(contract, Mapping) and isinstance(contract.get("purpose"), str)
                else decision.purpose
            ),
            "control_role": "dismiss",
            "frontier_scope": "local",
            "expected_state_hint": "close_popup_x",
            "popup_close_pixel_visual": True,
            "popup_close_bottom_x_fallback": True,
            "popup_close_corner_fallback": True,
            "forced_pixel_popup_close": True,
            "app_popup_close_corner": True,
        }
        if isinstance(bounds, Mapping):
            meta["normalized_bounds"] = dict(bounds)
        if isinstance(contract, Mapping):
            meta["popup_close_corner_contract"] = dict(contract)
        return CandidateAction(
            candidate_id=candidate_id,
            semantic_target=str(decision.semantic_target or "关闭弹层下方X"),
            action_type=InteractionOperation.CLICK,
            risk_level=decision.risk_level,
            exploration_value=100,
            navigation_depth=0,
            expected_surface_kind=work.surface_kind,
            metadata=meta,
        )
    # 合成像素底栏下一栏：eligible 漏 seek 时须允许执行。
    if (
        isinstance(candidate_id, str)
        and candidate_id.startswith("forced-pixel-bottom-nav:")
        and decision.action_type
        in {InteractionOperation.CLICK, InteractionOperation.TAP}
    ):
        bounds = decision.parameters.get("normalized_bounds")
        meta = {
            "purpose": decision.purpose,
            "frontier_scope": "app",
            "bottom_navigation_tab_seek": True,
            "coverage_page_seek": True,
            "require_screenshot_evidence": True,
            "bottom_nav_pixel_visual": True,
            "forced_pixel_bottom_nav": True,
            "expected_state_hint": str(
                decision.parameters.get("expected_state_hint") or ""
            )
            or None,
        }
        if isinstance(bounds, Mapping):
            meta["normalized_bounds"] = dict(bounds)
        return CandidateAction(
            candidate_id=candidate_id,
            semantic_target=str(decision.semantic_target or "底栏"),
            action_type=InteractionOperation.CLICK,
            risk_level=decision.risk_level,
            exploration_value=100,
            navigation_depth=0,
            expected_surface_kind=work.surface_kind,
            metadata=meta,
        )
    # 合成首页影片相对第一卡：池空时 deterministic 像素须允许执行
    # （QC 9ced42b8：点完误顶栏后签发 forced-pixel-home-content-entry 整轮 FAILED）。
    if (
        isinstance(candidate_id, str)
        and candidate_id.startswith("forced-pixel-home-content-entry:")
        and decision.action_type
        in {InteractionOperation.CLICK, InteractionOperation.TAP}
    ):
        bounds = decision.parameters.get("normalized_bounds")
        meta = {
            "purpose": decision.purpose,
            "frontier_scope": "local",
            "home_content_entry_seek": True,
            "forced_pixel_home_content_entry": True,
            "require_screenshot_evidence": True,
            "expected_state_hint": str(
                decision.parameters.get("expected_state_hint") or ""
            )
            or "home_content_entry_first_card",
        }
        if isinstance(bounds, Mapping):
            meta["normalized_bounds"] = dict(bounds)
        return CandidateAction(
            candidate_id=candidate_id,
            semantic_target=str(decision.semantic_target or "首页今日新剧第一片"),
            action_type=InteractionOperation.CLICK,
            risk_level=decision.risk_level,
            exploration_value=95,
            navigation_depth=0,
            expected_surface_kind=work.surface_kind,
            metadata=meta,
        )
    # 合成主页上方分类：池空时 deterministic 像素／槽位合成须允许执行。
    if (
        isinstance(candidate_id, str)
        and (
            candidate_id.startswith("forced-home-top-category:")
            or candidate_id.startswith("forced-pixel-home-top-category:")
        )
        and decision.action_type
        in {InteractionOperation.CLICK, InteractionOperation.TAP}
    ):
        bounds = decision.parameters.get("normalized_bounds")
        meta = {
            "purpose": decision.purpose,
            "frontier_scope": "local",
            "home_top_category_tab_seek": True,
            "coverage_page_seek": True,
            "require_screenshot_evidence": True,
            "home_top_category_slot_synthetic": candidate_id.startswith(
                "forced-home-top-category:"
            ),
            "home_top_category_pixel_visual": candidate_id.startswith(
                "forced-pixel-home-top-category:"
            ),
            "forced_pixel_home_top_category": candidate_id.startswith(
                "forced-pixel-home-top-category:"
            ),
            "expected_state_hint": str(
                decision.parameters.get("expected_state_hint") or ""
            )
            or None,
        }
        if isinstance(bounds, Mapping):
            meta["normalized_bounds"] = dict(bounds)
        return CandidateAction(
            candidate_id=candidate_id,
            semantic_target=str(decision.semantic_target or "上方分类"),
            action_type=InteractionOperation.CLICK,
            risk_level=decision.risk_level,
            exploration_value=100,
            navigation_depth=0,
            expected_surface_kind=work.surface_kind,
            metadata=meta,
        )
    # 合成上方分类第一片／3D漫画取证入口：池空时 deterministic 槽位须允许执行。
    if (
        isinstance(candidate_id, str)
        and (
            candidate_id.startswith("forced-home-top-sample-first-card:")
            or candidate_id.startswith("forced-home-top-sample-match:")
            or candidate_id.startswith("forced-pixel-home-top-sample-first-card:")
            or candidate_id.startswith("forced-pixel-home-top-sample-match:")
        )
        and decision.action_type
        in {InteractionOperation.CLICK, InteractionOperation.TAP}
    ):
        bounds = decision.parameters.get("normalized_bounds")
        sample_label = decision.parameters.get("home_top_category_sample_label")
        meta = {
            "purpose": decision.purpose,
            "frontier_scope": "local",
            "home_top_category_sample_seek": True,
            "home_top_category_sample_phase": str(
                decision.parameters.get("home_top_category_sample_phase")
                or "need_first_card"
            ),
            "require_screenshot_evidence": True,
            "home_top_category_sample_first_card_synthetic": (
                "sample-first-card:" in candidate_id
            ),
            "home_top_category_sample_match_synthetic": (
                "sample-match:" in candidate_id
            ),
            "home_top_sample_pixel_visual": candidate_id.startswith(
                "forced-pixel-home-top-sample-"
            )
            or decision.parameters.get("home_top_sample_pixel_visual") is True,
            "forced_pixel_home_top_sample": candidate_id.startswith(
                "forced-pixel-home-top-sample-"
            )
            or decision.parameters.get("forced_pixel_home_top_sample") is True,
            "expected_state_hint": str(
                decision.parameters.get("expected_state_hint") or ""
            )
            or "home_top_sample_first_card",
        }
        if isinstance(sample_label, str) and sample_label.strip():
            meta["home_top_category_sample_label"] = sample_label.strip()
        match_texts = decision.parameters.get("home_top_category_sample_match_texts")
        if isinstance(match_texts, (list, tuple)):
            meta["home_top_category_sample_match_texts"] = [
                str(item).strip() for item in match_texts if str(item or "").strip()
            ]
        if isinstance(bounds, Mapping):
            meta["normalized_bounds"] = dict(bounds)
        return CandidateAction(
            candidate_id=candidate_id,
            semantic_target=str(decision.semantic_target or "列表取证入口"),
            action_type=InteractionOperation.CLICK,
            risk_level=decision.risk_level,
            exploration_value=100,
            navigation_depth=0,
            expected_surface_kind=work.surface_kind,
            metadata=meta,
        )
    # 上方分类条滑离课纲：合成横向滑回，池空时须允许执行。
    if (
        isinstance(candidate_id, str)
        and candidate_id.startswith("forced-home-top-strip-reset:")
        and decision.action_type is InteractionOperation.SCROLL
    ):
        bounds = decision.parameters.get("normalized_bounds")
        action_parameters = decision.parameters.get("action_parameters")
        meta = {
            "purpose": decision.purpose,
            "frontier_scope": "local",
            "home_top_category_strip_reset": True,
            "require_screenshot_evidence": True,
        }
        if isinstance(bounds, Mapping):
            meta["normalized_bounds"] = dict(bounds)
        if isinstance(action_parameters, Mapping):
            meta["action_parameters"] = dict(action_parameters)
        return CandidateAction(
            candidate_id=candidate_id,
            semantic_target=str(decision.semantic_target or "上方分类条滑回最新"),
            action_type=InteractionOperation.SCROLL,
            risk_level=decision.risk_level,
            exploration_value=100,
            navigation_depth=0,
            expected_surface_kind=work.surface_kind,
            metadata=meta,
        )
    if (
        decision.parameters.get("product_script_orchestrated") is True
        and isinstance(candidate_id, str)
        and "-script:" in candidate_id
    ):
        bounds = decision.parameters.get("normalized_bounds")
        meta = {
            "purpose": decision.purpose,
            "frontier_scope": "local",
            "product_script_orchestrated": True,
            "require_screenshot_evidence": True,
            "action_parameters": {
                key: decision.parameters[key]
                for key in (
                    "from",
                    "to",
                    "duration_ms",
                    "product_settle_after_action_ms",
                )
                if key in decision.parameters
            },
        }
        for key, value in decision.parameters.items():
            if key.endswith("_script") and value is True:
                meta[key] = True
        if isinstance(bounds, Mapping):
            meta["normalized_bounds"] = dict(bounds)
        return CandidateAction(
            candidate_id=candidate_id,
            semantic_target=str(decision.semantic_target or "產品腳本步驟"),
            action_type=decision.action_type,
            risk_level=decision.risk_level,
            exploration_value=100,
            navigation_depth=0,
            expected_surface_kind=work.surface_kind,
            metadata=meta,
        )
    if (
        isinstance(candidate_id, str)
        and candidate_id.startswith("hjll-script:")
        and decision.parameters.get("hjll_script") is True
    ):
        bounds = decision.parameters.get("normalized_bounds")
        meta = {
            "purpose": decision.purpose,
            "frontier_scope": "local",
            "hjll_script": True,
            "hjll_product_orchestrated_point": True,
            "require_screenshot_evidence": True,
        }
        if decision.parameters.get("splash_body_evidence") is True:
            meta["splash_body_evidence"] = True
        if decision.parameters.get("splash_skip_after_only") is True:
            meta["splash_skip_after_only"] = True
        if isinstance(bounds, Mapping):
            meta["normalized_bounds"] = dict(bounds)
        return CandidateAction(
            candidate_id=candidate_id,
            semantic_target=str(decision.semantic_target or "HJLL 脚本步骤"),
            action_type=decision.action_type,
            risk_level=decision.risk_level,
            exploration_value=100,
            navigation_depth=0,
            expected_surface_kind=work.surface_kind,
            metadata=meta,
        )
    if (
        isinstance(candidate_id, str)
        and candidate_id.startswith("hlapp-script:")
        and decision.parameters.get("hlapp_script") is True
    ):
        bounds = decision.parameters.get("normalized_bounds")
        meta = {
            "purpose": decision.purpose,
            "frontier_scope": "local",
            "hlapp_script": True,
            "hlapp_product_orchestrated_point": True,
            "require_screenshot_evidence": True,
            "action_parameters": {
                key: decision.parameters[key]
                for key in ("from", "to", "duration_ms", "hlapp_settle_after_action_ms")
                if key in decision.parameters
            },
        }
        if decision.parameters.get("splash_body_evidence") is True:
            meta["splash_body_evidence"] = True
        if decision.parameters.get("splash_skip_after_only") is True:
            meta["splash_skip_after_only"] = True
        if isinstance(bounds, Mapping):
            meta["normalized_bounds"] = dict(bounds)
        return CandidateAction(
            candidate_id=candidate_id,
            semantic_target=str(decision.semantic_target or "HLAPP 脚本步骤"),
            action_type=decision.action_type,
            risk_level=decision.risk_level,
            exploration_value=100,
            navigation_depth=0,
            expected_surface_kind=work.surface_kind,
            metadata=meta,
        )
    if (
        isinstance(candidate_id, str)
        and candidate_id.startswith("hlwapp-script:")
        and decision.parameters.get("hlwapp_script") is True
    ):
        bounds = decision.parameters.get("normalized_bounds")
        meta = {
            "purpose": decision.purpose,
            "frontier_scope": "local",
            "hlwapp_script": True,
            "hlwapp_product_orchestrated_point": True,
            "require_screenshot_evidence": True,
            "action_parameters": {
                key: decision.parameters[key]
                for key in ("from", "to", "duration_ms", "hlwapp_settle_after_action_ms")
                if key in decision.parameters
            },
        }
        if decision.parameters.get("hlwapp_browser_hold") is True:
            meta["hlwapp_browser_hold"] = True
        if isinstance(bounds, Mapping):
            meta["normalized_bounds"] = dict(bounds)
        return CandidateAction(
            candidate_id=candidate_id,
            semantic_target=str(decision.semantic_target or "黑料网脚本步骤"),
            action_type=decision.action_type,
            risk_level=decision.risk_level,
            exploration_value=100,
            navigation_depth=0,
            expected_surface_kind=work.surface_kind,
            metadata=meta,
        )
    raise ValueError(
        f"ActionIntent candidate_id 不属于当前 App Frontier: {candidate_id!r}"
    )


def _eligible_candidate_count(work: ExplorationWork) -> int:
    """按当前 Work 的真实边界、状态和预算统计可执行候选。"""

    return count_eligible_app_candidates(work)


def _pending_is_bottom_nav(pending) -> bool:
    """调度侧识别底栏 sibling，供 RETRY_WAIT 预算与恢复错误分流。"""

    metadata = pending.candidate.metadata
    if metadata.get("bottom_navigation_tab_seek") is True:
        return True
    if metadata.get("frontier_scope") == "app":
        terms = " ".join(
            (
                str(pending.candidate.semantic_target or ""),
                str(metadata.get("purpose") or ""),
            )
        ).casefold()
        if any(marker in terms for marker in ("底栏", "底部tab", "底部导航", "bottom")):
            return True
    terms = str(pending.candidate.semantic_target or "").casefold()
    return any(marker in terms for marker in ("底栏", "底部tab", "底部导航"))


def _should_skip_stale_bottom_nav_pending(*, work: ExplorationWork, pending) -> bool:
    """底栏未完时跳过非课纲下一栏的历史 sibling，避免先点社区／我的。"""

    if not _pending_is_bottom_nav(pending):
        return False
    from services.api.app.application.app_coverage_page_seek import (
        VISITED_BOTTOM_NAVIGATION_LABELS_KEY,
        _next_unvisited_playbook_bottom_tab_label,
        _normalize_bottom_tab_label,
        _playbook_bottom_tab_labels_ordered,
        coverage_incomplete_allows_bottom_nav_through_overlay,
    )
    from services.api.app.application.app_semantic_progress import (
        bottom_tab_label_already_visited,
    )

    observation = work.frontier.observation
    metadata = observation.metadata if isinstance(observation.metadata, Mapping) else {}
    if not coverage_incomplete_allows_bottom_nav_through_overlay(
        coverage_metadata=metadata,
    ):
        return False
    # 当前帧已有底栏：历史 sibling 一律让路。
    if any(
        candidate.metadata.get("bottom_navigation_tab_seek") is True
        for candidate in work.frontier.candidates
    ):
        return True
    # 当前帧无底栏：仍只允许课纲下一栏历史 sibling，禁止先领「社区」。
    visited_raw = metadata.get(VISITED_BOTTOM_NAVIGATION_LABELS_KEY)
    visited: set[str] = set()
    if isinstance(visited_raw, (list, tuple)):
        visited = {
            _normalize_bottom_tab_label(item)
            for item in visited_raw
            if isinstance(item, str) and _normalize_bottom_tab_label(item)
        }
    ordered = _playbook_bottom_tab_labels_ordered(
        metadata,
        package_name=str(metadata.get("package_name") or "") or None,
    )
    next_label = _next_unvisited_playbook_bottom_tab_label(
        visited_labels=visited,
        ordered_labels=ordered,
        page_type=str(observation.page_type or ""),
    )
    if not next_label:
        return False
    pending_label = _normalize_bottom_tab_label(pending.candidate.semantic_target)
    return not bottom_tab_label_already_visited(pending_label, {next_label})


def _frontier_restoration_target_status(
    error: AppFrontierRestorationError,
    *,
    attempt_count: int,
    bottom_nav: bool = False,
) -> AppFrontierStatus:
    """候选瞬态消失只重试有限次，其余恢复错误立即进入安全阻断。"""

    # 明确禁止空水平槽重绑：立刻阻断，禁止 RETRY_WAIT 空转。
    message = str(error)
    if "禁止空水平槽重绑" in message:
        return AppFrontierStatus.BLOCKED
    retryable = error.retryable
    # 底栏 SOURCE_UNRECOVERABLE：即便旧路径未标 retryable，也按有界重试处理。
    if (
        bottom_nav
        and error.error_code
        in {
            "APP_FRONTIER_SOURCE_UNRECOVERABLE",
            "APP_FRONTIER_CANDIDATE_REVALIDATION_REQUIRED",
        }
    ):
        retryable = True
    if retryable and app_frontier_same_semantic_retry_allowed(
        attempt_count,
        bottom_nav=bottom_nav,
    ):
        return AppFrontierStatus.RETRY_WAIT
    return AppFrontierStatus.BLOCKED


def _frontier_status_for_turn(turn: ExplorerTurnResult) -> AppFrontierStatus:
    coordination = turn.coordination
    if coordination is None:
        return AppFrontierStatus.FAILED
    status = coordination.interaction.status
    if status in {
        InteractionResultStatus.SUCCEEDED,
        InteractionResultStatus.EVIDENCE_PENDING,
        InteractionResultStatus.EVIDENCE_FAILED,
    }:
        return AppFrontierStatus.VISITED
    if status is InteractionResultStatus.BLOCKED:
        return AppFrontierStatus.BLOCKED
    return AppFrontierStatus.FAILED


def _frontier_arrival_outcome(
    *,
    turn: ExplorerTurnResult,
    item: AppFrontierItem,
    successor: ExplorationWork | None,
) -> tuple[AppFrontierStatus, str | None, str | None]:
    """候选只有在动作后截图证明确实到达目标页时才算 visited。"""

    interaction_status = _frontier_status_for_turn(turn)
    coordination = turn.coordination
    interaction_error = coordination.interaction.error_code if coordination is not None else None
    interaction_message = (
        coordination.interaction.error_message if coordination is not None else None
    )
    if (
        coordination is not None
        and coordination.interaction.status is InteractionResultStatus.FAILED
        and coordination.interaction.retryable
        and interaction_error
        in {
            "STALE_VISUAL_CANDIDATE",
            "APP_CANDIDATE_REVALIDATION_REQUIRED",
            # 滚动／微变后需同语义重绑；一次失败不得永久烧死底栏／搜索入口 sibling。
            "POST_SCROLL_REBIND_REQUIRED",
        }
    ):
        # 候选过期发生在 Driver 动作之前；after Capture 已提供新视觉事实时，
        # 旧 Frontier 语义项必须等待当前帧重新验签，不能被永久写成 failed。
        return AppFrontierStatus.RETRY_WAIT, interaction_error, interaction_message
    # 底栏巡检：设备通道未知不得一次永久 failed，恢复后按 a11y 重绑再试。
    # 上方分类同理：通道异常不得一次烧死课纲，否则会假推进后切短剧。
    if (
        coordination is not None
        and coordination.interaction.status is InteractionResultStatus.FAILED
        and interaction_error
        in {
            "APP_ACTION_OUTCOME_UNKNOWN_DURING_EXECUTION",
            "APP_ACTION_OUTCOME_UNKNOWN_AFTER_CRASH",
            "APP_ACTION_OUTCOME_UNKNOWN_DURING_CANCELLATION",
            # 手势带拒点后须抬升重绑，不得一次永久 failed。
            "ANDROID_POINT_IN_GESTURE_ZONE",
        }
        and (
            item.candidate.metadata.get("bottom_navigation_tab_seek") is True
            or item.candidate.metadata.get("home_top_category_tab_seek") is True
            or item.candidate.metadata.get("frontier_scope") == "app"
        )
    ):
        return (
            AppFrontierStatus.RETRY_WAIT,
            interaction_error,
            interaction_message
            or "上方分类／底栏落点不安全或结果未知，恢复后按条带重绑再试",
        )
    if interaction_status is not AppFrontierStatus.VISITED:
        return interaction_status, interaction_error, interaction_message

    proof = (
        action_observation_for_successor(turn=turn, successor=successor)
        if successor is not None
        else None
    )
    if proof is not None and proof.get("outcome") in {
        "business_arrived",
        "local_state_changed",
        "obstruction_removed",
        "observation_completed",
        "surface_restored",
        "tab_seek_screenshot_evidenced",
    }:
        # 底栏巡检：须 after 页型切到目标栏（见 tab_seek_transition_reason），
        # 不得仅凭截图对绑定就 VISITED。
        if proof.get("outcome") == "tab_seek_screenshot_evidenced":
            return AppFrontierStatus.VISITED, interaction_error, interaction_message
        expected = item.candidate.metadata.get("expected_next_page_type")
        if (
            not isinstance(expected, str)
            or not expected.strip()
            or (
                proof.get("business_visible") is True
                and expected.strip().casefold()
                == str(successor.frontier.observation.page_type or "").casefold()
            )
        ):
            return AppFrontierStatus.VISITED, interaction_error, interaction_message
    if proof is not None and proof.get("outcome") == "unproven":
        reason = str(proof.get("reason") or "")
        if item.candidate.metadata.get("bottom_navigation_tab_seek") is True:
            # 任意未证明切栏：有界重试／换栏，禁止 FAILED／SUPERSEDED 抽空 Frontier。
            return (
                AppFrontierStatus.RETRY_WAIT,
                "APP_FRONTIER_TARGET_ARRIVAL_UNPROVEN",
                f"底栏点击后未证明切栏：{reason or 'unproven'}",
            )
        if item.candidate.metadata.get("home_top_category_tab_seek") is True:
            # 上方分类同页馈流未确认：有界重试，禁止一次 FAILED 抽空课纲。
            return (
                AppFrontierStatus.RETRY_WAIT,
                "APP_FRONTIER_TARGET_ARRIVAL_UNPROVEN",
                f"上方分类点击后未证明切栏／馈流：{reason or 'unproven'}",
            )
    if proof is not None and proof.get("outcome") == "intermediate":
        if item.candidate.metadata.get("bottom_navigation_tab_seek") is True:
            return (
                AppFrontierStatus.RETRY_WAIT,
                "APP_FRONTIER_TARGET_ARRIVAL_UNPROVEN",
                f"底栏点击后仍为中间态：{proof.get('reason')}，重绑后再试",
            )
        if item.candidate.metadata.get("home_top_category_tab_seek") is True:
            return (
                AppFrontierStatus.RETRY_WAIT,
                "APP_FRONTIER_TARGET_ARRIVAL_UNPROVEN",
                f"上方分类点击后仍为中间态：{proof.get('reason')}，重绑后再试",
            )
        # 中间页面或替换后的弹窗允许后继继续，但不能冒充原候选已到达。
        return (
            AppFrontierStatus.SUPERSEDED,
            "APP_FRONTIER_INTERMEDIATE_PAGE_REACHED",
            f"动作后观察到中间状态：{proof.get('reason')}，由后继继续探索",
        )
    # 底栏巡检点进系统手势区会落到桌面：恢复后应重绑 a11y 再试，不得一次永久 failed。
    if (
        proof is not None
        and proof.get("outcome") == "external"
        and (
            item.candidate.metadata.get("bottom_navigation_tab_seek") is True
            or item.candidate.metadata.get("frontier_scope") == "app"
        )
        and str(proof.get("reason") or "") in {
            "after_package_outside_scope",
            "recovery_still_outside_target_app",
        }
    ):
        return (
            AppFrontierStatus.RETRY_WAIT,
            "APP_FRONTIER_TARGET_ARRIVAL_UNPROVEN",
            f"底栏点击后离开目标包（{proof.get('reason')}），恢复后按 a11y 重绑再试",
        )
    reason = proof.get("reason") if proof else "proof_missing_or_mismatched"
    return (
        AppFrontierStatus.FAILED,
        "APP_FRONTIER_TARGET_ARRIVAL_UNPROVEN",
        f"缺少同次动作的有效到达证明：{reason}",
    )


async def _stop_frontier_guard(guard) -> None:
    if guard is None:
        return
    # Work/Frontier 已在同一事务收口后，后台续租可能观察到预期的终态。
    with suppress(AppExplorationLeaseLostError):
        await guard.stop()


def _prefer_current_goal_terminal(
    *,
    work: ExplorationWork,
    recovered_decision: ActionIntent | None,
) -> ActionIntent | ExplorationTerminal | None:
    """恢复旧动作前重算当前覆盖，避免异步回写后继续执行过期意图。"""

    if recovered_decision is None:
        return None
    evaluation = AppExplorationTerminalEvaluator(now=lambda: datetime.now(UTC)).evaluate(
        work,
        eligible_candidate_count=_eligible_candidate_count(work),
    )
    return evaluation.terminal or recovered_decision


def _is_goal_completion_terminal(terminal: ExplorationTerminal) -> bool:
    """锁定目标已完整满足时，不得再被开放的任务级 Frontier 覆盖。"""

    summary = terminal.coverage_summary
    return (
        terminal.outcome == AppExplorationOutcome.EXPLORATION_COMPLETED.value
        and terminal.reason_code == "exploration_completed"
        and summary.get("goal_driven") is True
        and summary.get("coverage_complete") is True
    )


def _frontier_exhausted_terminal(
    proposed: ExplorationTerminal,
    *,
    counts: AppFrontierCounts,
    superseded_reason_code: str | None = None,
) -> ExplorationTerminal:
    summary = {
        **dict(proposed.coverage_summary),
        "app_global_frontier": {
            "protocol_version": "app-global-frontier-summary.v1",
            **counts.to_dict(),
            "nonterminal_count": counts.nonterminal_count,
            "exhausted": True,
        },
    }
    # 统一改写成通用码会让所有 App 失败长一个样；把评估器算出的细分原因留在
    # 摘要里，供排障区分 stable_page_not_reached / minimum_coverage_not_met 等。
    if superseded_reason_code:
        summary["insufficient_reason_code"] = superseded_reason_code
    if proposed.outcome is not None:
        return replace(proposed, coverage_summary=summary)
    return ExplorationTerminal(
        work_id=proposed.work_id,
        task_id=proposed.task_id,
        status=ExplorationTerminalStatus.NO_PROGRESS,
        reason="任务级 App Frontier 已排空，但未建立确定性完成条件。",
        outcome=AppExplorationOutcome.EVIDENCE_INSUFFICIENT.value,
        reason_code="app_global_frontier_exhausted_without_completion",
        coverage_summary=summary,
    )


def _evaluate_exhausted_frontier_terminal(
    proposed: ExplorationTerminal,
    *,
    work: ExplorationWork,
    counts: AppFrontierCounts,
) -> ExplorationTerminal:
    """Frontier 排空时从当前 Work 重建完整覆盖摘要，再形成通用终态。"""

    frontier_summary = {
        "protocol_version": "app-global-frontier-summary.v1",
        **counts.to_dict(),
        "nonterminal_count": counts.nonterminal_count,
        "exhausted": True,
    }
    evaluated_work = replace(
        work,
        frontier=replace(
            work.frontier,
            observation=replace(
                work.frontier.observation,
                metadata={
                    **dict(work.frontier.observation.metadata),
                    "app_global_frontier": frontier_summary,
                },
            ),
        ),
    )
    evaluation = AppExplorationTerminalEvaluator(now=lambda: datetime.now(UTC)).evaluate(
        evaluated_work,
        eligible_candidate_count=0,
        proposed_terminal=proposed,
    )
    if evaluation.terminal is None:
        coverage = work.frontier.observation.metadata.get("app_rule_inventory_coverage")
        if (
            isinstance(coverage, Mapping)
            and coverage.get("protocol_version") == "app-rule-coverage-facts.v1"
            and isinstance(coverage.get("pending_inspection_work_count"), int)
            and not isinstance(coverage.get("pending_inspection_work_count"), bool)
            and coverage["pending_inspection_work_count"] > 0
            and datetime.now(UTC) < work.deadline
        ):
            # 判定工作已经发布时先等待真实写回；下一次领取会刷新同一运行谱系的事实。
            raise AppFrontierDeferredError("App 活会话规则正在判定，等待缺证反馈")
        # 普通评估器允许启动清障继续；此处队列已确认排空且没有可恢复入口，
        # 应保留证据不足与完整覆盖摘要，而不是把正常停止误报为技术异常。
        return _frontier_exhausted_terminal(
            replace(
                proposed,
                status=ExplorationTerminalStatus.NO_PROGRESS,
                outcome=None,
                reason_code=None,
                coverage_summary=evaluation.coverage.to_dict(),
            ),
            counts=counts,
            superseded_reason_code=proposed.reason_code,
        )
    if _is_goal_completion_terminal(evaluation.terminal):
        return _frontier_exhausted_terminal(
            evaluation.terminal,
            counts=counts,
        )
    return _frontier_exhausted_terminal(
        replace(
            evaluation.terminal,
            outcome=None,
            reason_code=None,
        ),
        counts=counts,
        superseded_reason_code=evaluation.terminal.reason_code,
    )


def _frontier_settlement(
    *,
    work: ExplorationWork,
    agent_run_id: str,
    assigned_item,
    selected_frontier_item,
    result_capture_id: str | None,
    target_status: AppFrontierStatus | None = None,
    turn: ExplorerTurnResult | None = None,
    successor: ExplorationWork | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
    retry_delay_seconds: int = 0,
) -> AppFrontierSettlement | None:
    item = assigned_item.item if assigned_item is not None else selected_frontier_item
    if item is None:
        return None
    if turn is not None:
        target_status, arrival_error_code, arrival_error_message = _frontier_arrival_outcome(
            turn=turn,
            item=item,
            successor=successor,
        )
        error_code = arrival_error_code or error_code
        error_message = arrival_error_message or error_message
    if target_status is None:
        raise ValueError("App Frontier settlement 缺少 target_status")
    if (
        target_status is AppFrontierStatus.RETRY_WAIT
        and assigned_item is not None
        and error_code
        in {
            "STALE_VISUAL_CANDIDATE",
            "APP_CANDIDATE_REVALIDATION_REQUIRED",
        }
        and app_frontier_same_semantic_retry_exhausted(assigned_item.attempt_count)
    ):
        # 动作前候选漂移属于瞬态，但同一语义项初次领取外最多重试两次。
        # DB 的 attempt_count 是唯一事实源；达到上限后必须永久阻断，避免
        # retry_wait 在下一轮继续重放同一不可重验证的候选。
        target_status = AppFrontierStatus.BLOCKED
        retry_delay_seconds = 0
        error_message = error_message or "同一语义候选连续三次无法重新验证，已阻断。"
    if target_status is AppFrontierStatus.RETRY_WAIT and retry_delay_seconds <= 0:
        # 动作结果中的视觉过期由结算层识别，调用方尚未进入异常重试分支；
        # 给数据库一个最短正延迟，避免同一 Worker 在同一瞬间重领旧视觉项。
        retry_delay_seconds = 1
    if (
        target_status is AppFrontierStatus.RETRY_WAIT
        and error_code
        in {
            "APP_ACTION_OUTCOME_UNKNOWN_DURING_EXECUTION",
            "APP_ACTION_OUTCOME_UNKNOWN_AFTER_CRASH",
            "APP_ACTION_OUTCOME_UNKNOWN_DURING_CANCELLATION",
        }
        and (
            item.candidate.metadata.get("bottom_navigation_tab_seek") is True
            or item.candidate.metadata.get("frontier_scope") == "app"
            or _pending_is_bottom_nav(item)
        )
    ):
        # 云手机通道未知后冷却，避免 attempt 被连点烧光。
        from services.api.app.application.app_frontier_restoration import (
            APP_FRONTIER_BOTTOM_NAV_UNKNOWN_RETRY_DELAY_SECONDS,
        )

        retry_delay_seconds = max(
            retry_delay_seconds,
            APP_FRONTIER_BOTTOM_NAV_UNKNOWN_RETRY_DELAY_SECONDS,
        )
    return AppFrontierSettlement(
        task_id=work.task_id,
        agent_run_id=agent_run_id,
        session_id=work.session_id,
        candidate_fingerprint=item.candidate_fingerprint,
        target_status=target_status,
        frontier_item_id=(item.frontier_item_id if assigned_item is not None else None),
        lease_token=(assigned_item.lease_token if assigned_item is not None else None),
        row_version=(assigned_item.row_version if assigned_item is not None else None),
        result_capture_id=result_capture_id,
        error_code=error_code,
        error_message=error_message,
        retry_delay_seconds=retry_delay_seconds,
    )


def _validate_successor(
    *,
    prior_work: ExplorationWork,
    turn: ExplorerTurnResult,
    successor: AppScreenshotExplorationResult,
) -> None:
    if not isinstance(successor, AppScreenshotExplorationResult):
        raise TypeError("App 后继发布器返回了无效结果")
    work = successor.work
    coordination = turn.coordination
    result_capture = (
        coordination.after_capture if coordination is not None else turn.observation_capture
    )
    if result_capture is None:
        raise ValueError("App 非终态轮缺少结果 Capture")
    if (
        work.task_id != prior_work.task_id
        or work.session_id != prior_work.session_id
        or work.surface_kind is not prior_work.surface_kind
        or work.source_capture_id != result_capture.capture_id
        or work.work_id == prior_work.work_id
        or work.app_goal != prior_work.app_goal
    ):
        raise ValueError("App 后继 Work 与当前轮身份不一致")
    _validate_coverage_progression(prior_work=prior_work, successor_work=work)


def _validate_coverage_progression(
    *,
    prior_work: ExplorationWork,
    successor_work: ExplorationWork,
) -> None:
    """覆盖快照只能累计，后继 Work 不得删除已证实页面或里程碑。"""

    if prior_work.app_goal is None:
        return
    prior = prior_work.frontier.observation.metadata
    successor = successor_work.frontier.observation.metadata
    for field_name in (
        "covered_page_types",
        "covered_milestones",
        "blocked_page_types",
        "blocked_milestones",
        "covered_business_state_ids",
    ):
        previous_values = _coverage_texts(prior.get(field_name))
        next_values = _coverage_texts(successor.get(field_name))
        if not previous_values.issubset(next_values):
            raise ValueError(f"App 后继 Work 删除了已持久化覆盖事实：{field_name}")
    previous_count = _coverage_count(prior.get("covered_business_page_count"))
    next_count = _coverage_count(successor.get("covered_business_page_count"))
    if next_count < previous_count:
        raise ValueError("App 后继 Work 的业务页面覆盖计数发生回退")


def _coverage_texts(value: object) -> set[str]:
    if not isinstance(value, (list, tuple)):
        return set()
    return {item for item in value if isinstance(item, str) and item.strip()}


def _coverage_count(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _with_terminal_action_count(
    *,
    work,
    turn: ExplorerTurnResult,
    settled_work: ExplorationWork | None = None,
) -> ExplorerTurnResult:
    """把最后一轮动作结算存进已有终态，重启恢复不能退回父 Work 的旧预算。"""
    if (
        not isinstance(work, ExplorationWork)
        or not isinstance(turn.decision, ExplorationTerminal)
        or (turn.coordination is None and settled_work is None)
    ):
        return turn
    budget = (settled_work or work).budget
    consumed = budget.max_actions - budget.remaining_actions
    if settled_work is None and turn.coordination is not None:
        consumed += 1
    prior_consumed = work.budget.max_actions - work.budget.remaining_actions
    if (
        budget.max_actions != work.budget.max_actions
        or not prior_consumed <= consumed <= min(budget.max_actions, prior_consumed + 1)
    ):
        raise ValueError("App 终态动作结算与锁定预算不一致")
    summary = {**dict(turn.decision.coverage_summary), "consumed_action_count": consumed}
    if turn.coordination is not None:
        summary["result_capture_id"] = turn.coordination.after_capture.capture_id
    return replace(turn, decision=replace(turn.decision, coverage_summary=summary))


def _successful_target(
    turn: ExplorerTurnResult,
) -> tuple[ExplorerWorkStatus, ExplorationTerminal | None]:
    if isinstance(turn.decision, ExplorationTerminal):
        target_by_terminal = {
            ExplorationTerminalStatus.COMPLETED: ExplorerWorkStatus.SUCCEEDED,
            ExplorationTerminalStatus.BOUNDED: ExplorerWorkStatus.BOUNDED,
            ExplorationTerminalStatus.BUDGET_EXHAUSTED: (ExplorerWorkStatus.BUDGET_EXHAUSTED),
            ExplorationTerminalStatus.BLOCKED: ExplorerWorkStatus.BLOCKED,
            ExplorationTerminalStatus.NO_PROGRESS: ExplorerWorkStatus.NO_PROGRESS,
        }
        return target_by_terminal[turn.decision.status], turn.decision
    if isinstance(turn.decision, TimedObservationIntent):
        if turn.observation_capture is None or turn.coordination is not None:
            raise ValueError("Timed Observation 必须只返回 Observation Capture")
        return ExplorerWorkStatus.SUCCEEDED, None
    if turn.coordination is None:
        raise ValueError("动作决策必须返回 Capture Coordination 结果")
    return ExplorerWorkStatus.SUCCEEDED, None


def _package_boundary_terminal(
    *,
    work: ExplorationWork,
    turn: ExplorerTurnResult,
) -> ExplorationTerminal | None:
    """目标 App 越界时停止生成后继。

    即使 interaction.retryable=True，本轮也不得标 SUCCEEDED 再发业务 successor；
    桌面／外跳恢复由下一轮 Work 的 package-boundary BACK／warm_restore 处理。
    """

    coordination = turn.coordination
    if (
        coordination is None
        or coordination.interaction.error_code != "APP_PACKAGE_BOUNDARY_VIOLATION"
    ):
        return None
    return ExplorationTerminal(
        work_id=work.work_id,
        task_id=work.task_id,
        status=ExplorationTerminalStatus.NO_PROGRESS,
        reason="当前前台应用已越过目标 App 包边界，停止生成后继探索工作。",
        outcome=AppExplorationOutcome.EVIDENCE_INSUFFICIENT.value,
        reason_code="app_package_boundary_violation",
    )


def _is_retryable(exc: Exception) -> bool:
    if isinstance(exc, CaptureCoordinationError):
        # 动作前 Capture 失败可重试；动作后 Capture 仅当显式标记 retryable
        # （Official 502／TIMEOUT）。
        if exc.stage is CaptureCoordinationStage.BEFORE_CAPTURE:
            return True
        if exc.stage is CaptureCoordinationStage.AFTER_CAPTURE:
            return bool(getattr(exc, "retryable", False))
        return False
    return getattr(exc, "retryable", False) is True


def _safe_error_code(exc: Exception) -> str:
    value = getattr(exc, "error_code", None)
    if isinstance(value, str) and value.strip() and value == value.strip():
        return value[:128]
    return "APP_EXPLORATION_TURN_FAILED"
