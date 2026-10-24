"""Worker 的消费资源许可与冻结 Run 墙钟、取消监护。"""

from __future__ import annotations

import asyncio
import math
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from services.api.app.infrastructure.queue.redis_queue import AgentRunRecord


class WorkerWorkflowCanceled(RuntimeError):
    """运行期间收到权威取消事实，执行协程已停止。"""


class WorkerWorkflowDeadlineExceeded(TimeoutError):
    """冻结墙钟预算已经耗尽，执行协程已停止。"""


async def run_with_worker_controls[T](
    *,
    record: AgentRunRecord,
    default_duration_seconds: float,
    operation: Callable[[], Awaitable[T]],
    is_cancel_requested: Callable[[], Awaitable[bool]],
    poll_interval_seconds: float = 1.0,
) -> T:
    """以原 Run 开始时间监督完整 Workflow；退出必须等执行和监护协程停止。"""
    frozen_duration = record.loop_guard.get("max_duration_seconds", default_duration_seconds)
    if (
        isinstance(frozen_duration, bool)
        or not isinstance(frozen_duration, (int, float))
        or frozen_duration <= 0
        or not math.isfinite(frozen_duration)
    ):
        raise ValueError("Run 冻结墙钟预算必须是正数")
    elapsed = (
        max(0.0, (datetime.now(UTC) - record.started_at).total_seconds())
        if record.started_at is not None
        else 0.0
    )
    remaining = float(frozen_duration) - elapsed
    async def monitor_cancel() -> None:
        while True:
            await asyncio.sleep(poll_interval_seconds)
            if await is_cancel_requested():
                return

    execution: asyncio.Task[T] | None = None
    monitor: asyncio.Task[None] | None = None
    deadline = asyncio.timeout(remaining)
    try:
        async with deadline:
            # 首次权威取消读取也在同一墙钟内，读库阻塞不能无限占用执行许可。
            if await is_cancel_requested():
                raise WorkerWorkflowCanceled("任务已取消")
            if remaining <= 0:
                raise WorkerWorkflowDeadlineExceeded("Run 冻结墙钟预算已耗尽")
            execution = asyncio.create_task(operation())
            monitor = asyncio.create_task(monitor_cancel())
            completed, _ = await asyncio.wait(
                {execution, monitor}, return_when=asyncio.FIRST_COMPLETED
            )
            if monitor in completed:
                # 取消事实读取失败也停止当前副作用，保留原基础设施异常。
                await monitor
                raise WorkerWorkflowCanceled("运行期间收到取消请求")
            return await execution
    except TimeoutError as exc:
        if not deadline.expired():
            raise
        raise WorkerWorkflowDeadlineExceeded("Run 冻结墙钟预算已耗尽") from exc
    finally:
        tasks = [task for task in (execution, monitor) if task is not None]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
