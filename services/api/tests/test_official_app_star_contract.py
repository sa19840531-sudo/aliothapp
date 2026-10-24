"""星语 Official App HTTP 合同的本地 Mock 闸门。"""

from __future__ import annotations

import hashlib
import logging
import os
from collections.abc import Awaitable, Callable
from uuid import uuid4

import httpx
import pytest

from services.api.app.application.capture_profiles import CaptureProfilePolicy
from services.api.app.core.config import WorkflowSettings
from services.api.app.domain.interaction import (
    AppInstallRequest,
    AppOpenRequest,
    SurfaceKind,
    SurfaceSessionRef,
    SurfaceSessionSpec,
)
from services.api.app.infrastructure.app_runtime.official import (
    OfficialAppRuntime,
    OfficialAppRuntimeError,
)
from services.api.app.infrastructure.app_runtime.official_api import (
    HttpOfficialAppApiClient,
    OfficialAppApiError,
)

PNG = b"\x89PNG\r\n\x1a\nstar-contract"
AUTH_TOKEN = "star-secret-token"
ADAPTER_LOGGER = "services.api.app.infrastructure.app_runtime.official_api"
ALLOWED_LOG_FIELDS = {
    "task_id",
    "session_id",
    "request_id",
    "capture_id",
    "artifact_id",
    "checksum",
    "size",
    "error_code",
}


def _settings(*, max_screenshot_bytes: int = 1024) -> WorkflowSettings:
    return WorkflowSettings(
        official_app_base_url="https://star.example.test",
        official_app_timeout_seconds=0.2,
        official_app_max_screenshot_bytes=max_screenshot_bytes,
        official_app_auth_header_name="X-Star-Authorization",
        official_app_auth_token=AUTH_TOKEN,
        official_app_start_session_path="/v1/sessions",
        official_app_install_path="/v1/apps/install",
        official_app_open_path="/v1/apps/open",
        official_app_capture_path="/v1/apps/capture",
        official_app_execute_path="/v1/apps/execute",
        official_app_close_path="/v1/sessions/close",
    )


def _spec() -> SurfaceSessionSpec:
    return SurfaceSessionSpec(
        task_id="task-star-1",
        session_id="session-star-1",
        surface_kind=SurfaceKind.ANDROID,
        platform_build_ref="build-star-1",
    )


async def _opened_runtime(
    handler: Callable[[httpx.Request], Awaitable[httpx.Response]],
    *,
    max_screenshot_bytes: int = 1024,
) -> tuple[OfficialAppRuntime, object]:
    client = HttpOfficialAppApiClient(
        settings=_settings(max_screenshot_bytes=max_screenshot_bytes),
        transport=httpx.MockTransport(handler),  # type: ignore[arg-type]
    )
    runtime = OfficialAppRuntime(api_client=client, max_screenshot_bytes=max_screenshot_bytes)
    session = await runtime.start_session(_spec())
    await runtime.open_app(
        AppOpenRequest(
            task_id="task-star-1",
            session_id="session-star-1",
            package_name="com.example.star",
        )
    )
    return runtime, session


