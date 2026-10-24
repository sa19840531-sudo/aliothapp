"""采集时序变化不能用截图之后的DOM冒充截图来源。"""

import hashlib

import pytest


@pytest.mark.parametrize("change", [False, True])
async def test_real_capture_brackets_canvas_frame_instead_of_binding_late_dom(change, tmp_path):
    from playwright.async_api import async_playwright

    from services.api.app.infrastructure.browser.h5_frame_origin import capture_h5_frame

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            page = await browser.new_page(viewport={"width": 240, "height": 320})
            await page.set_content("""<style>body{margin:0}</style>
              <canvas width=240 height=320></canvas>
              <script>const c=document.querySelector('canvas');const g=c.getContext('2d');
              g.fillStyle='white';g.fillRect(0,0,240,320);</script>""")
            original = page.screenshot

            async def screenshot(**kwargs):
                png = await original(**kwargs)
                if change:
                    await page.set_content("<main>截图之后才出现的正文</main>")
                return png

            page.screenshot = screenshot
            png, origin = await capture_h5_frame(page, full_page=False)
            (tmp_path / "frame.png").write_bytes(png)
            (tmp_path / "after-dom.html").write_text(await page.content(), encoding="utf-8")
            assert origin["screenshot_sha256"] == hashlib.sha256(png).hexdigest()
            assert origin["before"]["canvas_count"] == 1
            assert origin["before"]["canvases"][0]["sample_png_sha256"]
            assert origin["atomic_frame_binding"] is False
            assert origin["status"] == ("source_changed" if change else "bracketed")
            assert origin["after"]["canvas_count"] == (0 if change else 1)
        finally:
            await browser.close()


async def test_canvas_change_without_dom_change_is_not_a_stable_frame(tmp_path):
    from playwright.async_api import async_playwright

    from services.api.app.infrastructure.browser.h5_frame_origin import capture_h5_frame

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            page = await browser.new_page(viewport={"width": 240, "height": 320})
            await page.set_content("<canvas width=240 height=320></canvas>")
            original = page.screenshot

            async def screenshot(**kwargs):
                png = await original(**kwargs)
                await page.evaluate("""() => {const g=document.querySelector('canvas')
                    .getContext('2d');g.fillStyle='red';g.fillRect(0,0,240,320)}""")
                return png

            page.screenshot = screenshot
            png, origin = await capture_h5_frame(page, full_page=False)
            (tmp_path / "blank-before-paint.png").write_bytes(png)
            assert origin["before"]["body_markup_sha256"] == origin["after"]["body_markup_sha256"]
            assert origin["status"] == "source_changed"
        finally:
            await browser.close()
