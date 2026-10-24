"""Fake App Runtime 的安装、打开、截图、动作与故障注入测试。"""

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta

import pytest

from services.api.app.application.capture_profiles import CaptureProfilePolicy
from services.api.app.domain.interaction import (
    ActionIntent,
    AppInstallRequest,
    AppOpenRequest,
    InteractionOperation,
    SurfaceKind,
    SurfaceSessionSpec,
)
from services.api.app.domain.task import TaskTargetPlatform
from services.api.app.infrastructure.app_runtime.fake import (
    FakeAppRuntime,
    FakeAppRuntimeError,
    FakeAppScenario,
)


def _session_spec() -> SurfaceSessionSpec:
    return SurfaceSessionSpec(
        task_id="task-1",
        session_id="session-1",
        surface_kind=SurfaceKind.ANDROID,
        platform_build_ref="app-build:build-1",
    )


def _install_request(*, sha256: str = "a" * 64) -> AppInstallRequest:
    return AppInstallRequest(
        task_id="task-1",
        session_id="session-1",
        platform_build_ref="app-build:build-1",
        sha256=sha256,
        package_name="com.example.target",
    )


def _open_request() -> AppOpenRequest:
    return AppOpenRequest(
        task_id="task-1",
        session_id="session-1",
        package_name="com.example.target",
    )


def _intent(action_type: InteractionOperation) -> ActionIntent:
    return ActionIntent(
        task_id="task-1",
        session_id="session-1",
        intent_id=f"intent-{action_type.value}",
        idempotency_key=f"session-1:{action_type.value}",
        surface_kind=SurfaceKind.ANDROID,
        source_capture_id="capture-1",
        action_type=action_type,
        semantic_target=(
            "主要操作按钮"
            if action_type in {InteractionOperation.CLICK, InteractionOperation.INPUT}
            else None
        ),
        purpose="验证 Fake App 动作",
        capture_profile=CaptureProfilePolicy.for_platform(TaskTargetPlatform.APP),
        deadline=datetime.now(UTC) + timedelta(minutes=1),
    )


async def _ready_runtime(
    scenario: FakeAppScenario | None = None,
) -> tuple[FakeAppRuntime, object]:
    runtime = FakeAppRuntime(scenario=scenario)
    session = await runtime.start_session(_session_spec())
    installed = await runtime.ensure_installed(_install_request())
    assert installed.succeeded is True
    opened = await runtime.open_app(_open_request())
    assert opened.succeeded is True
    return runtime, session


async def test_fake_runtime_reuses_same_checksum_install_and_captures_png() -> None:
    runtime = FakeAppRuntime()
    session = await runtime.start_session(_session_spec())

    first = await runtime.ensure_installed(_install_request())
    repeated = await runtime.ensure_installed(_install_request())
    opened = await runtime.open_app(_open_request())
    artifacts = await runtime.capture(
        session=session,
        profile=CaptureProfilePolicy.for_platform(TaskTargetPlatform.APP),
    )

    assert first.succeeded is True
    assert first.reused_existing is False
    assert repeated.request_id == first.request_id
    assert repeated.reused_existing is True
    assert opened.succeeded is True
    assert opened.foreground_verified is True
    assert {item.artifact_type for item in artifacts.artifacts} == {
        "screenshot",
        "action_trace",
        "ui_tree",
        "view_hierarchy",
        "ui_snapshot",
        "visible_text",
        "media_state",
        "device_log",
        "network_trace",
        "har",
        "fault_artifact",
    }
    snapshot = next(
        item.content for item in artifacts.artifacts if item.artifact_type == "ui_snapshot"
    )
    assert isinstance(snapshot, Mapping)
    assert str(snapshot.get("screen_id") or "").startswith("screen:")
    media = next(
        item.content for item in artifacts.artifacts if item.artifact_type == "media_state"
    )
    assert isinstance(media, Mapping)
    assert "player_wake_before_capture" not in media
    assert "player_wake_before_capture" not in artifacts.metadata
    screenshot = next(
        item.content for item in artifacts.artifacts if item.artifact_type == "screenshot"
    )
    assert isinstance(screenshot, bytes)
    assert screenshot.startswith(b"\x89PNG\r\n\x1a\n")
    assert artifacts.metadata["package_name"] == "com.example.target"
    assert artifacts.metadata["width"] > 0
    assert artifacts.metadata["height"] > 0


async def test_fake_runtime_timed_capture_emits_temporal_trace() -> None:
    from services.api.app.domain.interaction import InteractionOperation, SurfaceKind

    runtime, session = await _ready_runtime()
    profile = CaptureProfilePolicy.for_surface(
        SurfaceKind.ANDROID,
        InteractionOperation.TIMED_CAPTURE,
    )
    artifacts = await runtime.capture(session=session, profile=profile)
    types = {item.artifact_type for item in artifacts.artifacts}
    assert "temporal_trace" in types
    trace = next(item.content for item in artifacts.artifacts if item.artifact_type == "temporal_trace")
    assert isinstance(trace, Mapping)
    assert trace["clock"] == "monotonic"
    samples = list(trace["samples"])
    assert len(samples) == 3
    assert float(samples[0]["offset_ms"]) < float(samples[1]["offset_ms"])


