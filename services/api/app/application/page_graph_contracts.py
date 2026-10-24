"""页面图用例的仓储端口，应用层不依赖 PostgreSQL 实现。"""

from typing import Any, Protocol


class PageGraphUnavailableError(RuntimeError):
    """页面图迁移未就绪或图投影依赖不可用。"""


class PageGraphRepository(Protocol):
    """读取同运行图快照或按权威记录重建，不执行浏览器与设备动作。"""

    async def get_graph(
        self, *, task_id: str, agent_run_id: str | None, finalization_epoch: int | None
    ) -> dict[str, Any]: ...

    async def rebuild_run(
        self, *, task_id: str, agent_run_id: str, finalization_epoch: int | None = None
    ) -> Any: ...
