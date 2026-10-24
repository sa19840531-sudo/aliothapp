"""App 仍缺／偏弱项闭环：clear_data、state_type 落库、CA 审计、容器轮转。"""

from __future__ import annotations

import pytest

from services.api.app.application.app_network_mitm_certs import (
    AppNetworkMitmAuthority,
    android_system_ca_filename,
    cryptography_available,
)
from services.api.app.application.app_network_proxy import (
    get_app_network_proxy_hub,
    install_device_mitm_ca,
    reset_app_network_proxy_hub_for_tests,
)
from services.api.app.application.app_surface_evidence import (
    project_visible_text_from_hierarchy,
)
from services.api.app.application.capture_profiles import CaptureProfilePolicy
from services.api.app.domain.interaction import (
    CaptureKind,
    CaptureRef,
    InteractionOperation,
    SurfaceKind,
    SurfaceSessionSpec,
    build_android_screenshot_only_manifest,
    default_capture_state_type,
    rotate_app_data_container_ref,
    with_rotated_app_data_container,
)
from services.api.app.infrastructure.device.adb_ops import AdbDeviceOps
from services.api.app.infrastructure.device.android_surface_driver import (
    AndroidAdbSurfaceDriver,
)


def test_capture_ref_includes_state_type_from_role() -> None:
    ref = CaptureRef(
        task_id="task-1",
        session_id="session-1",
        capture_id="capture-1",
        surface_kind=SurfaceKind.ANDROID,
        capture_kind=CaptureKind.APP,
        role="after_action",
    )
    assert ref.state_type == "after_action"
    payload = ref.to_dict()
    assert payload["state_type"] == "after_action"
    restored = CaptureRef.from_dict(payload)
    assert restored.state_type == "after_action"


def test_default_capture_state_type_maps_bootstrap_to_baseline() -> None:
    assert default_capture_state_type("bootstrap") == "baseline"
    assert default_capture_state_type("before_action") == "before_action"


def test_android_session_spec_fills_reset_policy_and_container() -> None:
    spec = SurfaceSessionSpec(
        task_id="task-1",
        session_id="session-1",
        surface_kind=SurfaceKind.ANDROID,
        device_ref="cloud_phone:qa",
        platform_build_ref="build:1",
    )
    assert spec.reset_policy == "force_stop"
    assert spec.options["app_data_container_ref"] == "cloud_phone:qa:app-data"
    assert spec.options["process_identity"] == "cloud_phone:qa:process"
    assert spec.options["lifecycle_phase"] == "foreground"


def test_clear_data_rotates_app_data_container_ref() -> None:
    first = rotate_app_data_container_ref(
        "cloud_phone:qa:app-data",
        device_ref="cloud_phone:qa",
    )
    assert first == "cloud_phone:qa:app-data:g1"
    second = rotate_app_data_container_ref(first, device_ref="cloud_phone:qa")
    assert second == "cloud_phone:qa:app-data:g2"
    spec = SurfaceSessionSpec(
        task_id="task-1",
        session_id="session-1",
        surface_kind=SurfaceKind.ANDROID,
        device_ref="cloud_phone:qa",
        platform_build_ref="build:1",
        reset_policy="clear_data",
    )
    rotated = with_rotated_app_data_container(spec)
    assert rotated.options["app_data_container_ref"] == "cloud_phone:qa:app-data:g1"
    assert rotated.options["data_cleared"] is True


def test_apply_reset_policy_clear_data_runs_pm_clear() -> None:
    calls: list[list[str]] = []

    def shell_fn(args, timeout):
        del timeout
        calls.append(list(args))

    ops = AdbDeviceOps(
        backend="google_emulator",
        width=1080,
        height=1920,
        _log=lambda _msg: None,
        _screencap_fn=lambda _path: None,
        _shell_fn=shell_fn,
        _shell_output_fn=lambda args, timeout: "",
    )
    applied = ops.apply_reset_policy(
        "com.example.app",
        reset_policy="clear_data",
        activity=".MainActivity",
    )
    assert applied["mode"] == "clear_data"
    assert applied["container_rotated"] is True
    assert any(item[:2] == ["pm", "clear"] for item in calls)


def test_manifest_marks_input_and_visible_text_ready() -> None:
    manifest = build_android_screenshot_only_manifest(
        adapter_id="test",
        adapter_version="1",
        backend="fake",
    )
    assert manifest.actions["input"].status.value == "ready"
    assert manifest.observations["visible_text"].status.value == "ready"
    assert manifest.artifacts["visible_text"].status.value == "ready"
    assert manifest.observations["ocr_text"].status.value == "unsupported"


