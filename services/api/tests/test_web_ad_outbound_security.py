"""广告专用出站边界，所有网络测试只使用受控本地目标。"""

from __future__ import annotations

import os
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from services.api.app.application.web_ad_landing_evidence import select_web_ad_candidate
from services.api.app.infrastructure.browser.page_capture import _safe_direct_ad_landing_url


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/",
        "http://169.254.169.254/",
        "http://10.0.0.1/",
        "http://192.168.1.1/",
        "http://0.0.0.0/",
        "http://224.0.0.1/",
        "http://240.0.0.1/",
        "http://[::1]/",
        "http://[fe80::1]/",
        "http://[::ffff:127.0.0.1]/",
        "http://user:secret@example.net/",
        "file:///etc/hosts",
        "http://[invalid/",
    ],
)
def test_nonpublic_or_invalid_ad_is_neither_candidate_nor_direct_target(url):
    assert _safe_direct_ad_landing_url(url) is None
    assert (
        select_web_ad_candidate(
            registered_domain="source.example",
            capture_snapshots=(
                (
                    "capture",
                    {
                        "elements": [
                            {
                                "href": url,
                                "class_name": "banner-ad",
                                "visible": True,
                            }
                        ]
                    },
                ),
            ),
        )
        is None
    )


@pytest.fixture
def local_target():
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            requests.append(self.path)
            client_redirects = {
                "/computed-redirect": (
                    b'<script>const p=["land","ing"].join("");'
                    b'setTimeout(()=>location.assign("http://landing.example/"+p),50)</script>'
                ),
                "/computed-private": (
                    b'<script>const host=[127,0,0,1].join(".");'
                    b'setTimeout(()=>location.assign("http://"+host+"/never"),50)</script>'
                ),
                "/script-redirect": (
                    b'<script>window.location.href="http://landing.example/landing";</script>'
                ),
                "/meta-redirect": (
                    b'<meta http-equiv="refresh" content="0; url=/landing">'
                ),
            }
            if self.path in client_redirects:
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(client_redirects[self.path])
                return
            if self.path.startswith("/large/"):
                size = int(self.path.rsplit("/", 1)[-1])
                self.send_response(200)
                if not any(marker in self.path for marker in ("/stream/", "/unbounded/")):
                    self.send_header("Content-Length", str(size))
                self.end_headers()
                try:
                    self.wfile.write(b"x" * size)
                except (BrokenPipeError, ConnectionResetError):
                    pass
                return
            redirects = {
                "/private": "http://169.254.169.254/never",
                "/credential": "http://user:secret@public.example/never",
                "/redirect": "/landing",
                "/loop": "/loop",
            }
            if self.path in redirects:
                self.send_response(302)
                self.send_header("Location", redirects[self.path])
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(
                b'<html><body>controlled landing<img src="http://127.0.0.1/never">'
                b'<script>window.executed=true;fetch("http://127.0.0.1/never")</script></body></html>'
            )

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server.server_port, requests
    server.shutdown()
    server.server_close()
    thread.join()


def controlled_transport(monkeypatch, local_target, answers=None):
    from services.api.app.infrastructure.browser import ad_safe_transport as module

    port, requests = local_target
    answers = answers or [["8.8.8.8"]]
    resolutions = iter(answers)
    last = answers[-1]

    async def resolve(host, target_port):
        return next(resolutions, last)

    connect = socket.create_connection

    def controlled_connect(address, timeout):
        assert address[0] == "8.8.8.8"
        return connect(("127.0.0.1", port), timeout)

    monkeypatch.setattr(module, "_resolve_addresses", resolve)
    monkeypatch.setattr(module, "_connect_pinned", controlled_connect)
    return module.AdSafeTransport(timeout_seconds=5), requests


