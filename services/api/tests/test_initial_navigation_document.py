"""首导航必须基于实际主文档，缓存命中不能被误判为失败。"""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from playwright.async_api import async_playwright

from services.api.app.application.capture_coordination import CaptureCoordinationCommand
from services.api.app.application.capture_coordinator import CaptureCoordinator
from services.api.app.application.interaction_command_validation import InteractionCommandValidator
from services.api.app.application.interaction_runtime import InMemoryActionEvidenceBuffer
from services.api.app.application.web_evidence_exit_gate import NoopEvidenceExitGate
from services.api.app.domain.interaction import ActionExecutionStatus, InteractionResultStatus
from services.api.app.infrastructure.browser import page_capture
from services.api.app.infrastructure.browser.capture_runtime import BrowserCaptureWriter
from services.api.app.infrastructure.browser.navigation_document import navigation_document_facts
from services.api.tests.test_browser_navigation_audit import _CaptureAdapter
from services.api.tests.test_browser_navigation_landing_scope import (
    OUTSIDE_URL,
    TARGET_URL,
    _CapturePersistence,
    _executor,
    _page_result,
    _request,
    _scope,
)
from services.api.tests.test_capture_coordinator import RecordingActionExecutionRepository
from services.api.tests.web_browser_test_support import launch_web_test_browser


@pytest.mark.parametrize("status_code", [200, 304, None])
def test_readable_cache_or_committed_document_is_available(status_code):
    facts = navigation_document_facts(
        final_url=TARGET_URL, status_code=status_code,
        readiness={"document_ready_state": "complete", "blank_document": False},
    )
    assert facts["inspectable"] is True
    assert facts["error_code"] is None
    assert facts["main_response_status"] == status_code


@pytest.mark.parametrize(
    "status_code,readiness,code",
    [
        (200, {"document_ready_state": "complete", "blank_document": True},
         "WEB_NAVIGATION_BLANK_DOCUMENT"),
        (304, {"document_ready_state": "complete", "blank_document": True},
         "WEB_NAVIGATION_BLANK_DOCUMENT"),
        (200, None, "WEB_NAVIGATION_DOCUMENT_UNREADABLE"),
        (200, {"document_ready_state": "loading"}, "WEB_NAVIGATION_DOCUMENT_UNREADABLE"),
        (404, {"document_ready_state": "complete", "blank_document": False},
         "WEB_NAVIGATION_HTTP_ERROR"),
        (503, {"document_ready_state": "complete", "blank_document": False},
         "WEB_NAVIGATION_HTTP_ERROR"),
        (204, {"document_ready_state": "complete", "blank_document": False},
         "WEB_NAVIGATION_HTTP_ERROR"),
    ],
)
async def test_unavailable_document_keeps_true_after_evidence(status_code, readiness, code):
    result = _page_result(TARGET_URL, status_code=status_code)
    result.metrics["navigation_document"] = navigation_document_facts(
        final_url=TARGET_URL, status_code=status_code, readiness=readiness,
    )
    browser = SimpleNamespace(capture=AsyncMock(return_value=result))
    reader = SimpleNamespace(read_command_scope=AsyncMock(return_value=_scope()))
    buffer = InMemoryActionEvidenceBuffer()
    interaction = await _executor(browser, reader, buffer).execute(_request())
    assert interaction.status is InteractionResultStatus.FAILED
    assert interaction.error_code == code
    assert interaction.retryable is False
    assert interaction.state_summary["post_action_evidence_available"] is True
    assert interaction.state_diff["arrival_verified"] is False
    pending = await buffer.get(action_execution_id=interaction.action_execution_id)
    assert pending.browser_result.final_url == TARGET_URL
    assert pending.browser_result.metrics["navigation_scope"]["verified"] is True
    assert pending.browser_result.metrics["diagnostic_capture"] is True
    assert pending.browser_result.metrics["navigation_scope"]["error_code"] == code
    assert pending.browser_result.artifacts == result.artifacts


