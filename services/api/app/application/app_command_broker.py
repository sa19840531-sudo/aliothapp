"""App ADB Command Broker（对照页 M3／仓库 M4）：observe 默认；actions 灰度需显式 authority。

正式生产默认 ``authority.M4=false`` → 仅 observe。
Lab／灰度显式打开 authority 后：
- 可授权低风险 ``tap``／``swipe``／``back``（须 Package allowlist；坐标动作须 Capture epoch）；
- 变更执行须声明 Broker 为单写 Owner；前台包须落在 allowlist；
- 动作后采集真实后态（不表示到达）；
- ``shell``／安装卸载等仍一律拒绝；
- Receipt 100% 绑定；**不**表示页面到达；**不**替换正式 Capture。

一个 Session 仍禁止双执行器并行写动作。生产路径的 SurfaceDriver 调用必须经
``execute_app_driver_action_via_broker``，保证每条正式动作都有 Broker Receipt；
底层 ADB 低风险动作仍可用 ``execute_authorized_app_adb_action``。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from enum import StrEnum
from typing import Any

from services.api.app.application.app_bounded_exploration_milestones import (
    build_app_bounded_exploration_milestone_policy,
    milestone_authority_enabled,
    milestone_shadow_enabled,
)
from services.api.app.domain.protocol import freeze_json_mapping

APP_COMMAND_BROKER_VERSION = "app-command-broker.v3"
APP_COMMAND_RECEIPT_VERSION = "app-command-receipt.v2"
APP_ADB_SHADOW_OBSERVE_VERSION = "app-adb-shadow-observe.v1"
APP_ADB_EXECUTOR_OWNER_VERSION = "app-adb-executor-owner.v1"
APP_ADB_TIMEOUT_POLICY_VERSION = "app-adb-timeout-policy.v1"

# 影子／灰度默认超时；超时须可杀停，禁止无界挂起。
APP_ADB_DEFAULT_OBSERVE_TIMEOUT_MS = 15_000
APP_ADB_DEFAULT_ACTION_TIMEOUT_MS = 5_000

APP_ADB_OBSERVE_COMMANDS: frozenset[str] = frozenset(
    {
        "foreground_identity",
        "wm_size",
        "ui_hierarchy_summary",
        "window_capability",
        "network_capability",
    }
)
# actions 灰度允许的低风险变更命令（仍须 allowlist + epoch 门禁）。
APP_ADB_LOW_RISK_ACTION_COMMANDS: frozenset[str] = frozenset(
    {
        "tap",
        "swipe",
        "back",
    }
)
# 即使 authority 打开也禁止。
APP_ADB_HIGH_RISK_FORBIDDEN_COMMANDS: frozenset[str] = frozenset(
    {
        "shell",
        "install",
        "uninstall",
        "clear_data",
        "force_stop",
        "home",
        "input_text",
    }
)
APP_ADB_MUTATING_COMMANDS: frozenset[str] = (
    APP_ADB_LOW_RISK_ACTION_COMMANDS | APP_ADB_HIGH_RISK_FORBIDDEN_COMMANDS
)


class AppCommandBrokerMode(StrEnum):
    """与 H5 CLI 对齐的分阶段模式；生产默认 observe，Lab 可开 actions。"""

    OFF = "off"
    OBSERVE = "observe"
    ACTIONS = "actions"


def build_lab_m4_actions_milestone_policy(
    overrides: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Lab／灰度显式打开 M4 actions；生产工厂不得调用本函数。"""

    raw = dict(overrides) if isinstance(overrides, Mapping) else {}
    authority = dict(raw.get("authority") or {})
    shadow = dict(raw.get("shadow") or {})
    authority["M4"] = True
    shadow["M4"] = True
    return build_app_bounded_exploration_milestone_policy(
        {**raw, "authority": authority, "shadow": shadow},
        apply_production_env=False,
    )


