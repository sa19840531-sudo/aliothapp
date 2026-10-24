"""App Bootstrap 租约守卫的续租与失效测试。"""

from __future__ import annotations

import asyncio

import pytest

from services.api.app.application.app_bootstrap_lease import (
    AppBootstrapLeaseGuard,
    AppBootstrapLeaseLostError,
)
from services.api.app.application.app_bootstrap_state import RenewAppBootstrapLeaseCommand
from services.api.app.application.app_metrics import (
    APP_BOOTSTRAP_LEASE_LOST_TOTAL,
    app_metric_value,
)


class RecordingLeaseRepository:
    def __init__(self, failure: BaseException | None = None) -> None:
        self.calls = 0
        self.failure = failure

    async def renew(self, command: RenewAppBootstrapLeaseCommand) -> None:
        del command
        self.calls += 1
        if self.failure is not None:
            raise self.failure


def _command() -> RenewAppBootstrapLeaseCommand:
    return RenewAppBootstrapLeaseCommand(
        task_id="11111111-1111-1111-1111-111111111111",
        session_id="app-session-1",
        platform_build_ref="app-build:aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        lease_owner="app-worker-1",
        lease_token="33333333-3333-3333-3333-333333333333",
        lease_seconds=120,
    )


async def test_guard_renews_in_background_and_stops_cleanly() -> None:
    repository = RecordingLeaseRepository()
    guard = AppBootstrapLeaseGuard(repository, _command(), renew_interval_seconds=0)

    await guard.start()
    await asyncio.sleep(1.05)
    await guard.assert_healthy()
    await guard.stop()

    assert repository.calls >= 1


async def test_guard_start_renews_synchronously_and_exposes_original_failure() -> None:
    renewal_error = RuntimeError("lease rejected")
    repository = RecordingLeaseRepository(renewal_error)
    guard = AppBootstrapLeaseGuard(repository, _command(), renew_interval_seconds=0)
    metric_before = app_metric_value(APP_BOOTSTRAP_LEASE_LOST_TOTAL)

    with pytest.raises(AppBootstrapLeaseLostError) as error:
        await guard.start()
    assert error.value.__cause__ is renewal_error
    assert repository.calls == 1
    with pytest.raises(AppBootstrapLeaseLostError):
        await guard.stop()
    assert app_metric_value(APP_BOOTSTRAP_LEASE_LOST_TOTAL) == metric_before + 1
