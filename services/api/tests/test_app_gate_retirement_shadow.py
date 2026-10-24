"""App Gate／工作台／退役（M7 影子 + Lab 裁决灰度）回归。"""

from __future__ import annotations

from services.api.app.application.app_bounded_exploration_milestones import (
    build_app_bounded_exploration_milestone_policy,
    milestone_authority_enabled,
    milestone_shadow_enabled,
)
from services.api.app.application.app_gate_retirement_shadow import (
    APP_M7_GATE_RETIREMENT_VERSION,
    attach_app_m7_gate_retirement_shadow,
    build_app_m7_gate_enforcement_shadow,
    build_app_retirement_candidate_catalog,
    build_app_workbench_page_graph_projection,
    build_lab_m7_gate_enforcement_milestone_policy,
    resolve_app_m7_gate_enforcement,
    resolve_app_m7_gate_shadow_active,
)


def _scoped_graph(*, include_detail: bool = True) -> dict:
    scope = {"task_id": "t", "agent_run_id": "r", "finalization_epoch": 1}
    states = [
        {
            **scope,
            "state_id": "s1",
            "logical_page_key": "page:home",
            "state_fingerprint": "a",
            "surface_session_id": "sess",
            "identity_status": "confirmed",
            "counts_as_business_page": True,
            "is_overlay": False,
            "is_external": False,
        },
    ]
    transitions: list[dict] = []
    if include_detail:
        states.append(
            {
                **scope,
                "state_id": "s2",
                "logical_page_key": "page:detail",
                "state_fingerprint": "b",
                "surface_session_id": "sess",
                "identity_status": "confirmed",
                "counts_as_business_page": True,
                "is_overlay": False,
                "is_external": False,
            }
        )
        transitions.append(
            {
                **scope,
                "transition_id": "e1",
                "source_state_id": "s1",
                "target_state_id": "s2",
                "status": "completed",
                "proof": "trusted",
                "counts_as_business_arrival": True,
            }
        )
    return {**scope, "states": states, "transitions": transitions}


def test_m7_production_default_shadow_without_enforcement() -> None:
    policy = build_app_bounded_exploration_milestone_policy()
    assert milestone_shadow_enabled(policy, "M7") is True
    assert milestone_authority_enabled(policy, "M7") is False
    assert resolve_app_m7_gate_shadow_active(policy) is True
    assert resolve_app_m7_gate_enforcement(policy) is False


def test_m7_lab_enables_gate_advisory() -> None:
    policy = build_lab_m7_gate_enforcement_milestone_policy()
    assert resolve_app_m7_gate_enforcement(policy) is True
    assert resolve_app_m7_gate_shadow_active(policy) is True


def test_m7_aligned_shadow_does_not_would_block() -> None:
    graph = _scoped_graph()
    shadow = build_app_m7_gate_enforcement_shadow(
        coverage_metadata={
            "covered_business_state_ids": ["page:home", "page:detail"],
            "coverage_complete": True,
            "visual_quality_satisfied": True,
        },
        graph=graph,
    )
    assert shadow["protocol_version"] == APP_M7_GATE_RETIREMENT_VERSION
    assert shadow["gate_consumes_graph"] is False
    assert shadow["enforces_gate"] is False
    assert shadow["gate_shadow_only"] is True
    assert shadow["mutates_completion_gate_sql"] is False
    assert shadow["page_graph_consistent"] is True
    assert shadow["would_block_if_enforced"] is False
    assert shadow["formal_gate_inputs_unchanged"] is True
    assert shadow["lab_gate_passed"] is None
    assert shadow["workbench_projection"]["drives_formal_gate"] is False
    assert shadow["retirement_catalog"]["any_deletion_executed"] is False


def test_m7_lab_advisory_pass_and_fail_without_sql_mutation() -> None:
    graph = _scoped_graph()
    policy = build_lab_m7_gate_enforcement_milestone_policy()
    passed = build_app_m7_gate_enforcement_shadow(
        coverage_metadata={
            "covered_business_state_ids": ["page:home", "page:detail"],
            "coverage_complete": True,
            "visual_quality_satisfied": True,
        },
        graph=graph,
        milestone_policy=policy,
    )
    assert passed["gate_consumes_graph"] is True
    assert passed["enforces_gate"] is True
    assert passed["lab_gate_passed"] is True
    assert passed["mutates_completion_gate_sql"] is False
    assert passed["writes_formal_block_reasons"] is False
    assert passed["formal_gate_inputs_unchanged"] is True
    assert "completion_gate_sql_unchanged" in passed["reason_codes"]

    failed = build_app_m7_gate_enforcement_shadow(
        coverage_metadata={
            "covered_business_state_ids": ["page:home", "page:orphan"],
            "coverage_complete": True,
            "visual_quality_satisfied": True,
        },
        graph=_scoped_graph(include_detail=False),
        milestone_policy=policy,
    )
    assert failed["lab_gate_passed"] is False
    assert "page_graph_coverage_mismatch" in failed["lab_block_reasons"]
    assert failed["mutates_completion_gate_sql"] is False


