"""本地深层DOM复现真实8层selector别名；不访问原站或强制点击。"""

import pytest
from playwright.async_api import async_playwright

from services.api.app.infrastructure.browser.page_capture import (
    _collect_media_state,
    _collect_ui_snapshot,
)


@pytest.mark.parametrize("duplicate_attribute", [None, "id", "data-testid", "name"])
async def test_deep_tab_selector_uniquely_binds_original_node(duplicate_attribute):
    attribute = f'{duplicate_attribute}="same"' if duplicate_attribute else ""
    target = (
        f'<div role="tab" {attribute} style="width:80px;height:40px">最新上架</div>'
        f'<video {attribute} width="200" height="100"></video>'
    )
    body = "".join(
        f'<section id="root-{index}">' + "<div>" * 10 + target + "</div>" * 10 + "</section>"
        for index in range(2)
    )
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            page = await browser.new_page(viewport={"width": 390, "height": 844})
            await page.route("**/*", lambda route: route.abort())
            await page.set_content(body)
            snapshot = await _collect_ui_snapshot(page, surface_session_id="h5:offline")
            tabs = [item for item in snapshot["elements"] if item.get("role") == "tab"]
            assert len(tabs) == 2
            assert len({item["selector"] for item in tabs}) == 2
            for item in tabs:
                locator = page.locator(item["selector"])
                assert await locator.count() == 1
                assert await locator.get_attribute("role") == "tab"
                assert await locator.inner_text() == item["text"]
                assert item["dom_sibling_index"] == 1
                await locator.click(timeout=1000)
            media = await _collect_media_state(page)
            assert len(media["items"]) == 2
            assert len({item["selector"] for item in media["items"]}) == 2
            for item in media["items"]:
                locator = page.locator(item["selector"])
                assert await locator.count() == 1
                assert await locator.evaluate("element => element.tagName") == "VIDEO"
        finally:
            await browser.close()
