"""Web V2 Runtime 的独立 Provider、Executor 与 Handler 边界。"""

from __future__ import annotations

from typing import Protocol

from services.api.app.agents.runtime.models import RunResult
from services.api.app.core.config import Settings
from services.api.app.infrastructure.queue.redis_queue import AgentRunRecord


class WebWorkflowNotConfiguredError(RuntimeError):
    """Web V2 Run 没有显式注入专用 Workflow Handler。"""


class WebRuntimeProvider(Protocol):
    """生产组合根提供完成依赖注入的 Web V2 Runtime。"""

    async def build(self, *, record: AgentRunRecord, settings: Settings) -> object: ...


class WebV2RuntimeExecutor(Protocol):
    """把 Web Runtime 编排为一次仅探索 Run。"""

    async def run(
        self,
        runtime: object,
        *,
        record: AgentRunRecord,
        settings: Settings,
        progress_recorder: object | None = None,
    ) -> RunResult: ...


class WebWorkflowHandler(Protocol):
    """Web Worker 对专用 Runtime 的唯一编排入口。"""

    async def run(
        self,
        *,
        record: AgentRunRecord,
        settings: Settings,
        progress_recorder: object | None = None,
    ) -> RunResult: ...


class RuntimeBackedWebWorkflowHandler:
    """通过显式 Provider/Executor 运行 Web，禁止借用 H5/App Handler。"""

    def __init__(
        self,
        *,
        provider: WebRuntimeProvider,
        executor: WebV2RuntimeExecutor,
        formal_session_closer: object | None = None,
    ) -> None:
        self._provider = provider
        self._executor = executor
        self._formal_session_closer = formal_session_closer

    @property
    def formal_session_closer(self) -> object | None:
        """返回与 Runtime 共用浏览器实例且支持重启恢复的正式关闭器。"""

        return self._formal_session_closer

    async def run(
        self,
        *,
        record: AgentRunRecord,
        settings: Settings,
        progress_recorder: object | None = None,
    ) -> RunResult:
        runtime = await self._provider.build(record=record, settings=settings)
        return await self._executor.run(
            runtime,
            record=record,
            settings=settings,
            progress_recorder=progress_recorder,
        )


__all__ = [
    "RuntimeBackedWebWorkflowHandler",
    "WebRuntimeProvider",
    "WebV2RuntimeExecutor",
    "WebWorkflowHandler",
    "WebWorkflowNotConfiguredError",
]
