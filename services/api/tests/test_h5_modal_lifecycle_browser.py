"""真实同结构广告链的生命周期边界：短暂隐藏可留痕，素材和层级变化不能冒充关闭。"""

from types import SimpleNamespace

import pytest
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright

from services.api.app.application.h5_adaptive_navigation import (
    adaptive_navigation_selector,
    discover_h5_adaptive_navigation_patterns,
)
from services.api.app.application.h5_modal_close import H5_MODAL_CLOSE_SELECTOR, h5_modal_close
from services.api.app.domain.exploration import CandidateAction
from services.api.app.domain.exploration.h5_structural_navigation import (
    H5_BOTTOM_NAVIGATION_FAMILY_ADAPTIVE,
    h5_bottom_navigation_action_parameters,
    h5_bottom_navigation_selector,
    h5_global_bottom_navigation_index,
)
from services.api.app.domain.interaction import InteractionOperation, InteractionRiskLevel
from services.api.app.infrastructure.browser.h5_modal_dismissal_target import signed_modal_target
from services.api.app.infrastructure.browser.page_capture import (
    PageCaptureMode,
    PageCaptureRequest,
    PageTriggerAction,
    _clear_remaining_obstructions,
    _collect_ui_snapshot,
    _observe_popup_dismissal_outcome,
    _run_single_trigger,
    _wait_for_popup_dismissal,
)


@pytest.mark.parametrize("mode", ["parent_hidden", "node_hidden", "media_only", "untrusted"])
async def test_reused_modal_requires_trusted_close_and_observed_hidden_lifecycle(mode):
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            page = await browser.new_page(viewport={"width": 390, "height": 844})
            await page.set_content("""
                <style>#portal{position:fixed;inset:0;background:#888;z-index:2000}
                #panel{position:absolute;left:49px;top:210px;width:292px;height:337px}
                img{width:281px;height:281px} button{width:44px;height:44px}</style>
                <main><h1>业务列表</h1></main>
                <div id="portal"><div id="panel" role="dialog">
                  <img><button type="button">关闭</button></div></div>
            """)
            await page.evaluate("""mode => {
                const panel = document.querySelector('#panel');
                const image = panel.querySelector('img');
                const asset = color => 'data:image/svg+xml,' + encodeURIComponent(
                    '<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10">'
                    + '<rect width="10" height="10" fill="'+color+'"/></svg>');
                image.src = asset('red');
                panel.querySelector('button').onclick = () => {
                    const hidden = mode === 'node_hidden' ? panel : panel.parentElement;
                    if (mode !== 'media_only') hidden.style.display = 'none';
                    setTimeout(() => {
                        image.src = asset('blue');
                        panel.style.zIndex = '2002';
                        hidden.style.display = '';
                    }, 25);
                };
            }""", mode)
            target = await signed_modal_target(page, SimpleNamespace(
                selector="#panel button", modal_selector="#panel",
                obstruction_signature="same-structure-fixture",
            ), prefer_safe_outside=False)
            assert target is not None
            if mode == "untrusted":
                await page.evaluate("document.querySelector('#panel button').click()")
            else:
                await page.locator("#panel button").click()
            # 在短暂隐藏之后才开始原有 100ms 轮询，必须依赖动作绑定的生命周期证明。
            await page.wait_for_timeout(80)
            expected = mode in {"parent_hidden", "node_hidden"}
            assert await _wait_for_popup_dismissal(
                page=page, popup_target=target, timeout_ms=1200,
                timeout_error_type=PlaywrightTimeoutError,
            ) is expected
            assert await _observe_popup_dismissal_outcome(page=page, popup_target=target) == (
                "next_popup_layer_visible" if expected else "original_layer_still_visible"
            )
            assert await page.locator("#panel").is_visible()
            assert await page.evaluate("window.__aliothModalObservers.size") == 0
        finally:
            await browser.close()


