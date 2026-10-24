"""Tool 权限校验：按运行上下文的允许清单软阻断越权的 Tool 调用。

采用"软阻断"语义（不调用 ``call_next``，仅设置 ``context.result``），而非
``raise MiddlewareTermination`` 的硬终止：一次越权调用不代表整个任务都该终止，
应允许模型据此改用其他被允许的 Tool 继续尝试。硬终止语义（步数上限/卡死检测）
由 ``loop_guard.LoopGuardMiddleware`` 负责，两者职责不重叠，可在同一中间件链条
里组合使用（详见仓库记忆中"软阻断 vs 硬终止"的实测结论）。

必须放在 ``ActionTraceMiddleware`` 内层，这样越权尝试也会被外层记录进审计留痕；
与 ``LoopGuardMiddleware`` 之间没有强制的先后顺序要求。
"""

from collections.abc import Awaitable, Callable, Iterable

from agent_framework import FunctionInvocationContext, FunctionMiddleware

from services.api.app.agents.tools.registry import PermissionLevel, ToolRegistry, default_registry

_PERMISSION_DENIED_MESSAGE_TEMPLATE = (
    "权限不足：Tool「{name}」需要权限级别 {required}，当前运行上下文仅允许 {allowed}。"
)


class PermissionEnforcementMiddleware(FunctionMiddleware):
    """按运行上下文允许的权限级别集合，校验每一次 Tool 调用。"""

    def __init__(
        self,
        *,
        allowed_permission_levels: Iterable[PermissionLevel] = (PermissionLevel.SAFE,),
        registry: ToolRegistry | None = None,
    ) -> None:
        self._allowed = set(allowed_permission_levels)
        self._registry = registry if registry is not None else default_registry

    async def process(
        self, context: FunctionInvocationContext, call_next: Callable[[], Awaitable[None]]
    ) -> None:
        metadata = self._registry.get(context.function.name)
        if metadata is None or metadata.permission_level in self._allowed:
            # 未在注册表登记的工具理论上不应出现；防御性放行，避免误伤框架自身可能
            # 挂载的内置工具，同时不掩盖"确实已注册但权限不足"的场景。
            await call_next()
            return

        context.result = _PERMISSION_DENIED_MESSAGE_TEMPLATE.format(
            name=context.function.name,
            required=metadata.permission_level.value,
            allowed=", ".join(sorted(level.value for level in self._allowed)),
        )