@pytest.mark.asyncio
async def test_transport_follows_validated_relative_redirect(local_target, monkeypatch):
    transport, requests = controlled_transport(monkeypatch, local_target)
    result = await transport.fetch("http://public.example/redirect", resource_kind="document")
    assert result.url == "http://public.example/landing"
    assert b"controlled landing" in result.body
    assert requests == ["/redirect", "/landing"]
    assert [entry["status"] for entry in transport.har_entries] == [302, 200]
    assert all(entry["resource_type"] == "document" for entry in transport.har_entries)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("path", "expected_url"),
    [
        ("script-redirect", "http://landing.example/landing"),
        ("meta-redirect", "http://public.example/landing"),
    ],
)
async def test_transport_follows_validated_client_document_redirect(
    local_target, monkeypatch, path, expected_url
):
    """广告入口的 location/meta 跳转允许跨域，但每一跳仍走固定地址校验。"""

    transport, requests = controlled_transport(monkeypatch, local_target)
    result = await transport.fetch(f"http://public.example/{path}", resource_kind="document")

    assert result.url == expected_url
    assert requests == [f"/{path}", "/landing"]
    assert [entry["status"] for entry in transport.har_entries] == [200, 200]
    assert transport.har_entries[0]["redirect"] is True
    assert transport.har_entries[0]["redirect_kind"] == "client_document"


@pytest.mark.asyncio
async def test_transport_har_is_bounded_and_drops_query_headers_and_body(local_target, monkeypatch):
    transport, requests = controlled_transport(monkeypatch, local_target)
    await transport.fetch("http://public.example/landing?token=secret", resource_kind="document")

    assert requests == ["/landing?token=secret"]
    assert transport.har_entries == [
        {
            "index": 0,
            "method": "GET",
            "resource_type": "document",
            "url": "http://public.example/landing",
            "status": 200,
            "body_size": len(
                b'<html><body>controlled landing<img src="http://127.0.0.1/never">'
                b'<script>window.executed=true;fetch("http://127.0.0.1/never")</script></body></html>'
            ),
            "redirect": False,
            "duration_ms": transport.har_entries[0]["duration_ms"],
        }
    ]
    assert isinstance(transport.har_entries[0]["duration_ms"], int)
    assert 0 <= transport.har_entries[0]["duration_ms"] <= 30_000
    assert "secret" not in repr(transport.har_entries)


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming_last", [False, True])
async def test_total_budget_blocks_next_request_and_each_stream_read(
    local_target,
    monkeypatch,
    streaming_last,
):
    from services.api.app.application.web_ad_outbound_policy import AdOutboundDenied
    from services.api.app.infrastructure.browser import ad_safe_transport as module

    transport, requests = controlled_transport(monkeypatch, local_target)
    read_sizes = []
    real_read = module.http.client.HTTPResponse.read1

    def tracked_read(response, size):
        chunk = real_read(response, size)
        read_sizes.append(len(chunk))
        return chunk

    monkeypatch.setattr(module.http.client.HTTPResponse, "read1", tracked_read)
    size, count = (6 * 1024 * 1024, 5) if streaming_last else (8 * 1024 * 1024, 4)
    for _ in range(count):
        prefix = "unbounded/" if streaming_last else ""
        result = await transport.fetch(f"http://public.example/large/{prefix}{size}")
        assert len(result.body) == size
    if streaming_last:
        with pytest.raises(AdOutboundDenied, match="TOTAL_BYTES_LIMIT"):
            await transport.fetch("http://public.example/large/stream/8388608")
        assert sum(read_sizes) == 32 * 1024 * 1024
        assert len(requests) == 6
    with pytest.raises(AdOutboundDenied, match="TOTAL_BYTES_LIMIT"):
        await transport.fetch("http://public.example/large/8388608")
    assert sum(read_sizes) == 32 * 1024 * 1024
    assert len(requests) == (6 if streaming_last else 4)


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["private", "credential", "loop"])
async def test_redirect_is_rejected_before_unsafe_connection(local_target, monkeypatch, path):
    from services.api.app.application.web_ad_outbound_policy import AdOutboundDenied

    transport, requests = controlled_transport(monkeypatch, local_target)
    with pytest.raises(AdOutboundDenied):
        await transport.fetch(f"http://public.example/{path}")
    assert "/never" not in requests
    assert len(requests) <= 6


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "answers",
    [
        [["8.8.8.8", "127.0.0.1"]],
        [["169.254.169.254"]],
        [[]],
        [["8.8.8.8"], ["1.1.1.1"]],
        [["8.8.8.8"], ["127.0.0.1"]],
    ],
)
async def test_dns_nonpublic_mixed_empty_and_rebinding_fail_closed(
    local_target, monkeypatch, answers
):
    from services.api.app.application.web_ad_outbound_policy import AdOutboundDenied

    transport, requests = controlled_transport(monkeypatch, local_target, answers)
    with pytest.raises(AdOutboundDenied):
        await transport.fetch("http://public.example/redirect")
    assert len(requests) <= 1


