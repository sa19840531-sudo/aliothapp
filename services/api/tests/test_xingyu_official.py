"""星語 Official App API：閘門、認證、冪等與乾跑契約。"""

from __future__ import annotations

import hashlib
import importlib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from services.api.app.application.xingyu_runtime.build_artifact_fetch import (
    BuildArtifactError,
    resolve_apk_path,
    verify_sha256,
)
from services.api.app.application.xingyu_runtime.device_bridge import (
    DryRunDeviceBridge,
    LiveCloudPhoneDeviceBridge,
    _LiveDevice,
)
from services.api.app.application.xingyu_runtime.service import (
    XingyuOfficialService,
    reset_xingyu_official_service,
)
from services.api.app.infrastructure.device.adb_ops import AdbDeviceOps
from services.api.app.infrastructure.device.android_target_resolver import (
    CanonicalPointTargetResolver,
)

TOKEN = "xingyu-test-token"


class _SemanticTargetOps(AdbDeviceOps):
    def __init__(self, xml: str) -> None:
        self.calls: list[tuple[str, ...]] = []
        self._xml = xml
        super().__init__(
            backend="fake",
            width=1080,
            height=1920,
            _log=lambda _message: None,
            _screencap_fn=lambda _dest: None,
            _shell_fn=self._shell,
            _shell_output_fn=self._shell_output,
        )

    def _shell(self, args: list[str], _timeout: float) -> None:
        self.calls.append(tuple(args))

    def _shell_output(self, args: list[str], _timeout: float) -> str:
        self.calls.append(tuple(args))
        return self._xml if args[:1] == ["cat"] else ""


_SEMANTIC_TARGET_XML = """
<hierarchy>
  <node class="android.widget.Button" content-desc="下次开启"
        clickable="true" enabled="true" visible-to-user="true"
        bounds="[174,1121][510,1250]" />
</hierarchy>
"""


def _enable_official(monkeypatch: pytest.MonkeyPatch, *, dry_run: bool = True) -> None:
    monkeypatch.setenv("ALIOTH_XINGYU_OFFICIAL_ENABLED", "1")
    monkeypatch.setenv("ALIOTH_XINGYU_OFFICIAL_TOKEN", TOKEN)
    monkeypatch.setenv("ALIOTH_API_BIND_HOST", "127.0.0.1")
    monkeypatch.delenv("ALIOTH_XINGYU_OFFICIAL_ALLOW_NON_LOOPBACK", raising=False)
    if dry_run:
        monkeypatch.setenv("ALIOTH_XINGYU_OFFICIAL_DRY_RUN", "1")
    else:
        monkeypatch.delenv("ALIOTH_XINGYU_OFFICIAL_DRY_RUN", raising=False)


def _client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    _enable_official(monkeypatch)
    service = XingyuOfficialService(bridge=DryRunDeviceBridge())
    reset_xingyu_official_service(service)
    import services.api.app.main as main_mod

    importlib.reload(main_mod)
    return TestClient(main_mod.create_app())


def test_xingyu_official_disabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ALIOTH_XINGYU_OFFICIAL_ENABLED", raising=False)
    monkeypatch.setenv("ALIOTH_API_BIND_HOST", "127.0.0.1")
    from services.api.app.api.v1.xingyu_official.gate import xingyu_official_router_enabled

    assert xingyu_official_router_enabled() is False


def test_xingyu_official_requires_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALIOTH_XINGYU_OFFICIAL_ENABLED", "1")
    monkeypatch.delenv("ALIOTH_XINGYU_OFFICIAL_TOKEN", raising=False)
    monkeypatch.setenv("ALIOTH_API_BIND_HOST", "127.0.0.1")
    from services.api.app.api.v1.xingyu_official.gate import xingyu_official_router_enabled

    assert xingyu_official_router_enabled() is False


