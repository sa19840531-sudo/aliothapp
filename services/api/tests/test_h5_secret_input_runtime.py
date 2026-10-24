"""H5 专用秘密输入 Runtime 的无泄露与原子清空回归。"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from services.api.app.application.capture_coordinator import InProcessSessionCommandGate
from services.api.app.application.h5_secret_input import (
    H5SecretInputCommand,
    H5SecretInputResult,
)
from services.api.app.core.h5_auth_config import FileH5CredentialConfigResolver
from services.api.app.infrastructure.browser import page_capture as page_capture_module
from services.api.app.infrastructure.browser.h5_secret_input_runtime import (
    PlaywrightH5SecretInputRuntime,
)
from services.api.app.infrastructure.browser.page_capture import (
    PlaywrightPageCaptureBrowser,
)
from services.api.app.infrastructure.browser.semantic_target_resolver import (
    UiSnapshotSemanticTargetResolver,
)

_PHONE_CANARY = "AUTH_CANARY_PHONE_8917"
_PASSWORD_CANARY = "AUTH_CANARY_PASSWORD_6241"


def test_secret_request_body_classifier_requires_exact_runtime_credentials() -> None:
    request = SimpleNamespace(
        post_data_json={
            "username": _PHONE_CANARY,
            "password": _PASSWORD_CANARY,
        }
    )

    assert page_capture_module._request_contains_secret_credentials(
        request=request,
        phone=_PHONE_CANARY,
        password=_PASSWORD_CANARY,
    ) is True
    assert page_capture_module._request_contains_secret_credentials(
        request=request,
        phone=_PHONE_CANARY,
        password="different",
    ) is False


def test_secret_request_body_classifier_fails_closed_for_non_mapping_body() -> None:
    request = SimpleNamespace(post_data_json="opaque")

    assert page_capture_module._request_contains_secret_credentials(
        request=request,
        phone=_PHONE_CANARY,
        password=_PASSWORD_CANARY,
    ) is False


def test_secret_request_body_classifier_accepts_exact_form_encoded_body() -> None:
    request = SimpleNamespace(
        post_data_json=None,
        post_data=(
            f"username={_PHONE_CANARY}&password={_PASSWORD_CANARY}"
        ),
    )

    assert page_capture_module._request_contains_secret_credentials(
        request=request,
        phone=_PHONE_CANARY,
        password=_PASSWORD_CANARY,
    ) is True


class _SnapshotReader:
    async def read_ui_snapshot(self, *, task_id: str, page_capture_id: str):
        assert task_id == "task-auth"
        assert page_capture_id == "capture-auth"
        return {
            "schema_version": "ui-snapshot.v2",
            "elements": [
                {
                    "selector": "#phone",
                    "tag": "input",
                    "type": "tel",
                    "placeholder": "请输入手机号",
                    "interactive": True,
                    "disabled": False,
                    "read_only": False,
                },
                {
                    "selector": "#password",
                    "tag": "input",
                    "type": "password",
                    "placeholder": "请输入密码",
                    "interactive": True,
                    "disabled": False,
                    "read_only": False,
                },
                {
                    "selector": "#submit",
                    "tag": "button",
                    "text": "登录",
                    "interactive": True,
                    "disabled": False,
                    "hit_test": {"center_hits_target": True},
                },
            ],
        }


class _BrowserPort:
    def __init__(self, *, fail_with_secret: bool = False) -> None:
        self.fail_with_secret = fail_with_secret
        self.call: dict[str, object] | None = None

    async def submit_secret_form(self, **kwargs):
        self.call = kwargs
        if self.fail_with_secret:
            raise RuntimeError(str(kwargs["password"]))
        return page_capture_module.H5BrowserSecretFormResult(
            submitted=True,
            fields_scrubbed=True,
            hostname="h5.example.com",
            reason_code="secret_form_submitted",
        )


def _resolver(tmp_path) -> tuple[FileH5CredentialConfigResolver, object]:
    config_path = tmp_path / "h5-auth.json"
    config_path.write_text(
        json.dumps(
            {
                "schema_version": "h5-auth-config.v1",
                "global": {
                    "phone": _PHONE_CANARY,
                    "password": _PASSWORD_CANARY,
                },
                "domains": {},
            }
        ),
        encoding="utf-8",
    )
    resolver = FileH5CredentialConfigResolver.from_path(
        config_path,
        authorized_hostnames=("h5.example.com",),
    )
    ref = resolver.resolve_ref("h5.example.com")
    assert ref is not None
    return resolver, ref


def _command(ref) -> H5SecretInputCommand:
    return H5SecretInputCommand(
        task_id="task-auth",
        session_id="session-auth",
        source_capture_id="capture-auth",
        target_hostname="h5.example.com",
        credential_ref=ref,
        phone_semantic_target="请输入手机号",
        password_semantic_target="请输入密码",
        submit_semantic_target="登录",
        deadline=datetime.now(UTC) + timedelta(seconds=30),
    )


@pytest.mark.asyncio
async def test_runtime_resolves_selectors_and_reveals_secret_only_to_browser(tmp_path) -> None:
    credentials, ref = _resolver(tmp_path)
    browser = _BrowserPort()
    runtime = PlaywrightH5SecretInputRuntime(
        credential_resolver=credentials,
        snapshot_reader=_SnapshotReader(),
        target_resolver=UiSnapshotSemanticTargetResolver(
            snapshot_reader=_SnapshotReader()
        ),
        browser=browser,
        session_command_gate=InProcessSessionCommandGate(),
    )
    command = _command(ref)

    result = await runtime.execute(command)

    assert result == H5SecretInputResult(
        submitted=True,
        fields_scrubbed=True,
        hostname="h5.example.com",
        reason_code="secret_form_submitted",
    )
    assert browser.call == {
        "session_id": "session-auth",
        "target_hostname": "h5.example.com",
        "phone_selector": "#phone",
        "password_selector": "#password",
        "submit_selector": "#submit",
        "submit_semantic_target": "登录",
        "allowed_request_hostnames": (),
        "phone": _PHONE_CANARY,
        "password": _PASSWORD_CANARY,
        "timeout_ms": pytest.approx(30_000, abs=1_500),
    }
    safe_output = json.dumps(
        {
            "command": command.to_safe_dict(),
            "command_repr": repr(command),
            "result": result.to_safe_dict(),
        },
        ensure_ascii=False,
    )
    assert _PHONE_CANARY not in safe_output
    assert _PASSWORD_CANARY not in safe_output
    assert "selector" not in safe_output


@pytest.mark.asyncio
async def test_runtime_sanitizes_browser_exception_that_contains_password(tmp_path) -> None:
    credentials, ref = _resolver(tmp_path)
    runtime = PlaywrightH5SecretInputRuntime(
        credential_resolver=credentials,
        snapshot_reader=_SnapshotReader(),
        target_resolver=UiSnapshotSemanticTargetResolver(
            snapshot_reader=_SnapshotReader()
        ),
        browser=_BrowserPort(fail_with_secret=True),
        session_command_gate=InProcessSessionCommandGate(),
    )

    result = await runtime.execute(_command(ref))

    rendered = json.dumps(result.to_safe_dict(), ensure_ascii=False)
    assert result.submitted is False
    assert result.fields_scrubbed is False
    assert result.reason_code == "browser_secret_submit_failed"
    assert _PASSWORD_CANARY not in rendered


class _Locator:
    def __init__(
        self,
        selector: str,
        events: list[tuple[str, str]],
        *,
        refuses_clear: bool = False,
        page=None,
    ) -> None:
        self.selector = selector
        self.events = events
        self.refuses_clear = refuses_clear
        self.value = ""
        self.page = page

    async def count(self) -> int:
        return 1

    async def fill(self, value: str, *, timeout: int) -> None:
        del timeout
        label = "clear" if value == "" else "fill"
        self.events.append((label, self.selector))
        if value or not self.refuses_clear:
            self.value = value

    async def input_value(self, *, timeout: int) -> str:
        del timeout
        return self.value

    async def press_sequentially(
        self,
        value: str,
        *,
        delay: int,
        timeout: int,
    ) -> None:
        assert delay > 0
        assert timeout > 0
        self.events.append(("type", self.selector))
        self.value += value
        if (
            self.page is not None
            and self.page.clear_phone_after_password_type
            and self.selector == "#password"
        ):
            self.page.locators["#phone"].value = ""

    async def click(self, *, timeout: int) -> None:
        del timeout
        self.events.append(("click", self.selector))
        if (
            self.page is not None
            and (self.selector == "#submit" or self.selector.startswith("#flt-semantic-node-"))
            and await self.page.emit_request()
        ):
            self.page.semantic_signature = "after"

    async def press(self, key: str, *, timeout: int) -> None:
        assert key in {"ControlOrMeta+A", "Backspace", "Tab"}
        assert timeout > 0
        self.events.append(("press", f"{self.selector}:{key}"))
        if key == "Backspace":
            self.value = ""

    async def tap(self, *, timeout: int) -> None:
        assert timeout > 0
        self.events.append(("tap", self.selector))
        if self.page is not None and await self.page.emit_request():
            self.page.semantic_signature = "after"

    async def evaluate(self, expression: str, argument=None):
        del argument
        assert "tagName" in expression
        if self.selector.startswith("#flt-semantic-node-"):
            return {
                "tag": "flt-semantics",
                "tappable": True,
                "text": "注册",
                "center_hit": True,
            }
        return {
            "tag": "button",
            "tappable": False,
            "text": "登录",
            "center_hit": True,
        }

    async def dispatch_event(self, event: str, *, timeout: int) -> None:
        assert event == "click"
        assert timeout > 0
        self.events.append(("dispatch_click", self.selector))
        if self.page is not None and await self.page.emit_request():
            self.page.semantic_signature = "after"

    async def bounding_box(self):
        if self.selector.startswith("#flt-semantic-node-"):
            return {"x": 41.6, "y": 428.48, "width": 143.0, "height": 41.59}
        return None


class _Mouse:
    def __init__(self, page) -> None:
        self.page = page

    async def click(self, x: float, y: float) -> None:
        self.page.events.append(("mouse_click", f"{x:.2f},{y:.2f}"))
        if await self.page.emit_request():
            self.page.semantic_signature = "after"


class _Touchscreen:
    def __init__(self, page) -> None:
        self.page = page

    async def tap(self, x: float, y: float) -> None:
        self.page.events.append(("touch_tap", f"{x:.2f},{y:.2f}"))
        if await self.page.emit_request():
            self.page.semantic_signature = "after"


class _Route:
    def __init__(self, url: str, *, method: str = "POST") -> None:
        self.request = SimpleNamespace(url=url, method=method)
        self.aborted = False
        self.continued = False

    async def abort(self) -> None:
        self.aborted = True

    async def continue_(self) -> None:
        self.continued = True


class _Page:
    def __init__(
        self,
        *,
        url: str,
        refuses_clear_selector: str | None = None,
        request_on_click_url: str | None = None,
        transient_semantic_effect: bool = False,
        auth_feedback_code: str | None = None,
        transient_auth_feedback_code: str | None = None,
        clear_phone_after_password_type: bool = False,
        secondary_request_on_click_url: str | None = None,
        secondary_request_on_click_method: str = "GET",
    ) -> None:
        self.url = url
        self.events: list[tuple[str, str]] = []
        self.route_handler = None
        self.refuses_clear_selector = refuses_clear_selector
        self.semantic_signature = "before"
        self.mouse = _Mouse(self)
        self.touchscreen = _Touchscreen(self)
        self.request_on_click_url = request_on_click_url
        self.last_route = None
        self.transient_semantic_effect = transient_semantic_effect
        self.auth_feedback_code = auth_feedback_code
        self.transient_auth_feedback_code = transient_auth_feedback_code
        self.wait_count = 0
        self.clear_phone_after_password_type = clear_phone_after_password_type
        self.secondary_request_on_click_url = secondary_request_on_click_url
        self.secondary_request_on_click_method = secondary_request_on_click_method
        self.locators: dict[str, _Locator] = {}

    def locator(self, selector: str) -> _Locator:
        if selector not in self.locators:
            self.locators[selector] = _Locator(
                selector,
                self.events,
                refuses_clear=selector == self.refuses_clear_selector,
                page=self,
            )
        return self.locators[selector]

    async def emit_request(self) -> bool:
        if self.request_on_click_url is None:
            return True
        assert self.route_handler is not None
        route = _Route(self.request_on_click_url)
        self.last_route = route
        await self.route_handler(route)
        primary_succeeded = route.continued and not route.aborted
        if self.secondary_request_on_click_url is not None:
            secondary_route = _Route(
                self.secondary_request_on_click_url,
                method=self.secondary_request_on_click_method,
            )
            await self.route_handler(secondary_route)
            assert secondary_route.aborted is True
        return primary_succeeded

    async def route(self, pattern: str, handler) -> None:
        assert pattern == "**/*"
        self.route_handler = handler

    async def unroute(self, pattern: str, handler) -> None:
        assert pattern == "**/*"
        assert handler is self.route_handler
        self.route_handler = None

    async def wait_for_load_state(self, state: str, *, timeout: int) -> None:
        assert state == "domcontentloaded"
        assert timeout > 0

    async def wait_for_timeout(self, timeout: int) -> None:
        assert timeout > 0
        self.wait_count += 1
        if self.transient_semantic_effect:
            self.semantic_signature = "before"

    async def evaluate(self, expression: str, argument=None):
        del argument
        if "secretSubmitEffectProbe" in expression:
            return {
                "url": self.url,
                "submit_visible": self.semantic_signature == "before",
                "dialog_visible": self.semantic_signature == "after",
                "success_marker_visible": False,
                "feedback_code": (
                    self.transient_auth_feedback_code
                    if self.wait_count == 1
                    else self.auth_feedback_code
                ),
            }
        assert "flt-semantics-host" in expression
        return self.semantic_signature

def _bind_page(browser: PlaywrightPageCaptureBrowser, page: _Page) -> None:
    browser._sessions["session-auth"] = page_capture_module._PlaywrightSession(
        playwright=SimpleNamespace(),
        browser=SimpleNamespace(),
        context=SimpleNamespace(),
        page=page,
    )


@pytest.mark.asyncio
async def test_browser_secret_window_fills_submits_then_scrubs_remaining_fields() -> None:
    browser = PlaywrightPageCaptureBrowser()
    page = _Page(url="https://h5.example.com/login")
    _bind_page(browser, page)
    session = browser._sessions["session-auth"]
    session.console_logs.append({"text": _PASSWORD_CANARY})
    session.network_requests.append({"url": f"https://h5.example.com/?p={_PHONE_CANARY}"})
    session.network_inflight[1] = {"url": "https://h5.example.com/login"}

    result = await browser.submit_secret_form(
        session_id="session-auth",
        target_hostname="h5.example.com",
        phone_selector="#phone",
        password_selector="#password",
        submit_selector="#submit",
        submit_semantic_target="登录",
        phone=_PHONE_CANARY,
        password=_PASSWORD_CANARY,
        timeout_ms=5_000,
    )

    assert result.submitted is True
    assert result.fields_scrubbed is True
    assert result.reason_code == "secret_form_submitted"
    assert session.observers_muted is True
    assert session.console_logs == []
    assert session.network_requests == []
    assert session.network_inflight == {}
    assert page.events == [
        ("fill", "#phone"),
        ("fill", "#password"),
        ("click", "#submit"),
        ("clear", "#phone"),
        ("clear", "#password"),
    ]


@pytest.mark.asyncio
async def test_flutter_secret_submit_uses_live_verified_single_semantic_click() -> None:
    browser = PlaywrightPageCaptureBrowser()
    page = _Page(url="https://h5.example.com/login")
    _bind_page(browser, page)

    result = await browser.submit_secret_form(
        session_id="session-auth",
        target_hostname="h5.example.com",
        phone_selector="#phone",
        password_selector="#password",
        submit_selector="#flt-semantic-node-456",
        submit_semantic_target="注册",
        phone=_PHONE_CANARY,
        password=_PASSWORD_CANARY,
        timeout_ms=5_000,
    )

    assert result.submitted is True
    assert result.fields_scrubbed is True
    assert page.events == [
        ("click", "#phone"),
        ("press", "#phone:ControlOrMeta+A"),
        ("press", "#phone:Backspace"),
        ("type", "#phone"),
        ("click", "#password"),
        ("press", "#password:ControlOrMeta+A"),
        ("press", "#password:Backspace"),
        ("type", "#password"),
        ("click", "#flt-semantic-node-456"),
        ("clear", "#phone"),
        ("clear", "#password"),
    ]


@pytest.mark.asyncio
async def test_flutter_secret_submit_rechecks_both_values_before_click() -> None:
    browser = PlaywrightPageCaptureBrowser()
    page = _Page(
        url="https://h5.example.com/login",
        clear_phone_after_password_type=True,
    )
    _bind_page(browser, page)

    result = await browser.submit_secret_form(
        session_id="session-auth",
        target_hostname="h5.example.com",
        phone_selector="#phone",
        password_selector="#password",
        submit_selector="#flt-semantic-node-456",
        submit_semantic_target="注册",
        phone=_PHONE_CANARY,
        password=_PASSWORD_CANARY,
        timeout_ms=5_000,
    )

    assert result.submitted is False
    assert result.fields_scrubbed is True
    assert result.reason_code == "secret_form_value_sync_failed"
    assert ("click", "#flt-semantic-node-456") not in page.events


@pytest.mark.asyncio
async def test_flutter_secret_submit_fails_closed_when_live_label_drifts() -> None:
    browser = PlaywrightPageCaptureBrowser()
    page = _Page(url="https://h5.example.com/login")
    _bind_page(browser, page)

    result = await browser.submit_secret_form(
        session_id="session-auth",
        target_hostname="h5.example.com",
        phone_selector="#phone",
        password_selector="#password",
        submit_selector="#flt-semantic-node-456",
        submit_semantic_target="登录",
        phone=_PHONE_CANARY,
        password=_PASSWORD_CANARY,
        timeout_ms=5_000,
    )

    assert result.submitted is False
    assert result.fields_scrubbed is True
    assert result.reason_code == "secret_form_submit_failed"
    assert not any(event[0] == "tap" for event in page.events)


@pytest.mark.asyncio
async def test_flutter_secret_submit_projects_rate_limit_without_raw_message() -> None:
    browser = PlaywrightPageCaptureBrowser()
    page = _Page(
        url="https://h5.example.com/login",
        transient_semantic_effect=True,
        auth_feedback_code="registration_rate_limited",
    )
    _bind_page(browser, page)

    result = await browser.submit_secret_form(
        session_id="session-auth",
        target_hostname="h5.example.com",
        phone_selector="#phone",
        password_selector="#password",
        submit_selector="#flt-semantic-node-456",
        submit_semantic_target="注册",
        phone=_PHONE_CANARY,
        password=_PASSWORD_CANARY,
        timeout_ms=5_000,
    )

    assert result.submitted is False
    assert result.fields_scrubbed is True
    assert result.reason_code == "registration_rate_limited"


@pytest.mark.asyncio
async def test_flutter_secret_submit_captures_one_sample_feedback() -> None:
    page = _Page(
        url="https://h5.example.com/mineloginpage",
        transient_auth_feedback_code="registration_rate_limited",
    )
    result = await page_capture_module._submit_secret_form_on_page(
        page=page,
        target_hostname="h5.example.com",
        phone_selector="#phone",
        password_selector="#password",
        submit_selector="#flt-semantic-node-454",
        submit_semantic_target="注册",
        allowed_request_hostnames=(),
        phone=_PHONE_CANARY,
        password=_PASSWORD_CANARY,
        timeout_ms=8_000,
    )

    assert result.submitted is False
    assert result.fields_scrubbed is True
    assert result.reason_code == "registration_rate_limited"


@pytest.mark.asyncio
async def test_flutter_secret_submit_projects_controller_validation_feedback() -> None:
    page = _Page(
        url="https://h5.example.com/mineloginpage",
        transient_auth_feedback_code="secret_form_controller_username_empty",
    )
    result = await page_capture_module._submit_secret_form_on_page(
        page=page,
        target_hostname="h5.example.com",
        phone_selector="#phone",
        password_selector="#password",
        submit_selector="#flt-semantic-node-454",
        submit_semantic_target="注册",
        allowed_request_hostnames=(),
        phone=_PHONE_CANARY,
        password=_PASSWORD_CANARY,
        timeout_ms=8_000,
    )

    assert result.submitted is False
    assert result.fields_scrubbed is True
    assert result.reason_code == "secret_form_controller_username_empty"


@pytest.mark.asyncio
async def test_flutter_secret_submit_rejects_transient_only_semantic_change() -> None:
    browser = PlaywrightPageCaptureBrowser()
    page = _Page(
        url="https://h5.example.com/login",
        transient_semantic_effect=True,
    )
    _bind_page(browser, page)

    result = await browser.submit_secret_form(
        session_id="session-auth",
        target_hostname="h5.example.com",
        phone_selector="#phone",
        password_selector="#password",
        submit_selector="#flt-semantic-node-456",
        submit_semantic_target="注册",
        phone=_PHONE_CANARY,
        password=_PASSWORD_CANARY,
        timeout_ms=5_000,
    )

    assert result.submitted is False
    assert result.fields_scrubbed is True
    assert result.reason_code == "secret_form_submit_no_request"


@pytest.mark.asyncio
async def test_flutter_secret_submit_requires_observation_after_registration_post() -> None:
    browser = PlaywrightPageCaptureBrowser()
    page = _Page(
        url="https://h5.example.com/login",
        request_on_click_url="https://h5.example.com/api/account/register",
        transient_semantic_effect=True,
    )
    _bind_page(browser, page)

    result = await browser.submit_secret_form(
        session_id="session-auth",
        target_hostname="h5.example.com",
        phone_selector="#phone",
        password_selector="#password",
        submit_selector="#flt-semantic-node-456",
        submit_semantic_target="注册",
        phone=_PHONE_CANARY,
        password=_PASSWORD_CANARY,
        timeout_ms=5_000,
    )

    assert result.submitted is True
    assert result.fields_scrubbed is True
    assert result.reason_code == "secret_form_submit_response_observe_required"


@pytest.mark.asyncio
async def test_flutter_secret_submit_does_not_accept_noncredential_post() -> None:
    browser = PlaywrightPageCaptureBrowser()
    page = _Page(
        url="https://h5.example.com/login",
        request_on_click_url="https://h5.example.com/api/analytics/click",
        transient_semantic_effect=True,
    )
    _bind_page(browser, page)

    result = await browser.submit_secret_form(
        session_id="session-auth",
        target_hostname="h5.example.com",
        phone_selector="#phone",
        password_selector="#password",
        submit_selector="#flt-semantic-node-456",
        submit_semantic_target="注册",
        phone=_PHONE_CANARY,
        password=_PASSWORD_CANARY,
        timeout_ms=5_000,
    )

    assert result.submitted is False
    assert result.fields_scrubbed is True
    assert result.reason_code == "secret_form_submit_noncredential_post_only"


@pytest.mark.asyncio
async def test_browser_rejects_cross_hostname_before_filling() -> None:
    browser = PlaywrightPageCaptureBrowser()
    page = _Page(url="https://identity.example.net/login")
    _bind_page(browser, page)

    result = await browser.submit_secret_form(
        session_id="session-auth",
        target_hostname="h5.example.com",
        phone_selector="#phone",
        password_selector="#password",
        submit_selector="#submit",
        submit_semantic_target="登录",
        phone=_PHONE_CANARY,
        password=_PASSWORD_CANARY,
        timeout_ms=5_000,
    )

    assert result.submitted is False
    assert result.fields_scrubbed is True
    assert result.reason_code == "secret_form_hostname_mismatch"
    assert page.events == []


@pytest.mark.asyncio
async def test_browser_allows_explicit_auth_api_hostname_only() -> None:
    browser = PlaywrightPageCaptureBrowser()
    page = _Page(
        url="https://h5.example.com/login",
        request_on_click_url="https://api.uszim.com/account/register",
    )
    _bind_page(browser, page)

    result = await browser.submit_secret_form(
        session_id="session-auth",
        target_hostname="h5.example.com",
        phone_selector="#phone",
        password_selector="#password",
        submit_selector="#submit",
        submit_semantic_target="登录",
        allowed_request_hostnames=("api.uszim.com",),
        phone=_PHONE_CANARY,
        password=_PASSWORD_CANARY,
        timeout_ms=5_000,
    )

    assert result.submitted is True
    assert page.last_route is not None
    assert page.last_route.continued is True
    assert page.last_route.aborted is False


@pytest.mark.asyncio
async def test_blocked_cross_origin_get_does_not_override_ui_registration_success() -> None:
    browser = PlaywrightPageCaptureBrowser()
    page = _Page(
        url="https://h5.example.com/login",
        request_on_click_url="https://h5.example.com/api/account/register",
        secondary_request_on_click_url="https://cdn.example.net/banner.webp",
    )
    _bind_page(browser, page)

    result = await browser.submit_secret_form(
        session_id="session-auth",
        target_hostname="h5.example.com",
        phone_selector="#phone",
        password_selector="#password",
        submit_selector="#flt-semantic-node-456",
        submit_semantic_target="注册",
        phone=_PHONE_CANARY,
        password=_PASSWORD_CANARY,
        timeout_ms=5_000,
    )

    assert result.submitted is True
    assert result.fields_scrubbed is True
    assert result.reason_code == "secret_form_submitted"


@pytest.mark.asyncio
async def test_blocked_unrelated_post_does_not_override_allowed_registration() -> None:
    browser = PlaywrightPageCaptureBrowser()
    page = _Page(
        url="https://h5.example.com/login",
        request_on_click_url="https://h5.example.com/api/account/register",
        secondary_request_on_click_url="https://sdk.example.net/session/bootstrap",
        secondary_request_on_click_method="POST",
    )
    _bind_page(browser, page)

    result = await browser.submit_secret_form(
        session_id="session-auth",
        target_hostname="h5.example.com",
        phone_selector="#phone",
        password_selector="#password",
        submit_selector="#flt-semantic-node-456",
        submit_semantic_target="注册",
        phone=_PHONE_CANARY,
        password=_PASSWORD_CANARY,
        timeout_ms=5_000,
    )

    assert result.submitted is True
    assert result.fields_scrubbed is True
    assert result.reason_code == "secret_form_submitted"


@pytest.mark.asyncio
async def test_browser_blocks_unlisted_auth_api_hostname() -> None:
    browser = PlaywrightPageCaptureBrowser()
    page = _Page(
        url="https://h5.example.com/login",
        request_on_click_url="https://api.uszim.com/account/register",
    )
    _bind_page(browser, page)

    result = await browser.submit_secret_form(
        session_id="session-auth",
        target_hostname="h5.example.com",
        phone_selector="#phone",
        password_selector="#password",
        submit_selector="#submit",
        submit_semantic_target="登录",
        allowed_request_hostnames=(),
        phone=_PHONE_CANARY,
        password=_PASSWORD_CANARY,
        timeout_ms=5_000,
    )

    assert result.submitted is False
    assert result.reason_code == "cross_hostname_request_blocked"
    assert page.last_route is not None
    assert page.last_route.aborted is True


@pytest.mark.asyncio
async def test_browser_fails_closed_when_secret_field_cannot_be_verified_empty() -> None:
    browser = PlaywrightPageCaptureBrowser()
    page = _Page(
        url="https://h5.example.com/login",
        refuses_clear_selector="#password",
    )
    _bind_page(browser, page)

    result = await browser.submit_secret_form(
        session_id="session-auth",
        target_hostname="h5.example.com",
        phone_selector="#phone",
        password_selector="#password",
        submit_selector="#submit",
        submit_semantic_target="登录",
        phone=_PHONE_CANARY,
        password=_PASSWORD_CANARY,
        timeout_ms=5_000,
    )

    assert result.submitted is False
    assert result.fields_scrubbed is False
    assert result.reason_code == "secret_fields_scrub_failed"
