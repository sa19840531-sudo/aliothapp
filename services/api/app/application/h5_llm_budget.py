"""H5 物理请求准入与费用流水分离，未发送请求不计作模型用量。"""

from services.api.app.agents.runtime.run_context import get_agent_run_context


class H5LLMBudgetExhausted(RuntimeError):
    error_code = "h5_llm_call_budget_exhausted"
    retryable = False

    def __init__(self, message: str = "H5 冻结模型调用预算已耗尽") -> None:
        super().__init__(message)


async def reserve_current_h5_llm_call() -> None:
    context = get_agent_run_context()
    if context is None or not context.h5_llm_budget:
        return
    if not context.task_id:
        raise RuntimeError("H5 模型准入缺少任务身份")
    from services.api.app.infrastructure.db.h5_llm_budget_repository import (
        PostgresH5LLMBudgetRepository,
    )

    await PostgresH5LLMBudgetRepository().reserve(
        task_id=context.task_id,
        consumer_agent_run_id=context.agent_run_id,
    )


def h5_llm_budget_error_code(error: BaseException) -> str | None:
    """保留 SDK 包装下的预算拒绝，不把它当网络抖动反复排队。"""
    visited = set()
    pending = [error]
    while pending:
        current = pending.pop()
        if id(current) in visited:
            continue
        visited.add(id(current))
        if isinstance(current, H5LLMBudgetExhausted):
            return current.error_code
        pending.extend(
            nested for nested in (
                current.__cause__, current.__context__, *current.args,
                *getattr(current, "exceptions", ()),
            ) if isinstance(nested, BaseException)
        )
    return None
