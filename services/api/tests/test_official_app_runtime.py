"""Official App Runtime 的 PNG-only 行为契约。"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta

import pytest

from services.api.app.application.capture_profiles import CaptureProfilePolicy
from services.api.app.domain.interaction import (
    ActionIntent,
    AppInstallRequest,
    AppOpenRequest,
    CaptureProfile,
    InteractionOperation,
    InteractionRiskLevel,
    SurfaceKind,
    SurfaceSessionSpec,
)
from services.api.app.domain.task import TaskTargetPlatform
from services.api.app.infrastructure.app_runtime.official import (
    OfficialAppRuntime,
    OfficialAppRuntimeError,
)
from services.api.app.infrastructure.app_runtime.official_api import (
    ACTION_OUTCOME_UNKNOWN_ERROR_CODE,
    OfficialActionResponse,
    OfficialAppApiError,
    OfficialAuxCaptureResponse,
    OfficialCaptureResponse,
    OfficialInstallResponse,
    OfficialOpenResponse,
    OfficialSessionResponse,
    OfficialUiTreeResponse,
)
from services.api.app.infrastructure.storage.local_evidence_store import serialize_artifact

PNG = b"\x89PNG\r\n\x1a\nvalid-png"


class FakeOfficialAppApiClient:
    """只模拟星语边界，保留 Runtime 的真实映射和校验。"""

    def __init__(
        self,
        *,
        capture: OfficialCaptureResponse,
        include_auxiliary: bool = False,
    ) -> None:
        self.capture_response = capture
        self.include_auxiliary = include_auxiliary
        self.installs = []
        self.opens = []
        self.executions = []

    async def start_session(self, spec: SurfaceSessionSpec) -> OfficialSessionResponse:
        return OfficialSessionResponse(request_id="session-request-1", session_handle="private-1")

    async def install(self, request: AppInstallRequest) -> OfficialInstallResponse:
        self.installs.append(request)
        return OfficialInstallResponse(
            request_id="install-request-1",
            succeeded=True,
            retryable=False,
            installed_package="com.example.target",
        )

    async def open(self, request: AppOpenRequest) -> OfficialOpenResponse:
        self.opens.append(request)
        return OfficialOpenResponse(
            request_id="open-request-1",
            succeeded=True,
            retryable=False,
            package_name=request.package_name,
            foreground_verified=True,
            activity=".MainActivity",
        )

    async def capture_png(self, session, profile: CaptureProfile) -> OfficialCaptureResponse:
        return self.capture_response

    async def capture_ui_tree(self, session, profile: CaptureProfile) -> OfficialUiTreeResponse:
        if not self.include_auxiliary:
            raise RuntimeError("test client does not provide UI Tree")
        return OfficialUiTreeResponse(
            content_type="application/xml",
            xml_text=(
                '<hierarchy><node package="com.example.target" '
                'class="android.widget.Button" text="home" clickable="true" '
                'bounds="[0,0][2,3]" /></hierarchy>'
            ),
        )

    async def capture_aux(
        self,
        session,
        *,
        artifact_type: str,
        package_name: str | None = None,
    ) -> OfficialAuxCaptureResponse:
        if not self.include_auxiliary:
            raise RuntimeError("test client does not provide auxiliary artifacts")
        if artifact_type == "clipboard":
            return OfficialAuxCaptureResponse(
                artifact_type=artifact_type,
                content_type="application/json",
                payload={
                    "status": "empty",
                    "text": "",
                    "dumpsys_excerpt": "",
                    "package_name": package_name,
                },
            )
        return OfficialAuxCaptureResponse(
            artifact_type=artifact_type,
            content_type="application/json",
            payload={
                "status": "captured",
                "package_name": package_name,
            },
        )

    async def execute(
        self,
        session,
        intent: ActionIntent,
        action_execution_id: str,
    ) -> OfficialActionResponse:
        self.executions.append((session, intent, action_execution_id))
        return OfficialActionResponse(
            request_id="action-request-1",
            action_execution_id=action_execution_id,
            succeeded=True,
            retryable=False,
            action_trace=(
                {"action": intent.action_type.value, "semantic_target": intent.semantic_target},
            ),
        )

    async def close(self, session, reason: str) -> None:
        return None


def _capture(**overrides: object) -> OfficialCaptureResponse:
    values: dict[str, object] = {
        "content_type": "image/png",
        "png_bytes": PNG,
        "width": 2,
        "height": 3,
        "orientation": "portrait",
        "package_name": "com.example.target",
        "activity": ".MainActivity",
        "declared_sha256": hashlib.sha256(PNG).hexdigest(),
    }
    values.update(overrides)
    return OfficialCaptureResponse(**values)


async def _opened_runtime(
    *,
    capture: OfficialCaptureResponse,
    include_auxiliary: bool = False,
    client: FakeOfficialAppApiClient | None = None,
) -> tuple[OfficialAppRuntime, object]:
    runtime = OfficialAppRuntime(
        api_client=client
        or FakeOfficialAppApiClient(
            capture=capture,
            include_auxiliary=include_auxiliary,
        ),
        max_screenshot_bytes=1024,
    )
    spec = SurfaceSessionSpec(
        task_id="task-1",
        session_id="session-1",
        surface_kind=SurfaceKind.ANDROID,
        platform_build_ref="build-1",
    )
    session = await runtime.start_session(spec)
    await runtime.ensure_installed(
        AppInstallRequest(
            task_id="task-1",
            session_id="session-1",
            platform_build_ref="build-1",
            sha256="a" * 64,
            package_name="com.example.target",
        )
    )
    await runtime.open_app(
        AppOpenRequest(
            task_id="task-1",
            session_id="session-1",
            package_name="com.example.target",
        )
    )
    return runtime, session


async def test_capture_maps_valid_png_to_screenshot_and_action_trace_artifacts() -> None:
    runtime, session = await _opened_runtime(capture=_capture())

    artifacts = await runtime.capture(
        session=session,
        profile=CaptureProfilePolicy.for_surface(SurfaceKind.ANDROID),
    )

    assert [(item.artifact_type, item.content) for item in artifacts.artifacts] == [
        ("screenshot", PNG),
        ("action_trace", ()),
    ]
    assert artifacts.surface_ref == "app-surface:session-1"
    assert {
        "activity": ".MainActivity",
        "height": 3,
        "image_signature": f"sha256:{hashlib.sha256(PNG).hexdigest()}",
        "orientation": "portrait",
        "package_name": "com.example.target",
        "width": 2,
    }.items() <= artifacts.metadata.items()


async def test_full_capture_keeps_auxiliary_facts_on_their_evidence_entries() -> None:
    """截图顶层 metadata 只保留截图属性，辅助事实必须随对应制品落库。"""

    client = FakeOfficialAppApiClient(capture=_capture(), include_auxiliary=True)
    runtime, session = await _opened_runtime(
        capture=_capture(),
        include_auxiliary=True,
        client=client,
    )

    artifacts = await runtime.capture(
        session=session,
        profile=CaptureProfilePolicy.for_platform(TaskTargetPlatform.APP),
    )

    assert set(artifacts.metadata) <= {
        "package_name",
        "activity",
        "width",
        "height",
        "orientation",
        "image_signature",
        "player_wake_before_capture",
        "outbound_landing",
        "timed_capture",
    }
    assert {
        "package_name",
        "activity",
        "width",
        "height",
        "orientation",
        "image_signature",
    } <= set(artifacts.metadata)
    by_type = {item.artifact_type: item.content for item in artifacts.artifacts}
    snapshot = by_type["ui_snapshot"]
    assert snapshot["screen_id"].startswith("screen:")
    assert snapshot["structure_signature"].startswith("sha256:")
    visible_text = by_type["visible_text"]
    assert visible_text["source"] == "accessibility_hierarchy"
    assert by_type["media_state"]["playback_state_available"] is False
    assert "player_wake_before_capture" not in by_type["media_state"]
    assert by_type["device_log"]["status"] == "captured"
    assert client.executions == []  # 采集只读，触屏必须由正式 Coordinator 动作负责。

    data, extension, content_type, metadata = serialize_artifact("visible_text", visible_text)
    assert data == b"home"
    assert extension == "txt"
    assert content_type.startswith("text/plain")
    assert metadata["source"] == "accessibility_hierarchy"


@pytest.mark.parametrize(
    "overrides",
    [
        {"content_type": "image/jpeg"},
        {"png_bytes": b"not-a-png"},
        {"png_bytes": b""},
        {"width": 0},
        {"height": 0},
        {"png_bytes": PNG + b"x" * 1024},
        {"declared_sha256": "0" * 64},
    ],
)
async def test_capture_rejects_invalid_png_boundary_data(overrides: dict[str, object]) -> None:
    runtime, session = await _opened_runtime(capture=_capture(**overrides))

    with pytest.raises(OfficialAppRuntimeError) as error:
        await runtime.capture(
            session=session,
            profile=CaptureProfilePolicy.for_surface(SurfaceKind.ANDROID),
        )

    assert error.value.error_code == "CAPTURE_INVALID"
    assert error.value.retryable is False


async def test_install_open_and_execute_preserve_public_result_contract() -> None:
    client = FakeOfficialAppApiClient(capture=_capture())
    runtime = OfficialAppRuntime(api_client=client, max_screenshot_bytes=1024)
    spec = SurfaceSessionSpec(
        task_id="task-1",
        session_id="session-1",
        surface_kind=SurfaceKind.ANDROID,
        platform_build_ref="build-1",
    )
    session = await runtime.start_session(spec)
    installed = await runtime.ensure_installed(
        AppInstallRequest(
            task_id="task-1",
            session_id="session-1",
            platform_build_ref="build-1",
            sha256="a" * 64,
            package_name="com.example.target",
        )
    )
    opened = await runtime.open_app(
        AppOpenRequest(task_id="task-1", session_id="session-1", package_name="com.example.target")
    )
    intent = ActionIntent(
        task_id="task-1",
        session_id="session-1",
        intent_id="intent-1",
        idempotency_key="intent-1",
        surface_kind=SurfaceKind.ANDROID,
        source_capture_id="capture-1",
        action_type=InteractionOperation.CLICK,
        semantic_target="继续",
        purpose="继续探索",
        risk_level=InteractionRiskLevel.LOW,
        capture_profile=CaptureProfilePolicy.for_surface(SurfaceKind.ANDROID),
        deadline=datetime.now(UTC) + timedelta(minutes=1),
    )

    action = await runtime.execute(session=session, intent=intent, action_execution_id="exec-1")

    assert installed.request_id == "install-request-1"
    assert installed.retryable is False
    assert opened.request_id == "open-request-1"
    assert opened.retryable is False
    assert list(action.payload["action_trace"]) == [{"action": "click", "semantic_target": "继续"}]
    assert client.executions[0][0] == session

    artifacts = await runtime.capture(
        session=session,
        profile=CaptureProfilePolicy.for_surface(SurfaceKind.ANDROID),
    )
    trace = next(
        item.content for item in artifacts.artifacts if item.artifact_type == "action_trace"
    )
    assert trace == ({"action": "click", "semantic_target": "继续"},)


async def test_unknown_action_outcome_is_cached_and_not_replayed() -> None:
    class UnknownActionClient(FakeOfficialAppApiClient):
        async def execute(
            self,
            session,
            intent,
            action_execution_id,
            execution_context=None,
        ) -> OfficialActionResponse:
            self.executions.append((session, intent, action_execution_id, execution_context))
            raise OfficialAppApiError(
                "动作结果未知",
                error_code=ACTION_OUTCOME_UNKNOWN_ERROR_CODE,
                retryable=False,
            )

    client = UnknownActionClient(capture=_capture())
    runtime, session = await _opened_runtime(capture=_capture(), client=client)
    intent = ActionIntent(
        task_id="task-1",
        session_id="session-1",
        intent_id="intent-unknown",
        idempotency_key="intent-unknown",
        surface_kind=SurfaceKind.ANDROID,
        source_capture_id="capture-unknown",
        action_type=InteractionOperation.BACK,
        semantic_target=None,
        purpose="恢复未知动作结果",
        risk_level=InteractionRiskLevel.LOW,
        capture_profile=CaptureProfilePolicy.for_surface(SurfaceKind.ANDROID),
        deadline=datetime.now(UTC) + timedelta(minutes=1),
    )
    context = {
        "task_id": "task-1",
        "session_id": "session-1",
        "surface_kind": "android",
        "action_execution_id": "exec-unknown",
        "source_capture_id": "capture-unknown",
        "finalization_epoch": 0,
        "command_sequence": 1,
        "lease_owner": "test-owner",
        "lease_token": "test-token",
    }

    first = await runtime.execute_with_context(
        session=session,
        intent=intent,
        action_execution_id="exec-unknown",
        execution_context=context,
    )
    second = await runtime.execute_with_context(
        session=session,
        intent=intent,
        action_execution_id="exec-unknown",
        execution_context=context,
    )

    assert first == second
    assert first.succeeded is False
    assert first.retryable is False
    assert first.error_code == ACTION_OUTCOME_UNKNOWN_ERROR_CODE
    assert len(client.executions) == 1


async def test_action_trace_drops_unapproved_runtime_secrets() -> None:
    class SecretTraceClient(FakeOfficialAppApiClient):
        async def execute(self, session, intent, action_execution_id):
            return OfficialActionResponse(
                request_id="action-request-secret",
                action_execution_id=action_execution_id,
                succeeded=True,
                retryable=False,
                action_trace=(
                    {
                        "action": "click",
                        "semantic_target": "继续",
                        "token": "do-not-persist",
                        "session_handle": "private-handle",
                        "apk": "binary-ref",
                        "png": "binary-ref",
                        "driver": "private-driver",
                    },
                ),
            )

    client = SecretTraceClient(capture=_capture())
    runtime = OfficialAppRuntime(api_client=client, max_screenshot_bytes=1024)
    session = await runtime.start_session(
        SurfaceSessionSpec(
            task_id="task-1",
            session_id="session-1",
            surface_kind=SurfaceKind.ANDROID,
            platform_build_ref="build-1",
        )
    )
    await runtime.open_app(
        AppOpenRequest(
            task_id="task-1",
            session_id="session-1",
            package_name="com.example.target",
        )
    )
    intent = ActionIntent(
        task_id="task-1",
        session_id="session-1",
        intent_id="intent-1",
        idempotency_key="intent-1",
        surface_kind=SurfaceKind.ANDROID,
        source_capture_id="capture-1",
        action_type=InteractionOperation.CLICK,
        semantic_target="继续",
        purpose="继续探索",
        risk_level=InteractionRiskLevel.LOW,
        capture_profile=CaptureProfilePolicy.for_surface(SurfaceKind.ANDROID),
        deadline=datetime.now(UTC) + timedelta(minutes=1),
    )

    await runtime.execute(session=session, intent=intent, action_execution_id="exec-1")
    artifacts = await runtime.capture(
        session=session,
        profile=CaptureProfilePolicy.for_surface(SurfaceKind.ANDROID),
    )

    trace = next(
        item.content for item in artifacts.artifacts if item.artifact_type == "action_trace"
    )
    assert trace == ({"action": "click", "semantic_target": "继续"},)


async def test_action_trace_preserves_android_resolution_and_command_audit() -> None:
    class AuditTraceClient(FakeOfficialAppApiClient):
        async def execute(self, session, intent, action_execution_id):
            return OfficialActionResponse(
                request_id="action-request-audit",
                action_execution_id=action_execution_id,
                succeeded=True,
                retryable=False,
                action_trace=(
                    {
                        "op": "tap",
                        "semantic_target": "下次开启",
                        "resolution_method": "uiautomator.content_desc",
                        "resolved_x": 342,
                        "resolved_y": 1185,
                        "bounds_left": 174,
                        "bounds_bottom": 1250,
                        "command": "input tap",
                        "command_result": "ok",
                        "result": "ok",
                        "session_handle": "private-handle",
                    },
                ),
            )

    client = AuditTraceClient(capture=_capture())
    runtime, session = await _opened_runtime(capture=_capture(), client=client)
    intent = ActionIntent(
        task_id="task-1",
        session_id="session-1",
        intent_id="intent-1",
        idempotency_key="intent-1",
        surface_kind=SurfaceKind.ANDROID,
        source_capture_id="capture-1",
        action_type=InteractionOperation.CLICK,
        semantic_target="下次开启",
        purpose="处理系统弹窗",
        risk_level=InteractionRiskLevel.LOW,
        capture_profile=CaptureProfilePolicy.for_surface(SurfaceKind.ANDROID),
        deadline=datetime.now(UTC) + timedelta(minutes=1),
    )

    await runtime.execute(session=session, intent=intent, action_execution_id="exec-audit")
    artifacts = await runtime.capture(
        session=session,
        profile=CaptureProfilePolicy.for_surface(SurfaceKind.ANDROID),
    )

    trace = next(
        item.content for item in artifacts.artifacts if item.artifact_type == "action_trace"
    )
    assert trace == (
        {
            "op": "tap",
            "semantic_target": "下次开启",
            "resolution_method": "uiautomator.content_desc",
            "resolved_x": 342,
            "resolved_y": 1185,
            "bounds_left": 174,
            "bounds_bottom": 1250,
            "command": "input tap",
            "command_result": "ok",
            "result": "ok",
        },
    )