@pytest.mark.parametrize("asset_kind", ["data", "blob", "http"])
async def test_ten_reused_portal_layers_are_closed_once_each_with_fresh_proof(asset_kind):
    """真实浏览器观察的十层同结构链须逐层关闭，不扩大既有关闭上限。"""
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            context = await browser.new_context(viewport={"width": 390, "height": 844})
            page = await context.new_page()
            await page.route("https://fixture.test/**", lambda route: route.fulfill(
                content_type="text/html; charset=utf-8", body="<main><h1>个人中心</h1></main>",
            ))
            await page.route("https://fixture.test/assets/**", lambda route: route.fulfill(
                content_type="image/svg+xml", body=(
                    '<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10">'
                    '<rect width="10" height="10" fill="rgb('
                    + route.request.url.rsplit("/", 1)[-1].split(".")[0]
                    + ',0,0)"/></svg>'
                ),
            ))
            await page.goto("https://fixture.test/")
            await page.set_content("""
                <style>#portal{position:fixed;inset:0;background:#888;z-index:2000}
                #panel{position:absolute;left:49px;top:210px;width:292px;height:337px;
                  background:white} img{width:281px;height:281px}
                button{width:44px;height:44px}
                nav{position:fixed;bottom:8px;left:0;width:390px}
                nav ul{display:flex;padding:0;margin:0;list-style:none}
                nav a{display:block;width:65px;height:46px;font-size:12px}</style>
                <main><h1>业务列表</h1></main>
                <nav><ul><li><a href="/" aria-current="page">首页</a></li>
                  <li><a href="/video">视频</a></li><li><a href="/community">社区</a></li>
                  <li><a href="/recommend">推荐</a></li><li><a href="/activity">活动</a></li>
                  <li><a href="/profile">我的</a></li></ul></nav>
                <div id="portal"><div id="panel" role="dialog">
                  <img><button type="button">关闭</button></div></div>
            """)
            await page.evaluate("""assetKind => {
                const portal = document.querySelector('#portal');
                const panel = document.querySelector('#panel');
                const asset = step => {
                    const svg = '<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10">'
                        + '<rect width="10" height="10" fill="rgb('+step+',0,0)"/></svg>';
                    return assetKind === 'http' ? '/assets/'+step+'.svg'
                        : assetKind === 'blob' ? URL.createObjectURL(
                            new Blob([svg], {type:'image/svg+xml'}))
                        : 'data:image/svg+xml,' + encodeURIComponent(svg);
                };
                window.closes = 0;
                panel.querySelector('img').src = asset(0);
                panel.querySelector('button').onclick = () => {
                    window.closes += 1;
                    portal.style.display = 'none';
                    if (window.closes === 10) return;
                    setTimeout(() => {
                        panel.querySelector('img').src = asset(window.closes);
                        panel.style.zIndex = String(2000 + window.closes);
                        portal.style.display = '';
                    }, 25);
                };
            }""", asset_kind)
            proof = h5_modal_close(await _collect_ui_snapshot(page, surface_session_id="h5:portal"))
            assert proof is not None
            request = PageCaptureRequest(
                session_id="h5:portal", mode=PageCaptureMode.SINGLE_TRIGGER,
                trigger_action=PageTriggerAction.CLICK,
                selector=f"{H5_MODAL_CLOSE_SELECTOR}:{proof.obstruction_signature}",
                h5_popup_dismissal_strategy="safe_outside_then_close", capture_screenshot=True,
            )
            trace = []
            await _run_single_trigger(
                page=page, context=context, request=request, action_trace=trace,
                timeout_ms=3000, timeout_error_type=PlaywrightTimeoutError,
            )
            frames = []
            result = await _clear_remaining_obstructions(
                page=page, context=context, request=request, action_trace=trace,
                obstruction_frames=frames, timeout_ms=3000,
                timeout_error_type=PlaywrightTimeoutError,
            )
            assert result["dismissal_count"] == 10
            assert result["remaining_obstruction"] is False
            assert await page.evaluate("window.closes") == 10
            assert [frame["index"] for frame in frames] == list(range(1, 10))
            assert all(frame["encoding"] == "base64" and frame["data"] for frame in frames)
            assert len({frame["data"] for frame in frames}) == 9
            closes = [item for item in trace if item.get("dismissal_kind") == "close_control"]
            assert len(closes) == 10
            assert not await page.locator("#panel").is_visible()
            assert await page.evaluate("window.__aliothModalObservers.size") == 0
            snapshot = await _collect_ui_snapshot(page, surface_session_id="h5:portal")
            patterns = discover_h5_adaptive_navigation_patterns(snapshot)
            assert len(patterns) == 1
            signature = patterns[0].signature
            candidate = CandidateAction(
                candidate_id="post-clearance-profile",
                semantic_target=h5_bottom_navigation_selector(
                    family=H5_BOTTOM_NAVIGATION_FAMILY_ADAPTIVE, index=6,
                ),
                action_type=InteractionOperation.CLICK, risk_level=InteractionRiskLevel.LOW,
                exploration_value=25, navigation_depth=1,
                metadata={
                    "action_parameters": h5_bottom_navigation_action_parameters(
                        family=H5_BOTTOM_NAVIGATION_FAMILY_ADAPTIVE, index=6, label="我的",
                        interaction_pattern_signature=signature,
                    ),
                    "target_keywords": ["bottom_navigation", "我的"],
                    "navigation_knowledge_source": "static",
                    "navigation_knowledge_confidence": 0.95,
                    "expected_next_page_type": "profile",
                },
            )
            assert h5_global_bottom_navigation_index(candidate) == 6
            selector = adaptive_navigation_selector(
                snapshot, pattern_signature=signature, navigation_index=6,
                navigation_label="我的",
            )
            assert selector is not None
            await page.locator(selector).click()
            assert page.url == "https://fixture.test/profile"
            assert await page.locator("h1").inner_text() == "个人中心"
        finally:
            await browser.close()
