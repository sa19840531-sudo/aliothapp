"""H5 动态正文就绪与真实滚动证据的受控浏览器回归。"""

from time import monotonic

import pytest
from playwright.async_api import async_playwright

from services.api.app.infrastructure.browser.page_capture import (
    _collect_ui_snapshot,
    _wait_for_page_settle,
)


@pytest.mark.parametrize("variant", ["h5", "web", "blocking", "skeleton", "media"])
async def test_dynamic_business_wait_preserves_loading_and_web_boundary(variant):
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            page = await browser.new_page(viewport={"width": 390, "height": 844})
            await page.set_content("""
              <main><h1>这是可以真实阅读的业务内容，而不是加载中的占位页面</h1>
              <ul><li>第一个内容条目</li><li>第二个内容条目</li>
              <li>第三个内容条目</li><li>第四个内容条目</li></ul></main>
              <div class="spinner" style="width:20px;height:20px">加载</div>
              <span id="activity"></span>
              <script>setInterval(() => document.querySelector('#activity')
                .setAttribute('data-tick', String(Date.now())), 20);</script>
            """)
            if variant == "blocking":
                await page.locator(".spinner").evaluate(
                    "node => node.style.cssText='position:fixed;inset:0;background:white'"
                )
            elif variant == "skeleton":
                await page.locator("main").evaluate("node => node.className='skeleton'")
            elif variant == "media":
                async def slow_media(route):
                    await route.abort()
                await page.route("https://fixture.test/pending.png", slow_media)
                await page.locator("main").evaluate("""node => {
                    const img=document.createElement('img'); img.width=100; img.height=100;
                    img.src='https://fixture.test/pending.png'; node.prepend(img);
                }""")
            diagnostics = {}
            started = monotonic()
            ready = await _wait_for_page_settle(
                page=page, max_wait_ms=500, poll_interval_ms=40, stable_samples=2,
                allow_startup_obstruction_shortcut=False,
                allow_dynamic_business=variant != "web", diagnostics=diagnostics,
            )
            assert ready is (variant == "h5")
            if ready:
                assert monotonic() - started < .45
                assert diagnostics["outcome"] == "business_content_ready"
                assert "dom_quiet" not in diagnostics["satisfied_signals"]
        finally:
            await browser.close()


async def test_scroll_snapshot_records_real_container_and_not_assumed_bottom():
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            page = await browser.new_page(viewport={"width": 390, "height": 844})
            await page.set_content("""
              <main id="content" style="height:500px;overflow-y:auto">
              <article style="height:1200px"><h1>业务内容</h1><p>真实末项</p></article></main>
            """)
            first = await _collect_ui_snapshot(page, surface_session_id="h5:fixture")
            assert first["h5_scroll_state"]["container_selector"] == "#content"
            assert first["h5_scroll_state"]["at_bottom"] is False
            await page.locator("#content").evaluate("node => node.scrollTop=node.scrollHeight")
            last = await _collect_ui_snapshot(page, surface_session_id="h5:fixture")
            assert last["h5_scroll_state"]["at_bottom"] is True
            assert last["h5_scroll_state"]["scroll_top"] > 0
            web = await _collect_ui_snapshot(page, surface_session_id="web:fixture")
            assert "h5_scroll_state" not in web
        finally:
            await browser.close()
