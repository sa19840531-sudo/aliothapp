"""隔离浏览器验证真实命中和页面到达，不访问任务网站。"""

import pytest
from playwright.async_api import async_playwright

from services.api.app.agents.tools import page_detection_tools as tools
from services.api.app.application.h5_adaptive_navigation import (
    discover_h5_adaptive_navigation_patterns,
)
from services.api.app.application.h5_structural_navigation import (
    H5_BOTTOM_NAVIGATION_FAMILY_ADAPTIVE,
)
from services.api.app.domain.interaction import InteractionOperation
from services.api.app.infrastructure.browser.page_capture import _collect_ui_snapshot
from services.api.app.infrastructure.browser.semantic_target_resolver import (
    _select_structural_navigation_item,
)


@pytest.mark.parametrize("intercepted", [False, True])
async def test_non_dismissable_gate_does_not_authorize_intercepted_navigation(intercepted):
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            page = await browser.new_page(viewport={"width": 390, "height": 844})
            await page.route(
                "https://fixture.test/**",
                lambda route: route.fulfill(
                    content_type="text/html; charset=utf-8",
                    body="<main>正常业务页</main>",
                ),
            )
            await page.goto("https://fixture.test/")
            await page.set_content("""
                <style>body{margin:0}nav{position:fixed;bottom:8px;left:0;width:390px;z-index:20}
                nav ul{display:flex;padding:0;margin:0;list-style:none}
                nav a{display:block;width:65px;height:60px}
                #gate{position:fixed;inset:0 0 100px;background:#eee;z-index:10}</style>
                <main><h1>视频列表</h1></main><div id="gate" role="dialog">会员内容不可查看</div>
                <nav><ul><li><a href="/" aria-current="page">首页</a></li>
                <li><a href="/videos">视频</a></li><li><a href="/community">社区</a></li>
                <li><a href="/recommendations">推荐</a></li><li><a href="/events">活动</a></li>
                <li><a href="/profile">我的</a></li></ul></nav>
            """)
            if intercepted:
                await page.locator("#gate").evaluate(
                    "element => {element.style.inset='0';element.style.zIndex='30'}",
                )
            snapshot = await _collect_ui_snapshot(page, surface_session_id="h5:fixture")
            assert snapshot["h5_modal_census_truncated"] is False
            assert snapshot["h5_modal_census_selectors"]
            actions = tools._with_independent_h5_gate_navigation(
                page_type="video_list",
                page_state="popup",
                candidate_actions=[],
                patterns=discover_h5_adaptive_navigation_patterns(snapshot),
                pattern_statuses={},
                nav_alias_matches={},
                ui_snapshot=snapshot,
                authentication_required=False,
            )
            if intercepted:
                assert actions == []
            else:
                action = next(item for item in actions if "我的" in item.target_keywords)
                selector = _select_structural_navigation_item(
                    snapshot,
                    operation=InteractionOperation.CLICK,
                    target_mode="bottom_navigation",
                    navigation_family=H5_BOTTOM_NAVIGATION_FAMILY_ADAPTIVE,
                    navigation_index=6,
                    navigation_label="我的",
                    interaction_pattern_signature=action.interaction_pattern_signature,
                )
                assert await page.locator("#gate").is_visible()
                await page.locator(selector).click()
                assert page.url == "https://fixture.test/profile"
                assert await page.locator("main").inner_text() == "正常业务页"
        finally:
            await browser.close()


@pytest.mark.parametrize(
    "label, expected", [("设置", "settings"), ("会员中心", "payment"), ("播放视频", "video_detail")]
)
async def test_business_link_arrives_at_real_document_without_type_promotion(label, expected):
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            page = await browser.new_page(viewport={"width": 390, "height": 844})
            await page.route(
                "https://fixture.test/**",
                lambda route: route.fulfill(
                    content_type="text/html",
                    body=f"<main><h1>{expected}</h1></main>",
                ),
            )
            await page.goto("https://fixture.test/")
            await page.set_content(f'<main>首页</main><a href="/account/view">{label}</a>')
            snapshot = await _collect_ui_snapshot(page, surface_session_id="h5:fixture")
            actions = tools._with_h5_verified_business_links(
                page_type="home",
                page_state="stable",
                candidate_actions=[],
                page_url=page.url,
                ui_snapshot=snapshot,
            )
            assert len(actions) == 1
            assert actions[0].expected_next_page_type == expected
            await page.goto(actions[0].destination_url)
            assert await page.locator("h1").inner_text() == expected
        finally:
            await browser.close()