def build_app_adb_timeout_policy(
    *,
    observe_timeout_ms: int = APP_ADB_DEFAULT_OBSERVE_TIMEOUT_MS,
    action_timeout_ms: int = APP_ADB_DEFAULT_ACTION_TIMEOUT_MS,
) -> dict[str, Any]:
    """Broker 超时与杀停合同；禁止无界挂起。"""

    observe = max(1, int(observe_timeout_ms))
    action = max(1, int(action_timeout_ms))
    return dict(
        freeze_json_mapping(
            {
                "protocol_version": APP_ADB_TIMEOUT_POLICY_VERSION,
                "observe_timeout_ms": observe,
                "action_timeout_ms": action,
                "kill_on_timeout": True,
                "unbounded_wait_forbidden": True,
            },
            "app_adb_timeout_policy",
        )
    )


def build_app_adb_executor_owner_contract(
    *,
    session_id: str | None = None,
    serial: str | None = None,
    active_executor: str = "driver",
    broker_mode: AppCommandBrokerMode | str | None = None,
    milestone_policy: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """单写 Owner：Driver 与 Broker 不得并行变更；Agent 不得直连 ADB。"""

    mode = (
        AppCommandBrokerMode(str(broker_mode))
        if broker_mode is not None
        else resolve_app_command_broker_mode(milestone_policy)
    )
    active = str(active_executor or "none").strip().casefold() or "none"
    if active not in {"driver", "broker", "none"}:
        active = "none"
    broker_may_mutate = mode is AppCommandBrokerMode.ACTIONS and active == "broker"
    return dict(
        freeze_json_mapping(
            {
                "protocol_version": APP_ADB_EXECUTOR_OWNER_VERSION,
                "session_id": str(session_id).strip() if session_id else None,
                "serial": str(serial).strip() if serial else None,
                "active_executor": active,
                "broker_mode": str(mode),
                "single_writer": True,
                "dual_executor_forbidden": True,
                "agent_direct_adb_forbidden": True,
                "fallback_to_driver_allowed": True,
                "broker_may_mutate": broker_may_mutate,
                "driver_owns_default_path": not broker_may_mutate,
                "production_observe_default": mode is AppCommandBrokerMode.OBSERVE,
                "arrival_not_implied": True,
                "replaces_formal_capture": False,
            },
            "app_adb_executor_owner",
        )
    )


def _package_in_allowlist(package_name: str | None, allowlist: Sequence[str]) -> bool:
    pkg = str(package_name or "").strip().casefold()
    if not pkg:
        return False
    allowed = {str(item).strip().casefold() for item in allowlist if str(item).strip()}
    return pkg in allowed


def resolve_app_command_broker_mode(
    policy: Mapping[str, Any] | None = None,
) -> AppCommandBrokerMode:
    """shadow 关 → off；authority 开 → actions；否则 observe。"""

    if not milestone_shadow_enabled(policy, "M4"):
        return AppCommandBrokerMode.OFF
    if milestone_authority_enabled(policy, "M4"):
        return AppCommandBrokerMode.ACTIONS
    return AppCommandBrokerMode.OBSERVE


def build_app_command_receipt(
    *,
    command: str,
    argv: Sequence[str] | None = None,
    status: str,
    mode: AppCommandBrokerMode | str,
    reason_codes: Sequence[str] | None = None,
    package_allowlist: Sequence[str] | None = None,
    serial: str | None = None,
    capture_epoch: str | None = None,
    result_summary: Mapping[str, Any] | None = None,
    task_id: str | None = None,
    session_id: str | None = None,
    agent_run_id: str | None = None,
    intent_id: str | None = None,
    action_execution_id: str | None = None,
    source_capture_id: str | None = None,
    surface_kind: str | None = None,
    finalization_epoch: int | None = None,
    command_sequence: int | None = None,
) -> dict[str, Any]:
    """不可变命令回执；不表示页面到达。"""

    cmd = str(command or "").strip()
    payload = {
        "protocol_version": APP_COMMAND_RECEIPT_VERSION,
        "command": cmd,
        "argv": [str(item) for item in (argv or ())],
        "status": str(status or "unknown"),
        "mode": str(mode),
        "reason_codes": [str(item) for item in (reason_codes or ())],
        "package_allowlist": [str(item) for item in (package_allowlist or ())],
        "serial": str(serial).strip() if serial else None,
        "capture_epoch": str(capture_epoch).strip() if capture_epoch else None,
        "result_summary": dict(result_summary) if isinstance(result_summary, Mapping) else {},
        "mutates_device": cmd in APP_ADB_MUTATING_COMMANDS,
        "observe_only": cmd in APP_ADB_OBSERVE_COMMANDS,
        "low_risk_action": cmd in APP_ADB_LOW_RISK_ACTION_COMMANDS,
    }
    # 新增身份只在调用方提供时写入，保持既有 ADB 回执正文及哈希兼容。
    identity = {
        "task_id": task_id, "session_id": session_id, "agent_run_id": agent_run_id,
        "intent_id": intent_id, "action_execution_id": action_execution_id,
        "source_capture_id": source_capture_id, "surface_kind": surface_kind,
        "finalization_epoch": finalization_epoch, "command_sequence": command_sequence,
    }
    payload.update({key: value for key, value in identity.items() if value is not None})
    digest = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()
    payload["receipt_id"] = f"adb-receipt:{digest[:24]}"
    return dict(freeze_json_mapping(payload, "app_command_receipt"))


def _argv_are_nonnegative_ints(argv: Sequence[str], *, expected: int | tuple[int, ...]) -> bool:
    allowed = {expected} if isinstance(expected, int) else set(expected)
    if len(argv) not in allowed:
        return False
    for item in argv:
        text = str(item).strip()
        if not text.isdigit():
            return False
    return True


def _validate_action_argv(command: str, argv: Sequence[str]) -> list[str]:
    reasons: list[str] = []
    args = [str(item) for item in argv]
    if command == "tap":
        if not _argv_are_nonnegative_ints(args, expected=2):
            reasons.append("tap_requires_x_y_ints")
    elif command == "swipe":
        if not _argv_are_nonnegative_ints(args, expected=(4, 5)):
            reasons.append("swipe_requires_x1_y1_x2_y2_optional_duration")
    elif command == "back":
        if args:
            reasons.append("back_forbids_argv")
    return reasons


def authorize_app_adb_command(
    *,
    command: str,
    mode: AppCommandBrokerMode | str | None = None,
    milestone_policy: Mapping[str, Any] | None = None,
    argv: Sequence[str] | None = None,
    package_allowlist: Sequence[str] | None = None,
    serial: str | None = None,
    capture_epoch: str | None = None,
    foreground_package: str | None = None,
    active_executor: str | None = None,
) -> dict[str, Any]:
    """Broker 授权：observe 只放行观测；actions 另放行低风险变更。"""

    policy = (
        dict(milestone_policy)
        if isinstance(milestone_policy, Mapping)
        else build_app_bounded_exploration_milestone_policy()
    )
    resolved_mode = (
        AppCommandBrokerMode(str(mode))
        if mode is not None
        else resolve_app_command_broker_mode(policy)
    )
    cmd = str(command or "").strip()
    args = [str(item) for item in (argv or ())]
    allowlist = [str(item).strip() for item in (package_allowlist or ()) if str(item).strip()]
    epoch = str(capture_epoch).strip() if capture_epoch else ""
    foreground = str(foreground_package).strip() if foreground_package else ""

    def _reject(reasons: Sequence[str]) -> dict[str, Any]:
        return build_app_command_receipt(
            command=cmd or "unknown",
            argv=args,
            status="rejected",
            mode=resolved_mode,
            reason_codes=reasons,
            package_allowlist=allowlist,
            serial=serial,
            capture_epoch=epoch or None,
        )

    if resolved_mode is AppCommandBrokerMode.OFF:
        return _reject(["broker_mode_off"])
    if not cmd:
        return _reject(["empty_command"])

    if cmd in APP_ADB_HIGH_RISK_FORBIDDEN_COMMANDS:
        return _reject(["high_risk_command_forbidden", "mutating_command"])

    if cmd in APP_ADB_OBSERVE_COMMANDS:
        return build_app_command_receipt(
            command=cmd,
            argv=args,
            status="authorized_observe",
            mode=resolved_mode,
            reason_codes=["observe_allowlist"],
            package_allowlist=allowlist,
            serial=serial,
            capture_epoch=epoch or None,
        )

    if cmd in APP_ADB_LOW_RISK_ACTION_COMMANDS:
        if resolved_mode is not AppCommandBrokerMode.ACTIONS:
            return _reject(
                [
                    "mutating_or_unknown_command_forbidden_in_shadow",
                    "mutating_command",
                    "actions_mode_required",
                ]
            )
        reasons: list[str] = []
        # 变更动作必须由 Broker 声明为单写 Owner，禁止与 Driver 并行。
        owner = str(active_executor or "").strip().casefold()
        if owner != "broker":
            reasons.append("broker_action_requires_broker_owner")
            reasons.append("dual_executor_forbidden")
        if not allowlist:
            reasons.append("package_allowlist_required")
        if cmd in {"tap", "swipe"} and not epoch:
            reasons.append("capture_epoch_required_for_coordinate_action")
        if allowlist and foreground and not _package_in_allowlist(foreground, allowlist):
            reasons.append("foreground_outside_package_allowlist")
        reasons.extend(_validate_action_argv(cmd, args))
        if reasons:
            return _reject(reasons)
        return build_app_command_receipt(
            command=cmd,
            argv=args,
            status="authorized_action",
            mode=resolved_mode,
            reason_codes=["low_risk_action_allowlist", "m4_actions_grayscale"],
            package_allowlist=allowlist,
            serial=serial,
            capture_epoch=epoch,
        )

    return _reject(["unknown_command_forbidden", "not_in_observe_or_action_allowlist"])


def _read_runtime_identity(ops: Any) -> dict[str, Any] | None:
    if not hasattr(ops, "current_runtime_identity"):
        return None
    try:
        identity = ops.current_runtime_identity()
    except Exception:  # noqa: BLE001 - 后态采集失败不得掩盖动作本身
        return None
    if isinstance(identity, tuple) and identity:
        return {
            "package_name": identity[0],
            "activity": identity[1] if len(identity) > 1 else None,
            "arrival_not_implied": True,
        }
    if isinstance(identity, Mapping):
        return {
            "package_name": identity.get("package_name"),
            "activity": identity.get("activity"),
            "arrival_not_implied": True,
        }
    return None


def execute_authorized_app_adb_action(
    *,
    ops: Any,
    command: str,
    argv: Sequence[str] | None = None,
    milestone_policy: Mapping[str, Any] | None = None,
    package_allowlist: Sequence[str] | None = None,
    serial: str | None = None,
    capture_epoch: str | None = None,
    foreground_package: str | None = None,
    active_executor: str = "broker",
    collect_post_state: bool = True,
) -> dict[str, Any]:
    """仅在已授权 actions 回执后执行低风险设备动作；失败也写 Receipt。

    调用方必须保证本 Session 无第二执行器并行写动作，且 ``active_executor=broker``。
    """

    policy = (
        dict(milestone_policy)
        if isinstance(milestone_policy, Mapping)
        else build_app_bounded_exploration_milestone_policy()
    )
    mode = resolve_app_command_broker_mode(policy)
    pre_foreground = foreground_package
    if pre_foreground is None and hasattr(ops, "current_runtime_identity"):
        pre = _read_runtime_identity(ops)
        if isinstance(pre, Mapping):
            pre_foreground = (
                str(pre.get("package_name")).strip() if pre.get("package_name") else None
            )
    auth = authorize_app_adb_command(
        command=command,
        mode=mode,
        milestone_policy=policy,
        argv=argv,
        package_allowlist=package_allowlist,
        serial=serial,
        capture_epoch=capture_epoch,
        foreground_package=pre_foreground,
        active_executor=active_executor,
    )
    if auth.get("status") != "authorized_action":
        return auth

    cmd = str(auth.get("command") or "")
    args = [str(item) for item in (auth.get("argv") or ())]
    allowlist = [str(item) for item in (package_allowlist or ()) if str(item).strip()]
    try:
        if cmd == "tap":
            ops.tap(int(args[0]), int(args[1]))
            summary: dict[str, Any] = {"ok": True, "x": int(args[0]), "y": int(args[1])}
        elif cmd == "swipe":
            x1, y1, x2, y2 = (int(args[0]), int(args[1]), int(args[2]), int(args[3]))
            duration_ms = int(args[4]) if len(args) >= 5 else 300
            if hasattr(ops, "swipe"):
                ops.swipe(x1, y1, x2, y2, duration_ms=duration_ms)
            else:
                raise AttributeError("ops_missing_swipe")
            summary = {
                "ok": True,
                "x1": x1,
                "y1": y1,
                "x2": x2,
                "y2": y2,
                "duration_ms": duration_ms,
            }
        elif cmd == "back":
            if hasattr(ops, "press_back"):
                ops.press_back()
            elif hasattr(ops, "back"):
                ops.back()
            else:
                raise AttributeError("ops_missing_back")
            summary = {"ok": True}
        else:
            return build_app_command_receipt(
                command=cmd,
                argv=args,
                status="rejected",
                mode=mode,
                reason_codes=["unsupported_authorized_action"],
                package_allowlist=package_allowlist,
                serial=serial,
                capture_epoch=capture_epoch,
            )
        reason_codes = ["action_executed", "arrival_not_implied"]
        if collect_post_state:
            post_state = _read_runtime_identity(ops)
            if post_state is not None:
                summary["post_state"] = post_state
                reason_codes.append("post_state_observed")
                post_pkg = post_state.get("package_name")
                if allowlist and post_pkg and not _package_in_allowlist(str(post_pkg), allowlist):
                    reason_codes.append("post_state_outside_package_allowlist")
            else:
                reason_codes.append("post_state_unavailable")
        return build_app_command_receipt(
            command=cmd,
            argv=args,
            status="completed_action",
            mode=mode,
            reason_codes=reason_codes,
            package_allowlist=package_allowlist,
            serial=serial,
            capture_epoch=capture_epoch,
            result_summary=summary,
        )
    except Exception as exc:  # noqa: BLE001 - 执行失败必须留证
        return build_app_command_receipt(
            command=cmd,
            argv=args,
            status="failed_action",
            mode=mode,
            reason_codes=["action_failed", type(exc).__name__],
            package_allowlist=package_allowlist,
            serial=serial,
            capture_epoch=capture_epoch,
            result_summary={"ok": False, "error": str(exc)[:240]},
        )


async def execute_app_driver_action_via_broker(
    *,
    driver: Any,
    session: Any,
    intent: Any,
    action_execution_id: str,
    execution_context: Any | None = None,
    milestone_policy: Mapping[str, Any] | None = None,
    package_allowlist: Sequence[str] | None = None,
    serial: str | None = None,
    capture_epoch: str | None = None,
    foreground_package: str | None = None,
    agent_run_id: str | None = None,
    source_capture_id: str | None = None,
    command_validator: Any | None = None,
) -> tuple[Any, dict[str, Any]]:
    """兼容导出；正式 Driver 适配器位于独立模块。"""

    from services.api.app.application.app_driver_command_broker import (
        execute_app_driver_action_via_broker as _execute_app_driver_action_via_broker,
    )

    return await _execute_app_driver_action_via_broker(
        driver=driver,
        session=session,
        intent=intent,
        action_execution_id=action_execution_id,
        execution_context=execution_context,
        milestone_policy=milestone_policy,
        package_allowlist=package_allowlist,
        serial=serial,
        capture_epoch=capture_epoch,
        foreground_package=foreground_package,
        agent_run_id=agent_run_id,
        source_capture_id=source_capture_id,
        command_validator=command_validator,
    )


def build_app_adb_shadow_observation_profile(
    *,
    foreground_package: str | None = None,
    foreground_activity: str | None = None,
    display_width: int | None = None,
    display_height: int | None = None,
    ui_hierarchy_available: bool | None = None,
    ui_node_count: int | None = None,
    network_observable: bool | None = None,
    window_dump_available: bool | None = None,
    failure_reasons: Sequence[str] | None = None,
) -> dict[str, Any]:
    """ADB 影子观测能力快照；缺测记 unavailable，不伪造空闲网络。"""

    network_status: str
    if network_observable is True:
        network_status = "observable"
    elif network_observable is False:
        network_status = "not_observable"
    else:
        network_status = "unknown"
    payload = {
        "protocol_version": APP_ADB_SHADOW_OBSERVE_VERSION,
        "foreground": {
            "package_name": str(foreground_package).strip() if foreground_package else None,
            "activity": str(foreground_activity).strip() if foreground_activity else None,
        },
        "display": {
            "width": int(display_width) if display_width is not None else None,
            "height": int(display_height) if display_height is not None else None,
        },
        "ui_hierarchy": {
            "available": ui_hierarchy_available,
            "node_count": int(ui_node_count) if ui_node_count is not None else None,
        },
        "window_capability": {
            "available": window_dump_available,
        },
        "network_capability": {
            "status": network_status,
            "https_lifecycle_assumed": False,
        },
        "failure_reasons": [str(item) for item in (failure_reasons or ())],
    }
    return dict(freeze_json_mapping(payload, "app_adb_shadow_observe"))


def compare_app_adb_shadow_with_driver(
    *,
    driver_observation: Mapping[str, Any] | None,
    adb_observation: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """对比既有 Driver 观察与 ADB 影子观察；差异只审计，不改正式 Capture。"""

    driver = driver_observation if isinstance(driver_observation, Mapping) else {}
    adb = adb_observation if isinstance(adb_observation, Mapping) else {}
    diffs: list[str] = []

    def _pkg(raw: Mapping[str, Any]) -> str:
        fg = raw.get("foreground") if isinstance(raw.get("foreground"), Mapping) else raw
        return str((fg or {}).get("package_name") or raw.get("package_name") or "").strip()

    def _act(raw: Mapping[str, Any]) -> str:
        fg = raw.get("foreground") if isinstance(raw.get("foreground"), Mapping) else raw
        return str((fg or {}).get("activity") or raw.get("activity") or "").strip()

    driver_pkg, adb_pkg = _pkg(driver), _pkg(adb)
    driver_act, adb_act = _act(driver), _act(adb)
    if driver_pkg and adb_pkg and driver_pkg != adb_pkg:
        diffs.append("package_mismatch")
    if driver_act and adb_act and driver_act != adb_act:
        diffs.append("activity_mismatch")

    driver_display = driver.get("display") if isinstance(driver.get("display"), Mapping) else {}
    adb_display = adb.get("display") if isinstance(adb.get("display"), Mapping) else {}
    for key in ("width", "height"):
        left, right = driver_display.get(key), adb_display.get(key)
        if left is not None and right is not None and int(left) != int(right):
            diffs.append(f"display_{key}_mismatch")

    driver_ui = driver.get("ui_hierarchy") if isinstance(driver.get("ui_hierarchy"), Mapping) else {}
    adb_ui = adb.get("ui_hierarchy") if isinstance(adb.get("ui_hierarchy"), Mapping) else {}
    if driver_ui.get("available") is True and adb_ui.get("available") is False:
        diffs.append("ui_hierarchy_driver_ok_adb_unavailable")
    if driver_ui.get("available") is False and adb_ui.get("available") is True:
        diffs.append("ui_hierarchy_adb_ok_driver_unavailable")

    return {
        "protocol_version": "app-adb-driver-shadow-compare.v1",
        "aligned": not diffs,
        "diff_codes": diffs,
        "replaces_formal_capture": False,
        "driver_present": bool(driver),
        "adb_present": bool(adb),
    }


def collect_app_adb_shadow_observation(
    *,
    ops: Any,
    package_allowlist: Sequence[str] | None = None,
    serial: str | None = None,
    milestone_policy: Mapping[str, Any] | None = None,
    include_ui_hierarchy: bool = True,
) -> dict[str, Any]:
    """在已授权 observe／actions 模式下经 Broker 收集 ADB 观测（需注入 ops）。

    本函数仍不执行 tap／swipe／back；网络能力默认记 unknown／not assumed。
    """

    policy = (
        dict(milestone_policy)
        if isinstance(milestone_policy, Mapping)
        else build_app_bounded_exploration_milestone_policy()
    )
    mode = resolve_app_command_broker_mode(policy)
    receipts: list[dict[str, Any]] = []
    failures: list[str] = []

    def _run(command: str, runner: Callable[[], Any]) -> Any:
        auth = authorize_app_adb_command(
            command=command,
            mode=mode,
            milestone_policy=policy,
            package_allowlist=package_allowlist,
            serial=serial,
        )
        if auth.get("status") != "authorized_observe":
            receipts.append(dict(auth))
            return None
        try:
            value = runner()
            summary = {"ok": True}
            if command == "ui_hierarchy_summary" and isinstance(value, Mapping):
                summary = {
                    "ok": True,
                    "available": value.get("available"),
                    "node_count": value.get("node_count"),
                }
            elif command == "foreground_identity" and isinstance(value, tuple):
                summary = {
                    "ok": True,
                    "package_name": value[0] if value else None,
                    "activity": value[1] if len(value) > 1 else None,
                }
            elif command == "wm_size" and isinstance(value, tuple):
                summary = {"ok": True, "width": value[0], "height": value[1]}
            receipts.append(
                build_app_command_receipt(
                    command=command,
                    status="completed_observe",
                    mode=mode,
                    reason_codes=["observe_executed"],
                    package_allowlist=package_allowlist,
                    serial=serial,
                    result_summary=summary,
                )
            )
            return value
        except Exception as exc:  # noqa: BLE001 - 影子失败必须留证
            failures.append(f"{command}:{type(exc).__name__}")
            receipts.append(
                build_app_command_receipt(
                    command=command,
                    status="failed_observe",
                    mode=mode,
                    reason_codes=["observe_failed", type(exc).__name__],
                    package_allowlist=package_allowlist,
                    serial=serial,
                )
            )
            return None

    identity = _run("foreground_identity", ops.current_runtime_identity)
    size = _run("wm_size", ops.wm_size)
    ui_summary: dict[str, Any] | None = None
    if include_ui_hierarchy:

        def _ui() -> dict[str, Any]:
            raw = ops.dump_ui_hierarchy()
            text = str(raw or "")
            available = "<hierarchy" in text or text.startswith("<?xml")
            node_count = text.count("<node") if available else 0
            return {"available": available, "node_count": node_count, "bytes": len(text.encode("utf-8"))}

        ui_summary = _run("ui_hierarchy_summary", _ui)

    window_available = None

    def _window() -> bool:
        if hasattr(ops, "shell_output"):
            text = str(ops.shell_output(["dumpsys", "window", "windows"], timeout=15.0) or "")
            return bool(text.strip())
        return False

    window_available = _run("window_capability", _window)

    network_receipt = authorize_app_adb_command(
        command="network_capability",
        mode=mode,
        milestone_policy=policy,
        package_allowlist=package_allowlist,
        serial=serial,
    )
    if network_receipt.get("status") == "authorized_observe":
        receipts.append(
            build_app_command_receipt(
                command="network_capability",
                status="completed_observe",
                mode=mode,
                reason_codes=["network_marked_unknown_no_https_assumption"],
                package_allowlist=package_allowlist,
                serial=serial,
                result_summary={"status": "unknown", "https_lifecycle_assumed": False},
            )
        )

    fg_pkg = identity[0] if isinstance(identity, tuple) and identity else None
    fg_act = identity[1] if isinstance(identity, tuple) and len(identity) > 1 else None
    width = size[0] if isinstance(size, tuple) and size else None
    height = size[1] if isinstance(size, tuple) and len(size) > 1 else None
    profile = build_app_adb_shadow_observation_profile(
        foreground_package=fg_pkg,
        foreground_activity=fg_act,
        display_width=width,
        display_height=height,
        ui_hierarchy_available=(
            None if ui_summary is None else bool(ui_summary.get("available"))
        ),
        ui_node_count=(
            None
            if ui_summary is None or ui_summary.get("node_count") is None
            else int(ui_summary["node_count"])
        ),
        network_observable=None,
        window_dump_available=window_available if isinstance(window_available, bool) else None,
        failure_reasons=failures,
    )
    return {
        "protocol_version": APP_COMMAND_BROKER_VERSION,
        "mode": str(mode),
        "observation": profile,
        "receipts": receipts,
        "mutates_device": False,
        "replaces_formal_capture": False,
        "executes_actions": mode is AppCommandBrokerMode.ACTIONS,
    }


def attach_app_adb_broker_shadow(
    coverage_metadata: Mapping[str, Any] | None,
    *,
    driver_observation: Mapping[str, Any] | None = None,
    adb_observation: Mapping[str, Any] | None = None,
    receipts: Sequence[Mapping[str, Any]] | None = None,
    milestone_policy: Mapping[str, Any] | None = None,
    session_id: str | None = None,
    serial: str | None = None,
    active_executor: str = "driver",
) -> dict[str, Any]:
    """把 Broker 观测／对账／actions 能力声明写入 coverage 元数据；不改正式 Capture。"""

    policy = (
        dict(milestone_policy)
        if isinstance(milestone_policy, Mapping)
        else build_app_bounded_exploration_milestone_policy()
    )
    meta = dict(coverage_metadata) if isinstance(coverage_metadata, Mapping) else {}
    mode = resolve_app_command_broker_mode(policy)
    owner = build_app_adb_executor_owner_contract(
        session_id=session_id,
        serial=serial,
        active_executor=active_executor,
        broker_mode=mode,
        milestone_policy=policy,
    )
    timeout_policy = build_app_adb_timeout_policy()

    if mode is AppCommandBrokerMode.OFF:
        meta["app_adb_command_broker_shadow"] = {
            "protocol_version": APP_COMMAND_BROKER_VERSION,
            "active": False,
            "mode": str(mode),
            "replaces_formal_capture": False,
            "executes_actions": False,
            "executor_owner": owner,
            "timeout_policy": timeout_policy,
            "agent_direct_adb_forbidden": True,
        }
        return meta

    compare = compare_app_adb_shadow_with_driver(
        driver_observation=driver_observation,
        adb_observation=adb_observation,
    )
    actions_enabled = mode is AppCommandBrokerMode.ACTIONS
    meta["app_adb_command_broker_shadow"] = {
        "protocol_version": APP_COMMAND_BROKER_VERSION,
        "active": True,
        "mode": str(mode),
        "observe_allowlist": sorted(APP_ADB_OBSERVE_COMMANDS),
        "action_allowlist": sorted(APP_ADB_LOW_RISK_ACTION_COMMANDS) if actions_enabled else [],
        "high_risk_commands_forbidden": True,
        "mutating_commands_forbidden": not actions_enabled,
        "replaces_formal_capture": False,
        "executes_actions": actions_enabled,
        "dual_executor_forbidden": True,
        "agent_direct_adb_forbidden": True,
        "fallback_to_driver_allowed": True,
        "arrival_not_implied": True,
        "executor_owner": owner,
        "timeout_policy": timeout_policy,
        "driver_observation": dict(driver_observation)
        if isinstance(driver_observation, Mapping)
        else None,
        "adb_observation": dict(adb_observation) if isinstance(adb_observation, Mapping) else None,
        "compare": compare,
        "receipts": [dict(item) for item in (receipts or ()) if isinstance(item, Mapping)],
        "milestone": "M4",
        "comparison_page_stage": "M3",
    }
    return meta


def driver_observation_from_analysis_facts(
    *,
    package_name: str | None,
    activity: str | None = None,
    width: int | None = None,
    height: int | None = None,
    surface_facts: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """从现有分析／Capture 事实投影 Driver 侧观察摘要，供影子对账。"""

    facts = surface_facts if isinstance(surface_facts, Mapping) else {}
    controls = facts.get("trusted_accessibility_controls")
    ui_available = isinstance(controls, Mapping) and bool(controls)
    node_count = None
    if isinstance(controls, Mapping) and "node_count" in controls:
        try:
            node_count = int(controls["node_count"])
        except (TypeError, ValueError):
            node_count = None
    return build_app_adb_shadow_observation_profile(
        foreground_package=package_name,
        foreground_activity=activity,
        display_width=width,
        display_height=height,
        ui_hierarchy_available=ui_available if controls is not None else None,
        ui_node_count=node_count,
        network_observable=None,
        window_dump_available=None,
    )
