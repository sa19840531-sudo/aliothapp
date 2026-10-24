"""H5 业务点击不能因页面促销文案提前结束路由等待。"""

import asyncio

import pytest
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright

from services.api.app.infrastructure.browser import page_capture
from services.api.app.infrastructure.browser.capture_runtime import (
    _logical_navigation_postcondition_error,
    _public_state_diff,
)


@pytest.mark.parametrize(
    "shortcut,expected", [(True, "settled"), (False, "settled")],
)
async def test_promotion_countdown_requires_stability_even_when_shortcut_is_enabled(
    shortcut, expected,
):
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            page = await browser.new_page()
            await page.set_content("<main><h1>产品首页</h1><p>尾款最后倒计时1天</p></main>")
            diagnostics = {}
            assert await page_capture._wait_for_page_settle(
                page=page, diagnostics=diagnostics, max_wait_ms=5000,
                allow_startup_obstruction_shortcut=shortcut,
            ) is True
            assert diagnostics["outcome"] == expected
            assert diagnostics["stable_sample_count"] >= page_capture.PAGE_SETTLE_STABLE_SAMPLES
        finally:
            await browser.close()


@pytest.mark.parametrize("destination", ["/detail", None])
async def test_h5_business_click_waits_for_actual_route_not_promotion_text(
    monkeypatch, destination,
):
    original_collect = page_capture._collect_artifacts

    async def collect_after_route(**kwargs):
        # 模拟截图采集期间异步路由已经落地，而动作轨迹仍沿用早期状态。
        await asyncio.sleep(0.6)
        return await original_collect(**kwargs)

    monkeypatch.setattr(page_capture, "_collect_artifacts", collect_after_route)
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            page = await browser.new_page(viewport={"width": 390, "height": 844})

            async def respond(route):
                await route.fulfill(content_type="text/html; charset=utf-8", body="""
                    <main><h1>内容列表</h1><p>尾款最后倒计时1天</p>
                      <button id="card">查看详情</button></main>
                    <script>window.clicks=0;document.querySelector('#card').onclick=()=>{
                      window.clicks++;
                      if(window.destination)setTimeout(()=>{
                        history.pushState({},'',window.destination);
                        document.body.innerHTML='<main><h1>内容详情</h1><p>正文</p></main>';
                      },350);
                    };</script>
                """)

            await page.route("https://delayed.test/**", respond)
            await page.goto("https://delayed.test/list")
            await page.evaluate("value=>window.destination=value", destination)
            collector = page_capture.PlaywrightPageCaptureBrowser(logical_surface_enabled=True)
            result = await collector._capture_on_page_impl(
                request=page_capture.PageCaptureRequest(
                    mode=page_capture.PageCaptureMode.SINGLE_TRIGGER,
                    trigger_action="click", selector="#card", session_id="h5:fixture",
                    navigation_depth=1, logical_surface_enabled=True, capture_screenshot=False,
                ), page=page, context=page.context, console_logs=[], network_requests=[],
                timeout_error_type=PlaywrightTimeoutError,
            )
            assert await page.evaluate("window.clicks") == 1
            diff = _public_state_diff(result)
            expected_url = "https://delayed.test" + (destination or "/list")
            assert result.final_url == expected_url
            assert diff["after_location"] == expected_url
            assert _logical_navigation_postcondition_error(
                parameters={"logical_target": {"expects_logical_navigation": True}},
                state_diff=diff,
            ) == (None if destination else "LOGICAL_TRANSITION_UNVERIFIED")
        finally:
            await browser.close()
