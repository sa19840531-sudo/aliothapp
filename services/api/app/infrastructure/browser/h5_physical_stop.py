"""原实例关闭确认与原浏览器PID消失共同构成停止证明。"""

import asyncio
import os
from datetime import UTC, datetime
from uuid import uuid4

from services.api.app.application.h5_recapture_lease import (
    H5PhysicalOperationNotDrained,
    retain_h5_pending_operation,
)

H5_BROWSER_STOP_TIMEOUT_SECONDS = 5


class H5BrowserStopGuard:
    def __init__(self, *, browser, browser_pid, playwright=None, cli_session=None):
        self.resource_id = str(uuid4())
        self.browser_pid = browser_pid
        self._browser = browser
        self._playwright = playwright
        self._cli_session = cli_session
        self._stop_task = None

    @classmethod
    async def bind(cls, browser, *, owned_local=False, playwright=None, cli_session=None):
        if not owned_local:
            raise ValueError("非本地独占启动的浏览器不能绑定停止证明，也不能由此链路关闭")
        pid, session = None, None
        try:
            async with asyncio.timeout(2):
                session = await browser.new_browser_cdp_session()
                processes = await session.send("SystemInfo.getProcessInfo")
                pids = [p["id"] for p in processes["processInfo"] if p["type"] == "browser"]
                if len(pids) == 1 and isinstance(pids[0], int) and pids[0] > 0:
                    pid = pids[0]
        except Exception:
            # 身份读取失败不猜进程；此实例只能保留停止未确认状态。
            pass
        finally:
            if session is not None:
                try:
                    async with asyncio.timeout(1):
                        await session.detach()
                except Exception:
                    pass
        return cls(browser=browser, browser_pid=pid, playwright=playwright, cli_session=cli_session)

    async def stop_and_prove(self):
        if self._stop_task is None:
            self._stop_task = asyncio.create_task(self._stop())
        task = self._stop_task
        # 不使用wait_for；关闭协程吞取消时保留原句柄，不能启动第二次关闭冒充ACK。
        try:
            done, _ = await asyncio.wait({task}, timeout=H5_BROWSER_STOP_TIMEOUT_SECONDS)
        except asyncio.CancelledError:
            retain_h5_pending_operation(task)
            raise
        if not done:
            retain_h5_pending_operation(task)
            raise H5PhysicalOperationNotDrained("H5 浏览器关闭在途，停止未确认")
        return task.result()

    async def _stop(self):
        await self._browser.close()
        if self.browser_pid is None or self._browser.is_connected():
            raise H5PhysicalOperationNotDrained("H5 浏览器缺少原进程身份或仍连接，不能解封")
        try:
            # 只观察PID，不发送停止信号。PID被复用也保守拒绝，不误杀任何进程。
            os.kill(self.browser_pid, 0)
        except ProcessLookupError:
            if self._cli_session is not None:
                await self._cli_session.close()
            if self._playwright is not None:
                await self._playwright.stop()
            return {
                "resource_id": self.resource_id, "browser_pid": self.browser_pid,
                "method": "owned_browser_close_ack_and_pid_absent",
                "observed_at": datetime.now(UTC).isoformat(),
                "driver_stop_ack": self._playwright is not None,
                "cli_detach_ack": self._cli_session is not None,
            }
        except PermissionError:
            pass
        raise H5PhysicalOperationNotDrained("H5 原浏览器进程仍存在或不可核验，不能解封")
