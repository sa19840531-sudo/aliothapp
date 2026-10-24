"""H5 首跳身份兼容仅处理不可用入口，保留冻结落点边界。"""

from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import urlsplit

import pytest

from services.api.app.infrastructure.browser.page_capture import (
    PageCaptureArtifact,
    PageCaptureMode,
    PageCaptureRequest,
    PageCaptureResult,
    PlaywrightPageCaptureBrowser,
    _PlaywrightSession,
)


def _result(url: str) -> PageCaptureResult:
    trace = [{"action": "navigation_observation", "final_url": url}]
    return PageCaptureResult(
        execution_status="success",
        mode="goto",
        reachable=True,
        final_url=url,
        title="fixture",
        status_code=200,
        metrics={"navigation_document": {"inspectable": True}},
        action_trace=trace,
        artifacts=[
            PageCaptureArtifact(artifact_type="action_trace", artifact_id=None, content=trace)
        ],
    )


def _session():
    page = SimpleNamespace(on=lambda *_: None)
    replacement = SimpleNamespace(new_page=AsyncMock(return_value=page), close=AsyncMock())
    cdp = SimpleNamespace(send=AsyncMock(return_value={}), detach=AsyncMock())
    replacement.new_cdp_session = AsyncMock(return_value=cdp)
    page.context = replacement
    browser = SimpleNamespace(new_context=AsyncMock(return_value=replacement))
    session = _PlaywrightSession(
        playwright=SimpleNamespace(devices={"Pixel 7": {"user_agent": "fixture-mobile"}}),
        browser=browser,
        context=SimpleNamespace(close=AsyncMock()),
        page=page,
    )
    return session, replacement


@pytest.mark.asyncio
async def test_valid_native_entry_does_not_change_any_browser_identity() -> None:
    browser = PlaywrightPageCaptureBrowser()
    session, _ = _session()
    result = _result("https://product.test/")
    request = PageCaptureRequest(
        mode=PageCaptureMode.GOTO,
        session_id="h5:run",
        url=result.final_url,
        _h5_entry_landing_validation=AsyncMock(return_value=True),
    )
    assert (
        await browser._retry_h5_entry_profile(
            request=request,
            session=session,
            result=result,
            timeout_error_type=TimeoutError,
        )
        is result
    )
    session.browser.new_context.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["http_error", "blank_document"])
