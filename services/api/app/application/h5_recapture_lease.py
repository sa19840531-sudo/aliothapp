"""补证恢复阶段的请求续租与截止取消，复用已有续租围栏。"""

import asyncio
import inspect
from collections.abc import Awaitable, Callable
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import datetime

from services.api.app.application.app_exploration_lease import (
    AppExplorationLeaseLostError,
    _RenewingLeaseGuard,
)
from services.api.app.application.evidence_requests import EvidenceRequestProcessingLease
from services.api.app.application.h5_session_journey import (
    quarantine_h5_browser_operations,
    quarantine_h5_journeys,
    track_h5_physical_operation,
)

H5_CANCEL_DRAIN_TIMEOUT_SECONDS = 5
_pending_physical_tasks = set()


class H5PhysicalOperationNotDrained(RuntimeError):
    error_code = "H5_PHYSICAL_OPERATION_NOT_DRAINED"
    retryable = False


def retain_h5_pending_operation(task):
    if task in _pending_physical_tasks:
        return
    _pending_physical_tasks.add(task)

    def ended(done):
        _pending_physical_tasks.discard(done)
        if not done.cancelled():
            done.exception()

    task.add_done_callback(ended)


async def run_h5_physical_operation(operation, *, timeout_seconds):
    """保护实际子操作，不让wait_for的取消等待越过有界清理窗口。"""
    if timeout_seconds <= 0:
        if inspect.iscoroutine(operation):
            operation.close()
        else:
            await _cancel_and_drain(asyncio.ensure_future(operation))
        raise TimeoutError("H5 页面执行窗口已耗尽，禁止启动新物理操作")
    task = asyncio.ensure_future(operation)
    track_h5_physical_operation(task)
    try:
        async with asyncio.timeout(timeout_seconds):
            return await asyncio.shield(task)
    except (asyncio.CancelledError, TimeoutError):
        quarantine_h5_browser_operations("H5_BROWSER_WAIT_STOP_UNCONFIRMED")
        raise
    finally:
        await _cancel_and_drain(task)


class H5RecaptureLeaseLostError(AppExplorationLeaseLostError):
    error_code = "H5_RECAPTURE_LEASE_LOST"


class H5RecaptureDeadlineError(RuntimeError):
    error_code = "H5_RECAPTURE_DEADLINE_EXHAUSTED"
    retryable = False


@dataclass(frozen=True)
class _Renewal:
    lease: EvidenceRequestProcessingLease
    agent_run_id: str
    lease_seconds: int
    deadline: datetime


class _RequestLeasePort:
    def __init__(self, renew):
        self._renew = renew

    async def renew(self, *, command: _Renewal):
        if await self._renew(
            lease=command.lease, agent_run_id=command.agent_run_id,
            lease_seconds=command.lease_seconds, deadline=command.deadline,
        ) is not True:
            raise H5RecaptureLeaseLostError("补证请求租约围栏已失效")


async def _cancel_and_drain(task):
    # 重复取消只作用于所有者，不能再次打断子操作的异步清理。
    if not task.done() and (not isinstance(task, asyncio.Task) or not task.cancelling()):
        task.cancel()
    cancelled = False
    deadline = asyncio.get_running_loop().time() + H5_CANCEL_DRAIN_TIMEOUT_SECONDS
    while not task.done():
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0 or task in _pending_physical_tasks:
            quarantine_h5_journeys("H5_PHYSICAL_OPERATION_NOT_DRAINED")
            if task not in _pending_physical_tasks:
                retain_h5_pending_operation(task)
            raise H5PhysicalOperationNotDrained("H5 子操作不响应取消，物理排空未确认")
        try:
            await asyncio.wait({task}, timeout=remaining)
        except asyncio.CancelledError:
            cancelled = cancelled or not task.done()
    if not task.cancelled():
        task.exception()
    if cancelled:
        raise asyncio.CancelledError


class H5BoundedLeaseGuard(_RenewingLeaseGuard):
    """H5 请求与Probe共用有界续租，不改变App/Web的租约实现。"""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._renew_interval_seconds = min(
            self._renew_interval_seconds, self._command.lease_seconds / 3,
        )

    async def _renew_with_retry(self):
        # 数据库等待不能越过已知租约；短租约按比例留取消窗口，不扩大租期。
        remaining = self._remaining_lease_seconds() - min(1, self._command.lease_seconds / 10)
        if remaining <= 0:
            raise H5RecaptureLeaseLostError("H5 请求续租窗口已耗尽")
        async with asyncio.timeout(remaining):
            await super()._renew_with_retry()

    async def run(self, operation):
        task = asyncio.ensure_future(operation)
        track_h5_physical_operation(task)
        lost = asyncio.create_task(self._renew_failure_event.wait())
        try:
            await self.assert_healthy()
            completed, _ = await asyncio.wait({task, lost}, return_when=asyncio.FIRST_COMPLETED)
            if lost in completed:
                await _cancel_and_drain(task)
                await self.assert_healthy()
            result = task.result()
            await self.assert_healthy()
            return result
        finally:
            # 主协程取消也必须排空动作，不能释放旅程后让旧子协程继续切页。
            try:
                await _cancel_and_drain(lost)
            finally:
                await _cancel_and_drain(task)

    async def __aenter__(self):
        await super().__aenter__()
        owner = asyncio.current_task()

        async def watch():
            await self._renew_failure_event.wait()
            owner.cancel()

        self._owner_watch = asyncio.create_task(watch(), name="h5-journey-lease-lost")
        return self

    async def __aexit__(self, *args):
        try:
            await _cancel_and_drain(self._owner_watch)
        finally:
            await super().__aexit__(*args)

    async def stop(self):
        task, self._renew_task = self._renew_task, None
        if task is not None:
            await _cancel_and_drain(task)
        await self.assert_healthy()


async def run_h5_recapture_stage(
    *, operation: Callable[[], Awaitable], requests, lease: EvidenceRequestProcessingLease,
    agent_run_id: str, lease_seconds: int, deadline: datetime, now: Callable[[], datetime],
):
    renew = getattr(requests, "renew_h5_lease", None)
    guard = (H5BoundedLeaseGuard(
        queue=_RequestLeasePort(renew),
        command=_Renewal(lease, agent_run_id, lease_seconds, deadline),
        renew_interval_seconds=max(1, lease_seconds // 3),
        lost_error_type=H5RecaptureLeaseLostError,
        lost_message="H5 补证请求租约已失效", task_name="h5-recapture-lease-renew",
    ) if callable(renew) else None)
    seconds = (deadline - now()).total_seconds()
    if seconds <= 0:
        raise H5RecaptureDeadlineError("补证准备已超过冻结截止")
    timeout = asyncio.timeout(seconds)
    task = None
    try:
        async with timeout:
            async with guard if guard is not None else nullcontext():
                task = asyncio.ensure_future(operation())
                try:
                    result = (await guard.run(task) if guard is not None
                              else await asyncio.shield(task))
                finally:
                    # 外层取消、超时或丢租均先排空自己的操作，再停止续租和结算请求。
                    await _cancel_and_drain(task)
        if timeout.expired() or now() >= deadline:
            raise H5RecaptureDeadlineError("补证阶段晚到结果超过冻结截止")
        return result
    except TimeoutError:
        if not timeout.expired():
            raise
        raise H5RecaptureDeadlineError("补证阶段用尽冻结时间，实际操作已停止") from None