def test_unauthorized_without_token(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(monkeypatch)
    response = client.post("/v1/sessions", json={"task_id": "t1", "session_id": "s1"})
    assert response.status_code == 401


def test_session_install_open_capture_execute_close(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    apk = tmp_path / "demo.apk"
    apk.write_bytes(b"fake-apk-bytes")
    digest = hashlib.sha256(apk.read_bytes()).hexdigest()
    monkeypatch.setenv("ALIOTH_XINGYU_BUILD_CACHE", str(tmp_path))

    client = _client(monkeypatch)
    headers = {"Authorization": TOKEN, "Idempotency-Key": "session:s-flow-1"}

    started = client.post(
        "/v1/sessions",
        headers=headers,
        json={
            "task_id": "task-1",
            "session_id": "s-flow-1",
            "surface_kind": "android",
            "platform_build_ref": "demo",
            "options": {"backend": "cloud_phone"},
        },
    )
    assert started.status_code == 200
    body = started.json()
    assert body["request_id"]
    handle = body["session_handle"]
    assert handle.startswith("xyh_")

    # 冪等重放
    replay = client.post(
        "/v1/sessions",
        headers=headers,
        json={
            "task_id": "task-1",
            "session_id": "s-flow-1",
            "surface_kind": "android",
            "platform_build_ref": "demo",
            "options": {"backend": "cloud_phone"},
        },
    )
    assert replay.status_code == 200
    assert replay.json()["session_handle"] == handle

    # 同 key 異 payload → 409
    conflict = client.post(
        "/v1/sessions",
        headers=headers,
        json={
            "task_id": "task-OTHER",
            "session_id": "s-flow-1",
            "surface_kind": "android",
            "platform_build_ref": "demo",
            "options": {"backend": "cloud_phone"},
        },
    )
    assert conflict.status_code == 409

    installed = client.post(
        "/v1/apps/install",
        headers={"Authorization": TOKEN, "Idempotency-Key": "install:s-flow-1:sha"},
        json={
            "task_id": "task-1",
            "session_id": "s-flow-1",
            "session_handle": handle,
            "platform_build_ref": "demo",
            "sha256": digest,
            "package_name": "com.example.flow",
            "protocol_version": "app-bootstrap.v1",
            "replace_policy": "same_checksum_only",
        },
    )
    assert installed.status_code == 200
    assert installed.json()["succeeded"] is True
    assert installed.json()["installed_package"] == "com.example.flow"

    opened = client.post(
        "/v1/apps/open",
        headers={"Authorization": TOKEN, "Idempotency-Key": "open:s-flow-1"},
        json={
            "task_id": "task-1",
            "session_id": "s-flow-1",
            "session_handle": handle,
            "package_name": "com.example.flow",
            "cold_start": True,
            "protocol_version": "app-bootstrap.v1",
        },
    )
    assert opened.status_code == 200
    assert opened.json()["succeeded"] is True
    assert opened.json()["foreground_verified"] is True

    captured = client.post(
        "/v1/apps/capture",
        headers={"Authorization": TOKEN, "Idempotency-Key": "capture:s-flow-1:p1"},
        json={
            "task_id": "task-1",
            "session_id": "s-flow-1",
            "session_handle": handle,
            "surface_ref": "android-primary:s-flow-1",
            "capture_profile": {"profile_id": "p1"},
        },
    )
    assert captured.status_code == 200
    assert captured.headers["content-type"].startswith("image/png")
    assert int(captured.headers["x-alioth-width"]) == 1
    assert int(captured.headers["x-alioth-height"]) == 1
    assert captured.headers["x-alioth-orientation"] == "portrait"
    assert captured.content.startswith(b"\x89PNG")

    executed = client.post(
        "/v1/apps/execute",
        headers={"Authorization": TOKEN, "Idempotency-Key": "action:ae-1"},
        json={
            "session_handle": handle,
            "surface_ref": "android-primary:s-flow-1",
            "action_execution_id": "ae-1",
            "intent": {
                "action_type": "tap",
                "parameters": {"point": {"x": 100, "y": 200, "space": "canonical_px"}},
            },
        },
    )
    assert executed.status_code == 200
    assert executed.json()["succeeded"] is True
    assert executed.json()["action_execution_id"] == "ae-1"

    unsupported = client.post(
        "/v1/apps/execute",
        headers={"Authorization": TOKEN, "Idempotency-Key": "action:ae-input"},
        json={
            "session_handle": handle,
            "surface_ref": "android-primary:s-flow-1",
            "action_execution_id": "ae-input",
            "intent": {"action_type": "input", "parameters": {"text": "hi"}},
        },
    )
    assert unsupported.status_code == 200
    assert unsupported.json()["succeeded"] is False
    assert unsupported.json()["error_code"] == "ACTION_UNSUPPORTED"

    clicked = client.post(
        "/v1/apps/execute",
        headers={"Authorization": TOKEN, "Idempotency-Key": "action:ae-click"},
        json={
            "session_handle": handle,
            "surface_ref": "android-primary:s-flow-1",
            "action_execution_id": "ae-click",
            "intent": {
                "action_type": "click",
                "parameters": {"resolved_target": {"x": 540, "y": 960}},
            },
        },
    )
    assert clicked.status_code == 200
    assert clicked.json()["succeeded"] is True
    assert clicked.json()["action_trace"][0]["op"] == "tap"

    closed = client.post(
        "/v1/sessions/close",
        headers={"Authorization": TOKEN, "Idempotency-Key": "close:s-flow-1:done"},
        json={
            "task_id": "task-1",
            "session_id": "s-flow-1",
            "session_handle": handle,
            "reason": "done",
        },
    )
    assert closed.status_code == 200
    assert closed.json()["closed"] is True

    missing = client.post(
        "/v1/apps/capture",
        headers={"Authorization": TOKEN},
        json={"session_handle": handle, "session_id": "s-flow-1", "task_id": "task-1"},
    )
    assert missing.status_code == 404


def test_live_bridge_resolves_semantic_content_desc_and_records_tap_audit() -> None:
    ops = _SemanticTargetOps(_SEMANTIC_TARGET_XML)
    device = _LiveDevice(
        backend="cloud_phone",
        serial="cloud:1",
        ops=ops,
        controller=object(),
        resolver=CanonicalPointTargetResolver(device_width=1080, device_height=1920),
    )

    outcome = LiveCloudPhoneDeviceBridge().execute(
        device,
        action_type="click",
        parameters={
            "resolved_target": {"x": 540, "y": 960},
            "surface_phase_key": "system_dialog",
        },
        semantic_target="下次开启",
    )

    assert outcome.succeeded is True
    assert ("input", "tap", "342", "1185") in ops.calls
    trace = outcome.action_trace[0]
    assert trace["resolution_method"] == "uiautomator.content_desc"
    assert trace["resolved_x"] == 342
    assert trace["resolved_y"] == 1185
    assert trace["bounds_left"] == 174
    assert trace["bounds_bottom"] == 1250
    assert trace["command"] == "input tap"
    assert trace["command_result"] == "ok"


def test_live_bridge_blocks_system_dialog_visual_fallback_when_target_missing() -> None:
    ops = _SemanticTargetOps(
        "<hierarchy><node class=\"android.widget.Button\" content-desc=\"去开启\" "
        "clickable=\"true\" enabled=\"true\" visible-to-user=\"true\" "
        "bounds=\"[570,1121][906,1250]\" /></hierarchy>"
    )
    device = _LiveDevice(
        backend="cloud_phone",
        serial="cloud:1",
        ops=ops,
        controller=object(),
        resolver=CanonicalPointTargetResolver(device_width=1080, device_height=1920),
    )

    outcome = LiveCloudPhoneDeviceBridge().execute(
        device,
        action_type="click",
        parameters={
            "resolved_target": {"x": 342, "y": 1185},
            "surface_phase_key": "system_dialog",
        },
        semantic_target="下次开启",
    )

    assert outcome.succeeded is False
    assert outcome.error_code == "ANDROID_TARGET_NOT_FOUND"
    assert not any(call[:2] == ("input", "tap") for call in ops.calls)
    assert outcome.action_trace[0]["command_result"] == "not_executed"


def _capture_device(
    runtime_identity: tuple[str, str | None] | None,
    *,
    png_width: int = 540,
    png_height: int = 960,
) -> _LiveDevice:
    """构造带可控前台身份的真实桥接测试设备。"""

    png = (
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
        + png_width.to_bytes(4, byteorder="big")
        + png_height.to_bytes(4, byteorder="big")
        + b"\x08\x06\x00\x00\x00"
        + b"capture-test" * 16
    )

    def _write_png(dest: Path) -> None:
        dest.write_bytes(png)

    ops = AdbDeviceOps(
        backend="cloud_phone",
        width=1080,
        height=1920,
        _log=lambda _message: None,
        _screencap_fn=_write_png,
        _shell_fn=lambda _args, _timeout: None,
    )
    ops.current_runtime_identity = lambda: runtime_identity  # type: ignore[method-assign]
    return _LiveDevice(
        backend="cloud_phone",
        serial="cloud:1",
        ops=ops,
        controller=object(),
        resolver=CanonicalPointTargetResolver(device_width=1080, device_height=1920),
    )


def test_live_bridge_capture_does_not_project_launcher_as_target_app() -> None:
    device = _capture_device(
        ("com.android.launcher3", "com.android.launcher3.Launcher")
    )

    shot = LiveCloudPhoneDeviceBridge().capture(
        device,
        package_name="com.ojvcei.efggwrfd",
        activity="com.ojvcei.efggwrfd.MainActivity",
    )

    assert shot.package_name == "com.android.launcher3"
    assert shot.activity == "com.android.launcher3.Launcher"
    assert shot.package_name != "com.ojvcei.efggwrfd"


def test_live_bridge_capture_preserves_target_app_runtime_identity() -> None:
    device = _capture_device(
        ("com.ojvcei.efggwrfd", "com.ojvcei.efggwrfd.MainActivity")
    )

    shot = LiveCloudPhoneDeviceBridge().capture(
        device,
        package_name="com.ojvcei.efggwrfd",
        activity="com.ojvcei.efggwrfd.MainActivity",
    )

    assert shot.package_name == "com.ojvcei.efggwrfd"
    assert shot.activity == "com.ojvcei.efggwrfd.MainActivity"


def test_live_bridge_capture_reports_png_pixels_not_device_logical_size() -> None:
    device = _capture_device(
        ("com.ojvcei.efggwrfd", "com.ojvcei.efggwrfd.MainActivity"),
        png_width=540,
        png_height=960,
    )

    shot = LiveCloudPhoneDeviceBridge().capture(
        device,
        package_name="com.ojvcei.efggwrfd",
        activity="com.ojvcei.efggwrfd.MainActivity",
    )

    assert device.ops.wm_size() == (1080, 1920)
    assert (shot.width, shot.height) == (540, 960)
    assert shot.orientation == "portrait"


def test_live_bridge_capture_keeps_identity_unknown_when_adb_identity_unavailable() -> None:
    device = _capture_device(None)

    shot = LiveCloudPhoneDeviceBridge().capture(
        device,
        package_name="com.ojvcei.efggwrfd",
        activity="com.ojvcei.efggwrfd.MainActivity",
    )

    assert shot.package_name is None
    assert shot.activity is None


def test_live_bridge_recaptures_when_app_switches_to_browser_during_pull(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device = _capture_device(("com.example.app", "MainActivity"))
    identities = iter(
        (
            ("com.example.app", "MainActivity"),
            ("com.android.chrome", "ChromeTabbedActivity"),
            ("com.android.chrome", "ChromeTabbedActivity"),
            ("com.android.chrome", "ChromeTabbedActivity"),
        )
    )
    device.ops.current_runtime_identity = lambda: next(identities)  # type: ignore[method-assign]
    original_capture = device.ops._screencap_fn
    captures = 0

    def _capture_frame(dest: Path) -> None:
        nonlocal captures
        captures += 1
        original_capture(dest)
        marker = b"old-app-frame" if captures == 1 else b"browser-frame"
        dest.write_bytes(dest.read_bytes() + marker)

    device.ops._screencap_fn = _capture_frame
    monkeypatch.setattr(
        "services.api.app.application.xingyu_runtime.device_bridge."
        "_CAPTURE_IDENTITY_SETTLE_SECONDS",
        0,
    )

    shot = LiveCloudPhoneDeviceBridge().capture(
        device, package_name="com.example.app", activity="MainActivity"
    )

    assert captures == 2
    assert shot.png_bytes.endswith(b"browser-frame")
    assert shot.package_name == "com.android.chrome"
    assert shot.activity == "ChromeTabbedActivity"


def test_live_bridge_fails_closed_when_capture_identity_never_stabilizes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device = _capture_device(None)
    identities = iter((f"com.example.app{i}", "MainActivity") for i in range(6))
    device.ops.current_runtime_identity = lambda: next(identities)  # type: ignore[method-assign]
    monkeypatch.setattr(
        "services.api.app.application.xingyu_runtime.device_bridge."
        "_CAPTURE_IDENTITY_SETTLE_SECONDS",
        0,
    )

    with pytest.raises(RuntimeError, match="APP_CAPTURE_FOREGROUND_UNSTABLE"):
        LiveCloudPhoneDeviceBridge().capture(
            device, package_name="com.example.app", activity="MainActivity"
        )


def test_live_bridge_refreshes_morelogin_binding_before_capture_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device = _capture_device(
        ("com.ojvcei.efggwrfd", "com.ojvcei.efggwrfd.MainActivity")
    )

    def _fail_screencap(_dest: Path) -> None:
        raise RuntimeError("公网 ADB 截图通道已老化")

    device.ops._screencap_fn = _fail_screencap

    class _RefreshingController:
        def __init__(self) -> None:
            self.calls = 0

        def ensure_ready(self) -> tuple[str, list[str]]:
            self.calls += 1
            return "cloud:2", ["morelogin adb refreshed"]

    controller = _RefreshingController()
    device.controller = controller
    refreshed_ops = _capture_device(
        ("com.ojvcei.efggwrfd", "com.ojvcei.efggwrfd.MainActivity")
    ).ops
    monkeypatch.setattr(
        "services.api.app.infrastructure.device.adb_session_health.open_adb_device_ops",
        lambda *args, **kwargs: refreshed_ops,
    )
    bridge = LiveCloudPhoneDeviceBridge()

    shot = bridge.capture(
        device,
        package_name="com.ojvcei.efggwrfd",
        activity="com.ojvcei.efggwrfd.MainActivity",
    )

    assert controller.calls == 1
    assert device.serial == "cloud:2"
    assert device.ops is refreshed_ops
    assert device.capture_refresh_required is False
    assert device.adb_session_health.refresh_count == 1
    assert shot.package_name == "com.ojvcei.efggwrfd"


def test_live_bridge_capture_retries_twice_before_giving_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device = _capture_device(
        ("com.ojvcei.efggwrfd", "com.ojvcei.efggwrfd.MainActivity")
    )

    def _always_fail(_dest: Path) -> None:
        raise RuntimeError("公网 ADB 持续 502")

    device.ops._screencap_fn = _always_fail

    class _RefreshingController:
        def __init__(self) -> None:
            self.calls = 0

        def ensure_ready(self) -> tuple[str, list[str]]:
            self.calls += 1
            return f"cloud:{self.calls}", ["morelogin adb refreshed"]

    controller = _RefreshingController()
    device.controller = controller

    def _failing_ops(*args, **kwargs):
        del args, kwargs
        ops = _capture_device(
            ("com.ojvcei.efggwrfd", "com.ojvcei.efggwrfd.MainActivity")
        ).ops
        ops._screencap_fn = _always_fail
        return ops

    monkeypatch.setattr(
        "services.api.app.infrastructure.device.adb_session_health.open_adb_device_ops",
        _failing_ops,
    )
    bridge = LiveCloudPhoneDeviceBridge()

    with pytest.raises(RuntimeError, match="持续 502"):
        bridge.capture(
            device,
            package_name="com.ojvcei.efggwrfd",
            activity="com.ojvcei.efggwrfd.MainActivity",
        )

    assert controller.calls == 2
    assert device.capture_refresh_required is True


def test_live_bridge_replaces_stale_controller_during_capture_recovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device = _capture_device(
        ("com.ojvcei.efggwrfd", "com.ojvcei.efggwrfd.MainActivity")
    )

    def _fail_screencap(_dest: Path) -> None:
        raise RuntimeError("旧公网 ADB 截图通道已失效")

    device.ops._screencap_fn = _fail_screencap

    class _FreshController:
        def ensure_ready(self, *, timeout_seconds: float) -> tuple[str, list[str]]:
            assert timeout_seconds == 25.0
            return "cloud:fresh", ["fresh controller ready"]

    fresh = _FreshController()

    class _StaleController:
        def ensure_ready(self, *, timeout_seconds: float) -> tuple[str, list[str]]:
            assert timeout_seconds == 25.0
            raise RuntimeError("stale controller")

        def fresh_for_adb_refresh(self) -> object:
            return fresh

    device.controller = _StaleController()
    refreshed_ops = _capture_device(
        ("com.ojvcei.efggwrfd", "com.ojvcei.efggwrfd.MainActivity")
    ).ops
    monkeypatch.setattr(
        "services.api.app.infrastructure.device.adb_session_health.open_adb_device_ops",
        lambda *args, **kwargs: refreshed_ops,
    )

    shot = LiveCloudPhoneDeviceBridge().capture(
        device,
        package_name="com.ojvcei.efggwrfd",
        activity="com.ojvcei.efggwrfd.MainActivity",
    )

    assert device.controller is fresh
    assert device.serial == "cloud:fresh"
    assert device.ops is refreshed_ops
    assert device.adb_session_health.refresh_count == 1
    assert shot.package_name == "com.ojvcei.efggwrfd"


def test_live_bridge_healthy_first_capture_does_not_proactive_refresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device = _capture_device(
        ("com.ojvcei.efggwrfd", "com.ojvcei.efggwrfd.MainActivity")
    )
    refresh_calls: list[str] = []

    def _forbidden_refresh(*args, **kwargs):
        del args, kwargs
        refresh_calls.append("refresh")
        raise AssertionError("健康首帧不得预防性刷新")

    monkeypatch.setattr(
        "services.api.app.application.xingyu_runtime.device_bridge.recover_cloud_phone_adb_binding",
        _forbidden_refresh,
    )

    shot = LiveCloudPhoneDeviceBridge().capture(
        device,
        package_name="com.ojvcei.efggwrfd",
        activity="com.ojvcei.efggwrfd.MainActivity",
    )

    assert refresh_calls == []
    assert device.adb_session_health.refresh_count == 0
    assert shot.package_name == "com.ojvcei.efggwrfd"


def test_live_bridge_proactive_refresh_on_pending_slow_pull(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device = _capture_device(
        ("com.ojvcei.efggwrfd", "com.ojvcei.efggwrfd.MainActivity")
    )
    device.adb_session_health.pending_refresh_reason = "slow_pull"
    device.adb_session_health.last_refresh_at = 0.0

    class _Controller:
        def ensure_ready(self) -> tuple[str, list[str]]:
            return "cloud:3", ["proactive"]

    device.controller = _Controller()
    refreshed_ops = _capture_device(
        ("com.ojvcei.efggwrfd", "com.ojvcei.efggwrfd.MainActivity")
    ).ops
    monkeypatch.setattr(
        "services.api.app.infrastructure.device.adb_session_health.open_adb_device_ops",
        lambda *args, **kwargs: refreshed_ops,
    )

    shot = LiveCloudPhoneDeviceBridge().capture(
        device,
        package_name="com.ojvcei.efggwrfd",
        activity="com.ojvcei.efggwrfd.MainActivity",
    )

    assert device.serial == "cloud:3"
    assert device.ops is refreshed_ops
    assert device.adb_session_health.refresh_count == 1
    assert device.adb_session_health.last_decision_reason == "slow_pull"
    assert shot.package_name == "com.ojvcei.efggwrfd"



def test_live_bridge_close_clears_leftover_qc_app() -> None:
    shell_calls: list[tuple[str, ...]] = []

    class _Ops(AdbDeviceOps):
        def __init__(self) -> None:
            super().__init__(
                backend="fake",
                width=1080,
                height=1920,
                _log=lambda _message: None,
                _screencap_fn=lambda _dest: None,
                _shell_fn=self._shell,
                _shell_output_fn=self._shell_output,
            )

        def _shell(self, args: list[str], _timeout: float) -> None:
            shell_calls.append(tuple(args))

        def _shell_output(self, args: list[str], _timeout: float) -> str:
            shell_calls.append(tuple(args))
            if args[:2] == ["dumpsys", "activity"]:
                return (
                    "mResumedActivity: ActivityRecord{1 u0 "
                    "com.leftover.app/.MainActivity t1}"
                )
            return ""

    device = _LiveDevice(
        backend="cloud_phone",
        serial="cloud:1",
        ops=_Ops(),
        controller=object(),
        resolver=CanonicalPointTargetResolver(device_width=1080, device_height=1920),
    )

    LiveCloudPhoneDeviceBridge().close(
        device,
        package_name="com.first.apk",
        reason="exploration_completed",
    )

    assert ("am", "force-stop", "com.first.apk") in shell_calls
    assert ("am", "force-stop", "com.leftover.app") in shell_calls
    assert ("input", "keyevent", "3") in shell_calls


def test_session_recovers_after_in_memory_store_loss(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """模拟 API --reload：内存表清空后，安装须按磁盘索引 + 旧 handle 恢复。"""

    apk = tmp_path / "demo.apk"
    apk.write_bytes(b"fake-apk-bytes")
    digest = hashlib.sha256(apk.read_bytes()).hexdigest()
    index_path = tmp_path / "xingyu-session-index.json"
    monkeypatch.setenv("ALIOTH_XINGYU_BUILD_CACHE", str(tmp_path))
    monkeypatch.setenv("ALIOTH_XINGYU_SESSION_INDEX", str(index_path))

    from services.api.app.application.xingyu_runtime.session_store import (
        XingyuSessionStore,
    )

    service = XingyuOfficialService(
        bridge=DryRunDeviceBridge(),
        sessions=XingyuSessionStore(persist_path=index_path),
    )
    started = service.start_session(
        {
            "task_id": "task-recover",
            "session_id": "s-recover",
            "options": {
                "backend": "cloud_phone",
                "morelogin_env_id": "env-recover-1",
            },
            "remark": "recover-test",
        }
    )
    handle = started["session_handle"]
    assert handle.startswith("xyh_")
    assert index_path.is_file()

    # 模拟 uvicorn reload：新进程空内存表，但磁盘索引仍在
    reloaded = XingyuOfficialService(
        bridge=DryRunDeviceBridge(),
        sessions=XingyuSessionStore(persist_path=index_path),
    )
    assert reloaded.sessions.get_by_handle(handle) is None

    installed = reloaded.install(
        {
            "task_id": "task-recover",
            "session_id": "s-recover",
            "session_handle": handle,
            "platform_build_ref": "demo",
            "sha256": digest,
            "package_name": "com.example.recover",
            "protocol_version": "app-bootstrap.v1",
            "replace_policy": "same_checksum_only",
        }
    )
    assert installed["succeeded"] is True
    assert installed["installed_package"] == "com.example.recover"
    restored = reloaded.sessions.get_by_handle(handle)
    assert restored is not None
    assert restored.session_id == "s-recover"
    assert restored.morelogin_env_id == "env-recover-1"


def test_store_loss_and_reattach_skip_leftover_cleanup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """任务进行中重建绑定禁止 leftover force-stop／HOME，避免截成系统桌面。"""

    index_path = tmp_path / "xingyu-session-index.json"
    monkeypatch.setenv("ALIOTH_XINGYU_SESSION_INDEX", str(index_path))

    from services.api.app.application.xingyu_runtime.session_store import (
        XingyuSessionStore,
    )
    from services.api.app.infrastructure.device.adb_ops import AdbDeviceOps

    cleanup_calls: list[dict[str, object]] = []
    start_flags: list[bool] = []

    class _Ops(AdbDeviceOps):
        def __init__(self) -> None:
            super().__init__(
                backend="fake",
                width=1080,
                height=1920,
                _log=lambda _m: None,
                _screencap_fn=lambda _d: None,
                _shell_fn=lambda *_a, **_k: None,
            )

        def clear_leftover_qc_app(
            self,
            *,
            package_name: str | None = None,
            press_home: bool = True,
        ) -> list[str]:
            cleanup_calls.append(
                {"package_name": package_name, "press_home": press_home}
            )
            return ["force_stop foreground=com.example.active", "press HOME"]

    class _Controller:
        def __init__(self, **kwargs: object) -> None:
            del kwargs

        def ensure_ready(self) -> tuple[str, list[str]]:
            return "cloud:recover", ["ready"]

    class _RecordingBridge(LiveCloudPhoneDeviceBridge):
        def start(
            self,
            *,
            backend: str,
            morelogin_env_id: str | None = None,
            clear_leftover: bool = True,
        ) -> tuple[str | None, object]:
            start_flags.append(clear_leftover)
            return super().start(
                backend=backend,
                morelogin_env_id=morelogin_env_id,
                clear_leftover=clear_leftover,
            )

    monkeypatch.setattr(
        "services.api.app.infrastructure.device.cloud_phone.CloudPhoneController",
        _Controller,
    )
    monkeypatch.setattr(
        "services.api.app.application.xingyu_runtime.device_bridge.open_adb_device_ops",
        lambda *args, **kwargs: _Ops(),
    )

    bridge = _RecordingBridge()
    sessions = XingyuSessionStore(persist_path=index_path)
    service = XingyuOfficialService(bridge=bridge, sessions=sessions)
    started = service.start_session(
        {
            "task_id": "task-active",
            "session_id": "s-active",
            "options": {"backend": "cloud_phone", "morelogin_env_id": "env-1"},
        }
    )
    assert start_flags == [True]
    assert len(cleanup_calls) == 1
    handle = started["session_handle"]
    service.sessions.update("s-active", package_name="com.example.active")

    # 模拟内存丢失后的动作请求恢复
    reloaded = XingyuOfficialService(
        bridge=_RecordingBridge(),
        sessions=XingyuSessionStore(persist_path=index_path),
    )
    cleanup_before = len(cleanup_calls)
    flags_before = len(start_flags)
    recovered = reloaded._require_session(
        {
            "session_handle": handle,
            "task_id": "task-active",
            "session_id": "s-active",
            "options": {"backend": "cloud_phone", "morelogin_env_id": "env-1"},
        }
    )
    assert recovered.session_id == "s-active"
    assert start_flags[flags_before:] == [False]
    assert len(cleanup_calls) == cleanup_before

    reattach_flags_before = len(start_flags)
    reattach_cleanup_before = len(cleanup_calls)
    service.reattach_session(
        {
            "task_id": "task-active",
            "session_id": "s-active",
            "options": {"backend": "cloud_phone", "morelogin_env_id": "env-1"},
        }
    )
    assert start_flags[reattach_flags_before:] == [False]
    assert len(cleanup_calls) == reattach_cleanup_before


def test_checksum_mismatch(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    apk = tmp_path / "bad.apk"
    apk.write_bytes(b"abc")
    monkeypatch.setenv("ALIOTH_XINGYU_BUILD_CACHE", str(tmp_path))
    client = _client(monkeypatch)
    headers = {"Authorization": TOKEN}
    started = client.post(
        "/v1/sessions",
        headers={**headers, "Idempotency-Key": "session:s-bad"},
        json={"task_id": "t", "session_id": "s-bad", "options": {}},
    )
    handle = started.json()["session_handle"]
    result = client.post(
        "/v1/apps/install",
        headers={**headers, "Idempotency-Key": "install:bad"},
        json={
            "session_handle": handle,
            "platform_build_ref": "bad",
            "sha256": "0" * 64,
            "task_id": "t",
            "session_id": "s-bad",
        },
    )
    assert result.status_code == 200
    assert result.json()["succeeded"] is False
    assert result.json()["error_code"] == "INSTALL_CHECKSUM_MISMATCH"


def test_resolve_apk_and_verify(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    apk = tmp_path / "build-1.apk"
    apk.write_bytes(b"hello-apk")
    monkeypatch.setenv("ALIOTH_XINGYU_BUILD_CACHE", str(tmp_path))
    path = resolve_apk_path("build-1")
    assert path == apk.resolve()
    digest = hashlib.sha256(b"hello-apk").hexdigest()
    assert verify_sha256(path, digest) == digest
    with pytest.raises(BuildArtifactError) as exc:
        verify_sha256(path, "a" * 64)
    assert exc.value.error_code == "INSTALL_CHECKSUM_MISMATCH"


def test_resolve_app_build_ref_aliases(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    build_id = "62681f10-c761-4ade-8834-16879baae6f8"
    apk = tmp_path / f"{build_id}.apk"
    apk.write_bytes(b"app-build-bytes")
    monkeypatch.setenv("ALIOTH_XINGYU_BUILD_CACHE", str(tmp_path))
    path = resolve_apk_path(f"app-build:{build_id}")
    assert path == apk.resolve()

    # 另一命名：目錄前綴
    other_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    nested_apk = tmp_path / "app-build" / f"{other_id}.apk"
    nested_apk.parent.mkdir(parents=True, exist_ok=True)
    nested_apk.write_bytes(b"nested")
    assert resolve_apk_path(f"app-build:{other_id}") == nested_apk.resolve()


def test_resolve_apk_from_platform_build_content_address(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """青雲上传落盘为 {sha256[:2]}/{sha256}.apk，星语 install 必须能解析。"""
    digest = hashlib.sha256(b"platform-build-apk").hexdigest()
    apk = tmp_path / digest[:2] / f"{digest}.apk"
    apk.parent.mkdir(parents=True, exist_ok=True)
    apk.write_bytes(b"platform-build-apk")
    monkeypatch.setenv("ALIOTH_PLATFORM_BUILD_STORAGE_ROOT", str(tmp_path))
    monkeypatch.delenv("ALIOTH_XINGYU_BUILD_CACHE", raising=False)
    monkeypatch.delenv("ALIOTH_DATABASE_URL", raising=False)
    path = resolve_apk_path(
        "app-build:5d8ad774-9581-4b80-8f2a-3b9e94494fba",
        sha256=digest,
    )
    assert path == apk.resolve()


def test_action_normalize_click_and_resolved_target() -> None:
    from services.api.app.application.xingyu_runtime.action_normalize import (
        normalize_action_type,
        normalize_execute_parameters,
    )

    assert normalize_action_type("CLICK") == "tap"
    assert normalize_action_type("tap") == "tap"
    params = normalize_execute_parameters({"resolved_target": {"x": 10, "y": 20}})
    assert params["point"] == {"x": 10, "y": 20, "space": "device_px"}

    scaled = normalize_execute_parameters(
        {
            "resolved_target": {
                "x": 283,
                "y": 34,
                "space": "capture_px",
                "base_width": 322,
                "base_height": 572,
            }
        }
    )
    assert scaled["point"] == {"x": 283, "y": 34, "space": "capture_px"}
    assert scaled["base_width"] == 322
    assert scaled["base_height"] == 572
    assert scaled["display"] == {"width": 322, "height": 572}

@pytest.mark.asyncio
async def test_with_idempotency_runs_sync_handler_off_event_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """同步 ADB handler 不得占用事件循环，否则提交 App 任务后整站探活假死。"""
    import asyncio
    import importlib
    import time

    from fastapi.responses import JSONResponse
    from starlette.requests import Request

    xingyu_router_mod = importlib.import_module(
        "services.api.app.api.v1.xingyu_official.router"
    )
    service = XingyuOfficialService(bridge=DryRunDeviceBridge())
    reset_xingyu_official_service(service)
    monkeypatch.setattr(
        xingyu_router_mod,
        "get_xingyu_official_service",
        lambda: service,
    )

    started = time.perf_counter()
    saw_concurrent_progress = False

    async def _probe_event_loop() -> None:
        nonlocal saw_concurrent_progress
        await asyncio.sleep(0.05)
        if time.perf_counter() - started < 0.4:
            saw_concurrent_progress = True

    def blocking_handler(
        _service: XingyuOfficialService, _payload: dict
    ) -> JSONResponse:
        time.sleep(0.35)
        return JSONResponse({"ok": True})

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/v1/sessions",
        "headers": [(b"content-type", b"application/json")],
    }

    async def receive() -> dict:
        return {
            "type": "http.request",
            "body": b'{"task_id":"t","session_id":"s"}',
            "more_body": False,
        }

    request = Request(scope, receive)
    probe = asyncio.create_task(_probe_event_loop())
    response = await xingyu_router_mod._with_idempotency(
        request, None, blocking_handler
    )
    await probe
    assert response.status_code == 200
    assert saw_concurrent_progress is True


def test_dry_run_device_bridge_install_accepts_same_label_skip_authorization() -> None:
    bridge = DryRunDeviceBridge()
    installed = bridge.install(
        {"kind": "dry_run"},
        Path("dryrun.apk"),
        package_hint="com.example.test",
        same_label_skip_authorization="authorized-skip",
    )
    assert installed == "com.example.test"
