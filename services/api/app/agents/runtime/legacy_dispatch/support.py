"""V1 兼容派发规划器共用的格式化函数。"""

from typing import Any


def required_tool(tool_name: str) -> dict[str, Any]:
    """返回 Agent Framework 的单工具强制选择参数。"""
    return {"mode": "required", "required_function_name": tool_name}


def text(value: Any) -> str | None:
    normalized = str(value or "").strip()
    return normalized or None


def integer(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0
