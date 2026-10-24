"""生产 authority 分阶段开闸合同回归。"""

from __future__ import annotations

from services.api.app.application.app_bounded_exploration_milestones import (
    PRODUCTION_AUTHORITY_ENV_KEYS,
    PRODUCTION_GATE_SQL_CONSUMES_GRAPH_ENV,
    build_app_bounded_exploration_milestone_policy,
    build_production_authority_rollout_checklist,
    milestone_authority_enabled,
    resolve_production_m6_supplemental_ready,
)
from services.api.app.application.app_command_broker import (
    AppCommandBrokerMode,
    resolve_app_command_broker_mode,
)
from services.api.app.application.app_exploration_grayscale import (
    resolve_app_m5_schedules_from_graph,
)
from services.api.app.application.app_gate_retirement_shadow import (
    resolve_app_m7_gate_enforcement,
)
from services.api.app.application.app_graph_recapture import (
    resolve_app_m6_runtime_ready,
)


def test_production_default_authority_all_closed_except_m0() -> None:
    policy = build_app_bounded_exploration_milestone_policy(environ={}, apply_production_env=True)
    assert milestone_authority_enabled(policy, "M0") is True
    for key in ("M1", "M2", "M4", "M5", "M6", "M7"):
        assert milestone_authority_enabled(policy, key) is False
    assert policy["production_gate_sql_consumes_graph"] is False


def test_production_env_opens_m4_only() -> None:
    env = {PRODUCTION_AUTHORITY_ENV_KEYS["M4"]: "true"}
    policy = build_app_bounded_exploration_milestone_policy(environ=env)
    assert milestone_authority_enabled(policy, "M4") is True
    assert milestone_authority_enabled(policy, "M5") is False
    assert resolve_app_command_broker_mode(policy) is AppCommandBrokerMode.ACTIONS


def test_production_env_skip_order_fail_closed() -> None:
    env = {
        PRODUCTION_AUTHORITY_ENV_KEYS["M5"]: "true",
        # 缺 M4
    }
    policy = build_app_bounded_exploration_milestone_policy(environ=env)
    assert milestone_authority_enabled(policy, "M5") is False
    assert "M5_requires_M4" in policy["production_authority_order_violations"]
    assert resolve_app_m5_schedules_from_graph(policy) is False


def test_production_env_ordered_chain_m4_to_m7() -> None:
    env = {
        PRODUCTION_AUTHORITY_ENV_KEYS["M4"]: "1",
        PRODUCTION_AUTHORITY_ENV_KEYS["M5"]: "true",
        PRODUCTION_AUTHORITY_ENV_KEYS["M6"]: "yes",
        PRODUCTION_AUTHORITY_ENV_KEYS["M7"]: "on",
    }
    policy = build_app_bounded_exploration_milestone_policy(environ=env)
    assert list(policy["production_authority_order_violations"]) == []
    assert resolve_app_command_broker_mode(policy) is AppCommandBrokerMode.ACTIONS
    assert resolve_app_m5_schedules_from_graph(policy) is True
    assert resolve_app_m6_runtime_ready(policy) is True
    assert resolve_app_m7_gate_enforcement(policy) is True


def test_production_gate_sql_requires_m7() -> None:
    env = {PRODUCTION_GATE_SQL_CONSUMES_GRAPH_ENV: "true"}
    policy = build_app_bounded_exploration_milestone_policy(environ=env)
    assert policy["production_gate_sql_consumes_graph"] is False
    assert "gate_sql_requires_m7_authority" in policy["production_authority_order_violations"]

    env2 = {
        PRODUCTION_AUTHORITY_ENV_KEYS["M4"]: "true",
        PRODUCTION_AUTHORITY_ENV_KEYS["M5"]: "true",
        PRODUCTION_AUTHORITY_ENV_KEYS["M6"]: "true",
        PRODUCTION_AUTHORITY_ENV_KEYS["M7"]: "true",
        PRODUCTION_GATE_SQL_CONSUMES_GRAPH_ENV: "true",
    }
    policy2 = build_app_bounded_exploration_milestone_policy(environ=env2)
    assert policy2["production_gate_sql_consumes_graph"] is True


def test_resolve_production_m6_supplemental_ready() -> None:
    assert resolve_production_m6_supplemental_ready(environ={}, explicit=None) is False
    assert resolve_production_m6_supplemental_ready(environ={}, explicit=True) is True
    assert resolve_production_m6_supplemental_ready(
        environ={PRODUCTION_AUTHORITY_ENV_KEYS["M6"]: "true"},
        explicit=False,
    ) is False
    env = {
        PRODUCTION_AUTHORITY_ENV_KEYS["M4"]: "true",
        PRODUCTION_AUTHORITY_ENV_KEYS["M5"]: "true",
        PRODUCTION_AUTHORITY_ENV_KEYS["M6"]: "true",
    }
    assert resolve_production_m6_supplemental_ready(environ=env, explicit=None) is True


def test_production_rollout_checklist_contract() -> None:
    checklist = build_production_authority_rollout_checklist()
    assert checklist["unlock_order"] == ["M4", "M5", "M6", "M7"]
    assert "silent_default_authority_true" in checklist["forbidden"]
    assert len(checklist["steps"]) == 5  # M1 Settle + M4..M7
