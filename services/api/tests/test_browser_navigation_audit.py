"""导航审计保留真实响应和自然重定向血缘，不能由落点倒推跳转原因。"""

from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from playwright.async_api import async_playwright

from services.api.app.application.capture_coordination import CapturePhase, CaptureWriteRequest
from services.api.app.application.interaction_runtime import (
    InMemoryActionEvidenceBuffer,
    PendingActionEvidence,
)
from services.api.app.domain.interaction import (
    CaptureDirective,
    InteractionResult,
    InteractionResultStatus,
    SurfaceKind,
)
from services.api.app.infrastructure.browser import page_capture
from services.api.app.infrastructure.browser.capture_runtime import BrowserCaptureWriter
from services.api.app.infrastructure.browser.page_capture import (
    PageCaptureMode,
    PageCaptureRequest,
    PlaywrightPageCaptureBrowser,
    _attach_observers,
)
from services.api.tests.test_web_capture_runtime import RecordingPersistence

TARGET_URL = "https://source.alioth.test/"
OUTSIDE_URL = "https://outside.alioth.test/"


@pytest.fixture
def fast_capture(monkeypatch):
    # 本组只验证导航与网络审计；动画静默等待和页面语义另有专项测试。
    monkeypatch.setattr(page_capture, "_wait_for_page_settle", AsyncMock(return_value=True))
    monkeypatch.setattr(page_capture, "_collect_artifacts", AsyncMock(return_value=[]))


class _CaptureAdapter:
    def __init__(self, page, context, *, cli_session=None):
        self.page = page
        self.context = context
        self.cli_session = cli_session
        self.console = []
        self.network = []
        self.browser = PlaywrightPageCaptureBrowser()

    async def capture(self, request):
        return await self.browser._capture_on_page(
            request=request,
            context=self.context,
            page=self.page,
            console_logs=self.console,
            network_requests=self.network,
            timeout_error_type=TimeoutError,
        )

    async def navigate(self, url=TARGET_URL):
        return await self.capture(
            PageCaptureRequest(
                mode=PageCaptureMode.GOTO,
                url=url,
                session_id="h5:audit-run",
                startup_policy="direct_home",
                capture_dom=False,
                capture_visible_text=False,
                capture_screenshot=False,
                _cli_session=self.cli_session,
            )
        )


def _navigation_observation(result):
    return next(item for item in result.action_trace if item["action"] == "navigation_observation")


async def _persist_merged_after(adapter, action_result):
    buffer = InMemoryActionEvidenceBuffer()
    await buffer.put(
        PendingActionEvidence(
            interaction=InteractionResult(
                task_id="task-1",
                session_id="h5:audit-run",
                intent_id="navigation-audit",
                action_execution_id="action-audit",
                source_capture_id="capture-before",
                status=InteractionResultStatus.EVIDENCE_PENDING,
            ),
            browser_result=action_result,
        )
    )
    persistence = RecordingPersistence()
    writer = BrowserCaptureWriter(browser=adapter, persistence=persistence, evidence_buffer=buffer)
    await writer.capture(
        CaptureWriteRequest(
            task_id="task-1",
            agent_run_id="audit-run",
            session_id="h5:audit-run",
            surface_kind=SurfaceKind.WEB,
            intent_id="navigation-audit",
            phase=CapturePhase.AFTER_ACTION,
            directive=CaptureDirective(role="after_action", artifact_types=("action_trace",)),
            parent_capture_id="capture-before",
            action_execution_id="action-audit",
            session_command_sequence=1,
            finalization_epoch=1,
            deadline=datetime.now(UTC) + timedelta(seconds=30),
        )
    )
    return persistence.calls[0]["result"]


