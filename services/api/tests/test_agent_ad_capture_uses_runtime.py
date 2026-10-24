"""agent_ad_capture 必須經 CaptureCoordinator，禁止直呼 adb ops。"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from services.api.app.application.interaction_command_validation import TrustedCommandScope
from services.api.app.domain.interaction import (
    CommandPolicy,
    InteractionOperation,
    InteractionRiskLevel,
    SurfaceKind,
)
from services.api.app.infrastructure.device.adb_ops import AdbDeviceOps
from services.api.app.infrastructure.device.android_capture_runtime import (
    build_android_action_intent,
    build_mobile_demo_android_runtime,
    start_mobile_demo_android_runtime,
)


class _FakeOps(AdbDeviceOps):
    def __init__(self) -> None:
        self.calls: list[tuple] = []
        super().__init__(
            backend="fake",
            width=1080,
            height=1920,
            _log=lambda _m: None,
            _screencap_fn=self._screencap,
            _shell_fn=self._shell,
        )

    def _screencap(self, dest: Path) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 120)
        self.calls.append(("screencap", str(dest)))

    def _shell(self, args: list[str], timeout: float) -> None:
        self.calls.append(("shell", tuple(args), timeout))


class _ScopeReader:
    def __init__(self, scope: TrustedCommandScope) -> None:
        self.read_command_scope = AsyncMock(return_value=scope)


@pytest.mark.asyncio
async def test_runtime_coordinate_tap_goes_through_executor(tmp_path: Path) -> None:
    ops = _FakeOps()
    scope_reader = _ScopeReader(
        TrustedCommandScope(
            task_id="task-demo",
            session_id="session-demo",
            surface_kind=SurfaceKind.ANDROID,
            policy=CommandPolicy(),
            finalization_epoch=0,
        )
    )
    runtime = build_mobile_demo_android_runtime(
        task_id="task-demo",
        session_id="session-demo",
        backend="fake",
        shots_dir=tmp_path,
        ops=ops,
        command_scope_reader=scope_reader,
    )
    await start_mobile_demo_android_runtime(runtime)
    # 觀察截圖建立 source
    shot = tmp_path / "obs.png"
    await runtime.observe_to_file(shot)
    assert shot.is_file()

    intent = build_android_action_intent(
        runtime=runtime,
        intent_id="i-tap-1",
        action_type=InteractionOperation.TAP,
        purpose="关闭弹窗",
        semantic_target="关闭按钮",
        parameters={
            "point": {"x": 540, "y": 100, "space": "canonical_px"},
            "display": {"width": 1080, "height": 1920},
        },
    )
    # 关闭普通弹窗是低风险点击，不应通过测试夹具恢复 HIGH 默认。
    intent = replace(intent, risk_level=InteractionRiskLevel.LOW)
    result = await runtime.coordinate_intent(intent)
    assert result.interaction.status.value == "succeeded"
    assert scope_reader.read_command_scope.await_count >= 1
    assert any(c[0] == "shell" and c[1][:2] == ("input", "tap") for c in ops.calls)
    # before + after + observe 至少三次截图
    assert sum(1 for c in ops.calls if c[0] == "screencap") >= 3
    await runtime.close()


@pytest.mark.asyncio
async def test_execute_decision_uses_coordinate_not_direct_ops(tmp_path: Path) -> None:
    mod = pytest.importorskip(
        "services.api.app.api.v1.mobile_demo_agent_ad_capture",
        reason="Mobile Demo Agent Ad Capture 已下线，正式链路改由 App Workflow 承接",
    )

    ops = _FakeOps()
    runtime = build_mobile_demo_android_runtime(
        task_id="task-demo",
        session_id="session-demo",
        backend="fake",
        shots_dir=tmp_path,
        ops=ops,
    )
    await start_mobile_demo_android_runtime(runtime)
    await runtime.observe_to_file(tmp_path / "seed.png")

    with patch.object(
        runtime,
        "coordinate_intent",
        new=AsyncMock(
            side_effect=runtime.coordinate_intent,
        ),
    ) as mocked:
        detail = await mod._execute_decision_via_runtime(
            runtime=runtime,
            decision={
                "action": "tap",
                "x": 100,
                "y": 200,
                "reason": "关闭",
                "target_hint": "X",
                "coordinate_space": "canonical_px",
            },
            step=1,
            log=lambda _m: None,
        )
        assert mocked.await_count == 1
        assert "tap=" in detail
    await runtime.close()


@pytest.mark.asyncio
async def test_execute_decision_rejects_missing_tap_coordinates(tmp_path: Path) -> None:
    mod = pytest.importorskip(
        "services.api.app.api.v1.mobile_demo_agent_ad_capture",
        reason="Mobile Demo Agent Ad Capture 已下线，正式链路改由 App Workflow 承接",
    )

    ops = _FakeOps()
    runtime = build_mobile_demo_android_runtime(
        task_id="task-demo",
        session_id="session-demo",
        backend="fake",
        shots_dir=tmp_path,
        ops=ops,
    )
    await start_mobile_demo_android_runtime(runtime)
    await runtime.observe_to_file(tmp_path / "seed.png")

    with pytest.raises(ValueError, match="坐标"):
        await mod._execute_decision_via_runtime(
            runtime=runtime,
            decision={"action": "tap", "reason": "关闭", "target_hint": "X"},
            step=1,
            log=lambda _m: None,
        )
    await runtime.close()


def test_agent_module_has_no_device_handle() -> None:
    pytest.importorskip(
        "services.api.app.api.v1.mobile_demo_agent_ad_capture",
        reason="Mobile Demo Agent Ad Capture 已下线，正式链路改由 App Workflow 承接",
    )
    src = Path(mod_path()).read_text(encoding="utf-8")
    assert "class _DeviceHandle" not in src
    assert "def _open_device" not in src
    assert "AndroidAdbSurfaceDriver" in src or "build_mobile_demo_android_runtime" in src


def mod_path() -> Path:
    import services.api.app.api.v1.mobile_demo_agent_ad_capture as m

    return Path(m.__file__)