@pytest.mark.asyncio
async def test_https_downgrade_denied_without_network(local_target, monkeypatch):
    from services.api.app.application.web_ad_outbound_policy import AdOutboundDenied

    transport, requests = controlled_transport(monkeypatch, local_target)
    with pytest.raises(AdOutboundDenied, match="HTTPS_DOWNGRADE"):
        await transport.fetch(
            "http://public.example/landing", previous_url="https://source.example/"
        )
    assert not requests


@pytest.mark.asyncio
@pytest.mark.parametrize('javascript', [False, True])
async def test_real_chromium_uses_only_safe_transport_and_isolates_source(
    local_target, monkeypatch, javascript
):
    from pathlib import Path

    from playwright.async_api import async_playwright

    from services.api.app.infrastructure.browser import ad_isolated_page
    from services.api.app.infrastructure.browser.page_capture import _collect_ui_snapshot

    executable = os.environ.get(
        "ALIOTH_TEST_CHROME_EXECUTABLE",
        "/root/.cache/ms-playwright/chromium-1234/chrome-linux64/chrome",
    )
    if not Path(executable).exists():
        pytest.skip("缺少受控 Chromium")
    transport, requests = controlled_transport(monkeypatch, local_target)
    monkeypatch.setattr(ad_isolated_page, "AdSafeTransport", lambda **kwargs: transport)
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            executable_path=executable, headless=True, args=["--no-sandbox"]
        )
        source = await browser.new_context()
        await source.add_cookies(
            [{"name": "session", "value": "source-only", "url": "http://public.example/"}]
        )
        page = await source.new_page()
        await page.set_content("<html>source</html>")
        async with ad_isolated_page.isolated_ad_page(
            browser=browser,
            url="http://public.example/redirect",
            source_url="http://source.example/",
            timeout_ms=5000,
            snapshot_collector=_collect_ui_snapshot,
            enable_javascript=javascript,
        ) as (landing, _):
            assert landing.url == "http://public.example/landing"
            isolated_context = next(
                context for context in browser.contexts if context is not source
            )
            assert await isolated_context.cookies() == []
            executed = await isolated_context.pages[0].evaluate("window.executed === true")
            assert executed is javascript
            assert "controlled landing" in await landing.content()
            ui_snapshot = await landing.ui_snapshot()
            assert ui_snapshot["schema_version"] == "ui-snapshot.v2"
            assert ui_snapshot["elements"]
            har_entries = await landing.har_entries()
            assert [entry["resource_type"] for entry in har_entries] == ["document", "document"]
            assert [entry["status"] for entry in har_entries] == [302, 200]
            assert await isolated_context.pages[0].opener() is None
            assert len(browser.contexts) == 2
        assert len(browser.contexts) == 1
        assert page.url == "about:blank"
        assert requests == ["/redirect", "/landing"]
        await browser.close()


@pytest.mark.asyncio
async def test_dns_timeout_does_not_connect(local_target, monkeypatch):
    from services.api.app.application.web_ad_outbound_policy import AdOutboundDenied
    from services.api.app.infrastructure.browser import ad_safe_transport as module

    transport, requests = controlled_transport(monkeypatch, local_target)

    async def resolve(*args):
        raise TimeoutError

    monkeypatch.setattr(module, "_resolve_addresses", resolve)
    with pytest.raises(AdOutboundDenied, match="DNS"):
        await transport.fetch("http://public.example/")
    assert not requests


@pytest.mark.asyncio
async def test_transport_falls_back_between_validated_public_dns_addresses(monkeypatch):
    """容器无 IPv6 路由时可尝试同一固定公网集合中的 IPv4，不放宽 DNS 门禁。"""
    from services.api.app.infrastructure.browser import ad_safe_transport as module

    async def resolve(_host, _port):
        return ["2600:4860:4860::8888", "8.8.8.8"]

    attempts = []

    def fetch(url, address, deadline, remaining_bytes, consume_bytes):
        attempts.append(address)
        if ":" not in address:
            raise OSError("受控环境的首选地址不可达")
        return module.AdResponse(url, 200, {"content-type": "text/html"}, b"ok")

    monkeypatch.setattr(module, "_resolve_addresses", resolve)
    monkeypatch.setattr(module, "_fetch_pinned", fetch)
    transport = module.AdSafeTransport(timeout_seconds=5)

    result = await transport.fetch("https://public.example/", resource_kind="document")

    assert result.body == b"ok"
    assert attempts == ["8.8.8.8", "2600:4860:4860::8888"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "location",
    [
        "http://public.example/downgrade",
        "https://user:secret@public.example/credential",
        "https://127.0.0.1/private",
        "file:///etc/hosts",
        "https://[::1]/private",
    ],
)
async def test_https_location_checked_before_second_connection(local_target, monkeypatch, location):
    from services.api.app.application.web_ad_outbound_policy import AdOutboundDenied
    from services.api.app.infrastructure.browser import ad_safe_transport as module

    transport, requests = controlled_transport(monkeypatch, local_target)
    calls = []

    def fetch(url, address, deadline, remaining_bytes, consume_bytes):
        calls.append(url)
        return module.AdResponse(url, 302, {"location": location}, b"")

    monkeypatch.setattr(module, "_fetch_pinned", fetch)
    with pytest.raises(AdOutboundDenied):
        await transport.fetch("https://public.example/start")
    assert len(calls) == 1 and not requests


