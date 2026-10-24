"""Agent Run 运行上下文：为 Tool 提供当前任务与运行 ID。"""

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass


@dataclass(frozen=True)
class AgentRunContext:
    """一次 Agent Run 注入给 Tool 的系统上下文。"""

    task_id: str | None
    agent_run_id: str
    task_config: dict[str, object] | None = None
    # Formal Inspection 写回围栏：有 Work Item 时必须携带有效租约。
    work_item_id: str | None = None
    attempt_id: str | None = None
    lease_token: str | None = None
    # 仅 H5 运行及其质检消费者启用物理调用准入，App/Web 保持原行为。
    h5_llm_budget: bool = False


_current_context: ContextVar[AgentRunContext | None] = ContextVar(
    "alioth_agent_run_context", default=None
)


@contextmanager
def bind_agent_run_context(context: AgentRunContext) -> Iterator[None]:
    """在当前异步任务上下文中绑定 Agent Run 上下文。"""
    token = _current_context.set(context)
    try:
        yield
    finally:
        _current_context.reset(token)


def get_agent_run_context() -> AgentRunContext | None:
    """读取当前 Agent Run 上下文；未绑定时返回 None。"""
    return _current_context.get()


def require_agent_run_context() -> AgentRunContext:
    """读取当前 Agent Run 上下文；缺少业务任务 ID 时抛出明确错误。"""
    context = get_agent_run_context()
    if context is None or not context.task_id:
        raise RuntimeError("缺少 Agent Run 上下文：业务 Tool 必须在绑定 task_id 后调用")
    return context
