"""星语 Official App API 的二进制和配置边界契约。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from services.api.app.application.capture_profiles import CaptureProfilePolicy
from services.api.app.core.config import WorkflowSettings
from services.api.app.domain.interaction import (
    ActionIntent,
    InteractionOperation,
    InteractionRiskLevel,
    SurfaceKind,
    SurfaceSessionRef,
    SurfaceSessionSpec,
)
from services.api.app.infrastructure.app_runtime.factory import (
    AppRuntimeConfigurationError,
    AppRuntimeFactory,
)
from services.api.app.infrastructure.app_runtime.official_api import (
    HttpOfficialAppApiClient,
    OfficialAppApiError,
    _http_status_error,
    _is_action_outcome_unknown_error,
)

PNG = b"\x89PNG\r\n\x1a\nbinary"


@pytest.mark.parametrize("status,unknown", [(400, False), (403, False), (500, True), (503, True)])
def test_upstream_business_code_cannot_hide_unknown_server_action(status, unknown):
    error = _http_status_error(httpx.Response(
        status, json={"error_code": "ACTION_EXECUTION_FAILED", "retryable": False},
    ))
    assert error.error_code == "ACTION_EXECUTION_FAILED"
    assert _is_action_outcome_unknown_error(error) is unknown


def _settings() -> WorkflowSettings:
    return WorkflowSettings(
        official_app_base_url="https://official.example.test",
        official_app_auth_header_name="X-Official-Token",
        official_app_auth_token="secret-token",
        official_app_start_session_path="/api/session/start",
        official_app_install_path="/api/app/install",
        official_app_open_path="/api/app/open",
        official_app_capture_path="/api/app/capture",
        official_app_capture_aux_path="/api/app/capture-aux",
        official_app_execute_path="/api/app/execute",
        official_app_close_path="/api/session/close",
    )


async def test_http_client_reads_capture_as_bounded_png_binary() -> None:
    capture_keys: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/session/start":
            assert request.headers["X-Official-Token"] == "secret-token"
            return httpx.Response(
                200,
                json={"request_id": "session-request-1", "session_handle": "private-1"},
            )
        assert request.url.path == "/api/app/capture"
        assert request.headers["X-Official-Token"] == "secret-token"
        capture_keys.append(request.headers["Idempotency-Key"])
        return httpx.Response(
            200,
            content=PNG + (b"" if len(capture_keys) == 1 else b"fresh"),
            headers={
                "content-type": "image/png",
                "x-alioth-width": "2",
                "x-alioth-height": "3",
                "x-alioth-orientation": "portrait",
                "x-alioth-package-name": "com.example.target",
                "x-alioth-activity": ".MainActivity",
            },
        )

    client = HttpOfficialAppApiClient(
        settings=_settings(),
        transport=httpx.MockTransport(handler),
    )
    await client.start_session(
        SurfaceSessionSpec(
            task_id="task-1",
            session_id="session-1",
            surface_kind=SurfaceKind.ANDROID,
            platform_build_ref="build-1",
        )
    )
    response = await client.capture_png(
        session=SurfaceSessionRef(
            task_id="task-1",
            session_id="session-1",
            surface_kind=SurfaceKind.ANDROID,
            primary_surface_ref="app-surface:session-1",
        ),
        profile=CaptureProfilePolicy.for_surface(SurfaceKind.ANDROID),
    )
    second = await client.capture_png(
        session=SurfaceSessionRef(
            task_id="task-1",
            session_id="session-1",
            surface_kind=SurfaceKind.ANDROID,
            primary_surface_ref="app-surface:session-1",
        ),
        profile=CaptureProfilePolicy.for_surface(SurfaceKind.ANDROID),
    )

    assert response.png_bytes == PNG
    assert second.png_bytes == PNG + b"fresh"
    assert len(set(capture_keys)) == 2
    assert all(key.startswith("capture:") for key in capture_keys)
    assert response.content_type == "image/png"
    assert response.width == 2
    assert response.height == 3


async def test_capture_retries_three_times_with_same_key_and_next_call_gets_new_key() -> None:
    capture_keys: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/session/start":
            return httpx.Response(
                200,
                json={"request_id": "session-request-1", "session_handle": "private-1"},
            )
        assert request.url.path == "/api/app/capture"
        capture_keys.append(request.headers["Idempotency-Key"])
        if len(capture_keys) <= 2:
            return httpx.Response(502, text="transient capture failure")
        return httpx.Response(
            200,
            content=PNG,
            headers={
                "content-type": "image/png",
                "x-alioth-width": "2",
                "x-alioth-height": "3",
                "x-alioth-orientation": "portrait",
            },
        )

    client = HttpOfficialAppApiClient(
        settings=_settings(),
        transport=httpx.MockTransport(handler),
    )
    session_spec = SurfaceSessionSpec(
        task_id="task-capture-retry",
        session_id="session-capture-retry",
        surface_kind=SurfaceKind.ANDROID,
        platform_build_ref="build-capture-retry",
    )
    await client.start_session(session_spec)
    session = SurfaceSessionRef(
        task_id=session_spec.task_id,
        session_id=session_spec.session_id,
        surface_kind=SurfaceKind.ANDROID,
        primary_surface_ref="app-surface:session-capture-retry",
    )
    profile = CaptureProfilePolicy.for_surface(SurfaceKind.ANDROID)

    first = await client.capture_png(session=session, profile=profile)
    second = await client.capture_png(session=session, profile=profile)

    assert first.png_bytes == PNG
    assert second.png_bytes == PNG
    assert len(capture_keys) == 4
    assert len(set(capture_keys[:3])) == 1
    assert capture_keys[3] != capture_keys[0]


@pytest.mark.parametrize("stream_error", [httpx.ReadError, httpx.ReadTimeout])
async def test_capture_retries_when_png_stream_is_interrupted_after_headers(
    monkeypatch: pytest.MonkeyPatch,
    stream_error: type[httpx.RequestError],
) -> None:
    """响应头到达后 PNG 串流中断时，仍须刷新通道并重新采集。"""

    async def _no_sleep(*_args, **_kwargs) -> None:
        return None

    monkeypatch.setattr(
        "services.api.app.infrastructure.app_runtime.official_api.asyncio.sleep",
        _no_sleep,
    )

    class _InterruptedPngStream(httpx.AsyncByteStream):
        def __init__(self, request: httpx.Request) -> None:
            self.request = request

        async def __aiter__(self):
            yield PNG[:8]
            raise stream_error("模拟响应体中断", request=self.request)

        async def aclose(self) -> None:
            return None

    capture_requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/session/start":
            return httpx.Response(
                200,
                json={"request_id": "session-request-1", "session_handle": "private-1"},
            )
        capture_requests.append(request)
        headers = {
            "content-type": "image/png",
            "x-alioth-width": "2",
            "x-alioth-height": "3",
            "x-alioth-orientation": "portrait",
        }
        if len(capture_requests) == 1:
            return httpx.Response(
                200,
                stream=_InterruptedPngStream(request),
                headers=headers,
                request=request,
            )
        return httpx.Response(200, content=PNG, headers=headers, request=request)

    client = HttpOfficialAppApiClient(
        settings=_settings(),
        transport=httpx.MockTransport(handler),
    )
    session_spec = SurfaceSessionSpec(
        task_id="task-capture-stream-retry",
        session_id="session-capture-stream-retry",
        surface_kind=SurfaceKind.ANDROID,
        platform_build_ref="build-capture-stream-retry",
    )
    await client.start_session(session_spec)

    result = await client.capture_png(
        session=SurfaceSessionRef(
            task_id=session_spec.task_id,
            session_id=session_spec.session_id,
            surface_kind=SurfaceKind.ANDROID,
            primary_surface_ref="app-surface:capture-stream-retry",
        ),
        profile=CaptureProfilePolicy.for_surface(SurfaceKind.ANDROID),
    )

    assert result.png_bytes == PNG
    assert len(capture_requests) == 2
    assert capture_requests[0].headers["Idempotency-Key"] != capture_requests[1].headers[
        "Idempotency-Key"
    ]
    assert "X-Alioth-Force-Adb-Refresh" not in capture_requests[0].headers
    assert capture_requests[1].headers["X-Alioth-Force-Adb-Refresh"] == "1"
    assert client._capture_transient_streak.get(session_spec.session_id, 0) == 0


async def test_capture_preserves_explicit_retry_budget_above_capture_floor() -> None:
    capture_attempts = 0
    capture_keys: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal capture_attempts
        if request.url.path == "/api/session/start":
            return httpx.Response(
                200,
                json={"request_id": "session-request-1", "session_handle": "private-1"},
            )
        capture_attempts += 1
        capture_keys.append(request.headers["Idempotency-Key"])
        if capture_attempts <= 3:
            return httpx.Response(502, text="transient capture failure")
        return httpx.Response(
            200,
            content=PNG,
            headers={
                "content-type": "image/png",
                "x-alioth-width": "2",
                "x-alioth-height": "3",
                "x-alioth-orientation": "portrait",
            },
        )

    client = HttpOfficialAppApiClient(
        settings=_settings(),
        transport=httpx.MockTransport(handler),
        max_retries=3,
    )
    session_spec = SurfaceSessionSpec(
        task_id="task-capture-explicit-retry",
        session_id="session-capture-explicit-retry",
        surface_kind=SurfaceKind.ANDROID,
        platform_build_ref="build-capture-explicit-retry",
    )
    await client.start_session(session_spec)

    await client.capture_png(
        session=SurfaceSessionRef(
            task_id=session_spec.task_id,
            session_id=session_spec.session_id,
            surface_kind=SurfaceKind.ANDROID,
            primary_surface_ref="app-surface:session-capture-explicit-retry",
        ),
        profile=CaptureProfilePolicy.for_surface(SurfaceKind.ANDROID),
    )

    assert capture_attempts == 4
    assert len(set(capture_keys)) == 1


@pytest.mark.parametrize("failure", ["502", "500", "timeout", "network", "invalid_contract"])
async def test_execute_does_not_retry_unknown_server_failure(failure) -> None:
    execute_requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/session/start":
            return httpx.Response(
                200,
                json={"request_id": "session-request-1", "session_handle": "private-1"},
            )
        assert request.url.path == "/api/app/execute"
        execute_requests.append(request)
        if failure == "timeout":
            raise httpx.ReadTimeout("模拟响应超时", request=request)
        if failure == "network":
            raise httpx.ReadError("模拟响应中断", request=request)
        if failure == "invalid_contract":
            return httpx.Response(200, json={"succeeded": True})
        return httpx.Response(
            int(failure),
            json={"error_code": "ACTION_EXECUTION_FAILED", "retryable": True},
            headers={"x-request-id": "action-request-unknown"},
        )

    client = HttpOfficialAppApiClient(
        settings=_settings(),
        transport=httpx.MockTransport(handler),
        max_retries=3,
    )
    spec = SurfaceSessionSpec(
        task_id="task-action-unknown",
        session_id="session-action-unknown",
        surface_kind=SurfaceKind.ANDROID,
        platform_build_ref="build-action-unknown",
    )
    await client.start_session(spec)
    session = SurfaceSessionRef(
        task_id=spec.task_id,
        session_id=spec.session_id,
        surface_kind=SurfaceKind.ANDROID,
        primary_surface_ref="app-surface:session-action-unknown",
    )
    intent = ActionIntent(
        task_id=spec.task_id,
        session_id=spec.session_id,
        intent_id="intent-action-unknown",
        idempotency_key="intent-action-unknown",
        surface_kind=SurfaceKind.ANDROID,
        source_capture_id="capture-action-unknown",
        action_type=InteractionOperation.BACK,
        semantic_target=None,
        purpose="动作结果未知测试",
        risk_level=InteractionRiskLevel.LOW,
        capture_profile=CaptureProfilePolicy.for_surface(SurfaceKind.ANDROID),
        deadline=datetime.now(UTC) + timedelta(minutes=1),
    )

    with pytest.raises(OfficialAppApiError) as caught:
        await client.execute(
            session=session,
            intent=intent,
            action_execution_id="action-unknown",
        )

    assert caught.value.error_code == "ACTION_OUTCOME_UNKNOWN"
    assert caught.value.retryable is False
    assert caught.value.request_id == (
        "action-request-unknown" if failure in {"502", "500"} else None
    )
    assert len(execute_requests) == 1
    assert execute_requests[0].headers["Idempotency-Key"] == "action:action-unknown"


async def test_ui_tree_capture_retries_with_capture_floor() -> None:
    capture_attempts = 0
    capture_keys: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal capture_attempts
        if request.url.path == "/api/session/start":
            return httpx.Response(
                200,
                json={"request_id": "session-request-1", "session_handle": "private-1"},
            )
        assert request.url.path == "/v1/apps/capture-ui-tree"
        capture_attempts += 1
        capture_keys.append(request.headers["Idempotency-Key"])
        if capture_attempts <= 2:
            return httpx.Response(502, text="transient ui tree failure")
        return httpx.Response(
            200,
            content=b'<?xml version="1.0"?><hierarchy/>',
            headers={"content-type": "application/xml"},
        )

    client = HttpOfficialAppApiClient(
        settings=_settings(),
        transport=httpx.MockTransport(handler),
    )
    session_spec = SurfaceSessionSpec(
        task_id="task-ui-tree-retry",
        session_id="session-ui-tree-retry",
        surface_kind=SurfaceKind.ANDROID,
        platform_build_ref="build-ui-tree-retry",
    )
    await client.start_session(session_spec)

    result = await client.capture_ui_tree(
        session=SurfaceSessionRef(
            task_id=session_spec.task_id,
            session_id=session_spec.session_id,
            surface_kind=SurfaceKind.ANDROID,
            primary_surface_ref="app-surface:session-ui-tree-retry",
        ),
        profile=CaptureProfilePolicy.for_surface(SurfaceKind.ANDROID),
    )

    assert result.xml_text == '<?xml version="1.0"?><hierarchy/>'
    assert capture_attempts == 3
    assert len(set(capture_keys)) == 1


@pytest.mark.parametrize(
    ("value", "error_type"),
    [(True, TypeError), (-1, ValueError), (1.5, TypeError)],
)
async def test_request_with_retry_validates_per_call_retry_override(
    value: object,
    error_type: type[Exception],
) -> None:
    client = HttpOfficialAppApiClient(
        settings=_settings(),
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(500),  # type: ignore[arg-type]
        ),
    )

    with pytest.raises(error_type, match="max_retries"):
        await client._request_with_retry(
            method="GET",
            path="/api/retry-override",
            payload={},
            idempotency_key="retry-override-test",
            max_retries=value,  # type: ignore[arg-type]
        )


async def test_http_client_reattach_preserves_session_spec_contract() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/sessions/reattach"
        assert request.headers["Idempotency-Key"] == "reattach:session-reattach-1"
        assert request.read().decode("utf-8")
        payload = request.content.decode("utf-8")
        assert '"account_environment_ref":"account-env-1"' in payload
        assert '"options":{"locale":"zh-CN"}' in payload
        return httpx.Response(
            200,
            json={"request_id": "reattach-request-1", "session_handle": "private-reattach-1"},
        )

    client = HttpOfficialAppApiClient(
        settings=_settings(),
        transport=httpx.MockTransport(handler),
    )
    result = await client.reattach_session(
        SurfaceSessionSpec(
            task_id="task-reattach-1",
            session_id="session-reattach-1",
            surface_kind=SurfaceKind.ANDROID,
            account_environment_ref="account-env-1",
            platform_build_ref="build-reattach-1",
            options={"locale": "zh-CN"},
        )
    )

    assert result.session_handle == "private-reattach-1"


async def test_http_client_accepts_bounded_aux_with_matching_identity() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/session/start":
            return httpx.Response(
                200,
                json={"request_id": "session-request-1", "session_handle": "private-1"},
            )
        assert request.url.path == "/api/app/capture-aux"
        return httpx.Response(
            200,
            json={
                "artifact_type": "device_log",
                "content_type": "application/json",
                "request_id": "aux-request-1",
                "payload": {
                    "status": "captured",
                    "task_id": "task-1",
                    "session_id": "session-1",
                    "package_name": "com.example.target",
                    "lines": ["I/App: ready"],
                },
            },
        )

    client = HttpOfficialAppApiClient(
        settings=_settings(),
        transport=httpx.MockTransport(handler),
    )
    await client.start_session(
        SurfaceSessionSpec(
            task_id="task-1",
            session_id="session-1",
            surface_kind=SurfaceKind.ANDROID,
            platform_build_ref="build-1",
        )
    )

    result = await client.capture_aux(
        SurfaceSessionRef(
            task_id="task-1",
            session_id="session-1",
            surface_kind=SurfaceKind.ANDROID,
            primary_surface_ref="app-surface:session-1",
        ),
        artifact_type="device_log",
        package_name="com.example.target",
    )

    assert result.artifact_type == "device_log"
    assert result.payload["status"] == "captured"


@pytest.mark.parametrize(
    "response_payload",
    [
        {
            "artifact_type": "network_trace",
            "content_type": "application/json",
            "payload": {"status": "captured"},
        },
        {
            "artifact_type": "device_log",
            "content_type": "text/plain",
            "payload": {"status": "captured"},
        },
        {
            "artifact_type": "device_log",
            "content_type": "application/json",
            "payload": {"status": "captured", "session_id": "other-session"},
        },
        {
            "artifact_type": "device_log",
            "content_type": "application/json",
            "payload": {},
        },
    ],
)
async def test_http_client_rejects_aux_contract_drift(
    response_payload: dict[str, object],
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/session/start":
            return httpx.Response(
                200,
                json={"request_id": "session-request-1", "session_handle": "private-1"},
            )
        return httpx.Response(200, json=response_payload)

    client = HttpOfficialAppApiClient(
        settings=_settings(),
        transport=httpx.MockTransport(handler),
    )
    await client.start_session(
        SurfaceSessionSpec(
            task_id="task-1",
            session_id="session-1",
            surface_kind=SurfaceKind.ANDROID,
            platform_build_ref="build-1",
        )
    )

    with pytest.raises(OfficialAppApiError) as caught:
        await client.capture_aux(
            SurfaceSessionRef(
                task_id="task-1",
                session_id="session-1",
                surface_kind=SurfaceKind.ANDROID,
                primary_surface_ref="app-surface:session-1",
            ),
            artifact_type="device_log",
            package_name="com.example.target",
        )

    assert caught.value.error_code == "CAPTURE_CONTRACT_INVALID"


async def test_http_client_rejects_oversized_aux_response() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/session/start":
            return httpx.Response(
                200,
                json={"request_id": "session-request-1", "session_handle": "private-1"},
            )
        return httpx.Response(
            200,
            json={
                "artifact_type": "device_log",
                "content_type": "application/json",
                "payload": {"status": "captured", "content": "x" * (1024 * 1024)},
            },
        )

    client = HttpOfficialAppApiClient(
        settings=_settings(),
        transport=httpx.MockTransport(handler),
    )
    await client.start_session(
        SurfaceSessionSpec(
            task_id="task-1",
            session_id="session-1",
            surface_kind=SurfaceKind.ANDROID,
            platform_build_ref="build-1",
        )
    )

    with pytest.raises(OfficialAppApiError) as caught:
        await client.capture_aux(
            SurfaceSessionRef(
                task_id="task-1",
                session_id="session-1",
                surface_kind=SurfaceKind.ANDROID,
                primary_surface_ref="app-surface:session-1",
            ),
            artifact_type="device_log",
        )

    assert caught.value.error_code == "CAPTURE_CONTRACT_INVALID"


def test_factory_rejects_official_runtime_without_explicit_client_and_auth_configuration() -> None:
    with pytest.raises(AppRuntimeConfigurationError, match="OfficialAppApiClient"):
        AppRuntimeFactory(environment="local", workflow_settings=_settings()).build("official")

    with pytest.raises(AppRuntimeConfigurationError, match="认证"):
        AppRuntimeFactory(
            environment="local",
            workflow_settings=WorkflowSettings(official_app_base_url="https://official.example.test"),
        ).build("official", official_client=object())


@pytest.mark.parametrize(
    "headers",
    [
        {
            "content-type": "image/png",
            "x-alioth-height": "3",
            "x-alioth-orientation": "portrait",
        },
        {
            "content-type": "image/png",
            "x-alioth-width": "0",
            "x-alioth-height": "3",
            "x-alioth-orientation": "portrait",
        },
        {
            "content-type": "image/png",
            "x-alioth-width": "2",
            "x-alioth-height": "invalid",
            "x-alioth-orientation": "portrait",
        },
        {
            "content-type": "image/png",
            "x-alioth-width": "2",
            "x-alioth-height": "3",
            "x-alioth-orientation": "diagonal",
        },
    ],
)
async def test_http_client_maps_invalid_capture_headers_to_contract_error(
    headers: dict[str, str],
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/session/start":
            return httpx.Response(
                200,
                json={"request_id": "session-request-1", "session_handle": "private-1"},
            )
        return httpx.Response(200, content=PNG, headers=headers)

    client = HttpOfficialAppApiClient(
        settings=_settings(),
        transport=httpx.MockTransport(handler),
    )
    await client.start_session(
        SurfaceSessionSpec(
            task_id="task-1",
            session_id="session-1",
            surface_kind=SurfaceKind.ANDROID,
            platform_build_ref="build-1",
        )
    )

    with pytest.raises(OfficialAppApiError) as error:
        await client.capture_png(
            session=SurfaceSessionRef(
                task_id="task-1",
                session_id="session-1",
                surface_kind=SurfaceKind.ANDROID,
                primary_surface_ref="app-surface:session-1",
            ),
            profile=CaptureProfilePolicy.for_surface(SurfaceKind.ANDROID),
        )

    assert error.value.error_code == "OFFICIAL_RESPONSE_INVALID"
    assert error.value.retryable is False


def test_factory_rejects_client_without_minimum_official_protocol() -> None:
    with pytest.raises(AppRuntimeConfigurationError, match="最小协议"):
        AppRuntimeFactory(environment="local", workflow_settings=_settings()).build(
            "official",
            official_client=object(),
        )


def test_capture_request_timeout_covers_cloud_phone_pull_budget() -> None:
    from services.api.app.infrastructure.app_runtime.official_api import (
        _CAPTURE_TIMEOUT_FLOOR_SECONDS,
        _capture_request_timeout,
    )
    from services.api.app.infrastructure.device import adb_session_health as health_mod
    from services.api.app.infrastructure.device.adb_ops import (
        _CAPTURE_IN_FRAME_FAILURE_RETRIES,
        _SCREENSHOT_CLOUD_PHONE_PULL_TIMEOUT_SECONDS,
        _SCREENSHOT_CLOUD_PHONE_RETRY_PULL_TIMEOUT_SECONDS,
    )

    # 通用 Official 超时仍可为 60s；Capture 必须盖住「身份稳定×帧内最坏」墙钟
    settings = WorkflowSettings(
        official_app_base_url="https://official.example.test",
        official_app_timeout_seconds=60.0,
        official_app_capture_timeout_seconds=60.0,
    )
    timeout = _capture_request_timeout(settings)
    assert _CAPTURE_TIMEOUT_FLOOR_SECONDS == 780.0
    assert timeout.read == 780.0
    assert timeout.connect == 780.0
    ready = float(health_mod._CAPTURE_REFRESH_READY_TIMEOUT_SECONDS)
    # 与 device_bridge._CAPTURE_IDENTITY_ATTEMPTS 对齐：首帧刷新一次 + 最多 3 次采图路径
    identity_attempts = 3
    per_frame = (
        float(_SCREENSHOT_CLOUD_PHONE_PULL_TIMEOUT_SECONDS)
        + _CAPTURE_IN_FRAME_FAILURE_RETRIES
        * (ready + float(_SCREENSHOT_CLOUD_PHONE_RETRY_PULL_TIMEOUT_SECONDS))
    )
    worst = ready + identity_attempts * per_frame
    assert worst <= float(timeout.read), (
        f"身份稳定×帧内最坏预算 {worst}s 不得超过 Capture HTTP floor {timeout.read}s"
    )


@pytest.mark.asyncio
async def test_capture_png_forces_adb_refresh_after_transient_502(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """同 session 连续 502 时后续请求必须带 force_adb_refresh。"""

    async def _no_sleep(*_args, **_kwargs) -> None:
        return None

    monkeypatch.setattr(
        "services.api.app.infrastructure.app_runtime.official_api.asyncio.sleep",
        _no_sleep,
    )

    seen_bodies: list[dict] = []
    seen_refresh_headers: list[str | None] = []
    calls = {"n": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/session/start":
            return httpx.Response(
                200,
                json={"request_id": "session-request-1", "session_handle": "private-1"},
            )
        import json as _json

        body = _json.loads(request.content.decode("utf-8"))
        seen_bodies.append(body)
        seen_refresh_headers.append(request.headers.get("X-Alioth-Force-Adb-Refresh"))
        calls["n"] += 1
        if calls["n"] <= 2:
            return httpx.Response(502, json={"error_code": "CAPTURE_FAILED", "retryable": True})
        return httpx.Response(
            200,
            content=PNG,
            headers={
                "content-type": "image/png",
                "x-alioth-width": "2",
                "x-alioth-height": "3",
                "x-alioth-orientation": "portrait",
                "x-alioth-package-name": "com.example.target",
                "x-alioth-activity": ".MainActivity",
            },
        )

    settings = WorkflowSettings(
        official_app_base_url="https://official.example.test",
        official_app_auth_header_name="X-Official-Token",
        official_app_auth_token="secret-token",
        official_app_start_session_path="/api/session/start",
        official_app_install_path="/api/app/install",
        official_app_open_path="/api/app/open",
        official_app_capture_path="/api/app/capture",
        official_app_capture_aux_path="/api/app/capture-aux",
        official_app_execute_path="/api/app/execute",
        official_app_close_path="/api/session/close",
        official_app_timeout_seconds=60.0,
        official_app_capture_timeout_seconds=240.0,
    )
    client = HttpOfficialAppApiClient(
        settings=settings,
        transport=httpx.MockTransport(handler),
        max_retries=3,
    )
    session_spec = SurfaceSessionSpec(
        task_id="task-1",
        session_id="session-1",
        surface_kind=SurfaceKind.ANDROID,
        platform_build_ref="build-1",
    )
    await client.start_session(session_spec)
    profile = CaptureProfilePolicy.for_surface(SurfaceKind.ANDROID)
    result = await client.capture_png(
        session=SurfaceSessionRef(
            task_id="task-1",
            session_id="session-1",
            surface_kind=SurfaceKind.ANDROID,
            primary_surface_ref="surface-1",
        ),
        profile=profile,
    )
    assert result.width == 2
    assert calls["n"] >= 3
    assert seen_bodies[0].get("force_adb_refresh") in (None, False)
    assert all(body == seen_bodies[0] for body in seen_bodies)
    assert seen_refresh_headers == [None, "1", "1"]
    assert client._capture_transient_streak.get("session-1", 0) == 0


@pytest.mark.asyncio
async def test_capture_png_uses_capture_timeout_not_global_timeout() -> None:
    seen_timeout_reads: list[float | None] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/session/start":
            return httpx.Response(
                200,
                json={"request_id": "session-request-1", "session_handle": "private-1"},
            )
        return httpx.Response(
            200,
            content=PNG,
            headers={
                "content-type": "image/png",
                "x-alioth-width": "2",
                "x-alioth-height": "3",
                "x-alioth-orientation": "portrait",
                "x-alioth-package-name": "com.example.target",
                "x-alioth-activity": ".MainActivity",
            },
        )

    settings = WorkflowSettings(
        official_app_base_url="https://official.example.test",
        official_app_auth_header_name="X-Official-Token",
        official_app_auth_token="secret-token",
        official_app_start_session_path="/api/session/start",
        official_app_install_path="/api/app/install",
        official_app_open_path="/api/app/open",
        official_app_capture_path="/api/app/capture",
        official_app_capture_aux_path="/api/app/capture-aux",
        official_app_execute_path="/api/app/execute",
        official_app_close_path="/api/session/close",
        official_app_timeout_seconds=60.0,
        official_app_capture_timeout_seconds=780.0,
    )
    client = HttpOfficialAppApiClient(
        settings=settings,
        transport=httpx.MockTransport(handler),
    )
    original_send = client._client.send

    async def tracking_send(request: httpx.Request, **kwargs):
        timeout = request.extensions.get("timeout")
        if isinstance(timeout, dict):
            seen_timeout_reads.append(timeout.get("read"))
        elif isinstance(timeout, httpx.Timeout):
            seen_timeout_reads.append(timeout.read)
        else:
            seen_timeout_reads.append(None)
        return await original_send(request, **kwargs)

    client._client.send = tracking_send  # type: ignore[method-assign]
    await client.start_session(
        SurfaceSessionSpec(
            task_id="task-1",
            session_id="session-1",
            surface_kind=SurfaceKind.ANDROID,
            platform_build_ref="build-1",
        )
    )
    await client.capture_png(
        session=SurfaceSessionRef(
            task_id="task-1",
            session_id="session-1",
            surface_kind=SurfaceKind.ANDROID,
            primary_surface_ref="app-surface:session-1",
        ),
        profile=CaptureProfilePolicy.for_surface(SurfaceKind.ANDROID),
    )
    assert 780.0 in seen_timeout_reads, (
        f"expected capture read timeout 780s, got {seen_timeout_reads!r}"
    )