async def test_fake_runtime_returns_screenshot_sequence_and_reuses_last_frame() -> None:
    first_frame = b"\x89PNG\r\n\x1a\nfirst"
    second_frame = b"\x89PNG\r\n\x1a\nsecond"
    runtime, session = await _ready_runtime(
        FakeAppScenario(screenshot_sequence=(first_frame, second_frame))
    )
    profile = CaptureProfilePolicy.for_platform(TaskTargetPlatform.APP)

    first = await runtime.capture(session=session, profile=profile)
    second = await runtime.capture(session=session, profile=profile)
    exhausted = await runtime.capture(session=session, profile=profile)

    screenshots = [
        next(item.content for item in captured.artifacts if item.artifact_type == "screenshot")
        for captured in (first, second, exhausted)
    ]
    assert screenshots == [first_frame, second_frame, second_frame]
    assert first.metadata["image_signature"] != second.metadata["image_signature"]
    assert exhausted.metadata["image_signature"] == second.metadata["image_signature"]


async def test_fake_runtime_requires_install_before_open() -> None:
    runtime = FakeAppRuntime()
    await runtime.start_session(_session_spec())

    result = await runtime.open_app(_open_request())

    assert result.succeeded is False
    assert result.retryable is False
    assert result.error_code == "PACKAGE_MISSING"


async def test_fake_runtime_requires_open_before_capture() -> None:
    runtime = FakeAppRuntime()
    session = await runtime.start_session(_session_spec())
    await runtime.ensure_installed(_install_request())

    with pytest.raises(FakeAppRuntimeError, match="尚未打开") as caught:
        await runtime.capture(
            session=session,
            profile=CaptureProfilePolicy.for_platform(TaskTargetPlatform.APP),
        )

    assert caught.value.error_code == "OPEN_REQUIRED"
    assert caught.value.retryable is False


async def test_fake_runtime_supports_input_action() -> None:
    runtime, session = await _ready_runtime()

    result = await runtime.execute(
        session=session,
        intent=_intent(InteractionOperation.INPUT),
        action_execution_id="action-input",
    )

    assert result.succeeded is True
    assert result.error_code is None


@pytest.mark.parametrize(
    ("scenario", "stage", "error_code"),
    [
        (FakeAppScenario(start_error_code="DEVICE_BUSY"), "start", "DEVICE_BUSY"),
        (FakeAppScenario(install_error_code="INSTALL_FAILED"), "install", "INSTALL_FAILED"),
        (FakeAppScenario(open_error_code="OPEN_FAILED"), "open", "OPEN_FAILED"),
        (FakeAppScenario(capture_error_code="CAPTURE_FAILED"), "capture", "CAPTURE_FAILED"),
    ],
)
async def test_fake_runtime_injects_bootstrap_failures(
    scenario: FakeAppScenario,
    stage: str,
    error_code: str,
) -> None:
    runtime = FakeAppRuntime(scenario=scenario)

    if stage == "start":
        with pytest.raises(FakeAppRuntimeError) as caught:
            await runtime.start_session(_session_spec())
        assert caught.value.error_code == error_code
        assert caught.value.retryable is True
        return

    session = await runtime.start_session(_session_spec())
    installed = await runtime.ensure_installed(_install_request())
    if stage == "install":
        assert installed.error_code == error_code
        return
    assert installed.succeeded is True

    opened = await runtime.open_app(_open_request())
    if stage == "open":
        assert opened.error_code == error_code
        return
    assert opened.succeeded is True

    with pytest.raises(FakeAppRuntimeError) as caught:
        await runtime.capture(
            session=session,
            profile=CaptureProfilePolicy.for_platform(TaskTargetPlatform.APP),
        )
    assert caught.value.error_code == error_code


async def test_fake_runtime_marks_scripted_session_crash_on_action() -> None:
    runtime, session = await _ready_runtime(FakeAppScenario(crash_after_action_count=0))

    result = await runtime.execute(
        session=session,
        intent=_intent(InteractionOperation.CLICK),
        action_execution_id="action-crashed",
    )

    assert result.succeeded is False
    assert result.retryable is True
    assert result.error_code == "SESSION_CRASHED"


def test_fake_runtime_capability_manifest_is_android_with_ui_tree() -> None:
    manifest = FakeAppRuntime().describe_capabilities()

    assert manifest.surface_kind is SurfaceKind.ANDROID
    assert set(manifest.action_types) >= {"session", "install", "open", "click", "scroll", "input"}
    assert set(manifest.artifact_types) >= {
        "screenshot",
        "action_trace",
        "ui_tree",
        "visible_text",
    }
    assert "dom" not in manifest.artifact_types
    assert "ocr_text" not in manifest.artifact_types
