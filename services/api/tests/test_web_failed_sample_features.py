"""失败样本归纳的同帧 Web 特征与隔离回归。"""

import pytest

from services.api.app.application.web_page_classification import web_page_structure_facts
from services.api.app.application.web_search_structure_facts import web_search_structure_facts
from services.api.app.infrastructure.browser.page_capture import _collect_ui_snapshot
from services.api.tests.web_browser_test_support import launch_web_test_browser


@pytest.mark.asyncio
@pytest.mark.parametrize('advertising', [True, False])
async def test_body_counter_excludes_explicit_ad_links_but_retains_second_body(advertising):
    from playwright.async_api import async_playwright

    markers = 'data-event="ad_click" data-ad-slot="footer"' if advertising else ''
    html = '<article><p>' + '独立正文。' * 60 + '</p></article>'
    html += '<div class="post-content"><h3>附加区域</h3>' + ''.join(
        f'<a href="https://example.test/{i}" {markers}>另一区域文字{i}。</a>'
        for i in range(30)) + '</div>'
    async with async_playwright() as playwright:
        browser = await launch_web_test_browser(playwright.chromium, headless=True)
        try:
            page = await browser.new_page()
            await page.set_content(html)
            snapshot = await _collect_ui_snapshot(page, web_detail_candidate_priority=True)
            facts = web_page_structure_facts(snapshot).subject_region_facts
            assert facts.primary_article_body_count == (1 if advertising else 2)
        finally:
            await browser.close()


@pytest.mark.parametrize("change", [
    {"complete": False}, {"document_time_origin_ms": 2},
    {"search_trigger_count": True}, {"search_input_count": 0},
    {"schema_version": "untrusted"}, {"search_input_count": 1001},
])
def test_reject_incomplete_cross_document_or_malformed_search_census(change):
    snapshot = {
        "web_detail_candidate_priority_enabled": True,
        "document": {"time_origin_ms": 1},
        "web_native_structure": {"document_time_origin_ms": 1, "search_structure": {
            "schema_version": "web-search-structure.v1", "complete": True,
            "document_time_origin_ms": 1, "search_input_count": 1,
            "search_trigger_count": 1, **change,
        }},
    }
    assert web_search_structure_facts(snapshot) == {}


@pytest.mark.asyncio
async def test_same_frame_search_survives_node_budget_without_changing_default_capture():
    from playwright.async_api import async_playwright

    html = '<nav>' + ''.join(f'<a href="/{i}">入口{i}</a>' for i in range(400)) + '</nav>'
    html += ('<main><div class="search-box"><input type="text">'
             '<button type="button">搜索</button></div></main>')
    async with async_playwright() as playwright:
        browser = await launch_web_test_browser(playwright.chromium, headless=True)
        page = await browser.new_page()
        await page.set_content(html)
        default = await _collect_ui_snapshot(page)
        snapshot = await _collect_ui_snapshot(page, web_detail_candidate_priority=True)
        await browser.close()
    assert "web_native_structure" not in default
    facts = web_page_structure_facts(snapshot)
    assert facts.search_input_count == facts.search_trigger_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('wrapper,expected', [
    ('<main>{}</main>', 1), ('<div role="main">{}</div>', 1),
    ('<header>{}</header>', 0), ('<main><nav>{}</nav></main>', 0),
    ('<main><div hidden>{}</div></main>', 0),
    ('<main><div class="advertisement">{}</div></main>', 0),
])
async def test_bem_search_bar_counts_paired_controls_only_in_primary_region(wrapper, expected):
    from playwright.async_api import async_playwright

    html = wrapper.format('<div class="search-page__bar"><div class="search-page__bar-inner">'
        '<input type="text"><button type="button" class="search-page__submit">'
        '<svg></svg></button></div></div>')
    async with async_playwright() as playwright:
        browser = await launch_web_test_browser(playwright.chromium, headless=True)
        try:
            page = await browser.new_page()
            await page.set_content(html)
            snapshot = await _collect_ui_snapshot(page, web_detail_candidate_priority=True)
            facts = web_search_structure_facts(snapshot)
            assert facts['search_input_count'] == expected
            assert facts['search_trigger_count'] == expected
        finally:
            await browser.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("wrapper", [
    '<header>{}</header>', '<nav>{}</nav>', '<aside>{}</aside>',
    '<main hidden>{}</main>', '<div role="banner">{}</div>',
    '<div class="advertisement">{}</div>',
])
async def test_search_census_excludes_global_secondary_hidden_or_ad_controls(wrapper):
    from playwright.async_api import async_playwright

    html = wrapper.format('<div class="search-box"><input><button>搜索</button></div>')
    async with async_playwright() as playwright:
        browser = await launch_web_test_browser(playwright.chromium, headless=True)
        page = await browser.new_page()
        await page.set_content(html)
        snapshot = await _collect_ui_snapshot(page, web_detail_candidate_priority=True)
        await browser.close()
    assert web_search_structure_facts(snapshot) == {
        "search_input_count": 0, "search_trigger_count": 0,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("unsafe", [False, True])
async def test_nested_traditional_recommendation_heading_does_not_absorb_article(unsafe):
    from playwright.async_api import async_playwright

    article = '<article><p>' + '正文' * 200 + '</p></article>'
    links = ''.join(f'<a href="https://example.test/item/{i}">卡片{i}</a>' for i in range(5))
    section = '<section><div><div><h2>猜你喜歡</h2></div></div>'
    html = (
        ('' if unsafe else article) + section + (article if unsafe else '') + links + '</section>'
    )
    async with async_playwright() as playwright:
        browser = await launch_web_test_browser(playwright.chromium, headless=True)
        page = await browser.new_page()
        await page.set_content(html)
        snapshot = await _collect_ui_snapshot(page, web_detail_candidate_priority=True)
        await browser.close()
    secondary = snapshot['web_native_structure']['secondary_regions']
    assert bool(secondary) is not unsafe
    facts = web_page_structure_facts(snapshot)
    assert facts.subject_region_facts.primary_subject_count == 1
    if not unsafe:
        assert facts.subject_region_facts.repeated_primary_card_count == 0
    else:
        assert facts.subject_region_facts.repeated_primary_card_count >= 2


@pytest.mark.asyncio
@pytest.mark.parametrize('body', [True, False])
async def test_markdown_preview_body_and_contextual_recommendations_are_separate(body):
    from playwright.async_api import async_playwright

    html = '<main><h1>页面标题</h1>'
    if body:
        html += '<div class="editormd-preview"><p>' + '独立正文内容。'*60 + '</p></div>'
    html += '<section><div><h2>大家都在搜</h2></div>'
    html += ''.join(f'<div><a href="https://example.com/item/{i}">条目 {i}</a></div>'
                    for i in range(4)) + '</section></main>'
    async with async_playwright() as playwright:
        browser = await launch_web_test_browser(playwright.chromium, headless=True)
        page = await browser.new_page()
        await page.set_content(html)
        snapshot = await _collect_ui_snapshot(page,web_detail_candidate_priority=True)
        await browser.close()
    assert not snapshot.get('collection_error')
    assert bool(snapshot['web_native_structure']['secondary_regions']) is body
    facts = web_page_structure_facts(snapshot)
    if body:
        assert facts.subject_region_facts.primary_subject_count == 1
        assert facts.subject_region_facts.primary_article_text_length >= 120
        assert facts.subject_region_facts.repeated_primary_card_count == 0
