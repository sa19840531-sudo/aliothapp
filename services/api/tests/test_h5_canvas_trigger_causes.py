"""新建独占浏览器内区分GPU故障、上下文配额和正常生命周期。"""

import asyncio
import json
import os
import signal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from services.api.app.infrastructure.browser.page_capture import (
    PageCaptureMode,
    PageCaptureRequest,
    PlaywrightPageCaptureBrowser,
)

_HTML = """<style>body{margin:0}canvas{display:block}</style>
<canvas id=gl width=240 height=320></canvas><canvas id=two width=240 height=320></canvas>
<script>
window.gl=document.querySelector('#gl').getContext('webgl');
window.two=document.querySelector('#two').getContext('2d');
gl.clearColor(.96,.96,.96,1);gl.clear(gl.COLOR_BUFFER_BIT);
two.fillStyle='#f5f5f5';two.fillRect(0,0,240,320);
window.loss=[];document.addEventListener('webglcontextlost',e=>loss.push({
  trusted:e.isTrusted,type:e.type,target:e.target.id,message:e.statusMessage}),true);
document.addEventListener('contextlost',e=>loss.push({
  trusted:e.isTrusted,type:e.type,target:e.target.id}),true);
</script>"""


async def _state(page):
    return await page.evaluate("""() => ({gl_lost:gl.isContextLost(),
      two_lost:two.isContextLost(),two_pixel:[...two.getImageData(0,0,1,1).data],loss})""")


async def _owned_gpu_parent_chain(browser_pid, gpu_pid, *, read_parent):
    if (
        type(browser_pid) is not int or type(gpu_pid) is not int
        or min(browser_pid, gpu_pid) <= 1 or browser_pid == gpu_pid
    ):
        return None
    seen, chain, current = {gpu_pid}, [], gpu_pid
    for _ in range(8):
        parent = await read_parent(current)
        if type(parent) is not int or parent <= 1 or parent in seen:
            return None
        chain.append((current, parent))
        if parent == browser_pid:
            return tuple(chain)
        seen.add(parent)
        current = parent
    return None


@pytest.mark.parametrize("parents,expected", [
    ({42: 40}, ((42, 40),)),
    ({42: 41, 41: 40}, ((42, 41), (41, 40))),
    ({42: 90, 90: 1}, None),
    ({42: 41, 41: 42}, None),
    ({42: None}, None),
    ({42: 0}, None),
])
async def test_gpu_fault_requires_bounded_ancestry_to_its_owned_browser(parents, expected):
    async def read_parent(pid):
        return parents.get(pid)

    assert await _owned_gpu_parent_chain(40, 42, read_parent=read_parent) == expected


@pytest.mark.parametrize("browser_pid,gpu_pid", [(40, 40), (True, 42), (40, False), (40, -1)])
async def test_invalid_gpu_identity_is_rejected_before_process_read(browser_pid, gpu_pid):
    reader = AsyncMock(return_value=40)
    assert await _owned_gpu_parent_chain(browser_pid, gpu_pid, read_parent=reader) is None
    reader.assert_not_awaited()


async def test_gpu_ancestry_cannot_walk_unbounded_or_treat_foreign_root_as_owned():
    reader = AsyncMock(side_effect=lambda pid: pid + 1)
    assert await _owned_gpu_parent_chain(90, 42, read_parent=reader) is None
    assert reader.await_count == 8


