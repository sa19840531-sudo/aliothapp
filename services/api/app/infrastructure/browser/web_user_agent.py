"""从真实 Chromium 获取 Web 会话的浏览器 User-Agent。"""

from __future__ import annotations

from typing import Any


async def web_chrome_user_agent(browser: Any) -> str:
    """只替换无头标记，保留实际 Chromium 的平台与版本。"""
    context = await browser.new_context()
    try:
        page = await context.new_page()
        observed = await page.evaluate("navigator.userAgent")
    finally:
        await context.close()
    if not isinstance(observed, str) or "Chrome/" not in observed:
        raise RuntimeError("WEB_CHROMIUM_USER_AGENT_UNAVAILABLE")
    return observed.replace("HeadlessChrome/", "Chrome/", 1)
