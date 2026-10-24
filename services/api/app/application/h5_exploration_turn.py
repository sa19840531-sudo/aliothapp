"""H5 Explorer 单轮工作的租约、执行与后继提交。"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Protocol
from urllib.parse import urlsplit
from uuid import uuid4

from services.api.app.application.app_exploration_lease import (
    AppExplorationLeaseGuard,
    AppExplorationLeaseLostError,
)
from services.api.app.application.capture_coordination import (
    CaptureCoordinationError,
    CaptureCoordinationStage,
)
from services.api.app.application.exploration_terminal_settlement import (
    with_terminal_settlement,
)
from services.api.app.application.exploration_work_queue import (
    ClaimExplorerWorkCommand,
    CommitExplorerWorkSuccessorCommand,
    ExplorerWorkDeadlineExceededError,
    ExplorerWorkQueue,
    ExplorerWorkStatus,
    ExplorerWorkType,
    RenewExplorerWorkLeaseCommand,
    TransitionExplorerWorkCommand,
    UpdateExplorerWorkPayloadCommand,
)
from services.api.app.application.explorer_turn import (
    ExplorerTurnExecutor,
    ExplorerTurnResult,
)
from services.api.app.application.h5_exploration_cancellation import (
    H5CancellationCheckpoint,
    H5ExplorationCanceled,
)
from services.api.app.application.h5_exploration_recovery import (
    H5BrowserRecoveryAttemptFacts,
    H5RecoveryOutcome,
)
from services.api.app.application.h5_exploration_workflow import (
    ContinueH5ExplorationCommand,
    H5ExplorationResult,
    ReobserveH5ExplorationCommand,
    h5_action_failure_is_policy_blocked,
    rebind_h5_selected_entry_to_current_observation,
)
from services.api.app.application.h5_list_detail_navigation import (
    H5_LIST_DETAIL_TARGET,
    h5_list_detail_expected_page_type,
    h5_list_detail_selector_allowed,
)
from services.api.app.application.h5_profile_visual_navigation import (
    h5_profile_visual_entry_contract,
)
from services.api.app.application.h5_recapture_lease import H5BoundedLeaseGuard
from services.api.app.application.h5_session_journey import (
    H5SessionJourneyDeadline,
    H5SessionJourneyLost,
    h5_session_journey,
)
from services.api.app.application.h5_structural_navigation import (
    h5_bottom_navigation_action_parameters,
    h5_bottom_navigation_family,
    h5_bottom_navigation_selector,
)
from services.api.app.domain.exploration import (
    ExplorationTerminal,
    ExplorationTerminalStatus,
    ExplorationWork,
)
from services.api.app.domain.exploration.h5_frontier import (
    H5_TASK_FRONTIER_METADATA_KEY,
    H5FrontierEntryStatus,
    H5TaskFrontierSnapshot,
    canonical_h5_route_identity,
)
from services.api.app.domain.interaction import (
    InteractionOperation,
    InteractionResultStatus,
    InteractionRiskLevel,
    SurfaceKind,
)


@dataclass(frozen=True, kw_only=True)
class H5ExplorationTurnOutcome:
    """一次 H5 Explorer Work 领取与执行的引用型结果。"""

    claimed: bool
    work_id: str | None = None
    final_status: ExplorerWorkStatus | None = None
    turn: ExplorerTurnResult | None = None
    result_capture_id: str | None = None
    successor_work_id: str | None = None
    error_code: str | None = None
    # 本轮以「换一条 Frontier sibling 继续探索」收场：是进展而非原地重试
    reselected_sibling: bool = False
    # 实际来源恢复单独交给离页检查点，不在同轮再次导航。
    restored_source: bool = False


class H5ExplorationContinuationPort(Protocol):
    async def prepare_after_action(
        self,
        command: ContinueH5ExplorationCommand,
    ) -> H5ExplorationResult: ...

    async def prepare_after_action_failure(
        self,
        command: ReobserveH5ExplorationCommand,
    ) -> H5ExplorationResult: ...


@dataclass(frozen=True, kw_only=True)
class H5FrontierRecoveryWorkResult:
    """恢复来源页面后交给 Explorer 的瞬时 Work 快照。"""

    work: ExplorationWork
    restored: bool
    reason_code: str | None = None
    # 恢复耗尽后已改选到另一条 pending sibling：重投的是实质不同的工作
    reselected_sibling: bool = False


class H5FrontierRecoveryPort(Protocol):
    """H5 组合根注入的受控来源恢复端口。"""

    async def restore(
        self,
        *,
        work: ExplorationWork,
        agent_run_id: str,
        entry: object,
    ) -> H5FrontierRecoveryWorkResult: ...


class H5BrowserLossRecoveryPort(Protocol):
    """H5 组合根注入的浏览器进程丢失恢复端口。"""

    async def recover(
        self,
        *,
        work: ExplorationWork,
        agent_run_id: str,
        error: CaptureCoordinationError,
    ) -> H5RecoveryOutcome: ...


class H5ExplorationTurnRunner:
    """领取一条 Web Exploration Work 并在父租约中提交后继。"""

    def __init__(
        self,
        *,
        queue: ExplorerWorkQueue,
        turn_executor: ExplorerTurnExecutor,
        continuation: H5ExplorationContinuationPort,
        recovery: H5FrontierRecoveryPort | None = None,
        browser_loss_recovery: H5BrowserLossRecoveryPort | None = None,
        lease_owner: str,
        lease_seconds: int = 180,
        # LLM 网关 503 窗口实测可持续 30 秒以上；配合工作项 3 次尝试预算，
        # 20 秒退避让整体容忍度达到约 2 分钟连续故障。
        retry_delay_seconds: int = 20,
        cancellation: H5CancellationCheckpoint | None = None,
        session_lineage: object | None = None,
        emit_wakeup: bool = True,
        session_command_gate: object | None = None,
        adaptive_journey: object | None = None,
    ) -> None:
        if not isinstance(lease_owner, str) or not lease_owner.strip():
            raise ValueError("lease_owner 必须为非空字符串")
        if lease_owner != lease_owner.strip():
            raise ValueError("lease_owner 不能包含首尾空白")
        if lease_seconds <= 0 or retry_delay_seconds < 0:
            raise ValueError("租约时长和重试延迟不合法")
        self._queue = queue
        self._turn_executor = turn_executor
        self._continuation = continuation
        self._recovery = recovery
        self._browser_loss_recovery = browser_loss_recovery
        self._lease_owner = lease_owner
        self._lease_seconds = lease_seconds
        self._retry_delay_seconds = retry_delay_seconds
        self._cancellation = cancellation or H5CancellationCheckpoint()
        self._session_lineage = session_lineage
        self._emit_wakeup = emit_wakeup
        self._session_command_gate = session_command_gate
        self._adaptive_journey = adaptive_journey

    async def run_one(
        self,
        *,
        task_id: str,
        agent_run_id: str,
    ) -> H5ExplorationTurnOutcome:
        for field_name, value in (("task_id", task_id), ("agent_run_id", agent_run_id)):
            if not isinstance(value, str) or not value.strip() or value != value.strip():
                raise ValueError(f"{field_name} 必须为无首尾空白的非空字符串")
        try:
            await self._raise_if_canceled(
                task_id=task_id,
                agent_run_id=agent_run_id,
                stage="before_claim",
            )
        except H5ExplorationCanceled:
            await self.cancel_nonterminal(task_id=task_id)
            return H5ExplorationTurnOutcome(
                claimed=False,
                final_status=ExplorerWorkStatus.CANCELLED,
                error_code=H5ExplorationCanceled.error_code,
            )
        lease_token = str(uuid4())
        lease = await self._queue.claim(
            command=ClaimExplorerWorkCommand(
                work_type=ExplorerWorkType.EXPLORATION,
                lease_owner=self._lease_owner,
                lease_seconds=self._lease_seconds,
                task_id=task_id,
                surface_kinds=(SurfaceKind.WEB,),
            ),
            lease_token=lease_token,
        )
        if lease is None:
            return H5ExplorationTurnOutcome(claimed=False)
        register_session = getattr(self._session_lineage, "register", None)
        if callable(register_session):
            register_session(lease.work.session_id)
        running_version = await self._queue.transition(
            command=TransitionExplorerWorkCommand(
                work_id=lease.work.work_id,
                expected_status=ExplorerWorkStatus.LEASED,
                target_status=ExplorerWorkStatus.RUNNING,
                lease_token=lease.lease_token,
                row_version=lease.row_version,
            )
        )
        guard = H5BoundedLeaseGuard(
            queue=self._queue,
            command=RenewExplorerWorkLeaseCommand(
                work_id=lease.work.work_id,
                lease_owner=self._lease_owner,
                lease_token=lease.lease_token,
                lease_seconds=self._lease_seconds,
            ),
            renew_interval_seconds=max(1, self._lease_seconds // 3),
            lost_error_type=AppExplorationLeaseLostError,
            lost_message="H5 主探索租约已失效", task_name="h5-exploration-lease-renew",
        )
        execution_work = lease.work
        journey_deadline = min(
            lease.work.deadline,
            datetime.now(UTC) + timedelta(milliseconds=lease.work.budget.remaining_duration_ms),
        )
        try:
            try:
                async with asyncio.timeout(max(
                    0, (journey_deadline - datetime.now(UTC)).total_seconds(),
                )):
                    await guard.start()
            except TimeoutError:
                await guard.stop()
                raise H5SessionJourneyDeadline("H5 首次续租耗尽冻结时间") from None
            async with guard, h5_session_journey(
                gate=self._session_command_gate, task_id=task_id,
                session_id=lease.work.session_id, deadline=journey_deadline,
            ):
                _validate_h5_work(lease.work)
                await self._raise_if_canceled(
                    task_id=task_id,
                    agent_run_id=agent_run_id,
                    stage="before_action",
                )
                synchronize = getattr(self._recovery, "synchronize", None)
                attempt = _validated_browser_attempt(execution_work, agent_run_id)
                has_started_browser_attempt = attempt is not None and (
                    attempt.status == "started" or (
                        attempt.status == "completed" and attempt.reason_code is not None
                    )
                )
                # 已持久化恢复先接续恢复，不能先同步已 LOST 的旧现场。
                if callable(synchronize) and not has_started_browser_attempt:
                    synchronized = await guard.run(synchronize(
                        work=execution_work, agent_run_id=agent_run_id,
                    ))
                    if not isinstance(synchronized, ExplorationWork) or any(
                        getattr(synchronized, field) != getattr(execution_work, field)
                        for field in (
                            "work_id", "task_id", "session_id", "surface_kind",
                            "budget", "deadline",
                        )
                    ):
                        raise ValueError("H5 现场同步不得改变工作归属、预算或截止时间")
                    if synchronized != execution_work:
                        execution_work = synchronized
                        running_version = await self._queue.update_payload(
                            command=UpdateExplorerWorkPayloadCommand(
                                work=execution_work, lease_token=lease.lease_token,
                                row_version=running_version,
                            )
                        )
                selected = _selected_task_frontier_entry(execution_work)
                if (
                    selected is not None
                    and selected.source_state_version
                    != execution_work.frontier.observation.state_version
                    and (
                        _can_execute_global_bottom_navigation(
                            work=execution_work,
                            entry=selected,
                        )
                        or _can_execute_stable_same_source_page(
                            work=execution_work,
                            entry=selected,
                        )
                    )
                ):
                    execution_work = rebind_h5_selected_entry_to_current_observation(
                        execution_work
                    )
                    running_version = await self._queue.update_payload(
                        command=UpdateExplorerWorkPayloadCommand(
                            work=execution_work,
                            lease_token=lease.lease_token,
                            row_version=running_version,
                        )
                    )
                if (
                    isinstance(execution_work, ExplorationWork)
                    and self._recovery is not None
                    and not has_started_browser_attempt
                ):
                    selected = _selected_entry_requiring_recovery(execution_work)
                    if selected is not None:
                        recovery = await guard.run(
                            self._recovery.restore(
                                work=execution_work,
                                agent_run_id=agent_run_id,
                                entry=selected,
                            )
                        )
                        if not recovery.restored:
                            running_version = await self._queue.update_payload(
                                command=UpdateExplorerWorkPayloadCommand(
                                    work=recovery.work,
                                    lease_token=lease.lease_token,
                                    row_version=running_version,
                                )
                            )
                            exhausted = recovery.reason_code == "h5_frontier_restore_exhausted"
                            # 单条目恢复耗尽只损失该条目：更新后的载荷已清空选中
                            # 绑定，只要 Frontier 仍有 pending 或已改选的 sibling
                            # 就重领续跑，
                            # 而不是把整个 Run 有界收束。
                            bound_run = exhausted and not _has_continuable_task_frontier_entries(
                                recovery.work
                            )
                            target_status = (
                                ExplorerWorkStatus.BOUNDED
                                if bound_run
                                else ExplorerWorkStatus.RETRY_WAIT
                            )
                            await self._queue.transition(
                                command=TransitionExplorerWorkCommand(
                                    work_id=lease.work.work_id,
                                    expected_status=ExplorerWorkStatus.RUNNING,
                                    target_status=target_status,
                                    lease_token=lease.lease_token,
                                    row_version=running_version,
                                    terminal=(
                                        _bounded_terminal(
                                            lease.work,
                                            reason="H5 Frontier 来源恢复次数已耗尽",
                                            reason_code=recovery.reason_code,
                                        )
                                        if bound_run
                                        else None
                                    ),
                                    error_code=recovery.reason_code,
                                    error_message="H5 Frontier 来源恢复未完成",
                                    retry_delay_seconds=(
                                        0 if bound_run else self._retry_delay_seconds
                                    ),
                                    # 已改选到另一条 sibling：投递预算属于上一条，
                                    # 不重置会让 Frontier 剩几条就被 max_attempts
                                    # 掐死（实测 3 次预算只够试 1.5 条 sibling）。
                                    reset_attempt_count=(
                                        not bound_run and recovery.reselected_sibling
                                    ),
                                )
                            )
                            return H5ExplorationTurnOutcome(
                                claimed=True,
                                work_id=lease.work.work_id,
                                final_status=target_status,
                                error_code=recovery.reason_code,
                                reselected_sibling=(not bound_run and recovery.reselected_sibling),
                            )
                        running_version = await self._queue.update_payload(
                            command=UpdateExplorerWorkPayloadCommand(
                                work=recovery.work,
                                lease_token=lease.lease_token,
                                row_version=running_version,
                            )
                        )
                        if recovery.work.source_capture_id != execution_work.source_capture_id:
                            # 新恢复帧的现场规则尚未排空，先让外层检查点处理再重领。
                            await self._queue.transition(
                                command=TransitionExplorerWorkCommand(
                                    work_id=lease.work.work_id,
                                    expected_status=ExplorerWorkStatus.RUNNING,
                                    target_status=ExplorerWorkStatus.RETRY_WAIT,
                                    lease_token=lease.lease_token,
                                    row_version=running_version,
                                    retry_delay_seconds=0,
                                    reset_attempt_count=True,
                                )
                            )
                            return H5ExplorationTurnOutcome(
                                claimed=True,
                                work_id=lease.work.work_id,
                                final_status=ExplorerWorkStatus.RETRY_WAIT,
                                result_capture_id=recovery.work.source_capture_id,
                                restored_source=True,
                            )
                        execution_work = recovery.work
                started_attempt = execution_work.frontier.observation.metadata.get(
                    "h5_browser_recovery_attempt"
                )
                if (
                    isinstance(started_attempt, Mapping)
                    and started_attempt.get("status") == "completed"
                    and started_attempt.get("reason_code") is not None
                    and self._browser_loss_recovery is not None
                ):
                    previous_facts = H5BrowserRecoveryAttemptFacts.from_dict(started_attempt)
                    max_attempts = (
                        1 if previous_facts.failure_stage.value == "before_capture" else 2
                    )
                    if previous_facts.recovery_attempt_count >= max_attempts:
                        await self._queue.transition(
                            command=TransitionExplorerWorkCommand(
                                work_id=lease.work.work_id,
                                expected_status=ExplorerWorkStatus.RUNNING,
                                target_status=ExplorerWorkStatus.BOUNDED,
                                lease_token=lease.lease_token,
                                row_version=running_version,
                                terminal=_bounded_terminal(
                                    lease.work,
                                    reason="H5 浏览器恢复 attempt 已耗尽",
                                    reason_code="h5_browser_recovery_attempts_exhausted",
                                ),
                                error_code="h5_browser_recovery_attempts_exhausted",
                                error_message="H5 浏览器恢复 attempt 已耗尽",
                            )
                        )
                        return H5ExplorationTurnOutcome(
                            claimed=True,
                            work_id=lease.work.work_id,
                            final_status=ExplorerWorkStatus.BOUNDED,
                            error_code="h5_browser_recovery_attempts_exhausted",
                        )
                    followup_error = CaptureCoordinationError(
                        stage=CaptureCoordinationStage(previous_facts.failure_stage.value),
                        error_code=previous_facts.error_code,
                        message="继续持久化的 H5 浏览器恢复 attempt",
                        cause_type="PersistedBrowserLossAttempt",
                    )
                    reserve = getattr(
                        self._browser_loss_recovery,
                        "reserve_attempt",
                        None,
                    )
                    if not callable(reserve):
                        raise RuntimeError("H5 browser recovery 缺少 follow-up attempt 预留")
                    reservation = await guard.run(
                        reserve(
                            work=execution_work,
                            agent_run_id=agent_run_id,
                            error=followup_error,
                        )
                    )
                    execution_work = reservation.work
                    if reservation.reserved:
                        running_version = await self._queue.update_payload(
                            command=UpdateExplorerWorkPayloadCommand(
                                work=execution_work,
                                lease_token=lease.lease_token,
                                row_version=running_version,
                            )
                        )
                    started_attempt = execution_work.frontier.observation.metadata.get(
                        "h5_browser_recovery_attempt"
                    )
                if (
                    isinstance(started_attempt, Mapping)
                    and started_attempt.get("status") == "started"
                    and self._browser_loss_recovery is not None
                ):
                    stage = CaptureCoordinationStage(str(started_attempt.get("failure_stage")))
                    resumed_error = CaptureCoordinationError(
                        stage=stage,
                        error_code=str(started_attempt.get("error_code") or "browser_session_lost"),
                        message="恢复持久化的 H5 浏览器丢失 attempt",
                        cause_type="PersistedBrowserLossAttempt",
                    )
                    recovery_outcome = await guard.run(
                        self._browser_loss_recovery.recover(
                            work=execution_work,
                            agent_run_id=agent_run_id,
                            error=resumed_error,
                            attempt_reserved=True,
                        )
                    )
                    recovered_work = recovery_outcome.recovery_work
                    if recovered_work is None or recovery_outcome.reason_code is not None:
                        completed_work = _complete_browser_attempt(
                            execution_work,
                            recovery_outcome.reason_code,
                        )
                        if completed_work is not execution_work:
                            running_version = await self._queue.update_payload(
                                command=UpdateExplorerWorkPayloadCommand(
                                    work=completed_work,
                                    lease_token=lease.lease_token,
                                    row_version=running_version,
                                )
                            )
                        await self._queue.transition(
                            command=TransitionExplorerWorkCommand(
                                work_id=lease.work.work_id,
                                expected_status=ExplorerWorkStatus.RUNNING,
                                target_status=ExplorerWorkStatus.BOUNDED,
                                lease_token=lease.lease_token,
                                row_version=running_version,
                                terminal=_bounded_terminal(
                                    lease.work,
                                    reason="H5 浏览器恢复重放未通过安全门禁",
                                    reason_code=recovery_outcome.reason_code,
                                ),
                                error_code=recovery_outcome.reason_code,
                                error_message="H5 浏览器恢复重放未通过安全门禁",
                            )
                        )
                        return H5ExplorationTurnOutcome(
                            claimed=True,
                            work_id=lease.work.work_id,
                            final_status=ExplorerWorkStatus.BOUNDED,
                            error_code=recovery_outcome.reason_code,
                        )
                    await self._raise_if_canceled(
                        task_id=task_id,
                        agent_run_id=agent_run_id,
                        stage="before_successor",
                    )
                    return await self._commit_or_settle_recovery_deadline(
                        guard=guard,
                        agent_run_id=agent_run_id,
                        command=CommitExplorerWorkSuccessorCommand(
                            parent_work_id=lease.work.work_id,
                            task_id=lease.work.task_id,
                            session_id=lease.work.session_id,
                            lease_owner=self._lease_owner,
                            lease_token=lease.lease_token,
                            row_version=running_version,
                            successor=recovered_work,
                            parent_target_status=ExplorerWorkStatus.SUPERSEDED,
                        ),
                        result_capture_id=recovered_work.source_capture_id,
                        error_code=resumed_error.error_code,
                    )
                try:
                    if self._adaptive_journey is not None:
                        prepared = await guard.run(self._adaptive_journey.prepare(
                            work=execution_work, agent_run_id=agent_run_id,
                        ))
                        if prepared != execution_work:
                            running_version = await self._queue.update_payload(
                                command=UpdateExplorerWorkPayloadCommand(
                                    work=prepared, lease_token=lease.lease_token,
                                    row_version=running_version,
                                )
                            )
                            execution_work = prepared
                    turn = await guard.run(
                        self._adaptive_journey.execute(
                            work=execution_work,
                            agent_run_id=agent_run_id,
                            executor=self._turn_executor,
                        )
                        if self._adaptive_journey is not None
                        else self._turn_executor.execute(
                            work=execution_work, agent_run_id=agent_run_id,
                        )
                    )
                except CaptureCoordinationError as exc:
                    # 协调器返回前取消可能已经生效；取消必须优先于采集故障恢复和失败计数。
                    await self._raise_if_canceled(
                        task_id=task_id,
                        agent_run_id=agent_run_id,
                        stage="after_capture_coordination_error",
                    )
                    if _is_action_reobservation(exc):
                        assert exc.before_capture is not None
                        assert exc.interaction is not None
                        await self._raise_if_canceled(
                            task_id=task_id,
                            agent_run_id=agent_run_id,
                            stage="before_action_reobservation",
                        )
                        successor = await guard.run(
                            self._continuation.prepare_after_action_failure(
                                ReobserveH5ExplorationCommand(
                                    prior_work=execution_work,
                                    before_capture=exc.before_capture,
                                    error_code=exc.error_code,
                                    retryable=exc.interaction.retryable,
                                )
                            )
                        )
                        _validate_action_reobservation_successor(
                            prior_work=execution_work,
                            source_capture_id=exc.before_capture.capture_id,
                            successor=successor,
                        )
                        await self._raise_if_canceled(
                            task_id=task_id,
                            agent_run_id=agent_run_id,
                            stage="before_successor",
                        )
                        return await self._commit_or_settle_deadline(
                            guard=guard,
                            agent_run_id=agent_run_id,
                            command=CommitExplorerWorkSuccessorCommand(
                                parent_work_id=lease.work.work_id,
                                task_id=lease.work.task_id,
                                session_id=lease.work.session_id,
                                lease_owner=self._lease_owner,
                                lease_token=lease.lease_token,
                                row_version=running_version,
                                successor=successor.work,
                                parent_target_status=ExplorerWorkStatus.SUPERSEDED,
                            ),
                            result_capture_id=exc.before_capture.capture_id,
                            error_code=exc.error_code,
                        )
                    if (
                        not _requires_safe_session_rebaseline(exc)
                        or self._browser_loss_recovery is None
                    ):
                        raise
                    reserve = getattr(self._browser_loss_recovery, "reserve_attempt", None)
                    attempt_reserved = False
                    if callable(reserve):
                        reservation = await guard.run(
                            reserve(work=execution_work, agent_run_id=agent_run_id, error=exc)
                        )
                        execution_work = reservation.work
                        if reservation.reserved:
                            running_version = await self._queue.update_payload(
                                command=UpdateExplorerWorkPayloadCommand(
                                    work=execution_work,
                                    lease_token=lease.lease_token,
                                    row_version=running_version,
                                )
                            )
                        attempt_reserved = reservation.reserved
                    recovery_call = self._browser_loss_recovery.recover(
                        work=execution_work,
                        agent_run_id=agent_run_id,
                        error=exc,
                        **({"attempt_reserved": attempt_reserved} if callable(reserve) else {}),
                    )
                    recovery_outcome = await guard.run(recovery_call)
                    recovered_work = recovery_outcome.recovery_work
                    if recovered_work is None:
                        target_status = ExplorerWorkStatus.BOUNDED
                        completed_work = _complete_browser_attempt(
                            execution_work,
                            recovery_outcome.reason_code,
                        )
                        if completed_work is not execution_work:
                            running_version = await self._queue.update_payload(
                                command=UpdateExplorerWorkPayloadCommand(
                                    work=completed_work,
                                    lease_token=lease.lease_token,
                                    row_version=running_version,
                                )
                            )
                        await self._queue.transition(
                            command=TransitionExplorerWorkCommand(
                                work_id=lease.work.work_id,
                                expected_status=ExplorerWorkStatus.RUNNING,
                                target_status=target_status,
                                lease_token=lease.lease_token,
                                row_version=running_version,
                                terminal=_bounded_terminal(
                                    lease.work,
                                    reason="H5 浏览器恢复未通过安全门禁",
                                    reason_code=recovery_outcome.reason_code,
                                ),
                                error_code=recovery_outcome.reason_code,
                                error_message="H5 浏览器恢复未通过安全门禁",
                            )
                        )
                        return H5ExplorationTurnOutcome(
                            claimed=True,
                            work_id=lease.work.work_id,
                            final_status=target_status,
                            error_code=recovery_outcome.reason_code,
                        )
                    if recovery_outcome.reason_code is not None:
                        if recovered_work.work_id != lease.work.work_id:
                            raise ValueError("失败恢复事实必须写回原 Work 身份") from exc
                        running_version = await self._queue.update_payload(
                            command=UpdateExplorerWorkPayloadCommand(
                                work=recovered_work,
                                lease_token=lease.lease_token,
                                row_version=running_version,
                            )
                        )
                        await self._queue.transition(
                            command=TransitionExplorerWorkCommand(
                                work_id=lease.work.work_id,
                                expected_status=ExplorerWorkStatus.RUNNING,
                                target_status=ExplorerWorkStatus.BOUNDED,
                                lease_token=lease.lease_token,
                                row_version=running_version,
                                terminal=_bounded_terminal(
                                    lease.work,
                                    reason="H5 浏览器恢复未通过安全门禁",
                                    reason_code=recovery_outcome.reason_code,
                                ),
                                error_code=recovery_outcome.reason_code,
                                error_message="H5 浏览器恢复未通过安全门禁",
                            )
                        )
                        return H5ExplorationTurnOutcome(
                            claimed=True,
                            work_id=lease.work.work_id,
                            final_status=ExplorerWorkStatus.BOUNDED,
                            error_code=recovery_outcome.reason_code,
                        )
                    if recovery_outcome.retry_original_action:
                        if recovered_work.work_id == lease.work.work_id:
                            raise ValueError("BEFORE_CAPTURE 恢复必须发布独立 retry Work") from exc
                        await self._raise_if_canceled(
                            task_id=task_id,
                            agent_run_id=agent_run_id,
                            stage="before_successor",
                        )
                        return await self._commit_or_settle_recovery_deadline(
                            guard=guard,
                            agent_run_id=agent_run_id,
                            command=CommitExplorerWorkSuccessorCommand(
                                parent_work_id=lease.work.work_id,
                                task_id=lease.work.task_id,
                                session_id=lease.work.session_id,
                                lease_owner=self._lease_owner,
                                lease_token=lease.lease_token,
                                row_version=running_version,
                                successor=recovered_work,
                                parent_target_status=ExplorerWorkStatus.SUPERSEDED,
                            ),
                            result_capture_id=recovered_work.source_capture_id,
                            error_code=exc.error_code,
                        )
                    elif recovery_outcome.supersede_failed_work:
                        await self._raise_if_canceled(
                            task_id=task_id,
                            agent_run_id=agent_run_id,
                            stage="before_successor",
                        )
                        return await self._commit_or_settle_recovery_deadline(
                            guard=guard,
                            agent_run_id=agent_run_id,
                            command=CommitExplorerWorkSuccessorCommand(
                                parent_work_id=lease.work.work_id,
                                task_id=lease.work.task_id,
                                session_id=lease.work.session_id,
                                lease_owner=self._lease_owner,
                                lease_token=lease.lease_token,
                                row_version=running_version,
                                successor=recovered_work,
                                parent_target_status=ExplorerWorkStatus.SUPERSEDED,
                            ),
                            result_capture_id=recovered_work.source_capture_id,
                            error_code=exc.error_code,
                        )
                    else:
                        raise RuntimeError("H5 浏览器恢复返回了不一致结果") from exc
                await guard.assert_healthy()
                await self._raise_if_canceled(
                    task_id=task_id,
                    agent_run_id=agent_run_id,
                    stage="after_capture_commit",
                )
                target_status, terminal = _successful_target(turn)
                result_capture_id = (
                    turn.coordination.after_capture.capture_id
                    if turn.coordination is not None
                    else None
                )
                successor_work_id = None
                if not turn.is_terminal:
                    await self._raise_if_canceled(
                        task_id=task_id,
                        agent_run_id=agent_run_id,
                        stage="before_successor",
                    )
                    successor = await guard.run(
                        self._continuation.prepare_after_action(
                            ContinueH5ExplorationCommand(
                                prior_work=execution_work,
                                turn=turn,
                            )
                        )
                    )
                    await guard.assert_healthy()
                    _validate_successor(
                        prior_work=execution_work,
                        turn=turn,
                        successor=successor,
                    )
                    await self._raise_if_canceled(
                        task_id=task_id,
                        agent_run_id=agent_run_id,
                        stage="before_successor",
                    )
                    return await self._commit_or_settle_deadline(
                        guard=guard,
                        agent_run_id=agent_run_id,
                        command=CommitExplorerWorkSuccessorCommand(
                            parent_work_id=lease.work.work_id,
                            task_id=lease.work.task_id,
                            session_id=lease.work.session_id,
                            lease_owner=self._lease_owner,
                            lease_token=lease.lease_token,
                            row_version=running_version,
                            successor=successor.work,
                        ),
                        turn=turn,
                        result_capture_id=result_capture_id,
                    )
                else:
                    # 带有真实动作协调结果的直接终态也要把最后一项动作和
                    # after Capture 写入摘要；纯终态观察不虚构动作消费。
                    if terminal is not None and turn.coordination is not None:
                        terminal = _terminal_with_action_result(
                            terminal=terminal,
                            work=execution_work,
                            result_capture_id=result_capture_id,
                        )
                        # 只有原本的 COMPLETED 可以因 deadline 改成预算终态；
                        # BOUNDED/BLOCKED/NO_PROGRESS 等严重语义必须保留。
                        if (
                            terminal.status is ExplorationTerminalStatus.COMPLETED
                            and execution_work.deadline <= datetime.now(UTC)
                        ):
                            terminal = _terminal_after_deadline_action(
                                terminal=terminal,
                            )
                            target_status = ExplorerWorkStatus.BUDGET_EXHAUSTED
                        turn = replace(turn, decision=terminal)
                    await self._queue.transition(
                        command=TransitionExplorerWorkCommand(
                            work_id=lease.work.work_id,
                            expected_status=ExplorerWorkStatus.RUNNING,
                            target_status=target_status,
                            lease_token=lease.lease_token,
                            row_version=running_version,
                            terminal=terminal,
                        )
                    )
                return H5ExplorationTurnOutcome(
                    claimed=True,
                    work_id=lease.work.work_id,
                    final_status=target_status,
                    turn=turn,
                    result_capture_id=result_capture_id,
                    successor_work_id=successor_work_id,
                )
        except H5ExplorationCanceled:
            await self.cancel_nonterminal(task_id=task_id)
            return H5ExplorationTurnOutcome(
                claimed=True,
                work_id=lease.work.work_id,
                final_status=ExplorerWorkStatus.CANCELLED,
                error_code=H5ExplorationCanceled.error_code,
            )
        except AppExplorationLeaseLostError:
            raise
        except Exception as exc:  # noqa: BLE001 - 失败必须先归还持久化工作租约
            target_status = _failure_target(exc)
            try:
                attempt = _validated_browser_attempt(execution_work, agent_run_id)
            except (TypeError, ValueError):
                # 损坏或异属记录不能伪装为可重试恢复，更不能在异常收口中再次解码失败。
                attempt = None
                target_status = ExplorerWorkStatus.FAILED
            if (attempt is not None and attempt.status == "started"
                    and not isinstance(exc, (H5SessionJourneyDeadline, H5SessionJourneyLost))):
                target_status = ExplorerWorkStatus.RETRY_WAIT
            retryable = target_status is ExplorerWorkStatus.RETRY_WAIT
            error_code = _safe_error_code(exc)
            completed_work = (
                _complete_browser_attempt(execution_work, "h5_browser_recovery_side_effect_failed")
                if attempt is not None else execution_work
            )
            if completed_work is not execution_work:
                running_version = await self._queue.update_payload(
                    command=UpdateExplorerWorkPayloadCommand(
                        work=completed_work,
                        lease_token=lease.lease_token,
                        row_version=running_version,
                    )
                )
            await self._queue.transition(
                command=TransitionExplorerWorkCommand(
                    work_id=lease.work.work_id,
                    expected_status=ExplorerWorkStatus.RUNNING,
                    target_status=target_status,
                    lease_token=lease.lease_token,
                    row_version=running_version,
                    terminal=None,
                    error_code=error_code,
                    error_message=f"H5 Explorer 单轮执行失败: {type(exc).__name__}: {exc}"[:500],
                    retry_delay_seconds=(self._retry_delay_seconds if retryable else 0),
                )
            )
            return H5ExplorationTurnOutcome(
                claimed=True,
                work_id=lease.work.work_id,
                final_status=target_status,
                error_code=error_code,
            )

    async def _commit_or_settle_recovery_deadline(
        self,
        *,
        guard: AppExplorationLeaseGuard,
        agent_run_id: str,
        command: CommitExplorerWorkSuccessorCommand,
        result_capture_id: str | None = None,
        error_code: str | None = None,
    ) -> H5ExplorationTurnOutcome:
        """恢复跨截止时只收束父 Work，不把跨 Session 结果发布为正式后继。"""
        return await self._commit_or_settle_deadline(
            guard=guard,
            agent_run_id=agent_run_id,
            command=command,
            result_capture_id=result_capture_id,
            error_code=error_code,
            settle_result_work=False,
        )

    async def _commit_or_settle_deadline(
        self,
        *,
        guard: AppExplorationLeaseGuard,
        agent_run_id: str,
        command: CommitExplorerWorkSuccessorCommand,
        turn: ExplorerTurnResult | None = None,
        result_capture_id: str | None = None,
        error_code: str | None = None,
        settle_result_work: bool = True,
    ) -> H5ExplorationTurnOutcome:
        """已完成的一轮即使跨过截止，也只结算当前 Work，不发布过期后继。"""
        await guard.assert_healthy()
        if command.successor.deadline > datetime.now(UTC):
            try:
                committed = await self._queue.commit_successor(
                    command=command,
                    emit_wakeup=self._emit_wakeup,
                )
            except ExplorerWorkDeadlineExceededError:
                # DB 提交也可能跨越截止；仅接受明确的截止拒绝，不吞其他持久化错误。
                pass
            else:
                return H5ExplorationTurnOutcome(
                    claimed=True,
                    work_id=command.parent_work_id,
                    final_status=command.parent_target_status,
                    turn=turn,
                    result_capture_id=result_capture_id,
                    successor_work_id=committed.successor_work_id,
                    error_code=error_code,
                )
        await guard.assert_healthy()
        await self._raise_if_canceled(
            task_id=command.task_id,
            agent_run_id=agent_run_id,
            stage="before_deadline_settlement",
        )
        terminal = with_terminal_settlement(
            ExplorationTerminal(
                work_id=command.parent_work_id,
                task_id=command.task_id,
                status=ExplorationTerminalStatus.BUDGET_EXHAUSTED,
                reason="Explorer Work deadline 已到，已结算本轮结果并停止探索。",
                outcome="bounded",
                reason_code="h5_work_deadline_reached",
                coverage_summary={"budget_exhaustion_reason": "deadline_reached"},
            ),
            result_work=command.successor,
        )
        terminal_turn = (
            replace(turn, decision=terminal)
            if turn is not None
            else ExplorerTurnResult(
                work_id=command.parent_work_id,
                task_id=command.task_id,
                decision=terminal,
                coordination=None,
            )
        )
        await self._queue.transition(
            command=TransitionExplorerWorkCommand(
                work_id=command.parent_work_id,
                expected_status=ExplorerWorkStatus.RUNNING,
                target_status=ExplorerWorkStatus.BUDGET_EXHAUSTED,
                lease_token=command.lease_token,
                row_version=command.row_version,
                terminal=terminal,
                settlement_work=(command.successor if settle_result_work else None),
                error_code=error_code,
            )
        )
        return H5ExplorationTurnOutcome(
            claimed=True,
            work_id=command.parent_work_id,
            final_status=ExplorerWorkStatus.BUDGET_EXHAUSTED,
            turn=terminal_turn,
            result_capture_id=result_capture_id,
            error_code=error_code,
        )

    async def cancel_nonterminal(
        self,
        *,
        task_id: str,
        reason: str = "H5 探索收到统一 Run 取消信号",
    ) -> int:
        cancel = getattr(self._queue, "cancel_nonterminal", None)
        if not callable(cancel):
            return 0
        return int(
            await cancel(
                task_id=task_id,
                reason=reason,
                work_types=(ExplorerWorkType.EXPLORATION,),
                h5_only=True,
            )
        )

    async def reconcile_terminal_frontier(self, *, task_id: str) -> bool:
        """探索工作全部终态后，将最终 Frontier 与 Coverage 收敛到同一事实。"""

        reconcile = getattr(self._queue, "reconcile_h5_terminal_frontier", None)
        if not callable(reconcile):
            return False
        return bool(await reconcile(task_id=task_id))

    async def next_claim_at(self, *, task_id: str) -> datetime | None:
        """读取 PostgreSQL 中当前 H5 Work 的下一次安全重领时间。"""
        return await self._queue.next_claim_at(
            task_id=task_id,
            work_type=ExplorerWorkType.EXPLORATION,
        )

    async def _raise_if_canceled(
        self,
        *,
        task_id: str,
        agent_run_id: str,
        stage: str,
    ) -> None:
        await self._cancellation.raise_if_requested(
            task_id=task_id,
            agent_run_id=agent_run_id,
            stage=stage,
        )


def _validate_h5_work(work: object) -> None:
    if not isinstance(work, ExplorationWork):
        raise ValueError("H5 Runner 只消费 Exploration Work")
    if work.surface_kind is not SurfaceKind.WEB:
        raise ValueError("H5 Runner 只消费 Web Exploration Work")


def _has_continuable_task_frontier_entries(work: ExplorationWork) -> bool:
    """判断更新后的任务 Frontier 是否仍有 pending 或已改选的条目。"""
    payload = work.frontier.observation.metadata.get(H5_TASK_FRONTIER_METADATA_KEY)
    if not isinstance(payload, Mapping):
        return False
    snapshot = H5TaskFrontierSnapshot.from_dict(payload)
    return any(
        entry.status in {H5FrontierEntryStatus.PENDING, H5FrontierEntryStatus.SELECTED}
        for entry in snapshot.entries
    )


def _selected_entry_requiring_recovery(work: ExplorationWork):
    payload = work.frontier.observation.metadata.get(H5_TASK_FRONTIER_METADATA_KEY)
    if not isinstance(payload, Mapping):
        return None
    snapshot = H5TaskFrontierSnapshot.from_dict(payload)
    selected = [
        entry for entry in snapshot.entries if entry.status is H5FrontierEntryStatus.SELECTED
    ]
    selected_entry_id = work.frontier.observation.metadata.get("h5_task_frontier_selected_entry_id")
    if not selected and selected_entry_id is None and not work.frontier.candidates:
        return None
    if len(selected) != 1 or selected[0].entry_id != selected_entry_id:
        raise ValueError("H5 Work 的 selected Frontier entry 不唯一或不一致")
    if tuple(work.frontier.candidates) != (selected[0].candidate,):
        raise ValueError("H5 Work 当前候选未绑定 selected Frontier entry")
    if _can_execute_global_bottom_navigation(
        work=work,
        entry=selected[0],
    ):
        return None
    if work.frontier.observation.metadata.get("h5_frontier_source_recovery_pending") is True:
        return selected[0]
    if _can_execute_stable_same_source_page(
        work=work,
        entry=selected[0],
    ):
        return None
    if selected[0].source_state_version == work.frontier.observation.state_version:
        return None
    return selected[0]


def _selected_task_frontier_entry(work: ExplorationWork):
    payload = work.frontier.observation.metadata.get(H5_TASK_FRONTIER_METADATA_KEY)
    if not isinstance(payload, Mapping):
        return None
    snapshot = H5TaskFrontierSnapshot.from_dict(payload)
    selected = tuple(
        entry for entry in snapshot.entries if entry.status is H5FrontierEntryStatus.SELECTED
    )
    if not selected:
        return None
    if len(selected) != 1:
        raise ValueError("H5 Work 的 selected Frontier entry 不唯一")
    return selected[0]


def _can_execute_global_bottom_navigation(*, work: ExplorationWork, entry: object) -> bool:
    """当前稳定业务页可直接复核全局底栏，不回放会重触发开屏的旧 URL。"""
    candidate = getattr(entry, "candidate", None)
    source_location = getattr(entry, "source_location", None)
    if candidate is None:
        return False
    parameters = candidate.metadata.get("action_parameters")
    if (
        not isinstance(parameters, Mapping)
        or parameters.get("target_mode") != "bottom_navigation"
    ):
        return False
    navigation_index = parameters.get("navigation_index")
    navigation_label = parameters.get("navigation_label")
    navigation_family = h5_bottom_navigation_family(parameters.get("navigation_family"))
    expected_target = (
        h5_bottom_navigation_selector(
            family=navigation_family,
            index=navigation_index,
        )
        if navigation_family is not None
        and isinstance(navigation_index, int)
        and not isinstance(navigation_index, bool)
        and 1 <= navigation_index <= 50
        else None
    )
    try:
        expected_parameters = (
            h5_bottom_navigation_action_parameters(
                family=navigation_family,
                index=navigation_index,
                label=navigation_label,
                interaction_pattern_signature=parameters.get("interaction_pattern_signature"),
            )
            if expected_target is not None and isinstance(navigation_label, str)
            else None
        )
    except ValueError:
        return False
    observation = work.frontier.observation
    page_type = (observation.page_type or "").casefold()
    page_state = observation.page_state.casefold()
    current_navigation_indices = observation.metadata.get("global_bottom_navigation_indices")
    current_capture_proves_navigation = bool(
        isinstance(current_navigation_indices, (list, tuple))
        and navigation_index in current_navigation_indices
    )
    current_targets = observation.metadata.get("global_bottom_navigation_targets")
    freshly_proved_target = bool(
        isinstance(current_targets, (list, tuple))
        and sum(
            isinstance(target, Mapping)
            and target.get("semantic_target") == candidate.semantic_target
            and target.get("action_parameters") == dict(parameters)
            for target in current_targets
        ) == 1
    )
    independent_navigation = bool(
        candidate.metadata.get("independent_bottom_navigation") is True
        and freshly_proved_target
        and observation.metadata.get("captcha_verification_required") is not True
    )
    try:
        source_url = urlsplit(source_location) if isinstance(source_location, str) else None
        current_url = urlsplit(observation.location)
        same_origin = bool(
            source_url is not None
            and source_url.scheme.casefold() == current_url.scheme.casefold()
            and source_url.hostname == current_url.hostname
            and source_url.port == current_url.port
        )
    except ValueError:
        same_origin = False
    terminal_ambiguity_without_side_effect = bool(
        page_type == ""
        and page_state == "stable"
        and same_origin
        and (
            observation.metadata.get("h5_frontier_source_recovery_pending") is not True
            or freshly_proved_target
        )
        and observation.metadata.get("action_reobserve_error_code") == "SEMANTIC_TARGET_AMBIGUOUS"
        and observation.metadata.get("action_reobservation_retryable") is False
        and isinstance(
            observation.metadata.get("action_reobservation_candidate_identity"),
            Mapping,
        )
    )
    return (
        expected_parameters is not None
        and dict(parameters) == expected_parameters
        and isinstance(navigation_index, int)
        and not isinstance(navigation_index, bool)
        and 1 <= navigation_index <= 50
        and isinstance(navigation_label, str)
        and bool(navigation_label.strip())
        and candidate.semantic_target == expected_target
        and candidate.action_type is InteractionOperation.CLICK
        and candidate.risk_level is InteractionRiskLevel.LOW
        and candidate.navigation_depth == 1
        and same_origin
        and (
            observation.metadata.get("h5_frontier_source_recovery_pending") is not True
            or freshly_proved_target
        )
        and (
            freshly_proved_target
            or candidate.metadata.get("navigation_knowledge_source")
            in {"static", "exact", "vector", "contextual"}
        )
        and (
            (
                page_type not in {"", "line_loading", "startup_ad"}
                and current_capture_proves_navigation
            )
            or terminal_ambiguity_without_side_effect
        )
        and (
            page_state in {"stable", "loading"}
            or (
                independent_navigation
                and page_state
                in {"popup", "entitlement_locked", "modal", "overlay", "dialog", "obstructed"}
            )
            or (
                page_state == "empty"
                and observation.metadata.get("detection_status") == "recognized"
            )
        )
        and (observation.overlay_signature is None or independent_navigation)
        and observation.metadata.get("requires_auth") is not True
    )


def _can_execute_stable_same_source_page(
    *,
    work: ExplorationWork,
    entry: object,
) -> bool:
    """子页返回到同 URL 同页型后复用当前可信 Capture，避免重复导航触发开屏。"""
    if work.frontier.observation.metadata.get("h5_frontier_source_recovery_pending") is True:
        return False
    candidate = getattr(entry, "candidate", None)
    source_location = getattr(entry, "source_location", None)
    if candidate is None or not isinstance(source_location, str):
        return False
    observation = work.frontier.observation
    source_page_type = candidate.metadata.get("source_page_type")
    current_page_type = observation.page_type
    trusted_list_detail = _trusted_same_page_list_detail_candidate(
        work=work,
        candidate=candidate,
    )
    trusted_root_sibling = bool(
        candidate.navigation_depth == 0
        and getattr(entry, "navigation_depth", None) == 0
        and candidate.metadata.get("source_page_state") == "stable"
    )
    reusable_risk = candidate.risk_level is InteractionRiskLevel.LOW or (
        _is_profile_payment_visual_candidate(
            candidate,
            source_location=source_location,
        )
    )
    try:
        same_location = canonical_h5_route_identity(source_location) == canonical_h5_route_identity(
            observation.location
        )
    except ValueError:
        return False
    source_identity = candidate.metadata.get("source_identity_fingerprint")
    current_identity = observation.metadata.get("logical_surface_identity_fingerprint")
    if (source_identity or current_identity) and source_identity != current_identity:
        return False
    return bool(
        same_location
        and isinstance(source_page_type, str)
        and source_page_type.strip()
        and isinstance(current_page_type, str)
        and current_page_type.casefold() == source_page_type.casefold()
        and observation.page_state.casefold() == "stable"
        and observation.overlay_signature is None
        and observation.metadata.get("detection_status") == "recognized"
        and observation.metadata.get("requires_auth") is not True
        and candidate.action_type is InteractionOperation.CLICK
        and reusable_risk
        and (
            trusted_root_sibling
            or candidate.navigation_depth == 1
            or (trusted_list_detail and candidate.navigation_depth == 2)
        )
        and isinstance(candidate.metadata.get("expected_next_page_type"), str)
    )


def _trusted_same_page_list_detail_candidate(
    *,
    work: ExplorationWork,
    candidate: object,
) -> bool:
    """仅认可页面检测层签发且仍与当前稳定列表匹配的首卡能力。"""
    metadata = getattr(candidate, "metadata", None)
    if not isinstance(metadata, Mapping):
        return False
    parameters = metadata.get("action_parameters")
    if not isinstance(parameters, Mapping):
        return False
    selector = parameters.get("list_detail_selector")
    source_page_type = parameters.get("source_page_type")
    expected_page_type = parameters.get("expected_page_type")
    if not all(
        isinstance(value, str) and value.strip()
        for value in (selector, source_page_type, expected_page_type)
    ):
        return False
    assert isinstance(selector, str)
    assert isinstance(source_page_type, str)
    assert isinstance(expected_page_type, str)
    keywords = metadata.get("target_keywords")
    normalized_keywords = (
        {str(item).strip() for item in keywords if str(item).strip()}
        if isinstance(keywords, (list, tuple, set, frozenset))
        else set()
    )
    return bool(
        dict(parameters)
        == {
            "target_mode": "list_detail_navigation",
            "source_page_type": source_page_type,
            "expected_page_type": expected_page_type,
            "list_detail_selector": selector,
        }
        and source_page_type == (work.frontier.observation.page_type or "").casefold()
        and expected_page_type == h5_list_detail_expected_page_type(source_page_type)
        and h5_list_detail_selector_allowed(
            source_page_type=source_page_type,
            selector=selector,
        )
        and work.frontier.observation.page_state.casefold()
        in {"stable", "normal", "feed", "loaded", "notice"}
        and getattr(candidate, "semantic_target", None) == H5_LIST_DETAIL_TARGET
        and getattr(candidate, "action_type", None) is InteractionOperation.CLICK
        and getattr(candidate, "risk_level", None) is InteractionRiskLevel.LOW
        and getattr(candidate, "navigation_depth", None) == 2
        and metadata.get("expected_next_page_type") == expected_page_type
        and {"nested_content", "list_detail_navigation"}.issubset(normalized_keywords)
    )


def _is_profile_payment_visual_candidate(
    candidate: object,
    *,
    source_location: str,
) -> bool:
    """仅放行已签名截图坐标的个人中心入口，区域一致性仍由 Runtime 重验。"""
    metadata = getattr(candidate, "metadata", None)
    if not isinstance(metadata, Mapping):
        return False
    parameters = metadata.get("action_parameters")
    keywords = metadata.get("target_keywords")
    normalized_keywords = (
        {str(item).strip() for item in keywords if str(item).strip()}
        if isinstance(keywords, (list, tuple, set, frozenset))
        else set()
    )
    visual_source_url = (
        parameters.get("visual_source_url") if isinstance(parameters, Mapping) else None
    )
    contract = h5_profile_visual_entry_contract(
        getattr(candidate, "semantic_target", None),
        entry_kind=(
            parameters.get("visual_entry_kind") if isinstance(parameters, Mapping) else None
        ),
    )
    try:
        same_visual_location = isinstance(visual_source_url, str) and urlsplit(
            visual_source_url
        )._replace(fragment="") == urlsplit(source_location)._replace(fragment="")
    except ValueError:
        same_visual_location = False
    return bool(
        getattr(candidate, "risk_level", None) is InteractionRiskLevel.MEDIUM
        and contract is not None
        and metadata.get("source_page_type") == "profile"
        and metadata.get("expected_next_page_type") == contract.expected_page_type
        and {
            contract.target_keyword,
            "profile_local_action",
            f"page_type:{contract.expected_page_type}",
            f"entry:{contract.target}",
            "evidence:screenshot",
        }.issubset(normalized_keywords)
        and isinstance(parameters, Mapping)
        and parameters.get("target_mode") == "visual_coordinate"
        and parameters.get("visual_entry_kind") == contract.entry_kind
        and isinstance(parameters.get("visual_source_capture_id"), str)
        and bool(str(parameters.get("visual_source_capture_id")).strip())
        and isinstance(parameters.get("visual_source_artifact_id"), str)
        and bool(str(parameters.get("visual_source_artifact_id")).strip())
        and isinstance(visual_source_url, str)
        and bool(visual_source_url.strip())
        and same_visual_location
    )


def _validate_successor(
    *,
    prior_work: ExplorationWork,
    turn: ExplorerTurnResult,
    successor: H5ExplorationResult,
) -> None:
    if not isinstance(successor, H5ExplorationResult):
        raise TypeError("H5 后继发布器返回了无效结果")
    coordination = turn.coordination
    if coordination is None:
        raise ValueError("H5 动作轮缺少 Capture Coordination 结果")
    work = successor.work
    if (
        work.task_id != prior_work.task_id
        or work.session_id != prior_work.session_id
        or work.surface_kind is not SurfaceKind.WEB
        or work.source_capture_id != coordination.after_capture.capture_id
        or work.work_id == prior_work.work_id
    ):
        raise ValueError("H5 后继 Work 与动作轮身份不一致")
    _validate_task_frontier_successor(work)


def _validate_task_frontier_successor(work: ExplorationWork) -> None:
    """校验全局 Frontier 与当前可执行局部候选始终指向同一 selected entry。"""
    payload = work.frontier.observation.metadata.get(H5_TASK_FRONTIER_METADATA_KEY)
    if payload is None:
        return
    if not isinstance(payload, Mapping):
        raise ValueError("H5 后继 Work 的任务 Frontier 快照无效")
    snapshot = H5TaskFrontierSnapshot.from_dict(payload)
    selected = [
        entry for entry in snapshot.entries if entry.status is H5FrontierEntryStatus.SELECTED
    ]
    if len(selected) > 1:
        raise ValueError("H5 任务 Frontier 不能同时存在多个 selected entry")
    if len(work.frontier.candidates) != len(selected):
        raise ValueError("H5 后继 Work 与任务 Frontier selected entry 不一致")
    if selected and work.frontier.candidates[0] != selected[0].candidate:
        raise ValueError("H5 后继 Work 未暴露任务 Frontier 的 selected candidate")


def _successful_target(
    turn: ExplorerTurnResult,
) -> tuple[ExplorerWorkStatus, ExplorationTerminal | None]:
    if isinstance(turn.decision, ExplorationTerminal):
        target_by_terminal = {
            ExplorationTerminalStatus.COMPLETED: ExplorerWorkStatus.SUCCEEDED,
            ExplorationTerminalStatus.BOUNDED: ExplorerWorkStatus.BOUNDED,
            ExplorationTerminalStatus.BUDGET_EXHAUSTED: ExplorerWorkStatus.BUDGET_EXHAUSTED,
            ExplorationTerminalStatus.BLOCKED: ExplorerWorkStatus.BLOCKED,
            ExplorationTerminalStatus.NO_PROGRESS: ExplorerWorkStatus.NO_PROGRESS,
        }
        return target_by_terminal[turn.decision.status], turn.decision
    if turn.coordination is None:
        raise ValueError("动作决策必须返回 Capture Coordination 结果")
    return ExplorerWorkStatus.SUCCEEDED, None


def _terminal_after_deadline_action(
    *,
    terminal: ExplorationTerminal,
) -> ExplorationTerminal:
    """动作已有协调结果但收尾越过 deadline 时，改成预算终态。"""
    return replace(
        terminal,
        status=ExplorationTerminalStatus.BUDGET_EXHAUSTED,
        reason="Explorer Work deadline 已到，已保留本轮动作结果并停止探索。",
        outcome="bounded",
        reason_code="h5_work_deadline_reached",
    )


def _terminal_with_action_result(
    *,
    terminal: ExplorationTerminal,
    work: ExplorationWork,
    result_capture_id: str | None,
) -> ExplorationTerminal:
    """直接终态结算最后一项动作，并拒绝超出锁定预算的事实。"""
    prior_consumed = work.budget.max_actions - work.budget.remaining_actions
    consumed_action_count = prior_consumed + 1
    if not (
        prior_consumed <= consumed_action_count <= min(work.budget.max_actions, prior_consumed + 1)
    ):
        raise ValueError("H5 终态动作结算与锁定预算不一致")
    summary = {
        **dict(terminal.coverage_summary),
        "consumed_action_count": consumed_action_count,
    }
    if result_capture_id is not None:
        summary["result_capture_id"] = result_capture_id
    return replace(terminal, coverage_summary=summary)


def _bounded_terminal(
    work: ExplorationWork,
    *,
    reason: str,
    reason_code: str | None,
) -> ExplorationTerminal:
    """为基础设施恢复失败构造与 Work 身份一致的显式有界终态。"""
    stable_reason_code = reason_code or "h5_browser_recovery_failed"
    return ExplorationTerminal(
        work_id=work.work_id,
        task_id=work.task_id,
        status=ExplorationTerminalStatus.BOUNDED,
        reason=reason,
        outcome="bounded",
        reason_code=stable_reason_code,
    )


def _is_retryable(exc: Exception) -> bool:
    if isinstance(exc, CaptureCoordinationError):
        return exc.stage is CaptureCoordinationStage.BEFORE_CAPTURE
    if getattr(exc, "retryable", False) is True:
        return True
    return _is_transient_llm_failure(exc)


def _is_transient_llm_failure(exc: BaseException) -> bool:
    """LLM 网关短时故障（5xx/429/连接/超时）按基础设施抖动重试，不终结探索。

    模型客户端异常（如 agent_framework 的 ChatClientException）会把底层
    openai 异常放进 args 或因果链；按模块名识别，避免应用层直接依赖 SDK 类型。
    """
    seen: set[int] = set()
    pending: list[BaseException] = [exc]
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if (type(current).__module__ or "").split(".")[0] == "openai":
            status = getattr(current, "status_code", None)
            if isinstance(status, int) and (status >= 500 or status == 429):
                return True
            if type(current).__name__ in {"APIConnectionError", "APITimeoutError"}:
                return True
        for arg in getattr(current, "args", ()):
            if isinstance(arg, BaseException):
                pending.append(arg)
        if current.__cause__ is not None:
            pending.append(current.__cause__)
        if current.__context__ is not None:
            pending.append(current.__context__)
    return False


_RETRYABLE_ACTION_REOBSERVATION_ERROR_CODES = frozenset(
    {
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
        # deadline 在 Browser 调用前检查，尚未产生动作副作用；基于新的
        # before Capture 收口当前候选，再由 successor 预算门结束整轮。
        "ACTION_DEADLINE_EXCEEDED",
        # 视觉坐标未取得结构锚点时，Resolver 在 pointer 事件前封闭拒绝；
        # 保持结构门禁不变，从 before Capture 重观察并继续 sibling。
        "VISUAL_TARGET_REQUIRES_STRUCTURAL_ANCHOR",
    }
)
_TERMINAL_CANDIDATE_REOBSERVATION_ERROR_CODES = frozenset(
    {
        "SEMANTIC_TARGET_AMBIGUOUS",
        # Resolver 在指针执行前拒绝多目标；只关闭当前候选，不终止整轮探索。
        "LOGICAL_TARGET_NOT_UNIQUE",
        # 媒体没有可拖动的原生 controls 时尚未发生 pointer down，属于
        # 当前候选的确定性无副作用失败；必须重观察并继续 sibling，不能
        # 把携带首页导航 Frontier 的完整 Work 直接标成 failed。
        "SEEK_NATIVE_CONTROLS_UNAVAILABLE",
        # 浏览器历史为空时 Runtime 尚未产生页面跳转，属于当前返回候选的
        # 确定性无副作用失败；只关闭该候选并继续 sibling。
        "BROWSER_HISTORY_EMPTY",
        # 底栏专用执行器已经证明 URL 与目标激活状态均未变化；只淘汰
        # 当前候选。通用 ACTION_REQUEST_REJECTED 不具备此无副作用证明。
        "BOTTOM_NAVIGATION_TRANSITION_NOT_OBSERVED",
        "FLUTTER_BOTTOM_NAVIGATION_NO_EFFECT",
        # 请求校验在指针执行前拒绝不可信关闭合同，只终结当前候选。
        # 通用 PAGE_CAPTURE_ACTION_BLOCKED 不具备这一无副作用边界。
        "H5_POPUP_DISMISSAL_INTENT_INVALID",
    }
)


def _is_action_reobservation(exc: CaptureCoordinationError) -> bool:
    """仅恢复白名单内、有动作前证据且尚未产生副作用的候选失败。"""
    if (
        exc.stage is not CaptureCoordinationStage.ACTION
        or exc.before_capture is None
        or exc.interaction is None
    ):
        return False
    if exc.error_code == "LOGICAL_TARGET_HIT_TEST_FAILED":
        return (
            exc.interaction.retryable is True
            and exc.interaction.status is InteractionResultStatus.BLOCKED
            and exc.after_capture is None
            and exc.before_capture.role == "before_action"
            and exc.before_capture.task_id == exc.interaction.task_id
            and exc.before_capture.session_id == exc.interaction.session_id
            and exc.before_capture.capture_id == exc.interaction.source_capture_id
            and exc.before_capture.action_execution_id == exc.interaction.action_execution_id
            and exc.interaction.result_capture_id is None
        )
    if (
        exc.error_code in _RETRYABLE_ACTION_REOBSERVATION_ERROR_CODES
        and exc.interaction.retryable is True
    ):
        return True
    if h5_action_failure_is_policy_blocked(exc.error_code):
        return (
            exc.interaction.status is InteractionResultStatus.BLOCKED
            and exc.interaction.retryable is False
        )
    return (
        exc.error_code in _TERMINAL_CANDIDATE_REOBSERVATION_ERROR_CODES
        and exc.interaction.retryable is False
    )


def _validate_action_reobservation_successor(
    *,
    prior_work: ExplorationWork,
    source_capture_id: str,
    successor: H5ExplorationResult,
) -> None:
    work = successor.work
    if (
        work.task_id != prior_work.task_id
        or work.session_id != prior_work.session_id
        or work.surface_kind is not SurfaceKind.WEB
        or work.source_capture_id != source_capture_id
        or work.work_id == prior_work.work_id
        or work.frontier.observation.metadata.get("parent_work_id") != prior_work.work_id
    ):
        raise ValueError("H5 Action 重观察后继身份不一致")


def _requires_safe_session_rebaseline(exc: CaptureCoordinationError) -> bool:
    """判断动作后 Surface 是否必须作废并从最后安全位置重建。

    浏览器句柄丢失与动作超时都无法证明当前页面状态。超时动作不能原地重放，
    也不能沿用动作前页面继续执行 sibling；复用既有 unknown-action 恢复链，
    先作废旧 Session、重建安全状态并把原候选失败关闭。
    """

    code = exc.error_code.casefold()
    cause = (exc.cause_type or "").casefold()
    browser_lost = code in {
        "browser_session_lost",
        "session_lost",
        "connection_closed",
        "browser_process_exited",
        "page_capture_session_crashed",
    } or cause in {
        "targetclosederror",
        "browserdisconnectederror",
        "connectionclosederror",
        "persistedbrowserlossattempt",
        "browsersessioncrashederror",
    }
    if browser_lost:
        return True
    return bool(
        exc.stage is CaptureCoordinationStage.ACTION
        and code == "action_timed_out"
        and exc.before_capture is not None
        and exc.interaction is not None
        and exc.interaction.retryable is True
    )


def _validated_browser_attempt(
    work: ExplorationWork, agent_run_id: str,
) -> H5BrowserRecoveryAttemptFacts | None:
    payload = work.frontier.observation.metadata.get("h5_browser_recovery_attempt")
    if not isinstance(payload, Mapping):
        return None
    attempt = H5BrowserRecoveryAttemptFacts.from_dict(payload)
    if (
        attempt.task_id != work.task_id or attempt.agent_run_id != agent_run_id
        or attempt.source_session_id != work.session_id
        or attempt.failed_work_id != work.work_id
        or attempt.deadline != work.deadline.isoformat()
    ):
        raise ValueError("H5 恢复记录与当前工作身份或冻结截止不一致")
    return attempt


def _complete_browser_attempt(
    work: ExplorationWork,
    reason_code: str | None,
) -> ExplorationWork:
    payload = work.frontier.observation.metadata.get("h5_browser_recovery_attempt")
    if not isinstance(payload, Mapping):
        return work
    facts = H5BrowserRecoveryAttemptFacts.from_dict(payload)
    if facts.status != "started":
        return work
    completed = facts.complete(reason_code=reason_code or "h5_browser_recovery_failed_closed")
    frontier = replace(
        work.frontier,
        observation=replace(
            work.frontier.observation,
            metadata={
                **dict(work.frontier.observation.metadata),
                "h5_browser_recovery_attempt": completed.to_dict(),
            },
        ),
    )
    return replace(work, frontier=frontier, frontier_snapshot_id=frontier.snapshot_id)


def _failure_target(exc: Exception) -> ExplorerWorkStatus:
    """只按探索故障自身的可重试属性映射工作项状态。"""
    return ExplorerWorkStatus.RETRY_WAIT if _is_retryable(exc) else ExplorerWorkStatus.FAILED


def _safe_error_code(exc: Exception) -> str:
    value = getattr(exc, "error_code", None)
    if isinstance(value, str) and value.strip() and value == value.strip():
        return value[:128]
    return "H5_EXPLORATION_TURN_FAILED"