@pytest.mark.parametrize("fault", ["gpu_exit", "context_limit", "normal_close"])
async def test_owned_gpu_exit_and_context_limit_have_distinct_physical_evidence(fault, tmp_path):
    from playwright.async_api import async_playwright

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            headless=True, args=["--max-active-webgl-contexts=2"],
        )
        cdp = await browser.new_browser_cdp_session()
        page = await browser.new_page(viewport={"width": 240, "height": 640})
        console = []
        page.on("console", lambda msg: console.append({"type": msg.type, "text": msg.text}))
        try:
            await page.set_content(_HTML)
            before = {"processes": await cdp.send("SystemInfo.getProcessInfo"),
                      "gpu": await cdp.send("SystemInfo.getInfo"), "canvas": await _state(page)}
            assert before["canvas"]["two_pixel"] == [245, 245, 245, 255]
            assert not before["canvas"]["gl_lost"]
            (tmp_path / "before.json").write_text(json.dumps(before), encoding="utf-8")
            (tmp_path / "before.png").write_bytes(await page.screenshot())
            if fault == "gpu_exit":
                processes = before["processes"]["processInfo"]
                parent = next(p["id"] for p in processes if p["type"] == "browser")
                gpu = next(p["id"] for p in processes if p["type"] == "GPU")
                observed_parents = []

                async def read_parent(pid):
                    child = await asyncio.create_subprocess_exec(
                        "ps", "-p", str(pid), "-o", "ppid=", stdout=asyncio.subprocess.PIPE,
                    )
                    output, _ = await child.communicate()
                    value = output.strip()
                    result = int(value) if child.returncode == 0 and value.isdigit() else None
                    observed_parents.append((pid, result))
                    return result

                # Linux 经 zygote 派生GPU；证明整条有界祖先链，不猜或放宽外来进程归属。
                async with asyncio.timeout(2):
                    ownership = await _owned_gpu_parent_chain(parent, gpu, read_parent=read_parent)
                before["gpu_parent_chain"] = observed_parents
                (tmp_path / "before.json").write_text(json.dumps(before), encoding="utf-8")
                assert ownership is not None, "只允许注入本测试独占浏览器的GPU后代"
                current = (await cdp.send("SystemInfo.getProcessInfo"))["processInfo"]
                assert any(p["id"] == gpu and p["type"] == "GPU" for p in current)
                assert any(p["id"] == parent and p["type"] == "browser" for p in current)
                os.kill(gpu, signal.SIGKILL)
                await page.wait_for_function("gl.isContextLost()", timeout=3000)
            elif fault == "context_limit":
                await page.evaluate("""() => {window.extra=[];for(let i=0;i<3;i++) {
                  const c=document.createElement('canvas');c.width=c.height=16;
                  c.style.display='none';document.body.append(c);extra.push(c.getContext('webgl'));
                }}""")
                await page.wait_for_function("gl.isContextLost()", timeout=3000)
            else:
                await page.close()
                page = await browser.new_page(viewport={"width": 240, "height": 640})
                await page.set_content(_HTML)
            after = {"processes": await cdp.send("SystemInfo.getProcessInfo"),
                     "gpu": await cdp.send("SystemInfo.getInfo"), "canvas": await _state(page),
                     "console": console, "browser_version": browser.version}
            (tmp_path / "after.json").write_text(json.dumps(after), encoding="utf-8")
            try:
                (tmp_path / "after.png").write_bytes(await page.screenshot())
            except Exception as exc:
                # 原生GPU退出可能连截图面都不可用，明确记录，不重试造出健康帧。
                (tmp_path / "screenshot-error.txt").write_text(str(exc), encoding="utf-8")
                assert fault == "gpu_exit" and "Unable to capture screenshot" in str(exc)
            before_gpu = {p["id"] for p in before["processes"]["processInfo"] if p["type"] == "GPU"}
            after_gpu = {p["id"] for p in after["processes"]["processInfo"] if p["type"] == "GPU"}
            if fault == "gpu_exit":
                assert before_gpu.isdisjoint(after_gpu)
                assert after["canvas"]["gl_lost"]
                assert after["canvas"]["two_lost"]
                assert after["canvas"]["two_pixel"] == [0, 0, 0, 0]
                assert any(e["type"] == "contextlost" and e["trusted"]
                           for e in after["canvas"]["loss"])
            elif fault == "context_limit":
                assert before_gpu == after_gpu
                assert after["canvas"]["gl_lost"]
                assert not after["canvas"]["two_lost"]
                assert after["canvas"]["two_pixel"] == [245, 245, 245, 255]
                assert any("Too many active WebGL" in item["text"] for item in console)
            else:
                assert before_gpu == after_gpu
                assert not after["canvas"]["gl_lost"] and not after["canvas"]["two_lost"]
        finally:
            await browser.close()


@pytest.mark.parametrize("fault", ["identity_failure", "identity_cancel", "healthy"])
async def test_session_start_identity_failure_cleans_browser_and_driver(monkeypatch, fault):
    from services.api.app.infrastructure.browser import page_capture as module

    page = SimpleNamespace()
    context = SimpleNamespace(new_page=AsyncMock(return_value=page))
    browser = SimpleNamespace(new_context=AsyncMock(return_value=context), close=AsyncMock())
    playwright = SimpleNamespace(stop=AsyncMock())
    def factory():
        return SimpleNamespace(start=AsyncMock(return_value=playwright))
    adapter = PlaywrightPageCaptureBrowser()
    monkeypatch.setattr(adapter, "_launch_browser", AsyncMock(return_value=browser))
    monkeypatch.setattr(module, "install_h5_canvas_observer", AsyncMock())
    monkeypatch.setattr(module, "_attach_observers", lambda *_a, **_k: None)
    readonly = "services.api.app.infrastructure.browser.h5_readonly_display"
    monkeypatch.setattr(readonly + ".install_h5_readonly_display_guard", AsyncMock())
    error = (RuntimeError("identity failed") if fault == "identity_failure"
             else asyncio.CancelledError())
    monkeypatch.setattr(module.H5BrowserStopGuard, "bind", AsyncMock(
        side_effect=None if fault == "healthy" else error,
    ))
    request = PageCaptureRequest(mode=PageCaptureMode.GOTO, session_id="h5:startup-proof")
    if fault == "healthy":
        result = await adapter._create_session(factory, request=request)
        assert result.browser is browser
        browser.close.assert_not_awaited()
        playwright.stop.assert_not_awaited()
    else:
        with pytest.raises((RuntimeError, asyncio.CancelledError)):
            await adapter._create_session(factory, request=request)
        browser.close.assert_awaited_once()
        playwright.stop.assert_awaited_once()


async def test_real_identity_failure_does_not_leave_an_owned_chromium_alive(monkeypatch, tmp_path):
    from playwright.async_api import async_playwright

    from services.api.app.infrastructure.browser import page_capture as module

    adapter = PlaywrightPageCaptureBrowser()
    original = adapter._launch_browser
    owned = []

    async def launch(playwright):
        browser = await original(playwright)
        owned.append((browser, playwright))
        return browser

    monkeypatch.setattr(adapter, "_launch_browser", launch)
    monkeypatch.setattr(module.H5BrowserStopGuard, "bind", AsyncMock(
        side_effect=RuntimeError("controlled identity failure"),
    ))
    try:
        with pytest.raises(RuntimeError, match="controlled"):
            await adapter._create_session(async_playwright, request=PageCaptureRequest(
                mode=PageCaptureMode.GOTO, session_id="h5:real-startup-failure",
            ))
        observed = {"owned_browsers_connected": sum(b.is_connected() for b, _ in owned)}
        (tmp_path / "startup-resources.json").write_text(json.dumps(observed), encoding="utf-8")
        assert observed["owned_browsers_connected"] == 0
    finally:
        for browser, playwright in owned:
            await browser.close()
            await playwright.stop()
