"""用隔离浏览器的原生上下文丢失复现破图，不伪造截图或放宽视觉门禁。"""

import base64
import hashlib
import json
from pathlib import Path

import pytest


@pytest.mark.parametrize("lost", [True, False])
async def test_native_lost_canvas_frame_matches_saved_failure(lost, tmp_path):
    from playwright.async_api import async_playwright

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        page = await browser.new_page(viewport={"width": 240, "height": 320})
        try:
            await page.set_content("""<style>html,body{margin:0;width:240px;height:320px}
              canvas{display:block;width:240px;height:320px}</style>
              <canvas width=240 height=320></canvas><script>
              window.canvas=document.querySelector('canvas');
              window.gl=canvas.getContext('webgl');window.lossEvents=[];
              canvas.addEventListener('webglcontextlost',e=>{lossEvents.push(e.isTrusted)});
              gl.clearColor(.96,.96,.96,1);gl.clear(gl.COLOR_BUFFER_BIT);
              </script>""")
            assert await page.evaluate("gl !== null")
            if lost:
                await page.evaluate("gl.getExtension('WEBGL_lose_context').loseContext()")
                await page.wait_for_function("gl.isContextLost() && lossEvents.includes(true)")
            state = await page.evaluate("({lost:gl.isContextLost(),events:lossEvents})")
            png = await page.screenshot(type="png")
            (tmp_path / "frame.png").write_bytes(png)
            (tmp_path / "dom.html").write_text(await page.content(), encoding="utf-8")
            fixture = json.loads((Path(__file__).parent / "fixtures" /
                "h5_visual_rejected_frame_20261013.json").read_text(encoding="utf-8"))
            historical = base64.b64decode(fixture["failed_case"]["png_base64"])
            (tmp_path / "comparison.json").write_text(json.dumps({
                "state": state, "sha256": hashlib.sha256(png).hexdigest(),
                "historical_sha256": fixture["failed_case"]["sha256"],
                "identical_bytes": png == historical,
            }), encoding="utf-8")
            assert state["lost"] is lost
            assert (png == historical) is lost
        finally:
            await browser.close()


@pytest.mark.parametrize("case", ["native_loss", "healthy", "forged", "small_ad"])
async def test_capture_native_canvas_loss_is_not_published_as_loaded(case, tmp_path):
    from playwright.async_api import async_playwright

    from services.api.app.infrastructure.browser.h5_frame_origin import install_h5_canvas_observer
    from services.api.app.infrastructure.browser.page_capture import (
        PageCaptureMode,
        PageCaptureRequest,
        PlaywrightPageCaptureBrowser,
    )

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        page = await browser.new_page(viewport={"width": 240, "height": 320})
        observer = None
        try:
            size = 20 if case == "small_ad" else 240
            await page.set_content(f"""<style>body{{margin:0}}</style>
              <canvas width={size} height={20 if case == 'small_ad' else 320}></canvas>
              <script>window.canvas=document.querySelector('canvas');
              window.gl=canvas.getContext('webgl');</script>""")
            observer = await install_h5_canvas_observer(page)
            if case in {"native_loss", "small_ad"}:
                await page.evaluate("gl.getExtension('WEBGL_lose_context').loseContext()")
                await page.wait_for_function("gl.isContextLost()")
                await page.wait_for_timeout(20)
            elif case == "forged":
                await page.evaluate("canvas.dispatchEvent(new Event('webglcontextlost'))")
            result = await PlaywrightPageCaptureBrowser()._capture_on_page(
                request=PageCaptureRequest(
                    mode=PageCaptureMode.OBSERVE, session_id="h5:native-loss",
                ),
                context=page.context, page=page, console_logs=[], network_requests=[],
                timeout_error_type=TimeoutError,
            )
            screenshots = [a for a in result.artifacts if a.artifact_type == "screenshot"]
            assert screenshots, "故障帧也要保留，不抹掉采集证据"
            (tmp_path / "result.json").write_text(json.dumps(result.to_dict()), encoding="utf-8")
            if case == "native_loss":
                assert result.execution_status == "failed"
                assert result.metrics["browser_rendering_error"] == "H5_CANVAS_CONTEXT_LOST"
                snapshots = [
                    a.content for a in result.artifacts if a.artifact_type == "ui_snapshot"
                ]
                assert snapshots[0]["capture_degraded"] == "H5_CANVAS_CONTEXT_LOST"
                assert screenshots[0].content["frame_origin"]["status"] == "renderer_unavailable"
            else:
                assert result.execution_status == "success"
                assert "browser_rendering_error" not in result.metrics
        finally:
            if observer is not None:
                await observer.detach()
            await browser.close()


async def test_canvas_observer_runs_before_real_navigation_scripts(tmp_path):
    from urllib.parse import quote

    from services.api.app.infrastructure.browser.page_capture import (
        PageCaptureMode,
        PageCaptureRequest,
        PlaywrightPageCaptureBrowser,
    )

    html = """<meta name=viewport content="width=device-width,initial-scale=1">
      <style>html,body{margin:0;width:240px;height:320px}</style>
      <canvas width=240 height=320></canvas><script>
      const gl=document.querySelector('canvas').getContext('webgl');
      gl.getExtension('WEBGL_lose_context').loseContext();</script>"""
    adapter = PlaywrightPageCaptureBrowser()
    session = "h5:early-context-loss"
    try:
        result = await adapter.capture_with_timeout(
            PageCaptureRequest(mode=PageCaptureMode.GOTO, session_id=session,
                url="data:text/html," + quote(html), viewport_width=240, viewport_height=320),
            timeout_seconds=10,
        )
        (tmp_path / "early-loss-result.json").write_text(
            json.dumps(result.to_dict()), encoding="utf-8",
        )
        assert result.execution_status == "failed"
        origins = result.metrics["h5_screenshot_origins"]
        assert origins[0]["frame_origin"]["status"] == "renderer_unavailable"
    finally:
        await adapter.close_session(session)
