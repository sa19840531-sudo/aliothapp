"""H5 定向补采 Probe Work 的独立执行与挂接编排。"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Protocol
from uuid import uuid4

from services.api.app.application.app_exploration_lease import (
    AppExplorationLeaseLostError,
)
from services.api.app.application.evidence_request_consumption import (
    EvidenceRequestConsumptionError,
    EvidenceRequestConsumptionService,
    ProbeCompletionProof,
    ProbeSupplementAttachment,
)
from services.api.app.application.evidence_requests import EvidenceRequestStatus
from services.api.app.application.exploration_work_queue import (
    ClaimExplorerWorkCommand,
    ExplorerWorkPersistenceError,
    ExplorerWorkQueue,
    ExplorerWorkStatus,
    ExplorerWorkType,
    RenewExplorerWorkLeaseCommand,
    TransitionExplorerWorkCommand,
)
from services.api.app.application.explorer_turn import (
    ExplorerDecisionPort,
    ExplorerTurnExecutor,
)
from services.api.app.application.h5_probe_goal_planning import H5ProbeGoalPlanner
from services.api.app.application.h5_recapture_lease import H5BoundedLeaseGuard
from services.api.app.application.h5_session_journey import (
    H5SessionJourneyDeadline,
    h5_session_journey,
)
from services.api.app.application.h5_viewport import h5_viewport_parameters
from services.api.app.application.page_capture_persistence import (
    SUPPORTED_CAPTURE_ARTIFACT_TYPES,
)
from services.api.app.application.supplemental_evidence_policy import (
    SupplementalEvidenceOperation,
    SupplementalEvidencePolicy,
)
from services.api.app.domain.exploration import (
    ExplorationTerminal,
    ExplorationTerminalStatus,
    ProbeWork,
)
from services.api.app.domain.inspection import EvidenceSourceGrade
from services.api.app.domain.interaction import (
    ActionIntent,
    InteractionOperation,
    InteractionResult,
    InteractionResultStatus,
    SurfaceKind,
)

logger = logging.getLogger(__name__)

_EVIDENCE_OVERLAY_CLOSE_TARGET = "遮挡底层页面的弹窗关闭控件"
_H5_POPUP_DISMISSAL_TARGET = "关闭当前弹窗"
_H5_PROBE_POPUP_DISMISSAL_TARGET_MODE = "h5_probe_popup_dismissal"


class H5ProbeWorkError(RuntimeError):
    """H5 Probe 合同或执行结果不可信。"""

    def __init__(
        self, message: str, *, error_code: str, retryable: bool,
        outcome: H5ProbeWorkOutcome | None = None,
        interaction: InteractionResult | None = None,
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.retryable = retryable
        self.outcome = outcome
        self.interaction = interaction


class H5ProbeCompletionPort(Protocol):
    async def complete(
        self,
        *,
        task_id: str,
        outcome: H5ProbeWorkOutcome,
    ) -> ProbeSupplementAttachment: ...


class H5ProbeCompletionRecoveryReader(Protocol):
    async def find_next(
        self,
        *,
        task_id: str,
        agent_run_id: str,
    ) -> H5ProbeWorkOutcome | None: ...


class H5ProbeEvidencePreparationPort(Protocol):
    async def prepare(self, *, task_id: str, outcome: H5ProbeWorkOutcome) -> None: ...


@dataclass(frozen=True, kw_only=True)
class H5SharedProbeEvidence:
    """已验证的实际只读动作引用，不虚构当前 Probe 执行了动作。"""

    origin_work_id: str
    result_capture_id: str


class H5SharedProbeEvidencePort(Protocol):
    async def find(
        self, *, work: ProbeWork, agent_run_id: str,
    ) -> H5SharedProbeEvidence | None: ...


class H5InspectionWakeupPort(Protocol):
    async def reopen_waiting_evidence(self, *, task_id: str) -> int: ...


class H5ProbeTerminalSettlementPort(Protocol):
    async def settle_ready(self, *, task_id: str, agent_run_id: str) -> int: ...

    async def settle_completion_failure(
        self, *, task_id: str, agent_run_id: str,
        outcome: H5ProbeWorkOutcome, error_code: str,
    ) -> int: ...


@dataclass(frozen=True, slots=True, kw_only=True)
class H5ProbeWorkOutcome:
    """H5 Probe 的引用型结果，不携带浏览器对象。"""

    claimed: bool
    work_id: str | None = None
    final_status: ExplorerWorkStatus | None = None
    result_capture_id: str | None = None
    evidence_request_id: str | None = None
    restoration_attempt_id: str | None = None
    equivalence_assessment_id: str | None = None
    source_grade: EvidenceSourceGrade | None = None
    error_code: str | None = None
    completion_pending: bool = False


class H5ProbeDecisionAdapter:
    """在通用 Probe 决策上补齐 H5 执行所需的受信血缘和视口。"""

    def __init__(
        self, *, delegate: ExplorerDecisionPort, goal_planner: H5ProbeGoalPlanner | None = None,
    ) -> None:
        self._delegate = delegate
        self._goal_planner = goal_planner

    async def decide_next(self, work: ProbeWork) -> ActionIntent | ExplorationTerminal:
        if not isinstance(work, ProbeWork) or work.surface_kind is not SurfaceKind.WEB:
            raise H5ProbeWorkError(
                "H5 Probe 决策只接受 Web Surface 的 ProbeWork",
                error_code="H5_PROBE_WORK_INVALID",
                retryable=False,
            )
        decision = await self._delegate.decide_next(work)
        if isinstance(decision, ExplorationTerminal):
            return decision
        if not isinstance(decision, ActionIntent):
            raise H5ProbeWorkError(
                "H5 Probe 决策类型不受支持",
                error_code="H5_PROBE_DECISION_INVALID",
                retryable=False,
            )
        if (
            decision.capture_profile.before.role != "probe_before"
            or decision.capture_profile.after.role != "probe_result_replay"
        ):
            raise H5ProbeWorkError(
                "H5 Probe Capture 角色不符合补采合同",
                error_code="H5_PROBE_CAPTURE_ROLE_INVALID",
                retryable=False,
            )
        required_types = set(work.evidence_request.required_artifact_types)
        after_types = set(decision.capture_profile.after.artifact_types)
        unsupported = required_types.difference(SUPPORTED_CAPTURE_ARTIFACT_TYPES)
        if unsupported:
            raise H5ProbeWorkError(
                "H5 Probe 请求了不受支持的证据类型: " + ", ".join(sorted(unsupported)),
                error_code="H5_PROBE_ARTIFACT_UNSUPPORTED",
                retryable=False,
            )
        if not required_types.issubset(after_types) or "action_trace" not in after_types:
            raise H5ProbeWorkError(
                "H5 Probe 动作后采集未覆盖请求证据与 Action Trace",
                error_code="H5_PROBE_CAPTURE_PROFILE_INCOMPLETE",
                retryable=False,
            )
        if self._goal_planner is not None:
            from services.api.app.application.evidence_recovery_replay import (
                EvidenceRecoveryReplayError,
            )

            try:
                decision = await self._goal_planner.plan(work=work, baseline=decision)
            except (ValueError, EvidenceRecoveryReplayError) as exc:
                raise H5ProbeWorkError(
                    str(exc),
                    error_code=getattr(exc, "reason_code", "H5_PROBE_GOAL_SELECTION_INVALID"),
                    retryable=False,
                ) from exc
            if isinstance(decision, ExplorationTerminal):
                return decision
        parameters = dict(decision.parameters)
        requirement = work.evidence_request.required_interaction
        if (
            requirement is not None
            and requirement.operation is InteractionOperation.CLICK
            and requirement.semantic_target == _EVIDENCE_OVERLAY_CLOSE_TARGET
            and decision.action_type is InteractionOperation.CLICK
            and decision.semantic_target == _EVIDENCE_OVERLAY_CLOSE_TARGET
        ):
            # 规则侧只声明补采意图，不携带 H5 主探索阶段的临时能力签名。
            # 执行端必须基于来源 Snapshot 重新签发，不能把自然语言当作页面文案查找。
            parameters.setdefault(
                "target_mode",
                _H5_PROBE_POPUP_DISMISSAL_TARGET_MODE,
            )
            decision = replace(
                decision,
                semantic_target=_H5_POPUP_DISMISSAL_TARGET,
            )
        existing_work_id = parameters.get("exploration_work_id")
        if existing_work_id not in {None, work.work_id}:
            raise H5ProbeWorkError(
                "H5 Probe ActionIntent 绑定了其他工作项",
                error_code="H5_PROBE_WORK_LINEAGE_MISMATCH",
                retryable=False,
            )
        parameters.update(
            {
                "evidence_request_id": work.evidence_request.request_id,
                "exploration_work_id": work.work_id,
                "restoration_plan_hash": work.restoration_plan_hash,
                "restoration_attempt_id": work.restoration_attempt_id,
                "equivalence_assessment_id": work.equivalence_assessment_id,
                "supplemental_evidence_source": "targeted_recapture",
                **h5_viewport_parameters(),
            }
        )
        return replace(decision, parameters=parameters)


class H5ProbeCompletionService:
    """把 H5 Probe 成功事实转换为公共六字段证明并挂接。"""

    def __init__(
        self,
        *,
        consumption: EvidenceRequestConsumptionService,
        recovery: H5ProbeCompletionRecoveryReader | None = None,
        evidence_preparation: H5ProbeEvidencePreparationPort | None = None,
    ) -> None:
        self._consumption = consumption
        self._recovery = recovery
        self._evidence_preparation = evidence_preparation

    async def complete(
        self,
        *,
        task_id: str,
        outcome: H5ProbeWorkOutcome,
    ) -> ProbeSupplementAttachment:
        proof = _completion_proof(outcome)
        try:
            if self._evidence_preparation is not None:
                await self._evidence_preparation.prepare(task_id=task_id, outcome=outcome)
            return await self._consumption.attach_supplement(task_id=task_id, proof=proof)
        except (EvidenceRequestConsumptionError, H5ProbeWorkError) as exc:
            raise H5ProbeWorkError(
                str(exc),
                error_code=getattr(exc, "error_code", "H5_PROBE_SUPPLEMENT_ATTACH_FAILED"),
                retryable=exc.retryable,
                outcome=outcome,
            ) from exc

    async def recover_next(
        self,
        *,
        task_id: str,
        agent_run_id: str,
    ) -> ProbeSupplementAttachment | None:
        if self._recovery is None:
            return None
        outcome = await self._recovery.find_next(
            task_id=_require_text(task_id, "task_id"),
            agent_run_id=_require_text(agent_run_id, "agent_run_id"),
        )
        if outcome is None:
            return None
        return await self.complete(task_id=task_id, outcome=outcome)


class H5ProbeWorkRunner:
    """只消费当前 H5 任务的 Web Probe Work，并在成功后挂回补采证据。"""

    def __init__(
        self,
        *,
        queue: ExplorerWorkQueue,
        turn_executor: ExplorerTurnExecutor,
        request_consumption: EvidenceRequestConsumptionService,
        completion: H5ProbeCompletionPort,
        lease_owner: str,
        policy: SupplementalEvidencePolicy | None = None,
        lease_seconds: int = 180,
        retry_delay_seconds: int = 5,
        now: Callable[[], datetime] | None = None,
        shared_evidence: H5SharedProbeEvidencePort | None = None,
        session_command_gate: object | None = None,
    ) -> None:
        self._queue = queue
        self._turn_executor = turn_executor
        self._request_consumption = request_consumption
        self._completion = completion
        self._lease_owner = _require_text(lease_owner, "lease_owner")
        self._policy = policy or SupplementalEvidencePolicy()
        if lease_seconds <= 0 or retry_delay_seconds < 0:
            raise ValueError("租约时长和重试延迟不合法")
        self._lease_seconds = lease_seconds
        self._retry_delay_seconds = retry_delay_seconds
        self._now = now or (lambda: datetime.now(UTC))
        self._shared_evidence = shared_evidence
        self._session_command_gate = session_command_gate

    async def run_one(
        self,
        *,
        task_id: str,
        agent_run_id: str,
    ) -> H5ProbeWorkOutcome:
        task = _require_text(task_id, "task_id")
        run_id = _require_text(agent_run_id, "agent_run_id")
        self._policy.require_enabled(SupplementalEvidenceOperation.CLAIM_PROBE)
        lease = await self._queue.claim(
            command=ClaimExplorerWorkCommand(
                work_type=ExplorerWorkType.PROBE,
                lease_owner=self._lease_owner,
                lease_seconds=self._lease_seconds,
                task_id=task,
                surface_kinds=(SurfaceKind.WEB,),
            ),
            lease_token=str(uuid4()),
        )
        if lease is None:
            return H5ProbeWorkOutcome(claimed=False)
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
            lost_message="H5 Probe 租约已失效", task_name="h5-probe-lease-renew",
        )
        try:
            journey_deadline = min(
                lease.work.deadline,
                self._now() + timedelta(milliseconds=lease.work.budget.remaining_duration_ms),
            )
            await self._start_guard_before_deadline(guard, lease.work)
            async with guard, h5_session_journey(
                gate=self._session_command_gate, task_id=task,
                session_id=lease.work.session_id, deadline=journey_deadline, clock=self._now,
            ):
                work = _validate_h5_probe_work(lease.work, task_id=task)
                self._policy.require_enabled(SupplementalEvidenceOperation.EXECUTE_PROBE)
                request = await self._run_before_deadline(
                    guard, work,
                    self._request_consumption.ensure_running(
                        task_id=task,
                        request_id=work.evidence_request.request_id,
                    )
                )
                if request.status is EvidenceRequestStatus.SATISFIED:
                    await self._queue.transition(
                        command=TransitionExplorerWorkCommand(
                            work_id=work.work_id,
                            expected_status=ExplorerWorkStatus.RUNNING,
                            target_status=ExplorerWorkStatus.CANCELLED,
                            lease_token=lease.lease_token,
                            row_version=running_version,
                            error_code="H5_PROBE_REQUEST_ALREADY_SATISFIED",
                            error_message="Evidence Request 已由其他执行器满足",
                        )
                    )
                    return _outcome(
                        work=work,
                        status=ExplorerWorkStatus.CANCELLED,
                        error_code="H5_PROBE_REQUEST_ALREADY_SATISFIED",
                    )
                if request.status is not EvidenceRequestStatus.RUNNING:
                    raise H5ProbeWorkError(
                        "Evidence Request 不在 H5 Probe 可执行状态",
                        error_code="H5_PROBE_REQUEST_STATE_INVALID",
                        retryable=False,
                    )
                shared = (
                    await self._run_before_deadline(
                        guard, work, self._shared_evidence.find(work=work, agent_run_id=run_id),
                    ) if self._shared_evidence is not None else None
                )
                release_stale = getattr(self._shared_evidence, "release_stale_unexecuted", None)
                if shared is None and callable(release_stale) and await self._run_before_deadline(
                    guard, work, release_stale(
                        work=work, agent_run_id=run_id, lease_token=lease.lease_token,
                        row_version=running_version,
                    ),
                ):
                    return _outcome(work=work, status=ExplorerWorkStatus.SUPERSEDED,
                                    error_code="H5_SHARED_PROBE_SOURCE_REPLACED")
                turn = (
                    await self._run_before_deadline(
                        guard, work, self._turn_executor.execute(work=work, agent_run_id=run_id),
                    ) if shared is None else None
                )
                if turn is not None and isinstance(turn.decision, ExplorationTerminal):
                    target = _terminal_work_status(turn.decision)
                    await self._queue.transition(
                        command=TransitionExplorerWorkCommand(
                            work_id=work.work_id,
                            expected_status=ExplorerWorkStatus.RUNNING,
                            target_status=target,
                            lease_token=lease.lease_token,
                            row_version=running_version,
                            terminal=turn.decision,
                        )
                    )
                    return _outcome(work=work, status=target)
                result_capture_id = (
                    shared.result_capture_id if shared is not None
                    else _validate_successful_turn(work=work, turn=turn)
                )
                outcome = _outcome(
                    work=work,
                    status=ExplorerWorkStatus.SUCCEEDED,
                    result_capture_id=result_capture_id,
                )
                _completion_proof(outcome)
                await guard.assert_healthy()
                await self._queue.transition(
                    command=TransitionExplorerWorkCommand(
                        work_id=work.work_id,
                        expected_status=ExplorerWorkStatus.RUNNING,
                        target_status=ExplorerWorkStatus.SUCCEEDED,
                        lease_token=lease.lease_token,
                        row_version=running_version,
                        terminal=(ExplorationTerminal(
                            work_id=work.work_id, task_id=work.task_id,
                            status=ExplorationTerminalStatus.COMPLETED,
                            reason=f"h5_shared_evidence:{shared.origin_work_id}",
                        ) if shared is not None else None),
                    )
                )
        except AppExplorationLeaseLostError as exc:
            if self._now() < lease.work.deadline:
                raise
            # 到期仅结算自己仍持有的租约；围栏拒绝时交给持久化恢复器处理。
            try:
                await self._queue.transition(
                    command=TransitionExplorerWorkCommand(
                        work_id=lease.work.work_id,
                        expected_status=ExplorerWorkStatus.RUNNING,
                        target_status=ExplorerWorkStatus.FAILED,
                        lease_token=lease.lease_token,
                        row_version=running_version,
                        error_code="H5_PROBE_DEADLINE_EXHAUSTED",
                        error_message="H5 Probe 已超过截止时间，执行已停止",
                    )
                )
            except ExplorerWorkPersistenceError as settlement_error:
                raise exc from settlement_error
            return _outcome(
                work=_validate_h5_probe_work(lease.work, task_id=task),
                status=ExplorerWorkStatus.FAILED,
                error_code="H5_PROBE_DEADLINE_EXHAUSTED",
            )
        except ExplorerWorkPersistenceError:
            raise
        except Exception as exc:  # noqa: BLE001 - 统一映射为持久工作状态
            from services.api.app.application.h5_llm_budget import h5_llm_budget_error_code

            budget_error = h5_llm_budget_error_code(exc)
            error_code = budget_error or str(getattr(exc, "error_code", type(exc).__name__))
            if isinstance(exc, H5SessionJourneyDeadline):
                error_code = "H5_PROBE_DEADLINE_EXHAUSTED"
            interaction = getattr(exc, "interaction", None)
            retryable = not budget_error and _is_retryable_probe_failure(
                exc, interaction=interaction,
            )
            logger.warning(
                "task_id=%s work_id=%s H5 Probe 执行失败 reason=%s",
                task,
                lease.work.work_id,
                error_code,
                exc_info=True,
            )
            try:
                await self._queue.transition(
                    command=TransitionExplorerWorkCommand(
                        work_id=lease.work.work_id,
                        expected_status=ExplorerWorkStatus.RUNNING,
                        target_status=(
                            ExplorerWorkStatus.RETRY_WAIT
                            if retryable
                            else ExplorerWorkStatus.FAILED
                        ),
                        lease_token=lease.lease_token,
                        row_version=running_version,
                        error_code=error_code,
                        error_message=str(exc)[:1000],
                        retry_delay_seconds=self._retry_delay_seconds if retryable else 0,
                    )
                )
            except ExplorerWorkPersistenceError as settlement_error:
                if error_code == "H5_PROBE_DEADLINE_EXHAUSTED":
                    raise AppExplorationLeaseLostError(
                        "H5 Probe 到期结算被租约围栏拒绝，旧执行器不能写回",
                    ) from settlement_error
                raise
            return _outcome(
                work=_validate_h5_probe_work(lease.work, task_id=task),
                status=(
                    ExplorerWorkStatus.RETRY_WAIT
                    if retryable
                    else ExplorerWorkStatus.FAILED
                ),
                error_code=error_code,
            )
        try:
            await self._completion.complete(task_id=task, outcome=outcome)
        except Exception as exc:  # noqa: BLE001 - Work 已成功，交由恢复读取器补挂
            return replace(
                outcome,
                completion_pending=True,
                error_code=str(
                    getattr(exc, "error_code", "H5_PROBE_SUPPLEMENT_ATTACH_PENDING")
                ),
            )
        return outcome

    async def _start_guard_before_deadline(self, guard, work):
        try:
            await self._run_before_deadline(guard, work, guard.start())
        except BaseException:  # noqa: BLE001 - 取消也须停止已创建的续租任务
            with suppress(AppExplorationLeaseLostError):
                await guard.stop()
            raise

    async def _run_before_deadline(
        self, guard: H5BoundedLeaseGuard, work: ProbeWork, operation: Awaitable,
    ):
        """补采截止后先停止并等待实际操作，不能留下脱离租约的后台协程。"""
        task = asyncio.ensure_future(operation)
        started_at = self._now()
        stage_deadline = min(work.deadline, started_at + timedelta(
            milliseconds=work.budget.remaining_duration_ms,
        ))
        remaining = (stage_deadline - started_at).total_seconds()
        limit = asyncio.timeout(max(0, remaining))
        try:
            if remaining <= 0:
                raise H5ProbeWorkError(
                    "H5 Probe 已超过截止时间，不能继续执行",
                    error_code="H5_PROBE_DEADLINE_EXHAUSTED", retryable=False,
                )
            try:
                async with limit:
                    result = await guard.run(task)
                if limit.expired() or self._now() >= stage_deadline:
                    raise H5ProbeWorkError(
                        "H5 Probe 晚到结果超过截止时间，不能继续交接",
                        error_code="H5_PROBE_DEADLINE_EXHAUSTED", retryable=False,
                    )
                return result
            except TimeoutError:
                if not limit.expired():
                    raise
                raise H5ProbeWorkError(
                    "H5 Probe 已超过截止时间，执行已停止",
                    error_code="H5_PROBE_DEADLINE_EXHAUSTED", retryable=False,
                ) from None
        finally:
            if not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task


def _is_retryable_probe_failure(exc: Exception, *, interaction: object | None) -> bool:
    """只重试没有形成交互终态的瞬时故障。"""

    status = getattr(interaction, "status", None)
    if status in {
        InteractionResultStatus.FAILED,
        InteractionResultStatus.BLOCKED,
        InteractionResultStatus.TIMED_OUT,
        InteractionResultStatus.EVIDENCE_FAILED,
    }:
        # 动作账本已终结且采集可能推进现场，不能重放绑定旧来源的同一 Work。
        return False
    return bool(
        getattr(exc, "retryable", False)
        or getattr(interaction, "retryable", False)
    )


class H5ProbeDrainService:
    """在 H5 活会话内恢复补挂或执行一条 Probe，并唤醒规则重判。"""

    def __init__(
        self,
        *,
        runner: H5ProbeWorkRunner,
        completion: H5ProbeCompletionService,
        terminal_settlement: H5ProbeTerminalSettlementPort,
        inspection_wakeup: H5InspectionWakeupPort,
        policy: SupplementalEvidencePolicy,
    ) -> None:
        self._runner = runner
        self._completion = completion
        self._terminal_settlement = terminal_settlement
        self._inspection_wakeup = inspection_wakeup
        self._policy = policy

    async def drain_one(self, *, task_id: str, agent_run_id: str) -> bool:
        """返回本轮是否恢复或完成了补采事实；关闭态策略下安全空转。"""

        if self._policy.decide(SupplementalEvidenceOperation.CLAIM_PROBE).denied:
            return False
        settled = await self._terminal_settlement.settle_ready(
            task_id=task_id,
            agent_run_id=agent_run_id,
        )
        if settled > 0:
            await self._inspection_wakeup.reopen_waiting_evidence(task_id=task_id)
            return True
        try:
            recovered = await self._completion.recover_next(
                task_id=task_id,
                agent_run_id=agent_run_id,
            )
        except H5ProbeWorkError as exc:
            if not exc.retryable:
                settle_failure = getattr(
                    self._terminal_settlement, "settle_completion_failure", None,
                )
                if exc.outcome is not None and callable(settle_failure):
                    settled = await settle_failure(
                        task_id=task_id, agent_run_id=agent_run_id,
                        outcome=exc.outcome, error_code=exc.error_code,
                    )
                    if settled > 0:
                        await self._inspection_wakeup.reopen_waiting_evidence(task_id=task_id)
                        return True
                raise
            # 重挂的瞬时识别/清单故障不应直接杀死主探索，也不算业务进展。
            # 页面检查点仍按无进展、绝对墙钟和任务截止时间有界等待。
            logger.warning(
                "task_id=%s H5 Probe 挂接暂不可用 error_code=%s",
                task_id,
                exc.error_code,
            )
            return False
        if recovered is not None:
            await self._inspection_wakeup.reopen_waiting_evidence(task_id=task_id)
            return True
        try:
            outcome = await self._runner.run_one(task_id=task_id, agent_run_id=agent_run_id)
        except AppExplorationLeaseLostError:
            # 已停止的 Probe 只有经持久化围栏正式结算，才能作为排空进展。
            settled = await self._terminal_settlement.settle_ready(
                task_id=task_id, agent_run_id=agent_run_id,
            )
            if settled <= 0:
                raise
            await self._inspection_wakeup.reopen_waiting_evidence(task_id=task_id)
            return True
        if not outcome.claimed:
            return False
        if (
            outcome.final_status is ExplorerWorkStatus.SUCCEEDED
            and not outcome.completion_pending
        ):
            await self._inspection_wakeup.reopen_waiting_evidence(task_id=task_id)
            return True
        elif outcome.final_status is not ExplorerWorkStatus.RETRY_WAIT:
            settled = await self._terminal_settlement.settle_ready(
                task_id=task_id,
                agent_run_id=agent_run_id,
            )
            if settled > 0:
                await self._inspection_wakeup.reopen_waiting_evidence(task_id=task_id)
                return True
        # 单纯领取、重试或等待挂接不是补证进展；成功挂接由 recover_next 证明。
        return False


def _validate_h5_probe_work(work: object, *, task_id: str) -> ProbeWork:
    if (
        not isinstance(work, ProbeWork)
        or work.surface_kind is not SurfaceKind.WEB
        or work.task_id != task_id
    ):
        raise H5ProbeWorkError(
            "领取到的工作不是当前任务的 H5 ProbeWork",
            error_code="H5_PROBE_WORK_INVALID",
            retryable=False,
        )
    return work


def _validate_successful_turn(*, work: ProbeWork, turn: object) -> str:
    coordination = getattr(turn, "coordination", None)
    if coordination is None:
        raise H5ProbeWorkError(
            "H5 Probe 没有产生动作采集闭环",
            error_code="H5_PROBE_CAPTURE_MISSING",
            retryable=False,
        )
    interaction = coordination.interaction
    after_capture = coordination.after_capture
    if interaction.status is not InteractionResultStatus.SUCCEEDED:
        raise H5ProbeWorkError(
            interaction.error_message or "H5 Probe 动作未成功",
            error_code=interaction.error_code or "H5_PROBE_ACTION_FAILED",
            retryable=interaction.retryable,
            interaction=interaction,
        )
    if (
        after_capture.task_id != work.task_id
        or after_capture.session_id != work.session_id
        or after_capture.surface_kind is not SurfaceKind.WEB
        or after_capture.role != "probe_result_replay"
        or interaction.result_capture_id != after_capture.capture_id
    ):
        raise H5ProbeWorkError(
            "H5 Probe 结果 Capture 身份或角色不一致",
            error_code="H5_PROBE_RESULT_CAPTURE_INVALID",
            retryable=False,
        )
    return after_capture.capture_id


def _outcome(
    *,
    work: ProbeWork,
    status: ExplorerWorkStatus,
    result_capture_id: str | None = None,
    error_code: str | None = None,
) -> H5ProbeWorkOutcome:
    return H5ProbeWorkOutcome(
        claimed=True,
        work_id=work.work_id,
        final_status=status,
        result_capture_id=result_capture_id,
        evidence_request_id=work.evidence_request.request_id,
        restoration_attempt_id=work.restoration_attempt_id,
        equivalence_assessment_id=work.equivalence_assessment_id,
        source_grade=work.source_grade,
        error_code=error_code,
    )


def _completion_proof(outcome: H5ProbeWorkOutcome) -> ProbeCompletionProof:
    if (
        not isinstance(outcome, H5ProbeWorkOutcome)
        or not outcome.claimed
        or outcome.final_status is not ExplorerWorkStatus.SUCCEEDED
    ):
        raise H5ProbeWorkError(
            "只有成功的 H5 Probe Work 可以挂接补采证据",
            error_code="H5_PROBE_NOT_SUCCEEDED",
            retryable=False,
        )
    try:
        return ProbeCompletionProof(
            request_id=_require_text(outcome.evidence_request_id, "evidence_request_id"),
            result_capture_id=_require_text(outcome.result_capture_id, "result_capture_id"),
            work_id=_require_text(outcome.work_id, "work_id"),
            restoration_attempt_id=_require_text(
                outcome.restoration_attempt_id,
                "restoration_attempt_id",
            ),
            equivalence_assessment_id=_require_text(
                outcome.equivalence_assessment_id,
                "equivalence_assessment_id",
            ),
            source_grade=outcome.source_grade,
        )
    except (TypeError, ValueError, EvidenceRequestConsumptionError) as exc:
        raise H5ProbeWorkError(
            "H5 Probe 结果缺少完整证明血缘",
            error_code="H5_PROBE_PROOF_INCOMPLETE",
            retryable=False,
        ) from exc


def _terminal_work_status(terminal: ExplorationTerminal) -> ExplorerWorkStatus:
    mapping = {
        ExplorationTerminalStatus.COMPLETED: ExplorerWorkStatus.SUCCEEDED,
        ExplorationTerminalStatus.BOUNDED: ExplorerWorkStatus.BOUNDED,
        ExplorationTerminalStatus.BUDGET_EXHAUSTED: ExplorerWorkStatus.BUDGET_EXHAUSTED,
        ExplorationTerminalStatus.BLOCKED: ExplorerWorkStatus.BLOCKED,
        ExplorationTerminalStatus.NO_PROGRESS: ExplorerWorkStatus.NO_PROGRESS,
    }
    if terminal.status is ExplorationTerminalStatus.COMPLETED:
        raise H5ProbeWorkError(
            "H5 Probe 未产生结果 Capture 却声明完成",
            error_code="H5_PROBE_PREMATURE_COMPLETION",
            retryable=False,
        )
    return mapping[terminal.status]


def _require_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError(f"{field_name} 必须为无首尾空白的非空字符串")
    return value


__all__ = [
    "H5ProbeCompletionRecoveryReader",
    "H5ProbeCompletionService",
    "H5ProbeDecisionAdapter",
    "H5ProbeDrainService",
    "H5ProbeTerminalSettlementPort",
    "H5ProbeWorkError",
    "H5ProbeWorkOutcome",
    "H5ProbeWorkRunner",
]
