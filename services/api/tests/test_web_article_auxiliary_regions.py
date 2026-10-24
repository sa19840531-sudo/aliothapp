"""前后篇导航与带时间的文章元信息独立留证，不能吞并正文或内容列表。"""

import pytest

from services.api.app.application.web_page_classification import web_page_structure_facts
from services.api.app.infrastructure.browser.page_capture import _collect_ui_snapshot
from services.api.tests.web_browser_test_support import launch_web_test_browser


@pytest.mark.asyncio
@pytest.mark.parametrize("conflict", [False, True])
async def test_article_navigation_and_dated_metadata_are_not_primary_cards(conflict):
    from playwright.async_api import async_playwright

    body = '<div class="editormd-preview"><p>' + "正文。" * 100 + "</p></div>"
    html = (
        '<main><div class="detail-page"><h1>正文标题</h1>'
        '<div class="detail-date"><a href="/author">作者</a> • '
        "2026年09月19日</div>" + body + '</div><div class="pre-next-container">'
        '<a href="/one">上一篇：第一篇</a><a href="/two">下一篇：第二篇</a>'
        + (body if conflict else "")
        + "</div></main>"
    )
    async with async_playwright() as playwright:
        browser = await launch_web_test_browser(playwright.chromium, headless=True)
        try:
            page = await browser.new_page()
            await page.set_content(html)
            snapshot = await _collect_ui_snapshot(page, web_detail_candidate_priority=True)
            facts = web_page_structure_facts(
                snapshot, origin="https://example.test", current_url="https://example.test/detail"
            )
            assert facts.subject_region_facts.primary_article_body_count == (2 if conflict else 1)
            assert facts.subject_region_facts.repeated_primary_card_count == (2 if conflict else 0)
        finally:
            await browser.close()


@pytest.mark.asyncio
async def test_dated_independent_list_is_not_article_metadata():
    from playwright.async_api import async_playwright

    html = (
        "<main><h1>正文标题</h1><article><p>"
        + "正文。" * 100
        + "</p></article><section><h2>2026年09月19日更新</h2>"
        '<a href="/one">文章一</a><a href="/two">文章二</a></section></main>'
    )
    async with async_playwright() as playwright:
        browser = await launch_web_test_browser(playwright.chromium, headless=True)
        try:
            page = await browser.new_page()
            await page.set_content(html)
            default = await _collect_ui_snapshot(page)
            snapshot = await _collect_ui_snapshot(page, web_detail_candidate_priority=True)
            assert "web_native_structure" not in default
            assert snapshot["web_native_structure"]["auxiliary_regions"] == []
            assert (
                web_page_structure_facts(snapshot).subject_region_facts.repeated_primary_card_count
                == 2
            )
        finally:
            await browser.close()
