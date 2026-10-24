"""真实稀疏浏览器快照验证导航容器、CSS 隐藏和移动展开。"""

from contextlib import asynccontextmanager

import pytest

from services.api.app.application.web_desktop_navigation import extract_navigation_candidates
from services.api.app.infrastructure.browser.page_capture import _collect_ui_snapshot
from services.api.tests.web_browser_test_support import (
    WebBrowserUnavailable,
    launch_web_test_browser,
)


@asynccontextmanager
async def _real_navigation_page(html):
    from playwright.async_api import async_playwright

    async with async_playwright() as playwright:
        try:
            browser = await launch_web_test_browser(playwright.chromium, headless=True)
        except WebBrowserUnavailable as exc:
            pytest.skip(str(exc))
        try:
            page = await browser.new_page(viewport={"width": 1280, "height": 900})
            await page.route(
                "https://navigation-fixture.test/**",
                lambda route: route.fulfill(
                    status=200, content_type="text/html; charset=utf-8", body=html
                ),
            )
            await page.goto("https://navigation-fixture.test/")
            yield page
        finally:
            await browser.close()


async def _actual_candidates(page):
    snapshot = await _collect_ui_snapshot(page, web_detail_candidate_priority=True)
    candidates = extract_navigation_candidates(
        await page.content(), base_url=page.url, snapshot=snapshot, include_blocked=True
    )
    return snapshot, candidates


@pytest.mark.asyncio
@pytest.mark.parametrize("hidden", ["hidden", 'class="closed"'])
@pytest.mark.parametrize("hidden_first", [True, False])
async def test_actual_duplicate_navigation_never_borrows_other_nodes_visibility(
    hidden, hidden_first
):
    links = '<a href="/a">Alpha</a><a href="/b">Beta</a>'
    groups = [f"<nav {hidden}>{links}</nav>", f"<nav>{links}</nav>"]
    if not hidden_first:
        groups.reverse()
    async with _real_navigation_page(
        "<style>.closed{display:none}</style>" + "".join(groups)
    ) as page:
        snapshot, candidates = await _actual_candidates(page)
        assert sum(item["tag"] == "a" for item in snapshot["elements"]) == 2
        assert [(item.label, item.visibility, item.actionability) for item in candidates] == [
            ("Alpha", "visible", "actionable"),
            ("Beta", "visible", "actionable"),
        ]
        assert len({item.navigation_container_id for item in candidates}) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("attribute", ["data-route", "data-href", "data-url", "onclick"])
async def test_actual_route_buttons_keep_targets(attribute):
    controls = []
    for path, label in (("a", "Alpha"), ("b", "Beta")):
        value = f"location.href='/{path}'" if attribute == "onclick" else f"/{path}"
        controls.append(f'<button {attribute}="{value}">{label}</button>')
    async with _real_navigation_page("<nav>" + "".join(controls) + "</nav>") as page:
        snapshot, candidates = await _actual_candidates(page)
        assert sum(item["tag"] == "button" for item in snapshot["elements"]) == 2
        assert [(item.label, item.url, item.actionability) for item in candidates] == [
            ("Alpha", "https://navigation-fixture.test/a", "actionable"),
            ("Beta", "https://navigation-fixture.test/b", "actionable"),
        ]


@pytest.mark.asyncio
@pytest.mark.parametrize("inner", ["Alpha", "Visible", ""])
async def test_actual_accessible_label_uses_visible_text_then_aria_fallback(inner):
    html = (
        f'<nav><a href="/a" aria-label="Alpha">{inner}</a>'
        '<a href="/b" aria-label="Beta">Beta</a></nav>'
    )
    async with _real_navigation_page(html) as page:
        # 空文字链接仍占可见区域，避免把布局零面积误作名称问题。
        await page.add_style_tag(content="a{display:inline-block;width:100px;height:30px}")
        snapshot, candidates = await _actual_candidates(page)
        assert [item["text"] for item in snapshot["elements"] if item["tag"] == "a"] == [
            inner or "Alpha",
            "Beta",
        ]
        assert [(item.label, item.actionability) for item in candidates] == [
            (inner or "Alpha", "actionable"),
            ("Beta", "actionable"),
        ]


@pytest.mark.asyncio
@pytest.mark.parametrize("disabled", ["disabled", 'aria-disabled="true"', "inert"])
@pytest.mark.parametrize("all_disabled", [False, True])
async def test_actual_disabled_controls_never_prove_or_execute(disabled, all_disabled):
    html = (
        f"<nav><button {disabled}>Alpha</button>"
        f"<button {disabled if all_disabled else ''}>Beta</button></nav>"
    )
    async with _real_navigation_page(html) as page:
        snapshot, candidates = await _actual_candidates(page)
        assert sum(item["tag"] == "button" for item in snapshot["elements"]) == 2
        assert len(candidates) == 2
        assert all(item.actionability == "blocked" for item in candidates)
        assert "WEB_MENU_DISABLED_BLOCKED" in candidates[0].navigation_evidence
        assert all(
            "WEB_NAVIGATION_CONTAINER_UNPROVEN" in item.navigation_evidence for item in candidates
        )


@pytest.mark.asyncio
async def test_real_sparse_snapshot_css_visibility_and_mobile_expansion():
    from playwright.async_api import async_playwright

    html = """<style>.closed{display:none}</style><nav id="mobile">
    <button id="toggle" aria-expanded="false" onclick="
    document.querySelector('#menu').classList.remove('closed');
    this.setAttribute('aria-expanded','true')">分类</button>
    <div id="menu" class="closed"><a href="/a">分类甲</a><a href="/b">分类乙</a></div>
    <a class="logo" href="/">站标</a><a href="/login">登录</a>
    </nav><article class="card" data-route="/post/1">正文卡片</article>"""
    async with async_playwright() as playwright:
        try:
            browser = await launch_web_test_browser(playwright.chromium, headless=True)
        except WebBrowserUnavailable as exc:
            pytest.skip(str(exc))
        try:
            page = await browser.new_page(viewport={"width": 390, "height": 844})
            await page.route(
                "https://navigation-fixture.test/**",
                lambda route: route.fulfill(
                    status=200, content_type="text/html; charset=utf-8", body=html
                ),
            )
            await page.goto("https://navigation-fixture.test/")
            closed = await _collect_ui_snapshot(page, web_detail_candidate_priority=True)
            closed_candidates = extract_navigation_candidates(
                await page.content(), base_url=page.url, snapshot=closed
            )
            assert [item.label for item in closed_candidates] == ["分类", "分类甲", "分类乙"]
            await page.locator("#toggle").click()
            opened = await _collect_ui_snapshot(page, web_detail_candidate_priority=True)
            candidates = extract_navigation_candidates(
                await page.content(), base_url=page.url, snapshot=opened
            )
            assert [item.label for item in candidates] == ["分类", "分类甲", "分类乙"]
            assert all(item.navigation_container_id for item in candidates)
        finally:
            await browser.close()
