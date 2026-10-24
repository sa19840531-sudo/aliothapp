"""App 有限自主探索里程碑旗标与指标基线（M0 契约）。

正式生产路径默认不启用未完成里程碑的权威切换；影子写入可观测但不改 Coverage／Gate。

生产 authority 只能通过显式环境变量分阶段打开（见 ``PRODUCTION_AUTHORITY_ENV_KEYS``），
且 M4→M5→M6→M7 必须按序；违规时 fail-closed 关闭后级开关，禁止 silent cutover。
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any

from services.api.app.domain.protocol import freeze_json_mapping

APP_BOUNDED_EXPLORATION_MILESTONE_VERSION = "app-bounded-exploration-milestone.v2"

# 依序：M0 → M1 → M2 → M4 → M5 → M6 → M7（本路线跳过 M3）。
APP_MILESTONE_ORDER: tuple[str, ...] = (
    "M0",
    "M1",
    "M2",
    "M4",
    "M5",
    "M6",
    "M7",
)

# 生产 authority 开闸链（与 Lab 灰度顺序一致）；M1／M2 可独立，不插入此链。
PRODUCTION_AUTHORITY_UNLOCK_ORDER: tuple[str, ...] = ("M4", "M5", "M6", "M7")

PRODUCTION_AUTHORITY_ENV_KEYS: dict[str, str] = {
    "M1": "ALIOTH_APP_MILESTONE_AUTHORITY_M1",
    "M2": "ALIOTH_APP_MILESTONE_AUTHORITY_M2",
    "M4": "ALIOTH_APP_MILESTONE_AUTHORITY_M4",
    "M5": "ALIOTH_APP_MILESTONE_AUTHORITY_M5",
    "M6": "ALIOTH_APP_MILESTONE_AUTHORITY_M6",
    "M7": "ALIOTH_APP_MILESTONE_AUTHORITY_M7",
}

# 与 M7 Lab 裁决解耦：即使 M7 authority 打开，正式 Gate SQL 读图仍须单独评审此开关。
PRODUCTION_GATE_SQL_CONSUMES_GRAPH_ENV = "ALIOTH_APP_GATE_SQL_CONSUMES_GRAPH"

# 生产权威开关：未显式打开前，对应能力只能影子记录。
_DEFAULT_AUTHORITY: dict[str, bool] = {
    "M0": True,  # 契约与指标本身始终生效
    "M1": False,  # Settle Oracle 正式裁决
    "M2": False,  # 图参与调度／Coverage
    "M4": False,  # ADB Broker actions
    "M5": False,  # 图辅助调度
    "M6": False,  # 补采 runtime_ready
    "M7": False,  # Gate 图辅助裁决（仍不改 Gate SQL，除非独立 SQL 开关）
}

_DEFAULT_SHADOW: dict[str, bool] = {
    "M0": True,
    "M1": True,
    "M2": True,
    "M4": True,
    "M5": True,
    "M6": True,
    "M7": True,
}

APP_BOUNDED_EXPLORATION_METRIC_KEYS: tuple[str, ...] = (
    "verified_arrival_count",
    "stable_capture_success_count",
    "non_stable_as_business_evidence_count",
    "overlay_close_failure_terminal_count",
    "cross_epoch_coordinate_replay_count",
    "agent_direct_adb_attempt_count",
    "external_package_escape_count",
    "page_graph_coverage_reconcile_mismatch_count",
    "gate_shadow_would_block_count",
    "retirement_catalog_candidate_count",
)


def _parse_env_bool(raw: str | None) -> bool | None:
    if raw is None:
        return None
    text = str(raw).strip().casefold()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return None


def read_production_authority_env(
    environ: Mapping[str, str] | None = None,
) -> dict[str, bool]:
    """读取生产 authority 环境变量；未设置的里程碑不出现在结果中。"""

    env = environ if environ is not None else os.environ
    found: dict[str, bool] = {}
    for milestone, key in PRODUCTION_AUTHORITY_ENV_KEYS.items():
        parsed = _parse_env_bool(env.get(key))
        if parsed is not None:
            found[milestone] = parsed
    return found


def read_production_gate_sql_consumes_graph(
    environ: Mapping[str, str] | None = None,
) -> bool:
    """正式 Completion Gate SQL 是否读图；默认 false，与 M7 Lab 裁决分离。"""

    env = environ if environ is not None else os.environ
    parsed = _parse_env_bool(env.get(PRODUCTION_GATE_SQL_CONSUMES_GRAPH_ENV))
    return parsed is True


def validate_production_authority_unlock_order(
    authority: Mapping[str, bool],
) -> list[str]:
    """校验 M4→M5→M6→M7 开闸顺序；返回违规码（空表示通过）。"""

    violations: list[str] = []
    for index, milestone in enumerate(PRODUCTION_AUTHORITY_UNLOCK_ORDER):
        if not authority.get(milestone):
            continue
        if index == 0:
            continue
        prerequisite = PRODUCTION_AUTHORITY_UNLOCK_ORDER[index - 1]
        if not authority.get(prerequisite):
            violations.append(f"{milestone}_requires_{prerequisite}")
    return violations


def apply_production_authority_fail_closed(
    authority: dict[str, bool],
) -> tuple[dict[str, bool], list[str]]:
    """顺序违规时关闭后级 authority（fail-closed），不抬高前级。"""

    adjusted = dict(authority)
    reported = validate_production_authority_unlock_order(authority)
    violations = list(reported)
    guard = 0
    while violations and guard < len(PRODUCTION_AUTHORITY_UNLOCK_ORDER):
        for code in violations:
            later = code.split("_requires_")[0]
            if later in adjusted:
                adjusted[later] = False
        violations = validate_production_authority_unlock_order(adjusted)
        guard += 1
    return adjusted, reported


def build_production_authority_rollout_checklist() -> dict[str, Any]:
    """生产开闸验收清单（合同）；不自动改环境。"""

    return {
        "protocol_version": "app-production-authority-rollout.v1",
        "default_authority_all_false_except_m0": True,
        "unlock_order": list(PRODUCTION_AUTHORITY_UNLOCK_ORDER),
        "env_keys": dict(PRODUCTION_AUTHORITY_ENV_KEYS),
        "gate_sql_env": PRODUCTION_GATE_SQL_CONSUMES_GRAPH_ENV,
        "steps": [
            {
                "milestone": "M1",
                "env": PRODUCTION_AUTHORITY_ENV_KEYS["M1"],
                "enables": "formal_settle_oracle + settle_enforcement_preview",
                "must_prove": [
                    "no_forged_network_stable",
                    "app_ready_not_business_home",
                    "identity_drift_blocks_ready",
                    "frame_change_is_transitional",
                    "overlay_startup_not_stable_business",
                    "preview_not_gate_sql",
                ],
                "explicitly_not_approved": [
                    "gate_consumes_settle",
                    "completion_gate_sql_reads_settle",
                ],
                "review_doc": "docs/design/2026-09-09-app-m1-formal-settle-oracle-review.md",
            },
            {
                "milestone": "M4",
                "env": PRODUCTION_AUTHORITY_ENV_KEYS["M4"],
                "enables": "Broker low-risk tap/swipe/back",
                "must_prove": [
                    "receipt_coverage_100pct",
                    "zero_agent_direct_adb",
                    "no_cross_epoch_coordinate_replay",
                    "dual_executor_forbidden",
                ],
            },
            {
                "milestone": "M5",
                "env": PRODUCTION_AUTHORITY_ENV_KEYS["M5"],
                "requires": "M4",
                "enables": "schedules_from_graph prioritization",
                "must_prove": [
                    "overlay_external_not_in_business_denominator",
                    "external_bypass_no_business_coverage",
                ],
            },
            {
                "milestone": "M6",
                "env": PRODUCTION_AUTHORITY_ENV_KEYS["M6"],
                "requires": "M5",
                "enables": "supplemental runtime_ready plan executability",
                "must_prove": [
                    "fresh_cannot_fill_exact",
                    "probe_not_auto_without_policy",
                    "capture_series_lineage",
                ],
            },
            {
                "milestone": "M7",
                "env": PRODUCTION_AUTHORITY_ENV_KEYS["M7"],
                "requires": "M6",
                "enables": "advisory gate_consumes_graph decision",
                "must_prove": [
                    "mutates_completion_gate_sql_false_until_sql_env",
                    "writes_formal_block_reasons_false_until_sql_cutover",
                ],
                "sql_cutover_env": PRODUCTION_GATE_SQL_CONSUMES_GRAPH_ENV,
            },
        ],
        "forbidden": [
            "silent_default_authority_true",
            "skip_unlock_order",
            "gate_sql_cutover_without_m7_e2e",
            "gate_consumes_settle_without_m1_review",
            "delete_retirement_candidates_without_review",
        ],
    }


def build_app_bounded_exploration_milestone_policy(
    overrides: Mapping[str, Any] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    apply_production_env: bool = True,
) -> dict[str, Any]:
    """生成不可变里程碑策略；禁止 Agent 直连 ADB／Driver／CLI。

    优先级：默认 < 生产环境变量 < 调用方 overrides。
    生产 env 打开 M4–M7 时强制校验顺序；违规 fail-closed。
    """

    raw = overrides if isinstance(overrides, Mapping) else {}
    authority = dict(_DEFAULT_AUTHORITY)
    shadow = dict(_DEFAULT_SHADOW)
    env_authority: dict[str, bool] = {}
    order_violations: list[str] = []
    if apply_production_env:
        env_authority = read_production_authority_env(environ)
        for key, value in env_authority.items():
            authority[key] = value
        authority, order_violations = apply_production_authority_fail_closed(authority)
    raw_authority = raw.get("authority")
    raw_shadow = raw.get("shadow")
    if isinstance(raw_authority, Mapping):
        for key in APP_MILESTONE_ORDER:
            if key in raw_authority:
                authority[key] = bool(raw_authority[key])
        # overrides 不 fail-closed：Lab 工厂可显式单开；生产开闸请走环境变量顺序校验。
    if isinstance(raw_shadow, Mapping):
        for key in APP_MILESTONE_ORDER:
            if key in raw_shadow:
                shadow[key] = bool(raw_shadow[key])
    for key in APP_MILESTONE_ORDER:
        if authority[key]:
            shadow[key] = True
    gate_sql = read_production_gate_sql_consumes_graph(environ)
    if gate_sql and not authority.get("M7"):
        gate_sql = False
        order_violations = list(
            dict.fromkeys([*order_violations, "gate_sql_requires_m7_authority"])
        )
    payload = {
        "protocol_version": APP_BOUNDED_EXPLORATION_MILESTONE_VERSION,
        "order": list(APP_MILESTONE_ORDER),
        "authority": authority,
        "shadow": shadow,
        "metric_keys": list(APP_BOUNDED_EXPLORATION_METRIC_KEYS),
        "agent_direct_execution_forbidden": True,
        "forbidden_surfaces": ("adb", "driver", "bash", "cli"),
        "production_authority_env_applied": dict(env_authority),
        "production_authority_order_violations": order_violations,
        "production_gate_sql_consumes_graph": gate_sql,
        "notes": (
            "生产 authority 仅能通过显式环境变量分阶段打开；"
            "M4→M5→M6→M7 顺序违规 fail-closed；"
            "Gate SQL 读图须独立开关且依赖 M7；"
            "Agent 不得直连 ADB/Driver/bash/CLI。"
        ),
    }
    return dict(freeze_json_mapping(payload, "app_bounded_exploration_milestone"))


def milestone_shadow_enabled(policy: Mapping[str, Any] | None, milestone: str) -> bool:
    if not isinstance(policy, Mapping):
        policy = build_app_bounded_exploration_milestone_policy()
    shadow = policy.get("shadow")
    return isinstance(shadow, Mapping) and shadow.get(milestone) is True


def milestone_authority_enabled(policy: Mapping[str, Any] | None, milestone: str) -> bool:
    if not isinstance(policy, Mapping):
        policy = build_app_bounded_exploration_milestone_policy()
    authority = policy.get("authority")
    return isinstance(authority, Mapping) and authority.get(milestone) is True


def resolve_production_m6_supplemental_ready(
    *,
    environ: Mapping[str, str] | None = None,
    explicit: bool | None = None,
) -> bool:
    """组合根：显式 True 优先；显式 False 强制关；None 时跟随生产 M6 env。"""

    if explicit is True:
        return True
    if explicit is False:
        return False
    policy = build_app_bounded_exploration_milestone_policy(
        environ=environ,
        apply_production_env=True,
    )
    return milestone_authority_enabled(policy, "M6")
