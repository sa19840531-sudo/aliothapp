"""受限 CLI 参数、执行器选择和真实同页浏览器回归。"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from services.api.app.infrastructure.browser.page_capture import (
    PageCaptureMode,
    PageCaptureRequest,
    PageTriggerAction,
    PlaywrightPageCaptureBrowser,
)
from services.api.app.infrastructure.browser.playwright_cli_process import (
    CLI_VERSION,
    PlaywrightCliError,
    PlaywrightCliProcess,
)
from services.api.app.infrastructure.browser.playwright_cli_session import (
    CliBrowserSession,
    semantic_snapshot_digest,
)

CLI_TEST_SCRIPT = Path(os.getenv(
    "ALIOTH_CLI_TEST_SCRIPT", "infra/playwright-cli/node_modules/@playwright/cli/playwright-cli.js"
)).resolve()


@pytest.mark.parametrize("mode", ["off", "observe", "actions"])
def test_cli_runtime_settings_read_environment(monkeypatch, mode):
    from services.api.app.core.config import _build_workflow_settings

    monkeypatch.setenv("ALIOTH_H5_CLI_MODE", mode)
    monkeypatch.setenv("ALIOTH_PLAYWRIGHT_CLI_SCRIPT", "/opt/cli/playwright-cli.js")
    monkeypatch.setenv("ALIOTH_PLAYWRIGHT_CLI_WORKSPACE_ROOT", "/tmp/cli-task-tests")
    settings = _build_workflow_settings()
    assert settings.h5_cli_mode == mode
    assert settings.h5_cli_script == "/opt/cli/playwright-cli.js"
    assert settings.h5_cli_workspace_root == "/tmp/cli-task-tests"


def test_unknown_cli_mode_is_rejected(monkeypatch):
    from pydantic import ValidationError

    from services.api.app.core.config import _build_workflow_settings

    monkeypatch.setenv("ALIOTH_H5_CLI_MODE", "shell")
    with pytest.raises(ValidationError):
        _build_workflow_settings()


def test_failed_cli_constructor_cleans_its_workspace(tmp_path):
    with pytest.raises(FileNotFoundError):
        CliBrowserSession(
            session_id="task",
            root=tmp_path,
            script=tmp_path / "missing",
            mode="observe",
        )
    assert list(tmp_path.iterdir()) == []


def test_missing_node_is_rejected_and_cleans_only_its_cli_workspace(tmp_path, monkeypatch):
    from services.api.app.infrastructure.browser import playwright_cli_process

    script = tmp_path / "playwright-cli.js"
    script.write_text("", encoding="utf-8")
    retained = tmp_path / "retained-evidence.txt"
    retained.write_text("历史证据", encoding="utf-8")
    monkeypatch.setattr(playwright_cli_process.shutil, "which", lambda _name: None)
    with pytest.raises(PlaywrightCliError, match="CLI_NODE_UNAVAILABLE"):
        CliBrowserSession(
            session_id="missing-node-task", root=tmp_path,
            script=script, mode="observe",
        )
    assert retained.read_text(encoding="utf-8") == "历史证据"
    assert {item.name for item in tmp_path.iterdir()} == {script.name, retained.name}


async def test_cli_mode_requires_platform_session_before_browser_launch(tmp_path):
    browser = PlaywrightPageCaptureBrowser(
        cli_mode="observe",
        cli_script=str(tmp_path / "unused.js"),
        cli_workspace_root=str(tmp_path),
    )
    with pytest.raises(ValueError, match="必须绑定平台会话"):
        await browser.capture(
            PageCaptureRequest(mode=PageCaptureMode.GOTO, url="https://sample.test")
        )
    assert not browser._sessions
    assert list(tmp_path.iterdir()) == []


@pytest.fixture
def process(tmp_path):
    script = tmp_path / "playwright-cli.js"
    script.write_text("", encoding="utf-8")
    (tmp_path / "package.json").write_text(
        json.dumps(
            {
                "name": "@playwright/cli",
                "version": CLI_VERSION,
            }
        ),
        encoding="utf-8",
    )
    item = PlaywrightCliProcess(
        script=script,
        workspace=tmp_path,
        session_id="task-a:run-a",
        actions_enabled=True,
    )
    item._attached = True
    item._verified = True
    item._invoke = AsyncMock(return_value={"result": "ok"})
    return item


@pytest.mark.parametrize(
    "command,args",
    [
        ("eval", ("() => document.cookie",)),
        ("run-code", ("page.close()",)),
        ("route", ("**",)),
        ("shell", ("echo injected",)),
        ("close-all", ()),
        ("tab-new", ("https://external.test",)),
        ("attach", ("--cdp=http://external:9222",)),
        ("snapshot", ("--config=/tmp/other.json",)),
        ("click", ("--session=other",)),
        ("click", ("getByRole('button')",)),
        ("goto", ("file:///etc/passwd",)),
        ("goto", ("javascript:alert(1)",)),
        ("goto", ("https://user:password@target.test",)),
        ("mousewheel", ("0", "10001")),
        ("mousemove", ("-1", "1")),
        ("screenshot", ("--filename=/tmp/out.png",)),
    ],
)
async def test_rejected_command_never_reaches_child(process, command, args):
    with pytest.raises(PlaywrightCliError):
        await process.run(command, *args)
    process._invoke.assert_not_awaited()


@pytest.mark.parametrize(
    "command,args",
    [
        ("goto", ("https://target.test/",)),
        ("click", ("#next",)),
        ("go-back", ()),
        ("mousewheel", ("0", "300")),
        ("mousemove", ("100", "100")),
    ],
)
async def test_readonly_mode_rejects_every_action(process, command, args):
    process.actions_enabled = False
    with pytest.raises(PlaywrightCliError, match="CLI_READ_ONLY"):
        await process.run(command, *args)
    process._invoke.assert_not_awaited()


async def test_unknown_action_result_prevents_retry_or_executor_reuse(process):
    process._invoke.side_effect = PlaywrightCliError(
        "CLI_RESPONSE_UNAVAILABLE", action_uncertain=True
    )
    with pytest.raises(PlaywrightCliError) as error:
        await process.run("click", "#next")
    assert error.value.action_uncertain
    with pytest.raises(PlaywrightCliError, match="CLI_SESSION_UNAVAILABLE"):
        await process.run("click", "#next")
    assert process._invoke.await_count == 1


async def test_cancelled_action_poisoned_and_not_retried(process):
    process._invoke.side_effect = asyncio.CancelledError
    with pytest.raises(asyncio.CancelledError):
        await process.run("click", "#next")
    with pytest.raises(PlaywrightCliError, match="CLI_SESSION_UNAVAILABLE"):
        await process.run("snapshot")
    process._invoke.side_effect = None
    process._invoke.return_value = {"snapshot": "readonly"}
    readonly = await process.run_readonly("snapshot")
    assert readonly.output == {"snapshot": "readonly"}
    assert process._invoke.await_count == 2


async def test_output_overflow_marks_dispatched_action_uncertain(process):
    process.script.write_text(
        "process.stdout.write('x'.repeat(3 * 1024 * 1024));",
        encoding="utf-8",
    )
    with pytest.raises(PlaywrightCliError, match="CLI_OUTPUT_LIMIT") as error:
        await PlaywrightCliProcess._invoke(process, ("click", "#next"), action=True)
    assert error.value.action_uncertain is True


async def test_poisoned_session_cannot_choose_legacy_action(process, tmp_path):
    session = CliBrowserSession(
        session_id="task",
        root=tmp_path,
        script=process.script,
        mode="actions",
    )
    session.process = process
    process._poisoned = True
    try:
        with pytest.raises(PlaywrightCliError, match="CLI_SESSION_UNAVAILABLE"):
            await session.trigger(
                page=None,
                request=PageCaptureRequest(
                    mode=PageCaptureMode.SINGLE_TRIGGER,
                    trigger_action=PageTriggerAction.CLICK,
                    selector="__special_popup",
                ),
            )
        process._invoke.assert_not_awaited()
    finally:
        session.cleanup()


async def test_selector_guided_scroll_binds_target_and_limits_one_wheel_step(
    process,
    tmp_path,
):
    class Handle:
        async def bounding_box(self):
            return {"x": 80, "y": 1600, "width": 240, "height": 120}

        async def dispose(self):
            return None

    class Locator:
        async def count(self):
            return 1

        async def element_handle(self, *, timeout):
            assert timeout == 30000
            return Handle()

    class Page:
        url = "https://target.test/list"
        viewport_size = {"width": 1000, "height": 800}

        def locator(self, selector):
            assert selector == "#detail-card"
            return Locator()

    session = CliBrowserSession(
        session_id="target-scroll",
        root=tmp_path / "sessions",
        script=process.script,
        mode="actions",
    )
    session.process = process
    session.set_initial_url(Page.url)
    session.assert_page = AsyncMock(return_value={})
    try:
        receipt = await session.trigger(
            page=Page(),
            request=PageCaptureRequest(
                mode=PageCaptureMode.SINGLE_TRIGGER,
                trigger_action=PageTriggerAction.SCROLL,
                selector="#detail-card",
            ),
        )
    finally:
        session.cleanup()

    assert receipt["target_binding"] == "selector"
    assert receipt["requires_new_capture"] is True
    assert receipt["command"] == "mousewheel"
    dispatched = [call.args[0] for call in process._invoke.await_args_list]
    assert dispatched[0][0] == "mousemove"
    assert dispatched[1][0] == "mousewheel"
    assert 0 < int(dispatched[1][2]) <= 10000


@pytest.mark.parametrize(
    ("box", "error_code"),
    (
        ({"x": 80, "y": 100, "width": 240, "height": 120}, "CLI_TARGET_ALREADY_VISIBLE"),
        (None, "CLI_TARGET_BINDING_UNAVAILABLE"),
    ),
)
async def test_selector_guided_scroll_rejects_visible_or_unbound_target(
    process,
    tmp_path,
    box,
    error_code,
):
    class Handle:
        async def bounding_box(self):
            return box

        async def dispose(self):
            return None

    locator = SimpleNamespace(
        count=AsyncMock(return_value=1),
        element_handle=AsyncMock(return_value=Handle()),
    )
    page = SimpleNamespace(
        url="https://target.test/list",
        viewport_size={"width": 1000, "height": 800},
        locator=lambda selector: locator,
    )
    session = CliBrowserSession(
        session_id="target-scroll-reject",
        root=tmp_path / "sessions",
        script=process.script,
        mode="actions",
    )
    session.process = process
    session.set_initial_url(page.url)
    session.assert_page = AsyncMock(return_value={})
    try:
        with pytest.raises(PlaywrightCliError, match=error_code):
            await session.trigger(
                page=page,
                request=PageCaptureRequest(
                    mode=PageCaptureMode.SINGLE_TRIGGER,
                    trigger_action=PageTriggerAction.SCROLL,
                    selector="#detail-card",
                ),
            )
    finally:
        session.cleanup()
    process._invoke.assert_not_awaited()


def test_comparison_log_does_not_replace_verified_action_in_evidence_merge():
    from services.api.app.infrastructure.browser.capture_runtime import _merge_action_evidence
    from services.api.app.infrastructure.browser.page_capture import (
        PageCaptureArtifact,
        PageCaptureResult,
    )

    trace = {"action": "click", "postcondition_satisfied": True}
    observation = {
        "action": "observe",
        "comparison_only": True,
        "provider_observation": {"snapshot": "只读结构"},
    }

    def result(*, actions, artifacts):
        return PageCaptureResult(
            execution_status="succeeded",
            mode="observe",
            reachable=True,
            final_url="https://sample.test/",
            title="页面",
            status_code=200,
            action_trace=actions,
            artifacts=artifacts,
        )

    action_result = result(actions=[trace], artifacts=[])
    capture_result = result(
        actions=[],
        artifacts=[
            PageCaptureArtifact(
                artifact_type="action_trace", artifact_id=None, content=[observation]
            ),
        ],
    )
    merged = _merge_action_evidence(
        capture_result=capture_result,
        action_result=action_result,
        required_types={"action_trace"},
    )
    assert merged.action_trace == [trace]
    assert merged.artifacts[0].content == [observation, trace]
    assert capture_result.action_trace == []


async def test_shell_characters_remain_argument_data_and_are_not_executed(process):
    selector = '[data-label="$(touch /tmp/should-not-exist);`uname`"]'
    receipt = await process.run("click", selector)
    process._invoke.assert_awaited_once_with(("click", selector), action=True)
    assert selector not in str(receipt.audit())
    assert receipt.audit()["arrival_verified"] is False


async def test_wrong_cli_package_version_rejected_before_attach(process):
    process._attached = False
    (process.script.parent / "package.json").write_text(
        json.dumps(
            {
                "name": "@playwright/cli",
                "version": "0.0.0",
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(PlaywrightCliError, match="CLI_VERSION_MISMATCH"):
        await process.attach("http://127.0.0.1:9222")
    process._invoke.assert_not_awaited()


async def test_external_cdp_endpoint_is_not_allowed(process):
    process._attached = False
    with pytest.raises(PlaywrightCliError, match="CLI_ENDPOINT_REJECTED"):
        await process.attach("http://another-host:9222")
    process._invoke.assert_not_awaited()


async def test_backend_node_click_is_internal_and_carries_selector_binding(process):
    semantic_digest = "a" * 64
    receipt = await process.click_backend_node(
        17,
        "html > body > button#target",
        semantic_digest,
    )

    encoded = base64.urlsafe_b64encode(b"html > body > button#target").decode().rstrip("=")
    process._invoke.assert_awaited_once_with(
        (
            "click",
            f"backend-node-id=17;selector={encoded};semantic-sha256={semantic_digest}",
        ),
        action=True,
    )
    assert receipt.audit()["command"] == "click"
    with pytest.raises(PlaywrightCliError, match="CLI_TARGET_REJECTED"):
        process._validate("click", (f"backend-node-id=17;selector={encoded}",))


async def test_backend_node_click_rejects_invalid_binding(process):
    with pytest.raises(PlaywrightCliError, match="CLI_TARGET_REJECTED"):
        process._validate("click", ("backend-node-id=0;selector=Iw",), allow_backend_node=True)
    with pytest.raises(PlaywrightCliError, match="CLI_TARGET_REJECTED"):
        process._validate(
            "click",
            ("backend-node-id=17;selector=not*base64",),
            allow_backend_node=True,
        )
    with pytest.raises(PlaywrightCliError, match="CLI_TARGET_BINDING_UNAVAILABLE"):
        # 缺少预检语义摘要时不得退回只按 selector 点击。
        await process.click_backend_node(17, "html > body > button#target")


def test_semantic_snapshot_digest_is_stable_and_does_not_include_input_value():
    summary = {
        "text": "更新",
        "aria_label": "更新按钮",
        "role": "button",
        "type": None,
        "href": "https://sample.test/update",
        "value": "secret-must-not-be-read",
    }
    expected = semantic_snapshot_digest(summary)
    assert expected == semantic_snapshot_digest({
        key: summary[key] for key in ("text", "aria_label", "role", "type", "href")
    })
    assert expected != semantic_snapshot_digest({**summary, "text": "支付"})


async def test_unverified_bundle_never_connects_browser(process):
    process._attached = False
    with pytest.raises(PlaywrightCliError, match="CLI_BUNDLE_UNVERIFIED"):
        await process.attach("http://127.0.0.1:9222")
    process._invoke.assert_not_awaited()


async def test_read_observation_environment_drift_quarantines_session(process, tmp_path):
    session = CliBrowserSession(
        session_id="drift", root=tmp_path, script=process.script, mode="observe"
    )
    session.process = process
    session._observe = AsyncMock(return_value={"comparison_only": True})
    page = SimpleNamespace(evaluate=AsyncMock(side_effect=[{"dpr": 2}, {"dpr": 1}]))
    try:
        with pytest.raises(PlaywrightCliError, match="CLI_OBSERVATION_ENVIRONMENT_CHANGED"):
            await session.observe(page)
        with pytest.raises(PlaywrightCliError, match="CLI_SESSION_UNAVAILABLE"):
            process.require_action_session()
        process._invoke.assert_not_awaited()
    finally:
        session.cleanup()


def test_environment_binding_is_fixed_and_does_not_forward_other_settings(process):
    process._attached = False
    process.configure_environment(
        viewport={"width": 390, "height": 844},
        context_options={
            "device_scale_factor": 2,
            "is_mobile": True,
            "has_touch": True,
            "proxy": {"password": "must-not-be-forwarded"},
        },
    )
    assert process._context_options == {
        "viewport": {"width": 390, "height": 844},
        "screen": {"width": 390, "height": 844},
        "deviceScaleFactor": 2,
        "isMobile": True,
        "hasTouch": True,
    }
    with pytest.raises(PlaywrightCliError, match="CLI_ENVIRONMENT_ALREADY_BOUND"):
        process.configure_environment(viewport={"width": 800, "height": 600}, context_options={})


@pytest.mark.parametrize(
    "viewport,options",
    [
        ({"width": True, "height": 844}, {}),
        ({"width": 390, "height": 844}, {"device_scale_factor": 99}),
        ({"width": 390, "height": 844}, {"is_mobile": "true"}),
    ],
)
def test_environment_binding_rejects_invalid_values(process, viewport, options):
    process._attached = False
    with pytest.raises(PlaywrightCliError, match="CLI_ENVIRONMENT_REJECTED"):
        process.configure_environment(viewport=viewport, context_options=options)
    assert process._context_options is None


def test_sessions_have_independent_workspaces_and_cli_names(tmp_path):
    script = tmp_path / "playwright-cli.js"
    script.write_text("", encoding="utf-8")
    a = CliBrowserSession(session_id="task-a", root=tmp_path, script=script, mode="observe")
    b = CliBrowserSession(session_id="task-b", root=tmp_path, script=script, mode="observe")
    try:
        assert a.directory != b.directory
        assert a.process.session_name != b.process.session_name
        assert (a.directory / ".playwright").is_dir()
        assert (b.directory / ".playwright").is_dir()
    finally:
        a.cleanup()
        b.cleanup()


def test_cli_artifact_symlink_is_rejected(process, tmp_path):
    external = tmp_path / "sensitive.txt"
    external.write_text("keep", encoding="utf-8")
    target = tmp_path / ("capture-" + "a" * 32 + ".png")
    try:
        target.symlink_to(external)
    except OSError:
        pytest.skip("当前 Windows 账户未启用符号链接权限")
    with pytest.raises(PlaywrightCliError, match="CLI_ARTIFACT_PATH_REJECTED"):
        process._validate("screenshot", ("--filename=" + str(target), "--hires"))
    assert external.read_text() == "keep"


class SampleHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/detail":
            html = "<title>详情</title><h1>详情内容</h1>"
        else:
            html = """<title>探索样本</title><h1>导航入口</h1>
              <a id="detail" href="/detail">查看详情</a>
              <button id="update" onclick="this.textContent='操作完成'">更新文字</button>
              <button id="pay" onclick="this.textContent='错误付款'">立即付款</button>
              <e1 onclick="this.textContent='已验证节点'">普通节点</e1>
              <div style="height:2200px">长页面内容</div><p>页面底部</p>"""
        body = (
            '<meta name="viewport" content="width=device-width,initial-scale=1">' + html
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


class LandingHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = "<title>广告落地页</title><h1>广告内容</h1>".encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


class ExternalAdHandler(BaseHTTPRequestHandler):
    landing_port = 0

    def do_GET(self):
        if self.path == "/":
            html = f"""<title>广告源页</title><h1>业务导航</h1>
              <a id="ad" href="http://127.0.0.1:{self.landing_port}/landing">打开广告</a>"""
        else:
            html = "<title>广告源页</title><h1>业务导航</h1>"
        body = (
            '<meta name="viewport" content="width=device-width,initial-scale=1">' + html
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


@pytest.fixture
def sample_site():
    server = ThreadingHTTPServer(("127.0.0.1", 0), SampleHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}/"
    server.shutdown()
    thread.join(timeout=3)
    server.server_close()


@pytest.fixture
def external_ad_site():
    landing_server = ThreadingHTTPServer(("127.0.0.1", 0), LandingHandler)
    ExternalAdHandler.landing_port = landing_server.server_port
    source_server = ThreadingHTTPServer(("127.0.0.1", 0), ExternalAdHandler)
    landing_thread = Thread(target=landing_server.serve_forever, daemon=True)
    source_thread = Thread(target=source_server.serve_forever, daemon=True)
    landing_thread.start()
    source_thread.start()
    try:
        yield f"http://127.0.0.1:{source_server.server_port}/"
    finally:
        source_server.shutdown()
        landing_server.shutdown()
        source_thread.join(timeout=3)
        landing_thread.join(timeout=3)
        source_server.server_close()
        landing_server.server_close()


@pytest.mark.skipif(
    os.getenv("ALIOTH_CLI_INTEGRATION") != "1", reason="需要安装锁版 CLI 和 Chromium"
)
@pytest.mark.parametrize(
    "mode,width,height,dpr,mobile",
    [
        ("observe", 390, 844, 2, True),
        ("actions", 390, 844, 2, True),
        ("observe", 1280, 800, 1, False),
        ("observe", 320, 640, 3, True),
    ],
)
async def test_real_cli_uses_existing_page_with_capture_and_verification(
    tmp_path, sample_site, mode, width, height, dpr, mobile
):
    from PIL import Image, ImageChops

    script = CLI_TEST_SCRIPT
    browser = PlaywrightPageCaptureBrowser(
        cli_mode=mode,
        cli_script=str(script),
        cli_workspace_root=str(tmp_path),
    )
    try:
        first = await browser.capture(
            PageCaptureRequest(
                mode=PageCaptureMode.GOTO,
                url=sample_site,
                session_id="task-a",
                full_page=False,
                viewport_width=width,
                viewport_height=height,
                device_scale_factor=dpr,
                is_mobile=mobile,
                has_touch=mobile,
            )
        )
        assert first.metrics["playwright_cli"]["status"] == "observed"
        assert first.metrics["playwright_cli"]["same_url"] is True
        assert first.metrics["playwright_cli"]["same_title"] is True
        assert first.status_code == 200
        session = browser._sessions["task-a"]
        assert session.page.viewport_size == {"width": width, "height": height}
        assert await session.page.evaluate("devicePixelRatio") == dpr
        directory = session.cli_session.directory
        daemon_pid = session.cli_session.process.daemon_pid
        assert daemon_pid
        assert (directory / "daemon").is_dir()
        observation = next(
            entry["provider_observation"]
            for artifact in first.artifacts
            if artifact.artifact_type == "action_trace"
            for entry in artifact.content
            if "provider_observation" in entry
        )
        assert observation["snapshot"]
        assert observation["screenshot"]["data"]
        assert observation["comparison_only"] is True
        assert observation["environment_preserved"] is True
        assert observation["environment_before"] == observation["environment_after"]
        cli_image = Image.open(
            io.BytesIO(base64.b64decode(observation["screenshot"]["data"]))
        ).convert("RGB")
        legacy_image = Image.open(
            io.BytesIO(await session.page.screenshot(scale="device"))
        ).convert("RGB")
        assert cli_image.size == legacy_image.size == (width * dpr, height * dpr)
        assert ImageChops.difference(cli_image, legacy_image).getbbox() is None
        assert await session.page.evaluate("devicePixelRatio") == dpr
        if mode == "observe":
            with pytest.raises(PlaywrightCliError, match="CLI_READ_ONLY"):
                await session.cli_session.process.run("click", "#detail")
            assert session.page.url == sample_site
        else:
            changed = await browser.capture(
                PageCaptureRequest(
                    mode=PageCaptureMode.SINGLE_TRIGGER,
                    session_id="task-a",
                    trigger_action=PageTriggerAction.CLICK,
                    selector="#detail",
                    navigation_depth=1,
                    full_page=False,
                )
            )
            assert changed.final_url == sample_site + "detail"
            assert changed.action_trace[0]["provider"] == "playwright_cli"
            assert changed.action_trace[0]["arrival_verified"] is False
            assert changed.post_action_snapshot["final_url"] == changed.final_url
            assert changed.metrics["page_inspection_ready"] is True
            returned = await browser.capture(
                PageCaptureRequest(
                    mode=PageCaptureMode.SINGLE_TRIGGER,
                    session_id="task-a",
                    trigger_action=PageTriggerAction.BACK,
                    navigation_depth=1,
                    full_page=False,
                )
            )
            assert returned.final_url == sample_site
            assert returned.action_trace[0]["provider"] == "playwright_cli"
            await browser.capture(
                PageCaptureRequest(
                    mode=PageCaptureMode.SINGLE_TRIGGER,
                    session_id="task-a",
                    trigger_action=PageTriggerAction.CLICK,
                    selector="e1",
                    full_page=False,
                )
            )
            assert await session.page.locator("e1").inner_text() == "已验证节点"
            with pytest.raises(PlaywrightCliError, match="CLI_TARGET_RISK_REJECTED"):
                await browser.capture(
                    PageCaptureRequest(
                        mode=PageCaptureMode.SINGLE_TRIGGER,
                        session_id="task-a",
                        trigger_action=PageTriggerAction.CLICK,
                        selector="#pay",
                        full_page=False,
                    )
                )
            assert await session.page.locator("#pay").inner_text() == "立即付款"
            await session.cli_session.trigger(
                page=session.page,
                request=PageCaptureRequest(
                    mode=PageCaptureMode.SINGLE_TRIGGER,
                    trigger_action=PageTriggerAction.SCROLL,
                ),
            )
            await session.page.wait_for_function("window.scrollY > 0")
    finally:
        await browser.close_session("task-a")
    assert not directory.exists()
    for _ in range(50):
        try:
            os.kill(daemon_pid, 0)
        except ProcessLookupError:
            break
        await asyncio.sleep(0.05)
    else:
        pytest.fail("CLI 后台进程未退出")


@pytest.mark.skipif(
    os.getenv("ALIOTH_CLI_INTEGRATION") != "1", reason="需要安装锁版 CLI 和 Chromium"
)
async def test_real_cli_hands_external_ad_to_legacy_landing_flow(tmp_path, external_ad_site):
    browser = PlaywrightPageCaptureBrowser(
        cli_mode="actions",
        cli_script=str(CLI_TEST_SCRIPT),
        cli_workspace_root=str(tmp_path),
    )
    try:
        await browser.capture(
            PageCaptureRequest(
                mode=PageCaptureMode.GOTO,
                url=external_ad_site,
                session_id="external-ad",
                full_page=False,
            )
        )
        result = await browser.capture(
            PageCaptureRequest(
                mode=PageCaptureMode.SINGLE_TRIGGER,
                session_id="external-ad",
                trigger_action=PageTriggerAction.CLICK,
                selector="#ad",
                full_page=False,
                timeout_ms=5000,
            )
        )
        assert result.execution_status == "success"
        assert result.final_url == external_ad_site
        trace = result.action_trace[-1]
        assert trace.get("provider") != "playwright_cli"
        evidence = trace["ad_navigation_evidence"]
        assert evidence["status"] == "observed"
        assert evidence["navigation_mode"] == "same_page"
        assert evidence["landing"]["url"].endswith("/landing")
        sequences = [
            artifact
            for artifact in result.artifacts
            if artifact.artifact_type == "screenshot"
            and artifact.content.get("purpose") == "ad_landing_consistency"
        ]
        assert len(sequences) == 1
        assert len(sequences[0].content["frames"]) == 2
        assert browser._sessions["external-ad"].page.url == external_ad_site
    finally:
        await browser.close_session("external-ad")


@pytest.mark.skipif(
    os.getenv("ALIOTH_CLI_INTEGRATION") != "1", reason="需要安装锁版 CLI 和 Chromium"
)
async def test_real_cli_does_not_click_replaced_same_selector(tmp_path, sample_site):
    browser = PlaywrightPageCaptureBrowser(
        cli_mode="actions",
        cli_script=str(CLI_TEST_SCRIPT),
        cli_workspace_root=str(tmp_path),
    )
    try:
        await browser.capture(
            PageCaptureRequest(
                mode=PageCaptureMode.GOTO,
                url=sample_site,
                session_id="replacement",
                full_page=False,
            )
        )
        session = browser._sessions["replacement"]
        await session.page.evaluate(
            """() => {
              const target = document.querySelector('#detail');
              target.id = 'replace-target';
              target.textContent = '旧节点';
              target.onclick = () => { window.__clicked = 'old'; };
            }"""
        )
        original_click = session.cli_session.process.click_backend_node

        async def replace_before_cli_click(backend_node_id, selector, semantic_digest):
            await session.page.evaluate(
                """() => {
                  const old = document.querySelector('#replace-target');
                  const replacement = document.createElement('a');
                  replacement.id = 'replace-target';
                  replacement.href = '#replacement';
                  replacement.textContent = '新节点';
                  replacement.onclick = () => { window.__clicked = 'replacement'; };
                  old.replaceWith(replacement);
                }"""
            )
            return await original_click(backend_node_id, selector, semantic_digest)

        session.cli_session.process.click_backend_node = replace_before_cli_click
        with pytest.raises(PlaywrightCliError, match="CLI_COMMAND_FAILED"):
            await browser.capture(
                PageCaptureRequest(
                    mode=PageCaptureMode.SINGLE_TRIGGER,
                    session_id="replacement",
                    trigger_action=PageTriggerAction.CLICK,
                    selector="#replace-target",
                    full_page=False,
                    timeout_ms=5000,
                )
            )
        assert await session.page.evaluate("window.__clicked || null") is None
        assert await session.page.locator("#replace-target").inner_text() == "新节点"
    finally:
        await browser.close_session("replacement")


@pytest.mark.skipif(
    os.getenv("ALIOTH_CLI_INTEGRATION") != "1", reason="需要安装锁版 CLI 和 Chromium"
)
async def test_real_cli_does_not_click_same_semantics_inserted_during_binding(
    tmp_path, sample_site
):
    """节点绑定竞态不能把同语义的新节点当成原 ElementHandle。"""
    browser = PlaywrightPageCaptureBrowser(
        cli_mode="actions",
        cli_script=str(CLI_TEST_SCRIPT),
        cli_workspace_root=str(tmp_path),
    )
    try:
        await browser.capture(
            PageCaptureRequest(
                mode=PageCaptureMode.GOTO,
                url=sample_site,
                session_id="same-semantics-race",
                full_page=False,
            )
        )
        session = browser._sessions["same-semantics-race"]
        await session.page.evaluate(
            """() => {
              const target = document.querySelector('#detail');
              target.textContent = '同语义目标';
              target.onclick = event => {
                event.preventDefault();
                window.__clicked = 'old';
              };
            }"""
        )
        context = session.page.context
        original_new_cdp_session = context.new_cdp_session
        original_backend_node_id = session.cli_session._backend_node_id
        binding_phase = False
        redefine_blocked = False

        async def run_backend_node_id(**kwargs):
            nonlocal binding_phase
            binding_phase = True
            return await original_backend_node_id(**kwargs)

        session.cli_session._backend_node_id = run_backend_node_id

        async def inject_before_reclaim(page):
            cdp = await original_new_cdp_session(page)
            original_send = cdp.send
            injected = False

            async def inject_race() -> None:
                nonlocal redefine_blocked
                redefine_blocked = await page.evaluate(
                    """() => {
                      const old = document.querySelector('#detail');
                      const key = Object.getOwnPropertyNames(window)
                        .find(name => name.startsWith('__alioth_cli_binding_bridge_'));
                      let blocked = true;
                      if (key) {
                        try {
                          Object.defineProperty(window, key, {
                            configurable: true,
                            writable: true,
                            value: old.cloneNode(true),
                          });
                          blocked = false;
                        } catch (_) {
                          blocked = true;
                        }
                      }
                      const fresh = old.cloneNode(true);
                      fresh.onclick = event => {
                        event.preventDefault();
                        window.__clicked = 'new';
                      };
                      old.before(fresh);
                      return blocked;
                    }"""
                )

            async def send(method, params=None):
                nonlocal injected
                # HEAD 旧实现会在 DOM.getDocument 后按 CSS 查询；新实现会
                # 在 Runtime.evaluate 前取桥接对象。两条分支都必须真正触发
                # 同语义克隆，避免回归测试只覆盖其中一套时序。
                if not injected and binding_phase and method == "DOM.getDocument":
                    injected = True
                    result = await original_send(method, params or {})
                    await inject_race()
                    return result
                if not injected and binding_phase and method == "Runtime.evaluate":
                    injected = True
                    await inject_race()
                result = await original_send(method, params or {})
                return result

            cdp.send = send
            return cdp

        context.new_cdp_session = inject_before_reclaim
        with pytest.raises(PlaywrightCliError, match="CLI_COMMAND_FAILED"):
            await browser.capture(
                PageCaptureRequest(
                    mode=PageCaptureMode.SINGLE_TRIGGER,
                    session_id="same-semantics-race",
                    trigger_action=PageTriggerAction.CLICK,
                    selector="#detail",
                    full_page=False,
                    timeout_ms=5000,
                )
            )
        assert await session.page.evaluate("window.__clicked || null") is None
        assert await session.page.locator("#detail").count() == 2
        assert redefine_blocked is True
        assert await session.page.evaluate(
            """() => Object.getOwnPropertyNames(window)
              .filter(key => key.startsWith('__alioth_cli_binding_bridge_')).length"""
        ) == 1
    finally:
        await browser.close_session("same-semantics-race")


@pytest.mark.skipif(
    os.getenv("ALIOTH_CLI_INTEGRATION") != "1", reason="需要安装锁版 CLI 和 Chromium"
)
async def test_real_cli_rebinds_bridge_after_navigation_and_rejects_same_name_claim(
    tmp_path, sample_site
):
    """桥接按 document 重建，导航后同名页面对象不能冒充原桥接。"""
    browser = PlaywrightPageCaptureBrowser(
        cli_mode="actions",
        cli_script=str(CLI_TEST_SCRIPT),
        cli_workspace_root=str(tmp_path),
    )
    try:
        await browser.capture(
            PageCaptureRequest(
                mode=PageCaptureMode.GOTO,
                url=sample_site,
                session_id="bridge-navigation",
                full_page=False,
            )
        )
        session = browser._sessions["bridge-navigation"]
        await browser.capture(
            PageCaptureRequest(
                mode=PageCaptureMode.SINGLE_TRIGGER,
                session_id="bridge-navigation",
                trigger_action=PageTriggerAction.CLICK,
                selector="e1",
                full_page=False,
            )
        )
        bridge_handle = session.cli_session._binding_bridge_handle
        assert bridge_handle is not None
        assert await bridge_handle.evaluate(
            "(bridge, token) => bridge.get(token) === null",
            session.cli_session._binding_bridge_token,
        ) is True
        first_key = session.cli_session._binding_bridge_key

        await browser.capture(
            PageCaptureRequest(
                mode=PageCaptureMode.GOTO,
                url=sample_site,
                session_id="bridge-navigation",
                full_page=False,
            )
        )
        await browser.capture(
            PageCaptureRequest(
                mode=PageCaptureMode.SINGLE_TRIGGER,
                session_id="bridge-navigation",
                trigger_action=PageTriggerAction.CLICK,
                selector="e1",
                full_page=False,
            )
        )
        assert await session.page.locator("e1").inner_text() == "已验证节点"
        second_key = session.cli_session._binding_bridge_key
        assert second_key != first_key

        await browser.capture(
            PageCaptureRequest(
                mode=PageCaptureMode.GOTO,
                url=sample_site,
                session_id="bridge-navigation",
                full_page=False,
            )
        )
        await session.page.evaluate(
            """key => {
              window.__probe_called = false;
              Object.defineProperty(window, key, {
                configurable: true,
                enumerable: false,
                writable: true,
                value: {
                  probe() {
                    window.__probe_called = true;
                    return true;
                  },
                  set() {},
                  get() { return document.querySelector('e1'); },
                  clear() {},
                },
              });
            }""",
            second_key,
        )
        with pytest.raises(PlaywrightCliError, match="CLI_TARGET_BINDING_UNAVAILABLE"):
            await browser.capture(
                PageCaptureRequest(
                    mode=PageCaptureMode.SINGLE_TRIGGER,
                    session_id="bridge-navigation",
                    trigger_action=PageTriggerAction.CLICK,
                    selector="e1",
                    full_page=False,
                )
            )
        assert await session.page.evaluate("window.__probe_called") is False
        assert await session.page.locator("e1").inner_text() == "普通节点"
    finally:
        await browser.close_session("bridge-navigation")


@pytest.mark.skipif(
    os.getenv("ALIOTH_CLI_INTEGRATION") != "1", reason="需要安装锁版 CLI 和 Chromium"
)
async def test_real_cli_rejects_semantic_drift_on_same_backend_node(tmp_path, sample_site):
    """同一个 DOM 节点在预检后变成支付/外跳目标时不得继续点击。"""
    browser = PlaywrightPageCaptureBrowser(
        cli_mode="actions",
        cli_script=str(CLI_TEST_SCRIPT),
        cli_workspace_root=str(tmp_path),
    )
    try:
        await browser.capture(
            PageCaptureRequest(
                mode=PageCaptureMode.GOTO,
                url=sample_site,
                session_id="semantic-drift",
                full_page=False,
            )
        )
        session = browser._sessions["semantic-drift"]
        original_click = session.cli_session.process.click_backend_node

        async def mutate_before_cli_click(backend_node_id, selector, semantic_digest):
            # 保留同一 DOM 节点和 backendNodeId，只改变预检绑定的业务语义。
            await session.page.evaluate(
                """() => {
                  const target = document.querySelector('#detail');
                  target.textContent = '立即支付';
                  target.setAttribute('aria-label', '立即支付按钮');
                  target.setAttribute('href', 'https://outside.example/pay');
                  target.setAttribute('type', 'submit');
                }"""
            )
            return await original_click(backend_node_id, selector, semantic_digest)

        session.cli_session.process.click_backend_node = mutate_before_cli_click
        with pytest.raises(PlaywrightCliError, match="CLI_COMMAND_FAILED"):
            await browser.capture(
                PageCaptureRequest(
                    mode=PageCaptureMode.SINGLE_TRIGGER,
                    session_id="semantic-drift",
                    trigger_action=PageTriggerAction.CLICK,
                    selector="#detail",
                    full_page=False,
                    timeout_ms=5000,
                )
            )
        assert await session.page.evaluate("window.__clicked || null") is None
        assert await session.page.locator("#detail").inner_text() == "立即支付"
        assert await session.page.locator("#detail").get_attribute("href") == (
            "https://outside.example/pay"
        )
        assert session.page.url == sample_site
        assert session.cli_session.process.is_quarantined is True
    finally:
        await browser.close_session("semantic-drift")


@pytest.mark.skipif(
    os.getenv("ALIOTH_CLI_INTEGRATION") != "1", reason="需要安装锁版 CLI 和 Chromium"
)
async def test_quarantined_cli_keeps_readonly_after_capture_but_rejects_actions(
    tmp_path, sample_site
):
    browser = PlaywrightPageCaptureBrowser(
        cli_mode="actions",
        cli_script=str(CLI_TEST_SCRIPT),
        cli_workspace_root=str(tmp_path),
    )
    try:
        await browser.capture(
            PageCaptureRequest(
                mode=PageCaptureMode.GOTO,
                url=sample_site,
                session_id="unknown-action",
                full_page=False,
            )
        )
        session = browser._sessions["unknown-action"]
        original_invoke = session.cli_session.process._invoke

        async def fail_click_once(args, *, action):
            if args and args[0] == "click":
                raise PlaywrightCliError("CLI_RESPONSE_UNAVAILABLE", action_uncertain=True)
            return await original_invoke(args, action=action)

        session.cli_session.process._invoke = fail_click_once
        with pytest.raises(PlaywrightCliError, match="CLI_RESPONSE_UNAVAILABLE"):
            await browser.capture(
                PageCaptureRequest(
                    mode=PageCaptureMode.SINGLE_TRIGGER,
                    session_id="unknown-action",
                    trigger_action=PageTriggerAction.CLICK,
                    selector="#detail",
                    full_page=False,
                    timeout_ms=5000,
                )
            )
        assert session.cli_session.process.is_quarantined is True

        before_scroll = await session.page.evaluate(
            """() => {
              document.documentElement.style.minHeight = '2400px';
              window.scrollTo(0, 600);
              return window.scrollY;
            }"""
        )
        assert before_scroll > 0

        diagnostic = await browser.capture(
            PageCaptureRequest(
                mode=PageCaptureMode.OBSERVE,
                session_id="unknown-action",
                # 故意传入可能触发副作用的组合，验证隔离态内部会收敛为
                # 只读视口采集，而不是把调用方参数原样交给实现层。
                full_page=True,
                capture_transient_messages=True,
                capture_ad_landing_evidence=True,
                clear_obstructions=True,
                allow_high_z_obstruction_hide=True,
                verified_card_activation="unexpected",
            )
        )
        assert diagnostic.execution_status == "success"
        assert diagnostic.metrics["playwright_cli"]["status"] == "quarantined_readonly"
        assert diagnostic.metrics["playwright_cli"]["provider_outcome"] == "unknown"
        assert (
            diagnostic.metrics["playwright_cli"]["diagnostic_role"]
            == "post_action_readonly_unknown"
        )
        assert await session.page.evaluate("window.scrollY") == before_scroll
        assert any(item.artifact_type == "screenshot" for item in diagnostic.artifacts)
        with pytest.raises(PlaywrightCliError, match="CLI_SESSION_UNAVAILABLE"):
            await browser.capture(
                PageCaptureRequest(
                    mode=PageCaptureMode.SINGLE_TRIGGER,
                    session_id="unknown-action",
                    trigger_action=PageTriggerAction.CLICK,
                    selector="#detail",
                    full_page=False,
                )
            )
    finally:
        await browser.close_session("unknown-action")


@pytest.mark.skipif(
    os.getenv("ALIOTH_CLI_INTEGRATION") != "1", reason="需要安装锁版 CLI 和 Chromium"
)
async def test_real_browser_close_stops_cli_when_detach_response_fails(tmp_path, sample_site):
    browser = PlaywrightPageCaptureBrowser(
        cli_mode="observe",
        cli_script=str(CLI_TEST_SCRIPT),
        cli_workspace_root=str(tmp_path),
    )
    try:
        await browser.capture(
            PageCaptureRequest(
                mode=PageCaptureMode.GOTO,
                url=sample_site,
                session_id="detach-failure",
                full_page=False,
            )
        )
        session = browser._sessions["detach-failure"]
        pid = session.cli_session.process.daemon_pid
        directory = session.cli_session.directory
        session.cli_session.process._invoke = AsyncMock(
            side_effect=PlaywrightCliError("CLI_RESPONSE_UNAVAILABLE"),
        )
    finally:
        await browser.close_session("detach-failure")
    assert not directory.exists()
    for _ in range(50):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        await asyncio.sleep(0.05)
    else:
        pytest.fail("浏览器关闭后 CLI 后台进程仍未退出")


@pytest.mark.skipif(
    os.getenv("ALIOTH_CLI_INTEGRATION") != "1", reason="需要安装锁版 CLI 和 Chromium"
)
async def test_real_cli_sessions_isolate_storage_and_wrong_tab_binding(tmp_path, sample_site):
    browser = PlaywrightPageCaptureBrowser(
        cli_mode="observe",
        cli_script=str(CLI_TEST_SCRIPT),
        cli_workspace_root=str(tmp_path),
    )
    directories = []
    try:
        for session_id in ("isolated-a", "isolated-b"):
            await browser.capture(
                PageCaptureRequest(
                    mode=PageCaptureMode.GOTO,
                    url=sample_site,
                    session_id=session_id,
                    full_page=False,
                )
            )
        a, b = browser._sessions["isolated-a"], browser._sessions["isolated-b"]
        directories = [a.cli_session.directory, b.cli_session.directory]
        assert directories[0] != directories[1]
        assert a.cli_session.process.daemon_pid != b.cli_session.process.daemon_pid
        await a.context.add_cookies([{"name": "task_scope", "value": "only-a", "url": sample_site}])
        assert any(cookie["name"] == "task_scope" for cookie in await a.context.cookies())
        assert all(cookie["name"] != "task_scope" for cookie in await b.context.cookies())
        extra = await b.context.new_page()
        await extra.goto(sample_site)
        listing = await b.cli_session.process.run("tab-list")
        current_line = next(
            line for line in listing.output["result"].splitlines() if "(current)" in line
        )
        current_index = int(current_line.split(":", 1)[0].removeprefix("- "))
        other_page = b.context.pages[1 - current_index]
        with pytest.raises(PlaywrightCliError, match="CLI_PAGE_BINDING_MISMATCH"):
            await b.cli_session.assert_page(other_page)
        await extra.close()
        await browser.close_session("isolated-a")
        assert not directories[0].exists()
        assert (await b.cli_session.observe(b.page))["url"] == sample_site
    finally:
        await browser.close_session("isolated-a")
        await browser.close_session("isolated-b")
    assert all(not directory.exists() for directory in directories)
