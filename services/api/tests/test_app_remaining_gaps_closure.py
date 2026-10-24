"""App 仍缺／偏弱闭环：环境身份、账号注入、View Hierarchy、设备池、恢复事件。"""

from __future__ import annotations

import pytest

from services.api.app.application.app_state_invariants import (
    build_app_state_invariant_snapshot,
)
from services.api.app.application.app_surface_evidence import (
    bound_view_hierarchy_payload,
    build_screen_id,
    project_ui_snapshot_from_hierarchy,
)
from services.api.app.application.device_pool import (
    DevicePoolError,
    reset_device_pool_for_tests,
)
from services.api.app.application.evidence_request_routing import (
    select_restoration_strategy,
)
from services.api.app.domain.inspection import (
    EvidenceReplayPolicy,
    EvidenceRequiredEquivalence,
    EvidenceSessionDependency,
    RestorationStrategy,
)
from services.api.app.domain.interaction import (
    AppAccountInjectRequest,
    SurfaceKind,
    SurfaceSessionSpec,
    build_android_screenshot_only_manifest,
)
from services.api.app.infrastructure.device.adb_ops import AdbDeviceOps
from services.api.app.infrastructure.device.android_hierarchy_match import (
    resolve_match_to_device_point,
)


def test_session_options_and_invariants_carry_locale_and_account() -> None:
    spec = SurfaceSessionSpec(
        task_id="t1",
        session_id="s1",
        surface_kind=SurfaceKind.ANDROID,
        device_ref="cloud_phone:qa",
        platform_build_ref="build:1",
        account_environment_ref="acct:qa-1",
        options={
            "language": "zh-CN",
            "region": "CN",
            "experiment": "exp-a",
        },
    )
    assert spec.options["language"] == "zh-CN"
    snap = build_app_state_invariant_snapshot(
        task_id="t1",
        capture_id="c1",
        session_id="s1",
        surface_kind=SurfaceKind.ANDROID,
        account_environment_ref="acct:qa-1",
        session_options=spec.options,
        package_name="com.example",
    )
    assert snap.environment["language"] == "zh-CN"
    assert snap.environment["region"] == "CN"
    assert snap.environment["experiment"] == "exp-a"
    assert snap.environment["account_environment_ref"] == "acct:qa-1"


def test_view_hierarchy_manifest_and_payload() -> None:
    manifest = build_android_screenshot_only_manifest(
        adapter_id="t", adapter_version="1", backend="fake"
    )
    assert manifest.observations["view_hierarchy"].status.value == "ready"
    assert manifest.observations["orientation"].status.value == "ready"
    assert "view_id" in manifest.actions["click"].target_modes
    assert "set_clipboard" in manifest.actions
    assert "open_external" in manifest.actions
    payload = bound_view_hierarchy_payload(
        raw_text="ACTIVITY com.example/.Main",
        package_name="com.example",
        status="captured",
    )
    assert payload["source"] == "dumpsys_activity_top"
    assert payload["artifact_kind"] == "view_hierarchy"


def test_screen_id_stable_against_volatile_text() -> None:
    xml_a = (
        '<?xml version="1.0"?><hierarchy>'
        '<node class="android.widget.Button" resource-id="com.app:id/login" '
        'text="登录" clickable="true" bounds="[0,0][100,50]"/>'
        '<node class="android.widget.TextView" text="12:34:56" '
        'clickable="false" bounds="[0,50][100,80]"/>'
        "</hierarchy>"
    )
    xml_b = (
        '<?xml version="1.0"?><hierarchy>'
        '<node class="android.widget.Button" resource-id="com.app:id/login" '
        'text="登录" clickable="true" bounds="[0,0][100,50]"/>'
        '<node class="android.widget.TextView" text="12:35:01" '
        'clickable="false" bounds="[0,50][100,80]"/>'
        "</hierarchy>"
    )
    a = project_ui_snapshot_from_hierarchy(xml_a, package_name="com.app", activity=".Main")
    b = project_ui_snapshot_from_hierarchy(xml_b, package_name="com.app", activity=".Main")
    assert a["screen_id"] == b["screen_id"]
    salted = build_screen_id(
        package_name="com.app",
        activity=".Main",
        structure_signature=a["structure_signature"].removeprefix("sha256:"),
        language="en",
        region="US",
    )
    assert salted != a["screen_id"]


