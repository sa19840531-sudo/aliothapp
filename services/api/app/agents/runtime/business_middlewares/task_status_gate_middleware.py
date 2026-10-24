"""任务状态门禁：仅允许在白名单状态下执行变更类工具。"""

from collections.abc import Awaitable, Callable, Iterable

from agent_framework import FunctionInvocationContext, FunctionMiddleware

from services.api.app.agents.runtime.business_middlewares.base import (
    TaskRuntimeStateProvider,
    current_task_id,
    default_mutating_tools,
)
from services.api.app.agents.tools.registry import ToolRegistry, default_registry


class TaskStatusGateMiddleware(FunctionMiddleware):
    """按任务状态白名单放行变更类工具。"""

    def __init__(
        self,
        provider: TaskRuntimeStateProvider,
        *,
        allowed_statuses: Iterable[str] = ("running",),
        mutating_tools: Iterable[str] | None = None,
        registry: ToolRegistry | None = None,
    ) -> None:
        self._provider = provider
        self._allowed_statuses = {item.strip().lower() for item in allowed_statuses if item}
        target_registry = registry if registry is not None else default_registry
        if mutating_tools is not None:
            self._mutating_tools = set(mutating_tools)
        else:
            self._mutating_tools = default_mutating_tools(target_registry)

    async def process(
        self, context: FunctionInvocationContext, call_next: Callable[[], Awaitable[None]]
    ) -> None:
        if context.function.name not in self._mutating_tools:
            await call_next()
            return

        task_id = current_task_id()
        if not task_id:
            await call_next()
            return

        snapshot = await self._provider.get_task_snapshot(task_id)
        if snapshot is None:
            context.result = f"任务状态门禁阻断：task_id={task_id} 不存在。"
            return

        current_status = snapshot.status.lower()
        if current_status not in self._allowed_statuses:
            context.result = (
                "任务状态门禁阻断："
                f"tool={context.function.name} task_status={snapshot.status} "
                f"allowed={sorted(self._allowed_statuses)}"
            )
            return

        await call_next()
