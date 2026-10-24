"""App Explorer Work 的续租与旧 Worker 隔离。"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable
from contextlib import suppress
from typing import TypeVar

from services.api.app.application.app_frontier_queue import (
    AppFrontierQueue,
    RenewAppFrontierLeaseCommand,
)
from services.api.app.application.exploration_work_queue import (
    ExplorerWorkQueue,
    RenewExplorerWorkLeaseCommand,
)


class AppExplorationLeaseLostError(RuntimeError):
    """当前 Worker 已失去 Explorer Work 租约，必须停止外部操作。"""

    error_code = "APP_EXPLORATION_LEASE_LOST"


class AppFrontierLeaseLostError(AppExplorationLeaseLostError):
    """当前 Worker 已失去任务级 App Frontier 租约。"""

    error_code = "APP_FRONTIER_LEASE_LOST"


ResultT = TypeVar("ResultT")

_DEFAULT_RENEW_RETRY_DELAY_SECONDS = 0.25
_MAX_RENEW_RETRY_DELAY_SECONDS = 5.0


def _monotonic() -> float:
    """返回续租边界使用的单调时钟；测试可替换以避免真实等待。"""
    return time.monotonic()


async def _sleep(delay_seconds: float) -> None:
    """等待续租重试退避；测试可替换以推进确定性时钟。"""
    await asyncio.sleep(delay_seconds)


def _is_transient_renew_error(exc: BaseException) -> bool:
    """只把网络／数据库连接瞬态故障视为可重试。

    队列在 SQL 返回 0 行时会抛出自己的持久化拒绝异常；这类异常不能被
    误判成连接抖动。asyncpg/psycopg 的连接异常没有统一的跨驱动基类，
    因而只按模块和异常名识别连接、重置、超时相关类型。
    """
    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, (TimeoutError, ConnectionError, OSError)):
            return True
        module = type(current).__module__.lower()
        name = type(current).__name__.lower()
        if module.startswith(("asyncpg", "psycopg", "sqlalchemy")) and any(
            marker in name
            for marker in (
                "connection",
                "interface",
                "operational",
                "reset",
                "timeout",
            )
        ):
            return True
        current = current.__cause__ or current.__context__
    return False


class _RenewalWindowExpiredError(TimeoutError):
    """瞬态故障持续到本地已知租约窗口结束。"""


class _RenewingLeaseGuard:
    """对 Work 与 Frontier 复用同一套持续续租和旧 Worker 隔离逻辑。"""

    def __init__(
        self,
        *,
        queue: object,
        command: object,
        renew_interval_seconds: int,
        lost_error_type: type[AppExplorationLeaseLostError],
        lost_message: str,
        task_name: str,
    ) -> None:
        if (
            isinstance(renew_interval_seconds, bool)
            or not isinstance(renew_interval_seconds, int)
            or renew_interval_seconds <= 0
        ):
            raise ValueError("renew_interval_seconds 必须为正整数")
        self._queue = queue
        self._command = command
        self._renew_interval_seconds = renew_interval_seconds
        self._lost_error_type = lost_error_type
        self._lost_message = lost_message
        self._task_name = task_name
        self._monotonic = _monotonic
        self._sleep = _sleep
        self._renew_task: asyncio.Task[None] | None = None
        self._renew_failure: BaseException | None = None
        self._renew_failure_event = asyncio.Event()
        # claim/transition 或最近一次成功续租都已建立 lease_seconds 的窗口。
        # 续租调用可能因连接抖动失败，但在该窗口内仍不能直接判定旧 Worker 越权。
        self._lease_deadline: float | None = None

    async def __aenter__(self):
        await self.start()
        return self

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        del exc_type, exc, traceback
        await self.stop()

    async def start(self) -> None:
        if self._renew_task is not None:
            await self.assert_healthy()
            return
        self._lease_deadline = self._monotonic() + self._command.lease_seconds
        try:
            await self._renew_with_retry()
        except asyncio.CancelledError:
            raise
        except BaseException as exc:  # noqa: BLE001 - 转为统一租约失效错误
            self._renew_failure = exc
            self._renew_failure_event.set()
            await self.assert_healthy()
        self._renew_task = asyncio.create_task(
            self._renew_forever(),
            name=self._task_name,
        )
        await self.assert_healthy()

    async def assert_healthy(self) -> None:
        if self._renew_failure is not None:
            raise self._lost_error_type(self._lost_message) from self._renew_failure

    async def run(self, operation: Awaitable[ResultT]) -> ResultT:
        """续租失败时立刻取消外部操作，阻止旧 Worker 继续下一步调用。"""
        await self.assert_healthy()
        operation_task = asyncio.ensure_future(operation)
        lease_lost_task = asyncio.create_task(
            self._renew_failure_event.wait(),
            name=f"{self._task_name}-lost-wait",
        )
        try:
            completed, _ = await asyncio.wait(
                {operation_task, lease_lost_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if lease_lost_task in completed:
                operation_task.cancel()
                with suppress(asyncio.CancelledError):
                    await operation_task
                await self.assert_healthy()
            result = await operation_task
            await self.assert_healthy()
            return result
        finally:
            lease_lost_task.cancel()
            # 巢状租约／Worker 取消时，同步清理本层拥有的操作。
            if not operation_task.done():
                operation_task.cancel()
            await asyncio.gather(operation_task, lease_lost_task, return_exceptions=True)

    async def stop(self) -> None:
        task = self._renew_task
        self._renew_task = None
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        await self.assert_healthy()

    async def _renew_forever(self) -> None:
        try:
            while True:
                await self._sleep(self._renew_interval_seconds)
                await self._renew_with_retry()
        except asyncio.CancelledError:
            raise
        except BaseException as exc:  # noqa: BLE001 - 由调用协程在安全点统一处理
            self._renew_failure = exc
            self._renew_failure_event.set()

    async def _renew_with_retry(self) -> None:
        """在已知租约截止前重试瞬态连接故障，持久化拒绝立即收口。"""
        retry_delay = _DEFAULT_RENEW_RETRY_DELAY_SECONDS
        while True:
            try:
                await self._queue.renew(command=self._command)
            except asyncio.CancelledError:
                raise
            except BaseException as exc:  # noqa: BLE001 - 仅按下方策略分级
                if not _is_transient_renew_error(exc):
                    # 例如 SQL 条件已不满足、owner/token 不匹配等明确拒绝，
                    # 必须立即停止外部动作，不能用重试掩盖 fencing 事实。
                    raise
                remaining = self._remaining_lease_seconds()
                if remaining <= 0:
                    raise _RenewalWindowExpiredError(
                        "瞬态数据库故障持续到 Explorer/App Frontier 租约截止"
                    ) from exc
                await self._sleep(min(retry_delay, remaining))
                retry_delay = min(
                    retry_delay * 2,
                    _MAX_RENEW_RETRY_DELAY_SECONDS,
                )
            else:
                # renew SQL 以数据库 now() 为基准完成后，重新开始本地有效窗口。
                self._lease_deadline = self._monotonic() + self._command.lease_seconds
                return

    def _remaining_lease_seconds(self) -> float:
        """计算本地已知租约窗口剩余时间，避免无限重试。"""
        if self._lease_deadline is None:
            return 0.0
        return self._lease_deadline - self._monotonic()


class AppExplorationLeaseGuard(_RenewingLeaseGuard):
    """在动作、截图理解和后继提交期间持续续租。"""

    def __init__(
        self,
        *,
        queue: ExplorerWorkQueue,
        command: RenewExplorerWorkLeaseCommand,
        renew_interval_seconds: int,
    ) -> None:
        if not isinstance(command, RenewExplorerWorkLeaseCommand):
            raise TypeError("command 必须为 RenewExplorerWorkLeaseCommand")
        super().__init__(
            queue=queue,
            command=command,
            renew_interval_seconds=renew_interval_seconds,
            lost_error_type=AppExplorationLeaseLostError,
            lost_message="App Explorer Work 租约已失效",
            task_name="app-exploration-lease-renew",
        )


class AppFrontierLeaseGuard(_RenewingLeaseGuard):
    """在 sibling 恢复、设备动作和父 Work 提交期间持续续租。"""

    def __init__(
        self,
        *,
        queue: AppFrontierQueue,
        command: RenewAppFrontierLeaseCommand,
        renew_interval_seconds: int,
    ) -> None:
        if not isinstance(command, RenewAppFrontierLeaseCommand):
            raise TypeError("command 必须为 RenewAppFrontierLeaseCommand")
        super().__init__(
            queue=queue,
            command=command,
            renew_interval_seconds=renew_interval_seconds,
            lost_error_type=AppFrontierLeaseLostError,
            lost_message="App Frontier 租约已失效",
            task_name="app-frontier-lease-renew",
        )
