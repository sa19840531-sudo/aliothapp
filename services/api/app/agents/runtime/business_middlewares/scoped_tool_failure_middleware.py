"""在工具内部重试耗尽后，把页面/规则局部异常转换为结构化结果。"""

from collections.abc import Awaitable, Callable

from agent_framework import FunctionInvocationContext, FunctionMiddleware, MiddlewareTermination

from services.api.app.agents.runtime.business_middlewares.base import extract_arguments
from services.api.app.agents.runtime.tool_failure_policy import classify_tool_exception


class ScopedToolFailureMiddleware(FunctionMiddleware):
    """隔离已知质检工具故障；全局护栏与未知异常保持原有传播语义。"""

    async def process(
        self,
        context: FunctionInvocationContext,
        call_next: Callable[[], Awaitable[None]],
    ) -> None:
        try:
            await call_next()
        except MiddlewareTermination:
            raise
        except Exception as exc:  # noqa: BLE001 - 只转换明确登记的质检工具异常
            failure = classify_tool_exception(context.function.name, exc)
            if failure is None:
                raise
            arguments = extract_arguments(context.arguments)
            page_capture_id = arguments.get("page_capture_id")
            context.result = failure.to_result(
                page_capture_id=(
                    str(page_capture_id).strip() if page_capture_id is not None else None
                )
            )
