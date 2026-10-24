"""随机双 origin 的真实 Chrome 见证；只替换到本地 HTTP 服务的传输边界。"""

import asyncio
import os
import socket
import threading
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from uuid import uuid4

import pytest


@pytest.fixture
async def chrome_page():
    from playwright.async_api import async_playwright

    executable = os.getenv("ALIOTH_WEB_TEST_BROWSER") or next(
        (
            path
            for path in (
                "C:/Program Files/Google/Chrome/Application/chrome.exe",
                "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
            )
            if Path(path).is_file()
        ),
        None,
    )
    async with async_playwright() as playwright:
        if executable is None and not Path(playwright.chromium.executable_path).is_file():
            pytest.skip("缺少真实 Chrome/Chromium")
        browser = await playwright.chromium.launch(
            executable_path=executable,
            headless=True,
            args=["--host-resolver-rules=MAP *.test 127.0.0.1", "--no-proxy-server"],
        )
        try:
            yield await browser.new_page()
        finally:
            await browser.close()


@pytest.fixture
def two_origins():
    names = (f"cold-{uuid4().hex}.test", f"foreign-{uuid4().hex}.test")
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append((self.headers.get("Host"), self.path))
            if self.path == "/redirect":
                self.send_response(302)
                self.send_header("Location", f"http://{names[1]}:{servers[1].server_port}/landing")
                self.end_headers()
                return
            body = (
                '<main><a href="/menu">菜单</a><a href="/redirect">跨域重定向</a>'
                f'<a class="ad" href="http://{names[1]}:{servers[1].server_port}/ad">广告</a></main>'
                if self.path == "/"
                else '<main><a href="/poison">外站内部菜单</a></main>'
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    servers = [ThreadingHTTPServer(("127.0.0.1", 0), Handler) for _ in names]
    threads = [threading.Thread(target=server.serve_forever, daemon=True) for server in servers]
    for thread in threads:
        thread.start()
    try:
        yield (
            names,
            dict(zip(names, [server.server_port for server in servers], strict=True)),
            requests,
        )
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()
        for thread in threads:
            thread.join(timeout=3)
            assert not thread.is_alive()


@pytest.mark.asyncio
async def test_controlled_origins_prove_cross_origin_and_ad_contracts(
    chrome_page, two_origins, monkeypatch
):
    from services.api.app.application.web_frontier_scheduler import (
        RuntimeAnchor,
        WebFrontierScheduler,
    )
    from services.api.app.application.web_runtime_discovery import (
        CanonicalSiteScope,
        TrustedWebCaptureAuthority,
        UntrustedWebCaptureError,
        WebCapture,
    )
    from services.api.app.domain.exploration.web_frontier_policy import WebFrontierPolicy
    from services.api.app.infrastructure.browser import ad_safe_transport
    from services.api.app.infrastructure.browser.ad_isolated_page import isolated_ad_page
    from services.api.app.infrastructure.browser.page_capture import (
        BlockedPageCaptureActionError,
        PageCaptureMode,
        PageCaptureRequest,
        _run_single_trigger,
    )

    names, ports, requests = two_origins
    source, foreign = (f"http://{name}:{ports[name]}" for name in names)
    page = chrome_page
    async with asyncio.timeout(30):
        await page.goto(source + "/", wait_until="domcontentloaded", timeout=5000)
        authority = TrustedWebCaptureAuthority(
            site_scope=CanonicalSiteScope.bind(source + "/", (source + "/",)),
            session_id="web:controlled",
            finalization_epoch=1,
        )
        capture = WebCapture(
            capture_id="home",
            session_id="web:controlled",
            finalization_epoch=1,
            requested_url=source + "/",
            final_url=page.url,
            current=True,
            ready=True,
            capture_state_fingerprint="home",
            depth=0,
        )
        hrefs = await page.locator("a").evaluate_all(
            "nodes => nodes.map(n => n.getAttribute('href'))"
        )
        scheduler = WebFrontierScheduler(
            policy=WebFrontierPolicy.for_new_run(), capture_authority=authority, menu_limit=10
        )
        first = scheduler.ingest(capture, tuple(RuntimeAnchor(href=href) for href in hrefs))
        assert source + "/menu" in first.frontier_urls
        assert foreign + "/ad" not in first.frontier_urls
        await page.goto(source + "/redirect", wait_until="domcontentloaded", timeout=5000)
        assert page.url == foreign + "/landing"
        foreign_capture = replace(
            capture,
            capture_id="foreign",
            requested_url=source + "/redirect",
            final_url=page.url,
            capture_state_fingerprint="foreign",
        )

        def forbidden_reader(_capture):
            raise AssertionError("跨域落地不得读取 DOM")

        scheduler.anchor_reader = forbidden_reader
        with pytest.raises(UntrustedWebCaptureError, match="WEB_DISCOVERY_SOURCE_ORIGIN_UNTRUSTED"):
            await scheduler.discover_once(foreign_capture)
        assert foreign + "/poison" not in first.frontier_urls
        await page.goto(source + "/", wait_until="domcontentloaded", timeout=5000)
        recovered = replace(
            capture,
            capture_id="recovered",
            final_url=page.url,
            capture_state_fingerprint="recovered",
        )
        authority.assert_discoverable(recovered)
        resumed = scheduler.ingest(recovered, (RuntimeAnchor(href="/new-menu"),))
        assert source + "/new-menu" in resumed.frontier_urls
        assert foreign + "/poison" not in resumed.frontier_urls

        async def resolve_local(host, port):
            assert host in ports
            return ["93.184.216.34"]

        def connect_local(address, timeout):
            assert address[0] == "93.184.216.34"
            return socket.create_connection(("127.0.0.1", ports[names[1]]), timeout=timeout)

        monkeypatch.setattr(ad_safe_transport, "_resolve_addresses", resolve_local)
        monkeypatch.setattr(ad_safe_transport, "_connect_pinned", connect_local)
        browser = page.context.browser
        initial_contexts = tuple(browser.contexts)
        await page.context.add_cookies([{"name": "business", "value": "private", "url": source}])
        async with asyncio.timeout(15):
            async with isolated_ad_page(
                browser=browser, url=foreign + "/ad", source_url=source + "/", timeout_ms=5000
            ) as (ad, _transport):
                isolated = [
                    context for context in browser.contexts if context not in initial_contexts
                ]
                assert len(isolated) == 1
                assert isolated[0] is not page.context
                assert await isolated[0].cookies() == []
                assert ad.url == foreign + "/ad"
                assert (await ad.screenshot()).startswith(b"\x89PNG")
                assert page.url == source + "/"
        assert tuple(browser.contexts) == initial_contexts
        before = len(requests)
        with pytest.raises(BlockedPageCaptureActionError) as caught:
            await _run_single_trigger(
                page=page,
                context=page.context,
                request=PageCaptureRequest(
                    mode=PageCaptureMode.SINGLE_TRIGGER,
                    trigger_action="click",
                    selector=".ad",
                    navigation_depth=1,
                    capture_ad_landing_evidence=True,
                ),
                action_trace=[],
                timeout_ms=1000,
            )
        assert caught.value.error_code == "WEB_AD_CLICK_FORBIDDEN"
        assert len(requests) == before
        assert page.url == source + "/"


@pytest.mark.asyncio
async def test_real_chrome_obstruction_metrics_and_full_page_are_projected(chrome_page):
    from scripts.validation.web_acceptance_source import project_database_rows
    from services.api.app.application.web_full_page_stabilization import (
        WebFullPageStabilizationPolicy,
        capture_stabilized_screenshot,
        stabilize_for_full_page,
    )
    from services.api.app.infrastructure.browser import page_capture
    from services.api.tests.test_web_v2_live_acceptance_review import full_page_rows

    page = chrome_page
    html = """<style>.popup{position:fixed;inset:10%;background:white;
    z-index:99999;border:1px solid}
    main{height:3500px}</style><main>测试正文</main>
    <div role="dialog" class="popup" id="first"><p>第一层提示</p><button class="close"
      onclick="this.parentElement.remove();document.querySelector('#second').hidden=false">关闭</button></div>
    <div role="dialog" class="popup" id="second" hidden><p>第二层提示</p><button class="close"
      onclick="this.parentElement.remove()">关闭</button></div>"""
    await page.route(
        "http://popup.test/**", lambda route: route.fulfill(content_type="text/html", body=html)
    )
    await page.goto("http://popup.test/", timeout=5000)
    trace, frames = [], []
    async with asyncio.timeout(30):
        metrics = await page_capture._clear_page_entry_obstructions(
            page=page,
            context=page.context,
            request=page_capture.PageCaptureRequest(
                mode=page_capture.PageCaptureMode.OBSERVE,
                capture_screenshot=True,
                clear_obstructions=True,
                navigation_depth=1,
            ),
            action_trace=trace,
            obstruction_frames=frames,
            timeout_ms=10000,
            timeout_error_type=TimeoutError,
        )
        assert metrics["dismissal_count"] == 2, metrics
        assert await page.locator('[role="dialog"]').count() == 0
        stabilized = await stabilize_for_full_page(page, WebFullPageStabilizationPolicy())
        png, _audit = await capture_stabilized_screenshot(page, stabilized)
        assert png.startswith(b"\x89PNG")
    rows = full_page_rows()
    rows["captures"][0]["obstruction_chain"] = metrics
    state = project_database_rows(rows, task_id="task", run_id="run")
    assert state["capabilities"]["popup"] is True
    assert state["capabilities"]["long_page"] is True
    assert state["captures"][1]["full_page_screenshot_artifact_id"] == "full-png"
