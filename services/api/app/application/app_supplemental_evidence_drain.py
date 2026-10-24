"""App 活会话规则、补采请求与 Probe 的有界排空协调。"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from services.api.app.application.app_live_inspection_publish import (
    AppActiveSessionInspectionRoundResult,
)
from services.api.app.application.app_probe_completion import (
    AppProbeCompletionError,
    AppProbeCompletionResult,
)
from services.api.app.application.app_targeted_recapture_consumer import (
    AppTargetedRecaptureConsumerOutcome,
    AppTargetedRecaptureDisposition,
)
from services.api.app.application.exploration_work_queue import (
    ExplorerWorkStatus,
    ExplorerWorkType,
)
from services.api.app.application.supplemental_evidence_policy import (
    SupplementalEvidenceOperation,
    SupplementalEvidencePolicy,
)
from services.api.app.domain.exploration import (
    ExplorationBudget,
    consume_exploration_action_budget,
    copy_exploration_budget_after_observation,
)


class AppSupplementalEvidenceDrainError(RuntimeError):
    """补采闭环在截止、预算或轮数边界内无法排空。"""


@dataclass(frozen=True, kw_only=True)
class AppSupplementalEvidenceDrainState:
    """Evidence Request、Probe 和补挂恢复的数据库事实。"""

    nonterminal_request_count: int
    nonterminal_probe_count: int
    pending_completion_count: int
    next_attempt_at: datetime | None = None

    def __post_init__(self) -> None:
        for field_name in (
            "nonterminal_request_count",
            "nonterminal_probe_count",
            "pending_completion_count",
        ):
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{field_name} 必须为非负整数")
        if self.next_attempt_at is not None and self.next_attempt_at.utcoffset() is None:
            raise ValueError("next_attempt_at 必须带时区")

    @property
    def drained(self) -> bool:
        return (
            self.nonterminal_request_count == 0
            and self.nonterminal_probe_count == 0
            and self.pending_completion_count == 0
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class AppSupplementalEvidenceExhaustionSettlement:
    """一次耗尽边界内的纯结算结果。"""

    settled_request_count: int = 0
    cancelled_probe_count: int = 0

    def __post_init__(self) -> None:
        for field_name in ("settled_request_count", "cancelled_probe_count"):
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{field_name} 必须为非负整数")

    @property
    def changed(self) -> bool:
        """只要有请求或未执行 Probe 被收口，就需要唤醒 Judge。"""

        return self.settled_request_count > 0 or self.cancelled_probe_count > 0


@dataclass(frozen=True, kw_only=True)
class AppSupplementalEvidenceDrainResult:
    """一次关闭前补采排空摘要。"""

    round_count: int
    request_attempt_count: int
    probe_turn_count: int
    recovered_completion_count: int
    consumed_action_count: int
    remaining_budget: ExplorationBudget


class _InspectionDrainer(Protocol):
    async def drain_round(
        self, *, task_id: str, agent_run_id: str
    ) -> AppActiveSessionInspectionRoundResult: ...


class _RecaptureConsumer(Protocol):
    async def consume_next(
        self,
        *,
        task_id: str,
        agent_run_id: str,
        finalization_epoch: int,
        budget: ExplorationBudget,
        deadline: datetime | None = None,
    ) -> AppTargetedRecaptureConsumerOutcome: ...


class _ProbeRuntime(Protocol):
    async def run_one_turn(self, **kwargs: object) -> object: ...


class _ProbeCompletionRecovery(Protocol):
    async def complete(self, *, task_id: str, outcome: object) -> AppProbeCompletionResult: ...

    async def recover_next(
        self,
        *,
        task_id: str,
        agent_run_id: str,
        finalization_epoch: int,
    ) -> AppProbeCompletionResult | None: ...


class _InspectionWakeup(Protocol):
    async def reopen_waiting_evidence(self, *, task_id: str) -> int: ...


class _DrainStateReader(Protocol):
    async def load(
        self,
        *,
        task_id: str,
        agent_run_id: str,
        finalization_epoch: int,
    ) -> AppSupplementalEvidenceDrainState: ...


class _DrainExhaustionSettler(Protocol):
    """在不能再发起动作时按当前 Task/Run/Epoch 纯结算。"""

    async def settle_exhausted(
        self,
        *,
        task_id: str,
        agent_run_id: str,
        finalization_epoch: int,
        reason_code: str,
    ) -> AppSupplementalEvidenceExhaustionSettlement: ...


class AppSupplementalEvidenceDrainService:
    """在同一 Session 和剩余预算内排空规则缺证闭环。"""

    def __init__(
        self,
        *,
        inspection: _InspectionDrainer,
        recapture: _RecaptureConsumer,
        probe_runtime: _ProbeRuntime,
        probe_completion: _ProbeCompletionRecovery,
        inspection_wakeup: _InspectionWakeup,
        state_reader: _DrainStateReader,
        exhaustion_settlement: _DrainExhaustionSettler | None = None,
        policy: SupplementalEvidencePolicy,
        max_rounds: int = 100,
        max_poll_seconds: float = 2.0,
        now: Callable[[], datetime] | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        if isinstance(max_rounds, bool) or not isinstance(max_rounds, int) or max_rounds <= 0:
            raise ValueError("max_rounds 必须为正整数")
        if max_poll_seconds <= 0:
            raise ValueError("max_poll_seconds 必须大于零")
        self._inspection = inspection
        self._recapture = recapture
        self._probe_runtime = probe_runtime
        self._probe_completion = probe_completion
        self._inspection_wakeup = inspection_wakeup
        self._state_reader = state_reader
        self._exhaustion_settlement = exhaustion_settlement
        self._policy = policy
        self._max_rounds = max_rounds
        self._max_poll_seconds = max_poll_seconds
        self._now = now or (lambda: datetime.now(UTC))
        self._sleep = sleep or asyncio.sleep

    async def drain(
        self,
        *,
        task_id: str,
        agent_run_id: str,
        finalization_epoch: int,
        budget: ExplorationBudget,
        deadline: datetime,
    ) -> AppSupplementalEvidenceDrainResult:
        task = _require_text(task_id, "task_id")
        run_id = _require_text(agent_run_id, "agent_run_id")
        if (
            isinstance(finalization_epoch, bool)
            or not isinstance(finalization_epoch, int)
            or finalization_epoch < 0
        ):
            raise ValueError("finalization_epoch 必须为非负整数")
        if not isinstance(budget, ExplorationBudget):
            raise TypeError("budget 必须为 ExplorationBudget")
        if deadline.utcoffset() is None:
            raise ValueError("deadline 必须带时区")
        self._policy.require_enabled(SupplementalEvidenceOperation.EXECUTE_PROBE)

        current_budget = budget
        accounted_at = self._checked_now()

        def refresh_budget() -> datetime:
            """所有异步阶段和轮间等待共享一个耗时游标，避免漏扣或重扣。"""
            nonlocal current_budget, accounted_at
            current_time = self._checked_now()
            current_budget = copy_exploration_budget_after_observation(
                current_budget,
                elapsed_duration_ms=max(
                    int((current_time - accounted_at).total_seconds() * 1000), 0
                ),
            )
            accounted_at = current_time
            return current_time

        def can_start_action(now: datetime) -> bool:
            return (
                now < deadline
                and current_budget.remaining_duration_ms > 0
                and current_budget.resolved_remaining_business_actions > 0
            )

        request_attempts = 0
        probe_turns = 0
        recovered_completions = 0
        consumed_actions = 0
        for round_count in range(1, self._max_rounds + 1):
            round_started = refresh_budget()
            # 没有精确收口端口时保留旧的无副作用失败边界；生产装配提供端口后，
            # 即使进入时已经到期，也必须先恢复已执行事实，再完成纯结算和判定唤醒。
            if (
                round_count == 1
                and round_started >= deadline
                and self._exhaustion_settlement is None
            ):
                raise AppSupplementalEvidenceDrainError("App 补采排空超过任务截止时间")
            progress = False

            recovered = await self._probe_completion.recover_next(
                task_id=task,
                agent_run_id=run_id,
                finalization_epoch=finalization_epoch,
            )
            if recovered is not None:
                recovered_completions += 1
                progress = True
            if round_count == 1 or recovered is not None:
                reopened = await self._inspection_wakeup.reopen_waiting_evidence(task_id=task)
                progress = progress or reopened > 0

            inspection_round = await self._inspection.drain_round(
                task_id=task,
                agent_run_id=run_id,
            )
            progress = progress or inspection_round.published_work_item_count > 0

            current_time = refresh_budget()
            recapture = AppTargetedRecaptureConsumerOutcome(
                disposition=AppTargetedRecaptureDisposition.EMPTY
            )
            if current_time < deadline and current_budget.remaining_duration_ms > 0:
                # 零业务额度仍允许消费者把请求结算为不足，但不得执行恢复动作。
                recapture = await self._recapture.consume_next(
                    task_id=task,
                    agent_run_id=run_id,
                    finalization_epoch=finalization_epoch,
                    budget=current_budget,
                    deadline=deadline,
                )
            if recapture.disposition is not AppTargetedRecaptureDisposition.EMPTY:
                request_attempts += 1
                progress = True
            if recapture.disposition in {
                AppTargetedRecaptureDisposition.UNFULFILLABLE,
                AppTargetedRecaptureDisposition.EXPIRED,
            }:
                reopened = await self._inspection_wakeup.reopen_waiting_evidence(task_id=task)
                progress = progress or reopened > 0
            if recapture.consumed_action_count:
                current_budget = _consume_actions(
                    current_budget,
                    count=recapture.consumed_action_count,
                )
                consumed_actions += recapture.consumed_action_count

            # Probe 派发前复核本轮期限；零剩余预算时跳过新编排，不抛错以免阻断收口。
            probe_gate_now = self._checked_now()
            if probe_gate_now >= deadline and self._exhaustion_settlement is None:
                raise AppSupplementalEvidenceDrainError("App 补采排空超过任务截止时间")
            probe_outcome = None
            try:
                if can_start_action(refresh_budget()):
                    probe_outcome = await self._probe_runtime.run_one_turn(
                        task_id=task,
                        agent_run_id=run_id,
                        work_type=ExplorerWorkType.PROBE,
                    )
            except AppProbeCompletionError as exc:
                if not exc.retryable:
                    raise
                progress = True
            if getattr(probe_outcome, "claimed", False):
                probe_turns += 1
                progress = True
                if _probe_consumed_action(probe_outcome):
                    current_budget = _consume_actions(current_budget, count=1)
                    consumed_actions += 1
                if getattr(probe_outcome, "final_status", None) is ExplorerWorkStatus.SUCCEEDED:
                    try:
                        await self._probe_completion.complete(
                            task_id=task,
                            outcome=probe_outcome,
                        )
                    except AppProbeCompletionError as exc:
                        if not exc.retryable:
                            raise
                    else:
                        await self._inspection_wakeup.reopen_waiting_evidence(task_id=task)

            state = await self._state_reader.load(
                task_id=task,
                agent_run_id=run_id,
                finalization_epoch=finalization_epoch,
            )
            current_time = refresh_budget()

            # 到达任一动作边界后，只收口已经受理但尚未执行的请求和 Probe；
            # 进行中、已有结果或待挂回的事实由下一轮恢复链继续处理。
            if not can_start_action(current_time) and not state.drained:
                settled = await self._settle_exhausted(
                    task_id=task,
                    agent_run_id=run_id,
                    finalization_epoch=finalization_epoch,
                    current_time=current_time,
                    current_budget=current_budget,
                    deadline=deadline,
                )
                current_time = refresh_budget()
                if settled.changed:
                    progress = True
                    reopened = await self._inspection_wakeup.reopen_waiting_evidence(
                        task_id=task
                    )
                    progress = progress or reopened > 0
                    state = await self._state_reader.load(
                        task_id=task,
                        agent_run_id=run_id,
                        finalization_epoch=finalization_epoch,
                    )
                    current_time = refresh_budget()
            if inspection_round.state.drained and state.drained:
                return AppSupplementalEvidenceDrainResult(
                    round_count=round_count,
                    request_attempt_count=request_attempts,
                    probe_turn_count=probe_turns,
                    recovered_completion_count=recovered_completions,
                    consumed_action_count=consumed_actions,
                    remaining_budget=current_budget,
                )

            if not progress:
                if current_time >= deadline:
                    raise AppSupplementalEvidenceDrainError("App 补采排空超过任务截止时间")
                if current_budget.remaining_duration_ms == 0:
                    raise AppSupplementalEvidenceDrainError("App 补采排空耗尽剩余时长")
                if current_budget.resolved_remaining_business_actions == 0:
                    raise AppSupplementalEvidenceDrainError("App 补采排空耗尽业务动作预算")
                delay = _poll_delay(
                    now=current_time,
                    deadline=deadline,
                    next_attempt_at=state.next_attempt_at,
                    max_poll_seconds=self._max_poll_seconds,
                )
                delay = min(delay, current_budget.remaining_duration_ms / 1000)
                if delay <= 0:
                    raise AppSupplementalEvidenceDrainError("App 补采排空没有可等待的重试窗口")
                await self._sleep(delay)
        raise AppSupplementalEvidenceDrainError("App 补采排空超过最大轮数")

    async def _settle_exhausted(
        self,
        *,
        task_id: str,
        agent_run_id: str,
        finalization_epoch: int,
        current_time: datetime,
        current_budget: ExplorationBudget,
        deadline: datetime,
    ) -> AppSupplementalEvidenceExhaustionSettlement:
        """只在不能再发起动作时委托精确执行域结算。"""

        settler = self._exhaustion_settlement
        if settler is None:
            return AppSupplementalEvidenceExhaustionSettlement()
        if current_time >= deadline:
            reason_code = "SUPPLEMENTAL_DRAIN_DEADLINE_EXHAUSTED"
        elif current_budget.remaining_duration_ms == 0:
            reason_code = "SUPPLEMENTAL_DRAIN_DURATION_EXHAUSTED"
        else:
            reason_code = "SUPPLEMENTAL_DRAIN_ACTION_BUDGET_EXHAUSTED"
        return await settler.settle_exhausted(
            task_id=task_id,
            agent_run_id=agent_run_id,
            finalization_epoch=finalization_epoch,
            reason_code=reason_code,
        )

    def _checked_now(self) -> datetime:
        value = self._now()
        if value.utcoffset() is None:
            raise ValueError("clock 必须返回带时区时间")
        return value


def _consume_actions(budget: ExplorationBudget, *, count: int) -> ExplorationBudget:
    if count > budget.resolved_remaining_business_actions:
        raise AppSupplementalEvidenceDrainError("App 补采动作超出剩余业务预算")
    current = budget
    for _ in range(count):
        current = consume_exploration_action_budget(
            current,
            counts_toward_business=True,
            elapsed_duration_ms=0,
        )
    return current


def _probe_consumed_action(outcome: object) -> bool:
    turn = getattr(outcome, "turn", None)
    return getattr(turn, "coordination", None) is not None


def _poll_delay(
    *,
    now: datetime,
    deadline: datetime,
    next_attempt_at: datetime | None,
    max_poll_seconds: float,
) -> float:
    remaining = max((deadline - now).total_seconds(), 0.0)
    delay = min(max_poll_seconds, remaining)
    if next_attempt_at is not None:
        delay = min(delay, max((next_attempt_at - now).total_seconds(), 0.05))
    return delay


def _require_text(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError(f"{field_name} 必须为无首尾空白的非空字符串")
    return value
