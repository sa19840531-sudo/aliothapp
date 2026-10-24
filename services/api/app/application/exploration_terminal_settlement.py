"""把已完成一轮的预算与观察引用保存到现有终态，不启动新的探索。"""

from collections.abc import Mapping
from dataclasses import replace

from services.api.app.application.bounded_exploration_scheduler import (
    BOUNDED_EXPLORATION_STATE_KEY,
)
from services.api.app.domain.exploration import ExplorationTerminal, ExplorationWork


def with_terminal_settlement(
    terminal: ExplorationTerminal,
    *,
    result_work: ExplorationWork,
) -> ExplorationTerminal:
    """终态归属仍是父 Work；预算和调度摘要来自已验证的本轮结果。"""
    metadata = result_work.frontier.observation.metadata
    if (
        terminal.task_id != result_work.task_id
        or metadata.get("parent_work_id") != terminal.work_id
    ):
        raise ValueError("终态结算与结果 Work 谱系不一致")
    summary = {
        **dict(terminal.coverage_summary),
        "result_capture_id": result_work.source_capture_id,
        "settled_budget": result_work.budget.to_dict(),
        "consumed_action_count": (
            result_work.budget.max_actions - result_work.budget.remaining_actions
        ),
    }
    scheduler = metadata.get(BOUNDED_EXPLORATION_STATE_KEY)
    if isinstance(scheduler, Mapping):
        summary[BOUNDED_EXPLORATION_STATE_KEY] = dict(scheduler)
    return replace(terminal, coverage_summary=summary)