async def test_star_mock_contract_injects_auth_and_maps_png_to_raw_artifact_set() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/v1/sessions":
            return httpx.Response(
                200,
                json={"request_id": "request-session-1", "session_handle": "private-handle"},
            )
        if request.url.path == "/v1/apps/open":
            return httpx.Response(
                200,
                json={
                    "request_id": "request-open-1",
                    "succeeded": True,
                    "retryable": False,
                    "package_name": "com.example.star",
                    "foreground_verified": True,
                },
            )
        if request.url.path == "/v1/apps/capture-ui-tree":
            return httpx.Response(
                200,
                content=b"<hierarchy><node text=\"home\" /></hierarchy>",
                headers={"content-type": "application/xml"},
            )
        assert request.url.path == "/v1/apps/capture"
        return httpx.Response(
            200,
            content=PNG,
            headers={
                "content-type": "image/png",
                "x-alioth-width": "1080",
                "x-alioth-height": "1920",
                "x-alioth-orientation": "portrait",
                "x-alioth-sha256": hashlib.sha256(PNG).hexdigest(),
            },
        )

    runtime, session = await _opened_runtime(handler)
    artifacts = await runtime.capture(
        session=session,
        profile=CaptureProfilePolicy.for_surface(SurfaceKind.ANDROID),
    )

    assert {request.url.path for request in requests} == {
        "/v1/sessions",
            "/v1/apps/open",
            "/v1/apps/capture",
            "/v1/apps/capture-ui-tree",
            "/v1/apps/capture-aux",
        }
    assert all(request.headers["X-Star-Authorization"] == AUTH_TOKEN for request in requests)
    assert requests[0].headers["Idempotency-Key"] == "session:session-star-1"
    artifact_payloads = [(item.artifact_type, item.content) for item in artifacts.artifacts]
    assert artifact_payloads == [
        ("screenshot", PNG),
        ("action_trace", ()),
        ("ui_tree", b"<hierarchy><node text=\"home\" /></hierarchy>"),
        (
            "media_state",
            {
                "schema_version": "app-media-state.v1",
                "source": "android_accessibility_class",
                "item_count": 0,
                "items_truncated": False,
                "capture_degraded": True,
                "playback_state_available": False,
                "native_interaction": "not_observed",
                "continuous_observation": False,
                "gap_codes": ("playback_progress_material_missing",),
                "limitations": (
                    "accessibility_hierarchy_does_not_expose_"
                    "playback_timing_or_resource_url",
                    "hidden_player_controls_project_empty_media_state_"
                    "wake_before_capture",
                ),
                "items": (),
                "display_candidates": (),
            },
        ),
    ]
    assert artifact_payloads[3][1]["schema_version"] == "app-media-state.v1"
    assert artifact_payloads[3][1]["source"] == "android_accessibility_class"
    assert set(artifacts.metadata) == {
        "package_name",
        "activity",
        "width",
        "height",
        "orientation",
        "image_signature",
    }