def test_adb_describe_capabilities_aligns_with_official_manifest() -> None:
    driver = AndroidAdbSurfaceDriver(backend="ldplayer", ld_index=0)
    manifest = driver.describe_capabilities()
    assert manifest.actions["warm_restore"].status.value == "ready"
    assert manifest.actions["deep_link"].status.value == "ready"
    assert manifest.artifacts["device_log"].status.value == "ready"
    assert manifest.artifacts["ui_snapshot"].status.value == "ready"
    assert "input" in manifest.actions


def test_capture_profile_includes_visible_text() -> None:
    profile = CaptureProfilePolicy.for_surface(SurfaceKind.ANDROID)
    assert "visible_text" in profile.after.artifact_types


def test_project_visible_text_from_hierarchy() -> None:
    xml = (
        '<?xml version="1.0"?><hierarchy>'
        '<node text="登录" content-desc="login" class="android.widget.Button"/>'
        '<node text="登录" content-desc="" class="android.widget.TextView"/>'
        "</hierarchy>"
    )
    payload = project_visible_text_from_hierarchy(xml)
    assert payload["source"] == "accessibility_hierarchy"
    assert "登录" in payload["text"]
    assert "login" in payload["text"]


def test_install_device_mitm_ca_stages_and_prompts_when_not_debuggable() -> None:
    if not cryptography_available():
        pytest.skip("cryptography 不可用")
    calls: list[list[str]] = []

    class _Ops:
        def shell_output(self, args, *, timeout=30.0):
            del timeout
            if args[:2] == ["getprop", "ro.debuggable"]:
                return "0"
            if args[:1] == ["ls"]:
                return ""
            return ""

        def _shell_fn(self, args, timeout):
            del timeout
            calls.append(list(args))

    authority = AppNetworkMitmAuthority(storage_dir=None)
    reset_app_network_proxy_hub_for_tests()
    result = install_device_mitm_ca(_Ops(), ca_cert_pem=authority.ca_cert_pem)
    assert result["status"] == "staged_user_prompted"
    assert result["staged_path"] == "/sdcard/Download/alioth-mitm-ca.crt"
    assert result["installer_launched"] is True
    assert any("base64" in " ".join(item) for item in calls)
    assert any(item[:2] == ["am", "start"] for item in calls)
    name = android_system_ca_filename(authority.ca_cert_pem)
    assert name.endswith(".0")
    hub = get_app_network_proxy_hub()
    hub.record_ca_install_result("session-ca", result)
    status = hub.ca_install_status("session-ca")
    assert status is not None
    assert status["status"] == "staged_user_prompted"


@pytest.mark.asyncio
async def test_fake_clear_data_rotates_container_on_reset_app() -> None:
    from services.api.app.domain.interaction import (
        AppInstallRequest,
        AppOpenRequest,
    )
    from services.api.app.infrastructure.app_runtime.fake import FakeAppRuntime

    runtime = FakeAppRuntime()
    spec = SurfaceSessionSpec(
        task_id="task-1",
        session_id="session-1",
        surface_kind=SurfaceKind.ANDROID,
        platform_build_ref="build:1",
        device_ref="fake:default",
        reset_policy="clear_data",
    )
    await runtime.start_session(spec)
    await runtime.ensure_installed(
        AppInstallRequest(
            task_id="task-1",
            session_id="session-1",
            platform_build_ref="build:1",
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
    before = runtime._sessions["session-1"].spec.options["app_data_container_ref"]  # noqa: SLF001
    result = await runtime.reset_app(
        AppOpenRequest(
            task_id="task-1",
            session_id="session-1",
            package_name="com.example.target",
            cold_start=True,
        )
    )
    assert result.succeeded is True
    after = runtime._sessions["session-1"].spec.options["app_data_container_ref"]  # noqa: SLF001
    assert after != before
    assert after.endswith(":g1")


@pytest.mark.asyncio
async def test_fake_supports_warm_restore_and_input() -> None:
    from services.api.app.domain.interaction import (
        AppInstallRequest,
        AppOpenRequest,
    )
    from services.api.app.infrastructure.app_runtime.fake import FakeAppRuntime

    runtime = FakeAppRuntime()
    spec = SurfaceSessionSpec(
        task_id="task-1",
        session_id="session-1",
        surface_kind=SurfaceKind.ANDROID,
        platform_build_ref="build:1",
        device_ref="fake:default",
    )
    await runtime.start_session(spec)
    await runtime.ensure_installed(
        AppInstallRequest(
            task_id="task-1",
            session_id="session-1",
            platform_build_ref="build:1",
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
    warm = await runtime.warm_restore(
        AppOpenRequest(
            task_id="task-1",
            session_id="session-1",
            package_name="com.example.target",
            cold_start=False,
        )
    )
    assert warm.succeeded is True
    manifest = runtime.describe_capabilities()
    assert manifest.actions["input"].status.value == "ready"
    assert InteractionOperation.WARM_RESTORE.value == "warm_restore"
