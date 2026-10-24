"""App 探索阶段的协作式取消门禁。"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from contextlib import suppress

from services.api.app.application.app_workflow import AppWorkflowCancellation


class AppExplorationCanceledError(RuntimeError):
    """取消到达后停止新的 App 探索副作用。"""

    error_code = "WORKFLOW_CANCELED"
    retryable = False


async def raise_if_app_exploration_canceled(
    cancellation: AppWorkflowCancellation | None,
    *,
    task_id: str,
    agent_run_id: str,
) -> None:
    """在副作用边界前读取统一 Run 取消事实。"""

    if cancellation is not None and await cancellation.is_cancel_requested(
        task_id=task_id,
        agent_run_id=agent_run_id,
    ):
        raise AppExplorationCanceledError("App 探索已取消")


async def run_app_exploration_operation[ResultT](
    operation: Awaitable[ResultT],
    *,
    cancellation: AppWorkflowCancellation | None,
    task_id: str,
    agent_run_id: str,
    poll_interval_seconds: float,
) -> ResultT:
    """在操作执行期间轮询取消，命中后取消本地协程并停止后续副作用。"""

    if cancellation is None:
        return await operation
    try:
        await raise_if_app_exploration_canceled(
            cancellation,
            task_id=task_id,
            agent_run_id=agent_run_id,
        )
    except BaseException:
        close = getattr(operation, "close", None)
        if callable(close):
            close()
        raise
    operation_task = asyncio.ensure_future(operation)
    cancellation_task = asyncio.create_task(
        _wait_for_cancellation(
            cancellation,
            task_id=task_id,
            agent_run_id=agent_run_id,
            poll_interval_seconds=poll_interval_seconds,
        ),
        name="app-exploration-cancellation-wait",
    )
    try:
        completed, _ = await asyncio.wait(
            {operation_task, cancellation_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if cancellation_task in completed:
            cancellation_error = cancellation_task.exception()
            operation_task.cancel()
            with suppress(asyncio.CancelledError):
                await operation_task
            if cancellation_error is not None:
                raise cancellation_error
            raise AppExplorationCanceledError("App 探索已取消")
        result = await operation_task
        await raise_if_app_exploration_canceled(
            cancellation,
            task_id=task_id,
            agent_run_id=agent_run_id,
        )
        return result
    finally:
        cancellation_task.cancel()
        # 外层取消或租约失效也必须等待装置操作退出，不能遗留后台副作用。
        if not operation_task.done():
            operation_task.cancel()
        await asyncio.gather(operation_task, cancellation_task, return_exceptions=True)


async def _wait_for_cancellation(
    cancellation: AppWorkflowCancellation,
    *,
    task_id: str,
    agent_run_id: str,
    poll_interval_seconds: float,
) -> None:
    while True:
        if await cancellation.is_cancel_requested(
            task_id=task_id,
            agent_run_id=agent_run_id,
        ):
            return
        await asyncio.sleep(poll_interval_seconds)
