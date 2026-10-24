"""审计日志中间件：记录工具调用前后与拦截原因。"""

import logging
from collections.abc import Awaitable, Callable

from agent_framework import FunctionInvocationContext, FunctionMiddleware, MiddlewareTermination

from services.api.app.agents.runtime.business_middlewares.base import current_task_id

logger = logging.getLogger(__name__)


class AuditLogMiddleware(FunctionMiddleware):
    """记录业务侧审计日志，不改变调用结果。"""

    async def process(
        self, context: FunctionInvocationContext, call_next: Callable[[], Awaitable[None]]
    ) -> None:
        task_id = current_task_id()
        logger.info("业务审计开始：task_id=%s tool=%s", task_id, context.function.name)
        try:
            await call_next()
        except MiddlewareTermination:
            logger.warning(
                "业务审计终止：task_id=%s tool=%s result=%s",
                task_id,
                context.function.name,
                context.result,
            )
            raise
        except Exception:
            logger.exception("业务审计异常：task_id=%s tool=%s", task_id, context.function.name)
            raise
        else:
            logger.info("业务审计完成：task_id=%s tool=%s", task_id, context.function.name)
