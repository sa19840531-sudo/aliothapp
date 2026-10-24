"""大内联DOM须先在隔离世界缩减，再传输并保留可审计语义。"""

import hashlib
import json

import pytest
from playwright.async_api import async_playwright

from services.api.app.infrastructure.browser.page_capture import (
    PageCaptureMode,
    PageCaptureRequest,
    _collect_artifacts,
)


@pytest.mark.parametrize("large", [False, True])
async def test_h5_dom_never_transfers_inline_payload_or_calls_site_clone_hook(large, tmp_path):
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            page = await browser.new_page(viewport={"width": 390, "height": 844})
            await page.route(
                "**/*",
                lambda route: (
                    route.fulfill(
                        content_type="text/html", body="<html><body><main>正文</main></body></html>"
                    )
                    if route.request.url == "https://fixture.invalid/"
                    else route.abort()
                ),
            )
            await page.goto("https://fixture.invalid/")
            count, size = (8, 2 * 1024 * 1024) if large else (1, 64)
            await page.evaluate(
                """({count,size}) => {
                window.effects=0;
                const value='data:image/png;base64,'+'A'.repeat(size);
                document.body.innerHTML='<main>正文</main>'
                  +'<button id="close" type="button">关闭</button>';
                for(let i=0;i<count;i++) {
                  const img=document.createElement('img');img.src=value;
                  document.body.append(img);
                }
                document.documentElement.style.cssText=
                  'width:390px;height:844px;background-image:url("'+value+'")';
                Element.prototype.cloneNode=function(){window.effects++;throw Error('site_hook')};
            }""",
                {"count": count, "size": size},
            )

            class WireGuard:
                def __getattr__(self, name):
                    return getattr(page, name)

                async def content(self):
                    raise AssertionError("禁止先经协议传输完整内联DOM再清洗")

            timings = {}
            result = await _collect_artifacts(
                page=WireGuard(),
                request=PageCaptureRequest(
                    mode=PageCaptureMode.OBSERVE,
                    session_id="h5:dom-wire",
                    capture_dom=True,
                    capture_visible_text=False,
                    capture_screenshot=False,
                ),
                include_ui_snapshot=False,
                timings_ms=timings,
            )
            dom = result[0].content
            assert "A" * 64 not in dom
            assert 'id="close"' in dom and "width: 390px" in dom and "height: 844px" in dom
            value = "data:image/png;base64," + "A" * size
            assert hashlib.sha256(value.encode()).hexdigest() in dom
            assert len(dom.encode()) < 20000
            assert await page.evaluate("window.effects") == 0
            assert "dom_browser_read" in timings and "dom_python_compaction" in timings
            (tmp_path / "dom.html").write_text(dom)
            (tmp_path / "timings.json").write_text(json.dumps(timings))
        finally:
            await browser.close()


@pytest.mark.parametrize("secure,large_text", [(True, False), (False, False), (True, True)])
async def test_dom_redaction_preserves_block_identity_and_bounds_without_live_changes(
    secure, large_text
):
    from services.api.app.infrastructure.browser.h5_dom_artifact import read_h5_dom_artifact

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            page = await browser.new_page()
            origin = "https://fixture.invalid/" if secure else "http://fixture.invalid/"
            await page.route(
                "**/*",
                lambda route: route.fulfill(
                    content_type="text/html", body="<html><body><main>正文</main></body></html>"
                ),
            )
            await page.goto(origin)
            await page.evaluate(
                """large=>{
                const template=document.createElement('template');
                template.innerHTML='<img src="data:image/png;base64,'+'A'.repeat(100000)+'">';
                document.body.append(template);
                const script=document.createElement('script');script.type='application/json';
                script.textContent='{"private_resource":"not-business"}';document.body.append(script);
                if(large) document.querySelector('main').textContent='正文'.repeat(100000);
            }""",
                large_text,
            )
            script = await page.locator("script").evaluate("node=>node.outerHTML")
            result = await read_h5_dom_artifact(page, max_bytes=4000, timeout_ms=3000)
            dom, metrics = result["dom"], result["metrics"]
            assert len(dom.encode()) <= 4000
            assert "A" * 50 not in dom and "private_resource" not in dom
            if secure and not large_text:
                assert hashlib.sha256(script.encode()).hexdigest() in dom
            if not secure:
                assert metrics["digest_unavailable_count"] > 0
                assert 'sha256="unavailable"' in dom
            assert metrics["truncated"] is large_text
            assert (
                await page.locator("template").evaluate(
                    'node=>node.content.querySelector("img").getAttribute("src").length'
                )
                > 100000
            )
            assert "private_resource" in await page.locator("script").text_content()
        finally:
            await browser.close()


async def test_dom_read_does_not_construct_site_custom_elements():
    from services.api.app.infrastructure.browser.h5_dom_artifact import read_h5_dom_artifact

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            page = await browser.new_page()
            await page.set_content("<main>正文</main>")
            await page.evaluate("""() => {
                window.constructed=0;
                customElements.define('business-widget', class extends HTMLElement {
                    constructor() {super();window.constructed++;}
                });
                document.body.append(document.createElement('business-widget'));
            }""")
            before = await page.evaluate("window.constructed")
            result = await read_h5_dom_artifact(page, max_bytes=4000, timeout_ms=3000)
            assert "business-widget" in result["dom"]
            assert await page.evaluate("window.constructed") == before
        finally:
            await browser.close()
