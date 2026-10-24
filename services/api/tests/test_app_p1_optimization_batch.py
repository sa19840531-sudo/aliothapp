"""App 判别指南、覆盖-图影子对账、就绪诊断与补采门禁回归。"""

from __future__ import annotations

from services.api.app.application.app_bounded_exploration_milestones import (
    APP_BOUNDED_EXPLORATION_MILESTONE_VERSION,
    APP_MILESTONE_ORDER,
    build_app_bounded_exploration_milestone_policy,
    milestone_authority_enabled,
    milestone_shadow_enabled,
)
from services.api.app.application.app_capture_readiness_diagnosis import (
    APP_CAPTURE_READINESS_DIAGNOSIS_VERSION,
    APP_SETTLE_SHADOW_VERSION,
    build_app_capture_readiness_diagnosis,
    classify_app_presentation_layer,
)
from services.api.app.application.page_graph_projection import (
    reconcile_coverage_with_page_graph,
)
from services.api.app.application.supplemental_evidence_policy import (
    build_current_supplemental_evidence_policy,
    build_lab_supplemental_evidence_policy,
)
from services.api.app.domain.page.discrimination_guide import (
    APP_PAGE_DISCRIMINATION_GUIDE_VERSION,
    build_app_page_discrimination_guide,
    format_app_page_discrimination_guide_for_prompt,
)
from services.api.app.domain.page.registry import get_default_page_type_registry
from services.api.app.infrastructure.app_vision.llm import _system_prompt


def test_discrimination_guide_is_versioned_and_includes_confusable_pairs() -> None:
    guide = build_app_page_discrimination_guide()
    assert guide["protocol_version"] == APP_PAGE_DISCRIMINATION_GUIDE_VERSION
    assert guide["guide_hash"]
    assert any(item["code"] == "home" for item in guide["entries"])
    assert any(
        pair["left"] == "content_list" and pair["right"] == "content_detail"
        for pair in guide["confusable_pairs"]
    )
    text = format_app_page_discrimination_guide_for_prompt(guide)
    assert "content_list vs content_detail" in text
    assert "页型可修订" in text


def test_app_vision_system_prompt_embeds_discrimination_guide() -> None:
    prompt = _system_prompt(
        page_codes=("home", "content_list"),
        allowed_actions=("click", "back"),
        max_candidates=3,
        page_types=get_default_page_type_registry(),
    )
    assert "判别指南" in prompt
    assert APP_PAGE_DISCRIMINATION_GUIDE_VERSION in prompt
    assert "不得用页型改写业务身份" in prompt


def test_coverage_page_graph_shadow_reconcile_does_not_change_authority() -> None:
    summary = reconcile_coverage_with_page_graph(
        coverage_metadata={
            "covered_business_state_ids": ("page:home", "page:detail"),
        },
        graph={
            "task_id": "t",
            "agent_run_id": "r",
            "finalization_epoch": 1,
            "states": [
                {
                    "task_id": "t",
                    "agent_run_id": "r",
                    "finalization_epoch": 1,
                    "state_id": "c1",
                    "logical_page_key": "page:home",
                    "state_fingerprint": "s",
                    "identity_status": "confirmed",
                    "counts_as_business_page": True,
                    "is_overlay": False,
                    "is_external": False,
                }
            ],
            "transitions": [
                {
                    "task_id": "t",
                    "agent_run_id": "r",
                    "finalization_epoch": 1,
                    "transition_id": "e1",
                    "source_state_id": "c0",
                    "target_state_id": "c1",
                    "status": "completed",
                    "proof": "trusted",
                    "counts_as_business_arrival": True,
                }
            ],
        },
    )
    assert summary["coverage_authority_changed"] is False
    assert summary["schedules_from_graph"] is False
    assert summary["gate_consumes_graph"] is False
    assert summary["matched_page_keys"] == ["page:home"]
    assert summary["only_in_coverage"] == ["page:detail"]
    assert summary["aligned"] is False


