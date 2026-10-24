"""V1 Main Agent 的规则 Judge 与可信写回派发规划。"""

from __future__ import annotations

from typing import Any

from services.api.app.agents.runtime.legacy_dispatch.models import WorkDispatch
from services.api.app.agents.runtime.legacy_dispatch.support import required_tool, text
from services.api.app.agents.runtime.working_memory import WorkingMemory
from services.api.app.application.rule_batching import (
    MAX_RULE_JUDGE_BATCH_SIZE,
    RuleExecutionBatchKey,
    build_rule_execution_batch_key,
)


class RuleDispatchPlanner:
    """从页面快照规划 Judge 或结果写回，不决定探索路径。"""

    def prepare(
        self,
        *,
        current_page: dict[str, Any],
        page_capture_id: str,
    ) -> WorkDispatch | None:
        candidates = current_page.get("candidate_rules")
        result = current_page.get("result")
        completed_rules = result.get("rules") if isinstance(result, dict) else None
        if not isinstance(candidates, list) or not isinstance(completed_rules, list):
            return None

        completed_keys = {
            key
            for item in completed_rules
            if isinstance(item, dict)
            if (key := _rule_key(item)) is not None
        }
        interaction_evidence_ready = current_page.get("interaction_evidence_ready") is True
        interaction_evidence_unavailable = (
            current_page.get("interaction_evidence_unavailable") is True
        )

        def evidence_ready(item: dict[str, Any]) -> bool:
            return (
                interaction_evidence_ready
                or interaction_evidence_unavailable
                or item.get("requires_interaction_evidence") is not True
            )

        pending_judgements = [
            item
            for item in current_page.get("pending_rule_judgements", [])
            if isinstance(item, dict)
            and _rule_key(item) not in completed_keys
            and _rule_key(item) is not None
        ]
        if pending_judgements:
            return _rule_judgement_write_dispatch(
                page_capture_id=page_capture_id,
                pending_judgements=pending_judgements,
            )

        unresolved_judge_rules = [
            item
            for item in candidates
            if isinstance(item, dict)
            and _rule_key(item) not in completed_keys
            and text(item.get("rule_code")) is not None
            and item.get("judge_tool_name") == "rule_judge_tool"
            and evidence_ready(item)
        ]
        if not unresolved_judge_rules:
            unresolved_candidates = [
                item
                for item in candidates
                if isinstance(item, dict)
                and _rule_key(item) not in completed_keys
                and _rule_key(item) is not None
                and evidence_ready(item)
            ]
            if not unresolved_candidates:
                return None
            first_batch_key = _rule_dispatch_batch_key(unresolved_candidates[0])
            unresolved_rules = [
                item
                for item in unresolved_candidates
                if _rule_dispatch_batch_key(item) == first_batch_key
            ][:MAX_RULE_JUDGE_BATCH_SIZE]
            executions = []
            for item in unresolved_rules:
                identity = {
                    key: item[key] for key in ("rule_id", "rule_code") if item.get(key) is not None
                }
                blocked_reason = (
                    text(item.get("route_blocked_reason"))
                    or "候选规则未提供可执行的统一 Judge 路由"
                )
                executions.append(
                    {
                        **identity,
                        "execution_status": "error",
                        "result_summary": "规则执行路由未就绪，已停止自动判定。",
                        "problem_desc": blocked_reason,
                        "error_code": "RULE_JUDGE_ROUTE_NOT_READY",
                        "error_message": blocked_reason,
                    }
                )
            return WorkDispatch(
                kind="rule_write",
                tool_name="record_rule_executions_batch_tool",
                arguments={
                    "page_capture_id": page_capture_id,
                    "executions": executions,
                },
                tool_choice=required_tool("record_rule_executions_batch_tool"),
                message=(
                    f"页面 {page_capture_id} 有 {len(executions)} 条规则缺少可执行 Judge 路由，"
                    "监督器将直接逐条写入 RULE_JUDGE_ROUTE_NOT_READY；不得由模型猜测结论。"
                ),
            )

        unresolved_judge_rules.sort(
            key=lambda item: 0 if text(item.get("session_dependency")) == "active_session" else 1
        )
        first_rule = unresolved_judge_rules[0]
        batch_key = _rule_dispatch_batch_key(first_rule)
        routed_rules = [
            item for item in unresolved_judge_rules if _rule_dispatch_batch_key(item) == batch_key
        ][:MAX_RULE_JUDGE_BATCH_SIZE]
        tool_name = "rule_judge_batch_tool" if len(routed_rules) > 1 else "rule_judge_tool"
        rule_codes = [str(item["rule_code"]) for item in routed_rules if item.get("rule_code")]
        raw_capture_roles = current_page.get("evidence_capture_roles")
        capture_roles = (
            {
                str(role): str(capture_id)
                for role, capture_id in raw_capture_roles.items()
                if text(role) is not None and text(capture_id) is not None
            }
            if isinstance(raw_capture_roles, dict)
            else {}
        )
        shared_arguments: dict[str, Any] = {}
        if len(capture_roles) > 1:
            shared_arguments["evidence_capture_roles"] = capture_roles
        if tool_name == "rule_judge_tool":
            call_hint = (
                f"调用参数必须精确包含 rule_code={rule_codes[0]!r}, "
                f"page_capture_id={page_capture_id!r}。"
            )
        else:
            rules_payload = [{"rule_code": rule_code} for rule_code in rule_codes]
            call_hint = (
                f"调用参数必须精确包含 page_capture_id={page_capture_id!r}, "
                f"rules={rules_payload!r}。"
            )
        return WorkDispatch(
            kind="rule_judge",
            session_dependency=(text(first_rule.get("session_dependency")) or "snapshot"),
            tool_name=tool_name,
            arguments=(
                {
                    "rule_code": rule_codes[0],
                    "page_capture_id": page_capture_id,
                    **shared_arguments,
                }
                if tool_name == "rule_judge_tool"
                else {
                    "page_capture_id": page_capture_id,
                    "rules": [{"rule_code": rule_code} for rule_code in rule_codes],
                    **shared_arguments,
                }
            ),
            tool_choice=required_tool(tool_name),
            message=(
                f"页面 {page_capture_id} 尚有同一执行批次规则 {rule_codes!r} "
                f"未经统一 Judge 判定。本轮必须调用 {tool_name}；{call_hint}"
                "证据与结构化事实由后端从可信页面采集自动组装，不得由模型复制或伪造；"
                "不得把其他执行批次的规则塞入本批次。"
                "判定成功后由下一轮受监督派发强制写回，不得执行页面采集或业务导航。"
            ),
        )


