"""受控双源浏览器验证恢复跳转与真实后帧血缘，不访问外网或数据库。"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from playwright.async_api import async_playwright

from services.api.app.application.web_desktop_exploration import WebDesktopMenuPageVisitor
from services.api.app.application.web_exploration_workflow import RunWebWorkflowCommand
from services.api.tests.web_browser_test_support import (
    WebBrowserUnavailable,
    launch_web_test_browser,
)


@pytest.mark.asyncio
async def test_browser_redirected_previous_page_recovers_entry_from_real_after_capture():
    entry_url = "https://recovery-site.test/"
    previous_url = "https://recovery-site.test/previous"
    unsafe_url = "https://recovery-away.test/unsafe"
    redirected_url = "https://recovery-away.test/redirected-again"
    requested_urls = []
    commands = []
    captures = {}
    transport_errors = []

    async with async_playwright() as playwright:
        try:
            browser = await launch_web_test_browser(playwright.chromium, headless=True)
        except WebBrowserUnavailable as exc:
            pytest.skip(str(exc))
        try:
            page = await browser.new_page()

            async def serve_controlled_route(route):
                # 所有请求均由本地路由提供响应，未知 URL 也不允许访问网络。
                requested_urls.append(route.request.url)
                if route.request.url == previous_url:
                    await route.fulfill(
                        status=200,
                        content_type="text/html; charset=utf-8",
                        body=f'<script>location.replace("{redirected_url}")</script>',
                    )
                elif route.request.url in {entry_url, unsafe_url, redirected_url}:
                    await route.fulfill(
                        status=200,
                        content_type="text/html; charset=utf-8",
                        body="<main>受控导航恢复页面</main>",
                    )
                else:
                    await route.abort()

            await page.route("**/*", serve_controlled_route)
            await page.goto(unsafe_url, wait_until="domcontentloaded")
            captures["unsafe"] = SimpleNamespace(capture_id="unsafe", url=page.url)

            class BrowserTransportCoordinator:
                async def coordinate(self, command):
                    # 薄传输适配保留动作源校验、真实浏览器导航与有序父子 Capture。
                    assert captures[command.intent.source_capture_id].url == page.url
                    commands.append(command)
                    sequence = len(commands)
                    before = SimpleNamespace(
                        capture_id=f"before-{sequence}",
                        parent_capture_id=command.intent.source_capture_id,
                        action_execution_id=f"action-{sequence}",
                        url=page.url,
                    )
                    captures[before.capture_id] = before
                    try:
                        await page.goto(
                            command.intent.parameters["destination_url"],
                            wait_until="networkidle",
                        )
                    except Exception as exc:
                        transport_errors.append(exc)
                        raise
                    after = SimpleNamespace(
                        capture_id=f"after-{sequence}",
                        parent_capture_id=before.capture_id,
                        action_execution_id=before.action_execution_id,
                        url=page.url,
                    )
                    captures[after.capture_id] = after
                    return SimpleNamespace(before_capture=before, after_capture=after)

            class CaptureReader:
                async def read_url(self, *, capture_id):
                    return captures[capture_id].url

            command = RunWebWorkflowCommand(
                task_id="task", agent_run_id="run", session_id="web:run",
                session_owner="web-v2-workflow", target_url=entry_url,
                browser_profile_id="desktop_fhd_1920x1080_v1",
                deadline=datetime.now(UTC) + timedelta(minutes=1),
            )
            visitor = WebDesktopMenuPageVisitor(
                coordinator=BrowserTransportCoordinator(), capture_url_reader=CaptureReader(),
            )
            recovered = await visitor._recover_from_unsafe_navigation(
                command=command, unsafe_capture_id="unsafe", recovery_url=previous_url,
                menu_intent_id="menu", action_parameters={},
            )

            assert not transport_errors
            assert recovered == "after-2"
            assert page.url == entry_url
            assert captures["after-1"].url == redirected_url
            assert [item.intent.source_capture_id for item in commands] == ["unsafe", "after-1"]
            assert captures["before-2"].parent_capture_id == "after-1"
            assert captures["after-2"].parent_capture_id == "before-2"
            assert [item.intent.parameters["destination_url"] for item in commands] == [
                previous_url, entry_url,
            ]
            assert len({item.intent.idempotency_key for item in commands}) == 2
            assert requested_urls.count(entry_url) == 1
            assert requested_urls.count(previous_url) == 1
            assert requested_urls.count(redirected_url) == 1
        finally:
            await browser.close()
