"""取消检查：在每次工具调用尝试前检查是否已收到外部取消请求，命中即硬终止循环。

设计动机：Worker 进程消费 Redis 队列执行 Agent Run 时，需要能响应"取消这次 Run"的
外部请求（如任务被用户手动终止）；由于 Agent Loop 是多轮工具调用的长流程，取消信号
需要在"步与步之间"的自然检查点生效，而不是粗暴 kill 掉整个 Worker 进程/任务协程。

必须作为"内层"FunctionMiddleware 使用（排在 ``ActionTraceMiddleware``/
``LoopGuardMiddleware`` 之后），保证被取消拦截的调用尝试仍会被外层记录进审计留痕，
判定与终止语义均与 ``loop_guard.LoopGuardMiddleware`` 一致（硬终止，而非软阻断）。
"""

from collections.abc import Awaitable, Callable

from agent_framework import FunctionInvocationContext, FunctionMiddleware, MiddlewareTermination

from services.api.app.agents.runtime.working_memory import WorkingMemory

_CANCELED_MESSAGE = "收到外部取消请求，Agent 循环已终止。"

IsCancelledCallback = Callable[[], Awaitable[bool]]


class CancellationCheckMiddleware(FunctionMiddleware):
    """在真正执行工具调用前检查是否已收到取消请求，命中即终止循环。"""

    def __init__(self, working_memory: WorkingMemory, is_cancelled: IsCancelledCallback) -> None:
        self._working_memory = working_memory
        self._is_cancelled = is_cancelled

    async def process(
        self, context: FunctionInvocationContext, call_next: Callable[[], Awaitable[None]]
    ) -> None:
        if await self._is_cancelled():
            self._working_memory.termination_reason = "canceled"
            context.result = _CANCELED_MESSAGE
            raise MiddlewareTermination(result=context.result)

        await call_next()
