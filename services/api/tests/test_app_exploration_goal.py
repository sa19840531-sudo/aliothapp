"""App 探索目标与覆盖快照领域合同测试。

这些测试先锁定边界合同，再由领域实现补齐；不依赖真实设备或 PostgreSQL。
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from services.api.app.domain.exploration import (
    APP_EXPLORATION_COVERAGE_PROTOCOL_VERSION,
    APP_EXPLORATION_GOAL_PROTOCOL_VERSION,
    AppExplorationCoverageSnapshot,
    AppExplorationGoal,
)


def _goal(**overrides: object) -> AppExplorationGoal:
    values: dict[str, object] = {
        "required_page_types": ("home", "content_detail", "profile"),
        "required_milestones": ("app_ready", "stable_business_surface"),
        "minimum_business_pages": 2,
        "allowed_packages": ("com.example.app", "com.example.app.debug"),
    }
    values.update(overrides)
    return AppExplorationGoal(**values)


def _snapshot(**overrides: object) -> AppExplorationCoverageSnapshot:
    values: dict[str, object] = {
        "required_page_types": ("home", "content_detail", "profile"),
        "covered_page_types": ("home",),
        "missing_page_types": ("content_detail",),
        "blocked_page_types": ("profile",),
        "required_milestones": ("app_ready", "stable_business_surface"),
        "covered_milestones": ("app_ready",),
        "missing_milestones": ("stable_business_surface",),
        "blocked_milestones": (),
        "minimum_business_pages": 2,
        "covered_business_page_count": 1,
        "eligible_candidate_count": 3,
        "frontier_exhausted": False,
        "stable_page_reached": True,
        "baseline_evidence_complete": False,
        "pending_action_count": 1,
        "pending_evidence_count": 0,
    }
    values.update(overrides)
    return AppExplorationCoverageSnapshot(**values)


def test_goal_is_versioned_and_round_trips_without_losing_order() -> None:
    goal = _goal()

    payload = goal.to_dict()
    restored = AppExplorationGoal.from_dict(payload)

    assert payload["protocol_version"] == APP_EXPLORATION_GOAL_PROTOCOL_VERSION
    assert payload == restored.to_dict()
    assert restored.required_page_types == ("home", "content_detail", "profile")
    assert restored.required_milestones == ("app_ready", "stable_business_surface")
    assert restored.allowed_packages == ("com.example.app", "com.example.app.debug")


@pytest.mark.parametrize(
    ("field_name", "value"),
    (
        ("required_page_types", ("home", "home")),
        ("required_milestones", ("app_ready", "app_ready")),
        ("allowed_packages", ("com.example.app", "com.example.app")),
        ("required_page_types", ("home", "")),
        ("required_milestones", (" ",)),
        ("allowed_packages", ("",)),
        ("required_page_types", ()),
        ("required_milestones", ()),
        ("allowed_packages", ()),
    ),
)
def test_goal_rejects_duplicate_or_empty_scope_values(
    field_name: str,
    value: tuple[str, ...],
) -> None:
    with pytest.raises(ValueError):
        _goal(**{field_name: value})


def test_goal_rejects_negative_minimum_business_pages_and_unknown_protocol() -> None:
    with pytest.raises(ValueError):
        _goal(minimum_business_pages=-1)

    payload = _goal().to_dict()
    payload["protocol_version"] = "app-exploration-goal.v0"
    with pytest.raises(ValueError):
        AppExplorationGoal.from_dict(payload)


def test_goal_v2_round_trips_applicability_groups_and_promotes_explicit_mode() -> None:
    goal = _goal(
        optional_page_types=("settings",),
        lifecycle_page_types=("line_loading",),
        applicability_mode="explicit_targets",
    )

    assert goal.protocol_version == "app-exploration-goal.v2"
    payload = goal.to_dict()
    restored = AppExplorationGoal.from_dict(payload)

    assert payload == restored.to_dict()
    assert restored.optional_page_types == ("settings",)
    assert restored.lifecycle_page_types == ("line_loading",)
    assert restored.applicability_mode == "explicit_targets"


def test_goal_v1_payload_remains_compatible_without_extended_fields() -> None:
    payload = _goal().to_dict()

    assert payload["protocol_version"] == APP_EXPLORATION_GOAL_PROTOCOL_VERSION
    assert "optional_page_types" not in payload
    assert AppExplorationGoal.from_dict(payload).to_dict() == payload


def test_goal_v1_payload_keeps_legacy_lifecycle_required_page_compatible() -> None:
    goal = _goal(required_page_types=("startup_ad", "guide_page"))

    payload = goal.to_dict()

    assert payload["protocol_version"] == APP_EXPLORATION_GOAL_PROTOCOL_VERSION
    assert AppExplorationGoal.from_dict(payload).required_page_types == (
        "startup_ad",
        "guide_page",
    )


def test_coverage_snapshot_is_versioned_deterministic_and_hash_verified() -> None:
    snapshot = _snapshot()
    replay = AppExplorationCoverageSnapshot.from_dict(snapshot.to_dict())

    assert snapshot.to_dict() == replay.to_dict()
    assert snapshot.to_dict()["protocol_version"] == APP_EXPLORATION_COVERAGE_PROTOCOL_VERSION
    assert snapshot.content_sha256.startswith("sha256:")
    assert snapshot.content_sha256 == snapshot.recompute_content_sha256()


@pytest.mark.parametrize(
    ("field_name", "value"),
    (
        ("covered_page_types", ("home", "profile")),
        ("missing_page_types", ("content_detail", "profile")),
        ("blocked_page_types", ("profile", "home")),
        ("covered_milestones", ("app_ready", "stable_business_surface")),
        ("missing_milestones", ("stable_business_surface", "app_ready")),
        ("blocked_milestones", ("stable_business_surface",)),
    ),
)
def test_coverage_snapshot_rejects_overlapping_page_or_milestone_partitions(
    field_name: str,
    value: tuple[str, ...],
) -> None:
    # 每个替换值都与另一分区重叠，或把同一 required 项重复归类。
    with pytest.raises(ValueError):
        _snapshot(**{field_name: value})


@pytest.mark.parametrize(
    "field_name",
    (
        "minimum_business_pages",
        "covered_business_page_count",
        "eligible_candidate_count",
        "pending_action_count",
        "pending_evidence_count",
    ),
)
def test_coverage_snapshot_rejects_negative_counters(field_name: str) -> None:
    with pytest.raises(ValueError):
        _snapshot(**{field_name: -1})


def test_coverage_snapshot_rejects_required_items_outside_exact_partition() -> None:
    with pytest.raises(ValueError):
        _snapshot(
            required_page_types=("home", "content_detail", "profile", "settings")
        )

    with pytest.raises(ValueError):
        _snapshot(covered_page_types=("unknown",))


def test_coverage_snapshot_hash_changes_when_content_changes() -> None:
    snapshot = _snapshot()
    changed = replace(snapshot, covered_business_page_count=2)

    assert changed.content_sha256 != snapshot.content_sha256
    assert changed.recompute_content_sha256() == changed.content_sha256
