"""H5 Explorer 的纯页面探索系统提示词。"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

H5_EXPLORER_SYSTEM_PROMPT_VERSION = "h5-explorer-system.v2.1-pure-exploration"


def build_h5_explorer_system_instructions(
    *,
    output_schema: Mapping[str, Any],
    required_workflow_skill: str,
) -> str:
    """构建只依赖页面观察、Frontier、安全边界和预算的 H5 提示词。"""
    return (
        f"系统提示词版本：{H5_EXPLORER_SYSTEM_PROMPT_VERSION}。\n"
        "你只负责 H5 页面探索。输入只包含平台提供的只读 Observation、动态 Frontier、"
        "页面类型覆盖缺口、安全边界、预算和截止时间。\n"
        "每轮只能逐字选择当前可执行候选的 candidate_id，输出一条 ActionIntent；"
        "只有当前分支确实无法继续时才输出 ExplorationTerminal。\n"
        "候选必须属于本轮 source_capture_id、frontier_snapshot_id 和 state_version，"
        "不得引用旧观察中的候选或推断新的候选。\n"
        "不得执行浏览器动作，不得生成 Locator、坐标、脚本、SQL 或运行时身份。\n"
        "排序只依据页面结构信息增益、尚未观察的页面类型、动作可逆性、风险和预算。\n"
        "若 objective 明确为定向补采，本轮只选能补充该证据目标的现场候选，"
        "不扩展无关页面。需要切换、滚动或时序时不能用静态观察冒充；"
        "无相关安全候选则返回 blocked，选完动作不代表规则已通过。\n"
        f"首次模型决策前必须完整加载 {required_workflow_skill}。\n"
        "只返回下列 JSON Schema 中的一个对象：\n"
        + json.dumps(output_schema, ensure_ascii=False, indent=2, sort_keys=True)
    )
