"""P0: 不限量 Frontier 与 Deep Link／Tap 分离。"""

from __future__ import annotations

from services.api.app.application.app_deep_link_contract import (
    DEEP_LINK_DIRECT_OBSERVATION_REASON,
    classify_deep_link_target,
    deep_link_is_hard_blocked,
    deep_link_proves_native_control_click,
)
from services.api.app.application.app_evidence_role_contract import (
    ROLE_DEEP_LINK_DIRECT_OBSERVATION,
    role_counts_as_business_arrival,
)
from services.api.app.application.app_exploration_policy import (
    build_dynamic_app_exploration_runtime_policy,
)
from services.api.app.application.app_frontier_quantity_contract import (
    page_limit_blocks_new_page_candidates,
    policy_is_quantity_unbounded,
)


def test_deep_link_never_proves_native_tap() -> None:
    assert deep_link_proves_native_control_click() is False


def test_deep_link_hard_blocks_store_tel_sms() -> None:
    assert deep_link_is_hard_blocked("market://details?id=com.example")
    assert deep_link_is_hard_blocked("tel:+123456")
    assert deep_link_is_hard_blocked("sms:10086")
    assert deep_link_is_hard_blocked(
        "intent://play.google.com/store/apps/details#Intent;scheme=https;end"
    )
    ok = classify_deep_link_target("myapp://home/feed")
    assert ok["hard_blocked"] is False
    assert ok["proves_native_control_click"] is False


def test_deep_link_evidence_role_not_business_arrival() -> None:
    assert role_counts_as_business_arrival(ROLE_DEEP_LINK_DIRECT_OBSERVATION) is False


def test_dynamic_frontier_is_quantity_unbounded() -> None:
    policy = build_dynamic_app_exploration_runtime_policy(
        task_config={"scan_mode": "full", "max_depth": 3},
        loop_guard={"max_depth": 3, "max_duration_seconds": 600, "max_actions": 48},
        page_sampling_policy={"mode": "by_page_type", "max_pages": 5},
        journey_steps=("home", "detail", "profile", "search", "settings", "mine"),
    )
    assert policy.exploration_mode == "dynamic_frontier"
    assert policy.quantity_unbounded is True
    assert policy.page_limit == 5
    # 不限量：minimum_business_pages 可大于 page_limit，不再 UNREACHABLE
    assert policy.minimum_business_pages >= 2
    payload = policy.to_dict()
    assert payload["quantity_unbounded"] is True
    assert policy_is_quantity_unbounded(payload) is True
    assert payload["quantity_contract"]["page_limit_role"] == (
        "optional_safety_hint_not_completion"
    )


def test_page_limit_does_not_block_when_unbounded() -> None:
    assert (
        page_limit_blocks_new_page_candidates(
            quantity_unbounded=True,
            page_limit=3,
            covered_count=10,
        )
        is False
    )
    assert (
        page_limit_blocks_new_page_candidates(
            quantity_unbounded=False,
            page_limit=3,
            covered_count=3,
        )
        is True
    )


def test_direct_observation_reason_constant() -> None:
    assert "not_native_tap" in DEEP_LINK_DIRECT_OBSERVATION_REASON
