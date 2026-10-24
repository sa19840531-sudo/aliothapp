"""使用真实 Chromium 验证绑定 Run 的页面会话崩溃后可以重建。"""

from __future__ import annotations

import argparse
import asyncio
import json
from uuid import uuid4

from services.api.app.infrastructure.browser.page_capture import (
    BrowserSessionCrashedError,
    PageCaptureMode,
    PageCaptureRequest,
    PlaywrightPageCaptureBrowser,
)


async def verify_browser_crash_recovery(*, url: str, timeout_ms: int) -> dict[str, object]:
    """打开目标页、终止 Chromium、确认故障分级，再使用 goto 重建会话。"""
    browser = PlaywrightPageCaptureBrowser(default_timeout_ms=timeout_ms)
    session_id = f"browser-crash-acceptance-{uuid4()}"
    request_options = {
        "capture_dom": False,
        "capture_visible_text": False,
        "capture_screenshot": False,
        "full_page": False,
        "frame_count": 1,
        "timeout_ms": timeout_ms,
        "session_id": session_id,
    }
    try:
        initial = await browser.capture(
            PageCaptureRequest(
                mode=PageCaptureMode.GOTO,
                url=url,
                **request_options,
            )
        )
        session = browser._sessions.get(session_id)
        if session is None:
            raise RuntimeError("首次采集后未建立绑定 Run 的浏览器会话")

        await session.browser.close()
        try:
            await browser.capture(
                PageCaptureRequest(
                    mode=PageCaptureMode.OBSERVE,
                    **request_options,
                )
            )
        except BrowserSessionCrashedError as exc:
            crash_error_code = exc.error_code
        else:
            raise RuntimeError("浏览器终止后未返回 PAGE_CAPTURE_SESSION_CRASHED")

        recovered = await browser.capture(
            PageCaptureRequest(
                mode=PageCaptureMode.GOTO,
                url=url,
                **request_options,
            )
        )
        if recovered.execution_status != "success" or recovered.reachable is not True:
            raise RuntimeError("浏览器会话重建后目标页面未恢复可达")
        return {
            "status": "passed",
            "session_id": session_id,
            "initial_url": initial.final_url,
            "crash_error_code": crash_error_code,
            "recovered_url": recovered.final_url,
            "recovered": True,
        }
    finally:
        await browser.close_session(session_id)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="真实终止一次 Chromium 会话并验证 Alioth 页面采集恢复路径"
    )
    parser.add_argument("--url", required=True, help="用于验收的低风险目标 URL")
    parser.add_argument("--timeout-ms", type=int, default=30_000)
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    result = asyncio.run(
        verify_browser_crash_recovery(
            url=args.url,
            timeout_ms=args.timeout_ms,
        )
    )
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
