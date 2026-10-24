"""广告／业务动作预算分账合同测试。"""

from __future__ import annotations

import pytest

from services.api.app.domain.exploration.contracts import (
    ExplorationBudget,
    ExplorationValidationError,
    build_split_exploration_budget,
    consume_exploration_action_budget,
    copy_exploration_budget_after_observation,
    split_app_action_budget_pools,
)


def test_legacy_budget_obstruction_actions_debit_business_pool() -> None:
    budget = ExplorationBudget(
        max_actions=10,
        remaining_actions=10,
        max_depth=3,
        remaining_duration_ms=60_000,
    )
    next_budget = consume_exploration_action_budget(
        budget,
        counts_toward_business=False,
        elapsed_duration_ms=100,
    )
    assert next_budget.resolved_remaining_business_actions == 9
    assert next_budget.resolved_remaining_obstruction_actions == 0
    assert next_budget.remaining_actions == 9


def test_legacy_four_field_budget_defaults_to_all_business() -> None:
    budget = ExplorationBudget(
        max_actions=10,
        remaining_actions=7,
        max_depth=3,
        remaining_duration_ms=60_000,
    )
    assert budget.resolved_max_business_actions == 10
    assert budget.resolved_remaining_business_actions == 7
    assert budget.resolved_max_obstruction_actions == 0
    assert budget.resolved_remaining_obstruction_actions == 0
    restored = ExplorationBudget.from_dict(
        {
            "max_actions": 10,
            "remaining_actions": 7,
            "max_depth": 3,
            "remaining_duration_ms": 60_000,
        }
    )
    assert restored.resolved_max_business_actions == 10
    assert restored.resolved_remaining_obstruction_actions == 0


def test_split_pools_reserve_obstruction_without_starving_required() -> None:
    max_business, max_obstruction, floor_ms = split_app_action_budget_pools(
        max_actions=48,
        required_action_count=28,
        duration_ms=3_600_000,
    )
    assert max_obstruction == 20
    assert max_business == 28
    assert floor_ms == max(3_600_000 * 28 // 48, 3_600_000 // 2)


def test_split_pools_fail_closed_when_required_exceeds_max_actions() -> None:
    with pytest.raises(ExplorationValidationError):
        split_app_action_budget_pools(
            max_actions=30,
            required_action_count=31,
            duration_ms=60_000,
        )


def test_split_pools_never_starve_required_when_fitting_max() -> None:
    max_business, max_obstruction, _floor = split_app_action_budget_pools(
        max_actions=30,
        required_action_count=28,
        duration_ms=60_000,
    )
    assert max_obstruction == 2
    assert max_business == 28
    assert max_business >= 28


def test_obstruction_actions_do_not_consume_business_remaining() -> None:
    budget = build_split_exploration_budget(
        max_actions=48,
        max_depth=4,
        duration_ms=3_600_000,
        required_action_count=28,
    )
    assert budget.resolved_remaining_business_actions == 28
    assert budget.resolved_remaining_obstruction_actions == 20
    next_budget = budget
    for _ in range(15):
        next_budget = consume_exploration_action_budget(
            next_budget,
            counts_toward_business=False,
            elapsed_duration_ms=1_000,
        )
    assert next_budget.resolved_remaining_business_actions == 28
    assert next_budget.resolved_remaining_obstruction_actions == 5
    assert next_budget.remaining_actions == 33


def test_business_actions_do_not_consume_obstruction_remaining() -> None:
    budget = build_split_exploration_budget(
        max_actions=48,
        max_depth=4,
        duration_ms=3_600_000,
        required_action_count=28,
    )
    next_budget = consume_exploration_action_budget(
        budget,
        counts_toward_business=True,
        elapsed_duration_ms=2_000,
    )
    assert next_budget.resolved_remaining_business_actions == 27
    assert next_budget.resolved_remaining_obstruction_actions == 20


def test_business_duration_floor_applied_once_after_obstruction() -> None:
    budget = build_split_exploration_budget(
        max_actions=48,
        max_depth=4,
        duration_ms=3_600_000,
        required_action_count=28,
    )
    after_ads = consume_exploration_action_budget(
        budget,
        counts_toward_business=False,
        elapsed_duration_ms=2_000_000,
    )
    assert after_ads.remaining_duration_ms == 1_600_000
    assert after_ads.business_duration_floor_ms >= 1_800_000
    topped = consume_exploration_action_budget(
        after_ads,
        counts_toward_business=True,
        elapsed_duration_ms=1_000,
        apply_business_duration_floor=True,
    )
    assert topped.business_duration_floor_applied == 1
    assert topped.remaining_duration_ms == after_ads.business_duration_floor_ms
    again = consume_exploration_action_budget(
        topped,
        counts_toward_business=True,
        elapsed_duration_ms=500_000,
        apply_business_duration_floor=True,
    )
    assert again.business_duration_floor_applied == 1
    assert again.remaining_duration_ms == topped.remaining_duration_ms - 500_000


def test_observation_copy_preserves_split_buckets() -> None:
    budget = build_split_exploration_budget(
        max_actions=40,
        max_depth=3,
        duration_ms=100_000,
        required_action_count=10,
    )
    copied = copy_exploration_budget_after_observation(
        budget,
        elapsed_duration_ms=1_000,
    )
    assert copied.remaining_actions == budget.remaining_actions
    assert copied.resolved_remaining_business_actions == budget.resolved_remaining_business_actions
    assert copied.resolved_remaining_obstruction_actions == (
        budget.resolved_remaining_obstruction_actions
    )
    assert copied.remaining_duration_ms == 99_000


def test_to_dict_round_trip_includes_split_fields() -> None:
    budget = build_split_exploration_budget(
        max_actions=40,
        max_depth=3,
        duration_ms=100_000,
        required_action_count=10,
    )
    restored = ExplorationBudget.from_dict(budget.to_dict())
    assert restored.to_dict() == budget.to_dict()


def test_supplemental_budget_preserves_business_obstruction_split() -> None:
    """关闭前补采预算不得把清障池抹平进业务池。"""
    from datetime import UTC, datetime, timedelta

    from services.api.app.application.app_runtime_composition import (
        _build_supplemental_evidence_budget,
    )

    now = datetime(2026, 9, 9, 12, 0, tzinfo=UTC)
    persisted = ExplorationBudget(
        max_actions=15,
        remaining_actions=15,
        max_depth=5,
        remaining_duration_ms=60_000,
        max_business_actions=10,
        remaining_business_actions=10,
        max_obstruction_actions=5,
        remaining_obstruction_actions=5,
    )
    built = _build_supplemental_evidence_budget(
        max_actions=20,
        consumed_actions=0,
        max_depth=5,
        deadline=now + timedelta(minutes=5),
        now=now,
        persisted_budget=persisted,
    )
    assert built.remaining_actions == 15
    assert built.resolved_remaining_business_actions == 10
    assert built.resolved_remaining_obstruction_actions == 5
