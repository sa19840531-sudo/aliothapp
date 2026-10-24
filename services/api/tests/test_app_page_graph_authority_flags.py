"""App 图／Gate 权威位：M5 调度、M7+SQL Gate（默认仍 Shadow）。"""

from services.api.app.application.app_bounded_exploration_milestones import (
    PRODUCTION_AUTHORITY_ENV_KEYS,
    PRODUCTION_GATE_SQL_CONSUMES_GRAPH_ENV,
    build_app_bounded_exploration_milestone_policy,
)
from services.api.app.application.page_graph_projection import (
    attach_app_coverage_page_graph_shadow,
)


def test_default_keeps_shadow_authority_bits_false() -> None:
    meta = attach_app_coverage_page_graph_shadow(
        {"visited_page_types": ["home"]},
        milestone_policy=build_app_bounded_exploration_milestone_policy(),
    )
    shadow = meta["app_coverage_page_graph_shadow"]
    assert shadow["schedules_from_graph"] is False
    assert shadow["gate_consumes_graph"] is False
    assert shadow["coverage_authority_changed"] is False


def test_m5_authority_enables_schedules_from_graph() -> None:
    policy = build_app_bounded_exploration_milestone_policy(
        environ={
            PRODUCTION_AUTHORITY_ENV_KEYS["M4"]: "true",
            PRODUCTION_AUTHORITY_ENV_KEYS["M5"]: "true",
        },
        apply_production_env=True,
    )
    # M2 shadow may be off by default — force shadow on via overlay
    policy = {
        **policy,
        "shadow": {**dict(policy.get("shadow") or {}), "M2": True},
    }
    meta = attach_app_coverage_page_graph_shadow(
        {"visited_page_types": ["home"]},
        milestone_policy=policy,
    )
    shadow = meta["app_coverage_page_graph_shadow"]
    assert shadow["schedules_from_graph"] is True
    assert shadow["gate_consumes_graph"] is False


def test_m7_and_sql_env_enable_gate_authority_bits() -> None:
    policy = build_app_bounded_exploration_milestone_policy(
        environ={
            PRODUCTION_AUTHORITY_ENV_KEYS["M4"]: "true",
            PRODUCTION_AUTHORITY_ENV_KEYS["M5"]: "true",
            PRODUCTION_AUTHORITY_ENV_KEYS["M6"]: "true",
            PRODUCTION_AUTHORITY_ENV_KEYS["M7"]: "true",
            PRODUCTION_GATE_SQL_CONSUMES_GRAPH_ENV: "true",
        },
        apply_production_env=True,
    )
    policy = {
        **policy,
        "shadow": {**dict(policy.get("shadow") or {}), "M2": True},
    }
    meta = attach_app_coverage_page_graph_shadow(
        {"visited_page_types": ["home"]},
        milestone_policy=policy,
    )
    shadow = meta["app_coverage_page_graph_shadow"]
    assert shadow["gate_consumes_graph"] is True
    assert shadow["coverage_authority_changed"] is True
    assert shadow["schedules_from_graph"] is True
