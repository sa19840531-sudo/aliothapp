"""保留破图生成的真实DOM与PNG；机制复现不冒充历史归因。"""

import base64
import hashlib
import json
from pathlib import Path

import pytest

from services.api.app.infrastructure.browser.page_capture import (
    _collect_visual_navigation_snapshot,
)


@pytest.mark.parametrize("kind", ["broken_image", "broken_default_margin",
                                  "broken_image_document", "painted_canvas", "unpainted_canvas"])
async def test_visual_frame_origin_preserves_dom_paint_and_image_failure(kind, tmp_path):
    playwright_api = pytest.importorskip("playwright.async_api")
    content = {
        "broken_image": '<img src="data:image/png;base64,AA==">',
        "broken_default_margin": '<img src="data:image/png;base64,AA==">',
        "broken_image_document": "",
        "painted_canvas": '<canvas width="240" height="320"></canvas><script>'
            'const c=document.querySelector("canvas");const x=c.getContext("2d");'
            'x.fillStyle="#f5f5f5";x.fillRect(0,0,240,320);'
            'x.strokeRect(18,72,97,46);</script>',
        "unpainted_canvas": '<canvas width="240" height="320"></canvas>',
    }[kind]
    async with playwright_api.async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        page = await browser.new_page(viewport={"width": 240, "height": 320})
        await page.route("**/*", lambda route: route.abort())
        try:
            style = ("" if kind == "broken_default_margin" else
                     '<style>html,body{margin:0;width:240px;height:320px}</style>')
            if kind == "broken_image_document":
                await page.goto("data:image/png;base64,AA==")
            else:
                await page.set_content(style + content)
            png = await page.screenshot(type="png")
            (tmp_path / "source.png").write_bytes(png)
            (tmp_path / "source-dom.html").write_text(await page.content(), encoding="utf-8")
            snapshot = await _collect_visual_navigation_snapshot(
                page, viewport_png=png, logical_surface_enabled=True,
                logical_surface_shadow=False, surface_session_id="h5:origin-test",
            )
            (tmp_path / "snapshot.json").write_text(
                json.dumps(snapshot, ensure_ascii=False), encoding="utf-8",
            )
            fixture = json.loads((Path(__file__).parent / "fixtures" /
                "h5_visual_rejected_frame_20261013.json").read_text(encoding="utf-8"))
            original = base64.b64decode(fixture["failed_case"]["png_base64"])
            (tmp_path / "historical.png").write_bytes(original)
            (tmp_path / "comparison.json").write_text(json.dumps({
                "kind": kind, "generated_sha256": hashlib.sha256(png).hexdigest(),
                "historical_sha256": hashlib.sha256(original).hexdigest(),
                "identical_bytes": png == original,
                "historical_dom_available": False,
            }), encoding="utf-8")
            origin = snapshot["visual_navigation_capture"].get("frame_origin")
            assert isinstance(origin, dict), "当前采集丢失位图来源DOM/绘制事实，无法定位破图"
            assert origin["url"] == ("data:image/png;base64,AA=="
                                     if kind == "broken_image_document" else "about:blank")
            if kind.startswith("broken"):
                assert origin["canvas_count"] == 0
                assert origin["broken_image_count"] == 1
            else:
                assert origin["canvas_count"] == 1
                assert origin["canvases"][0]["sample_rgba"] == (
                    [245, 245, 245, 255] if kind == "painted_canvas" else [0, 0, 0, 0]
                )
        finally:
            await browser.close()


async def test_diagnostic_does_not_call_site_encoder_or_claim_a_webgl_context():
    from services.api.app.infrastructure.browser.h5_frame_origin import read_h5_frame_origin

    playwright_api = pytest.importorskip("playwright.async_api")
    async with playwright_api.async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        page = await browser.new_page()
        try:
            await page.set_content('<canvas width="240" height="320"></canvas><script>'
                'window.effects=0;document.querySelector("canvas").toDataURL=()=>{'
                'window.effects++;return "data:image/png;base64,AA=="};</script>')
            await read_h5_frame_origin(page)
            assert await page.evaluate("window.effects") == 0
            assert await page.evaluate("!!document.querySelector('canvas').getContext('webgl')")
        finally:
            await browser.close()
