"""检查点目标只选择本帧已签发候选，既有调度器继续裁决预算和风险。"""

import json
from dataclasses import replace

from services.api.app.agents.explorer.maf_agent import ExplorerPrematureTerminalError
from services.api.app.application.bounded_exploration_scheduler import (
    BoundedExplorationScheduler,
    scheduler_state_from_metadata,
)
from services.api.app.domain.exploration import build_exploration_work
from services.api.app.domain.exploration.h5_frontier import (
    H5_TASK_FRONTIER_METADATA_KEY,
    H5TaskFrontierSnapshot,
)
from services.api.app.domain.interaction import ActionIntent, InteractionRiskLevel


class H5CheckpointPlanner:
    def __init__(self, *, delegate_factory, shortflow_delegate_factory=None):
        self._delegate_factory = delegate_factory
        self._shortflow_delegate_factory = shortflow_delegate_factory

    async def prepare(self, *, work, plan, checkpoint):
        from services.api.app.application.h5_exploration_workflow import _with_task_frontier

        skill = plan.parameters.get("workflow_skill")
        skill_hash = plan.parameters.get("workflow_skill_hash")
        if skill is not None or skill_hash is not None:
            from services.api.app.agents.skills.loader import h5_shortflow_skill_hash

            if h5_shortflow_skill_hash(skill) != skill_hash:
                raise ValueError("H5 短流程 Skill 内容漂移")
            if self._shortflow_delegate_factory is None:
                raise ValueError("H5 短流程 Agent 未接线")

        observation = work.frontier.observation
        if (
            observation.page_state != "stable"
            or observation.overlay_signature is not None
            or observation.metadata.get("requires_auth") is True
        ):
            return None
        raw = observation.metadata.get(H5_TASK_FRONTIER_METADATA_KEY)
        if raw is None:
            return None
        snapshot = H5TaskFrontierSnapshot.from_dict(raw)
        eligible = tuple(
            entry
            for entry in snapshot.entries
            if entry.status.value in ("selected", "pending")
            and entry.source_capture_id == work.source_capture_id
            and entry.source_state_version == observation.state_version
            and entry.candidate.risk_level is InteractionRiskLevel.LOW
        )
        state = scheduler_state_from_metadata(
            observation.metadata,
            max_actions=work.budget.max_actions,
            max_duration_ms=work.budget.remaining_duration_ms,
            deadline=work.deadline,
        )
        allowed = BoundedExplorationScheduler().filter_candidates(
            tuple(entry.candidate for entry in eligible),
            state=state,
            missing_page_types=frozenset(),
        )
        eligible = tuple(
            entry
            for entry in eligible
            if entry.candidate in allowed
            and entry.candidate.action_type in work.boundaries.allowed_operations
            and entry.candidate.navigation_depth <= work.budget.max_depth
            and entry.candidate.candidate_id not in work.frontier.blocked_candidate_ids
        )
        if not eligible:
            return None
        # 唯一候选不等于相关候选；只对已签发的原生全屏合同免模型选择。
        native_fullscreen = (
            len(eligible) == 1
            and dict(checkpoint.expected) == {"fullscreen_active": True}
            and (eligible[0].candidate.metadata.get("action_parameters") or {}).get(
                "goal_capability"
            )
            == "native_video_fullscreen"
        )
        if native_fullscreen:
            chosen = eligible[0]
        else:
            metadata = dict(observation.metadata)
            metadata.pop(H5_TASK_FRONTIER_METADATA_KEY, None)
            metadata["objective_satisfied"] = False
            selection = build_exploration_work(
                task_id=work.task_id,
                session_id=work.session_id,
                branch_id=f"checkpoint:{plan.execution_plan_id}:{checkpoint.checkpoint_id}",
                objective="仅选当前安全候选推进检查点，不判通过；无关时 blocked。合同："
                + json.dumps(checkpoint.to_dict(), ensure_ascii=False),
                surface_kind=work.surface_kind,
                source_capture_id=work.source_capture_id,
                frontier_snapshot_id=work.frontier_snapshot_id,
                frontier=replace(
                    work.frontier,
                    candidates=tuple(entry.candidate for entry in eligible),
                    observation=replace(observation, metadata=metadata),
                ),
                boundaries=work.boundaries,
                budget=work.budget,
                deadline=work.deadline,
            )
            try:
                delegate = (self._shortflow_delegate_factory(skill, skill_hash)
                            if skill is not None else self._delegate_factory())
                decision = await delegate.decide_next(selection)
            except ExplorerPrematureTerminalError:
                # 主探索有候选不代表有相关检查点动作，不能退回盲选。
                return None
            if not isinstance(decision, ActionIntent):
                return None
            chosen = next(
                (
                    entry
                    for entry in eligible
                    if entry.candidate.candidate_id == decision.parameters.get("candidate_id")
                    and decision.source_capture_id == work.source_capture_id
                    and decision.session_id == work.session_id
                    and decision.task_id == work.task_id
                    and decision.action_type == entry.candidate.action_type
                    and decision.semantic_target == entry.candidate.semantic_target
                ),
                None,
            )
            if chosen is None:
                raise ValueError("检查点模型选择未绑定本帧候选")
        for entry in snapshot.entries:
            if entry.status.value == "selected" and entry.entry_id != chosen.entry_id:
                snapshot = snapshot.defer_selected(entry.entry_id)
        current = next(entry for entry in snapshot.entries if entry.entry_id == chosen.entry_id)
        if current.status.value == "pending":
            snapshot = snapshot.select_entry(chosen.entry_id)
        frontier = _with_task_frontier(frontier=work.frontier, snapshot=snapshot)
        return replace(work, frontier=frontier, frontier_snapshot_id=frontier.snapshot_id)