@pytest.fixture
def loopback_redirect():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/source":
                self.send_response(302)
                self.send_header("Location", f"http://localhost:{self.server.server_port}/outside")
                self.end_headers()
            elif self.path == "/outside":
                body = "<html><body>落地页</body></html>".encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_error(404)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield (
            f"http://127.0.0.1:{server.server_port}/source",
            f"http://localhost:{server.server_port}/outside",
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


async def test_real_302_redirect_audit_and_navigation_har_survive_after_persistence(
    fast_capture, loopback_redirect
):
    target_url, outside_url = loopback_redirect
    async with async_playwright() as playwright:
        chromium = await playwright.chromium.launch(headless=True)
        context = await chromium.new_context()
        try:
            page = await context.new_page()
            adapter = _CaptureAdapter(page, context)
            _attach_observers(page, adapter.console, adapter.network)
            action_result = await adapter.navigate(target_url)
            expected = {
                "action": "navigation_observation",
                "requested_url": target_url,
                "response_url": outside_url,
                "status_code": 200,
                "redirect_chain": [target_url, outside_url],
                "final_url": outside_url,
            }
            assert _navigation_observation(action_result) == expected
            merged = await _persist_merged_after(adapter, action_result)
            assert merged.final_url == outside_url
            assert _navigation_observation(merged) == expected
            trace = next(item for item in merged.artifacts if item.artifact_type == "action_trace")
            assert expected in trace.content
            har = next(item for item in merged.artifacts if item.artifact_type == "har")
            responses = [
                item
                for item in har.content["entries"]
                if item["event"] == "response" and item["url"] in (target_url, outside_url)
            ]
            assert [(item["url"], item["status"]) for item in responses] == [
                (target_url, 302),
                (outside_url, 200),
            ]
        finally:
            await context.close()
            await chromium.close()


class _Page:
    def __init__(self, response, *, final_url):
        self.response = response
        self.final_url = final_url
        self.url = "about:blank"
        self.requested = []

    def set_default_timeout(self, timeout):
        pass

    async def goto(self, url, **kwargs):
        self.requested.append(url)
        self.url = self.final_url
        return self.response

    async def title(self):
        return "落地页"


def _response(status):
    return SimpleNamespace(
        status=status,
        url=TARGET_URL,
        request=SimpleNamespace(
            url=TARGET_URL, method="GET", resource_type="document", redirected_from=None
        ),
    )


async def test_cli_304_keeps_observed_status_and_original_receipt(fast_capture):
    page = _Page(_response(304), final_url=TARGET_URL)
    receipt = {
        "command": "goto",
        "argument_hash": "original-cli-command-hash",
        "command_succeeded": True,
        "arrival_verified": False,
    }

    async def navigate(current_page, url):
        return receipt, await current_page.goto(url)

    cli = SimpleNamespace(
        mode="actions",
        process=SimpleNamespace(is_quarantined=False, require_action_session=lambda: None),
        set_initial_url=lambda url: None,
        navigate=navigate,
        observe=AsyncMock(side_effect=RuntimeError("本测试不启用只读对比")),
    )
    adapter = _CaptureAdapter(page, SimpleNamespace(pages=[page]), cli_session=cli)
    action_result = await adapter.navigate()
    audit = _navigation_observation(action_result)
    assert audit["status_code"] == 304
    assert audit["response_url"] == TARGET_URL
    assert audit["redirect_chain"] == []
    assert action_result.status_code == 304
    assert action_result.action_trace[0] == {"action": "navigate", **receipt}
    merged = await _persist_merged_after(adapter, action_result)
    assert _navigation_observation(merged)["status_code"] == 304
    assert merged.action_trace[0]["argument_hash"] == receipt["argument_hash"]
    assert merged.action_trace[0]["arrival_verified"] is False
    assert page.requested == [TARGET_URL]


async def test_missing_response_does_not_invent_status_or_redirect_chain(fast_capture):
    page = _Page(None, final_url=OUTSIDE_URL)
    adapter = _CaptureAdapter(page, SimpleNamespace(pages=[page]))
    result = await adapter.navigate()
    assert _navigation_observation(result) == {
        "action": "navigation_observation",
        "requested_url": TARGET_URL,
        "response_url": None,
        "status_code": None,
        "redirect_chain": [],
        "final_url": OUTSIDE_URL,
    }
    assert result.status_code is None
    assert page.requested == [TARGET_URL]
