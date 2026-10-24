"""H5 检查点按每轮新来源重规划，动作只经既有 Coordinator 执行。"""

from dataclasses import replace
from datetime import UTC, datetime

from services.api.app.domain.exploration import ExplorationTerminal, ExplorationTerminalStatus
from services.api.app.domain.interaction import ActionIntent
from services.api.app.domain.journey.validation import stable_hash

ADAPTIVE_BINDING_KEY = "h5_checkpoint_attempt"


class H5AdaptiveJourneyRunner:
    def __init__(self, *, repository, planner):
        self._repository = repository
        self._planner = planner

    async def prepare(self, *, work, agent_run_id):
        if work.deadline <= datetime.now(UTC) or work.budget.remaining_actions <= 0:
            return work
        restore = getattr(self._repository, "restore_work", None)
        if restore is not None:
            prior = await restore(work=work, agent_run_id=agent_run_id)
            if prior is not None:
                return prior
        plans = await self._repository.list_plans(task_id=work.task_id, agent_run_id=agent_run_id)
        if not plans:
            return work
        pending = await self._repository.next_checkpoint(work=work, plans=plans)
        if pending is None:
            return work
        plan, checkpoint = pending
        if checkpoint.side_effect_policy != "read_only":
            return work
        # 规划只改变同现场的候选选择，不新建会话、不恢复或绕过风险门禁。
        prepared = await self._planner.prepare(work=work, plan=plan, checkpoint=checkpoint)
        if prepared is None:
            return work
        if (
            prepared.work_id != work.work_id
            or prepared.source_capture_id != work.source_capture_id
            or prepared.session_id != work.session_id
            or prepared.deadline != work.deadline
            or prepared.budget != work.budget
        ):
            raise ValueError("检查点规划不得改变 Work 来源、会话、预算或截止")
        context = {
            "execution_plan_id": plan.execution_plan_id,
            "checkpoint_id": checkpoint.checkpoint_id,
            "task_id": work.task_id,
            "agent_run_id": agent_run_id,
            "session_id": work.session_id,
            "source_capture_id": work.source_capture_id,
            "work_id": work.work_id,
        }
        context["attempt_id"] = "checkpoint-attempt:" + stable_hash(context)
        observation = prepared.frontier.observation
        frontier = replace(
            prepared.frontier,
            observation=replace(
                observation,
                metadata={**observation.metadata, ADAPTIVE_BINDING_KEY: context},
            ),
        )
        return replace(prepared, frontier=frontier)

    async def execute(self, *, work, agent_run_id, executor):
        context = work.frontier.observation.metadata.get(ADAPTIVE_BINDING_KEY)
        if context is None:
            return await executor.execute(work=work, agent_run_id=agent_run_id)
        for name, value in (
            ("task_id", work.task_id),
            ("agent_run_id", agent_run_id),
            ("session_id", work.session_id),
            ("work_id", work.work_id),
            ("source_capture_id", work.source_capture_id),
        ):
            if context.get(name) != value:
                raise ValueError("检查点尝试来源串属")
        prior = await self._repository.recover_attempt(work=work, context=context)
        if prior == "bound":
            return await self._repository.read_turn(work=work, context=context)
        if prior in ("reserved", "gap"):
            return await self._uncertain(work=work, agent_run_id=agent_run_id, executor=executor)
        if prior is not None:
            raise ValueError("检查点原尝试状态无效")
        decision = await executor.decide(work=work)
        if not isinstance(decision, ActionIntent):
            return await executor.execute_decision(
                work=work, agent_run_id=agent_run_id, decision=decision
            )
        checkpoint = await self._repository.get_checkpoint(context=context)
        from services.api.app.application.h5_checkpoint_observation import checkpoint_needs_series

        if checkpoint_needs_series(checkpoint):
            after = decision.capture_profile.after
            decision = replace(
                decision,
                capture_profile=replace(
                    decision.capture_profile,
                    after=replace(
                        after,
                        artifact_types=tuple(
                            dict.fromkeys(
                                (
                                    *after.artifact_types,
                                    "screenshot",
                                    "ui_snapshot",
                                    "media_state",
                                    "checkpoint_observation",
                                ),
                            )
                        ),
                    ),
                ),
            )
        claim = await self._repository.reserve(work=work, context=context, decision=decision)
        if claim == "uncertain":
            return await self._uncertain(work=work, agent_run_id=agent_run_id, executor=executor)
        if claim == "bound":
            return await self._repository.read_turn(work=work, context=context)
        if claim != "new":
            raise ValueError("检查点尝试准入返回值无效")
        try:
            turn = await executor.execute_decision(
                work=work, agent_run_id=agent_run_id, decision=decision
            )
            if turn.coordination is None:
                await self._repository.fail(context=context, reason="no_action_receipt")
                return turn
            await self._repository.bind(work=work, context=context, turn=turn)
            return turn
        except Exception as exc:
            # 原始证据仍由 Coordinator 保留；失败只记录缺口，不重放副作用。
            await self._repository.fail(context=context, reason=type(exc).__name__)
            raise

    async def _uncertain(self, *, work, agent_run_id, executor):
        terminal = ExplorationTerminal(
            work_id=work.work_id,
            task_id=work.task_id,
            status=ExplorationTerminalStatus.BLOCKED,
            reason="检查点旧尝试尚未完整绑定，禁止重放动作",
            outcome="evidence_insufficient",
            reason_code="H5_CHECKPOINT_ATTEMPT_UNCERTAIN",
        )
        return await executor.execute_decision(
            work=work, agent_run_id=agent_run_id, decision=terminal
        )
