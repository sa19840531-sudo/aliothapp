"""对照页 M3／仓库 M4 加深：单 Owner、前台 allowlist、后态回执。"""

from __future__ import annotations

from services.api.app.application.app_bounded_exploration_milestones import (
    build_app_bounded_exploration_milestone_policy,
    milestone_authority_enabled,
)
from services.api.app.application.app_command_broker import (
    APP_ADB_EXECUTOR_OWNER_VERSION,
    APP_COMMAND_BROKER_VERSION,
    AppCommandBrokerMode,
    attach_app_adb_broker_shadow,
    authorize_app_adb_command,
    build_app_adb_executor_owner_contract,
    build_app_adb_timeout_policy,
    build_lab_m4_actions_milestone_policy,
    execute_authorized_app_adb_action,
    resolve_app_command_broker_mode,
)


def test_broker_v3_production_still_observe_only() -> None:
    policy = build_app_bounded_exploration_milestone_policy()
    assert APP_COMMAND_BROKER_VERSION.endswith(".v3")
    assert resolve_app_command_broker_mode(policy) is AppCommandBrokerMode.OBSERVE
    assert milestone_authority_enabled(policy, "M4") is False
    meta = attach_app_adb_broker_shadow({})
    shadow = meta["app_adb_command_broker_shadow"]
    assert shadow["protocol_version"] == APP_COMMAND_BROKER_VERSION
    assert shadow["mode"] == "observe"
    assert shadow["executes_actions"] is False
    assert shadow["agent_direct_adb_forbidden"] is True
    assert shadow["fallback_to_driver_allowed"] is True
    assert shadow["executor_owner"]["driver_owns_default_path"] is True
    assert shadow["executor_owner"]["broker_may_mutate"] is False
    assert shadow["timeout_policy"]["unbounded_wait_forbidden"] is True
    assert shadow["comparison_page_stage"] == "M3"


def test_executor_owner_contract_forbids_dual_writer() -> None:
    owner = build_app_adb_executor_owner_contract(
        session_id="session-1",
        serial="emulator-5554",
        active_executor="driver",
        milestone_policy=build_lab_m4_actions_milestone_policy(),
    )
    assert owner["protocol_version"] == APP_ADB_EXECUTOR_OWNER_VERSION
    assert owner["dual_executor_forbidden"] is True
    assert owner["agent_direct_adb_forbidden"] is True
    assert owner["broker_may_mutate"] is False
    assert owner["fallback_to_driver_allowed"] is True

    broker_owner = build_app_adb_executor_owner_contract(
        active_executor="broker",
        milestone_policy=build_lab_m4_actions_milestone_policy(),
    )
    assert broker_owner["broker_may_mutate"] is True
    assert broker_owner["driver_owns_default_path"] is False

    timeout = build_app_adb_timeout_policy()
    assert timeout["kill_on_timeout"] is True
    assert timeout["observe_timeout_ms"] == 15_000


def test_action_requires_broker_owner_and_foreground_allowlist() -> None:
    policy = build_lab_m4_actions_milestone_policy()
    dual = authorize_app_adb_command(
        command="tap",
        argv=["1", "2"],
        milestone_policy=policy,
        package_allowlist=("com.example.app",),
        capture_epoch="epoch-1",
        active_executor="driver",
        foreground_package="com.example.app",
    )
    assert dual["status"] == "rejected"
    assert "broker_action_requires_broker_owner" in dual["reason_codes"]
    assert "dual_executor_forbidden" in dual["reason_codes"]

    outside = authorize_app_adb_command(
        command="tap",
        argv=["1", "2"],
        milestone_policy=policy,
        package_allowlist=("com.example.app",),
        capture_epoch="epoch-1",
        active_executor="broker",
        foreground_package="com.android.chrome",
    )
    assert outside["status"] == "rejected"
    assert "foreground_outside_package_allowlist" in outside["reason_codes"]


def test_execute_records_post_state_without_implying_arrival() -> None:
    class FakeOps:
        def __init__(self) -> None:
            self.pkg = "com.example.app"

        def tap(self, x, y) -> None:
            del x, y
            self.pkg = "com.example.app"

        def current_runtime_identity(self):
            return (self.pkg, ".Home")

    receipt = execute_authorized_app_adb_action(
        ops=FakeOps(),
        command="tap",
        argv=["10", "20"],
        milestone_policy=build_lab_m4_actions_milestone_policy(),
        package_allowlist=("com.example.app",),
        capture_epoch="epoch-b",
        active_executor="broker",
    )
    assert receipt["status"] == "completed_action"
    assert "arrival_not_implied" in receipt["reason_codes"]
    assert receipt["result_summary"]["post_state"]["arrival_not_implied"] is True
    assert receipt["result_summary"]["post_state"]["package_name"] == "com.example.app"


def test_execute_flags_post_state_outside_allowlist() -> None:
    class FakeOps:
        def tap(self, x, y) -> None:
            del x, y

        def current_runtime_identity(self):
            return ("com.android.chrome", ".Main")

    receipt = execute_authorized_app_adb_action(
        ops=FakeOps(),
        command="tap",
        argv=["10", "20"],
        milestone_policy=build_lab_m4_actions_milestone_policy(),
        package_allowlist=("com.example.app",),
        capture_epoch="epoch-c",
        active_executor="broker",
        foreground_package="com.example.app",
    )
    assert receipt["status"] == "completed_action"
    assert "post_state_outside_package_allowlist" in receipt["reason_codes"]
    assert "arrival_not_implied" in receipt["reason_codes"]
