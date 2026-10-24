"""Android Interaction Runtime：Driver 僅 Runtime 持有；經 Coordinator 執行動作。"""

from __future__ import annotations

from dataclasses import fields, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from services.api.app.application.capture_coordination import ExecuteInteractionRequest
from services.api.app.application.explorer_turn import ExplorerTurnExecutor
from services.api.app.application.interaction_command_validation import (
    InteractionCommandValidator,
    TrustedCommandScope,
)
from services.api.app.application.interaction_runtime import InMemoryActionEvidenceBuffer
from services.api.app.domain.exploration import (
    ExplorationBoundaries,
    ExplorationBudget,
    FrontierSnapshot,
    SideEffectPolicy,
    SurfaceObservation,
    build_exploration_work,
)
from services.api.app.domain.interaction import (
    ActionIntent,
    CaptureDirective,
    CaptureProfile,
    CommandPolicy,
    InteractionOperation,
    InteractionRiskLevel,
    RawActionResult,
    SurfaceKind,
    SurfaceSessionSpec,
)
from services.api.app.infrastructure.device.adb_ops import AdbDeviceOps
from services.api.app.infrastructure.device.android_capture_runtime import (
    AndroidInteractionExecutor,
    MobileDemoAndroidRuntime,
    _assemble_android_runtime_parts,
    build_android_action_intent,
    build_default_android_capture_runtime,
    build_mobile_demo_android_runtime,
    start_mobile_demo_android_runtime,
)
from services.api.app.infrastructure.device.android_runtime_observation import (
    AndroidBounds,
    AndroidRuntimeObservation,
    AndroidUiNode,
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


class _FailingArtifactService:
    async def persist_png(self, **_kwargs):
        raise RuntimeError("artifact persistence failed")


class _ScopeReader:
    def __init__(self, scope: TrustedCommandScope) -> None:
        self.scope = scope
        self.calls = 0

    async def read_command_scope(self, **_kwargs):
        self.calls += 1
        return self.scope


class _FakeAndroidDriver:
    session = object()

    def __init__(self, observation: AndroidRuntimeObservation) -> None:
        self.observation = observation
        self.execute_calls = 0

    async def observe_runtime_state(self):
        return self.observation

    async def execute(self, **_kwargs):
        self.execute_calls += 1
        return RawActionResult(
            action_execution_id="action-1",
            succeeded=True,
            retryable=False,
            payload={},
        )


def _android_click_intent(*, risk_level: InteractionRiskLevel) -> ActionIntent:
    return ActionIntent(
        task_id="task-1",
        session_id="session-1",
        intent_id="intent-1",
        idempotency_key="idem-1",
        surface_kind=SurfaceKind.ANDROID,
        source_capture_id="capture-1",
        action_type=InteractionOperation.CLICK,
        semantic_target="立即支付",
        purpose="点击当前按钮",
        capture_profile=CaptureProfile(
            profile_id="test",
            before=CaptureDirective(role="before_action", artifact_types=("screenshot",)),
            after=CaptureDirective(role="after_action", artifact_types=("screenshot",)),
        ),
        deadline=datetime.now(UTC) + timedelta(minutes=1),
        parameters={
            "point": {"x": 50, "y": 50, "space": "canonical_px"},
            "display": {"width": 1080, "height": 1920},
        },
        risk_level=risk_level,
    )


@pytest.mark.asyncio
async def test_android_resolved_payment_target_is_blocked_before_adb_driver() -> None:
    node = AndroidUiNode(
        node_id="pay-button",
        class_name="android.widget.Button",
        resource_id="com.example:id/pay",
        text="立即支付",
        content_desc="",
        bounds=AndroidBounds(left=0, top=0, right=100, bottom=100),
        visible_to_user=True,
        enabled=True,
        clickable=True,
        focusable=True,
    )
    observation = AndroidRuntimeObservation(
        available=True,
        source="uiautomator",
        observed_at=datetime.now(UTC),
        width=1080,
        height=1920,
        nodes=(node,),
    )
    reader = _ScopeReader(
        TrustedCommandScope(
            task_id="task-1",
            session_id="session-1",
            surface_kind=SurfaceKind.ANDROID,
            policy=CommandPolicy(
                allowed_risk_levels=(
                    InteractionRiskLevel.LOW,
                    InteractionRiskLevel.MEDIUM,
                )
            ),
            finalization_epoch=4,
        )
    )
    driver = _FakeAndroidDriver(observation)
    executor = AndroidInteractionExecutor(
        driver=driver,
        evidence_buffer=InMemoryActionEvidenceBuffer(),
        command_validator=InteractionCommandValidator(scope_reader=reader),
    )

    result = await executor.execute(
        ExecuteInteractionRequest(
            agent_run_id="run-1",
            intent=_android_click_intent(risk_level=InteractionRiskLevel.LOW),
            source_capture_id="capture-1",
            action_execution_id="action-1",
            finalization_epoch=4,
        )
    )

    assert result.status.value == "blocked"
    assert result.error_code == "COMMAND_TARGET_RISK_UNDERRATED"
    assert driver.execute_calls == 0
    assert reader.calls == 1


def test_mobile_demo_runtime_does_not_expose_public_driver_field() -> None:
    names = {f.name for f in fields(MobileDemoAndroidRuntime)}
    assert "driver" not in names
    assert "_driver" in names
    assert "session_command_gate" in names


def test_android_runtime_does_not_use_noop_evidence_exit_gate() -> None:
    source = Path(
        "services/api/app/infrastructure/device/android_capture_runtime.py"
    ).read_text(encoding="utf-8")
    assert "NoopEvidenceExitGate" not in source


@pytest.mark.asyncio
async def test_observe_goes_through_capture_writer_not_direct_driver(
    tmp_path: Path,
) -> None:
    from services.api.app.application.app_screenshot_artifacts import (
        AppScreenshotArtifactService,
    )
    from services.api.app.infrastructure.storage.local_evidence_store import (
        LocalEvidenceArtifactStore,
    )

    ops = _FakeOps()
    svc = AppScreenshotArtifactService(
        store=LocalEvidenceArtifactStore(root=tmp_path / "ev"),
        index_root=tmp_path / "idx",
    )
    runtime = build_mobile_demo_android_runtime(
        task_id="task-demo",
        session_id="session-demo",
        backend="fake",
        shots_dir=tmp_path,
        ops=ops,
        artifact_service=svc,
    )
    await start_mobile_demo_android_runtime(runtime)
    shot = tmp_path / "obs.png"
    cap_id = await runtime.observe_to_file(shot)
    assert shot.is_file()
    assert cap_id
    assert any(c[0] == "screencap" for c in ops.calls)
    assert cap_id in runtime.path_index
    assert runtime.last_artifact_ref is not None
    assert runtime.last_artifact_ref.artifact_id in runtime.last_artifact_ref.url
    first_capture_id = cap_id
    second_capture_id = await runtime.observe_to_file(tmp_path / "obs-2.png")
    assert second_capture_id != first_capture_id
    await runtime.close()
    assert runtime.session_command_gate._task_owners == {}


@pytest.mark.asyncio
async def test_observe_does_not_leave_local_evidence_when_artifact_persistence_fails(
    tmp_path: Path,
) -> None:
    runtime = build_mobile_demo_android_runtime(
        task_id="task-demo",
        session_id="session-demo",
        backend="fake",
        shots_dir=tmp_path,
        ops=_FakeOps(),
        artifact_service=_FailingArtifactService(),
    )
    await start_mobile_demo_android_runtime(runtime)

    with pytest.raises(RuntimeError, match="artifact persistence failed"):
        await runtime.observe_to_file(tmp_path / "failed.png")

    assert not (tmp_path / "failed.png").exists()
    assert runtime.path_index == {}
    assert not list((tmp_path / "_runtime_caps").glob("*.png"))
    await runtime.close()


@pytest.mark.asyncio
async def test_runtime_close_releases_session_gate_when_driver_close_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = build_mobile_demo_android_runtime(
        task_id="task-demo",
        session_id="session-demo",
        backend="fake",
        shots_dir=tmp_path,
        ops=_FakeOps(),
    )
    await start_mobile_demo_android_runtime(runtime)

    async def _fail_close(**_kwargs):
        raise RuntimeError("driver close failed")

    monkeypatch.setattr(runtime._driver, "close_session", _fail_close)
    with pytest.raises(RuntimeError, match="driver close failed"):
        await runtime.close()

    assert runtime.session_command_gate._task_owners == {}


@pytest.mark.asyncio
async def test_build_default_android_capture_runtime_holds_driver(
    tmp_path: Path,
) -> None:
    ops = _FakeOps()
    runtime = build_default_android_capture_runtime(
        backend="fake",
        shots_dir=tmp_path,
        ops=ops,
    )
    names = {f.name for f in fields(type(runtime))}
    assert "coordinator" in names
    assert "_driver" in names
    assert "driver" not in names
    await runtime.start_session(
        SurfaceSessionSpec(
            task_id="t1",
            session_id="s1",
            surface_kind=SurfaceKind.ANDROID,
        )
    )
    await runtime.close_session(task_id="t1", session_id="s1")


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ("high_risk", "clear_data"), ids=("high_risk", "clear_data"))
async def test_factory_default_policy_rejects_high_risk_and_clear_data_before_driver(
    tmp_path: Path,
    case: str,
) -> None:
    """默认工厂策略不应把未授权高风险动作送入 Android Driver。"""
    _driver, executor, _writer, _coordinator, _gate, _path_index = (
        _assemble_android_runtime_parts(
            backend="fake",
            shots_dir=tmp_path,
            ops=_FakeOps(),
        )
    )
    fake_driver = _FakeAndroidDriver(
        AndroidRuntimeObservation.unavailable(
            width=1080,
            height=1920,
            failure_reason="test observation unavailable",
        )
    )
    executor._driver = fake_driver

    if case == "high_risk":
        intent = _android_click_intent(risk_level=InteractionRiskLevel.HIGH)
        expected_error_code = "COMMAND_RISK_NOT_ALLOWED"
    else:
        intent = replace(
            _android_click_intent(risk_level=InteractionRiskLevel.LOW),
            intent_id="reset-clear-data",
            idempotency_key="idem-reset-clear-data",
            action_type=InteractionOperation.RESET_APP,
            semantic_target=None,
            purpose="清空数据",
            parameters={"reset_policy": "clear_data"},
        )
        expected_error_code = "COMMAND_RISK_UNDERRATED"

    result = await executor.execute(
        ExecuteInteractionRequest(
            agent_run_id="run-default-policy",
            intent=intent,
            source_capture_id="capture-1",
            action_execution_id=f"action-{case}",
        )
    )

    assert result.status.value == "blocked"
    assert result.error_code == expected_error_code
    assert fake_driver.execute_calls == 0


