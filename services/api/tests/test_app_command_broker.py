"""App Command Broker 的动作授权与正式 Driver 回执回归。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from services.api.app.application.app_bounded_exploration_milestones import (
    build_app_bounded_exploration_milestone_policy,
    milestone_authority_enabled,
    milestone_shadow_enabled,
)
from services.api.app.application.app_command_broker import (
    APP_COMMAND_BROKER_VERSION,
    APP_COMMAND_RECEIPT_VERSION,
    AppCommandBrokerMode,
    attach_app_adb_broker_shadow,
    authorize_app_adb_command,
    build_lab_m4_actions_milestone_policy,
    collect_app_adb_shadow_observation,
    compare_app_adb_shadow_with_driver,
    driver_observation_from_analysis_facts,
    execute_app_driver_action_via_broker,
    execute_authorized_app_adb_action,
    resolve_app_command_broker_mode,
)
from services.api.app.application.capture_profiles import CaptureProfilePolicy
from services.api.app.application.interaction_command_validation import (
    InteractionCommandValidator,
)
from services.api.app.domain.interaction import (
    ActionIntent,
    InteractionOperation,
    InteractionRiskLevel,
    RawActionResult,
    SurfaceDriverError,
    SurfaceKind,
    SurfaceSessionRef,
)


def test_m4_production_default_observe_without_authority() -> None:
    policy = build_app_bounded_exploration_milestone_policy()
    assert milestone_shadow_enabled(policy, "M4") is True
    assert milestone_authority_enabled(policy, "M4") is False
    assert resolve_app_command_broker_mode(policy) is AppCommandBrokerMode.OBSERVE


def test_m4_rejects_mutating_commands_in_observe() -> None:
    receipt = authorize_app_adb_command(command="tap", argv=["540", "960"])
    assert receipt["protocol_version"] == APP_COMMAND_RECEIPT_VERSION
    assert receipt["status"] == "rejected"
    assert receipt["mutates_device"] is True
    assert "actions_mode_required" in receipt["reason_codes"]

    shell = authorize_app_adb_command(command="shell", argv=["rm", "-rf", "/"])
    assert shell["status"] == "rejected"
    assert "high_risk_command_forbidden" in shell["reason_codes"]

    ok = authorize_app_adb_command(command="foreground_identity")
    assert ok["status"] == "authorized_observe"
    assert ok["observe_only"] is True


def test_m4_lab_actions_mode_authorizes_low_risk_only() -> None:
    policy = build_lab_m4_actions_milestone_policy()
    assert resolve_app_command_broker_mode(policy) is AppCommandBrokerMode.ACTIONS

    tap = authorize_app_adb_command(
        command="tap",
        argv=["540", "960"],
        milestone_policy=policy,
        package_allowlist=("com.example.app",),
        capture_epoch="capture-epoch-1",
        serial="emulator-5554",
        active_executor="broker",
        foreground_package="com.example.app",
    )
    assert tap["status"] == "authorized_action"
    assert tap["low_risk_action"] is True
    assert tap["capture_epoch"] == "capture-epoch-1"

    missing_epoch = authorize_app_adb_command(
        command="tap",
        argv=["1", "2"],
        milestone_policy=policy,
        package_allowlist=("com.example.app",),
        active_executor="broker",
    )
    assert missing_epoch["status"] == "rejected"
    assert "capture_epoch_required_for_coordinate_action" in missing_epoch["reason_codes"]

    shell = authorize_app_adb_command(
        command="shell",
        argv=["id"],
        milestone_policy=policy,
        package_allowlist=("com.example.app",),
        active_executor="broker",
    )
    assert shell["status"] == "rejected"
    assert "high_risk_command_forbidden" in shell["reason_codes"]


def test_m4_execute_authorized_action_via_fake_ops() -> None:
    calls: list[tuple] = []

    class FakeOps:
        def tap(self, x, y):
            calls.append(("tap", x, y))

        def swipe(self, x1, y1, x2, y2, duration_ms=300):
            calls.append(("swipe", x1, y1, x2, y2, duration_ms))

        def press_back(self):
            calls.append(("back",))

    policy = build_lab_m4_actions_milestone_policy()
    receipt = execute_authorized_app_adb_action(
        ops=FakeOps(),
        command="tap",
        argv=["10", "20"],
        milestone_policy=policy,
        package_allowlist=("com.example.app",),
        capture_epoch="epoch-a",
    )
    assert receipt["status"] == "completed_action"
    assert "arrival_not_implied" in receipt["reason_codes"]
    assert calls == [("tap", 10, 20)]

    rejected = execute_authorized_app_adb_action(
        ops=FakeOps(),
        command="tap",
        argv=["10", "20"],
        package_allowlist=("com.example.app",),
        capture_epoch="epoch-a",
    )
    assert rejected["status"] == "rejected"


def test_m4_compare_driver_and_adb_observations() -> None:
    driver = driver_observation_from_analysis_facts(
        package_name="com.example.app",
        activity=".Home",
        width=1080,
        height=1920,
        surface_facts={"trusted_accessibility_controls": {"node_count": 12}},
    )
    adb = {
        **driver,
        "foreground": {"package_name": "com.android.chrome", "activity": ".Main"},
    }
    compare = compare_app_adb_shadow_with_driver(
        driver_observation=driver,
        adb_observation=adb,
    )
    assert compare["aligned"] is False
    assert "package_mismatch" in compare["diff_codes"]
    assert compare["replaces_formal_capture"] is False


def test_m4_collect_observe_via_fake_ops() -> None:
    class FakeOps:
        def current_runtime_identity(self):
            return ("com.example.app", ".Home")

        def wm_size(self):
            return (1080, 1920)

        def dump_ui_hierarchy(self):
            return '<?xml version="1.0"?><hierarchy><node /></hierarchy>'

        def shell_output(self, args, timeout=30.0):
            del timeout
            if args[:2] == ["dumpsys", "window"]:
                return "Window #1"
            return ""

    payload = collect_app_adb_shadow_observation(
        ops=FakeOps(),
        package_allowlist=("com.example.app",),
        serial="emulator-5554",
    )
    assert payload["protocol_version"] == APP_COMMAND_BROKER_VERSION
    assert payload["mutates_device"] is False
    assert payload["replaces_formal_capture"] is False
    assert payload["executes_actions"] is False
    assert payload["observation"]["foreground"]["package_name"] == "com.example.app"
    assert payload["observation"]["network_capability"]["https_lifecycle_assumed"] is False
    assert all(item["command"] != "tap" for item in payload["receipts"])


def test_m4_attach_observe_default_and_lab_actions_capability() -> None:
    meta = attach_app_adb_broker_shadow(
        {"covered_page_types": ["home"]},
        driver_observation=driver_observation_from_analysis_facts(
            package_name="com.example.app",
            width=1080,
            height=1920,
        ),
        adb_observation=driver_observation_from_analysis_facts(
            package_name="com.example.app",
            width=1080,
            height=1920,
        ),
        receipts=[authorize_app_adb_command(command="wm_size")],
    )
    shadow = meta["app_adb_command_broker_shadow"]
    assert shadow["active"] is True
    assert shadow["mode"] == "observe"
    assert shadow["executes_actions"] is False
    assert shadow["mutating_commands_forbidden"] is True
    assert shadow["replaces_formal_capture"] is False
    assert shadow["dual_executor_forbidden"] is True

    lab = attach_app_adb_broker_shadow(
        {},
        milestone_policy=build_lab_m4_actions_milestone_policy(),
    )
    lab_shadow = lab["app_adb_command_broker_shadow"]
    assert lab_shadow["mode"] == "actions"
    assert lab_shadow["executes_actions"] is True
    assert "tap" in lab_shadow["action_allowlist"]
    assert lab_shadow["high_risk_commands_forbidden"] is True
    assert lab_shadow["replaces_formal_capture"] is False

@pytest.mark.asyncio
async def test_runtime_driver_actions_must_go_through_broker_receipt() -> None:
    """SurfaceDriver 动作必须经 Broker，并返回绑定身份的回执。"""

    class _Driver:
        async def execute(self, *, session, intent, action_execution_id):
            del session, intent
            return RawActionResult(
                action_execution_id=action_execution_id,
                succeeded=True,
                error_code=None,
                retryable=False,
            )

    session = SurfaceSessionRef(
        task_id="task-1",
        session_id="session-1",
        surface_kind=SurfaceKind.ANDROID,
        primary_surface_ref="app-surface:session-1",
    )
    intent = ActionIntent(
        task_id="task-1",
        session_id="session-1",
        intent_id="intent-1",
        idempotency_key="idem-1",
        surface_kind=SurfaceKind.ANDROID,
        source_capture_id="capture-1",
        action_type=InteractionOperation.BACK,
        semantic_target=None,
        purpose="返回",
        capture_profile=CaptureProfilePolicy.for_surface(
            SurfaceKind.ANDROID,
            InteractionOperation.BACK,
        ),
        deadline=datetime.now(UTC) + timedelta(minutes=1),
        parameters={},
        risk_level=InteractionRiskLevel.LOW,
    )

    raw, receipt = await execute_app_driver_action_via_broker(
        driver=_Driver(),
        session=session,
        intent=intent,
        action_execution_id="ae-broker-1",
        agent_run_id="run-1",
        source_capture_id="capture-1",
        command_validator=InteractionCommandValidator(),
    )
    assert raw.succeeded is True
    assert receipt["status"] == "completed_runtime_driver"
    assert "runtime_sole_driver_entry" in receipt["reason_codes"]
    assert receipt["command"] == "back"
    assert receipt["task_id"] == "task-1"
    assert receipt["session_id"] == "session-1"
    assert receipt["agent_run_id"] == "run-1"
    assert receipt["action_execution_id"] == "ae-broker-1"
    assert receipt["source_capture_id"] == "capture-1"


def _runtime_session() -> SurfaceSessionRef:
    return SurfaceSessionRef(
        task_id="task-1",
        session_id="session-1",
        surface_kind=SurfaceKind.ANDROID,
        primary_surface_ref="app-surface:session-1",
    )


def _runtime_intent(*, parameters: dict[str, object] | None = None) -> ActionIntent:
    return ActionIntent(
        task_id="task-1",
        session_id="session-1",
        intent_id="intent-1",
        idempotency_key="idem-1",
        surface_kind=SurfaceKind.ANDROID,
        source_capture_id="capture-1",
        action_type=InteractionOperation.INPUT,
        semantic_target="搜索框",
        purpose="输入测试内容",
        capture_profile=CaptureProfilePolicy.for_surface(
            SurfaceKind.ANDROID,
            InteractionOperation.INPUT,
        ),
        deadline=datetime.now(UTC) + timedelta(minutes=1),
        parameters=parameters or {"text": "private-input-canary"},
        risk_level=InteractionRiskLevel.LOW,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_kind", ["returned", "surface", "unknown"])
async def test_runtime_driver_failures_return_safe_receipts(
    failure_kind: str,
) -> None:
    class _Driver:
        async def execute(self, *, session, intent, action_execution_id):
            del session, intent
            if failure_kind == "returned":
                return RawActionResult(
                    action_execution_id=action_execution_id,
                    succeeded=False,
                    error_code="DRIVER_REJECTED",
                    retryable=True,
                )
            if failure_kind == "surface":
                raise SurfaceDriverError(
                    "private-input-canary",
                    error_code="DRIVER_TIMEOUT",
                    retryable=True,
                )
            raise RuntimeError("private-input-canary")

    raw, receipt = await execute_app_driver_action_via_broker(
        driver=_Driver(),
        session=_runtime_session(),
        intent=_runtime_intent(),
        action_execution_id="ae-failure-1",
        agent_run_id="run-1",
        source_capture_id="capture-1",
        command_validator=InteractionCommandValidator(),
    )

    assert raw.succeeded is False
    expected = {
        "returned": ("DRIVER_REJECTED", "failed_runtime_driver"),
        "surface": ("DRIVER_TIMEOUT", "surface_driver_error"),
        "unknown": ("APP_DRIVER_FAILED", "driver_exception"),
    }[failure_kind]
    assert raw.error_code == expected[0]
    assert receipt["status"] == expected[1]
    assert receipt["result_summary"]["driver_invoked"] is True
    assert receipt["action_execution_id"] == "ae-failure-1"
    assert receipt["source_capture_id"] == "capture-1"
    assert receipt["result_summary"]["parameter_keys"] == ("text",)
    assert receipt["result_summary"]["parameter_sha256"]
    assert "private-input-canary" not in repr(receipt)


@pytest.mark.asyncio
async def test_runtime_driver_rejects_context_identity_drift_before_driver() -> None:
    calls = 0

    class _Driver:
        async def execute(self, **_kwargs):  # noqa: ANN003
            nonlocal calls
            calls += 1
            raise AssertionError("校验失败时不应调用 Driver")

    raw, receipt = await execute_app_driver_action_via_broker(
        driver=_Driver(),
        session=_runtime_session(),
        intent=_runtime_intent(),
        action_execution_id="ae-context-1",
        source_capture_id="capture-1",
        execution_context={
            "task_id": "task-1",
            "session_id": "session-1",
            "surface_kind": "android",
            "action_execution_id": "ae-context-1",
            "source_capture_id": "capture-stale",
        },
    )

    assert raw.error_code == "EXECUTION_CONTEXT_MISMATCH"
    assert receipt["status"] == "rejected_runtime_driver"
    assert "broker_validation_failed" in receipt["reason_codes"]
    assert calls == 0
