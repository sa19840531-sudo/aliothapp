"""未知样式导航的完整几何识别不得扩大可执行站点范围。"""

import pytest
from playwright.async_api import async_playwright

from services.api.app.application.h5_adaptive_navigation import (
    adaptive_navigation_selector,
    discover_h5_adaptive_navigation_patterns,
)
from services.api.app.infrastructure.browser.page_capture import _collect_ui_snapshot


@pytest.mark.asyncio
async def test_external_link_preserves_native_internal_navigation_discovery() -> None:
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            page = await browser.new_page(viewport={"width": 390, "height": 844})
            await page.route(
                "https://fixture.test/**",
                lambda route: route.fulfill(content_type="text/html", body="<main>测试首页</main>"),
            )
            await page.goto("https://fixture.test/")
            await page.set_content("""
                <style>body {margin:0} nav {position:fixed;bottom:8px;left:0;width:390px}
                  ul {display:flex;justify-content:space-around;padding:0;margin:0;list-style:none}
                  a {display:block;width:24px;height:46px;font-size:12px}</style>
                <main><h1>测试首页</h1></main>
                <nav><ul>
                  <li><a href="/" aria-current="page">首页</a></li>
                  <li><a href="/section-a">甲区</a></li>
                  <li><a href="/section-b">乙区</a></li>
                  <li><a href="/section-c">丙区</a></li>
                  <li><a href="https://other.test/">账号</a></li>
                </ul></nav>
            """)
            snapshot = await _collect_ui_snapshot(page, surface_session_id="h5:fixture")
            patterns = discover_h5_adaptive_navigation_patterns(snapshot)

            assert len(patterns) == 1
            pattern = patterns[0]
            assert [item.label for item in pattern.items] == ["首页", "甲区", "乙区", "丙区"]
            selector = adaptive_navigation_selector(
                snapshot,
                pattern_signature=pattern.signature,
                navigation_index=2,
                navigation_label="甲区",
            )
            assert selector is not None
            assert await page.locator(selector).get_attribute("href") == "/section-a"
            assert (
                adaptive_navigation_selector(
                    snapshot,
                    pattern_signature=pattern.signature,
                    navigation_index=5,
                    navigation_label="账号",
                )
                is None
            )
        finally:
            await browser.close()
