"""App Bootstrap Workflow 租约续租与失效隔离。"""

from __future__ import annotations

import asyncio

from services.api.app.application.app_bootstrap_state import (
    AppBootstrapRepository,
    RenewAppBootstrapLeaseCommand,
)
from services.api.app.application.app_metrics import (
    APP_BOOTSTRAP_LEASE_LOST_TOTAL,
    record_app_metric_safely,
)


class AppBootstrapLeaseLostError(RuntimeError):
    """当前 Worker 不再持有 Bootstrap 租约，后续操作必须立即拒绝。"""


class AppBootstrapLeaseGuard:
    """在 Workflow 生命周期内续租，并将续租失败转换为可检查的失效状态。"""

    def __init__(
        self,
        repository: AppBootstrapRepository,
        command: RenewAppBootstrapLeaseCommand,
        renew_interval_seconds: int,
    ) -> None:
        if not isinstance(command, RenewAppBootstrapLeaseCommand):
            raise TypeError("command 必须为 RenewAppBootstrapLeaseCommand")
        if (
            isinstance(renew_interval_seconds, bool)
            or not isinstance(renew_interval_seconds, int)
        ):
            raise TypeError("renew_interval_seconds 必须为整数")
        self._repository = repository
        self._command = command
        self._renew_interval_seconds = max(1, renew_interval_seconds)
        self._renew_task: asyncio.Task[None] | None = None
        self._renew_failure: BaseException | None = None
        self._lease_lost_recorded = False

    async def __aenter__(self) -> AppBootstrapLeaseGuard:
        await self.start()
        return self

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        del exc_type, exc, traceback
        await self.stop()

    async def start(self) -> None:
        if self._renew_task is not None:
            await self.assert_healthy()
            return
        try:
            await self._repository.renew(self._command)
        except asyncio.CancelledError:
            raise
        except BaseException as exc:  # noqa: BLE001
            self._renew_failure = exc
            await self.assert_healthy()
        self._renew_task = asyncio.create_task(
            self._renew_forever(),
            name="app-bootstrap-lease-renew",
        )
        await self.assert_healthy()

    async def assert_healthy(self) -> None:
        if self._renew_failure is not None:
            if not self._lease_lost_recorded:
                record_app_metric_safely(
                    APP_BOOTSTRAP_LEASE_LOST_TOTAL,
                    phase="bootstrap",
                )
                self._lease_lost_recorded = True
            raise AppBootstrapLeaseLostError(
                "App Bootstrap Workflow 租约已失效"
            ) from self._renew_failure

    async def stop(self) -> None:
        task = self._renew_task
        self._renew_task = None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        await self.assert_healthy()

    async def _renew_forever(self) -> None:
        try:
            while True:
                await asyncio.sleep(self._renew_interval_seconds)
                await self._repository.renew(self._command)
        except asyncio.CancelledError:
            raise
        except BaseException as exc:  # noqa: BLE001
            self._renew_failure = exc