@pytest.mark.asyncio
async def test_factory_default_policy_keeps_low_risk_observe_and_click_available(
    tmp_path: Path,
) -> None:
    """默认安全策略仍允许低风险观察和普通点击进入 Driver。"""
    _driver, executor, _writer, _coordinator, _gate, _path_index = (
        _assemble_android_runtime_parts(
            backend="fake",
            shots_dir=tmp_path,
            ops=_FakeOps(),
        )
    )
    fake_driver = _FakeAndroidDriver(
        AndroidRuntimeObservation.unavailable(
            width=1080,
            height=1920,
            failure_reason="test observation unavailable",
        )
    )
    executor._driver = fake_driver
    observe_intent = replace(
        _android_click_intent(risk_level=InteractionRiskLevel.LOW),
        intent_id="observe-low",
        idempotency_key="idem-observe-low",
        action_type=InteractionOperation.OBSERVE,
        semantic_target=None,
        purpose="观察当前页面",
        parameters={},
    )
    click_intent = replace(
        _android_click_intent(risk_level=InteractionRiskLevel.LOW),
        intent_id="click-low",
        idempotency_key="idem-click-low",
        semantic_target="普通按钮",
        purpose="点击普通按钮",
    )

    observe_result = await executor.execute(
        ExecuteInteractionRequest(
            agent_run_id="run-default-policy",
            intent=observe_intent,
            source_capture_id="capture-1",
            action_execution_id="action-observe-low",
        )
    )
    click_result = await executor.execute(
        ExecuteInteractionRequest(
            agent_run_id="run-default-policy",
            intent=click_intent,
            source_capture_id="capture-1",
            action_execution_id="action-click-low",
        )
    )

    assert observe_result.status.value == "evidence_pending"
    assert click_result.status.value == "evidence_pending"
    assert fake_driver.execute_calls == 2


