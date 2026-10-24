"""受监督工作派发边界：每次 Agent 往返只执行一个结构化工作单元。"""

import asyncio
from collections.abc import Awaitable, Callable

from agent_framework import (
    FunctionInvocationContext,
    FunctionMiddleware,
    MiddlewareTermination,
)


class WorkDispatchBoundaryMiddleware(FunctionMiddleware):
    """在当前精确派发 Tool 返回后结束框架内层自由调用循环。"""

    def __init__(self) -> None:
        self._expected_tool_name: str | None = None
        self._consumed = False
        self._lock = asyncio.Lock()

    def begin_dispatch(self, expected_tool_name: str | None) -> None:
        """为新一次外层往返设置唯一可消费 Tool。"""
        self._expected_tool_name = expected_tool_name
        self._consumed = False

    async def process(
        self,
        context: FunctionInvocationContext,
        call_next: Callable[[], Awaitable[None]],
    ) -> None:
        expected = self._expected_tool_name
        if expected is None:
            await call_next()
            return
        if context.function.name != expected:
            context.result = {
                "execution_status": "blocked",
                "error_code": "WORK_DISPATCH_TOOL_MISMATCH",
                "retryable": True,
                "expected_tool_name": expected,
                "actual_tool_name": context.function.name,
                "message": "当前受监督工作单元仅允许执行指定 Tool，请返回外层重新派发。",
            }
            raise MiddlewareTermination(result=context.result)

        async with self._lock:
            if self._consumed:
                context.result = {
                    "execution_status": "blocked",
                    "error_code": "WORK_DISPATCH_ALREADY_CONSUMED",
                    "retryable": True,
                    "message": "当前受监督工作单元已执行，请返回外层重新派发。",
                }
            else:
                self._consumed = True
                await call_next()

        # 页面记忆、审计与重试中间件均已在 call_next 内完成。
        # 此处携带原 Tool 结果结束 FunctionInvocationLayer，禁止供应商
        # 忽略 tool_choice=none 后继续自由调用其他 Tool。
        raise MiddlewareTermination(result=context.result)