@pytest.mark.asyncio
async def test_real_ad_adapter_persists_desktop_pair_after_policy_compression(
    local_target,
    monkeypatch,
    tmp_path,
):
    case = await _real_ad_adapter_case(local_target, monkeypatch, tmp_path)
    assert case["receipt"].source_restored is True
    assert case["frame_sizes"] == [(1920, 1080), (1920, 1080)]
    assert case["compression"] == ["png_lossless_optimize", "png_lossless_optimize"]
    assert case["sha_valid"] is True
    assert case["phase_status"] == "completed"


@pytest.mark.asyncio
async def test_real_source_timer_popup_is_closed_and_invalidates_ad_return(
    local_target,
    monkeypatch,
    tmp_path,
):
    case = await _real_ad_adapter_case(local_target, monkeypatch, tmp_path, source_popup=True)
    assert case["source_pages"] == 1
    assert case["popup_closed"] is True
    assert case["receipt"].source_restored is False
    assert case["phase_status"] == "failed"
    assert case["phase_reason"] == "WEB_AD_LANDING_RETURN_FAILED"
    assert case["finished_count"] == 1 and case["request_count"] == 1
    direct = [
        a
        for a in case["actions"]
        if a.get("action") == "navigate" and a.get("navigation_mode") == "isolated_context_goto"
    ]
    assert len(direct) == 1
    navigation = direct[0]["navigation"]
    assert navigation["new_page_count"] == 2
    assert navigation["closed_after_observation_count"] == 2
    assert navigation["isolated_new_page_count"] == 1
    assert navigation["source_context_new_page_count"] == 1
    assert navigation["source_context_contaminated"] is True
    assert direct[0]["ad_navigation_evidence"]["landing"]["url"] == "http://public.example/landing"
    assert case["frame_sizes"] == [(1920, 1080), (1920, 1080)]


