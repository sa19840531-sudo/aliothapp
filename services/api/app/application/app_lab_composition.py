"""App Lab 组合根开关：真机对账用，不打开生产 AUTHORITY env。

环境变量：
- ``ALIOTH_APP_LAB_MILESTONE=off|M4|M5|M6|M7``（默认 off）
- ``ALIOTH_APP_LAB_EXECUTES_PROBE=true|false``（默认 false；刀 B）
- 打开 Lab 时禁止同时打开任一 ``ALIOTH_APP_MILESTONE_AUTHORITY_M4…M7``（fail-closed）
- Lab 永不改 ``ALIOTH_APP_GATE_SQL_CONSUMES_GRAPH``

行为（刀 A／刀 B 分账）：
- M4：Broker Lab actions
- M5：含 M4 + 图辅助调度
- M6：含 M5 + **刀 A** ``lab_plan_ready``（补采计划可声明可执行；``executes_probe`` 仍 false）
- M7：含 M6 + Lab Gate 裁决预览（不改 Gate SQL）
- **刀 B**：仅当 M6/M7 且 ``ALIOTH_APP_LAB_EXECUTES_PROBE=true`` 时
  ``lab_executes_probe=true`` → 才允许 SupplementalEvidencePolicy.runtime_ready
  （关闭前排空／Probe 消费者）。``lab_supplemental_ready`` 为刀 B 兼容别名。
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any

from services.api.app.application.app_bounded_exploration_milestones import (
    PRODUCTION_AUTHORITY_ENV_KEYS,
    PRODUCTION_AUTHORITY_UNLOCK_ORDER,
    build_app_bounded_exploration_milestone_policy,
    read_production_authority_env,
)
from services.api.app.application.app_command_broker import (
    build_lab_m4_actions_milestone_policy,
)
from services.api.app.application.app_exploration_grayscale import (
    build_lab_m5_graph_schedule_milestone_policy,
)
from services.api.app.application.app_gate_retirement_shadow import (
    build_lab_m7_gate_enforcement_milestone_policy,
)
from services.api.app.application.app_graph_recapture import (
    build_lab_m6_recapture_milestone_policy,
)
from services.api.app.domain.protocol import freeze_json_mapping

APP_LAB_COMPOSITION_VERSION = "app-lab-composition.v2"
APP_LAB_MILESTONE_ENV = "ALIOTH_APP_LAB_MILESTONE"
APP_LAB_EXECUTES_PROBE_ENV = "ALIOTH_APP_LAB_EXECUTES_PROBE"
APP_DEPLOYMENT_ENVIRONMENT_ENV = "ALIOTH_ENVIRONMENT"
APP_LAB_MILESTONE_VALUES: frozenset[str] = frozenset(
    {"off", "m4", "m5", "m6", "m7"}
)
_PRODUCTION_ENVIRONMENT_VALUES: frozenset[str] = frozenset({"prod", "production"})


def is_app_production_deployment(environ: Mapping[str, str] | None = None) -> bool:
    """部署环境是否为 production（不可用 Lab 开关伪造）。"""

    env = environ if environ is not None else os.environ
    return (env.get(APP_DEPLOYMENT_ENVIRONMENT_ENV) or "").strip().casefold() in (
        _PRODUCTION_ENVIRONMENT_VALUES
    )

__all__ = [
    "APP_DEPLOYMENT_ENVIRONMENT_ENV",
    "APP_LAB_COMPOSITION_VERSION",
    "APP_LAB_EXECUTES_PROBE_ENV",
    "APP_LAB_MILESTONE_ENV",
    "AppLabCompositionError",
    "build_app_lab_composition_audit",
    "is_app_production_deployment",
    "parse_app_lab_executes_probe",
    "parse_app_lab_milestone",
    "resolve_app_lab_composition",
    "resolve_effective_app_milestone_policy",
]


class AppLabCompositionError(ValueError):
    """Lab 组合根配置非法或与生产开闸冲突。"""


def parse_app_lab_milestone(raw: str | None) -> str | None:
    """解析 Lab 里程碑；``off``／空 → None；非法值抛错。"""

    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    key = text.casefold()
    if key in {"0", "false", "no", "off", "none", "disabled"}:
        return None
    if key.startswith("m") and key[1:].isdigit():
        key = f"m{key[1:]}"
    if key not in APP_LAB_MILESTONE_VALUES - {"off"}:
        raise AppLabCompositionError(
            f"invalid_{APP_LAB_MILESTONE_ENV}={text!r}; "
            "allowed=off|M4|M5|M6|M7"
        )
    return key.upper()


def parse_app_lab_executes_probe(raw: str | None) -> bool:
    """解析刀 B：未设／空／false → False；非法值抛错。"""

    if raw is None:
        return False
    text = str(raw).strip()
    if not text:
        return False
    key = text.casefold()
    if key in {"1", "true", "yes", "on", "enabled"}:
        return True
    if key in {"0", "false", "no", "off", "disabled"}:
        return False
    raise AppLabCompositionError(
        f"invalid_{APP_LAB_EXECUTES_PROBE_ENV}={text!r}; allowed=true|false"
    )


def _lab_policy_for_milestone(milestone: str) -> dict[str, Any]:
    if milestone == "M4":
        return build_lab_m4_actions_milestone_policy()
    if milestone == "M5":
        return build_lab_m5_graph_schedule_milestone_policy()
    if milestone == "M6":
        return build_lab_m6_recapture_milestone_policy()
    if milestone == "M7":
        return build_lab_m7_gate_enforcement_milestone_policy()
    raise AppLabCompositionError(f"unsupported_lab_milestone={milestone}")


def resolve_app_lab_composition(
    environ: Mapping[str, str] | None = None,
    *,
    strict_conflict: bool = True,
) -> dict[str, Any]:
    """解析 Lab 组合根；默认关闭，不触碰生产 AUTHORITY。"""

    env = environ if environ is not None else os.environ
    milestone = parse_app_lab_milestone(env.get(APP_LAB_MILESTONE_ENV))
    executes_probe_requested = parse_app_lab_executes_probe(
        env.get(APP_LAB_EXECUTES_PROBE_ENV)
    )
    if milestone is not None and is_app_production_deployment(env):
        raise AppLabCompositionError(
            "lab_forbidden_in_production_deployment:"
            f"{APP_DEPLOYMENT_ENVIRONMENT_ENV}=production 禁止 "
            f"{APP_LAB_MILESTONE_ENV}={milestone}；请设为 off"
        )
    production_authority = read_production_authority_env(env)
    production_open = [
        key
        for key in PRODUCTION_AUTHORITY_UNLOCK_ORDER
        if production_authority.get(key) is True
    ]
    if milestone is not None and production_open and strict_conflict:
        raise AppLabCompositionError(
            "lab_composition_conflicts_with_production_authority:"
            + ",".join(production_open)
            + f"; unset {[PRODUCTION_AUTHORITY_ENV_KEYS[k] for k in production_open]} "
            f"or set {APP_LAB_MILESTONE_ENV}=off"
        )
    if executes_probe_requested and milestone not in {"M6", "M7"}:
        raise AppLabCompositionError(
            f"{APP_LAB_EXECUTES_PROBE_ENV}=true 仅允许在 "
            f"{APP_LAB_MILESTONE_ENV}=M6|M7 时开启；"
            f"当前 milestone={milestone!r}"
        )
    if milestone is None:
        payload = {
            "protocol_version": APP_LAB_COMPOSITION_VERSION,
            "enabled": False,
            "milestone": None,
            "lab_plan_ready": False,
            "lab_executes_probe": False,
            # 兼容别名：历史字段曾误绑刀 A；现起仅代表刀 B（关闭前 Probe／排空）。
            "lab_supplemental_ready": False,
            "source": "production_default",
            "env_key": APP_LAB_MILESTONE_ENV,
            "executes_probe_env_key": APP_LAB_EXECUTES_PROBE_ENV,
            "milestone_policy": build_app_bounded_exploration_milestone_policy(
                environ=env,
                apply_production_env=True,
            ),
            "production_authority_open": production_open,
            "notes": (
                "Lab 关闭：使用生产默认里程碑（authority 仅 M0）；"
                "真机对账请设 ALIOTH_APP_LAB_MILESTONE=M4|M5|M6|M7；"
                "关闭前 Probe 另需 ALIOTH_APP_LAB_EXECUTES_PROBE=true（刀 B）。"
            ),
        }
        return dict(freeze_json_mapping(payload, "app_lab_composition"))

    policy = _lab_policy_for_milestone(milestone)
    lab_plan_ready = milestone in {"M6", "M7"}
    lab_executes_probe = bool(lab_plan_ready and executes_probe_requested)
    payload = {
        "protocol_version": APP_LAB_COMPOSITION_VERSION,
        "enabled": True,
        "milestone": milestone,
        "lab_plan_ready": lab_plan_ready,
        "lab_executes_probe": lab_executes_probe,
        "lab_supplemental_ready": lab_executes_probe,
        "source": "lab_env",
        "env_key": APP_LAB_MILESTONE_ENV,
        "executes_probe_env_key": APP_LAB_EXECUTES_PROBE_ENV,
        "milestone_policy": policy,
        "production_authority_open": production_open,
        "notes": (
            f"Lab {milestone}：组合根注入 Lab 工厂；"
            "不打开生产 AUTHORITY env；"
            f"刀 A lab_plan_ready={lab_plan_ready}（计划可声明可执行）；"
            f"刀 B lab_executes_probe={lab_executes_probe}（关闭前排空／Probe）；"
            "不改 Completion Gate SQL。"
        ),
    }
    return dict(freeze_json_mapping(payload, "app_lab_composition"))


def resolve_effective_app_milestone_policy(
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """探索／Broker 统一入口：Lab 开则用 Lab 工厂，否则生产默认。"""

    composition = resolve_app_lab_composition(environ)
    policy = composition.get("milestone_policy")
    if isinstance(policy, Mapping):
        return dict(policy)
    return build_app_bounded_exploration_milestone_policy(environ=environ)


def build_app_lab_composition_audit(
    composition: Mapping[str, Any] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """写入 Coverage 的精简审计栏（不含完整 policy 重复时可只存摘要）。"""

    raw = (
        dict(composition)
        if isinstance(composition, Mapping)
        else resolve_app_lab_composition(environ)
    )
    policy = raw.get("milestone_policy")
    authority = {}
    if isinstance(policy, Mapping) and isinstance(policy.get("authority"), Mapping):
        authority = {
            key: bool(policy["authority"].get(key))
            for key in ("M0", "M1", "M2", "M4", "M5", "M6", "M7")
        }
    return {
        "protocol_version": APP_LAB_COMPOSITION_VERSION,
        "enabled": bool(raw.get("enabled")),
        "milestone": raw.get("milestone"),
        "lab_plan_ready": bool(raw.get("lab_plan_ready")),
        "lab_executes_probe": bool(raw.get("lab_executes_probe")),
        "lab_supplemental_ready": bool(raw.get("lab_supplemental_ready")),
        "source": raw.get("source"),
        "authority_snapshot": authority,
        "production_authority_open": list(raw.get("production_authority_open") or ()),
    }
