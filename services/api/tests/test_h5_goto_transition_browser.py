"""H5 直开导航必须留下真实前后状态，不能只证明目标文档可读。"""

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


@pytest.mark.parametrize(
    "session_id,enabled,destination,expected",
    [
        ("h5:fixture", True, "/videos", None),
        ("h5:fixture", True, "/posts", "LOGICAL_TRANSITION_UNVERIFIED"),
        ("h5:fixture", False, "/videos", "LOGICAL_TRANSITION_UNVERIFIED"),
        ("web:fixture", True, "/videos", "LOGICAL_TRANSITION_UNVERIFIED"),
    ],
)
async def test_goto_records_h5_transition_without_accepting_same_route_or_changing_web(
    monkeypatch, session_id, enabled, destination, expected,
):
    monkeypatch.setattr(page_capture, "_wait_for_page_settle", AsyncMock(return_value=True))
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            context = await browser.new_context(viewport={"width": 390, "height": 844})

            async def fulfill(route):
                videos = route.request.url.endswith("/videos")
                body = (
                    '<main><h1>视频栏目</h1><video controls></video><button>播放</button></main>'
                    if videos else
                    '<main><h1>帖子栏目</h1><article><p>帖子正文</p></article></main>'
                )
                await route.fulfill(content_type="text/html", body=body)

            await context.route("https://fixture.test/**", fulfill)
            page = await context.new_page()
            await page.goto("https://fixture.test/posts")
            collector = PlaywrightPageCaptureBrowser(logical_surface_enabled=enabled)
            result = await collector._capture_on_page_impl(
                request=PageCaptureRequest(
                    mode=PageCaptureMode.GOTO,
                    url=f"https://fixture.test{destination}",
                    session_id=session_id,
                    capture_screenshot=False,
                ),
                context=context,
                page=page,
                console_logs=[],
                network_requests=[],
                timeout_error_type=PlaywrightTimeoutError,
            )
            diff = _public_state_diff(result)
            assert _logical_navigation_postcondition_error(
                parameters={"logical_target": {"expects_logical_navigation": True}},
                state_diff=diff,
            ) == expected
            if session_id.startswith("h5:") and enabled:
                assert diff["before_location"] == "https://fixture.test/posts"
                assert diff["after_location"] == f"https://fixture.test{destination}"
                assert diff["new_page_count"] == 0
                assert result.action_trace[-1]["new_page_urls"] == []
                assert "logical_surface_transition" in diff
            else:
                assert "logical_surface_transition" not in diff
        finally:
            await browser.close()