def test_m2_shadow_dual_write_aligns_coverage_and_keeps_overlay_diagnostic() -> None:
    from services.api.app.application.page_graph_projection import (
        attach_app_coverage_page_graph_shadow,
        build_app_page_graph_shadow_snapshot,
    )

    meta = attach_app_coverage_page_graph_shadow(
        {
            "covered_business_state_ids": ["pkg|act|vid:home", "pkg|act|vid:list"],
            "business_page_identity": "pkg|act|vid:list",
        },
        current_frame={
            "capture_id": "cap-overlay-1",
            "page_state": "popup",
            "overlay_signature": "ad:1",
            "package_within_boundary": True,
            "business_page_identity": "pkg|act|vid:list",
            "stable_business_page": False,
        },
    )
    snapshot = meta["app_page_graph_snapshot"]
    shadow = meta["app_coverage_page_graph_shadow"]
    assert snapshot["status"] == "shadow_dual_write"
    assert snapshot["coverage_authority_changed"] is False
    assert snapshot["schedules_from_graph"] is False
    assert shadow["dual_write"] is True
    assert shadow["aligned"] is True
    assert shadow["coverage_authority_changed"] is False
    assert shadow["overlay_state_count"] == 1
    assert "pkg|act|vid:home" in shadow["matched_page_keys"]
    assert any(s.get("is_overlay") for s in snapshot["states"])

    # 幂等：再附加一帧不丢已覆盖页。
    again = attach_app_coverage_page_graph_shadow(
        {**meta, "covered_business_state_ids": ["pkg|act|vid:home", "pkg|act|vid:list"]},
        graph=snapshot,
        current_frame={
            "capture_id": "cap-2",
            "page_state": "stable",
            "package_within_boundary": True,
            "business_page_identity": "pkg|act|vid:list",
            "stable_business_page": True,
        },
    )
    assert again["app_coverage_page_graph_shadow"]["aligned"] is True
    keys = {
        s["logical_page_key"]
        for s in again["app_page_graph_snapshot"]["states"]
        if s.get("counts_as_business_page")
    }
    assert keys == {"pkg|act|vid:home", "pkg|act|vid:list"}

    built = build_app_page_graph_shadow_snapshot(
        coverage_metadata={"covered_business_state_ids": ["only-coverage"]},
        current_frame={
            "capture_id": "ext-1",
            "page_state": "stable",
            "package_within_boundary": False,
        },
    )
    assert any(s.get("is_external") for s in built["states"])


def test_m2_forbids_opening_graph_authority_in_this_batch() -> None:
    from services.api.app.application.page_graph_projection import (
        attach_app_coverage_page_graph_shadow,
    )

    policy = build_app_bounded_exploration_milestone_policy(
        {"authority": {"M2": True}, "shadow": {"M2": True}}
    )
    try:
        attach_app_coverage_page_graph_shadow({}, milestone_policy=policy)
        raise AssertionError("expected ValueError")
    except ValueError as exc:
        assert "M2" in str(exc)


def test_m0_milestone_policy_forbids_agent_direct_execution() -> None:
    policy = build_app_bounded_exploration_milestone_policy()
    assert policy["protocol_version"] == APP_BOUNDED_EXPLORATION_MILESTONE_VERSION
    assert tuple(policy["order"]) == APP_MILESTONE_ORDER
    assert policy["agent_direct_execution_forbidden"] is True
    assert milestone_shadow_enabled(policy, "M1") is True
    assert milestone_authority_enabled(policy, "M1") is False
    assert milestone_authority_enabled(policy, "M2") is False


def test_capture_readiness_diagnosis_blocks_overlay_and_unknown() -> None:
    blocked = build_app_capture_readiness_diagnosis(
        page_state="popup",
        overlay_signature="ad:1",
        surface_facts={"popup_visible": True},
        has_confirmed_visual_identity=True,
    )
    assert blocked["protocol_version"] == APP_CAPTURE_READINESS_DIAGNOSIS_VERSION
    assert blocked["ready_for_business_evidence"] is False
    assert blocked["formal_settle_oracle"] is False
    assert blocked["presentation_layer"] == "overlay"
    assert blocked["evidence_role"] == "overlay_occurrence"
    assert blocked["settle_shadow"]["protocol_version"] == APP_SETTLE_SHADOW_VERSION
    assert blocked["settle_shadow"]["active"] is True
    assert blocked["settle_shadow"]["gate_shadow_only"] is True
    assert "display_layer_blocks_business" in blocked["reason_codes"]

    ready = build_app_capture_readiness_diagnosis(
        page_state="stable",
        has_confirmed_visual_identity=True,
    )
    assert ready["ready_for_business_evidence"] is True
    assert ready["presentation_layer"] == "business_surface"
    assert ready["evidence_role"] == "stable_business_capture"
    assert classify_app_presentation_layer(page_state="loading") == "transitional"


def test_settle_shadow_rejects_identity_sandwich_drift() -> None:
    diagnosis = build_app_capture_readiness_diagnosis(
        page_state="stable",
        has_confirmed_visual_identity=True,
        identity_before={"package_name": "com.example.app", "activity": ".Home"},
        identity_after={"package_name": "com.android.chrome", "activity": ".Main"},
    )
    assert diagnosis["ready_for_business_evidence"] is False
    assert diagnosis["identity_sandwich"]["matched"] is False
    assert "identity_sandwich_drift" in diagnosis["reason_codes"]
    assert diagnosis["evidence_role"] == "diagnostic_only"


def test_production_supplemental_evidence_keeps_runtime_ready_false() -> None:
    policy = build_current_supplemental_evidence_policy(enabled=True)
    assert policy.runtime_ready is False


def test_lab_supplemental_evidence_sets_runtime_ready_true() -> None:
    policy = build_lab_supplemental_evidence_policy(enabled=True)
    assert policy.enabled is True
    assert policy.runtime_ready is True
    policy.validate_runtime_ready()
