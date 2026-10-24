"""H5 纯页面探索 Agent 的输入输出合同。"""

from __future__ import annotations

from typing import Protocol

from services.api.app.domain.exploration import ExplorationTerminal, ExplorationWork
from services.api.app.domain.interaction import ActionIntent


class H5ExplorerAgent(Protocol):
    """只消费页面探索工作且只返回探索决策的 Agent 端口。"""

    async def decide_next(
        self,
        work: ExplorationWork,
    ) -> ActionIntent | ExplorationTerminal: ...