def test_m7_mismatch_preview_blocks_only_in_shadow() -> None:
    graph = _scoped_graph(include_detail=False)
    shadow = build_app_m7_gate_enforcement_shadow(
        coverage_metadata={
            "covered_business_state_ids": ["page:home", "page:orphan"],
            "coverage_complete": True,
        },
        graph=graph,
    )
    assert shadow["page_graph_consistent"] is False
    assert shadow["would_block_if_enforced"] is True
    assert "page_graph_coverage_mismatch" in shadow["would_block_reasons"]
    assert shadow["enforces_gate"] is False
    assert shadow["gate_consumes_graph"] is False


def test_m7_retirement_catalog_lab_review_still_forbids_deletion() -> None:
    catalog = build_app_retirement_candidate_catalog(
        observed_metrics={
            "page_graph_coverage_reconcile_mismatch_count": 0,
            "verified_arrival_count": 1,
            "stable_capture_success_count": 1,
            "non_stable_as_business_evidence_count": 0,
            "overlay_close_failure_terminal_count": 0,
            "agent_direct_adb_attempt_count": 0,
            "external_package_escape_count": 0,
        },
        lab_review_enabled=True,
    )
    assert catalog["any_deletion_executed"] is False
    assert catalog["rollback_review_required"] is True
    assert any(item["ready_for_retirement_review"] for item in catalog["candidates"])
    for item in catalog["candidates"]:
        assert item["deletion_allowed"] is False


def test_m7_workbench_projection_splits_layers() -> None:
    graph = _scoped_graph()
    graph["states"].append(
        {
            "task_id": "t",
            "agent_run_id": "r",
            "finalization_epoch": 1,
            "state_id": "ov",
            "logical_page_key": "overlay:popup",
            "is_overlay": True,
            "is_external": False,
            "counts_as_business_page": False,
        }
    )
    projection = build_app_workbench_page_graph_projection(
        graph,
        reconcile={
            "matched_count": 1,
            "only_in_coverage_count": 0,
            "only_in_graph_count": 0,
            "aligned": True,
        },
        drives_lab_advisory=True,
    )
    assert projection["business_state_count"] == 2
    assert projection["overlay_state_count"] == 1
    assert projection["trusted_edge_count"] == 1
    assert projection["drives_formal_gate"] is False
    assert projection["drives_lab_advisory"] is True


def test_m7_attach_production_and_lab() -> None:
    meta = attach_app_m7_gate_retirement_shadow(
        {
            "covered_business_state_ids": ["page:home", "page:detail"],
            "coverage_complete": True,
            "visual_quality_satisfied": True,
        },
        graph=_scoped_graph(),
    )
    shadow = meta["app_gate_graph_shadow"]
    assert shadow["active"] is True
    assert shadow["enforces_gate"] is False
    assert shadow["gate_consumes_graph"] is False
    assert shadow["mutates_completion_gate_sql"] is False

    lab = attach_app_m7_gate_retirement_shadow(
        {
            "covered_business_state_ids": ["page:home", "page:detail"],
            "coverage_complete": True,
            "visual_quality_satisfied": True,
        },
        graph=_scoped_graph(),
        milestone_policy=build_lab_m7_gate_enforcement_milestone_policy(),
    )
    lab_shadow = lab["app_gate_graph_shadow"]
    assert lab_shadow["enforces_gate"] is True
    assert lab_shadow["lab_gate_passed"] is True
    assert lab_shadow["mutates_completion_gate_sql"] is False


def test_production_gate_sql_cutover_sets_mutates_flag() -> None:
    from services.api.app.application.app_bounded_exploration_milestones import (
        PRODUCTION_AUTHORITY_ENV_KEYS,
        PRODUCTION_GATE_SQL_CONSUMES_GRAPH_ENV,
        build_app_bounded_exploration_milestone_policy,
    )
    from services.api.app.application.app_gate_retirement_shadow import (
        build_app_m7_gate_enforcement_shadow,
    )

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
    assert policy["production_gate_sql_consumes_graph"] is True
    shadow = build_app_m7_gate_enforcement_shadow(
        coverage_metadata={
            "coverage_complete": True,
            "visual_quality_satisfied": True,
        },
        milestone_policy=policy,
    )
    assert shadow["mutates_completion_gate_sql"] is True
    assert shadow["coverage_authority_changed"] is True
    assert shadow["gate_consumes_graph"] is True
    assert shadow["formal_gate_inputs_unchanged"] is False
