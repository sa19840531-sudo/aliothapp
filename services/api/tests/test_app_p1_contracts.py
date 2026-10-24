"""P1 contracts: ExternalTransition, ads, episode, occurrence, acceptance, login/age."""

from __future__ import annotations

from services.api.app.application.app_acceptance_matrix import (
    NAV_TARGET_COUNT_CASES,
    evaluate_deep_link_not_tap_pass,
    evaluate_nav_target_count_case,
    evaluate_single_broker,
    run_app_acceptance_suite,
)
from services.api.app.application.app_ad_landing_contract import (
    build_landing_observation,
    classify_ad_landing_route,
    inventory_ad_occurrences,
)
from services.api.app.application.app_episode_state_contract import (
    build_episode_state_revision,
    episode_candidates_from_controls,
    same_page_episode_revision,
)
from services.api.app.application.app_external_transition import (
    build_external_transition,
    external_transition_from_package_hop,
    project_external_transitions_for_shadow,
)
from services.api.app.application.app_login_detect_only_contract import (
    build_app_age_gate_protocol_snapshot,
    classify_login_surface,
    login_action_allowed,
)
from services.api.app.application.app_navigation_occurrence import (
    build_app_navigation_occurrence,
    merge_occurrences_keep_each,
)
from services.api.app.application.app_package_boundary import ExternalActivityKind
from services.api.app.application.page_graph_projection import (
    build_app_page_graph_shadow_snapshot,
)


def test_external_transition_not_business_coverage() -> None:
    transition = build_external_transition(
        source_capture_id="c1",
        source_package="com.demo.app",
        destination_package="com.android.chrome",
        destination_activity="Main",
    )
    assert transition["entity"] == "ExternalTransition"
    assert transition["enters_business_coverage"] is False
    assert transition["external_activity_kind"] == ExternalActivityKind.BROWSER.value
    hop = external_transition_from_package_hop(
        source_capture_id="c1",
        before_package="com.demo.app",
        before_activity=".A",
        after_package="com.android.vending",
        after_activity=".Store",
        allowed_hosts=("com.demo.app",),
    )
    assert hop is not None
    assert hop["hard_blocked"] is True


def test_shadow_includes_external_transitions() -> None:
    external = build_external_transition(
        source_capture_id="c1",
        source_package="com.demo.app",
        destination_package="com.android.chrome",
    )
    snap = build_app_page_graph_shadow_snapshot(
        coverage_metadata={"external_transitions": [external]},
    )
    projected = project_external_transitions_for_shadow([external])
    assert snap["external_transitions"] == projected
    assert snap["landing_observations"] == []
    assert snap["episode_state_revisions"] == []


def test_ad_landing_routes_and_observation() -> None:
    assert classify_ad_landing_route("https://ads.example/x")["executor"] == "isolated_playwright"
    assert classify_ad_landing_route("myapp://promo")["executor"] == "authorized_sidecar"
    assert classify_ad_landing_route("market://details?id=x")["hard_blocked"] is True
    occ = inventory_ad_occurrences(
        [{"text": "ad", "link": "https://ads.example/x"}],
        source_capture_id="cap-1",
    )
    assert len(occ) == 1
    landing = build_landing_observation(
        occurrence_id=occ[0]["occurrence_id"],
        uri="https://ads.example/x",
        artifact_ids=("dom-1", "shot-1"),
    )
    assert landing["entity"] == "LandingObservation"
    assert landing["enters_business_coverage"] is False
    assert landing["status"] == "observed"


def test_episode_same_page_revision() -> None:
    first = build_episode_state_revision(
        page_node_id="detail-1", season="1", episode="01"
    )
    second = build_episode_state_revision(
        page_node_id="detail-1", season="1", episode="02"
    )
    assert first["creates_new_page_type"] is False
    assert same_page_episode_revision(first, second) is True
    cands = episode_candidates_from_controls(
        [{"kind": "episode", "label": "第3集", "episode": "03"}],
        page_node_id="detail-1",
    )
    assert cands and cands[0]["episode"] == "03"


def test_navigation_occurrence_keeps_each() -> None:
    a = build_app_navigation_occurrence(
        occurrence_id="o1",
        logical_target_id="home",
        label="首页",
        source_capture_id="c1",
        package_name="com.demo.app",
        discovery_source="uia_aligned",
        capture_epoch="e1",
    )
    b = build_app_navigation_occurrence(
        occurrence_id="o2",
        logical_target_id="home",
        label="主页",
        source_capture_id="c2",
        package_name="com.demo.app",
    )
    grouped = merge_occurrences_keep_each([a, b])
    assert len(grouped["home"]) == 2


def test_acceptance_matrix_nav_counts() -> None:
    assert NAV_TARGET_COUNT_CASES == (0, 9, 10, 11, 25)
    ok = evaluate_nav_target_count_case(
        discovered_count=11, frontier_count=11, truncated_to_first_10=False
    )
    bad = evaluate_nav_target_count_case(
        discovered_count=11, frontier_count=10, truncated_to_first_10=True
    )
    assert ok["passed"] is True
    assert bad["passed"] is False
    suite = run_app_acceptance_suite(
        [
            ok,
            evaluate_deep_link_not_tap_pass(deep_link_counted_as_tap=False),
            evaluate_single_broker(concurrent_device_owners=False),
        ]
    )
    assert suite["passed"] is True


def test_login_detect_only_and_age_gate() -> None:
    login = classify_login_surface(
        page_state="login_required", surface_facts={"auth_gate_visible": True}
    )
    assert login["mode"] == "detect_only"
    assert login_action_allowed("type_password") is False
    assert login_action_allowed("tap") is True
    age = build_app_age_gate_protocol_snapshot(
        adult_declaration_frozen=True,
        unique_enter_control=True,
        confidence_ok=True,
        requires_identity_or_payment=False,
    )
    assert age["auto_enter_allowed"] is True
    assert age["home_judgment_blocked_while_gate_present"] is True
