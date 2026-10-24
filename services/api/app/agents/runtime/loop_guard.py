"""循环护栏：硬性步数上限 + 卡死/无进展检测。

单次 Agent Run 的最大迭代轮次通过 ``client.function_invocation_configuration`` 使用
框架内置防护；工具调用步数不使用框架 ``max_function_calls``，因为该配置会在并行批次
全部完成后才统计，无法形成硬上限。本模块在每个 Tool 真正执行前同步预占预算，不依赖
模型或供应商遵守 ``tool_choice="none"``，是基础步数预算、动态页面预算与绝对硬上限的
唯一权威执行入口。

必须作为"内层"FunctionMiddleware 使用，即在 ``agent.run(middleware=[...])`` 列表中
排在 ``ActionTraceMiddleware`` 之后，保证被本护栏拦截的调用尝试仍能被外层的
``ActionTraceMiddleware`` 记录进审计留痕。
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from agent_framework import FunctionInvocationContext, FunctionMiddleware, MiddlewareTermination

from services.api.app.agents.runtime.working_memory import WorkingMemory, normalize_arguments

_MAX_STEPS_MESSAGE = "已达到本次任务允许的最大工具调用步数，Agent 循环已自动终止。"
_STUCK_DETECTED_MESSAGE = (
    "检测到连续多次重复调用同一工具且参数不变，判定为无进展，Agent 循环已自动终止。"
)
_PAGE_RULE_STUCK_ISOLATED_MESSAGE = (
    "检测到页面规则处理重复且无进展；当前调用已终止，但未终止全局页面探索，"
    "请隔离本页失败规则并继续其他规则或页面。"
)
_PAGE_FLOW_STUCK_ISOLATED_MESSAGE = (
    "检测到当前页面采集或识别重复且无进展；本次调用已终止，"
    "但未终止全局页面探索，请使用当前 WorkingMemory 中的 Frontier 动作继续。"
)
_ALREADY_TERMINATED_MESSAGE = "Agent 循环已经终止，本次工具调用未执行。"


@dataclass(frozen=True)
class LoopGuardConfig:
    """循环护栏的可配置阈值。"""

    max_steps: int
    """基础工具调用步数；关闭动态扩展时即为硬上限。"""

    stuck_window: int
    """判定"卡死/无进展"所需的连续相同调用（工具名+参数完全一致）次数。"""

    adaptive_page_budget: bool = False
    """是否根据运行时动态发现的页面工作扩展预算。"""

    max_steps_hard_cap: int = 600
    """动态扩展也不得突破的系统硬上限。"""

    page_budget_base_steps: int = 12
    """动态页面预算的基础步数。"""

    page_budget_steps_per_page: int = 16
    """每个已建立规则质检上下文的逻辑页面追加的步数。"""


class LoopGuardMiddleware(FunctionMiddleware):
    """在真正执行工具调用前检查预算与无进展信号，命中即终止循环。"""

    def __init__(self, working_memory: WorkingMemory, config: LoopGuardConfig) -> None:
        self._working_memory = working_memory
        self._config = config

    async def process(
        self, context: FunctionInvocationContext, call_next: Callable[[], Awaitable[None]]
    ) -> None:
        arguments_repr = normalize_arguments(context.arguments)
        reservation = self._working_memory.reserve_call(
            context.function.name,
            arguments_repr,
            max_steps=self._effective_max_steps(),
            stuck_window=self._config.stuck_window,
        )
        if not reservation.accepted:
            context.result = _termination_message(reservation.termination_reason)
            raise MiddlewareTermination(result=context.result)

        await call_next()

    def _effective_max_steps(self) -> int:
        """页面总量未知时随动态发现工作扩容，同时保留不可突破的硬上限。"""
        configured_steps = min(
            self._config.max_steps,
            self._config.max_steps_hard_cap,
        )
        if not self._config.adaptive_page_budget:
            return configured_steps
        discovered_work = self._working_memory.discovered_page_work_count()
        if discovered_work == 0:
            return configured_steps
        adaptive_steps = (
            self._config.page_budget_base_steps
            + discovered_work * self._config.page_budget_steps_per_page
        )
        return min(
            self._config.max_steps_hard_cap,
            max(configured_steps, adaptive_steps),
        )


def _termination_message(reason: str | None) -> str:
    """把终止锁存原因转换为返回给模型的稳定文本。"""
    if reason == "max_steps_reached":
        return _MAX_STEPS_MESSAGE
    if reason == "stuck_detected":
        return _STUCK_DETECTED_MESSAGE
    if reason == "page_rule_stuck_isolated":
        return _PAGE_RULE_STUCK_ISOLATED_MESSAGE
    if reason == "page_flow_stuck_isolated":
        return _PAGE_FLOW_STUCK_ISOLATED_MESSAGE
    return _ALREADY_TERMINATED_MESSAGE
