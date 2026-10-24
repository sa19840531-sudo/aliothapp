"""任务收口结论：页面探索、证据充分、规则结果、最终完成检查分别给出结果。

排期「完善任务结束和资源清理流程」的读侧口径（01 专项 §6 步骤 9「结算剩余工作、冻结报告
输入并释放资源」）。四个维度各自独立：探索走完不等于证据够，证据够不等于规则判完，规则
判完也不等于完成检查放行。任何一维不达标都要说清楚是哪一条挡着、还差多少，不能只给一个
「未完成」。

本模块只做解释与判读，不查库、不改状态。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from services.api.app.domain.task.completion import CompletionBlockReason

CLOSURE_OUTCOME_PROTOCOL_VERSION = "task-closure-outcome.v1"


class ClosureDimensionState(StrEnum):
    """单个维度的结论。"""

    SATISFIED = "satisfied"
    INCOMPLETE = "incomplete"
    BLOCKED = "blocked"
    UNKNOWN = "unknown"


DIMENSION_STATE_LABELS: Mapping[ClosureDimensionState, str] = {
    ClosureDimensionState.SATISFIED: "达标",
    ClosureDimensionState.INCOMPLETE: "未走完",
    ClosureDimensionState.BLOCKED: "被挡住",
    ClosureDimensionState.UNKNOWN: "还没有结论",
}

# 完成检查的每条阻断原因：给运营看的大白话解释 + 该怎么办。
BLOCK_REASON_EXPLANATIONS: Mapping[str, tuple[str, str]] = {
    CompletionBlockReason.EXPLORATION_NOT_TERMINAL.value: (
        "页面探索还没走完",
        "探索还在跑或异常中断，等它结束或先看探索失败原因。",
    ),
    CompletionBlockReason.PENDING_ACTION.value: (
        "还有页面操作没做完",
        "点击、滑动等动作还在执行，等它们收尾。",
    ),
    CompletionBlockReason.PENDING_CAPTURE_COMMIT.value: (
        "还有截图没存完",
        "采集到的截图还没全部落库，等落库完成。",
    ),
    CompletionBlockReason.NONTERMINAL_ENFORCED_WORK_ITEM.value: (
        "还有规则检查没有结果",
        "有规则还停在等证据或重试里，等它们结算；长时间不动要看工作项卡在哪一步。",
    ),
    CompletionBlockReason.ACTIVE_SESSION_REQUEST.value: (
        "还有依赖当前浏览器会话的补采请求",
        "会话关掉这些请求就作废，等它们跑完或按不可补结算。",
    ),
    CompletionBlockReason.CANDIDATE_RULE_COVERAGE_NONTERMINAL.value: (
        "命中的规则还没全部判完",
        "有规则匹配上了但还没出结论，等判定完成。",
    ),
    CompletionBlockReason.BACKGROUND_EXECUTION.value: (
        "还有后台任务在跑",
        "等后台执行结束。",
    ),
    CompletionBlockReason.REVIEW_POLICY_UNSATISFIED.value: (
        "还有人工确认没做",
        "去复核中心把待确认的项处理完，任务才能出最终报告。",
    ),
    CompletionBlockReason.APP_CANDIDATE_INVENTORY_EMPTY.value: (
        "App 没有采到任何可判定的页面",
        "多半卡在开屏、权限弹窗或登录，看探索过程里的阻断原因。",
    ),
    CompletionBlockReason.APP_COVERAGE_OR_VISUAL_QUALITY_INCOMPLETE.value: (
        "App 页面覆盖或截图质量不达标",
        "看探索过程里的截图质量：被遮挡或诊断材料太多时结论不算数。",
    ),
    CompletionBlockReason.H5_COVERAGE_SCOPE_INCOMPLETE.value: (
        "H5 必检页面没走全",
        "看探索过程里的页面路线，确认缺的是哪几个页型。",
    ),
    CompletionBlockReason.WEB_COVERAGE_SNAPSHOT_INCOMPLETE.value: (
        "网页端覆盖清单还没封存",
        "等覆盖快照生成；一直不生成要看探索是否正常结束。",
    ),
    CompletionBlockReason.NATURAL_EVIDENCE_PRESENTATION_VIOLATION.value: (
        "有结论用了被遮挡或诊断材料",
        "这些结论不算正式通过，要按补采缺口重新取证。",
    ),
}

# 这几条只是「等人工」，不是系统卡住：任务照样能进复核中心。
REVIEW_READY_BLOCK_REASONS: frozenset[str] = frozenset(
    {
        CompletionBlockReason.REVIEW_POLICY_UNSATISFIED.value,
        CompletionBlockReason.H5_COVERAGE_SCOPE_INCOMPLETE.value,
        CompletionBlockReason.APP_CANDIDATE_INVENTORY_EMPTY.value,
        CompletionBlockReason.APP_COVERAGE_OR_VISUAL_QUALITY_INCOMPLETE.value,
    }
)


def explain_block_reason(code: str) -> dict[str, str]:
    """把门禁阻断原因翻成运营看得懂的一句话和处理建议。"""

    label, advice = BLOCK_REASON_EXPLANATIONS.get(code, (code, "未登记的阻断原因，找研发确认。"))
    return {
        "code": code,
        "label": label,
        "advice": advice,
        "waiting_for_people": code in REVIEW_READY_BLOCK_REASONS,
    }


@dataclass(frozen=True, kw_only=True)
class ClosureDimension:
    """一个维度的结论：状态、一句话说明、支撑数字。"""

    key: str
    title: str
    state: ClosureDimensionState
    summary: str
    metrics: Mapping[str, Any] = field(default_factory=dict)
    blockers: Sequence[Mapping[str, Any]] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "title": self.title,
            "state": self.state.value,
            "state_label": DIMENSION_STATE_LABELS[self.state],
            "summary": self.summary,
            "metrics": dict(self.metrics),
            "blockers": [dict(item) for item in self.blockers],
        }


def exploration_dimension(
    *,
    exploration_terminal: bool | None,
    pending_action_count: int,
    pending_capture_commit_count: int,
    background_execution_count: int,
    visited_page_type_count: int,
) -> ClosureDimension:
    """页面探索：动作、截图、后台执行都收尾了才算走完。"""

    metrics = {
        "visited_page_type_count": visited_page_type_count,
        "pending_action_count": pending_action_count,
        "pending_capture_commit_count": pending_capture_commit_count,
        "background_execution_count": background_execution_count,
    }
    if exploration_terminal is None:
        return ClosureDimension(
            key="exploration",
            title="页面探索",
            state=ClosureDimensionState.UNKNOWN,
            summary="探索还没有产生完成检查记录，先看任务是否还在跑。",
            metrics=metrics,
        )
    outstanding = pending_action_count + pending_capture_commit_count + background_execution_count
    if not exploration_terminal or outstanding > 0:
        parts = []
        if not exploration_terminal:
            parts.append("探索本身还没结束")
        if pending_action_count:
            parts.append(f"{pending_action_count} 个动作没做完")
        if pending_capture_commit_count:
            parts.append(f"{pending_capture_commit_count} 张截图没存完")
        if background_execution_count:
            parts.append(f"{background_execution_count} 个后台任务在跑")
        return ClosureDimension(
            key="exploration",
            title="页面探索",
            state=ClosureDimensionState.INCOMPLETE,
            summary="；".join(parts) + "。",
            metrics=metrics,
        )
    return ClosureDimension(
        key="exploration",
        title="页面探索",
        state=ClosureDimensionState.SATISFIED,
        summary=f"探索已结束，走到了 {visited_page_type_count} 类页面，动作和截图都收尾了。",
        metrics=metrics,
    )


def evidence_dimension(
    *,
    gap_count: int,
    presentation_violation_count: int,
    open_request_count: int,
    settled_request_count: int,
) -> ClosureDimension:
    """证据充分：还有多少规则没拿到能判的证据，补采请求有没有结清。"""

    metrics = {
        "evidence_gap_count": gap_count,
        "presentation_violation_count": presentation_violation_count,
        "open_supplement_request_count": open_request_count,
        "settled_supplement_request_count": settled_request_count,
    }
    if open_request_count > 0:
        return ClosureDimension(
            key="evidence",
            title="证据充分",
            state=ClosureDimensionState.INCOMPLETE,
            summary=f"还有 {open_request_count} 个补采请求没有结清，先等它们出结果。",
            metrics=metrics,
        )
    if presentation_violation_count > 0:
        return ClosureDimension(
            key="evidence",
            title="证据充分",
            state=ClosureDimensionState.BLOCKED,
            summary=(
                f"有 {presentation_violation_count} 条结论用了被遮挡或诊断材料，不算正式证据。"
            ),
            metrics=metrics,
        )
    if gap_count > 0:
        return ClosureDimension(
            key="evidence",
            title="证据充分",
            state=ClosureDimensionState.INCOMPLETE,
            summary=f"有 {gap_count} 条规则证据不足，缺口已记录在补采台账里。",
            metrics=metrics,
        )
    return ClosureDimension(
        key="evidence",
        title="证据充分",
        state=ClosureDimensionState.SATISFIED,
        summary="所有规则都拿到了可判定的证据，没有留下缺口。",
        metrics=metrics,
    )


def rule_dimension(
    *,
    total: int,
    group_counts: Mapping[str, int],
    group_labels: Mapping[str, str],
) -> ClosureDimension:
    """规则结果：六态分布；判没判完和判得通不通过是两件事。"""

    metrics = {
        "total": total,
        "group_counts": dict(group_counts),
        "group_labels": dict(group_labels),
    }
    if total <= 0:
        return ClosureDimension(
            key="rules",
            title="规则结果",
            state=ClosureDimensionState.UNKNOWN,
            summary="这次任务没有命中任何规则。",
            metrics=metrics,
        )
    passed = int(group_counts.get("passed", 0))
    failed = int(group_counts.get("failed", 0))
    other = int(group_counts.get("other", 0))
    detect_error = int(group_counts.get("detect_error", 0))
    not_applicable = int(group_counts.get("not_applicable", 0))
    coverage_gap = int(group_counts.get("coverage_gap", 0))
    summary = f"共 {total} 条：通过 {passed}，未通过 {failed}"
    # 六态一条不落地报出来，否则运营拿总数一减对不上，会以为少了一截。
    if not_applicable:
        summary += f"，本产品不适用 {not_applicable}"
    if coverage_gap:
        summary += f"，计划外未覆盖 {coverage_gap}"
    if other:
        # 六态里的「其他」= 证据不足 / 转人工 / 未执行，都还没有确定结论
        summary += f"，没有确定结论 {other}"
    if detect_error:
        summary += f"，检测异常 {detect_error}"
    state = (
        ClosureDimensionState.SATISFIED
        if other == 0 and detect_error == 0
        else ClosureDimensionState.INCOMPLETE
    )
    return ClosureDimension(
        key="rules",
        title="规则结果",
        state=state,
        summary=summary + "。",
        metrics=metrics,
    )


def completion_gate_dimension(
    *,
    gate_passed: bool | None,
    block_reason_codes: Sequence[str],
    evaluated_at: Any = None,
    nonterminal_work_item_count: int = 0,
    open_review_count: int = 0,
) -> ClosureDimension:
    """最终完成检查：放行了没有，没放行是被哪几条挡着。"""

    blockers = [explain_block_reason(code) for code in dict.fromkeys(block_reason_codes)]
    metrics = {
        "gate_passed": gate_passed,
        "evaluated_at": evaluated_at,
        "nonterminal_work_item_count": nonterminal_work_item_count,
        "open_review_count": open_review_count,
    }
    if gate_passed is None:
        return ClosureDimension(
            key="completion_gate",
            title="最终完成检查",
            state=ClosureDimensionState.UNKNOWN,
            summary="任务还没有做过完成检查。",
            metrics=metrics,
        )
    if gate_passed:
        return ClosureDimension(
            key="completion_gate",
            title="最终完成检查",
            state=ClosureDimensionState.SATISFIED,
            summary="完成检查已放行，报告输入已冻结。",
            metrics=metrics,
            blockers=blockers,
        )
    waiting_only = bool(blockers) and all(item["waiting_for_people"] for item in blockers)
    summary = "、".join(item["label"] for item in blockers) or "完成检查没放行，但没有记录原因"
    return ClosureDimension(
        key="completion_gate",
        title="最终完成检查",
        state=(
            ClosureDimensionState.INCOMPLETE if waiting_only else ClosureDimensionState.BLOCKED
        ),
        summary=summary + "。",
        metrics=metrics,
        blockers=blockers,
    )


def overall_summary(dimensions: Sequence[ClosureDimension]) -> str:
    """一句话总结：优先说被挡住的，其次说没走完的。"""

    blocked = [item for item in dimensions if item.state is ClosureDimensionState.BLOCKED]
    if blocked:
        return "、".join(item.title for item in blocked) + "被挡住，任务不能按正常完成收口。"
    incomplete = [item for item in dimensions if item.state is ClosureDimensionState.INCOMPLETE]
    if incomplete:
        return "、".join(item.title for item in incomplete) + "还没走完。"
    unknown = [item for item in dimensions if item.state is ClosureDimensionState.UNKNOWN]
    if unknown:
        return "、".join(item.title for item in unknown) + "还没有结论。"
    return "四项都达标，任务已正常收口。"


__all__ = [
    "BLOCK_REASON_EXPLANATIONS",
    "CLOSURE_OUTCOME_PROTOCOL_VERSION",
    "DIMENSION_STATE_LABELS",
    "REVIEW_READY_BLOCK_REASONS",
    "ClosureDimension",
    "ClosureDimensionState",
    "completion_gate_dimension",
    "evidence_dimension",
    "explain_block_reason",
    "exploration_dimension",
    "overall_summary",
    "rule_dimension",
]
