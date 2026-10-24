"""规则集版本锁定：同一次 Run 内检测规则集版本漂移并终止。"""

import logging
from collections.abc import Awaitable, Callable

from agent_framework import FunctionInvocationContext, FunctionMiddleware, MiddlewareTermination

from services.api.app.agents.runtime.business_middlewares.base import (
    TaskRuntimeStateProvider,
    current_task_id,
)

logger = logging.getLogger(__name__)


class RulesetVersionLockMiddleware(FunctionMiddleware):
    """锁定首次读取到的 ruleset_version，后续漂移即硬终止。"""

    def __init__(self, provider: TaskRuntimeStateProvider) -> None:
        self._provider = provider
        self._locked_task_id: str | None = None
        self._locked_ruleset_version: int | None = None

    async def process(
        self, context: FunctionInvocationContext, call_next: Callable[[], Awaitable[None]]
    ) -> None:
        task_id = current_task_id()
        if not task_id:
            await call_next()
            return

        snapshot = await self._provider.get_task_snapshot(task_id)
        if snapshot is None:
            logger.warning("规则集版本锁定跳过：task_id=%s 不存在。", task_id)
            await call_next()
            return

        if self._locked_ruleset_version is None:
            self._locked_task_id = task_id
            self._locked_ruleset_version = snapshot.ruleset_version
            await call_next()
            return

        if (
            self._locked_task_id != task_id
            or snapshot.ruleset_version != self._locked_ruleset_version
        ):
            message = (
                "检测到任务规则集版本发生变化，已终止本次 Agent 运行。"
                f"task_id={task_id} locked_version={self._locked_ruleset_version} "
                f"current_version={snapshot.ruleset_version}"
            )
            context.result = message
            raise MiddlewareTermination(result=message)

        await call_next()
