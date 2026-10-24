"""M1 加深：入口层协议与 Settle 审计捆回归。"""

from __future__ import annotations

from services.api.app.application.app_bounded_exploration_milestones import (
    build_app_bounded_exploration_milestone_policy,
    build_production_authority_rollout_checklist,
)
from services.api.app.application.app_capture_readiness_diagnosis import (
    APP_CAPTURE_READINESS_DIAGNOSIS_VERSION,
    APP_ENTRY_LAYER_VERSION,
    APP_SETTLE_ENFORCEMENT_PREVIEW_VERSION,
    SettleReadinessProfile,
    build_app_capture_readiness_diagnosis,
    build_m1_settle_audit_bundle,
    build_m1_settle_enforcement_preview,
    build_settle_shadow_manifest_audit,
    classify_app_entry_layer,
)


def test_entry_layer_never_equates_app_ready_with_business_home() -> None:
    startup = classify_app_entry_layer(page_state="startup_ad")
    assert startup["protocol_version"] == APP_ENTRY_LAYER_VERSION
    assert startup["kind"] == "startup_ad"
    assert startup["app_ready_equals_business_home"] is False
    assert startup["underlying_home_fabricated"] is False
    assert startup["counts_as_business_home_arrival"] is False
    assert startup["blocks_business_navigation"] is True
    assert "app_ready_not_business_home" in startup["reason_codes"]

    auth = classify_app_entry_layer(
        page_state="stable",
        surface_facts={"auth_gate_visible": True},
    )
    assert auth["kind"] == "auth_gate"
    assert auth["blocks_business_navigation"] is True

    surface = classify_app_entry_layer(page_state="stable")
    assert surface["kind"] == "business_surface"
    assert surface["blocks_business_navigation"] is False
    assert surface["app_ready_equals_business_home"] is False
    assert surface["counts_as_business_home_arrival"] is False


def test_m1_settle_blocks_startup_and_keeps_formal_oracle_false() -> None:
    blocked = build_app_capture_readiness_diagnosis(
        page_state="startup_ad",
        has_confirmed_visual_identity=True,
        profile=SettleReadinessProfile.ACTION_AFTER,
    )
    assert blocked["protocol_version"] == APP_CAPTURE_READINESS_DIAGNOSIS_VERSION
    assert blocked["formal_settle_oracle"] is False
    assert blocked["ready_for_business_evidence"] is False
    assert blocked["entry_layer"]["kind"] == "startup_ad"
    assert blocked["settle_shadow"]["gate_consumes_settle"] is False

    ready = build_app_capture_readiness_diagnosis(
        page_state="stable",
        has_confirmed_visual_identity=True,
        profile=SettleReadinessProfile.REOBSERVE,
    )
    assert ready["ready_for_business_evidence"] is True
    assert ready["entry_layer"]["kind"] == "business_surface"
    assert ready["formal_settle_oracle"] is False
    assert ready["network_observable"] == "not_observable"


def test_m1_settle_audit_bundle_is_gate_safe() -> None:
    after = build_app_capture_readiness_diagnosis(
        page_state="popup",
        overlay_signature="ad:1",
        has_confirmed_visual_identity=True,
        profile=SettleReadinessProfile.ACTION_AFTER,
    )
    reobserve = build_app_capture_readiness_diagnosis(
        page_state="popup",
        overlay_signature="ad:1",
        has_confirmed_visual_identity=True,
        profile=SettleReadinessProfile.REOBSERVE,
    )
    bundle = build_m1_settle_audit_bundle(
        action_after=after,
        reobserve=reobserve,
        entry_layer=after["entry_layer"],  # type: ignore[arg-type]
    )
    assert bundle["formal_settle_oracle"] is False
    assert bundle["gate_consumes_settle"] is False
    assert bundle["app_ready_equals_business_home"] is False
    assert bundle["underlying_home_fabricated"] is False
    assert bundle["entry_layer"]["kind"] == "overlay"
    audit = build_settle_shadow_manifest_audit(after)
    assert audit["entry_layer"]["kind"] == "overlay"
    assert audit["gate_consumes_settle"] is False


def test_m1_authority_enables_formal_oracle_and_enforcement_preview() -> None:
    policy = build_app_bounded_exploration_milestone_policy(
        {"authority": {"M1": True}},
        apply_production_env=False,
    )
    blocked = build_app_capture_readiness_diagnosis(
        page_state="startup_ad",
        has_confirmed_visual_identity=True,
        profile=SettleReadinessProfile.ACTION_AFTER,
        milestone_policy=policy,
    )
    assert blocked["formal_settle_oracle"] is True
    assert blocked["settle_shadow"]["gate_consumes_settle"] is False
    preview = blocked["settle_enforcement_preview"]
    assert preview["protocol_version"] == APP_SETTLE_ENFORCEMENT_PREVIEW_VERSION
    assert preview["formal_settle_oracle"] is True
    assert preview["would_block_business_evidence"] is True
    assert preview["would_block_if_enforced"] is True
    assert preview["gate_consumes_settle"] is False
    assert preview["mutates_completion_gate_sql"] is False

    ready = build_app_capture_readiness_diagnosis(
        page_state="stable",
        has_confirmed_visual_identity=True,
        profile=SettleReadinessProfile.REOBSERVE,
        milestone_policy=policy,
        package_name="com.example.app",
        image_signature="sha256:" + ("d" * 64),
        surface_facts={
            "trusted_accessibility_controls": {
                "bottom_navigation": [{"label": "首页"}],
            }
        },
    )
    assert ready["formal_settle_oracle"] is True
    assert ready["ready_for_business_evidence"] is True
    ready_preview = build_m1_settle_enforcement_preview(ready)
    assert ready_preview["would_block_business_evidence"] is False
    assert ready_preview["would_block_if_enforced"] is False
    assert ready_preview["gate_consumes_settle"] is False


def test_production_checklist_includes_m1_settle_review() -> None:
    checklist = build_production_authority_rollout_checklist()
    m1 = next(item for item in checklist["steps"] if item["milestone"] == "M1")
    assert "no_forged_network_stable" in m1["must_prove"]
    assert "gate_consumes_settle" in m1["explicitly_not_approved"]
    assert "gate_consumes_settle_without_m1_review" in checklist["forbidden"]
