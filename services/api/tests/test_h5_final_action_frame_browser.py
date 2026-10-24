"""204 的后验滞后边界：最终证据已换页，动作轨迹仍引用上一张中间帧。"""

from unittest.mock import AsyncMock

import pytest
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright

from services.api.app.infrastructure.browser import page_capture
from services.api.app.infrastructure.browser.capture_runtime import (
    _logical_navigation_postcondition_error,
    _public_state_diff,
)
from services.api.app.infrastructure.browser.page_capture import (
    PageCaptureMode,
    PageCaptureRequest,
    PlaywrightPageCaptureBrowser,
)


@pytest.mark.parametrize("effect", ["business", "repaint", "modal", "web"])
async def test_postcondition_binds_final_evidence_not_intermediate_snapshot(monkeypatch, effect):
    monkeypatch.setattr(page_capture, "_wait_for_page_settle", AsyncMock(return_value=True))
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            context = await browser.new_context(viewport={"width": 390, "height": 844})
            await context.route(
                "https://fixture.test/**",
                lambda route: route.fulfill(
                    content_type="text/html",
                    body='<main><h1>内容列表</h1><button id="open">打开详情</button></main>',
                ),
            )
            page = await context.new_page()
            await page.goto("https://fixture.test/list")
            original = page_capture._collect_ui_snapshot
            count = 0

            async def snapshot(*args, **kwargs):
                nonlocal count
                result = await original(*args, **kwargs)
                count += 1
                if count == 2:
                    # 模拟路由在首张动作后观察之后、正式采集边界之前提交。
                    if effect in {"business", "web"}:
                        await page.evaluate("""() => {
                            history.pushState({}, '', '/video/7');
                            document.body.innerHTML =
                                '<main><h1>视频详情</h1><video controls></video></main>';
                        }""")
                    elif effect == "repaint":
                        await page.evaluate("document.body.style.background = 'rgb(1, 2, 3)'")
                    else:
                        await page.evaluate("""() => {
                            const dialog = document.createElement('div');
                            dialog.setAttribute('role', 'dialog');
                            dialog.setAttribute('aria-modal', 'true');
                            dialog.style = 'position:fixed;inset:0;background:white;z-index:9999';
                            dialog.innerHTML = '<button>关闭</button>';
                            document.body.append(dialog);
                        }""")
                return result

            monkeypatch.setattr(page_capture, "_collect_ui_snapshot", snapshot)
            result = await PlaywrightPageCaptureBrowser(
                logical_surface_enabled=True
            )._capture_on_page_impl(
                request=PageCaptureRequest(
                    mode=PageCaptureMode.SINGLE_TRIGGER,
                    trigger_action="click",
                    selector="#open",
                    session_id="web:fixture" if effect == "web" else "h5:fixture",
                    logical_surface_enabled=True,
                    capture_screenshot=False,
                    navigation_depth=1,
                ),
                context=context,
                page=page,
                console_logs=[],
                network_requests=[],
                timeout_error_type=PlaywrightTimeoutError,
            )
            assert count >= 3
            final = next(a.content for a in result.artifacts if a.artifact_type == "ui_snapshot")
            diff = _public_state_diff(result)
            error = _logical_navigation_postcondition_error(
                parameters={"logical_target": {"expects_logical_navigation": True}},
                state_diff=diff,
            )
            assert error == (None if effect == "business" else "LOGICAL_TRANSITION_UNVERIFIED")
            if effect != "web":
                assert diff["after_location"] == result.final_url
                assert diff["location_changed"] is (effect == "business")
                transition = diff["logical_surface_transition"]
                assert (
                    transition["after_state_fingerprint"]
                    == final["logical_surface"]["state_fingerprint"]
                )
                assert (
                    transition["after_identity_fingerprint"]
                    == final["logical_surface"]["identity_fingerprint"]
                )
        finally:
            await browser.close()
