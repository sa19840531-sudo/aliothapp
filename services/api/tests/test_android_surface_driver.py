"""AndroidAdbSurfaceDriver 合同与坐标映射测试。"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from services.api.app.application.interaction_runtime import SemanticTargetResolutionRequest
from services.api.app.domain.interaction import (
    ActionIntent,
    CaptureDirective,
    CaptureProfile,
    InteractionOperation,
    InteractionRiskLevel,
    SurfaceKind,
    SurfaceSessionSpec,
)
from services.api.app.infrastructure.device.adb_ops import AdbDeviceOps, map_point
from services.api.app.infrastructure.device.android_runtime_observation import (
    parse_android_ui_hierarchy,
)
from services.api.app.infrastructure.device.android_surface_driver import (
    AndroidAdbSurfaceDriver,
)
from services.api.app.infrastructure.device.android_target_resolver import (
    CanonicalPointTargetResolver,
    SemanticTargetResolutionError,
)


class _FakeOps(AdbDeviceOps):
    def __init__(self) -> None:
        self.calls: list[tuple] = []
        super().__init__(
            backend="fake",
            width=720,
            height=1280,
            _log=lambda _m: None,
            _screencap_fn=self._screencap,
            _shell_fn=self._shell,
        )

    def _screencap(self, dest: Path) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        # 最小合法 PNG 头 + 填充
        dest.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 120)
        self.calls.append(("screencap", str(dest)))

    def _shell(self, args: list[str], timeout: float) -> None:
        self.calls.append(("shell", tuple(args), timeout))


class _RuntimeObservationOps(_FakeOps):
    def __init__(self) -> None:
        super().__init__()
        self._shell_output_fn = self._shell_output

    def _shell_output(self, args: list[str], timeout: float) -> str:
        self.calls.append(("shell_output", tuple(args), timeout))
        return (
            '<hierarchy><node index="0" text="关闭" '
            'resource-id="demo:id/close" class="android.widget.Button" '
            'content-desc="关闭" bounds="[100,200][180,280]" '
            'visible-to-user="true" enabled="true" clickable="true" '
            'focusable="true" /></hierarchy>'
        )


class _RuntimeIdentityOps(_FakeOps):
    def __init__(self) -> None:
        super().__init__()
        self._shell_output_fn = self._shell_output

    def _shell_output(self, args: list[str], timeout: float) -> str:
        self.calls.append(("shell_output", tuple(args), timeout))
        if args == ["wm", "size"]:
            return "Physical size: 720x1280"
        if args == ["dumpsys", "activity", "activities"]:
            return "mResumedActivity: ActivityRecord{3 u0 com.example.target/.MainActivity}"
        return ""


class _HjllRecoveryOps(_FakeOps):
    """模拟触控后外跳浏览器，再由产品编排拉回 HJLL。"""

    def __init__(self) -> None:
        super().__init__()
        self.foreground_package = "com.android.chrome"

    def current_runtime_identity(self) -> tuple[str, str | None]:
        return self.foreground_package, ".MainActivity"

    def warm_bring_to_front(self, package_name: str, *, activity: str | None = None) -> None:
        del activity
        self.calls.append(("warm_bring_to_front", package_name))
        self.foreground_package = package_name

    def back(self) -> None:
        self.calls.append(("back",))
        self.foreground_package = "com.hj.ttthree"


def _profile() -> CaptureProfile:
    return CaptureProfile(
        profile_id="t",
        before=CaptureDirective(role="before_action", artifact_types=("screenshot",)),
        after=CaptureDirective(role="after_action", artifact_types=("screenshot",)),
    )


def _intent(
    *,
    op: InteractionOperation,
    parameters=None,
    semantic_target: str | None = "关闭",
) -> ActionIntent:
    return ActionIntent(
        task_id="task-1",
        session_id="session-1",
        intent_id="intent-1",
        idempotency_key="task-1:intent-1",
        surface_kind=SurfaceKind.ANDROID,
        source_capture_id="cap-seed",
        action_type=op,
        semantic_target=semantic_target,
        purpose="test",
        parameters=parameters or {},
        risk_level=InteractionRiskLevel.LOW,
        capture_profile=_profile(),
        deadline=datetime.now(UTC) + timedelta(seconds=30),
    )


@pytest.mark.asyncio
async def test_capabilities_and_reject_web_session() -> None:
    driver = AndroidAdbSurfaceDriver(backend="fake", ops=_FakeOps())
    manifest = driver.describe_capabilities()
    assert manifest.surface_kind is SurfaceKind.ANDROID
    assert "tap" in manifest.action_types
    assert "input" in manifest.action_types
    assert "scroll" in manifest.action_types
    assert "screenshot" in manifest.artifact_types
    with pytest.raises(ValueError):
        await driver.start_session(
            SurfaceSessionSpec(
                task_id="t",
                session_id="s",
                surface_kind=SurfaceKind.WEB,
            )
        )


@pytest.mark.asyncio
async def test_tap_maps_canonical_and_calls_shell() -> None:
    ops = _FakeOps()
    driver = AndroidAdbSurfaceDriver(backend="fake", ops=ops)
    session = await driver.start_session(
        SurfaceSessionSpec(
            task_id="task-1",
            session_id="session-1",
            surface_kind=SurfaceKind.ANDROID,
        )
    )
    # 1080x1920 canonical (540,960) → 720x1280 device (360,640)
    intent = _intent(
        op=InteractionOperation.TAP,
        parameters={
            "point": {"x": 540, "y": 960, "space": "canonical_px"},
            "display": {"width": 1080, "height": 1920},
        },
    )
    raw = await driver.execute(session=session, intent=intent, action_execution_id="ae-1")
    assert raw.succeeded is True
    assert ("shell", ("input", "tap", "360", "640"), 20.0) in ops.calls


@pytest.mark.asyncio
async def test_hjll_orchestrated_settle_recovers_foreground_without_retargeting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """固定点触控成功后只等待／回前台，Driver 不会重算或改写点位。"""

    async def _no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(
        "services.api.app.infrastructure.device.android_surface_driver.asyncio.sleep",
        _no_sleep,
    )
    ops = _HjllRecoveryOps()
    driver = AndroidAdbSurfaceDriver(backend="fake", ops=ops)

    result = await driver.settle_hjll_orchestrated_action(
        expected_package="com.hj.ttthree",
        settle_ms=1_500,
    )

    assert result["recovered"] is True
    assert result["recovery"] == "back"
    assert result["package_name"] == "com.hj.ttthree"
    assert ("back",) in ops.calls
    assert not any(call[0] == "warm_bring_to_front" for call in ops.calls)


@pytest.mark.asyncio
async def test_hjll_orchestrated_settle_backs_out_of_play_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(
        "services.api.app.infrastructure.device.android_surface_driver.asyncio.sleep",
        _no_sleep,
    )
    ops = _HjllRecoveryOps()
    ops.foreground_package = "com.android.vending"
    driver = AndroidAdbSurfaceDriver(backend="fake", ops=ops)

    result = await driver.settle_hjll_orchestrated_action(
        expected_package="com.hj.ttthree",
        settle_ms=1_500,
    )

    assert result["recovered"] is True
    assert result["recovery"] == "back"
    assert ("back",) in ops.calls
    assert not any(call[0] == "warm_bring_to_front" for call in ops.calls)


@pytest.mark.asyncio
async def test_scroll_direction_and_back() -> None:
    ops = _FakeOps()
    driver = AndroidAdbSurfaceDriver(backend="fake", ops=ops)
    session = await driver.start_session(
        SurfaceSessionSpec(
            task_id="task-1",
            session_id="session-1",
            surface_kind=SurfaceKind.ANDROID,
        )
    )
    scroll = await driver.execute(
        session=session,
        intent=_intent(
            op=InteractionOperation.SCROLL,
            semantic_target=None,
            parameters={"direction": "down"},
        ),
        action_execution_id="ae-scroll",
    )
    assert scroll.succeeded is True
    assert any(c[0] == "shell" and c[1][0:2] == ("input", "swipe") for c in ops.calls)

    back = await driver.execute(
        session=session,
        intent=_intent(op=InteractionOperation.BACK, semantic_target=None),
        action_execution_id="ae-back",
    )
    assert back.succeeded is True
    assert ("shell", ("input", "keyevent", "4"), 15.0) in ops.calls


@pytest.mark.asyncio
async def test_capture_returns_screenshot_artifact(tmp_path: Path) -> None:
    ops = _FakeOps()
    driver = AndroidAdbSurfaceDriver(backend="fake", ops=ops, capture_dir=tmp_path)
    session = await driver.start_session(
        SurfaceSessionSpec(
            task_id="task-1",
            session_id="session-1",
            surface_kind=SurfaceKind.ANDROID,
        )
    )
    artifacts = await driver.capture(session=session, profile=_profile())
    assert artifacts.artifacts[0].artifact_type == "screenshot"
    assert isinstance(artifacts.artifacts[0].content, bytes)
    latency = artifacts.metadata["capture_pipeline_latency"]
    assert latency["protocol_version"] == "app-capture-pipeline-latency.v1"
    assert latency["path_mode"] == "backend_native"
    assert latency["png_bytes"] == len(artifacts.artifacts[0].content)
    assert isinstance(latency["wm_size_ms"], int)
    assert isinstance(latency["identity_ms"], int)
    assert isinstance(latency["total_ms"], int)
    assert latency["wm_size_source"] == "cached"
    assert latency["remote_screencap_ms"] is None
    assert latency["adb_pull_ms"] is None


@pytest.mark.asyncio
async def test_capture_pipeline_records_shell_wm_size_and_identity(tmp_path: Path) -> None:
    ops = _RuntimeIdentityOps()
    driver = AndroidAdbSurfaceDriver(backend="fake", ops=ops, capture_dir=tmp_path)
    session = await driver.start_session(
        SurfaceSessionSpec(
            task_id="task-1",
            session_id="session-1",
            surface_kind=SurfaceKind.ANDROID,
        )
    )

    artifacts = await driver.capture(session=session, profile=_profile())

    latency = artifacts.metadata["capture_pipeline_latency"]
    assert latency["wm_size_source"] == "shell"
    assert artifacts.metadata["package_name"] == "com.example.target"
    assert artifacts.metadata["activity"] == "com.example.target.MainActivity"
    assert any(
        call[0] == "shell_output" and call[1] == ("wm", "size") for call in ops.calls
    )
    ops = _RuntimeObservationOps()
    driver = AndroidAdbSurfaceDriver(backend="fake", ops=ops)
    await driver.start_session(
        SurfaceSessionSpec(
            task_id="task-1",
            session_id="session-1",
            surface_kind=SurfaceKind.ANDROID,
        )
    )

    observation = await driver.observe_runtime_state()

    assert observation.available is True
    assert (
        observation.dismiss_node_at_point(120, 220, coordinate_space="device_px")
        is not None
    )
    assert any(call[0] == "shell_output" for call in ops.calls)
    assert driver.describe_capabilities().observations["ui_tree"].status.value == "ready"


@pytest.mark.asyncio
async def test_observe_includes_current_runtime_package_and_activity() -> None:
    ops = _RuntimeIdentityOps()
    driver = AndroidAdbSurfaceDriver(backend="fake", ops=ops)
    session = await driver.start_session(
        SurfaceSessionSpec(
            task_id="task-1",
            session_id="session-1",
            surface_kind=SurfaceKind.ANDROID,
        )
    )

    observation = await driver.observe(session=session, profile=_profile())

    assert observation.payload["package_name"] == "com.example.target"
    assert observation.payload["activity"] == "com.example.target.MainActivity"
    assert any(call[0] == "shell_output" for call in ops.calls)


@pytest.mark.asyncio
async def test_read_runtime_identity_skips_screencap() -> None:
    ops = _RuntimeIdentityOps()
    driver = AndroidAdbSurfaceDriver(backend="fake", ops=ops)
    session = await driver.start_session(
        SurfaceSessionSpec(
            task_id="task-1",
            session_id="session-1",
            surface_kind=SurfaceKind.ANDROID,
        )
    )

    observation = await driver.read_runtime_identity(session=session)

    assert observation.payload["identity_source"] == "dumpsys"
    assert observation.payload["package_name"] == "com.example.target"
    assert observation.payload["activity"] == "com.example.target.MainActivity"
    assert not any(call[0] == "screencap" for call in ops.calls)
    assert any(
        call[0] == "shell_output" and call[1][0:2] == ("dumpsys", "activity")
        for call in ops.calls
    )


def test_resolver_point_mapping() -> None:
    resolver = CanonicalPointTargetResolver(device_width=720, device_height=1280)
    # sync path via resolve_android
    import asyncio

    async def _run() -> None:
        target = resolver.resolve_android(
            SemanticTargetResolutionRequest(
                task_id="t",
                session_id="s",
                source_capture_id="c",
                action_type=InteractionOperation.TAP,
                semantic_target="关闭",
                parameters={
                    "point": {"x": 540, "y": 960, "space": "canonical_px"},
                    "display": {"width": 1080, "height": 1920},
                },
            )
        )
        assert (target.tap_x, target.tap_y) == (360, 640)

        # Explorer CLICK + resolved_target 必须可点
        from services.api.app.application.xingyu_runtime.action_normalize import (
            normalize_action_type,
            normalize_execute_parameters,
        )

        op = InteractionOperation(normalize_action_type(InteractionOperation.CLICK.value))
        click_target = resolver.resolve_android(
            SemanticTargetResolutionRequest(
                task_id="t",
                session_id="s",
                source_capture_id="c",
                action_type=op,
                semantic_target="跳过",
                parameters=normalize_execute_parameters(
                    {"resolved_target": {"x": 100, "y": 200}}
                ),
            )
        )
        assert (click_target.tap_x, click_target.tap_y) == (100, 200)
        assert click_target.operation is InteractionOperation.TAP

    asyncio.run(_run())


def test_resolver_prefers_exact_content_desc_and_bounds_center() -> None:
    """系统弹窗按钮必须使用当前 UI Tree 的 content-desc 与 bounds。"""

    observation = parse_android_ui_hierarchy(
        """
        <hierarchy>
          <node class="android.widget.Button" content-desc="下次开启"
                clickable="true" enabled="true" visible-to-user="true"
                bounds="[174,1121][510,1250]" />
          <node class="android.widget.Button" content-desc="去开启"
                clickable="true" enabled="true" visible-to-user="true"
                bounds="[570,1121][906,1250]" />
        </hierarchy>
        """,
        width=1080,
        height=1920,
    )
    resolver = CanonicalPointTargetResolver(device_width=1080, device_height=1920)
    target = resolver.resolve_android(
        SemanticTargetResolutionRequest(
            task_id="task-1",
            session_id="session-1",
            source_capture_id="capture-1",
            action_type=InteractionOperation.CLICK,
            semantic_target="下次开启",
            parameters={"resolved_target": {"x": 540, "y": 960}},
        ),
        runtime_observation=observation,
    )

    assert (target.tap_x, target.tap_y) == (342, 1185)
    assert target.resolution_method == "uiautomator.content_desc"
    assert target.resolved_bounds is not None
    assert target.resolved_bounds.left == 174
    assert target.resolved_bounds.bottom == 1250


def test_resolver_maps_system_dialog_safe_alias_without_authorization_fallback() -> None:
    """系统弹窗别名只能命中安全拒绝按钮，不能被授权按钮抢占。"""

    observation = parse_android_ui_hierarchy(
        """
        <hierarchy>
          <node class="android.widget.Button" content-desc="下次开启"
                clickable="true" enabled="true" visible-to-user="true"
                bounds="[174,1121][510,1250]" />
          <node class="android.widget.Button" content-desc="去开启"
                clickable="true" enabled="true" visible-to-user="true"
                bounds="[570,1121][906,1250]" />
          <node class="android.widget.Button" content-desc="立即开启"
                clickable="true" enabled="true" visible-to-user="true"
                bounds="[174,1300][510,1429]" />
          <node class="android.widget.Button" content-desc="允许"
                clickable="true" enabled="true" visible-to-user="true"
                bounds="[570,1300][906,1429]" />
          <node class="android.widget.Button" content-desc="授权"
                clickable="true" enabled="true" visible-to-user="true"
                bounds="[174,1450][510,1579]" />
        </hierarchy>
        """,
        width=1080,
        height=1920,
    )
    resolver = CanonicalPointTargetResolver(device_width=1080, device_height=1920)

    target = resolver.resolve_android(
        SemanticTargetResolutionRequest(
            task_id="task-1",
            session_id="session-1",
            source_capture_id="capture-1",
            action_type=InteractionOperation.CLICK,
            semantic_target="稍后开启文件权限",
            parameters={
                "surface_phase_key": "system_dialog",
                "resolved_target": {"x": 800, "y": 1185},
            },
        ),
        runtime_observation=observation,
    )

    assert (target.tap_x, target.tap_y) == (342, 1185)
    assert target.resolution_method == "uiautomator.safe_dialog_alias"
    assert target.resolved_bounds is not None
    assert target.resolved_bounds.left == 174
    assert target.resolved_bounds.right == 510


def test_resolver_prefers_unique_popup_confirm_control_over_visual_point() -> None:
    observation = parse_android_ui_hierarchy(
        """
        <hierarchy>
          <node class="android.widget.Button" content-desc="确认"
                clickable="true" enabled="true" visible-to-user="true"
                bounds="[265,1370][815,1506]" />
        </hierarchy>
        """,
        width=1080,
        height=1920,
    )
    resolver = CanonicalPointTargetResolver(device_width=1080, device_height=1920)

    target = resolver.resolve_android(
        SemanticTargetResolutionRequest(
            task_id="task-1",
            session_id="session-1",
            source_capture_id="capture-1",
            action_type=InteractionOperation.CLICK,
            semantic_target="确认关闭公告",
            parameters={
                "surface_phase_key": "popup_chain",
                "decision_status": "ready_to_close",
                "reason_code": "close_control_ready",
                "point": {"x": 540, "y": 1258, "space": "device_px"},
            },
        ),
        runtime_observation=observation,
    )

    assert (target.tap_x, target.tap_y) == (540, 1438)
    assert target.resolution_method == "uiautomator.popup_close_alias"


def test_resolver_ignores_full_screen_dismiss_scrim_and_keeps_visual_point() -> None:
    """整屏 Dismiss 蒙层不得当关闭控件：取中心会点在广告宫格上。"""

    observation = parse_android_ui_hierarchy(
        """
        <hierarchy>
          <node class="android.widget.ImageView" content-desc="免费看片"
                clickable="true" enabled="true" visible-to-user="true"
                bounds="[84,887][285,1162]" />
          <node class="android.view.View" content-desc="Dismiss"
                clickable="true" enabled="true" visible-to-user="true"
                bounds="[0,0][1080,1920]" />
        </hierarchy>
        """,
        width=1080,
        height=1920,
    )
    resolver = CanonicalPointTargetResolver(device_width=1080, device_height=1920)

    target = resolver.resolve_android(
        SemanticTargetResolutionRequest(
            task_id="task-1",
            session_id="session-1",
            source_capture_id="capture-1",
            action_type=InteractionOperation.CLICK,
            semantic_target="关闭弹层",
            parameters={
                "surface_phase_key": "popup_chain",
                "decision_status": "ready_to_close",
                "reason_code": "close_control_ready",
                "point": {"x": 540, "y": 1703, "space": "device_px"},
            },
        ),
        runtime_observation=observation,
    )

    assert (target.tap_x, target.tap_y) == (540, 1703)
    assert target.resolution_method != "uiautomator.popup_close_alias"


def test_resolver_ignores_full_screen_exact_match_node() -> None:
    """精确同名节点若铺满整屏，同样禁止取中心，须回退视觉点。"""

    observation = parse_android_ui_hierarchy(
        """
        <hierarchy>
          <node class="android.view.View" content-desc="跳过"
                clickable="true" enabled="true" visible-to-user="true"
                bounds="[0,0][1080,1920]" />
        </hierarchy>
        """,
        width=1080,
        height=1920,
    )
    resolver = CanonicalPointTargetResolver(device_width=1080, device_height=1920)

    target = resolver.resolve_android(
        SemanticTargetResolutionRequest(
            task_id="task-1",
            session_id="session-1",
            source_capture_id="capture-1",
            action_type=InteractionOperation.CLICK,
            semantic_target="跳过",
            parameters={"point": {"x": 960, "y": 120, "space": "device_px"}},
        ),
        runtime_observation=observation,
    )

    assert (target.tap_x, target.tap_y) == (960, 120)
    assert target.resolution_method == "parameters.point"


def test_resolver_prefers_small_close_control_over_full_screen_scrim() -> None:
    """同帧同时存在整屏蒙层与真实关闭按钮时，必须落在真实按钮上。"""

    observation = parse_android_ui_hierarchy(
        """
        <hierarchy>
          <node class="android.view.View" content-desc="Dismiss"
                clickable="true" enabled="true" visible-to-user="true"
                bounds="[0,0][1080,1920]" />
          <node class="android.widget.ImageView" content-desc="关闭"
                clickable="true" enabled="true" visible-to-user="true"
                bounds="[492,1656][588,1751]" />
        </hierarchy>
        """,
        width=1080,
        height=1920,
    )
    resolver = CanonicalPointTargetResolver(device_width=1080, device_height=1920)

    target = resolver.resolve_android(
        SemanticTargetResolutionRequest(
            task_id="task-1",
            session_id="session-1",
            source_capture_id="capture-1",
            action_type=InteractionOperation.CLICK,
            semantic_target="关闭弹层",
            parameters={
                "surface_phase_key": "popup_chain",
                "decision_status": "ready_to_close",
                "reason_code": "close_control_ready",
                "point": {"x": 100, "y": 100, "space": "device_px"},
            },
        ),
        runtime_observation=observation,
    )

    assert (target.tap_x, target.tap_y) == (540, 1703)
    assert target.resolution_method == "uiautomator.popup_close_alias"


def test_resolver_rejects_system_dialog_safe_alias_without_safe_button() -> None:
    observation = parse_android_ui_hierarchy(
        """
        <hierarchy>
          <node class="android.widget.Button" content-desc="去开启"
                clickable="true" enabled="true" visible-to-user="true"
                bounds="[570,1121][906,1250]" />
          <node class="android.widget.Button" content-desc="允许"
                clickable="true" enabled="true" visible-to-user="true"
                bounds="[174,1300][510,1429]" />
        </hierarchy>
        """,
        width=1080,
        height=1920,
    )
    resolver = CanonicalPointTargetResolver(device_width=1080, device_height=1920)

    with pytest.raises(
        SemanticTargetResolutionError,
        match="缺少唯一安全拒绝按钮",
    ) as error:
        resolver.resolve_android(
            SemanticTargetResolutionRequest(
                task_id="task-1",
                session_id="session-1",
                source_capture_id="capture-1",
                action_type=InteractionOperation.CLICK,
                semantic_target="稍后开启文件权限",
                parameters={
                    "surface_phase_key": "system_dialog",
                    "resolved_target": {"x": 800, "y": 1185},
                },
            ),
            runtime_observation=observation,
        )

    assert error.value.error_code == "ANDROID_TARGET_NOT_FOUND"


def test_resolver_rejects_safe_alias_with_unnegated_grant_in_later_clause() -> None:
    """前一分句的安全词不能掩盖后一分句中的授权意图。"""

    observation = parse_android_ui_hierarchy(
        """
        <hierarchy>
          <node class="android.widget.Button" content-desc="下次开启"
                clickable="true" enabled="true" visible-to-user="true"
                bounds="[174,1121][510,1250]" />
        </hierarchy>
        """,
        width=1080,
        height=1920,
    )
    resolver = CanonicalPointTargetResolver(device_width=1080, device_height=1920)

    target = resolver.resolve_android(
        SemanticTargetResolutionRequest(
            task_id="task-1",
            session_id="session-1",
            source_capture_id="capture-1",
            action_type=InteractionOperation.CLICK,
            semantic_target="暂不执行无关动作，然后允许文件权限",
            parameters={
                "surface_phase_key": "system_dialog",
                "point": {"x": 800, "y": 1185, "space": "device_px"},
            },
        ),
        runtime_observation=observation,
    )

    assert (target.tap_x, target.tap_y) == (800, 1185)
    assert target.resolution_method == "parameters.point"


def test_resolver_rejects_ambiguous_system_dialog_safe_alias() -> None:
    observation = parse_android_ui_hierarchy(
        """
        <hierarchy>
          <node class="android.widget.Button" content-desc="下次开启"
                clickable="true" enabled="true" visible-to-user="true"
                bounds="[100,100][300,200]" />
          <node class="android.widget.Button" content-desc="下次开启"
                clickable="true" enabled="true" visible-to-user="true"
                bounds="[400,100][600,200]" />
        </hierarchy>
        """,
        width=1080,
        height=1920,
    )
    resolver = CanonicalPointTargetResolver(device_width=1080, device_height=1920)

    with pytest.raises(
        SemanticTargetResolutionError,
        match="多个安全语义按钮",
    ):
        resolver.resolve_android(
            SemanticTargetResolutionRequest(
                task_id="task-1",
                session_id="session-1",
                source_capture_id="capture-1",
                action_type=InteractionOperation.CLICK,
                semantic_target="稍后开启文件权限",
                parameters={
                    "surface_phase_key": "system_dialog",
                    "resolved_target": {"x": 200, "y": 150},
                },
            ),
            runtime_observation=observation,
        )


def test_resolver_does_not_apply_system_dialog_alias_on_ordinary_page() -> None:
    observation = parse_android_ui_hierarchy(
        """
        <hierarchy>
          <node class="android.widget.Button" content-desc="下次开启"
                clickable="true" enabled="true" visible-to-user="true"
                bounds="[174,1121][510,1250]" />
        </hierarchy>
        """,
        width=1080,
        height=1920,
    )
    resolver = CanonicalPointTargetResolver(device_width=1080, device_height=1920)

    target = resolver.resolve_android(
        SemanticTargetResolutionRequest(
            task_id="task-1",
            session_id="session-1",
            source_capture_id="capture-1",
            action_type=InteractionOperation.CLICK,
            semantic_target="稍后开启文件权限",
            parameters={
                "point": {"x": 800, "y": 1185, "space": "device_px"},
            },
        ),
        runtime_observation=observation,
    )

    assert (target.tap_x, target.tap_y) == (800, 1185)
    assert target.resolution_method == "parameters.point"


def test_resolver_rejects_ambiguous_exact_semantic_target() -> None:
    observation = parse_android_ui_hierarchy(
        """
        <hierarchy>
          <node class="android.widget.Button" content-desc="稍后"
                clickable="true" enabled="true" visible-to-user="true"
                bounds="[100,100][300,200]" />
          <node class="android.widget.Button" text="稍后"
                clickable="true" enabled="true" visible-to-user="true"
                bounds="[400,100][600,200]" />
        </hierarchy>
        """,
        width=1080,
        height=1920,
    )
    resolver = CanonicalPointTargetResolver(device_width=1080, device_height=1920)

    with pytest.raises(
        SemanticTargetResolutionError,
        match="多个精确可点击节点",
    ):
        resolver.resolve_android(
            SemanticTargetResolutionRequest(
                task_id="task-1",
                session_id="session-1",
                source_capture_id="capture-1",
                action_type=InteractionOperation.CLICK,
                semantic_target="稍后",
                parameters={"resolved_target": {"x": 200, "y": 150}},
            ),
            runtime_observation=observation,
        )


def test_resolver_prefers_quoted_uia_label_over_vision_point() -> None:
    """非底栏：Vision 长意图里的「短标签」须命中同帧唯一 UIA，覆盖 capture_px。"""

    observation = parse_android_ui_hierarchy(
        """
        <hierarchy>
          <node class="android.widget.TextView" text="动态"
                clickable="true" enabled="true" visible-to-user="true"
                bounds="[100,400][400,520]" />
          <node class="android.widget.TextView" text="消息"
                clickable="true" enabled="true" visible-to-user="true"
                bounds="[500,400][800,520]" />
        </hierarchy>
        """,
        width=1080,
        height=1920,
    )
    resolver = CanonicalPointTargetResolver(device_width=1080, device_height=1920)
    target = resolver.resolve_android(
        SemanticTargetResolutionRequest(
            task_id="task-1",
            session_id="session-1",
            source_capture_id="capture-1",
            action_type=InteractionOperation.CLICK,
            semantic_target="点击「动态」进入详情",
            parameters={"resolved_target": {"x": 900, "y": 1600}},
        ),
        runtime_observation=observation,
    )

    assert (target.tap_x, target.tap_y) == (250, 460)
    assert target.resolution_method == "uiautomator.text"
    assert target.resolved_bounds is not None
    assert target.resolved_bounds.left == 100


def test_resolver_prefers_unique_contained_uia_label_over_vision_point() -> None:
    """非底栏：无引号时，唯一被意图包含的短 UIA 标签仍优先于 Vision 点。"""

    observation = parse_android_ui_hierarchy(
        """
        <hierarchy>
          <node class="android.widget.TextView" text="热门话题"
                clickable="true" enabled="true" visible-to-user="true"
                bounds="[80,300][520,420]" />
          <node class="android.widget.TextView" text="设置"
                clickable="true" enabled="true" visible-to-user="true"
                bounds="[600,300][900,420]" />
        </hierarchy>
        """,
        width=1080,
        height=1920,
    )
    resolver = CanonicalPointTargetResolver(device_width=1080, device_height=1920)
    target = resolver.resolve_android(
        SemanticTargetResolutionRequest(
            task_id="task-1",
            session_id="session-1",
            source_capture_id="capture-1",
            action_type=InteractionOperation.CLICK,
            semantic_target="点击热门话题查看更多",
            parameters={"resolved_target": {"x": 10, "y": 10}},
        ),
        runtime_observation=observation,
    )

    assert (target.tap_x, target.tap_y) == (300, 360)
    assert target.resolution_method == "uiautomator.semantic_unique"


def test_resolver_rejects_ambiguous_contained_uia_labels() -> None:
    """多个短标签都被意图包含时 fail-closed，禁止猜点。"""

    observation = parse_android_ui_hierarchy(
        """
        <hierarchy>
          <node class="android.widget.TextView" text="动态"
                clickable="true" enabled="true" visible-to-user="true"
                bounds="[100,400][300,500]" />
          <node class="android.widget.TextView" text="详情"
                clickable="true" enabled="true" visible-to-user="true"
                bounds="[400,400][600,500]" />
        </hierarchy>
        """,
        width=1080,
        height=1920,
    )
    resolver = CanonicalPointTargetResolver(device_width=1080, device_height=1920)

    with pytest.raises(
        SemanticTargetResolutionError,
        match="多个可点击节点",
    ):
        resolver.resolve_android(
            SemanticTargetResolutionRequest(
                task_id="task-1",
                session_id="session-1",
                source_capture_id="capture-1",
                action_type=InteractionOperation.CLICK,
                semantic_target="点击动态详情",
                parameters={"resolved_target": {"x": 200, "y": 450}},
            ),
            runtime_observation=observation,
        )


@pytest.mark.asyncio
async def test_input_optional_focus_tap_then_type_text() -> None:
    ops = _FakeOps()
    driver = AndroidAdbSurfaceDriver(backend="fake", ops=ops)
    session = await driver.start_session(
        SurfaceSessionSpec(
            task_id="task-1",
            session_id="session-1",
            surface_kind=SurfaceKind.ANDROID,
        )
    )
    raw = await driver.execute(
        session=session,
        intent=_intent(
            op=InteractionOperation.INPUT,
            semantic_target="搜索框",
            parameters={
                "text": "hello world",
                "point": {"x": 540, "y": 200, "space": "canonical_px"},
                "display": {"width": 1080, "height": 1920},
            },
        ),
        action_execution_id="ae-input",
    )
    assert raw.succeeded is True
    assert any(c[0] == "shell" and c[1][:2] == ("input", "tap") for c in ops.calls)
    assert any(
        c[0] == "shell" and c[1][:2] == ("input", "text") and "hello%sworld" in c[1]
        for c in ops.calls
    )


@pytest.mark.asyncio
async def test_android_adapter_implements_common_surface_driver_contract(
    tmp_path: Path,
) -> None:
    """對齊 Web PlaywrightSurfaceDriverAdapter 的 SurfaceDriver Protocol 合同。"""
    from services.api.app.domain.interaction import SurfaceDriver
    from services.api.app.infrastructure.device.android_surface_driver import (
        AppSurfaceDriverAdapter,
    )

    ops = _FakeOps()
    adapter: SurfaceDriver = AppSurfaceDriverAdapter(
        backend="fake", ops=ops, capture_dir=tmp_path
    )
    session = await adapter.start_session(
        SurfaceSessionSpec(
            task_id="task-1",
            session_id="session-1",
            surface_kind=SurfaceKind.ANDROID,
        )
    )
    manifest = adapter.describe_capabilities()
    assert manifest.surface_kind is SurfaceKind.ANDROID
    await adapter.observe(session=session, profile=_profile())
    await adapter.capture(session=session, profile=_profile())
    raw = await adapter.execute(
        session=session,
        intent=_intent(
            op=InteractionOperation.TAP,
            parameters={
                "point": {"x": 10, "y": 10, "space": "device_px"},
            },
        ),
        action_execution_id="ae-contract",
    )
    assert raw.succeeded is True
    await adapter.close_session(session=session, reason="test_done")


def test_map_point_normalized() -> None:
    assert map_point(0.5, 0.5, device_w=100, device_h=200, space="normalized_01") == (
        50,
        100,
    )


def test_map_point_capture_px_scales_to_device() -> None:
    """截圖預覽縮圖座標必須按 base 等比映射到裝置，不可當 device_px 盲點。"""
    assert map_point(
        283,
        34,
        device_w=1080,
        device_h=1920,
        base_w=322,
        base_h=572,
        space="capture_px",
    ) == (949, 114)


def test_map_point_same_aspect_unchanged_no_letterbox() -> None:
    """寬高比一致時行為保持純等比，且審計標記 letterboxed=False。"""
    from services.api.app.infrastructure.device.adb_ops import map_point_to_device

    mapped = map_point_to_device(
        540,
        960,
        device_w=1080,
        device_h=1920,
        base_w=1080,
        base_h=1920,
        space="capture_px",
    )
    assert (mapped.x, mapped.y) == (540, 960)
    assert mapped.letterboxed is False
    assert mapped.rotation_unproven is True


def test_map_point_letterbox_maps_content_center() -> None:
    """雲手機上下黑邊：內容區中心應映到設備中心。"""
    from services.api.app.infrastructure.device.adb_ops import (
        CoordinateMappingError,
        map_point_to_device,
    )

    # 截图 1080x2400，设备 1080x1920 → 上下各约 240px 黑边
    device_w, device_h = 1080, 1920
    base_w, base_h = 1080, 2400
    content_h = base_w * device_h / device_w  # 1920
    top = (base_h - content_h) / 2.0  # 240
    mapped = map_point_to_device(
        base_w / 2,
        top + content_h / 2,
        device_w=device_w,
        device_h=device_h,
        base_w=base_w,
        base_h=base_h,
        space="capture_px",
    )
    assert mapped.letterboxed is True
    assert abs(mapped.x - device_w // 2) <= 1
    assert abs(mapped.y - device_h // 2) <= 1

    try:
        map_point_to_device(
            10,
            10,  # 落在上黑边
            device_w=device_w,
            device_h=device_h,
            base_w=base_w,
            base_h=base_h,
            space="capture_px",
        )
        raise AssertionError("expected ANDROID_POINT_IN_LETTERBOX")
    except CoordinateMappingError as exc:
        assert exc.error_code == "ANDROID_POINT_IN_LETTERBOX"


def test_resolver_rejects_safe_area_inset_tap() -> None:
    """系统栏 inset 外落点必须拒绝，不得 silently 夹点。"""
    from services.api.app.application.interaction_runtime import (
        SemanticTargetResolutionError,
    )
    from services.api.app.infrastructure.device.adb_ops import DisplaySafeArea

    resolver = CanonicalPointTargetResolver(
        device_width=1080,
        device_height=1920,
        safe_area=DisplaySafeArea(
            left=0,
            top=80,
            right=0,
            bottom=120,
            width=1080,
            height=1720,
            source="display_bounds",
        ),
    )
    try:
        resolver.resolve_android(
            SemanticTargetResolutionRequest(
                task_id="t",
                session_id="s",
                source_capture_id="c",
                action_type=InteractionOperation.TAP,
                semantic_target="状态栏误点",
                parameters={
                    "point": {"x": 540, "y": 40, "space": "device_px"},
                },
            )
        )
        raise AssertionError("expected ANDROID_POINT_OUTSIDE_SAFE_AREA")
    except SemanticTargetResolutionError as exc:
        assert exc.error_code == "ANDROID_POINT_OUTSIDE_SAFE_AREA"


def test_resolver_scales_capture_px_resolved_target() -> None:
    from services.api.app.application.xingyu_runtime.action_normalize import (
        normalize_execute_parameters,
    )

    resolver = CanonicalPointTargetResolver(device_width=1080, device_height=1920)
    target = resolver.resolve_android(
        SemanticTargetResolutionRequest(
            task_id="t",
            session_id="s",
            source_capture_id="c",
            action_type=InteractionOperation.TAP,
            semantic_target="右上跳过开屏",
            parameters=normalize_execute_parameters(
                {
                    "resolved_target": {
                        "x": 283,
                        "y": 34,
                        "space": "capture_px",
                        "base_width": 322,
                        "base_height": 572,
                    }
                }
            ),
        )
    )
    assert (target.tap_x, target.tap_y) == (949, 114)
    assert target.mapping_audit is not None
    assert target.mapping_audit["letterboxed"] is False
    assert target.mapping_audit["rotation_unproven"] is True


def test_resolver_letterbox_capture_px_within_tolerance() -> None:
    """letterbox 截图→设备点偏差应在容差内。"""
    from services.api.app.application.xingyu_runtime.action_normalize import (
        normalize_execute_parameters,
    )

    device_w, device_h = 1440, 3040
    # 模拟云手机预览：外框更“高”，上下黑边
    base_w, base_h = 360, 800
    content_h = base_w * device_h / device_w
    top = (base_h - content_h) / 2.0
    # 设备上「进入」约 (1267, 192) → 映回内容区坐标
    content_x = 1267 / device_w * base_w
    content_y = top + 192 / device_h * content_h

    resolver = CanonicalPointTargetResolver(device_width=device_w, device_height=device_h)
    target = resolver.resolve_android(
        SemanticTargetResolutionRequest(
            task_id="t",
            session_id="s",
            source_capture_id="c",
            action_type=InteractionOperation.TAP,
            semantic_target="进入",
            parameters=normalize_execute_parameters(
                {
                    "resolved_target": {
                        "x": content_x,
                        "y": content_y,
                        "space": "capture_px",
                        "base_width": base_w,
                        "base_height": base_h,
                    }
                }
            ),
        )
    )
    assert abs((target.tap_x or 0) - 1267) <= 2
    assert abs((target.tap_y or 0) - 192) <= 2
    assert target.mapping_audit is not None
    assert target.mapping_audit["letterboxed"] is True


class _PullLatencyOps(AdbDeviceOps):
    def __init__(self, pull_ms_values: list[int]) -> None:
        self._pull_ms_values = list(pull_ms_values)
        super().__init__(
            backend="cloud_phone",
            width=720,
            height=1280,
            _log=lambda _m: None,
            _screencap_fn=self._screencap,
            _shell_fn=self._shell,
        )

    def _screencap(self, dest: Path):
        from services.api.app.infrastructure.device.adb_ops import ScreencapStageTrace

        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 120)
        pull_ms = self._pull_ms_values.pop(0) if self._pull_ms_values else 4000
        return ScreencapStageTrace(
            path_mode="remote_pull",
            remote_screencap_ms=200,
            adb_pull_ms=pull_ms,
            cleanup_ms=50,
            png_bytes=128,
        )

    def _shell(self, args: list[str], timeout: float) -> None:
        del args, timeout


@pytest.mark.asyncio
async def test_cloud_phone_capture_records_adb_session_health(tmp_path: Path) -> None:
    ops = _PullLatencyOps([4000, 4000, 4000, 4000])
    driver = AndroidAdbSurfaceDriver(
        backend="cloud_phone",
        ops=ops,
        capture_dir=tmp_path,
        cloud_phone_controller=object(),
    )
    session = await driver.start_session(
        SurfaceSessionSpec(
            task_id="task-1",
            session_id="session-1",
            surface_kind=SurfaceKind.ANDROID,
        )
    )

    artifacts = await driver.capture(session=session, profile=_profile())

    health = artifacts.metadata["capture_pipeline_latency"]["adb_session_health"]
    assert health["protocol_version"] == "app-adb-session-health.v1"
    assert health["last_recorded_pull_ms"] == 4000
    assert health["refreshed"] is False


@pytest.mark.asyncio
async def test_cloud_phone_slow_pull_triggers_proactive_refresh_next_capture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Controller:
        def ensure_ready(self) -> tuple[str, list[str]]:
            return "cloud:health", ["refreshed"]

    refresh_calls: list[str] = []
    refreshed_ops = _PullLatencyOps([3000])

    def _fake_refresh(
        *, controller, log=None, pull_timeout_seconds=None, ready_timeout_seconds=25.0
    ):
        del log, pull_timeout_seconds, ready_timeout_seconds
        refresh_calls.append("refresh")
        return controller, "cloud:health", refreshed_ops, ["ok"]

    monkeypatch.setattr(
        "services.api.app.infrastructure.device.android_surface_driver.recover_cloud_phone_adb_binding",
        _fake_refresh,
    )
    ops = _PullLatencyOps([4000, 4000, 4000, 4000, 15_000, 3000])
    driver = AndroidAdbSurfaceDriver(
        backend="cloud_phone",
        ops=ops,
        capture_dir=tmp_path,
        cloud_phone_controller=_Controller(),
    )
    session = await driver.start_session(
        SurfaceSessionSpec(
            task_id="task-1",
            session_id="session-1",
            surface_kind=SurfaceKind.ANDROID,
        )
    )
    assert driver._adb_session_health is not None
    # 会话刚建立：禁止 age 误触发；仅靠慢 pull pending 驱动下一帧刷新
    now = time.monotonic()
    driver._adb_session_health.last_refresh_at = now
    driver._adb_session_health.session_started_at = now

    for _ in range(5):
        await driver.capture(session=session, profile=_profile())

    assert driver._adb_session_health.pending_refresh_reason == "slow_pull"
    assert refresh_calls == []

    # 越过冷却：模拟距上次刷新已超过 60s
    driver._adb_session_health.last_refresh_at = time.monotonic() - 70.0
    sixth = await driver.capture(session=session, profile=_profile())

    assert refresh_calls == ["refresh"]
    health = sixth.metadata["capture_pipeline_latency"]["adb_session_health"]
    assert health["refreshed"] is True
    assert health["refresh_count"] == 1
    assert driver.ops is refreshed_ops


@pytest.mark.asyncio
async def test_cloud_phone_capture_retries_after_screencap_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Controller:
        def ensure_ready(self) -> tuple[str, list[str]]:
            return "cloud:retry", ["refreshed"]

    class _FlakyOps(_PullLatencyOps):
        def __init__(self) -> None:
            super().__init__([500])
            self.fail_left = 1

        def screencap(self, dest: Path) -> None:  # type: ignore[override]
            if self.fail_left > 0:
                self.fail_left -= 1
                raise RuntimeError("公网 ADB 502")
            super().screencap(dest)

    refresh_calls: list[str] = []
    recovered = _PullLatencyOps([400])

    def _fake_refresh(
        *,
        controller,
        log=None,
        pull_timeout_seconds=None,
        ready_timeout_seconds=25.0,
    ):
        del log, pull_timeout_seconds, ready_timeout_seconds
        refresh_calls.append("refresh")
        return controller, "cloud:retry", recovered, ["ok"]

    monkeypatch.setattr(
        "services.api.app.infrastructure.device.android_surface_driver.recover_cloud_phone_adb_binding",
        _fake_refresh,
    )
    driver = AndroidAdbSurfaceDriver(
        backend="cloud_phone",
        ops=_FlakyOps(),
        capture_dir=tmp_path,
        cloud_phone_controller=_Controller(),
    )
    session = await driver.start_session(
        SurfaceSessionSpec(
            task_id="task-1",
            session_id="session-retry",
            surface_kind=SurfaceKind.ANDROID,
        )
    )
    artifacts = await driver.capture(session=session, profile=_profile())
    assert refresh_calls == ["refresh"]
    assert driver.ops is recovered
    assert artifacts.artifacts


def test_resolver_uia_landscape_age_gate_uses_current_display_size() -> None:
    """横屏年龄门 UIA 落点须在当前 Surface 尺寸内直接采用。"""

    observation = parse_android_ui_hierarchy(
        """
        <hierarchy>
          <node class="android.widget.Button" text="我已满18岁"
                clickable="true" enabled="true" visible-to-user="true"
                bounds="[475,927][2938,1067]" />
        </hierarchy>
        """,
        width=3040,
        height=1440,
    )
    resolver = CanonicalPointTargetResolver(device_width=3040, device_height=1440)
    target = resolver.resolve_android(
        SemanticTargetResolutionRequest(
            task_id="task-1",
            session_id="session-1",
            source_capture_id="capture-1",
            action_type=InteractionOperation.CLICK,
            semantic_target="我已满18岁",
            parameters={},
        ),
        runtime_observation=observation,
    )
    assert (target.tap_x, target.tap_y) == (1706, 997)
    assert target.resolution_method == "uiautomator.text"


def test_resolver_uia_oob_falls_back_to_capture_px_for_age_gate() -> None:
    """Physical 竖屏缓存 + 横屏 UIA：不得 OOB 卡死，回退 capture_px。"""

    from services.api.app.application.xingyu_runtime.action_normalize import (
        normalize_execute_parameters,
    )

    observation = parse_android_ui_hierarchy(
        """
        <hierarchy>
          <node class="android.widget.Button" text="我已满18岁"
                clickable="true" enabled="true" visible-to-user="true"
                bounds="[475,927][2938,1067]" />
        </hierarchy>
        """,
        width=3040,
        height=1440,
    )
    resolver = CanonicalPointTargetResolver(device_width=1440, device_height=3040)
    params = normalize_execute_parameters(
        {
            "resolved_target": {
                "x": 1706,
                "y": 997,
                "space": "capture_px",
                "base_width": 3040,
                "base_height": 1440,
            }
        }
    )
    target = resolver.resolve_android(
        SemanticTargetResolutionRequest(
            task_id="task-1",
            session_id="session-1",
            source_capture_id="capture-1",
            action_type=InteractionOperation.CLICK,
            semantic_target="我已满18岁",
            parameters=params,
        ),
        runtime_observation=observation,
    )
    assert target.resolution_method == "capture_px.uia_oob_fallback"
    assert target.tap_x is not None and target.tap_y is not None
    assert 0 <= target.tap_x < 1440
    assert 0 <= target.tap_y < 3040
