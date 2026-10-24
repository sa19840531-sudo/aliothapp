"""V1 Main Agent 兼容派发器的输出模型。"""

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class WorkDispatch:
    """一次外层续跑必须执行的最小工作单元。"""

    message: str
    tool_choice: dict[str, Any]
    kind: str
    exploration_action_id: str | None = None
    tool_name: str | None = None
    arguments: dict[str, Any] | None = None
    session_dependency: str | None = None
