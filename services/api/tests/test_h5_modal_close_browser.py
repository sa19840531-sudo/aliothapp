"""任意站点类名的弹窗须经真实 Chromium 采集、命中与关闭验证。"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright

from services.api.app.agents.tools.page_detection_tools import (
    CandidateAction,
    _with_obstruction_dismissal_candidate,
)
from services.api.app.application.h5_logical_surface_evidence import build_h5_logical_surface
from services.api.app.application.h5_modal_acknowledgement import (
    h5_modal_local_safe_acknowledgement,
)
from services.api.app.application.h5_modal_close import (
    H5_MODAL_CLOSE_CAPABILITY,
    H5_MODAL_CLOSE_SELECTOR,
    h5_modal_close,
)
from services.api.app.application.interaction_runtime import SemanticTargetResolutionRequest
from services.api.app.domain.interaction import InteractionOperation
from services.api.app.infrastructure.browser.page_capture import (
    BlockedPageCaptureActionError,
    PageCaptureMode,
    PageCaptureRequest,
    PageTriggerAction,
    PlaywrightPageCaptureBrowser,
    _clear_remaining_obstructions,
    _collect_ui_snapshot,
    _observe_popup_dismissal_outcome,
    _resolve_popup_dismissal_target,
    _run_single_trigger,
    _validate_request,
    _wait_for_popup_dismissal,
)
from services.api.app.infrastructure.browser.semantic_target_resolver import (
    UiSnapshotSemanticTargetResolver,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("variant", ["button", "icon", "external", "multiple", "distant", "web"])
async def test_modal_sibling_close_control_is_not_replaced_by_backdrop_click(variant):
    """框外同级关闭键须原生点击，但同级外跳按钮不得取得关闭证明。"""
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            context = await browser.new_context(viewport={"width": 390, "height": 844})
            page = await context.new_page()
            await page.set_content("""
                <style>
                  #backdrop{position:fixed;inset:0;background:#aaa;z-index:1000}
                  #panel{position:fixed;left:65px;top:248px;width:260px;height:264px;
                    background:white;z-index:1001}
                  #close{position:fixed;left:332px;top:190px;width:35px;height:35px;
                    z-index:1002;cursor:pointer}
                </style>
                <div id="scope"><div id="backdrop"></div>
                  <div id="panel" role="dialog">公告</div>
                  <div><button id="close" type="button" aria-label="关闭">×</button></div>
                </div>
            """)
            await page.evaluate("""variant => {
                let close = document.querySelector('#close');
                if (variant === 'external') close.dataset.url = 'https://outbound.test/';
                if (variant === 'multiple') {
                    const other = document.querySelector('#panel').cloneNode(true);
                    other.id = 'other-panel';
                    document.querySelector('#scope').append(other);
                }
                if (variant === 'distant') close.style.top = '20px';
                if (variant === 'icon') {
                    const icon = document.createElement('i');
                    icon.id = 'close'; icon.className = 'van-icon-close';
                    close.replaceWith(icon); close = icon;
                }
                close.onclick = () => document.querySelector('#scope').remove();
            }""", variant)
            proof = h5_modal_close(await _collect_ui_snapshot(
                page, surface_session_id="web:sibling" if variant == "web" else "h5:sibling",
            ))
            if variant not in {"button", "icon"}:
                assert proof is None or proof.selector != "#close"
                return
            assert proof is not None
            assert proof.selector == "#close"
            assert proof.outside_position is None
            trace = []
            await _run_single_trigger(
                page=page, context=context,
                request=PageCaptureRequest(
                    session_id="h5:sibling", mode=PageCaptureMode.SINGLE_TRIGGER,
                    trigger_action=PageTriggerAction.CLICK,
                    selector=f"{H5_MODAL_CLOSE_SELECTOR}:{proof.obstruction_signature}",
                    h5_popup_dismissal_strategy="safe_outside_then_close",
                    capture_screenshot=False,
                ),
                action_trace=trace, timeout_ms=3000,
                timeout_error_type=PlaywrightTimeoutError,
            )
            assert trace[-1]["dismissal_kind"] == "close_control"
            assert await page.locator("#scope").count() == 0
        finally:
            await browser.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("replacement", [
    "same_layer", "dynamic_copy", "new_layer", "removed", "hidden_replacement",
])
async def test_signed_dismissal_does_not_accept_same_layer_rerender(replacement):
    """节点重建不等于关闭，只有清空或不同弹层的结构才构成关闭证据。"""
    from services.api.app.infrastructure.browser.h5_modal_dismissal_target import (
        signed_modal_target,
    )

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            page = await browser.new_page(viewport={"width": 390, "height": 844})
            await page.set_content("""
                <div id="layer" role="dialog"
                  style="position:fixed;inset:100px 20px;background:white">
                  <p>公告</p><img><button type="button">我知道了</button></div>
            """)
            target = await signed_modal_target(page, SimpleNamespace(
                selector="#layer button", modal_selector="#layer",
                obstruction_signature="fixture",
            ), prefer_safe_outside=False)
            await page.evaluate("""(replacement) => {
                const old = document.querySelector('#layer');
                if (replacement === 'removed') { old.remove(); return; }
                const next = old.cloneNode(true);
                if (replacement === 'dynamic_copy') {
                    next.querySelector('p').textContent = '公告动态更新 123';
                    next.querySelector('img').src = 'data:image/gif;base64,'
                      + 'R0lGODlhAQABAIAAAP///w==';
                    next.style.opacity = '.8';
                }
                if (replacement === 'hidden_replacement') next.style.opacity = '0';
                if (replacement === 'new_layer') {
                    next.innerHTML = '<h2>后续公告</h2><section>'
                      + '<button type="button">关闭</button></section>';
                }
                old.replaceWith(next);
            }""", replacement)
            verified = await _wait_for_popup_dismissal(
                page=page, popup_target=target, timeout_ms=1200,
                timeout_error_type=PlaywrightTimeoutError,
            )
            assert verified is (replacement in {"new_layer", "removed", "hidden_replacement"})
            assert await _observe_popup_dismissal_outcome(page=page, popup_target=target) == {
                "same_layer": "original_layer_still_visible",
                "dynamic_copy": "original_layer_still_visible",
                "new_layer": "next_popup_layer_visible",
                "removed": "original_layer_absent",
                "hidden_replacement": "original_layer_absent",
            }[replacement]
        finally:
            await browser.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["different_ad", "same_ad_new_blob", "text_only"])
async def test_native_close_distinguishes_same_structure_ad_chain(change):
    """真实关闭键移除原节点后，新广告可同结构；同广告换 URL 和文案仍不算关闭。"""
    from services.api.app.infrastructure.browser.h5_modal_dismissal_target import (
        signed_modal_target,
    )

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            page = await browser.new_page(viewport={"width": 390, "height": 844})
            await page.route("https://modal-fixture.test/", lambda route: route.fulfill(body=""))
            await page.goto("https://modal-fixture.test/")
            await page.set_content("""
                <div id="layer" role="dialog"
                  style="position:fixed;inset:100px 20px;background:white">
                  <p>广告</p><img><button type="button">关闭</button></div>
            """)
            await page.evaluate("""(change) => {
                const source = '<svg xmlns="http://www.w3.org/2000/svg" '
                  + 'width="1" height="1"><rect fill="red" width="1" height="1"/></svg>';
                const asset = text => URL.createObjectURL(new Blob([text],
                  {type:'image/svg+xml'}));
                const old = document.querySelector('#layer');
                old.querySelector('img').src = asset(source);
                old.querySelector('button').onclick = () => {
                    const next = old.cloneNode(true);
                    if (change === 'text_only') next.querySelector('p').textContent = '公告更新';
                    else next.querySelector('img').src = asset(
                      change === 'different_ad' ? source.replace('red', 'blue') : source);
                    old.replaceWith(next);
                };
            }""", change)
            target = await signed_modal_target(page, SimpleNamespace(
                selector="#layer button", modal_selector="#layer",
                obstruction_signature="fixture",
            ), prefer_safe_outside=False)
            await page.locator("#layer button").click()
            assert await _wait_for_popup_dismissal(
                page=page, popup_target=target, timeout_ms=1200,
                timeout_error_type=PlaywrightTimeoutError,
            ) is (change == "different_ad")
            assert await _observe_popup_dismissal_outcome(page=page, popup_target=target) == (
                "next_popup_layer_visible" if change == "different_ad"
                else "original_layer_still_visible"
            )
        finally:
            await browser.close()


@pytest.mark.asyncio
async def test_signed_native_close_preserves_each_same_structure_chain_layer():
    """已有唯一安全关闭键时先点关闭键，同结构的后续广告逐层留痕。"""
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            context = await browser.new_context(viewport={"width": 390, "height": 844})
            page = await context.new_page()
            await page.route("https://modal-fixture.test/", lambda route: route.fulfill(body=""))
            await page.goto("https://modal-fixture.test/")
            await page.set_content("""
                <style>#backdrop{position:fixed;inset:0;background:#aaa;z-index:1000}
                  #layer{position:absolute;inset:100px 20px;background:white}</style>
                <div id="backdrop"><div id="layer" role="dialog">
                  <p>广告</p><img><button type="button">关闭</button></div></div>
            """)
            await page.evaluate("""() => {
                const asset = color => URL.createObjectURL(new Blob([
                  '<svg xmlns="http://www.w3.org/2000/svg" width="1" height="1">'
                  + '<rect fill="'+color+'" width="1" height="1"/></svg>'],
                  {type:'image/svg+xml'}));
                let step = 0;
                const dismiss = () => {
                    const old = document.querySelector('#layer');
                    if (++step === 2) {document.querySelector('#backdrop').remove(); return;}
                    const next = old.cloneNode(true);
                    next.querySelector('img').src = asset('blue');
                    next.querySelector('button').onclick = dismiss;
                    old.replaceWith(next);
                };
                const layer = document.querySelector('#layer');
                layer.querySelector('img').src = asset('red');
                layer.querySelector('button').onclick = dismiss;
                document.querySelector('#backdrop').onclick = event => {
                    if (event.target === event.currentTarget) dismiss();
                };
            }""")
            proof = h5_modal_close(await _collect_ui_snapshot(page, surface_session_id="h5:chain"))
            assert proof is not None
            request = PageCaptureRequest(
                session_id="h5:chain", mode=PageCaptureMode.SINGLE_TRIGGER,
                trigger_action=PageTriggerAction.CLICK,
                selector=f"{H5_MODAL_CLOSE_SELECTOR}:{proof.obstruction_signature}",
                h5_popup_dismissal_strategy="safe_outside_then_close", capture_screenshot=False,
            )
            trace = []
            await _run_single_trigger(
                page=page, context=context, request=request, action_trace=trace,
                timeout_ms=3000, timeout_error_type=PlaywrightTimeoutError,
            )
            assert trace[-1]["dismissal_kind"] == "close_control"
            assert trace[-1]["popup_dismissal_outcome"] == "next_popup_layer_visible"
            metrics = await _clear_remaining_obstructions(
                page=page, context=context, request=request, action_trace=trace,
                obstruction_frames=[], timeout_ms=3000, timeout_error_type=PlaywrightTimeoutError,
            )
            assert metrics["remaining_obstruction"] is False
            assert metrics["dismissal_count"] == 2
            assert await page.locator("#layer").count() == 0
        finally:
            await browser.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("binding", ["signed", "legacy_h5"])
async def test_same_layer_rerender_falls_back_to_real_close_control(monkeypatch, binding):
    """外侧点击触发重绘但未关闭时，必须继续原生点击真实关闭键。"""
    from services.api.app.infrastructure.browser import page_capture as module
    from services.api.app.infrastructure.browser.h5_modal_dismissal_target import (
        signed_modal_target,
    )

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            context = await browser.new_context(viewport={"width": 390, "height": 844})
            page = await context.new_page()
            await page.set_content("""
                <style>#backdrop{position:fixed;inset:0;background:#aaa}
                  #layer{position:absolute;inset:100px 20px;background:white}</style>
                <div id="backdrop" onclick="if(event.target===this){
                  const node=document.querySelector('#layer');
                  node.replaceWith(node.cloneNode(true));}">
                  <div id="layer" role="dialog"><p>公告</p>
                    <button type="button"
                      onclick="document.querySelector('#backdrop').remove()">关闭</button>
                  </div></div>
            """)
            target = await signed_modal_target(page, SimpleNamespace(
                selector="#layer button", modal_selector="#layer", obstruction_signature="fixture",
            ), prefer_safe_outside=True)
            assert target["kind"] == "overlay"
            if binding == "legacy_h5":
                binding_keys = {"dialog_node_token", "dialog_layer_shape", "require_hidden"}
                target = {key: value for key, value in target.items()
                          if key not in binding_keys}
            monkeypatch.setattr(
                module, "_resolve_popup_dismissal_target", AsyncMock(return_value=target),
            )
            trace = []
            await _run_single_trigger(
                page=page, context=context,
                request=PageCaptureRequest(
                    session_id="h5:fixture", mode=PageCaptureMode.SINGLE_TRIGGER,
                    trigger_action=PageTriggerAction.CLICK, selector="text=关闭",
                    h5_popup_dismissal_strategy="safe_outside_then_close",
                ), action_trace=trace, timeout_ms=3000,
                timeout_error_type=PlaywrightTimeoutError,
            )
            assert await page.locator("#layer").count() == 0
            assert trace[-1]["fallback"] == "visible_close_control"
            assert trace[-1]["dismissal_verified"] is True
            assert all(item["dismissal_verified"] is False
                       for item in trace[-1]["safe_outside_attempts"])
        finally:
            await browser.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("child_count,expected", [(388, True), (3999, True), (4000, False)])
async def test_modal_layer_binding_preserves_large_existing_ad_panel(child_count, expected):
    """实际广告弹窗有 389 个后代，关闭绑定不能引入过小的新拒绝门槛。"""
    from services.api.app.infrastructure.browser.h5_modal_dismissal_target import (
        signed_modal_target,
    )

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            page = await browser.new_page()
            await page.set_content('<div id="layer" role="dialog">'
                                   + '<span></span>' * child_count + '<button>关闭</button></div>')
            target = await signed_modal_target(page, SimpleNamespace(
                selector="#layer button", modal_selector="#layer", obstruction_signature="fixture",
            ), prefer_safe_outside=False)
            assert (target is not None) is expected
            if target is not None:
                assert target["dialog_layer_shape"]
        finally:
            await browser.close()


async def test_countdown_to_close_control_requires_fresh_signed_snapshot():
    """倒计时转为真实关闭键后须重新采集和签发，不复用旧层候选。"""
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            page = await browser.new_page(viewport={"width": 390, "height": 844})
            await page.set_content("""
                <style>body{margin:0} #layer{position:fixed;inset:0;z-index:999;
                    background:white} #control{position:absolute;right:16px;top:32px;
                    width:64px;height:40px}</style>
                <h1>内容列表</h1><a href="/detail">查看详情</a>
                <div id="layer" role="dialog" aria-modal="true">
                    <span id="control">2s</span></div>
            """)
            before = await _collect_ui_snapshot(page, surface_session_id="h5:fixture")
            assert h5_modal_close(before) is None
            await page.evaluate("""() => {
                const control = document.querySelector('#control');
                control.textContent = '跳过';
                control.onclick = () => document.querySelector('#layer').remove();
            }""")
            fresh = await _collect_ui_snapshot(page, surface_session_id="h5:fixture")
            proof = h5_modal_close(fresh)
            assert proof is not None
            assert h5_modal_close(before) is None
            target = await _resolve_popup_dismissal_target(
                page=page, selector=f"{H5_MODAL_CLOSE_SELECTOR}:{proof.obstruction_signature}",
            )
            assert target is not None and target["kind"] == "close_control"
            await page.locator(target["selector"]).click()
            assert await page.locator("#layer").count() == 0
            assert await page.locator("h1").inner_text() == "内容列表"
        finally:
            await browser.close()


@pytest.mark.asyncio
async def test_safe_acknowledgement_with_arbitrary_css_closes_real_modal(monkeypatch):
    from services.api.app.infrastructure.browser import page_capture as module
    from services.api.app.infrastructure.browser.capture_runtime import (
        _logical_target_runtime_binding,
    )

    monkeypatch.setattr(module, "_wait_for_page_settle", AsyncMock(return_value=True))
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            context = await browser.new_context(viewport={"width": 390, "height": 844})
            page = await context.new_page()
            await page.set_content("""
                <style>body{margin:0} #layer{position:fixed;inset:0;z-index:999;
                  background:#eee} #copy{position:absolute;top:260px;left:50px;
                  width:290px;height:220px} #ack{position:absolute;top:560px;left:135px;
                  width:120px;height:40px}</style>
                <main><h1>产品首页</h1><button>确认充值</button></main>
                <div id="layer" onclick="if(event.target===this)this.remove()">
                  <p id="copy">版本更新公告，更多内容持续上线</p>
                  <button id="ack" type="button"
                    onclick="document.querySelector('#layer').remove()">确认</button></div>
            """)
            snapshot = await _collect_ui_snapshot(
                page, surface_session_id="h5:fixture", logical_surface_enabled=True,
                logical_surface_shadow=False,
            )
            proof = h5_modal_local_safe_acknowledgement(snapshot)
            assert proof is not None
            surface = snapshot["logical_surface"]
            parameters = {
                "target_mode": "popup_dismissal",
                "popup_dismissal_capability": proof.capability,
                "obstruction_signature": proof.obstruction_signature,
                "logical_target": {
                    "protocol_version": "logical_target.v1",
                    "source_identity_fingerprint": surface["identity_fingerprint"],
                    "source_state_fingerprint": surface["state_fingerprint"],
                    "expects_logical_navigation": True,
                },
            }
            resolver = UiSnapshotSemanticTargetResolver(
                snapshot_reader=SimpleNamespace(read_ui_snapshot=AsyncMock(return_value=snapshot)),
                logical_targets_enabled=True,
            )
            target = await resolver.resolve(SemanticTargetResolutionRequest(
                task_id="task-modal", session_id="h5:fixture", source_capture_id="capture",
                action_type=InteractionOperation.CLICK, semantic_target="关闭当前弹窗",
                parameters=parameters,
            ))
            binding = _logical_target_runtime_binding(
                parameters=parameters, target=target, operation=InteractionOperation.CLICK,
            )
            assert binding is not None
            assert target.trusted_target_role == "dismiss"
            result = await PlaywrightPageCaptureBrowser(
                logical_surface_enabled=True, logical_surface_shadow=False,
            )._capture_on_page(
                request=PageCaptureRequest(
                    session_id="h5:fixture", mode=PageCaptureMode.SINGLE_TRIGGER,
                    trigger_action=PageTriggerAction.CLICK,
                    selector=target.selector,
                    clear_obstructions=True, startup_policy="direct_home",
                    allow_high_z_obstruction_hide=target.allow_high_z_obstruction_hide,
                    h5_popup_dismissal_strategy=target.h5_popup_dismissal_strategy,
                    logical_surface_enabled=True, logical_surface_shadow=False,
                    _logical_target_binding=binding,
                ), page=page, context=context, console_logs=[], network_requests=[],
                timeout_error_type=PlaywrightTimeoutError,
            )
            assert await page.locator("#layer").count() == 0
            assert any(item.get("dismissal_verified") is True for item in result.action_trace)
        finally:
            await browser.close()


@pytest.mark.asyncio
async def test_h5_close_capture_preserves_final_snapshot_and_verified_native_action(monkeypatch):
    from services.api.app.infrastructure.browser import page_capture as module

    monkeypatch.setattr(module, "_wait_for_page_settle", AsyncMock(return_value=True))
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            context = await browser.new_context(viewport={"width": 390, "height": 844})
            page = await context.new_page()
            await page.set_content("""
                <style>body{margin:0} #modal{position:fixed;inset:0;z-index:999;
                    background:white} button{width:50px;height:50px}</style>
                <main><h1>产品首页</h1><a href="/category">分类</a></main>
                <div id="modal" role="dialog" aria-modal="true"><h2>公告</h2>
                    <button type="button" aria-label="关闭"
                        onclick="document.querySelector('#modal').remove()">×</button></div>
            """)
            before = await _collect_ui_snapshot(page, surface_session_id="h5:fixture")
            proof = h5_modal_close(before)
            assert proof is not None
            result = await PlaywrightPageCaptureBrowser(
                logical_surface_enabled=True, logical_surface_shadow=False,
            )._capture_on_page(
                request=PageCaptureRequest(
                    session_id="h5:fixture", mode=PageCaptureMode.SINGLE_TRIGGER,
                    trigger_action=PageTriggerAction.CLICK,
                    selector=f"{H5_MODAL_CLOSE_SELECTOR}:{proof.obstruction_signature}",
                    clear_obstructions=True, startup_policy="direct_home",
                    logical_surface_enabled=True, logical_surface_shadow=False,
                ),
                page=page, context=context, console_logs=[], network_requests=[],
                timeout_error_type=PlaywrightTimeoutError,
            )
            snapshots = [item.content for item in result.artifacts
                         if item.artifact_type == "ui_snapshot"]
            assert len(snapshots) == 1
            snapshot = snapshots[0]
            assert snapshot.get("sequence") is not True
            assert snapshot["h5_modal_census_selectors"] == []
            assert "logical_surface" in snapshot, snapshot.get("logical_surface_degraded")
            assert any(item.get("dismissal_verified") is True for item in result.action_trace)
            assert await page.locator("#modal").count() == 0
        finally:
            await browser.close()


@pytest.mark.parametrize("session_id", ["h5:fixture", "web:fixture", None])
def test_unsafe_close_contract_is_rejected_before_pointer_with_h5_specific_code(session_id):
    request = PageCaptureRequest(
        session_id=session_id, mode=PageCaptureMode.SINGLE_TRIGGER,
        trigger_action=PageTriggerAction.CLICK, selector="#ordinary-business-button",
        clear_obstructions=True,
    )
    with pytest.raises(BlockedPageCaptureActionError) as caught:
        _validate_request(request)
    assert caught.value.error_code == (
        "H5_POPUP_DISMISSAL_INTENT_INVALID"
        if session_id and session_id.startswith("h5:") else "PAGE_CAPTURE_ACTION_BLOCKED"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("semantic", [True, False])
@pytest.mark.parametrize("hide_node", [True, False])
@pytest.mark.parametrize("control_tag", ["button", "div"])
async def test_custom_modal_is_captured_and_native_close_is_verified(
    semantic, hide_node, control_tag,
) -> None:
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            page = await browser.new_page(viewport={"width": 390, "height": 844})
            attributes = 'role="dialog" aria-modal="true"' if semantic else ""
            close_handler = (
                "document.querySelector('#shell').style.display='none'"
                if hide_node
                else "document.querySelector('#shell').remove()"
            )
            await page.set_content(f"""
                <style>body {{margin:0}} #shell {{position:fixed;inset:0;z-index:500}}
                  #body {{margin:200px 20px;background:white;height:300px}}
                  #exit {{width:40px;height:40px}}</style>
                <main><a href="/next">下一页</a></main>
                <div id="shell" {attributes}><div id="body"><h2>活动公告</h2>
                  <p id="ticker">公告一</p>
                  <{control_tag} id="exit" type="button" aria-label="关闭"
                    onclick="{close_handler}">×</{control_tag}>
                </div></div>
            """)
            snapshot = await _collect_ui_snapshot(page, surface_session_id="h5:fixture")
            proof = h5_modal_close(snapshot)
            assert proof is not None
            actions = _with_obstruction_dismissal_candidate(
                page_type="home",
                page_state="stable",
                candidate_actions=[],
                dom_chunks=[],
                ui_snapshot=snapshot,
            )
            assert len(actions) == 1
            assert actions[0].obstruction_capability == H5_MODAL_CLOSE_CAPABILITY
            assert actions[0].obstruction_signature == proof.obstruction_signature
            business_action = CandidateAction(
                action="click",
                target="下一页",
                risk_level="low",
                purpose="浏览下一栏目",
            )
            with_navigation = _with_obstruction_dismissal_candidate(
                page_type="home",
                page_state="popup",
                candidate_actions=[
                    CandidateAction(
                        action="click", target="关闭", risk_level="low", purpose="关闭弹窗"
                    ),
                    business_action,
                ],
                dom_chunks=[],
                ui_snapshot=snapshot,
            )
            assert business_action in with_navigation
            surface = build_h5_logical_surface(
                ui_snapshot=snapshot,
                page_url=page.url,
                surface_session_id="h5:fixture",
                document_token="fixture-document",
            )
            snapshot["logical_surface"] = {**surface, "shadow": False}
            resolver = UiSnapshotSemanticTargetResolver(
                snapshot_reader=SimpleNamespace(read_ui_snapshot=AsyncMock(return_value=snapshot)),
                logical_targets_enabled=True,
            )
            resolved = await resolver.resolve(
                SemanticTargetResolutionRequest(
                    task_id="task-modal",
                    session_id="h5:fixture",
                    source_capture_id="capture-modal",
                    action_type=InteractionOperation.CLICK,
                    semantic_target="关闭当前弹窗",
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
                )
            )
            assert resolved.target_trust == "trusted"
            assert resolved.trusted_target_role == "dismiss"
            target = await _resolve_popup_dismissal_target(
                page=page,
                selector=f"{H5_MODAL_CLOSE_SELECTOR}:{proof.obstruction_signature}",
            )
            assert target["pointer_click_required"] is True
            await page.locator("#ticker").evaluate("el => el.textContent = '公告二'")
            assert await _observe_popup_dismissal_outcome(page=page, popup_target=target) == (
                "original_layer_still_visible"
            )
            assert not await _wait_for_popup_dismissal(
                page=page,
                popup_target=target,
                timeout_ms=100,
                timeout_error_type=PlaywrightTimeoutError,
            )
            await page.locator(target["selector"]).click()
            assert await _wait_for_popup_dismissal(
                page=page,
                popup_target=target,
                timeout_ms=1500,
                timeout_error_type=PlaywrightTimeoutError,
            )
            assert await page.locator("main a").is_visible()
            assert await _observe_popup_dismissal_outcome(page=page, popup_target=target) == (
                "original_layer_hidden" if hide_node else "original_layer_absent"
            )
            after = await _collect_ui_snapshot(page, surface_session_id="h5:fixture")
            after_surface = build_h5_logical_surface(
                ui_snapshot=after,
                page_url=page.url,
                surface_session_id="h5:fixture",
                document_token="fixture-document",
            )
            assert after_surface["identity_fingerprint"] == surface["identity_fingerprint"]
            assert after_surface["state_signals"]["obstruction_count"] == "0"
        finally:
            await browser.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("image_close", [True, False])
async def test_large_document_close_contract_reaches_real_pointer_executor(image_close):
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            context = await browser.new_context(viewport={"width": 390, "height": 844})
            page = await context.new_page()
            body = "".join(
                f'<a href="/item/{i}" class="card">栏目{i} {"正文" * 300}</a>'
                for i in range(420)
            )
            control = (
                '<div class="btn" onclick="document.querySelector(\'#modal\').remove()">'
                '<img alt="" width="36" height="36" src="data:image/svg+xml,'
                '%3Csvg xmlns=%22http://www.w3.org/2000/svg%22/%3E"></div>'
                if image_close else
                '<button type="button" data-dismiss="modal" '
                'onclick="document.querySelector(\'#modal\').remove()">×</button>'
            )
            await page.set_content(f"""
                <style>body{{margin:0}} .card{{display:block;height:30px}}
                  #modal{{position:fixed;inset:0;z-index:500;background:white}}
                  #content{{margin:120px 20px}} .btn,button{{width:36px;height:36px}}</style>
                <main>{body}</main><div id="modal" role="dialog" class="van-dialog">
                  <div id="content" class="grid-main"><p>活动公告</p>{control}</div></div>
            """)
            snapshot = await _collect_ui_snapshot(
                page, surface_session_id="h5:fixture", logical_surface_enabled=True,
                logical_surface_shadow=False,
            )
            proof = h5_modal_close(snapshot)
            assert proof is not None
            assert snapshot["h5_modal_census_truncated"] is False
            resolver = UiSnapshotSemanticTargetResolver(
                snapshot_reader=SimpleNamespace(read_ui_snapshot=AsyncMock(return_value=snapshot)),
                logical_targets_enabled=True,
            )
            surface = snapshot["logical_surface"]
            target = await resolver.resolve(SemanticTargetResolutionRequest(
                task_id="task-modal", session_id="h5:fixture", source_capture_id="capture",
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
            request = PageCaptureRequest(
                session_id="h5:fixture", mode=PageCaptureMode.SINGLE_TRIGGER,
                trigger_action=PageTriggerAction.CLICK, selector=target.selector,
                clear_obstructions=target.clear_obstructions,
                allow_high_z_obstruction_hide=target.allow_high_z_obstruction_hide,
                h5_popup_dismissal_strategy=target.h5_popup_dismissal_strategy,
            )
            _validate_request(request)
            trace = []
            await _run_single_trigger(
                page=page, context=context, request=request, action_trace=trace,
                timeout_ms=3000, timeout_error_type=PlaywrightTimeoutError,
            )
            assert await page.locator("#modal").count() == 0
            assert trace[-1]["dismissal_verified"] is True
        finally:
            await browser.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ['a[href="/browse"]:visible', "浏览栏目"])
async def test_h5_navigation_uses_unique_hittable_link_not_offscreen_duplicate(target) -> None:
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            page = await browser.new_page(viewport={"width": 390, "height": 844})
            await page.route(
                "https://fixture.test/**",
                lambda route: route.fulfill(content_type="text/html", body="<main>导航测试</main>"),
            )
            await page.goto("https://fixture.test/")
            await page.set_content("""
                <style>body {margin:0} aside {position:fixed;left:-500px}</style>
                <main><h1>产品首页</h1><a id="visible-entry" href="/browse">浏览栏目</a>
                  <a href="/other">其他栏目</a></main>
                <aside role="navigation"><a href="/browse">浏览栏目</a></aside>
            """)
            snapshot = await _collect_ui_snapshot(page, surface_session_id="h5:fixture")
            surface = build_h5_logical_surface(
                ui_snapshot=snapshot,
                page_url=page.url,
                surface_session_id="h5:fixture",
                document_token="fixture-document",
            )
            snapshot["logical_surface"] = {**surface, "shadow": False}
            resolver = UiSnapshotSemanticTargetResolver(
                snapshot_reader=SimpleNamespace(read_ui_snapshot=AsyncMock(return_value=snapshot)),
                logical_targets_enabled=True,
            )
            request = SemanticTargetResolutionRequest(
                task_id="task-navigation",
                session_id="h5:fixture",
                source_capture_id="capture-navigation",
                action_type=InteractionOperation.CLICK,
                semantic_target=target,
                parameters={
                    "logical_target": {
                        "protocol_version": "logical_target.v1",
                        "source_identity_fingerprint": surface["identity_fingerprint"],
                        "source_state_fingerprint": surface["state_fingerprint"],
                        "expects_logical_navigation": True,
                    }
                },
            )
            resolved = await resolver.resolve(request)
            assert await page.locator(resolved.selector).get_attribute("id") == "visible-entry"
        finally:
            await browser.close()


@pytest.mark.asyncio
async def test_regional_icon_dismissal_survives_large_business_document() -> None:
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            page = await browser.new_page(viewport={"width": 390, "height": 844})
            await page.set_content("""
                <style>body {margin:0} #notice {position:fixed;top:0;left:0;
                  width:390px;height:70px;background:white} #exit {width:30px;height:30px}
                  main {padding-top:80px}</style>
                <main><h1>产品首页</h1><a href='/next'>栏目</a></main>
                <aside id='notice'>添加到主屏幕
                  <button id='exit' type='button' data-dismiss='modal'
                    onclick="document.querySelector('#notice').remove()">
                    <svg><use href='/icons.svg#close'></use></svg>
                  </button></aside>
            """)
            await page.locator("main").evaluate("""el => {
                for (let i=0;i<500;i++) {
                    const p=document.createElement('p'); p.textContent='内容 '+i;el.append(p);
                }
            }""")
            snapshot = await _collect_ui_snapshot(page, surface_session_id="h5:fixture")
            proof = h5_modal_close(snapshot)
            assert proof is not None
            surface = build_h5_logical_surface(
                ui_snapshot=snapshot,
                page_url=page.url,
                surface_session_id="h5:fixture",
                document_token="fixture-document",
            )
            snapshot["logical_surface"] = {**surface, "shadow": False}
            resolver = UiSnapshotSemanticTargetResolver(
                snapshot_reader=SimpleNamespace(read_ui_snapshot=AsyncMock(return_value=snapshot)),
                logical_targets_enabled=True,
            )
            resolved = await resolver.resolve(
                SemanticTargetResolutionRequest(
                    task_id="task-modal",
                    session_id="h5:fixture",
                    source_capture_id="capture-modal",
                    action_type=InteractionOperation.CLICK,
                    semantic_target="关闭当前弹窗",
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
                )
            )
            assert resolved.target_trust == "trusted"
            target = await _resolve_popup_dismissal_target(
                page=page,
                selector=f"{H5_MODAL_CLOSE_SELECTOR}:{proof.obstruction_signature}",
            )
            assert target is not None
            await page.locator(target["selector"]).click()
            assert await _wait_for_popup_dismissal(
                page=page,
                popup_target=target,
                timeout_ms=1500,
                timeout_error_type=PlaywrightTimeoutError,
            )
            assert await page.locator("main a").is_visible()
        finally:
            await browser.close()