async def _real_ad_adapter_case(local_target, monkeypatch, tmp_path, *, source_popup=False):
    import asyncio
    import base64
    import hashlib
    import json
    from contextlib import asynccontextmanager
    from io import BytesIO
    from pathlib import Path
    from types import SimpleNamespace
    from uuid import uuid4

    from PIL import Image
    from playwright.async_api import async_playwright

    from services.api.app.application.page_capture_persistence import PageCapturePersistenceService
    from services.api.app.application.web_ad_landing_evidence import WebAdLandingCaptureError
    from services.api.app.infrastructure.browser import ad_isolated_page, page_capture
    from services.api.app.infrastructure.browser.capture_runtime import WebAdLandingCaptureAdapter
    from services.api.app.infrastructure.browser.web_desktop_capture_policy import (
        WebDesktopCapturePolicyBrowser,
    )
    from services.api.app.infrastructure.storage.local_evidence_store import (
        LocalEvidenceArtifactStore,
    )

    executable = os.environ.get(
        "ALIOTH_TEST_CHROME_EXECUTABLE",
        "/root/.cache/ms-playwright/chromium-1234/chrome-linux64/chrome",
    )
    if not Path(executable).exists():
        pytest.skip("缺少受控 Chromium")
    transport, requests = controlled_transport(monkeypatch, local_target)
    monkeypatch.setattr(ad_isolated_page, "AdSafeTransport", lambda **kwargs: transport)
    stored = []
    finished = []

    class Repository:
        async def save_capture(self, **kwargs):
            stored.append(kwargs)
            return 1

        async def update_detection_result(self, **kwargs):
            assert kwargs["page_type"] == "ad_page"
            assert kwargs["catalog_version"] == "web-page-type-catalog.v9"
            assert kwargs["catalog_sha256"] == "sha256:" + "0" * 64
            return True

    class Gate:
        @asynccontextmanager
        async def hold(self, **kwargs):
            yield SimpleNamespace(command_sequence=1, finalization_epoch=0)

    class Claims:
        consumed = False

        async def claim(self, **kwargs):
            if self.consumed:
                return None
            self.consumed = True
            return "phase-once"

        async def finish(self, **kwargs):
            finished.append(kwargs)

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            executable_path=executable,
            headless=True,
            args=["--no-sandbox"],
        )
        try:
            source = await browser.new_context(
                viewport={"width": 1920, "height": 1080},
                device_scale_factor=1,
                is_mobile=False,
                has_touch=False,
            )
            await source.route(
                "http://source.example/**",
                lambda route: route.fulfill(
                    body=(
                        "<html><head><title>source</title></head><body>source content</body></html>"
                    ),
                    content_type="text/html",
                ),
            )
            page = await source.new_page()
            await page.goto("http://source.example/menu")
            popup_pages = []
            original_fetch = transport.fetch

            async def fetch(url, **kwargs):
                if source_popup and not popup_pages:
                    async with source.expect_page() as opened:
                        await page.evaluate("setTimeout(() => window.open('about:blank'), 10)")
                    popup_pages.append(await opened.value)
                return await original_fetch(url, **kwargs)

            monkeypatch.setattr(transport, "fetch", fetch)
            driver = page_capture.PlaywrightPageCaptureBrowser()

            class BoundBrowser:
                async def capture(self, request):
                    return await driver._capture_on_page(
                        request=request,
                        context=source,
                        page=page,
                        console_logs=[],
                        network_requests=[],
                        timeout_error_type=TimeoutError,
                    )

            store = LocalEvidenceArtifactStore(root=tmp_path)
            adapter = WebAdLandingCaptureAdapter(
                browser=WebDesktopCapturePolicyBrowser(browser=BoundBrowser()),
                persistence=PageCapturePersistenceService(
                    repository=Repository(),
                    artifact_store=store,
                    web_catalog_identity=SimpleNamespace(
                        catalog_version="web-page-type-catalog.v9",
                        catalog_sha256="sha256:" + "0" * 64,
                    ),
                ),
                session_command_gate=Gate(),
                outbound_claims=Claims(),
                timeout_seconds=25,
            )
            command = SimpleNamespace(
                task_id=str(uuid4()),
                agent_run_id=str(uuid4()),
                session_id="web:" + str(uuid4()),
                source_url=page.url,
                source_capture_id=str(uuid4()),
                destination_url="http://public.example/landing",
            )
            receipt = await adapter.capture(command)
            with pytest.raises(WebAdLandingCaptureError, match="禁止再次直开"):
                await adapter.capture(command)
            refs = dict(receipt.artifact_refs)
            pair_id = refs["source_screenshot"]
            landing_screenshot_id = refs["landing_screenshot"]
            assert pair_id != landing_screenshot_id
            artifact = next(a for a in stored[0]["artifacts"] if a.artifact_id == pair_id)
            landing_artifact = next(
                a for a in stored[0]["artifacts"] if a.artifact_id == landing_screenshot_id
            )
            raw = await store.read(artifact.storage_path)
            landing_raw = await store.read(landing_artifact.storage_path)
            with Image.open(BytesIO(landing_raw)) as landing_image:
                assert landing_image.size == (1920, 1080)
            frames = json.loads(raw)["frames"]
            sizes = []
            for frame in frames:
                with Image.open(BytesIO(base64.b64decode(frame["data"]))) as image:
                    sizes.append(image.size)
            await asyncio.sleep(0)
            return {
                "receipt": receipt,
                "frame_sizes": sizes,
                "compression": [frame["compression"] for frame in frames],
                "sha_valid": artifact.checksum == "sha256:" + hashlib.sha256(raw).hexdigest(),
                "phase_status": finished[0]["status"],
                "phase_reason": finished[0]["reason_code"],
                "finished_count": len(finished),
                "source_pages": len(source.pages),
                "popup_closed": all(p.is_closed() for p in popup_pages),
                "actions": stored[0]["action_log"],
                "request_count": len(requests),
            }
        finally:
            await browser.close()
