"""真实浏览器稀疏 Snapshot 到页面签发的正文/卡片范围回归。"""

import pytest

from services.api.app.application.web_page_classification import web_page_structure_facts
from services.api.app.infrastructure.browser.page_capture import _collect_ui_snapshot
from services.api.tests.test_web_page_recognition_rules import classify
from services.api.tests.web_browser_test_support import (
    WebBrowserUnavailable,
    launch_web_test_browser,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["body", "cards", "body_and_cards"])
async def test_actual_sparse_capture_preserves_body_and_excludes_plain_div_cards(kind):
    from playwright.async_api import async_playwright

    prose = (
        '<div id="wrapper"><div id="prose">'
        + "".join(
            f"<p {attribute}>" + "文" * 200 + "</p>"
            for attribute in ('id="paragraph-0"', 'data-testid="paragraph-1"', 'name="paragraph-2"')
        )
        + "</div></div>"
    )
    cards = "".join(
        f'<div><a href="/entry/{i}">推荐文章</a><p>' + "摘" * 200 + "</p></div>" for i in range(2)
    )
    html = (
        '<main id="body">'
        + (prose if kind != "cards" else "")
        + (cards if kind != "body" else "")
        + "</main>"
    )
    async with async_playwright() as playwright:
        try:
            browser = await launch_web_test_browser(playwright.chromium, headless=True)
        except WebBrowserUnavailable as exc:
            pytest.skip(str(exc))
        try:
            page = await browser.new_page(viewport={"width": 1280, "height": 900})
            await page.route(
                "https://body-fixture.test/**",
                lambda route: route.fulfill(
                    status=200, content_type="text/html; charset=utf-8", body=html
                ),
            )
            await page.goto("https://body-fixture.test/category")
            snapshot = await _collect_ui_snapshot(page, web_detail_candidate_priority=True)
            selectors = {item["selector"] for item in snapshot["elements"]}
            assert "#wrapper" not in selectors
            assert "#card-0" not in selectors
            assert not any(item["tag"] == "div" for item in snapshot["elements"])
            assert all(item["web_ancestor_structure_complete"] for item in snapshot["elements"])
            facts = web_page_structure_facts(
                snapshot,
                origin="https://body-fixture.test",
                current_url="https://body-fixture.test/category",
            )
            assert facts.paragraph_text_length == (0 if kind == "cards" else 600)
            result, _ = await classify("content_detail", facts)
            # 重复卡片与正文同屏不证明唯一详情主体；保留真实正文但拒绝误分类。
            assert result.page_type == ("content_detail" if kind == "body" else "unknown")
            if kind != "body":
                assert result.needs_review_reason
        finally:
            await browser.close()


def test_ancestor_metadata_is_bounded_normalized_and_marks_incomplete_chain():
    from services.api.app.infrastructure.browser.page_capture import _sanitize_ui_element

    element = _sanitize_ui_element(
        {
            "selector": "#p",
            "web_ancestor_structure_complete": True,
            "web_ancestor_structure": [
                {
                    "selector": f"#parent-{i}",
                    "tag": "div",
                    "repeat_group_size": 2,
                    "untrusted_extra": "discard",
                }
                for i in range(40)
            ],
        },
        resource_summaries=[],
    )
    assert len(element["web_ancestor_structure"]) == 32
    assert set(element["web_ancestor_structure"][0]) == {"selector", "tag", "repeat_group_size"}
    assert element["web_ancestor_structure_complete"] is False