def test_hierarchy_match_resolves_view_id() -> None:
    xml = (
        '<?xml version="1.0"?><hierarchy>'
        '<node class="android.widget.Button" resource-id="com.app:id/login" '
        'text="登录" clickable="true" enabled="true" visible-to-user="true" '
        'bounds="[10,20][110,70]"/>'
        "</hierarchy>"
    )
    x, y = resolve_match_to_device_point(
        xml,
        match={"view_id": "login"},
        device_width=1080,
        device_height=1920,
    )
    assert x == 60
    assert y == 45


def test_type_text_cjk_uses_clipboard_paste() -> None:
    calls: list[list[str]] = []

    def shell_fn(args, timeout):
        del timeout
        calls.append(list(args))

    ops = AdbDeviceOps(
        backend="google_emulator",
        width=1080,
        height=1920,
        _log=lambda _m: None,
        _screencap_fn=lambda _p: None,
        _shell_fn=shell_fn,
        _shell_output_fn=lambda args, timeout: "",
    )
    ops.type_text("你好")
    assert any(item[:2] == ["input", "keyevent"] and "279" in item for item in calls)


def test_device_pool_claim_and_release() -> None:
    pool = reset_device_pool_for_tests(slots=["cloud_phone:a", "cloud_phone:b"])
    first = pool.claim(task_id="t1", session_id="s1", preferred_device_ref="cloud_phone:a")
    assert first.device_ref == "cloud_phone:a"
    second = pool.claim(task_id="t2", session_id="s2", preferred_device_ref="cloud_phone:a")
    assert second.device_ref == "cloud_phone:b"
    assert pool.release(session_id="s1") is False
    assert pool.release(lease=first) is True
    again = pool.claim(task_id="t3", session_id="s3", preferred_device_ref="cloud_phone:a")
    assert again.device_ref == "cloud_phone:a"
    with pytest.raises(DevicePoolError) as busy:
        pool.claim(task_id="t4", session_id="s4", preferred_device_ref="cloud_phone:a")
    assert busy.value.error_code == "DEVICE_BUSY"


def test_select_restoration_strategy_for_session_events() -> None:
    from types import SimpleNamespace

    permission = SimpleNamespace(
        replay_policy=EvidenceReplayPolicy.SAME_CONTEXT_ALLOWED,
        required_equivalence=EvidenceRequiredEquivalence.LOGICAL_EQUIVALENT,
        session_dependency=EvidenceSessionDependency.SNAPSHOT,
        target_invariants={"environment": {"session_event": "permission_dialog"}},
    )
    assert (
        select_restoration_strategy(request=permission, surface_kind=SurfaceKind.ANDROID)
        is RestorationStrategy.SAME_APP_SESSION
    )
    death = SimpleNamespace(
        replay_policy=EvidenceReplayPolicy.NEW_SESSION_ALLOWED,
        required_equivalence=EvidenceRequiredEquivalence.LOGICAL_EQUIVALENT,
        session_dependency=EvidenceSessionDependency.SNAPSHOT,
        target_invariants={"environment": {"session_event": "process_death"}},
    )
    assert (
        select_restoration_strategy(request=death, surface_kind=SurfaceKind.ANDROID)
        is RestorationStrategy.NEW_SESSION_REPLAY
    )


@pytest.mark.asyncio
async def test_fake_inject_account_binds_ref() -> None:
    from services.api.app.infrastructure.app_runtime.fake import FakeAppRuntime

    runtime = FakeAppRuntime()
    await runtime.start_session(
        SurfaceSessionSpec(
            task_id="t1",
            session_id="s1",
            surface_kind=SurfaceKind.ANDROID,
            platform_build_ref="build:1",
            device_ref="fake:default",
        )
    )
    result = await runtime.inject_account(
        AppAccountInjectRequest(
            task_id="t1",
            session_id="s1",
            account_environment_ref="acct:demo",
            package_name="com.example",
        )
    )
    assert result.succeeded is True
    assert result.bound is True
    assert (
        runtime._sessions["s1"].spec.account_environment_ref == "acct:demo"  # noqa: SLF001
    )
    assert runtime._sessions["s1"].spec.options["account_bound"] is True  # noqa: SLF001
