"""真实浏览器验证 Web UA 与 H5 原有 UA 的隔离。"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from playwright.async_api import async_playwright

from services.api.app.infrastructure.browser.page_capture import (
    PageCaptureMode,
    PageCaptureRequest,
    PlaywrightPageCaptureBrowser,
    _dispose_playwright_session,
)


class _LocalChromeBrowser(PlaywrightPageCaptureBrowser):
    def __init__(self, executable_path: str) -> None:
        super().__init__()
        self._executable_path = executable_path

    async def _launch_browser(self, playwright):
        return await playwright.chromium.launch(
            headless=True, executable_path=self._executable_path
        )


def _chrome_executable() -> str:
    configured = os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH")
    candidate = Path(configured) if configured else Path(
        r"C:\Program Files\Google\Chrome\Application\chrome.exe"
    )
    if not candidate.is_file():
        pytest.skip("本机未安装可用的 Chromium／Chrome")
    return str(candidate)


@pytest.mark.asyncio
async def test_web_chromium_replaces_headless_token_without_changing_h5() -> None:
    executable = _chrome_executable()
    observed: dict[str, tuple[str, str]] = {}

    for policy in ("direct_home", "h5"):
        browser = _LocalChromeBrowser(executable)
        request = PageCaptureRequest(
            mode=PageCaptureMode.OBSERVE,
            session_id=f"ua-{policy}",
            startup_policy=policy,
            viewport_width=1920,
            viewport_height=1080,
            is_mobile=False,
            has_touch=False,
        )
        session = await browser._create_session(async_playwright, request=request)
        try:
            headers: list[str] = []

            def route_handler(captured_headers: list[str]):
                async def fulfill(route):
                    captured_headers.append((await route.request.all_headers())["user-agent"])
                    await route.fulfill(
                        status=200, content_type="text/html", body="<main>ok</main>"
                    )

                return fulfill

            await session.page.route("https://ua.example.test/**", route_handler(headers))
            await session.page.goto("https://ua.example.test/")
            observed[policy] = (
                await session.page.evaluate("navigator.userAgent"), headers[0]
            )
        finally:
            await _dispose_playwright_session(session)

    web_js, web_http = observed["direct_home"]
    h5_js, h5_http = observed["h5"]
    assert "HeadlessChrome/" in h5_js
    assert h5_js == h5_http
    assert web_js == web_http == h5_js.replace("HeadlessChrome/", "Chrome/", 1)
