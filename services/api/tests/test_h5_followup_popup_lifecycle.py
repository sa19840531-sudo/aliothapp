"""复盘后的弹层生命周期回归，不把素材轮播当作关闭。"""

from types import SimpleNamespace

import pytest
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright

from services.api.app.infrastructure.browser.h5_local_media_digest import (
    H5_LOCAL_MEDIA_DIGEST_JS,
    ensure_h5_local_media_digest,
)
from services.api.app.infrastructure.browser.h5_modal_dismissal_target import (
    sample_modal_close_geometry,
    signed_modal_target,
)
from services.api.app.infrastructure.browser.page_capture import (
    _collect_ui_snapshot,
    _observe_popup_dismissal_outcome,
    _wait_for_popup_dismissal,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("scheme", ["http", "https"])
async def test_local_media_fallback_limit_does_not_reduce_native_https_digest(scheme):
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            page = await browser.new_page()
            url = f"{scheme}://modal-fixture.test/"
            await page.route(url, lambda route: route.fulfill(body=""))
            await page.goto(url)
            await ensure_h5_local_media_digest(page)
            result = await page.evaluate(
                "async () => {" + H5_LOCAL_MEDIA_DIGEST_JS + """
                  try { return await h5LocalMediaDigest(new Uint8Array(6 * 1024 * 1024)); }
                  catch (error) { return String(error.message); }
                }
                """,
            )
            if scheme == "http":
                assert result == "local_media_size_limit"
            else:
                assert len(result) == 64
                assert all(char in "0123456789abcdef" for char in result)
        finally:
            await browser.close()


@pytest.mark.asyncio
async def test_http_same_shape_blob_layers_close_with_native_click_and_snapshot_identity():
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            page = await browser.new_page(viewport={"width": 390, "height": 844})
            await page.route("http://modal-fixture.test/", lambda route: route.fulfill(body=""))
            await page.goto("http://modal-fixture.test/")
            assert await page.evaluate("Boolean(crypto?.subtle)") is False
            await page.evaluate("""() => {
                window.clicks = 0;
                const render = () => {
                    if (window.clicks === 10) return;
                    const layer = document.createElement('div');
                    layer.id = 'layer'; layer.role = 'dialog';
                    layer.style = 'position:fixed;inset:100px 20px;background:white';
                    const img = document.createElement('img');
                    img.src = URL.createObjectURL(new Blob([
                      '<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100">'
                      + '<text x="10" y="50">'+window.clicks+'</text></svg>'],
                      {type:'image/svg+xml'}));
                    const button = document.createElement('button');
                    button.textContent = '关闭';
                    button.onclick = () => {window.clicks++; layer.remove(); render();};
                    layer.append(img, button); document.body.append(layer);
                };
                render();
            }""")
            for index in range(10):
                target = await signed_modal_target(page, SimpleNamespace(
                    selector="#layer button", modal_selector="#layer",
                    obstruction_signature="fixture",
                ), prefer_safe_outside=False)
                assert target["dialog_layer_media"] is not None
                snapshot = await _collect_ui_snapshot(page, surface_session_id="h5:fixture")
                assert snapshot.get("capture_degraded") is None
                assert "crypto_subtle_unavailable" not in str(snapshot)
                assert "h5_local_media_digest_unavailable" not in str(snapshot)
                modal = next(item for item in snapshot["elements"] if item["selector"] == "#layer")
                assert modal["h5_modal_media_identity"]
                assert all(";sha256=" in value for value in modal["h5_modal_media_identity"])
                if index == 0:
                    web_snapshot = await _collect_ui_snapshot(
                        page, surface_session_id="web:fixture",
                    )
                    assert all("h5_modal_media_identity" not in item
                               for item in web_snapshot["elements"])
                await page.locator("#layer button").click()
                assert await _wait_for_popup_dismissal(
                    page=page, popup_target=target, timeout_ms=1500,
                    timeout_error_type=PlaywrightTimeoutError,
                )
                assert await page.evaluate("window.clicks") == index + 1
            assert await page.locator("#layer").count() == 0
        finally:
            await browser.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("scheme", ["http", "https"])
@pytest.mark.parametrize(
    "variant", ["delayed_media", "reuse_hidden", "same_media_hidden", "rotation"],
)
async def test_popup_lifecycle_observes_async_success_without_accepting_rotation(variant, scheme):
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            page = await browser.new_page(viewport={"width": 390, "height": 844})
            url = f"{scheme}://modal-fixture.test/"
            await page.route(url, lambda route: route.fulfill(body=""))
            await page.goto(url)
            await page.set_content("""
                <div id="layer" role="dialog" style="position:fixed;inset:100px 20px">
                  <img><button type="button">关闭</button></div>
            """)
            await page.evaluate("""variant => {
                const asset = color => URL.createObjectURL(new Blob([
                  '<svg xmlns="http://www.w3.org/2000/svg" width="1" height="1">'
                  + '<rect fill="'+color+'" width="1" height="1"/></svg>'],
                  {type:'image/svg+xml'}));
                const old = document.querySelector('#layer');
                old.querySelector('img').src = asset('red');
                old.querySelector('button').onclick = () => {
                    if (variant === 'rotation') {
                        old.querySelector('img').src = asset('blue'); return;
                    }
                    if (variant === 'delayed_media') {
                        const next = old.cloneNode(true); old.replaceWith(next);
                        setTimeout(() => next.querySelector('img').src = asset('blue'), 250);
                    } else {
                        old.style.display = 'none';
                        setTimeout(() => {
                            if (variant === 'reuse_hidden') {
                                old.querySelector('img').src = asset('blue');
                                old.style.display = 'block';
                            } else {
                                const next = old.cloneNode(true);
                                next.style.display = 'block'; old.replaceWith(next);
                            }
                        }, 150);
                    }
                };
            }""", variant)
            target = await signed_modal_target(page, SimpleNamespace(
                selector="#layer button", modal_selector="#layer", obstruction_signature="fixture",
            ), prefer_safe_outside=False)
            assert target["dialog_layer_media"] is not None
            await page.locator("#layer button").click()
            verified = await _wait_for_popup_dismissal(
                page=page, popup_target=target, timeout_ms=2000,
                timeout_error_type=PlaywrightTimeoutError,
            )
            assert verified is (variant != "rotation")
            assert await _observe_popup_dismissal_outcome(page=page, popup_target=target) == (
                "original_layer_still_visible" if variant == "rotation"
                else "next_popup_layer_visible"
            )
        finally:
            await browser.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("layers", [10, 25, 50])
async def test_successful_long_popup_chain_has_no_twenty_layer_stop(layers):
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            page = await browser.new_page(viewport={"width": 390, "height": 844})
            await page.set_content("""
                <div id="layer" role="dialog" style="position:fixed;inset:100px 20px">
                  <button type="button" style="width:60px;height:40px">关闭</button></div>
            """)
            await page.evaluate("""layers => {
                window.remaining = layers; window.clicks = 0;
                document.querySelector('button').onclick = () => {
                    window.clicks++; window.remaining--;
                    const layer = document.querySelector('#layer');
                    layer.style.display = 'none';
                    if (window.remaining) setTimeout(() => {
                        layer.style.display = 'block';
                    }, 10);
                };
            }""", layers)
            for _ in range(layers):
                await page.locator("#layer button").wait_for(state="visible")
                target = await signed_modal_target(page, SimpleNamespace(
                    selector="#layer button", modal_selector="#layer",
                    obstruction_signature="same-close-control",
                ), prefer_safe_outside=False)
                geometry = await sample_modal_close_geometry(page, target)
                assert geometry["all_points_clicked"] is False
                assert await page.evaluate("window.clicks") == layers - await page.evaluate(
                    "window.remaining",
                )
                await page.locator("#layer button").click()
                assert await _wait_for_popup_dismissal(
                    page=page, popup_target=target, timeout_ms=1500,
                    timeout_error_type=PlaywrightTimeoutError,
                )
            assert await page.evaluate("window.clicks") == layers
            assert await page.locator("#layer").is_visible() is False
        finally:
            await browser.close()
