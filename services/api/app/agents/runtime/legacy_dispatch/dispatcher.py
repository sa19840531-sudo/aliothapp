"""V1 Main Agent 的同步兼容派发器。"""

from __future__ import annotations

from services.api.app.agents.runtime.legacy_dispatch.contracts import (
    LegacyExplorationRequest,
    LegacyFrontierSelector,
)
from services.api.app.agents.runtime.legacy_dispatch.exploration_dispatch import (
    build_legacy_exploration_dispatch,
)
from services.api.app.agents.runtime.legacy_dispatch.frontier_selector import (
    CurrentLegacyFrontierSelector,
)
from services.api.app.agents.runtime.legacy_dispatch.models import WorkDispatch
from services.api.app.agents.runtime.legacy_dispatch.rule_dispatch import (
    RuleDispatchPlanner,
)
from services.api.app.agents.runtime.legacy_dispatch.support import (
    integer,
    required_tool,
    text,
)
from services.api.app.agents.runtime.working_memory import WorkingMemory


class LegacyV1WorkDispatcher:
    """保留 V1 页面阶段顺序；不属于 V2 Orchestrator 或 Explorer Agent。"""

    def __init__(
        self,
        *,
        selector: LegacyFrontierSelector | None = None,
        rule_dispatch_planner: RuleDispatchPlanner | None = None,
    ) -> None:
        self._selector = selector if selector is not None else CurrentLegacyFrontierSelector()
        self._rule_dispatch_planner = (
            rule_dispatch_planner if rule_dispatch_planner is not None else RuleDispatchPlanner()
        )

    def prepare_next(self, working_memory: WorkingMemory) -> WorkDispatch | None:
        """按页内阶段优先、Explorer 动态决策次之的顺序派发一步。"""
        snapshot = working_memory.model_snapshot()
        current_page = snapshot.get("current_page")
        if isinstance(current_page, dict):
            page_capture_id = text(current_page.get("page_capture_id"))
            if page_capture_id is not None:
                blocking_decision = self._selector.decide_next(
                    frontier=working_memory,
                    request=LegacyExplorationRequest(
                        blocking_page_capture_id=page_capture_id,
                    ),
                )
                if (
                    blocking_decision is not None
                    and _can_execute_before_source_inspection(blocking_decision)
                    and not _has_unfinished_candidate_rules(current_page)
                ):
                    return build_legacy_exploration_dispatch(blocking_decision)

                detection = current_page.get("page_detection")
                if not isinstance(detection, dict):
                    return WorkDispatch(
                        kind="page_detection",
                        tool_name="page_detection_tool",
                        arguments={"page_capture_id": page_capture_id},
                        tool_choice=required_tool("page_detection_tool"),
                        message=(
                            "当前动态采集尚未识别，必须立即调用 "
                            f"page_detection_tool(page_capture_id={page_capture_id!r})。"
                            "本轮禁止输出结束文本，也禁止再次采集。"
                        ),
                    )

                if (
                    detection.get("detection_status") == "recognized"
                    and detection.get("rule_matching_allowed") is True
                ):
                    page_type = text(detection.get("page_type"))
                    page_state = text(detection.get("page_state"))
                    candidate_rule_count = integer(current_page.get("candidate_rule_count"))
                    completed_rule_count = integer(current_page.get("completed_rule_count"))
                    if current_page.get("inspection_status") == "matching":
                        arguments = {
                            "page_capture_id": page_capture_id,
                            "page_type": page_type,
                        }
                        if page_state is not None:
                            arguments["page_state"] = page_state
                        return WorkDispatch(
                            kind="rule_matching",
                            tool_name="candidate_rules_tool",
                            arguments=arguments,
                            tool_choice=required_tool("candidate_rules_tool"),
                            message=(
                                "当前页面已识别但尚未匹配规则，必须立即调用 "
                                "candidate_rules_tool，参数必须为 "
                                f"page_capture_id={page_capture_id!r}, "
                                f"page_type={page_type!r}, page_state={page_state!r}。"
                                "本轮禁止输出结束文本。"
                            ),
                        )
                    if completed_rule_count < candidate_rule_count:
                        rule_dispatch = self._rule_dispatch_planner.prepare(
                            current_page=current_page,
                            page_capture_id=page_capture_id,
                        )
                        if rule_dispatch is not None:
                            return rule_dispatch

                if blocking_decision is not None:
                    return build_legacy_exploration_dispatch(blocking_decision)

                if _should_abandon_unmatchable_detection(detection):
                    detection_status = text(detection.get("detection_status")) or "unmatchable"
                    if working_memory.abandon_unmatchable_page(
                        page_capture_id=page_capture_id,
                        reason=f"page_detection_{detection_status}",
                    ):
                        decision = self._selector.decide_next(
                            frontier=working_memory,
                            request=LegacyExplorationRequest(),
                        )
                        if decision is not None:
                            return build_legacy_exploration_dispatch(decision)
                        return None

                # 同页没有恢复动作时，重新识别是唯一不会越过页面边界的安全推进。
                return WorkDispatch(
                    kind="page_redetection",
                    tool_name="page_detection_tool",
                    arguments={"page_capture_id": page_capture_id},
                    tool_choice=required_tool("page_detection_tool"),
                    message=(
                        f"页面 {page_capture_id} 尚未进入可统计终态且没有可执行恢复动作。"
                        "请立即重新调用 page_detection_tool 补充识别；"
                        "本轮禁止输出结束文本或跳转其他页面。"
                    ),
                )

        decision = self._selector.decide_next(
            frontier=working_memory,
            request=LegacyExplorationRequest(),
        )
        return build_legacy_exploration_dispatch(decision) if decision is not None else None


def _should_abandon_unmatchable_detection(detection: dict[str, object]) -> bool:
    """无法靠同一证据重试变清晰的页面直接释放 Frontier。"""
    return text(detection.get("detection_status")) == "ambiguous"


def _can_execute_before_source_inspection(decision) -> bool:
    """显式不依赖源页质检完成的恢复动作可继续前置执行。"""
    action = decision.to_action_dict()
    return action.get("requires_source_inspection_complete") is False


def _has_unfinished_candidate_rules(current_page: dict[str, object]) -> bool:
    """当前页已匹配规则时，必须先收口规则结果再执行页面恢复动作。"""
    return integer(current_page.get("completed_rule_count")) < integer(
        current_page.get("candidate_rule_count")
    )
