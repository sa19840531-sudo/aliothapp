"""App 有限探索灰度（M5 图去重 + Lab 图调度）回归。"""

from __future__ import annotations

from services.api.app.application.app_bounded_exploration_milestones import (
    build_app_bounded_exploration_milestone_policy,
    milestone_authority_enabled,
    milestone_shadow_enabled,
)
from services.api.app.application.app_exploration_grayscale import (
    APP_M5_GRAYSCALE_VERSION,
    attach_app_m5_grayscale_shadow,
    build_composite_action_rebind_contract,
    build_lab_m5_graph_schedule_milestone_policy,
    dedup_discovery_candidates_by_page_graph,
    evaluate_external_bypass,
    prioritize_candidates_by_page_graph,
    resolve_app_m5_grayscale_active,
    resolve_app_m5_schedules_from_graph,
)
from services.api.app.domain.exploration.snapshots import CandidateAction
from services.api.app.domain.interaction import (
    InteractionOperation,
    InteractionRiskLevel,
    SurfaceKind,
)


def _candidate(
    candidate_id: str,
    *,
    expected_page_type: str | None = None,
    business_identity: str | None = None,
    coverage_seek: bool = False,
    frontier_scope: str = "page",
    graph_recovery: bool = False,
) -> CandidateAction:
    metadata: dict[str, object] = {"frontier_scope": frontier_scope}
    if expected_page_type:
        metadata["expected_next_page_type"] = expected_page_type
    if business_identity:
        metadata["expected_business_page_identity"] = business_identity
    if coverage_seek:
        metadata["coverage_page_seek"] = True
    if graph_recovery:
        metadata["graph_recovery"] = True
    return CandidateAction(
        candidate_id=candidate_id,
        semantic_target=candidate_id,
        action_type=InteractionOperation.CLICK,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=50,
        navigation_depth=0,
        expected_surface_kind=SurfaceKind.ANDROID,
        metadata=metadata,
    )


def test_m5_production_default_shadow_without_schedule_authority() -> None:
    policy = build_app_bounded_exploration_milestone_policy()
    assert milestone_shadow_enabled(policy, "M5") is True
    assert milestone_authority_enabled(policy, "M5") is False
    assert resolve_app_m5_grayscale_active(policy) is True
    assert resolve_app_m5_schedules_from_graph(policy) is False


def test_m5_lab_enables_schedules_from_graph() -> None:
    policy = build_lab_m5_graph_schedule_milestone_policy()
    assert resolve_app_m5_schedules_from_graph(policy) is True
    assert resolve_app_m5_grayscale_active(policy) is True


def test_m5_graph_dedup_suppresses_discovery_not_coverage_seek() -> None:
    graph = {
        "states": [
            {
                "logical_page_key": "pkg|act|vid:home",
                "identity_status": "confirmed",
                "counts_as_business_page": True,
                "is_overlay": False,
                "is_external": False,
            }
        ]
    }
    discovery = _candidate("dup-home", business_identity="pkg|act|vid:home")
    coverage = _candidate(
        "seek-home",
        business_identity="pkg|act|vid:home",
        expected_page_type="profile",
        coverage_seek=True,
    )
    fresh = _candidate("new-detail", business_identity="pkg|act|vid:detail")
    kept, suppressed = dedup_discovery_candidates_by_page_graph(
        (discovery, coverage, fresh),
        graph=graph,
        coverage_metadata={"covered_business_state_ids": ["pkg|act|vid:home"]},
    )
    kept_ids = {item.candidate_id for item in kept}
    assert "dup-home" not in kept_ids
    assert "seek-home" in kept_ids
    assert "new-detail" in kept_ids
    assert any(item["candidate_id"] == "dup-home" for item in suppressed)


def test_m5_lab_prioritize_unvisited_targets() -> None:
    graph = {
        "states": [
            {
                "logical_page_key": "pkg|act|vid:home",
                "identity_status": "confirmed",
                "counts_as_business_page": True,
                "is_overlay": False,
                "is_external": False,
            }
        ]
    }
    visited_ish = _candidate("to-home", business_identity="pkg|act|vid:home")
    unvisited = _candidate("to-detail", business_identity="pkg|act|vid:detail")
    recovery = _candidate("recover", graph_recovery=True)
    ordered, audit = prioritize_candidates_by_page_graph(
        (visited_ish, unvisited, recovery),
        graph=graph,
        coverage_metadata={"covered_business_state_ids": ["pkg|act|vid:home"]},
        schedules_from_graph=True,
    )
    assert [item.candidate_id for item in ordered[:2]] == ["to-detail", "recover"]
    assert any(item["reason"].startswith("m5_graph_schedule_boost") for item in audit)

    noop, empty = prioritize_candidates_by_page_graph(
        (visited_ish, unvisited),
        graph=graph,
        schedules_from_graph=False,
    )
    assert [item.candidate_id for item in noop] == ["to-home", "to-detail"]
    assert empty == []


def test_m5_external_bypass_browser_only_no_business_coverage() -> None:
    ok = evaluate_external_bypass(external_kind="browser", bounce_count=0)
    assert ok["allow_observe_landing"] is True
    assert ok["allow_business_actions"] is False
    assert ok["counts_as_business_coverage"] is False

    pay = evaluate_external_bypass(external_kind="payment")
    assert pay["allow_observe_landing"] is False

    loop = evaluate_external_bypass(external_kind="browser", bounce_count=3)
    assert loop["allow_observe_landing"] is False
    assert "external_bounce_limit" in loop["reason_codes"]


def test_m5_composite_rebind_forbids_cross_epoch_replay() -> None:
    contract = build_composite_action_rebind_contract(source_capture_id="cap-1")
    assert contract["cross_epoch_coordinate_replay_forbidden"] is True
    assert contract["requires_rebind_after_scroll"] is True
    assert contract["source_capture_id"] == "cap-1"


def test_m5_attach_production_and_lab_schedule_flags() -> None:
    meta = attach_app_m5_grayscale_shadow(
        {"covered_page_types": ["home"]},
        suppressed=[{"candidate_id": "x", "reason": "m5_graph_discovery_dedup"}],
        external_bypass=evaluate_external_bypass(external_kind="store"),
    )
    shadow = meta["app_exploration_grayscale_shadow"]
    assert shadow["protocol_version"] == APP_M5_GRAYSCALE_VERSION
    assert shadow["active"] is True
    assert shadow["queue_policy"]["required_ratio"] == 70
    assert shadow["schedules_from_graph"] is False
    assert shadow["gate_consumes_graph"] is False
    assert shadow["coverage_authority_changed"] is False

    lab = attach_app_m5_grayscale_shadow(
        {},
        milestone_policy=build_lab_m5_graph_schedule_milestone_policy(),
        schedule_boosts=[{"candidate_id": "a", "reason": "m5_graph_schedule_boost_unvisited"}],
    )
    lab_shadow = lab["app_exploration_grayscale_shadow"]
    assert lab_shadow["schedules_from_graph"] is True
    assert lab_shadow["gate_consumes_graph"] is False
    assert lab_shadow["coverage_authority_changed"] is False
    assert lab_shadow["schedule_boosts"]
