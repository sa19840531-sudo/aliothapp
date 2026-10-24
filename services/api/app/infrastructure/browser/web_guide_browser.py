"""引导页独立 Chromium 只读观察，不借用主线 Page 或 Context。"""

from __future__ import annotations

import struct
from collections.abc import Callable
from typing import Any

from playwright.async_api import async_playwright

from services.api.app.application.web_ad_outbound_policy import validate_ad_url
from services.api.app.application.web_guide_candidate import WebGuideCandidate
from services.api.app.application.web_guide_page import WebGuideLanding
from services.api.app.infrastructure.browser.ad_isolated_page import isolated_ad_page
from services.api.app.infrastructure.browser.page_capture import _collect_ui_snapshot
from services.api.app.infrastructure.browser.web_user_agent import web_chrome_user_agent

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


class WebGuidePageBrowser:
    """使用独立浏览器进程；固定地址传输、无 Cookie、无下载和无提交。"""

    def __init__(
        self, *, browser_launcher: Callable[[Any], Any],
        playwright_factory: Callable[[], Any] = async_playwright,
        timeout_ms: int = 15_000,
    ):
        self._browser_launcher = browser_launcher
        self._playwright_factory = playwright_factory
        self._timeout_ms = timeout_ms

    async def observe(self, candidate: WebGuideCandidate) -> WebGuideLanding:
        if candidate.kind != "page_candidate":
            raise ValueError("WEB_GUIDE_DOWNLOAD_RESOURCE")
        destination = validate_ad_url(
            candidate.destination_url, previous_url=candidate.source_url,
        )
        async with self._playwright_factory() as playwright:
            browser = await self._browser_launcher(playwright)
            try:
                async with isolated_ad_page(
                    browser=browser,
                    url=destination,
                    source_url=candidate.source_url,
                    timeout_ms=self._timeout_ms,
                    snapshot_collector=_collect_ui_snapshot,
                    enable_javascript=True,
                    user_agent=await web_chrome_user_agent(browser),
                ) as (view, transport):
                    final_url = validate_ad_url(view.url, previous_url=destination)
                    screenshot = await view.screenshot()
                    _validate_png(screenshot)
                    entries = await view.har_entries()
                    chain = tuple(dict.fromkeys((
                        destination,
                        *(
                            str(item.get("url")) for item in entries
                            if isinstance(item, dict)
                            and item.get("resource_type") == "document"
                            and item.get("url")
                        ),
                        final_url,
                    )))
                    return WebGuideLanding(
                        final_url=final_url,
                        title=await view.title(),
                        html=await view.content(),
                        screenshot=screenshot,
                        ui_snapshot=await view.ui_snapshot(),
                        redirect_chain=chain[:8],
                    )
            finally:
                await browser.close()


def _validate_png(value: bytes) -> None:
    if not isinstance(value, bytes) or not 33 <= len(value) <= 8 * 1024 * 1024:
        raise ValueError("WEB_GUIDE_SCREENSHOT_INVALID")
    if not value.startswith(_PNG_SIGNATURE) or value[12:16] != b"IHDR":
        raise ValueError("WEB_GUIDE_SCREENSHOT_INVALID")
    width, height = struct.unpack(">II", value[16:24])
    if (width, height) != (1920, 1080):
        raise ValueError("WEB_GUIDE_SCREENSHOT_INVALID")