@pytest.mark.parametrize(
    ("headers", "content", "max_screenshot_bytes"),
    [
        ({"content-type": "image/jpeg"}, PNG, 1024),
        ({"content-type": "image/png", "x-alioth-sha256": "0" * 64}, PNG, 1024),
        ({"content-type": "image/png"}, PNG + b"x" * 32, len(PNG)),
    ],
)
async def test_star_mock_contract_rejects_non_png_invalid_signature_and_oversize_response(
    headers: dict[str, str],
    content: bytes,
    max_screenshot_bytes: int,
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/sessions":
            return httpx.Response(
                200,
                json={"request_id": "request-session-1", "session_handle": "private-handle"},
            )
        if request.url.path == "/v1/apps/open":
            return httpx.Response(
                200,
                json={
                    "request_id": "request-open-1",
                    "succeeded": True,
                    "retryable": False,
                    "package_name": "com.example.star",
                    "foreground_verified": True,
                },
            )
        return httpx.Response(
            200,
            content=content,
            headers={
                "x-alioth-width": "1080",
                "x-alioth-height": "1920",
                "x-alioth-orientation": "portrait",
                **headers,
            },
        )

    runtime, session = await _opened_runtime(handler, max_screenshot_bytes=max_screenshot_bytes)

    with pytest.raises(OfficialAppRuntimeError) as error:
        await runtime.capture(
            session=session,
            profile=CaptureProfilePolicy.for_surface(SurfaceKind.ANDROID),
        )

    assert error.value.error_code == "CAPTURE_INVALID"
    assert error.value.retryable is False


async def test_star_mock_contract_retries_transient_failure_and_logs_only_whitelisted_fields(
    caplog: pytest.LogCaptureFixture,
) -> None:
    attempts = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(
                503,
                headers={"x-request-id": "request-transient-1"},
                text="do not disclose provider endpoint or credentials",
            )
        return httpx.Response(
            200,
            json={"request_id": "request-session-2", "session_handle": "private-handle"},
        )

    caplog.set_level(logging.INFO, logger=ADAPTER_LOGGER)
    client = HttpOfficialAppApiClient(
        settings=_settings(),
        transport=httpx.MockTransport(handler),
    )

    result = await client.start_session(_spec())

    assert result.request_id == "request-session-2"
    assert attempts == 2
    adapter_records = [
        record
        for record in caplog.records
        if record.name == ADAPTER_LOGGER
    ]
    joined = "\n".join(record.getMessage() for record in adapter_records)
    assert AUTH_TOKEN not in joined
    assert "private-handle" not in joined
    assert "star.example.test" not in joined
    assert all(
        set(getattr(record, "official_app_fields", {})) <= ALLOWED_LOG_FIELDS
        for record in adapter_records
    )


async def test_star_mock_contract_maps_timeout_without_leaking_transport_message() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout(f"timeout {AUTH_TOKEN}", request=request)

    client = HttpOfficialAppApiClient(
        settings=_settings(),
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(OfficialAppApiError) as error:
        await client.start_session(_spec())

    assert error.value.error_code == "OFFICIAL_API_TIMEOUT"
    assert error.value.retryable is True
    assert AUTH_TOKEN not in str(error.value)


@pytest.mark.parametrize(
    "payload",
    [
        {"request_id": "request-invalid-1"},
        {"request_id": "request-invalid-1", "session_handle": ""},
    ],
)
async def test_star_mock_contract_maps_invalid_success_response_as_non_retryable(
    payload: dict[str, str],
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    client = HttpOfficialAppApiClient(
        settings=_settings(),
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(OfficialAppApiError) as error:
        await client.start_session(_spec())

    assert error.value.error_code == "OFFICIAL_RESPONSE_INVALID"
    assert error.value.retryable is False


async def test_star_live_contract_runs_only_with_explicit_isolated_configuration() -> None:
    """真实星语合同入口只调用官方 API，不触碰 PostgreSQL 或 Fake Runtime。"""
    settings, install_request = _live_contract_configuration_or_skip()
    client = HttpOfficialAppApiClient(settings=settings)
    runtime = OfficialAppRuntime(
        api_client=client,
        max_screenshot_bytes=settings.official_app_max_screenshot_bytes,
    )
    session: SurfaceSessionRef | None = None
    try:
        session = await runtime.start_session(
            SurfaceSessionSpec(
                task_id=install_request.task_id,
                session_id=install_request.session_id,
                surface_kind=SurfaceKind.ANDROID,
                platform_build_ref=install_request.platform_build_ref,
            )
        )
        installed = await runtime.ensure_installed(install_request)
        assert installed.succeeded is True
        assert installed.installed_package == install_request.package_name
        opened = await runtime.open_app(
            AppOpenRequest(
                task_id=install_request.task_id,
                session_id=install_request.session_id,
                package_name=install_request.package_name,
            )
        )
        assert opened.succeeded is True
        assert opened.foreground_verified is True
        artifacts = await runtime.capture(
            session=session,
            profile=CaptureProfilePolicy.for_surface(SurfaceKind.ANDROID),
        )
        assert len(artifacts.artifacts) == 1
        assert artifacts.artifacts[0].artifact_type == "screenshot"
        assert artifacts.artifacts[0].content.startswith(b"\x89PNG\r\n\x1a\n")
    finally:
        if session is not None:
            await runtime.close_session(session=session, reason="contract_test_complete")
        await client.aclose()


def _live_contract_configuration_or_skip() -> tuple[WorkflowSettings, AppInstallRequest]:
    required = {
        "ALIOTH_OFFICIAL_APP_CONTRACT_TEST_ENABLED": os.getenv(
            "ALIOTH_OFFICIAL_APP_CONTRACT_TEST_ENABLED"
        ),
        "ALIOTH_OFFICIAL_APP_API_BASE_URL": os.getenv("ALIOTH_OFFICIAL_APP_API_BASE_URL"),
        "ALIOTH_OFFICIAL_APP_AUTH_TOKEN": os.getenv("ALIOTH_OFFICIAL_APP_AUTH_TOKEN"),
        "ALIOTH_OFFICIAL_APP_CONTRACT_PLATFORM_BUILD_REF": os.getenv(
            "ALIOTH_OFFICIAL_APP_CONTRACT_PLATFORM_BUILD_REF"
        ),
        "ALIOTH_OFFICIAL_APP_CONTRACT_PACKAGE_NAME": os.getenv(
            "ALIOTH_OFFICIAL_APP_CONTRACT_PACKAGE_NAME"
        ),
        "ALIOTH_OFFICIAL_APP_CONTRACT_APK_SHA256": os.getenv(
            "ALIOTH_OFFICIAL_APP_CONTRACT_APK_SHA256"
        ),
    }
    enabled = required["ALIOTH_OFFICIAL_APP_CONTRACT_TEST_ENABLED"]
    missing = [
        name
        for name, value in required.items()
        if name != "ALIOTH_OFFICIAL_APP_CONTRACT_TEST_ENABLED" and not value
    ]
    if enabled != "true" or missing:
        pytest.skip(
            "真实星语合同测试需要 ALIOTH_OFFICIAL_APP_CONTRACT_TEST_ENABLED=true 且显式配置："
            + ", ".join(missing or ["ALIOTH_OFFICIAL_APP_CONTRACT_TEST_ENABLED"])
        )

    task_id = f"star-contract-{uuid4()}"
    session_id = f"star-contract-{uuid4()}"
    settings = WorkflowSettings(
        official_app_base_url=required["ALIOTH_OFFICIAL_APP_API_BASE_URL"],
        official_app_timeout_seconds=float(
            os.getenv("ALIOTH_OFFICIAL_APP_TIMEOUT_SECONDS", "60")
        ),
        official_app_max_screenshot_bytes=int(
            os.getenv("ALIOTH_OFFICIAL_APP_MAX_SCREENSHOT_BYTES", str(20 * 1024 * 1024))
        ),
        official_app_auth_header_name=os.getenv(
            "ALIOTH_OFFICIAL_APP_AUTH_HEADER_NAME", "Authorization"
        ),
        official_app_auth_token=required["ALIOTH_OFFICIAL_APP_AUTH_TOKEN"],
        official_app_start_session_path=os.getenv(
            "ALIOTH_OFFICIAL_APP_START_SESSION_PATH", "/v1/sessions"
        ),
        official_app_install_path=os.getenv(
            "ALIOTH_OFFICIAL_APP_INSTALL_PATH", "/v1/apps/install"
        ),
        official_app_open_path=os.getenv("ALIOTH_OFFICIAL_APP_OPEN_PATH", "/v1/apps/open"),
        official_app_capture_path=os.getenv(
            "ALIOTH_OFFICIAL_APP_CAPTURE_PATH", "/v1/apps/capture"
        ),
        official_app_execute_path=os.getenv(
            "ALIOTH_OFFICIAL_APP_EXECUTE_PATH", "/v1/apps/execute"
        ),
        official_app_close_path=os.getenv(
            "ALIOTH_OFFICIAL_APP_CLOSE_PATH", "/v1/sessions/close"
        ),
    )
    return settings, AppInstallRequest(
        task_id=task_id,
        session_id=session_id,
        platform_build_ref=required["ALIOTH_OFFICIAL_APP_CONTRACT_PLATFORM_BUILD_REF"],
        sha256=required["ALIOTH_OFFICIAL_APP_CONTRACT_APK_SHA256"],
        package_name=required["ALIOTH_OFFICIAL_APP_CONTRACT_PACKAGE_NAME"],
    )
