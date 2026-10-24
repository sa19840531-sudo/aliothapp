"""V1 WorkingMemory Frontier 的同步选择器。"""

from services.api.app.agents.runtime.legacy_dispatch.contracts import (
    LegacyExplorationDecision,
    LegacyExplorationFrontier,
    LegacyExplorationRequest,
)


class CurrentLegacyFrontierSelector:
    """保留现有稳定排序与状态迁移，但不声明为 Explorer Agent。"""

    def decide_next(
        self,
        *,
        frontier: LegacyExplorationFrontier,
        request: LegacyExplorationRequest,
    ) -> LegacyExplorationDecision | None:
        action = frontier.prepare_next_exploration_action(
            blocking_page_capture_id=request.blocking_page_capture_id
        )
        if action is None:
            return None
        return LegacyExplorationDecision.from_action_payload(action)
