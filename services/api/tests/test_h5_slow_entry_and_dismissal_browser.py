"""慢启动与无站点映射的弹窗关闭必须通过真实浏览器验证。"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright

from services.api.app.agents.tools.page_detection_tools import _with_obstruction_dismissal_candidate
from services.api.app.application.h5_modal_acknowledgement import (
    H5_MODAL_LOCAL_SAFE_ACKNOWLEDGEMENT_SELECTOR,
    h5_modal_local_safe_acknowledgement,
)
from services.api.app.application.h5_modal_close import (
    H5_MODAL_CLOSE_CAPABILITY,
    H5_MODAL_CLOSE_SELECTOR,
    h5_modal_close,
)
from services.api.app.application.interaction_runtime import SemanticTargetResolutionRequest
from services.api.app.domain.interaction import InteractionOperation
from services.api.app.infrastructure.browser import page_capture as module
from services.api.app.infrastructure.browser.semantic_target_resolver import (
    UiSnapshotSemanticTargetResolver,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("control", [
    '<div id="exit">我知道了</div>',
    '<div id="exit" class="popup-close"><img alt="" width="30" height="30"></div>',
    '<button id="exit">x</button>',
    '<div id="exit">忽略</div>',
    '<div id="exit" style="width:300px">我知道了</div>',
    '<div id="exit" style="width:300px"><span>我知道了</span></div>',
    '<div id="exit" class="i-ic:outline-cancel" style="width:36px;height:36px"></div>',
])
async def test_delegated_modal_control_survives_large_background_and_closes(control, monkeypatch):
    monkeypatch.setattr(module, "_wait_for_page_settle", AsyncMock(return_value=True))
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            page = await browser.new_page(viewport={"width": 390, "height": 844})
            background = "<a href='/content'>产品内容</a>" * 500
            await page.set_content(f"""
                <style>body{{margin:0}} #shell{{position:fixed;inset:0;z-index:999}}
                  #panel{{position:absolute;left:40px;top:180px;width:310px;height:420px;
                    background:white}} #exit{{width:100px;height:40px}}</style>
                <main><input placeholder="搜索"><a href="/login">登录</a>{background}</main>
                <div id="shell"><div id="panel" role="dialog" aria-modal="true">
                  <p>活动公告，内容更新</p>{control}</div></div>
                <script>document.querySelector('#exit').addEventListener('click', () =>
                  document.querySelector('#shell').remove());</script>
            """)
            snapshot = await module._collect_ui_snapshot(page, surface_session_id="h5:fixture")
            close = h5_modal_close(snapshot)
            ack = h5_modal_local_safe_acknowledgement(snapshot)
            proof = close or ack
            assert proof is not None
            candidates = _with_obstruction_dismissal_candidate(
                page_type="home", page_state="stable", candidate_actions=[],
                dom_chunks=[await page.content()], ui_snapshot=snapshot,
            )
            assert len(candidates) == 1
            assert candidates[0].obstruction_signature == proof.obstruction_signature
            prefix = (
                H5_MODAL_CLOSE_SELECTOR if close else H5_MODAL_LOCAL_SAFE_ACKNOWLEDGEMENT_SELECTOR
            )
            result = await module.PlaywrightPageCaptureBrowser()._capture_on_page(
                request=module.PageCaptureRequest(
                    session_id="h5:fixture", mode=module.PageCaptureMode.SINGLE_TRIGGER,
                    trigger_action=module.PageTriggerAction.CLICK,
                    selector=f"{prefix}:{proof.obstruction_signature}",
                    clear_obstructions=True, allow_high_z_obstruction_hide=True,
                    h5_popup_dismissal_strategy="safe_outside_then_close",
                    allow_h5_popup_layer_continuation=True,
                ), page=page, context=page.context, console_logs=[], network_requests=[],
                timeout_error_type=PlaywrightTimeoutError,
            )
            assert await page.locator("#shell").count() == 0
            assert any(item.get("dismissal_verified") is True for item in result.action_trace)
        finally:
            await browser.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("closable", [True, False])
async def test_arbitrary_backdrop_without_close_button_has_verified_native_outcome(closable):
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            page = await browser.new_page(viewport={"width": 390, "height": 844})
            await page.set_content("""
                <style>body{margin:0} #screen{position:fixed;inset:0;z-index:800}
                  #notice{position:absolute;left:40px;top:180px;width:310px;height:400px}</style>
                <main><a href='/next'>下一页</a></main>
                <div id="screen"><div id="notice" role="dialog"><p>活动公告</p></div></div>
            """)
            if closable:
                await page.evaluate("document.querySelector('#screen').addEventListener('click',"
                                    "e => {if(e.target.id==='screen')e.target.remove()})")
            snapshot = await module._collect_ui_snapshot(
                page, surface_session_id="h5:fixture", logical_surface_enabled=True,
                logical_surface_shadow=False,
            )
            proof = h5_modal_close(snapshot)
            assert proof is not None and proof.outside_position is not None
            surface = snapshot["logical_surface"]
            resolver = UiSnapshotSemanticTargetResolver(
                snapshot_reader=SimpleNamespace(read_ui_snapshot=AsyncMock(return_value=snapshot)),
                logical_targets_enabled=True,
            )
            resolved = await resolver.resolve(SemanticTargetResolutionRequest(
                task_id="fixture", session_id="h5:fixture", source_capture_id="capture",
                action_type=InteractionOperation.CLICK, semantic_target="关闭当前弹窗",
                parameters={
                    "target_mode": "popup_dismissal",
                    "popup_dismissal_capability": H5_MODAL_CLOSE_CAPABILITY,
                    "obstruction_signature": proof.obstruction_signature,
                    "logical_target": {
                        "protocol_version": "logical_target.v1",
                        "source_identity_fingerprint": surface["identity_fingerprint"],
                        "source_state_fingerprint": surface["state_fingerprint"],
                        "expects_logical_navigation": True,
                    },
                },
            ))
            assert resolved.target_trust == "trusted"
            target = await module._resolve_popup_dismissal_target(
                page=page, selector=f"{H5_MODAL_CLOSE_SELECTOR}:{proof.obstruction_signature}",
                prefer_safe_outside=True,
            )
            assert target["kind"] == "overlay"
            await page.locator(target["selector"]).click(position=target["position"])
            verified = await module._wait_for_popup_dismissal(
                page=page, popup_target=target, timeout_ms=1000,
                timeout_error_type=PlaywrightTimeoutError,
            )
            assert verified is closable
        finally:
            await browser.close()


@pytest.mark.asyncio
async def test_replacement_modal_reusing_same_selector_does_not_keep_old_modal_alive():
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            page = await browser.new_page(viewport={"width": 390, "height": 844})
            await page.set_content("""
                <div id="modal" role="dialog" style="position:fixed;inset:0;z-index:900">
                  <button type="button" id="exit">关闭</button></div>
                <script>document.querySelector('#exit').onclick = () => {
                  document.querySelector('#modal').outerHTML =
                    '<div id="modal" role="dialog">下一层公告</div>';
                }</script>
            """)
            snapshot = await module._collect_ui_snapshot(page, surface_session_id="h5:fixture")
            proof = h5_modal_close(snapshot)
            assert proof is not None
            target = await module._resolve_popup_dismissal_target(
                page=page, selector=f"{H5_MODAL_CLOSE_SELECTOR}:{proof.obstruction_signature}",
            )
            await page.locator(target["selector"]).click()
            assert await module._wait_for_popup_dismissal(
                page=page, popup_target=target, timeout_ms=1500,
                timeout_error_type=PlaywrightTimeoutError,
            )
            assert await page.locator("#modal").inner_text() == "下一层公告"
        finally:
            await browser.close()


@pytest.mark.asyncio
async def test_signed_close_does_not_waste_ineffective_outside_attempts():
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            page = await browser.new_page(viewport={"width": 390, "height": 844})
            await page.set_content("""
                <style>body{margin:0} #mask{position:fixed;inset:0;z-index:800}
                  #panel{position:absolute;left:40px;top:180px;width:310px;height:400px}
                  #exit{width:100px;height:40px}</style>
                <main><h1>产品首页</h1></main>
                <div id="mask"><div id="panel" role="dialog"><p>公告</p>
                  <button id="exit" type="button">关闭</button></div></div>
                <script>document.querySelector('#exit').onclick = () =>
                  document.querySelector('#mask').remove();</script>
            """)
            proof = h5_modal_close(await module._collect_ui_snapshot(
                page, surface_session_id="h5:fixture",
            ))
            assert proof is not None
            trace = []
            await module._run_single_trigger(
                page=page, context=page.context,
                request=module.PageCaptureRequest(
                    session_id="h5:fixture", mode=module.PageCaptureMode.SINGLE_TRIGGER,
                    trigger_action=module.PageTriggerAction.CLICK,
                    selector=f"{H5_MODAL_CLOSE_SELECTOR}:{proof.obstruction_signature}",
                    h5_popup_dismissal_strategy="safe_outside_then_close",
                ), action_trace=trace, timeout_ms=6000, timeout_error_type=PlaywrightTimeoutError,
            )
            assert await page.locator("#mask").count() == 0
            assert "safe_outside_attempts" not in trace[-1]
            assert trace[-1]["selector"] == proof.selector
            assert trace[-1]["dismissal_kind"] == "close_control"
            assert trace[-1]["dismissal_verified"] is True
        finally:
            await browser.close()


@pytest.mark.asyncio
async def test_h5_chain_resigns_next_modal_instead_of_reusing_old_button_signature(monkeypatch):
    monkeypatch.setattr(module, "_wait_for_page_settle", AsyncMock(return_value=True))
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            page = await browser.new_page(viewport={"width": 390, "height": 844})
            await page.set_content("""
                <style>body{margin:0} #modal{position:fixed;inset:0;z-index:900}
                  #exit,#ack{width:120px;height:40px}
                  #tip{position:fixed;bottom:0;right:0;width:40px;height:30px}</style>
                <main><h1>产品首页</h1></main>
                <div id="tip" class="popup">提示</div>
                <div id="modal" role="dialog"><button id="exit" type="button">关闭</button></div>
                <script>document.querySelector('#exit').onclick = () => {
                  document.querySelector('#modal').outerHTML =
                    '<div id="modal" role="dialog"><p>下一层公告</p>'
                    + '<div id="ack">我知道了</div></div>';
                  document.querySelector('#ack').addEventListener('click', () =>
                    document.querySelector('#modal').remove());
                }</script>
            """)
            snapshot = await module._collect_ui_snapshot(page, surface_session_id="h5:fixture")
            proof = h5_modal_close(snapshot)
            assert proof is not None
            result = await module.PlaywrightPageCaptureBrowser()._capture_on_page(
                request=module.PageCaptureRequest(
                    session_id="h5:fixture", mode=module.PageCaptureMode.SINGLE_TRIGGER,
                    trigger_action=module.PageTriggerAction.CLICK,
                    selector=f"{H5_MODAL_CLOSE_SELECTOR}:{proof.obstruction_signature}",
                    clear_obstructions=True, allow_high_z_obstruction_hide=True,
                    h5_popup_dismissal_strategy="safe_outside_then_close",
                ), page=page, context=page.context, console_logs=[], network_requests=[],
                timeout_error_type=PlaywrightTimeoutError,
            )
            assert await page.locator("#modal").count() == 0
            assert sum(item.get("dismissal_verified") is True for item in result.action_trace) == 2
            assert result.metrics["obstruction_chain"]["remaining_obstruction"] is False
            assert await page.locator("#tip").is_visible()
            assert await module._visible_popup_remains(page, h5_session=True) is False
        finally:
            await browser.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy_target", [None, {"dialog_present": False}])
async def test_unrecognized_h5_modal_stays_blocking_after_legacy_lookup(monkeypatch, legacy_target):
    snapshot = {
        "viewport": {"width": 390, "height": 844},
        "h5_modal_census_selectors": ["#unfamiliar-layer"],
        "h5_modal_census_truncated": False,
        "elements": [{
            "selector": "#unfamiliar-layer", "tag": "section", "role": "dialog",
            "rect": {"x": 0, "y": 0, "width": 390, "height": 844},
            "style": {"display": "block", "visibility": "visible", "opacity": "1"},
            "hit_test": {"center_hits_target": True},
        }],
    }
    monkeypatch.setattr(module, "_collect_ui_snapshot", AsyncMock(return_value=snapshot))
    monkeypatch.setattr(module, "_resolve_popup_dismissal_target",
                        AsyncMock(return_value=legacy_target))
    target = await module._resolve_first_popup_dismissal_target(
        page=object(), selectors=("text=关闭",), h5_session=True,
    )
    assert target is not None and target["dialog_present"] is True
    assert target["kind"] == "unproven"
    assert await module._visible_popup_remains(object(), h5_session=True) is True


@pytest.mark.asyncio
async def test_h5_uncertain_or_blocking_snapshot_does_not_clear_obstruction(monkeypatch):
    legacy = AsyncMock(return_value={"dialog_present": True, "kind": "blocked"})
    monkeypatch.setattr(module, "_resolve_popup_dismissal_target", legacy)
    for snapshot in (
        {"elements": [], "h5_modal_census_truncated": True},
        {"elements": [{"selector": "#broken"}], "collection_error": "failed"},
        {"elements": [{"selector": "#dialog", "tag": "div", "role": "dialog",
                       "rect": {"width": 390, "height": 844}}],
         "viewport": {"width": 390, "height": 844},
         "h5_modal_census_selectors": ["#dialog"], "h5_modal_census_truncated": False},
    ):
        monkeypatch.setattr(module, "_collect_ui_snapshot", AsyncMock(return_value=snapshot))
        target = await module._resolve_first_popup_dismissal_target(
            page=object(), selectors=("text=关闭",), h5_session=True,
        )
        assert target["dialog_present"] is True


@pytest.mark.asyncio
async def test_reappearing_equivalent_modal_uses_signed_close_without_outside(monkeypatch):
    monkeypatch.setattr(module, "_wait_for_page_settle", AsyncMock(return_value=True))
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            page = await browser.new_page(viewport={"width": 390, "height": 844})
            await page.set_content("""
                <style>body{margin:0} #mask{position:fixed;inset:0;z-index:800}
                  #panel{position:absolute;left:40px;top:180px;width:310px;height:400px}
                  #exit{width:36px;height:36px}</style>
                <main><h1>产品首页</h1></main>
                <div id="mask"><div id="panel" role="dialog"><p>公告</p>
                  <div id="exit" class="i-ic:outline-cancel"></div></div></div>
                <script>
                  function wire() {
                    document.querySelector('#exit').onclick = () =>
                      document.querySelector('#mask').remove();
                    document.querySelector('#mask').onclick = e => {
                      if (e.target.id !== 'mask') return;
                      const old = document.querySelector('#mask');
                      old.outerHTML = old.outerHTML;
                      wire();
                    };
                  }
                  wire();
                </script>
            """)
            proof = h5_modal_close(await module._collect_ui_snapshot(
                page, surface_session_id="h5:fixture",
            ))
            assert proof is not None
            result = await module.PlaywrightPageCaptureBrowser()._capture_on_page(
                request=module.PageCaptureRequest(
                    session_id="h5:fixture", mode=module.PageCaptureMode.SINGLE_TRIGGER,
                    trigger_action=module.PageTriggerAction.CLICK,
                    selector=f"{H5_MODAL_CLOSE_SELECTOR}:{proof.obstruction_signature}",
                    clear_obstructions=True, allow_high_z_obstruction_hide=True,
                    h5_popup_dismissal_strategy="safe_outside_then_close",
                ), page=page, context=page.context, console_logs=[], network_requests=[],
                timeout_error_type=PlaywrightTimeoutError,
            )
            assert await page.locator("#mask").count() == 0
            # 唯一关闭键已签发时不先点会重绘原层的遮罩，仍只算一个关闭动作。
            assert result.metrics["obstruction_chain"]["dismissal_count"] == 1
            assert result.metrics["obstruction_chain"]["remaining_obstruction"] is False
            assert any(t.get("selector") == proof.selector for t in result.action_trace)
            assert any(t.get("dismissal_verified") is True for t in result.action_trace)
            assert not any(t.get("safe_outside_attempts") for t in result.action_trace)
        finally:
            await browser.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("control", [
    '<a id="exit" href="https://outside.test">关闭</a>',
    '<form><button id="exit">关闭</button><input></form>',
    '<button id="exit" type="button">打开</button>',
])
async def test_navigation_submit_and_open_are_not_signed_as_close(control):
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            page = await browser.new_page(viewport={"width": 390, "height": 844})
            await page.set_content(f"""
                <div role="dialog" style="position:fixed;inset:0;z-index:800">
                  <p>活动公告</p>{control}</div>
            """)
            snapshot = await module._collect_ui_snapshot(page, surface_session_id="h5:fixture")
            assert h5_modal_close(snapshot) is None
            assert h5_modal_local_safe_acknowledgement(snapshot) is None
        finally:
            await browser.close()


@pytest.mark.asyncio
async def test_proven_close_without_native_effect_cannot_hide_modal(monkeypatch):
    hide = AsyncMock(return_value={"hidden": True})
    monkeypatch.setattr(module, "_hide_verified_high_z_popup_obstruction", hide)
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            page = await browser.new_page(viewport={"width": 390, "height": 844})
            await page.set_content("""
                <div id="modal" role="dialog" style="position:fixed;inset:0;z-index:900">
                  <button id="exit" type="button">关闭</button></div>
            """)
            proof = h5_modal_close(await module._collect_ui_snapshot(
                page, surface_session_id="h5:fixture",
            ))
            assert proof is not None
            with pytest.raises(module.BlockedPageCaptureActionError) as error:
                await module._run_single_trigger(
                    page=page, context=page.context,
                    request=module.PageCaptureRequest(
                        session_id="h5:fixture", mode=module.PageCaptureMode.SINGLE_TRIGGER,
                        trigger_action=module.PageTriggerAction.CLICK,
                        selector=f"{H5_MODAL_CLOSE_SELECTOR}:{proof.obstruction_signature}",
                        allow_high_z_obstruction_hide=True,
                    ), action_trace=[], timeout_ms=3000, timeout_error_type=PlaywrightTimeoutError,
                )
            assert error.value.error_code == "POPUP_DISMISSAL_NOT_OBSERVED"
            assert await page.locator("#modal").is_visible()
            hide.assert_not_awaited()
        finally:
            await browser.close()


@pytest.mark.asyncio
async def test_state_change_wait_uses_playwright_keyword_argument():
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            page = await browser.new_page()
            await page.set_content("<main>首页</main>")
            revision = await module._install_page_state_monitor(page)
            await page.evaluate("setTimeout(() => document.body.append('更新'), 100)")
            changed = await module._wait_for_page_state_change(
                page=page, previous_revision=revision, timeout_ms=1000,
            )
            assert changed > revision
        finally:
            await browser.close()


@pytest.mark.asyncio
async def test_modal_signature_uses_blob_content_not_random_url_or_notice_text():
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            page = await browser.new_page(viewport={"width": 390, "height": 844})
            await page.route("https://modal.test/", lambda route: route.fulfill(
                content_type="text/html; charset=utf-8", body="""
                    <div role="dialog" style="position:fixed;inset:0;z-index:900">
                      <p id="notice">公告内容</p><img id="picture" width="200" height="200">
                      <button id="exit" type="button">关闭</button></div>
                """,
            ))
            await page.goto("https://modal.test/")

            async def proof_for(color):
                await page.evaluate("""async color => {
                  const canvas = document.createElement('canvas');
                  canvas.width = canvas.height = 10;
                  const ctx = canvas.getContext('2d');
                  ctx.fillStyle = color; ctx.fillRect(0,0,10,10);
                  const blob = await new Promise(resolve => canvas.toBlob(resolve));
                  const picture = document.querySelector('#picture');
                  picture.src = URL.createObjectURL(blob); await picture.decode();
                }""", color)
                return h5_modal_close(await module._collect_ui_snapshot(
                    page, surface_session_id="h5:fixture",
                ))

            red = await proof_for("red")
            assert red is not None
            await page.locator("#notice").evaluate("e => e.textContent = '公告滚动内容更新'")
            same_red = await proof_for("red")
            blue = await proof_for("blue")
            assert same_red.obstruction_signature == red.obstruction_signature
            assert blue.obstruction_signature != red.obstruction_signature
        finally:
            await browser.close()


@pytest.mark.asyncio
async def test_h5_entry_waits_for_slow_javascript_bootstrap_without_reload():
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            page = await browser.new_page(viewport={"width": 390, "height": 844})
            navigations = []

            async def respond(route):
                navigations.append(route.request.url)
                await route.fulfill(content_type="text/html; charset=utf-8", body="""
                    <script>setTimeout(() => {
                      document.body.innerHTML = '<main><h1>产品首页</h1>'
                        + '<button>分类</button></main>';
                    }, 16500)</script><body></body>
                """)

            await page.route("https://slow-entry.test/", respond)
            result = await module.PlaywrightPageCaptureBrowser()._capture_on_page(
                request=module.PageCaptureRequest(
                    session_id="h5:fixture", mode=module.PageCaptureMode.GOTO,
                    url="https://slow-entry.test/", timeout_ms=25000,
                ), page=page, context=page.context, console_logs=[], network_requests=[],
                timeout_error_type=PlaywrightTimeoutError,
            )
            assert result.metrics["navigation_document"]["inspectable"] is True
            assert len(navigations) == 1
        finally:
            await browser.close()
