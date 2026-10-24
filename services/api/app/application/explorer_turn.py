"""Explorer 一轮决策到受审计 Interaction 的应用层接线。"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Protocol

from services.api.app.application.capture_coordination import (
    CaptureCoordinationCommand,
    CaptureCoordinationResult,
)
from services.api.app.application.capture_coordinator import CaptureCoordinator
from services.api.app.application.exploration_dispatch_guard import bounded_dispatch_terminal
from services.api.app.domain.exploration import ExplorationTerminal, ExplorerWork
from services.api.app.domain.interaction import (
    ActionIntent,
    CaptureRef,
    SurfaceKind,
    TimedObservationIntent,
)

ExplorerBeforeSideEffect = Callable[
    [ExplorerWork, str, ActionIntent | TimedObservationIntent], Awaitable[None]
]


class ExplorerDecisionPort(Protocol):
    """应用层需要的最小 Explorer Agent 能力。"""

    async def decide_next(
        self,
        work: ExplorerWork,
    ) -> ActionIntent | TimedObservationIntent | ExplorationTerminal: ...


@dataclass(frozen=True, kw_only=True)
class ExplorerTurnResult:
    """一次 Explorer 决策及其可选的动作证据闭环。"""

    work_id: str
    task_id: str
    decision: ActionIntent | TimedObservationIntent | ExplorationTerminal
    coordination: CaptureCoordinationResult | None
    observation_capture: CaptureRef | None = None

    @property
    def is_terminal(self) -> bool:
        return isinstance(self.decision, ExplorationTerminal)


class ExplorerTurnExecutor:
    """不持有队列租约，只执行一轮 Agent 决策与 Capture 原子边界。"""

    def __init__(
        self,
        *,
        explorer: ExplorerDecisionPort,
        capture_coordinator: CaptureCoordinator,
        before_side_effect: ExplorerBeforeSideEffect | None = None,
    ) -> None:
        self._explorer = explorer
        self._capture_coordinator = capture_coordinator
        self._before_side_effect = before_side_effect

    async def execute(
        self,
        *,
        work: ExplorerWork,
        agent_run_id: str,
    ) -> ExplorerTurnResult:
        decision = await self.decide(work=work)
        return await self.execute_decision(
            work=work,
            agent_run_id=agent_run_id,
            decision=decision,
        )

    async def decide(
        self,
        *,
        work: ExplorerWork,
    ) -> ActionIntent | TimedObservationIntent | ExplorationTerminal:
        """只形成决策，供调用方在设备动作前先锁定额外业务事实。"""

        decision = await self._explorer.decide_next(work)
        decision = _bind_action_to_work(work=work, decision=decision)
        _validate_decision_identity(work=work, decision=decision)
        return decision

    async def decide_deterministic(
        self,
        *,
        work: ExplorerWork,
    ) -> ActionIntent | TimedObservationIntent | ExplorationTerminal | None:
        """先执行装饰器可证明的阶段动作；没有结果时再交给普通 Explorer。"""

        decide = getattr(self._explorer, "decide_deterministic", None)
        if not callable(decide):
            return None
        decision = await decide(work)
        if decision is None:
            return None
        decision = _bind_action_to_work(work=work, decision=decision)
        _validate_decision_identity(work=work, decision=decision)
        return decision

    async def decide_after_deterministic(
        self,
        *,
        work: ExplorerWork,
    ) -> ActionIntent | TimedObservationIntent | ExplorationTerminal:
        """阶段机已检查后直接进入普通决策，避免同一 Work 重复推进阶段状态。"""

        decide = getattr(self._explorer, "decide_after_deterministic", None)
        if not callable(decide):
            return await self.decide(work=work)
        decision = await decide(work)
        decision = _bind_action_to_work(work=work, decision=decision)
        _validate_decision_identity(work=work, decision=decision)
        return decision

    async def recover_decision(
        self,
        *,
        work: ExplorerWork,
    ) -> ActionIntent | None:
        """优先恢复已持久化意图，避免 Work 重领后由模型生成另一动作。"""

        finder = getattr(self._capture_coordinator, "find_recovery_intent", None)
        if not callable(finder):
            return None
        decision = await finder(
            task_id=work.task_id,
            session_id=work.session_id,
            exploration_work_id=work.work_id,
        )
        if decision is not None:
            _validate_decision_identity(work=work, decision=decision)
        return decision

    async def execute_decision(
        self,
        *,
        work: ExplorerWork,
        agent_run_id: str,
        decision: ActionIntent | TimedObservationIntent | ExplorationTerminal,
    ) -> ExplorerTurnResult:
        """执行已经通过身份校验的决策，不允许调用方替换 Work 来源。"""

        decision = _bind_action_to_work(work=work, decision=decision)
        _validate_decision_identity(work=work, decision=decision)
        decision = (
            bounded_dispatch_terminal(work=work, decision=decision, now=datetime.now(UTC))
            or decision
        )
        if isinstance(decision, ExplorationTerminal):
            return ExplorerTurnResult(
                work_id=work.work_id,
                task_id=work.task_id,
                decision=decision,
                coordination=None,
            )
        if self._before_side_effect is not None:
            await self._before_side_effect(
                work=work,
                agent_run_id=agent_run_id,
                decision=decision,
            )
        # 钩子可能等待租约或重新识别页面，返回时必须再次检查截止时间。
        bounded = bounded_dispatch_terminal(work=work, decision=decision, now=datetime.now(UTC))
        if bounded is not None:
            return ExplorerTurnResult(
                work_id=work.work_id, task_id=work.task_id, decision=bounded, coordination=None,
            )
        if isinstance(decision, TimedObservationIntent):
            remaining_seconds = (decision.deadline - datetime.now(UTC)).total_seconds()
            if remaining_seconds <= 0 or decision.delay_ms / 1_000 > remaining_seconds:
                raise TimeoutError("Timed Observation 超出探索截止时间")
            if decision.delay_ms > 0:
                await asyncio.sleep(decision.delay_ms / 1_000)
            # 等待可能跨过持久化截止时间；醒来后仍不得发送一次额外采集。
            bounded = bounded_dispatch_terminal(
                work=work, decision=decision, now=datetime.now(UTC),
            )
            if bounded is not None:
                return ExplorerTurnResult(
                    work_id=work.work_id, task_id=work.task_id,
                    decision=bounded, coordination=None,
                )
            if datetime.now(UTC) >= decision.deadline:
                raise TimeoutError("Timed Observation 等待后已超出探索截止时间")
            observation_capture = await self._capture_coordinator.capture_observation(
                agent_run_id=agent_run_id,
                intent=decision,
            )
            return ExplorerTurnResult(
                work_id=work.work_id,
                task_id=work.task_id,
                decision=decision,
                coordination=None,
                observation_capture=observation_capture,
            )
        coordination = await self._capture_coordinator.coordinate(
            CaptureCoordinationCommand(
                agent_run_id=agent_run_id,
                intent=decision,
            )
        )
        return ExplorerTurnResult(
            work_id=work.work_id,
            task_id=work.task_id,
            decision=decision,
            coordination=coordination,
        )


def _validate_decision_identity(
    *,
    work: ExplorerWork,
    decision: ActionIntent | TimedObservationIntent | ExplorationTerminal,
) -> None:
    if decision.task_id != work.task_id:
        raise ValueError("Explorer 决策 task_id 与工作项不一致")
    if isinstance(decision, ExplorationTerminal):
        if decision.work_id != work.work_id:
            raise ValueError("Explorer 终态 work_id 与工作项不一致")
        return
    if isinstance(decision, TimedObservationIntent):
        if decision.session_id != work.session_id:
            raise ValueError("TimedObservationIntent session_id 与工作项不一致")
        if decision.surface_kind is not work.surface_kind:
            raise ValueError("TimedObservationIntent surface_kind 与工作项不一致")
        if decision.source_capture_id != work.source_capture_id:
            raise ValueError("TimedObservationIntent source_capture_id 与工作项不一致")
        return
    if decision.session_id != work.session_id:
        raise ValueError("ActionIntent session_id 与工作项不一致")
    if decision.surface_kind is not work.surface_kind:
        raise ValueError("ActionIntent surface_kind 与工作项不一致")
    if decision.source_capture_id != work.source_capture_id:
        raise ValueError("ActionIntent source_capture_id 与工作项不一致")


def _bind_action_to_work(
    *,
    work: ExplorerWork,
    decision: ActionIntent | TimedObservationIntent | ExplorationTerminal,
) -> ActionIntent | TimedObservationIntent | ExplorationTerminal:
    """App 动作落账前绑定 Work 身份，恢复时以此读取原始意图。"""

    if not isinstance(decision, ActionIntent) or decision.surface_kind is SurfaceKind.WEB:
        return decision
    parameters = dict(decision.parameters)
    existing = parameters.get("exploration_work_id")
    if existing not in {None, work.work_id}:
        raise ValueError("ActionIntent exploration_work_id 与工作项不一致")
    parameters["exploration_work_id"] = work.work_id
    return replace(decision, parameters=parameters)