@pytest.mark.asyncio
async def test_explorer_turn_uses_android_coordinator_only(tmp_path: Path) -> None:
    """Explorer 只拿 CaptureCoordinator，不碰 App Driver。"""
    ops = _FakeOps()
    scope_reader = _ScopeReader(
        TrustedCommandScope(
            task_id="task-exp",
            session_id="session-exp",
            surface_kind=SurfaceKind.ANDROID,
            policy=CommandPolicy(
                allowed_risk_levels=(
                    InteractionRiskLevel.LOW,
                    InteractionRiskLevel.MEDIUM,
                    InteractionRiskLevel.HIGH,
                ),
                approved_high_risk_intent_ids=("exp-tap",),
            ),
            finalization_epoch=0,
        )
    )
    android_rt = build_mobile_demo_android_runtime(
        task_id="task-exp",
        session_id="session-exp",
        backend="fake",
        shots_dir=tmp_path,
        ops=ops,
        command_scope_reader=scope_reader,
    )
    await start_mobile_demo_android_runtime(android_rt)
    await android_rt.observe_to_file(tmp_path / "seed.png")

    observed_at = datetime.now(UTC)
    frontier = FrontierSnapshot(
        snapshot_id="frontier-android-1",
        observation=SurfaceObservation(
            capture_id=android_rt.last_capture_id,
            surface_kind=SurfaceKind.ANDROID,
            location="android://demo",
            state_version="state-1",
            observed_at=observed_at,
        ),
        candidates=(),
    )
    work = build_exploration_work(
        task_id="task-exp",
        session_id="session-exp",
        branch_id="branch-1",
        objective="关闭弹层",
        surface_kind=SurfaceKind.ANDROID,
        source_capture_id=android_rt.last_capture_id,
        frontier_snapshot_id="frontier-android-1",
        frontier=frontier,
        boundaries=ExplorationBoundaries(
            allowed_operations=(InteractionOperation.TAP,),
            allowed_hosts=("android.local",),
            allowed_route_prefixes=("/",),
            max_navigation_depth=2,
            side_effect_policy=SideEffectPolicy.READ_ONLY,
        ),
        budget=ExplorationBudget(
            max_actions=2,
            remaining_actions=2,
            max_depth=2,
            remaining_duration_ms=30_000,
        ),
        deadline=observed_at + timedelta(minutes=1),
    )
    intent = build_android_action_intent(
        runtime=android_rt,
        intent_id="exp-tap",
        action_type=InteractionOperation.TAP,
        purpose="关闭",
        semantic_target="关闭",
        parameters={
            "point": {"x": 100, "y": 100, "space": "canonical_px"},
            "display": {"width": 1080, "height": 1920},
        },
    )
    # 對齊 ExplorerWork 身份欄位
    assert intent.risk_level.value == "high"
    from dataclasses import replace

    intent = replace(
        intent,
        source_capture_id=work.source_capture_id,
        deadline=work.deadline,
    )

    class _Explorer:
        async def decide_next(self, _work):  # noqa: ANN001
            return intent

    executor = ExplorerTurnExecutor(
        explorer=_Explorer(),
        capture_coordinator=android_rt.coordinator,
    )
    result = await executor.execute(work=work, agent_run_id="run-android-1")
    assert result.coordination is not None
    assert scope_reader.calls >= 1
    assert any(c[0] == "shell" and c[1][:2] == ("input", "tap") for c in ops.calls)
    await android_rt.close()
