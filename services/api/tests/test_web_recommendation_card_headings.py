"""推荐卡片自身的链接标题不能误作跨区标题；独立正文仍构成边界。"""

import pytest

from services.api.app.application.web_page_classification import web_page_structure_facts
from services.api.app.infrastructure.browser.page_capture import _collect_ui_snapshot
from services.api.tests.web_browser_test_support import launch_web_test_browser


@pytest.mark.asyncio
@pytest.mark.parametrize("conflict", ["none", "body", "heading", "linked_heading"])
async def test_linked_card_headings_preserve_recommendation_boundary(conflict):
    from playwright.async_api import async_playwright

    body = '<div class="editormd-preview"><p>' + "正文内容。" * 70 + "</p></div>"
    extra = {
        "none": "",
        "body": body,
        "heading": "<h2>独立内容区</h2>",
        "linked_heading": '<a href="/category"><h2>独立分类列表</h2></a>',
    }[conflict]
    cards = "".join(
        f'<div><a href="https://example.test/item/{index}"><h3>内容{index}</h3></a></div>'
        for index in range(4)
    )
    html = "<main>" + body + "<section><h2>大家都在搜</h2>" + extra + cards + "</section></main>"
    async with async_playwright() as playwright:
        browser = await launch_web_test_browser(playwright.chromium, headless=True)
        try:
            page = await browser.new_page()
            await page.set_content(html)
            snapshot = await _collect_ui_snapshot(page, web_detail_candidate_priority=True)
            regions = snapshot["web_native_structure"]["secondary_regions"]
            assert bool(regions) is (conflict == "none")
            facts = web_page_structure_facts(snapshot)
            assert facts.article_body_count == (2 if conflict == "body" else 1)
            if conflict == "none":
                assert facts.repeated_primary_card_count == 0
        finally:
            await browser.close()
