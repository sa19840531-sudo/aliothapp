"""把 App 当前分支的无进展停止交给已有 Frontier 恢复流程。"""

from dataclasses import replace

from services.api.app.application.bounded_exploration_scheduler import (
    BOUNDED_EXPLORATION_STATE_KEY,
    ExplorationStopReason,
    scheduler_state_from_work,
)
from services.api.app.domain.exploration import (
    ExplorationTerminal,
    ExplorationTerminalStatus,
    ExplorationWork,
)
from services.api.app.domain.interaction import ActionIntent


def app_branch_no_progress_terminal(work: ExplorationWork) -> ExplorationTerminal | None:
    """只转换分支无进展；预算、时间及循环上限仍按原优先级停止。"""

    try:
        state = scheduler_state_from_work(work)
    except (TypeError, ValueError):
        # 损坏账本仍交唯一派发入口按 state_invalid 处理，不能重置后继续。
        return None
    if state is None or state.stop_reason() is not ExplorationStopReason.NO_PROGRESS_LIMIT:
        return None
    if replace(state, no_progress_count=0).stop_reason() is not ExplorationStopReason.NONE:
        return None
    return ExplorationTerminal(
        task_id=work.task_id,
        work_id=work.work_id,
        status=ExplorationTerminalStatus.BOUNDED,
        outcome="evidence_insufficient",
        reason_code="bounded_exploration_no_progress_limit",
        reason="当前分支连续无进展，检查任务级 Frontier 是否仍可恢复。",
        coverage_summary={"scheduler_stop_reason": "no_progress_limit"},
    )


def resume_app_frontier_branch(
    work: ExplorationWork,
    decision: ActionIntent,
) -> ExplorationWork:
    """为已选定分支开启有限恢复窗口，不把恢复准入记作实际进展。"""

    state = scheduler_state_from_work(work)
    if (
        state is None
        or state.stop_reason() is not ExplorationStopReason.NO_PROGRESS_LIMIT
        or replace(state, no_progress_count=0).stop_reason() is not ExplorationStopReason.NONE
        or not decision.parameters.get("app_frontier_item_id")
    ):
        return work
    # restore_only 同样需要通过派发检查；其按候选累计的恢复次数继续保留，
    # 上限判断仍由既有恢复流程负责，不因本窗口清零而重新获得恢复次数。
    metadata = dict(work.frontier.observation.metadata)
    metadata[BOUNDED_EXPLORATION_STATE_KEY] = replace(state, no_progress_count=0).to_dict()
    return replace(
        work,
        frontier=replace(
            work.frontier,
            observation=replace(work.frontier.observation, metadata=metadata),
        ),
    )
