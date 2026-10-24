"""App Run 执行租约守卫的协程生命周期测试。"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from services.api.app.application.app_run_recovery import (
    AppRunExecutionLease,
    AppRunExecutionLeaseGuard,
)


class RecordingRenewRepository:
    """仅实现守卫测试需要的续租端口。"""

    def __init__(self, lease: AppRunExecutionLease) -> None:
        self.lease = lease
        self.renew_count = 0

    async def renew(self, command):
        self.renew_count += 1
        self.lease = AppRunExecutionLease(
            task_id=command.task_id,
            agent_run_id=command.agent_run_id,
            lease_owner=command.lease_owner,
            lease_token=command.lease_token,
            lease_expires_at=datetime.now(UTC) + timedelta(seconds=command.lease_seconds),
            row_version=self.lease.row_version + 1,
            attempt_count=self.lease.attempt_count,
        )
        return self.lease


def _lease() -> AppRunExecutionLease:
    return AppRunExecutionLease(
        task_id="11111111-1111-1111-1111-111111111111",
        agent_run_id="22222222-2222-2222-2222-222222222222",
        lease_owner="app-worker-1",
        lease_token="33333333-3333-3333-3333-333333333333",
        lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
        row_version=0,
        attempt_count=1,
    )


async def test_guard_cancellation_stops_inflight_runtime_operation() -> None:
    repository = RecordingRenewRepository(_lease())
    guard = AppRunExecutionLeaseGuard(
        repository=repository,
        lease=repository.lease,
        lease_seconds=60,
        renew_interval_seconds=30,
    )
    operation_started = asyncio.Event()
    operation_stopped = asyncio.Event()

    async def operation() -> None:
        operation_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            operation_stopped.set()

    await guard.start()
    task = asyncio.create_task(guard.run(operation()))
    await operation_started.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert operation_stopped.is_set()
    await guard.stop()


async def test_pool_timeout_retries_until_renew_succeeds() -> None:
    repository = RecordingRenewRepository(_lease())
    calls = 0
    original = repository.renew

    async def flaky_renew(command):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise TimeoutError("pool acquire")
        return await original(command)

    repository.renew = flaky_renew
    guard = AppRunExecutionLeaseGuard(
        repository=repository,
        lease=repository.lease,
        lease_seconds=60,
        renew_interval_seconds=30,
    )
    await guard.start()
    assert calls == 2
    await guard.stop()