def prepare_page_rule_dispatch(
    working_memory: WorkingMemory,
    *,
    page_capture_id: str,
    planner: RuleDispatchPlanner | None = None,
) -> WorkDispatch | None:
    """为已移入后台的静态页面继续生成下一批规则判定。"""
    snapshot = working_memory.model_snapshot()
    pages = [snapshot.get("current_page"), *snapshot.get("pending_pages", [])]
    selected_planner = planner if planner is not None else RuleDispatchPlanner()
    for page in pages:
        if not isinstance(page, dict):
            continue
        if text(page.get("page_capture_id")) != page_capture_id:
            continue
        return selected_planner.prepare(
            current_page=page,
            page_capture_id=page_capture_id,
        )
    return None


def _rule_judgement_write_dispatch(
    *,
    page_capture_id: str,
    pending_judgements: list[dict[str, Any]],
) -> WorkDispatch:
    """判定完成后优先派发写回，避免强制判定工具在同一页循环。"""
    if len(pending_judgements) == 1:
        judgement = pending_judgements[0]
        identity = {
            key: judgement[key]
            for key in ("rule_id", "rule_code", "execution_status")
            if judgement.get(key) is not None
        }
        return WorkDispatch(
            kind="rule_write",
            tool_name="record_rule_execution_tool",
            arguments={"page_capture_id": page_capture_id, **identity},
            tool_choice=required_tool("record_rule_execution_tool"),
            message=(
                f"页面 {page_capture_id} 的规则已经完成统一判定但尚未写回。"
                "本轮必须调用 record_rule_execution_tool，至少原样传入 "
                f"page_capture_id={page_capture_id!r} 与判定身份 {identity!r}；"
                "中间件会从同一次可信判定补齐证据与判定字段。不得再次调用判定、"
                "页面采集或业务导航，不得输出结束文本。"
            ),
        )

    executions = [
        {
            key: judgement[key]
            for key in ("rule_id", "rule_code", "execution_status")
            if judgement.get(key) is not None
        }
        for judgement in pending_judgements
    ]
    return WorkDispatch(
        kind="rule_write",
        tool_name="record_rule_executions_batch_tool",
        arguments={
            "page_capture_id": page_capture_id,
            "executions": executions,
        },
        tool_choice=required_tool("record_rule_executions_batch_tool"),
        message=(
            f"页面 {page_capture_id} 有 {len(executions)} 条规则已判定未写回。"
            "本轮必须调用 record_rule_executions_batch_tool，原样传入 "
            f"page_capture_id={page_capture_id!r}, executions={executions!r}；"
            "中间件会补齐每条可信判定字段。不得再次判定、采集或导航，"
            "不得输出结束文本。"
        ),
    )


def _rule_key(item: dict[str, Any]) -> str | None:
    """返回候选/结果规则的稳定键。"""
    return text(item.get("rule_id")) or text(item.get("rule_code"))


def _rule_dispatch_batch_key(item: dict[str, Any]) -> RuleExecutionBatchKey:
    """使用生产计划同源的规则形成小批次，避免持久化协议漂移。"""
    return build_rule_execution_batch_key(
        strategy=text(item.get("strategy")),
        category=text(item.get("category")),
        session_dependency=text(item.get("session_dependency")),
    )
