"""V2 Explorer Agent 的异步输入输出端口。"""

from __future__ import annotations

from typing import Protocol

from services.api.app.domain.exploration import ExplorationTerminal, ExplorerWork
from services.api.app.domain.interaction import ActionIntent


class ExplorerAgent(Protocol):
    """读取探索工作并决定下一条 Action Intent 或显式终态。"""

    async def decide_next(
        self,
        work: ExplorerWork,
    ) -> ActionIntent | ExplorationTerminal: ...
