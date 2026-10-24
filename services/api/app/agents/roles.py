"""Alioth V2 允许实例化的 Agent 角色清单。"""

from enum import StrEnum


class V2AgentRole(StrEnum):
    """HTML 架构定义的两个真实 Agent。"""

    EXPLORER = "explorer"
    RULE_JUDGE = "rule_judge"
