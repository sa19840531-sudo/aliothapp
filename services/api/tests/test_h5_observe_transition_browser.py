"""真实浏览器观察必须留下同页证明，不能虚构切页或影响桌面 Web。"""

from unittest.mock import AsyncMock

import pytest
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright

from services.api.app.agents.tools.page_detection_tools import _bind_logical_transition_to_surface
from services.api.app.domain.exploration.h5_logical_surface import bind_h5_page_type
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


@pytest.mark.parametrize("session_id,enabled", [("h5:observe", True), ("web:observe", True),
                                               ("h5:observe", False)])
async def test_observe_retains_real_same_page_proof(monkeypatch, session_id, enabled):
    monkeypatch.setattr(page_capture, "_wait_for_page_settle", AsyncMock(return_value=True))
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            context = await browser.new_context(viewport={"width": 390, "height": 844})
            await context.route("https://fixture.test/**", lambda route: route.fulfill(
                content_type="text/html",
                body='<main><h1>栏目</h1><button>下一页</button></main>',
            ))
            page = await context.new_page()
            await page.goto("https://fixture.test/home")
            collector = PlaywrightPageCaptureBrowser(logical_surface_enabled=enabled)
            result = await collector._capture_on_page_impl(
                request=PageCaptureRequest(
                    mode=PageCaptureMode.OBSERVE, session_id=session_id,
                    logical_surface_enabled=enabled, capture_screenshot=False,
                ),
                context=context, page=page, console_logs=[], network_requests=[],
                timeout_error_type=PlaywrightTimeoutError,
            )
            diff = _public_state_diff(result)
            if not session_id.startswith("h5:") or not enabled:
                assert "logical_surface_transition" not in diff
                return
            transition = diff["logical_surface_transition"]
            assert transition["logical_transition_proof"] == "trusted"
            assert transition["transition_kind"] == "unchanged"
            assert _logical_navigation_postcondition_error(
                parameters={"logical_target": {"expects_logical_navigation": True}},
                state_diff=diff,
            ) == "LOGICAL_TRANSITION_UNVERIFIED"
            snapshot = next(a.content for a in result.artifacts if a.artifact_type == "ui_snapshot")
            bound = bind_h5_page_type(
                logical_surface=_bind_logical_transition_to_surface(
                    snapshot["logical_surface"], transition,
                ),
                page_type="content_list", detection_status="recognized",
                page_type_confidence=0.95, confidence_source="model",
            )
            assert bound["page_type_binding"]["rule_matching_allowed"] is True
        finally:
            await browser.close()
