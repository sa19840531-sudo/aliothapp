"""Web 局部 CSS 兼容，不修改网页全局或 App/H5 默认采样。"""

import pytest

from services.api.app.infrastructure.browser.page_capture import _collect_ui_snapshot
from services.api.tests.web_browser_test_support import launch_web_test_browser


@pytest.mark.asyncio
@pytest.mark.parametrize('value', ['undefined', '42', 'null'])
async def test_web_snapshot_tolerates_non_callable_escape_without_global_patch(value):
    from playwright.async_api import async_playwright

    async with async_playwright() as playwright:
        browser = await launch_web_test_browser(playwright.chromium, headless=True)
        page = await browser.new_page()
        await page.set_content('<main><div id="1:test"><input id="-2 input">'
                               '<button id="中文按钮">搜索</button></div></main>')
        await page.evaluate(f'CSS.escape = {value}')
        before = await page.evaluate('typeof CSS.escape')
        snapshot = await _collect_ui_snapshot(page, web_detail_candidate_priority=True)
        assert not snapshot.get('collection_error')
        assert snapshot['element_count'] > 0
        assert await page.evaluate('typeof CSS.escape') == before
        for node in snapshot['elements']:
            if node.get('selector') and node.get('tag') in {'button','input'}:
                assert await page.locator(node['selector']).count() == 1
        default = await _collect_ui_snapshot(page)
        assert default.get('collection_error')
        assert 'web_native_structure' not in default
        await browser.close()