@pytest.mark.parametrize("mobile_readable", [True, False])
async def test_same_scope_unreadable_entry_retries_once_and_requires_readable_mobile(
    failure, mobile_readable,
):
    browser = PlaywrightPageCaptureBrowser()
    session, replacement = _session()
    browser._sessions["h5:run"] = session
    native = replace(
        _result("https://product.test/"),
        status_code=403 if failure == "http_error" else 200,
        metrics={"navigation_document": {"inspectable": False}},
    )
    mobile = replace(
        _result("https://product.test/"),
        metrics={"navigation_document": {"inspectable": mobile_readable}},
    )
    browser._capture_on_page = AsyncMock(return_value=mobile)
    request = PageCaptureRequest(
        mode=PageCaptureMode.GOTO, session_id="h5:run", url=native.final_url,
        _h5_entry_landing_validation=AsyncMock(return_value=True),
    )
    result = await browser._retry_h5_entry_profile(
        request=request, session=session, result=native, timeout_error_type=TimeoutError,
    )
    session.browser.new_context.assert_awaited_once()
    browser._capture_on_page.assert_awaited_once()
    replacement.new_cdp_session.assert_awaited_once_with(browser._capture_on_page.call_args.kwargs["page"])
    assert result.status_code == (200 if mobile_readable else native.status_code)
    assert result.metrics["navigation_document"]["inspectable"] is mobile_readable
    assert browser._sessions["h5:run"].context is (
        replacement if mobile_readable else session.context
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("accepted", [True, False])
async def test_outside_native_entry_has_only_one_isolated_mobile_attempt(accepted) -> None:
    browser = PlaywrightPageCaptureBrowser()
    session, replacement = _session()
    browser._sessions["h5:run"] = session
    native = _result("https://outside.test/")
    mobile = _result("https://product.test/" if accepted else "https://outside.test/")
    browser._capture_on_page = AsyncMock(return_value=mobile)
    request = PageCaptureRequest(
        mode=PageCaptureMode.GOTO,
        session_id="h5:run",
        url="https://product.test/",
        _h5_entry_landing_validation=AsyncMock(side_effect=[False, accepted]),
    )
    result = await browser._retry_h5_entry_profile(
        request=request,
        session=session,
        result=native,
        timeout_error_type=TimeoutError,
    )
    session.browser.new_context.assert_awaited_once()
    browser._capture_on_page.assert_awaited_once()
    assert result.final_url == (mobile.final_url if accepted else native.final_url)
    assert len(result.metrics["h5_entry_profile_compatibility"]) == 2
    assert result.artifacts[0].content == result.action_trace
    if accepted:
        session.context.close.assert_awaited_once()
        replacement.close.assert_not_awaited()
        assert browser._sessions["h5:run"].context is replacement
    else:
        replacement.close.assert_awaited_once()
        session.context.close.assert_not_awaited()
        assert browser._sessions["h5:run"] is session


@pytest.mark.asyncio
async def test_mobile_failure_preserves_original_outside_diagnosis() -> None:
    browser = PlaywrightPageCaptureBrowser()
    session, replacement = _session()
    browser._capture_on_page = AsyncMock(side_effect=RuntimeError("fixture failure"))
    request = PageCaptureRequest(
        mode=PageCaptureMode.GOTO,
        session_id="h5:run",
        url="https://product.test/",
        _h5_entry_landing_validation=AsyncMock(return_value=False),
    )
    result = await browser._retry_h5_entry_profile(
        request=request,
        session=session,
        result=_result("https://outside.test/"),
        timeout_error_type=TimeoutError,
    )
    assert result.final_url == "https://outside.test/"
    assert result.metrics["h5_entry_profile_compatibility"][-1]["error_type"] == "RuntimeError"
    replacement.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_no_public_runtime_authority_means_no_profile_retry() -> None:
    browser = PlaywrightPageCaptureBrowser()
    session, _ = _session()
    request = PageCaptureRequest(
        mode=PageCaptureMode.GOTO, session_id="h5:run", url="https://product.test/"
    )
    result = _result("https://outside.test/")
    assert (
        await browser._retry_h5_entry_profile(
            request=request,
            session=session,
            result=result,
            timeout_error_type=TimeoutError,
        )
        is result
    )
    session.browser.new_context.assert_not_awaited()


@pytest.mark.asyncio
async def test_runtime_fence_failure_is_not_swallowed_as_profile_incompatibility() -> None:
    browser = PlaywrightPageCaptureBrowser()
    session, replacement = _session()
    browser._capture_on_page = AsyncMock(return_value=_result("https://product.test/"))
    request = PageCaptureRequest(
        mode=PageCaptureMode.GOTO,
        session_id="h5:run",
        url="https://product.test/",
        _h5_entry_landing_validation=AsyncMock(side_effect=[False, RuntimeError("epoch changed")]),
    )
    with pytest.raises(RuntimeError, match="epoch changed"):
        await browser._retry_h5_entry_profile(
            request=request,
            session=session,
            result=_result("https://outside.test/"),
            timeout_error_type=TimeoutError,
        )
    replacement.close.assert_awaited_once()


@pytest.fixture
def user_agent_sensitive_entry():
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            agent = self.headers.get("User-Agent", "")
            requests.append((self.path, agent))
            if self.path == "/sensitive" and "HeadlessChrome" in agent:
                self.send_response(302)
                self.send_header("Location", f"http://localhost:{self.server.server_port}/outside")
                self.end_headers()
                return
            body = b"<html><body><a href='/next'>next</a></body></html>"
            if self.path == "/popup-chain":
                body = """<html><body style="margin:0">
                    <main><h1>产品首页</h1><a href="/next">下一栏目</a></main>
                    <div id="notice" role="dialog" aria-modal="true"
                         style="position:fixed;inset:0;z-index:999;background:white">
                        <h2>版本公告</h2><button type="button" aria-label="关闭"
                            style="width:50px;height:50px"
                            onclick="this.parentNode.remove();document.getElementById('followup')
                                .style.display='block'">×</button></div>
                    <div id="followup" role="dialog" aria-modal="true"
                         style="display:none;position:fixed;inset:0;z-index:999;background:white">
                        <h2>活动公告</h2><button type="button" aria-label="关闭"
                            style="width:50px;height:50px"
                            onclick="this.parentNode.remove()">×</button></div>
                    </body></html>""".encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path, authorized_alias", [("/valid", False), ("/sensitive", False), ("/sensitive", True)]
)
async def test_real_entry_preserves_valid_native_and_authorized_redirects(
    monkeypatch,
    user_agent_sensitive_entry,
    path,
    authorized_alias,
):
    from services.api.app.infrastructure.browser import page_capture

    monkeypatch.setattr(page_capture, "_wait_for_page_settle", AsyncMock(return_value=True))
    monkeypatch.setattr(page_capture, "_collect_artifacts", AsyncMock(return_value=[]))
    root, requests = user_agent_sensitive_entry
    browser = PlaywrightPageCaptureBrowser()

    async def validate(url):
        return urlsplit(url).hostname == "127.0.0.1" or authorized_alias

    try:
        result = await browser.capture(
            PageCaptureRequest(
                mode=PageCaptureMode.GOTO,
                session_id="h5:entry-real",
                url=root + path,
                capture_screenshot=False,
                capture_dom=False,
                capture_visible_text=False,
                _h5_entry_landing_validation=validate,
            )
        )
        session = browser._sessions["h5:entry-real"]
        assert await validate(result.final_url)
        selected_agent = await session.page.evaluate("navigator.userAgent")
        if path == "/sensitive" and not authorized_alias:
            assert "Android" in selected_agent and "HeadlessChrome" not in selected_agent
            assert len(result.metrics["h5_entry_profile_compatibility"]) == 2
            assert any("HeadlessChrome" in agent for _, agent in requests)
        else:
            assert "HeadlessChrome" in selected_agent
            assert "h5_entry_profile_compatibility" not in result.metrics
        await session.page.goto(root + "/next")
        assert requests[-1] == ("/next", selected_agent)
    finally:
        await browser.close_session("h5:entry-real")