@pytest.mark.parametrize("status_code", [200, 304, None])
async def test_readable_target_navigation_remains_normal(status_code):
    result = _page_result(TARGET_URL, status_code=status_code)
    result.metrics["navigation_document"] = navigation_document_facts(
        final_url=TARGET_URL, status_code=status_code,
        readiness={"document_ready_state": "complete", "blank_document": False},
    )
    browser = SimpleNamespace(capture=AsyncMock(return_value=result))
    reader = SimpleNamespace(read_command_scope=AsyncMock(return_value=_scope()))
    executor = _executor(browser, reader, InMemoryActionEvidenceBuffer())
    interaction = await executor.execute(_request())
    assert interaction.status is InteractionResultStatus.EVIDENCE_PENDING
    assert interaction.error_code is None


async def test_coordinator_persists_document_failure_after_successful_followup_observation():
    modes = []

    async def capture(request):
        modes.append(request.mode)
        if request.mode is page_capture.PageCaptureMode.GOTO:
            result = _page_result(TARGET_URL, status_code=304)
            result.metrics["navigation_document"] = navigation_document_facts(
                final_url=TARGET_URL, status_code=304,
                readiness={"document_ready_state": "complete", "blank_document": True},
            )
            return result
        return _page_result("about:blank" if len(modes) == 1 else TARGET_URL, mode="observe")

    browser = SimpleNamespace(capture=AsyncMock(side_effect=capture))
    reader = SimpleNamespace(read_command_scope=AsyncMock(return_value=_scope(epoch=0)))
    buffer = InMemoryActionEvidenceBuffer()
    persistence = _CapturePersistence()
    ledger = RecordingActionExecutionRepository([])
    coordinator = CaptureCoordinator(
        capture_writer=BrowserCaptureWriter(
            browser=browser, persistence=persistence, evidence_buffer=buffer,
        ),
        interaction_executor=_executor(browser, reader, buffer),
        evidence_exit_gate=NoopEvidenceExitGate(), action_execution_repository=ledger,
        action_recovery_captures=persistence,
        command_validator=InteractionCommandValidator(scope_reader=reader),
    )
    request = _request()
    result = await coordinator.coordinate(CaptureCoordinationCommand(
        agent_run_id=request.agent_run_id, intent=request.intent,
    ))
    assert result.interaction.status is InteractionResultStatus.FAILED
    assert result.interaction.error_code == "WEB_NAVIGATION_BLANK_DOCUMENT"
    assert ledger.state.status is ActionExecutionStatus.FAILED
    assert ledger.state.result_capture_id == result.after_capture.capture_id
    after = persistence.saved[-1]["result"]
    assert after.metrics["diagnostic_capture"] is True
    assert after.metrics["diagnostic_role"] == "post_navigation_document_unavailable"
    assert after.metrics["postcondition_complete"] is False
    assert after.metrics["navigation_document"]["inspectable"] is False
    assert after.metrics["navigation_document"]["main_response_status"] == 304
    assert after.metrics["navigation_scope"]["error_code"] == "WEB_NAVIGATION_BLANK_DOCUMENT"
    assert after.final_url == TARGET_URL


