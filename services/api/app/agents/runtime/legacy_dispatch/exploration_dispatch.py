"""把 V1 Frontier 决策转换为当前页面 Tool 派发。"""

from __future__ import annotations

from typing import Any

from services.api.app.agents.runtime.legacy_dispatch.contracts import (
    LegacyExplorationDecision,
)
from services.api.app.agents.runtime.legacy_dispatch.models import WorkDispatch
from services.api.app.agents.runtime.legacy_dispatch.support import required_tool, text


def build_legacy_exploration_dispatch(
    decision: LegacyExplorationDecision,
) -> WorkDispatch:
    """构造只允许执行一个 V1 Frontier 已选动作的派发指令。"""
    action = decision.to_action_dict()
    action_id = decision.exploration_action_id
    call_hint = _page_action_call_hint(action)
    return WorkDispatch(
        kind="page_exploration",
        exploration_action_id=action_id,
        tool_name="page_capture_tool",
        arguments=_page_action_arguments(action),
        tool_choice=required_tool("page_capture_tool"),
        message=(
            "结构化 Frontier 已选中唯一下一动作；本轮必须调用 page_capture_tool，"
            "中间件会强制使用记录中的同会话参数。必须原样传入 "
            f"exploration_action_id={action_id!r}。{call_hint}动作：{action!r}。"
            "成功产生新 page_capture_id 后必须继续页面识别与逐页规则质检，"
            "不能把动作已生成或已选中当作探索完成。"
        ),
    )


def _page_action_call_hint(action: dict[str, Any]) -> str:
    """给不同兼容模型提供最小、合法的页面 Tool 参数示例。"""
    action_name = text(action.get("action"))
    target = text(action.get("target"))
    depth = action.get("navigation_depth")
    if action_name == "goto":
        return f"调用参数必须包含 mode='goto', url={target!r}, navigation_depth={depth!r}。"
    if action_name == "back":
        return (
            "调用参数必须包含 mode='single_trigger', trigger_action='back', "
            f"navigation_depth={depth!r}。"
        )
    if action_name == "click":
        return (
            f"调用参数必须包含 mode='single_trigger', trigger_action='click', selector={target!r}。"
        )
    if action_name == "hover":
        return (
            f"调用参数必须包含 mode='single_trigger', trigger_action='hover', selector={target!r}。"
        )
    if action_name == "observe":
        return "调用参数必须包含 mode='observe'。"
    if action_name == "scroll":
        return "调用参数必须包含 mode='single_trigger', trigger_action='scroll'。"
    return ""


def _page_action_arguments(action: dict[str, Any]) -> dict[str, Any]:
    """把受信任 V1 Frontier 转成直接执行器可用的精确 Tool 参数。"""
    action_name = text(action.get("action"))
    arguments: dict[str, Any] = {
        "exploration_action_id": str(action["exploration_action_id"]),
    }
    depth = action.get("navigation_depth")
    if isinstance(depth, int):
        arguments["navigation_depth"] = depth
    supersedes = text(action.get("supersedes_page_capture_id"))
    if supersedes is not None:
        arguments["supersedes_page_capture_id"] = supersedes
    capture_role = text(action.get("capture_role"))
    if capture_role is not None:
        arguments["capture_role"] = capture_role
    if action.get("clear_obstructions") is True:
        arguments["clear_obstructions"] = True
    if (
        str(action.get("purpose") or "").startswith("rule_evidence:")
        and action.get("resumes_page_exploration") is not True
    ):
        arguments["inherit_parent_semantics"] = True
    if action_name == "goto":
        arguments.update({"mode": "goto", "url": text(action.get("target"))})
    elif action_name == "click":
        arguments.update(
            {
                "mode": "single_trigger",
                "trigger_action": "click",
                "selector": text(action.get("target")),
            }
        )
    elif action_name == "hover":
        arguments.update(
            {
                "mode": "single_trigger",
                "trigger_action": "hover",
                "selector": text(action.get("target")),
            }
        )
    elif action_name == "back":
        arguments.update({"mode": "single_trigger", "trigger_action": "back"})
    elif action_name == "observe":
        arguments["mode"] = "observe"
    elif action_name == "scroll":
        arguments.update({"mode": "single_trigger", "trigger_action": "scroll"})
    return arguments
