"""App 图恢复与补采（M6 影子 + Lab runtime_ready 灰度）回归。"""

from __future__ import annotations

from services.api.app.application.app_bounded_exploration_milestones import (
    build_app_bounded_exploration_milestone_policy,
    milestone_authority_enabled,
    milestone_shadow_enabled,
)
from services.api.app.application.app_graph_recapture import (
    APP_GRAPH_RECAPTURE_VERSION,
    RecaptureEquivalenceMode,
    assess_recapture_equivalence_gate,
    attach_app_m6_recapture_shadow,
    build_app_capture_series_lineage,
    build_app_recapture_plan_shadow,
    build_lab_m6_recapture_milestone_policy,
    plan_app_graph_recovery_routes,
    resolve_app_m6_recapture_active,
    resolve_app_m6_runtime_ready,
)
from services.api.app.application.supplemental_evidence_policy import (
    build_current_supplemental_evidence_policy,
)


def _scoped_graph() -> dict:
    scope = {"task_id": "t", "agent_run_id": "r", "finalization_epoch": 1}
    return {
        **scope,
        "states": [
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
            },
        ],
        "transitions": [
            {
                **scope,
                "transition_id": "e1",
                "source_state_id": "s1",
                "target_state_id": "s2",
                "status": "completed",
                "proof": "trusted",
                "counts_as_business_arrival": True,
                "replay_eligible": True,
                "risk_level": "low",
            }
        ],
    }


def test_m6_production_default_shadow_without_runtime_ready() -> None:
    policy = build_app_bounded_exploration_milestone_policy()
    assert milestone_shadow_enabled(policy, "M6") is True
    assert milestone_authority_enabled(policy, "M6") is False
    assert resolve_app_m6_recapture_active(policy) is True
    assert resolve_app_m6_runtime_ready(policy) is False


def test_m6_lab_enables_runtime_ready_declaration() -> None:
    policy = build_lab_m6_recapture_milestone_policy()
    assert resolve_app_m6_runtime_ready(policy) is True
    assert resolve_app_m6_recapture_active(policy) is True


def test_m6_route_planner_requires_stepwise_reverification() -> None:
    planned = plan_app_graph_recovery_routes(
        graph=_scoped_graph(),
        source_state_id="s1",
        target_identity="page:detail",
    )
    assert planned["status"] == "available"
    assert planned["requires_stepwise_reverification"] is True
    assert planned["executes_device_actions"] is False
    assert planned["cross_epoch_coordinate_replay_forbidden"] is True
    assert planned["routes"][0]["step_count"] == 1


def test_m6_equivalence_rejects_fresh_for_exact_gap_and_high_risk() -> None:
    blocked = assess_recapture_equivalence_gate(
        declared_mode=RecaptureEquivalenceMode.FRESH,
        filling_exact_gap=True,
    )
    assert blocked["allowed"] is False
    assert "fresh_cannot_fill_exact_gap" in blocked["reason_codes"]

    pay = assess_recapture_equivalence_gate(
        declared_mode=RecaptureEquivalenceMode.EQUIVALENT,
        rule_code="payment_checkout",
    )
    assert pay["allowed"] is False
    assert pay["high_risk"] is True


def test_m6_capture_series_not_independent_arrival() -> None:
    series = build_app_capture_series_lineage(
        parent_capture_id="cap-parent",
        frame_capture_ids=("cap-1", "cap-2"),
        gap_codes=("below_fold_missing",),
    )
    assert series["counts_as_independent_business_arrival"] is False
    assert series["requires_parent_lineage"] is True
    assert series["parent_capture_id"] == "cap-parent"


def test_m6_plan_keeps_production_runtime_ready_false() -> None:
    plan = build_app_recapture_plan_shadow(
        evidence_request_id="req-1",
        target_identity="page:detail",
        declared_mode="exact",
        routes=plan_app_graph_recovery_routes(
            graph=_scoped_graph(),
            source_state_id="s1",
            target_identity="page:detail",
        ),
    )
    assert plan["protocol_version"] == APP_GRAPH_RECAPTURE_VERSION
    assert plan["runtime_ready"] is False
    assert plan["can_execute"] is False
    assert plan["executes_probe"] is False
    assert plan["supplemental_policy"]["runtime_ready"] is False
    assert plan["supplemental_policy"]["lab_injected"] is False
    assert build_current_supplemental_evidence_policy(enabled=True).runtime_ready is False


def test_m6_lab_plan_can_execute_without_auto_probe() -> None:
    plan = build_app_recapture_plan_shadow(
        evidence_request_id="req-lab",
        target_identity="page:detail",
        declared_mode="exact",
        routes=plan_app_graph_recovery_routes(
            graph=_scoped_graph(),
            source_state_id="s1",
            target_identity="page:detail",
        ),
        capture_series=build_app_capture_series_lineage(parent_capture_id="cap-p"),
        milestone_policy=build_lab_m6_recapture_milestone_policy(),
    )
    assert plan["runtime_ready"] is True
    assert plan["can_execute"] is True
    assert plan["executes_probe"] is False
    assert "probe_not_auto_dispatched" in plan["reason_codes"]
    assert plan["supplemental_policy"]["lab_injected"] is True


def test_m6_lab_plan_blocks_fresh_exact_gap_even_when_ready() -> None:
    plan = build_app_recapture_plan_shadow(
        target_identity="page:detail",
        declared_mode="fresh",
        filling_exact_gap=True,
        routes=plan_app_graph_recovery_routes(
            graph=_scoped_graph(),
            source_state_id="s1",
            target_identity="page:detail",
        ),
        milestone_policy=build_lab_m6_recapture_milestone_policy(),
    )
    assert plan["runtime_ready"] is True
    assert plan["can_execute"] is False
    assert "equivalence_gate_blocked" in plan["reason_codes"]


def test_m6_attach_writes_shadow() -> None:
    meta = attach_app_m6_recapture_shadow(
        {"business_page_identity": "page:detail"},
        graph=_scoped_graph(),
        source_state_id="s1",
        target_identity="page:detail",
        parent_capture_id="cap-p",
        frame_capture_ids=["cap-a"],
        declared_mode="exact",
    )
    shadow = meta["app_graph_recapture_shadow"]
    assert shadow["active"] is True
    assert shadow["runtime_ready"] is False
    assert shadow["routes"]["status"] == "available"
    assert shadow["capture_series"]["parent_capture_id"] == "cap-p"

    lab_meta = attach_app_m6_recapture_shadow(
        {"business_page_identity": "page:detail"},
        graph=_scoped_graph(),
        source_state_id="s1",
        target_identity="page:detail",
        parent_capture_id="cap-p",
        declared_mode="exact",
        milestone_policy=build_lab_m6_recapture_milestone_policy(),
    )
    lab_shadow = lab_meta["app_graph_recapture_shadow"]
    assert lab_shadow["runtime_ready"] is True
    assert lab_shadow["can_execute"] is True
    assert lab_shadow["executes_probe"] is False