@pytest.mark.parametrize(
    "body,status,code,final_url",
    [
        ("<html><body></body></html>", 200, "WEB_NAVIGATION_BLANK_DOCUMENT", TARGET_URL),
        ("<main>服务器暂不可用</main>", 503, "WEB_NAVIGATION_HTTP_ERROR", TARGET_URL),
        (f'<script>location.replace("{OUTSIDE_URL}")</script>', 200,
         "COMMAND_HOST_BOUNDARY_VIOLATION", OUTSIDE_URL),
        ("<main>目标首页 <button>查看产品</button></main>", 200, None, TARGET_URL),
        ("<title>site.test | 520: Web server is returning an unknown error</title>"
         "<main>Web server is returning an unknown error. Error code 520. "
         "Visit cloudflare.com for more information. Browser Working Cloudflare Working "
         "Host Error. The web page can not be displayed.</main>",
         200, "WEB_NAVIGATION_TARGET_DOCUMENT_UNAVAILABLE", TARGET_URL),
    ],
)
async def test_real_browser_production_capture_and_executor(
    body, status, code, final_url, monkeypatch,
):
    # 仅缩短动画静默等待；导航、主文档、DOM、截图、网络审计和 Executor 均走生产实现。
    monkeypatch.setattr(page_capture, "_wait_for_page_settle", AsyncMock(return_value=True))
    async with async_playwright() as playwright:
        browser = await launch_web_test_browser(playwright.chromium, headless=True)
        try:
            context = await browser.new_context()
            page = await context.new_page()

            async def serve(route):
                if route.request.url == TARGET_URL:
                    await route.fulfill(status=status, content_type="text/html", body=body)
                elif route.request.url == OUTSIDE_URL:
                    await route.fulfill(status=200, content_type="text/html",
                                        body="<main>Example Domain</main>")
                else:
                    await route.abort()

            await context.route("**/*", serve)
            adapter = _CaptureAdapter(page, context)
            page_capture._attach_observers(page, adapter.console, adapter.network)
            reader = SimpleNamespace(read_command_scope=AsyncMock(return_value=_scope()))
            buffer = InMemoryActionEvidenceBuffer()
            interaction = await _executor(adapter, reader, buffer).execute(_request())
            assert interaction.error_code == code
            assert interaction.status is (
                InteractionResultStatus.EVIDENCE_PENDING if code is None
                else InteractionResultStatus.BLOCKED
                if code == "COMMAND_HOST_BOUNDARY_VIOLATION" else InteractionResultStatus.FAILED
            )
            pending = await buffer.get(action_execution_id=interaction.action_execution_id)
            assert pending.browser_result.final_url == page.url == final_url
            assert {item.artifact_type for item in pending.browser_result.artifacts} >= {
                "dom", "screenshot", "action_trace", "har",
            }
            observation = next(item for item in pending.browser_result.action_trace
                               if item["action"] == "navigation_observation")
            assert observation["requested_url"] == TARGET_URL
            assert observation["final_url"] == final_url
            assert pending.browser_result.metrics.get("diagnostic_capture", False) is (
                code is not None
            )
        finally:
            await browser.close()


@pytest.fixture
def cache_document():
    statuses = []

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self):
            if self.headers.get("If-None-Match") == '"alioth-cache-test"':
                statuses.append(304)
                self.send_response(304)
                self.send_header("ETag", '"alioth-cache-test"')
                self.end_headers()
                return
            statuses.append(200)
            body = "<html><body><main>可读取的缓存首页</main></body></html>".encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "max-age=0, must-revalidate")
            self.send_header("ETag", '"alioth-cache-test"')
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/", statuses
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


async def test_real_304_cache_revalidation_preserves_usable_document(cache_document, monkeypatch):
    target_url, statuses = cache_document
    monkeypatch.setattr(page_capture, "_wait_for_page_settle", AsyncMock(return_value=True))
    async with async_playwright() as playwright:
        browser = await launch_web_test_browser(playwright.chromium, headless=True)
        try:
            context = await browser.new_context()
            page = await context.new_page()
            await page.goto(target_url, wait_until="domcontentloaded")
            adapter = _CaptureAdapter(page, context)
            reader = SimpleNamespace(read_command_scope=AsyncMock(
                return_value=_scope(allowed_hosts=("127.0.0.1",)),
            ))
            buffer = InMemoryActionEvidenceBuffer()
            executor = _executor(adapter, reader, buffer, destination_url=target_url)
            interaction = await executor.execute(_request(target_url))
            assert 304 in statuses
            assert interaction.status is InteractionResultStatus.EVIDENCE_PENDING
            pending = await buffer.get(action_execution_id=interaction.action_execution_id)
            assert pending.browser_result.metrics["navigation_document"]["inspectable"] is True
            assert pending.browser_result.final_url == target_url
            assert "可读取的缓存首页" in await page.inner_text("body")
        finally:
            await browser.close()
