"""App Agent Run 的执行租约、恢复快照与持续续租合同。"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol, TypeVar

from services.api.app.application.app_exploration_lease import (
    _is_transient_renew_error,
)
from services.api.app.infrastructure.queue.redis_queue import AgentRunRecord


class AppRunExecutionLeaseLostError(RuntimeError):
    """当前 Worker 已失去 App Run 执行租约，必须停止 Runtime。"""

    error_code = "APP_RUN_EXECUTION_LEASE_LOST"


class AppRunRecoveryPersistenceError(RuntimeError):
    """App Run 恢复事实的归属、租约或状态不满足写入条件。"""


@dataclass(frozen=True, kw_only=True)
class AppRunExecutionLease:
    """同一 App Agent Run 在任一时刻唯一的跨进程执行权。"""

    task_id: str
    agent_run_id: str
    lease_owner: str
    lease_token: str
    lease_expires_at: datetime
    row_version: int
    attempt_count: int


@dataclass(frozen=True, kw_only=True)
class AcquireAppRunExecutionLeaseCommand:
    """领取或接管已过期的 App Run 执行租约。"""

    task_id: str
    agent_run_id: str
    lease_owner: str
    lease_token: str
    lease_seconds: int


@dataclass(frozen=True, kw_only=True)
class RenewAppRunExecutionLeaseCommand:
    """续租当前 App Run 执行权。"""

    task_id: str
    agent_run_id: str
    lease_owner: str
    lease_token: str
    lease_seconds: int


@dataclass(frozen=True, kw_only=True)
class ReleaseAppRunExecutionLeaseCommand:
    """在 Run 已持久化终态后释放执行租约。"""

    task_id: str
    agent_run_id: str
    lease_owner: str
    lease_token: str


@dataclass(frozen=True, kw_only=True)
class AppRunRecoverySnapshot:
    """PostgreSQL 计算出的 App Run 恢复时机，不由 Redis 推断。"""

    record: AgentRunRecord
    ready: bool
    reason: str
    next_recovery_at: datetime | None = None
    execution_lease_active: bool = False


class AppRunRecoveryRepository(Protocol):
    """App Run 执行租约与恢复候选的持久化端口。"""

    async def acquire(
        self,
        command: AcquireAppRunExecutionLeaseCommand,
    ) -> AppRunExecutionLease | None: ...

    async def renew(
        self,
        command: RenewAppRunExecutionLeaseCommand,
    ) -> AppRunExecutionLease: ...

    async def release(
        self,
        command: ReleaseAppRunExecutionLeaseCommand,
    ) -> bool: ...

    async def list_recovery_snapshots(
        self,
        *,
        limit: int = 100,
    ) -> list[AppRunRecoverySnapshot]: ...

    async def get_recovery_snapshot(
        self,
        *,
        agent_run_id: str,
    ) -> AppRunRecoverySnapshot | None: ...


ResultT = TypeVar("ResultT")


class AppRunExecutionLeaseGuard:
    """运行 App Runtime 时持续续租，租约丢失会取消正在进行的外部操作。"""

    def __init__(
        self,
        *,
        repository: AppRunRecoveryRepository,
        lease: AppRunExecutionLease,
        lease_seconds: int,
        renew_interval_seconds: int,
    ) -> None:
        if lease_seconds <= 0 or renew_interval_seconds <= 0:
            raise ValueError("App Run 租约和续租间隔必须为正整数")
        self._repository = repository
        self._lease = lease
        self._lease_seconds = lease_seconds
        self._renew_interval_seconds = renew_interval_seconds
        self._renew_task: asyncio.Task[None] | None = None
        self._renew_failure: BaseException | None = None
        self._renew_failure_event = asyncio.Event()

    async def __aenter__(self) -> AppRunExecutionLeaseGuard:
        await self.start()
        return self

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        del exc_type, exc, traceback
        await self.stop()

    async def start(self) -> None:
        if self._renew_task is not None:
            await self.assert_healthy()
            return
        await self._renew_once()
        self._renew_task = asyncio.create_task(
            self._renew_forever(),
            name=f"app-run-lease:{self._lease.agent_run_id}",
        )

    async def assert_healthy(self) -> None:
        if self._renew_failure is not None:
            raise AppRunExecutionLeaseLostError("App Run 执行租约已失效") from (
                self._renew_failure
            )

    async def run(self, operation: Awaitable[ResultT]) -> ResultT:
        """续租失败时取消当前操作，旧 Worker 不得继续碰设备或持久化事实。"""

        await self.assert_healthy()
        operation_task = asyncio.ensure_future(operation)
        lease_lost_task = asyncio.create_task(
            self._renew_failure_event.wait(),
            name=f"app-run-lease-lost:{self._lease.agent_run_id}",
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
        except asyncio.CancelledError:
            # Worker 停机或上层取消时，必须同步停止设备/Runtime 操作；
            # 不能只结束等待者却把真实操作遗留在后台继续写事实。
            operation_task.cancel()
            await asyncio.gather(operation_task, return_exceptions=True)
            raise
        finally:
            lease_lost_task.cancel()
            with suppress(asyncio.CancelledError):
                await lease_lost_task

    async def stop(self) -> None:
        task = self._renew_task
        self._renew_task = None
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        await self.assert_healthy()

    async def _renew_once(self) -> None:
        delay = 1.0
        while True:
            try:
                self._lease = await self._repository.renew(
                    RenewAppRunExecutionLeaseCommand(
                        task_id=self._lease.task_id,
                        agent_run_id=self._lease.agent_run_id,
                        lease_owner=self._lease.lease_owner,
                        lease_token=self._lease.lease_token,
                        lease_seconds=self._lease_seconds,
                    )
                )
                return
            except asyncio.CancelledError:
                raise
            except BaseException as exc:  # noqa: BLE001 - 瞬态连接失败在租约窗口内重试
                remaining = self._lease_remaining_seconds()
                if not _is_transient_renew_error(exc) or remaining <= 1:
                    self._renew_failure = exc
                    self._renew_failure_event.set()
                    await self.assert_healthy()
                await asyncio.sleep(min(delay, remaining))
                delay = min(delay * 2, 8.0)

    def _lease_remaining_seconds(self) -> float:
        expires = self._lease.lease_expires_at
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=UTC)
        return (expires - datetime.now(UTC)).total_seconds()

    async def _renew_forever(self) -> None:
        try:
            while True:
                await asyncio.sleep(self._renew_interval_seconds)
                await self._renew_once()
        except asyncio.CancelledError:
            raise
        except BaseException as exc:  # noqa: BLE001 - 调用协程在安全点处理
            self._renew_failure = exc
            self._renew_failure_event.set()
