"""重复提交去重：同一次 Run 内对重复写操作进行软阻断。"""

from collections.abc import Awaitable, Callable, Iterable

from agent_framework import FunctionInvocationContext, FunctionMiddleware

from services.api.app.agents.runtime.business_middlewares.base import default_mutating_tools
from services.api.app.agents.runtime.working_memory import normalize_arguments
from services.api.app.agents.tools.registry import ToolRegistry, default_registry

_DYNAMIC_REPEATABLE_TOOLS = frozenset({"page_capture_tool"})


class DuplicateSubmissionDedupeMiddleware(FunctionMiddleware):
    """对同工具同参数的重复变更调用进行去重。"""

    def __init__(
        self,
        *,
        mutating_tools: Iterable[str] | None = None,
        registry: ToolRegistry | None = None,
    ) -> None:
        target_registry = registry if registry is not None else default_registry
        if mutating_tools is not None:
            self._mutating_tools = set(mutating_tools)
        else:
            self._mutating_tools = default_mutating_tools(target_registry)
        self._seen_signatures: set[str] = set()

    async def process(
        self, context: FunctionInvocationContext, call_next: Callable[[], Awaitable[None]]
    ) -> None:
        if (
            context.function.name not in self._mutating_tools
            or context.function.name in _DYNAMIC_REPEATABLE_TOOLS
        ):
            await call_next()
            return

        signature = f"{context.function.name}:{normalize_arguments(context.arguments)}"
        if signature in self._seen_signatures:
            context.result = {
                "execution_status": "blocked",
                "error_code": "DUPLICATE_SUBMISSION",
                "retryable": False,
                "message": "检测到同一工具与同一参数的重复提交，本次调用已跳过。",
            }
            return

        await call_next()
        if not _is_blocked_result(context.result):
            self._seen_signatures.add(signature)


def _is_blocked_result(result: object) -> bool:
    return isinstance(result, dict) and result.get("execution_status") == "blocked"
