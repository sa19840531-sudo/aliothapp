"""真实 Chrome 验证站点覆盖 Element 后的 Web 文本证据与默认路径隔离。"""

import pytest
from playwright.async_api import async_playwright

from services.api.app.infrastructure.browser.page_capture import (
    PageCaptureMode,
    PageCaptureRequest,
    _collect_artifacts,
)
from services.api.tests.web_browser_test_support import (
    WebBrowserUnavailable,
    launch_web_test_browser,
)

_PAGE = """
<html><body style="margin:0;font:20px sans-serif">
  <main style="position:absolute;left:20px;top:20px">可见正文</main>
  <div style="position:absolute;left:20px;top:100px">遮挡正文</div>
  <div style="position:absolute;left:0;top:90px;width:400px;height:70px;
              z-index:2;background:white"></div>
  <span style="display:none">样式隐藏</span>
  <span aria-hidden="true">语义隐藏</span>
  <span inert>惰性区域</span>
  <div style="position:absolute;left:20px;top:2500px">视口之外</div>
</body></html>
"""


@pytest.mark.parametrize(
    ("web_capture_stage", "shadow_element", "expected"),
    [
        ("web_page_probe", True, "可见正文"),
        (None, True, ""),
        ("web_page_probe", False, "可见正文"),
        (None, False, "可见正文"),
    ],
)
async def test_element_global_collision_is_recovered_only_for_explicit_web_capture(
    web_capture_stage, shadow_element, expected,
):
    async with async_playwright() as playwright:
        try:
            browser = await launch_web_test_browser(playwright.chromium, headless=True)
        except WebBrowserUnavailable as exc:
            pytest.skip(str(exc))
        try:
            page = await browser.new_page(viewport={"width": 1920, "height": 1080})
            await page.set_content(_PAGE)
            if shadow_element:
                await page.evaluate("window.Element = function RaphaelElement() {}")
                assert await page.evaluate("document.body instanceof Element") is False
            assert "可见正文" in await page.inner_text("body")
            artifacts = await _collect_artifacts(
                page,
                PageCaptureRequest(
                    mode=PageCaptureMode.OBSERVE,
                    capture_dom=False,
                    capture_screenshot=False,
                    web_capture_stage=web_capture_stage,
                    is_mobile=False,
                ),
                include_ui_snapshot=False,
            )
            visible_text = next(
                artifact.content for artifact in artifacts
                if artifact.artifact_type == "visible_text"
            )
            assert visible_text == expected
        finally:
            await browser.close()
