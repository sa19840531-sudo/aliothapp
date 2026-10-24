"""App 证据角色合同单测。"""

from services.api.app.application.app_evidence_role_contract import (
    APP_EVIDENCE_ROLE_CONTRACT_VERSION,
    ROLE_AD_OUTBOUND_LANDING,
    ROLE_LONG_PAGE_SCROLL_SERIES,
    ROLE_PLAYER_SCRUB_SERIES,
    ROLE_STITCHED_OVERVIEW_DERIVED,
    build_ad_outbound_triplet_evidence,
    build_checkout_chain_step_evidence,
    build_long_page_scroll_series_evidence,
    build_player_scrub_series_evidence,
    build_player_wake_before_capture_hint,
    build_stitched_overview_derived_evidence,
    build_viewport_primary_evidence,
    role_counts_as_business_arrival,
    role_is_derived_only,
    validate_app_evidence_record,
)


def test_viewport_primary_is_arrival_candidate() -> None:
    record = build_viewport_primary_evidence(capture_id="cap-1")
    assert record["evidence_role"] == "viewport_primary"
    assert record["counts_as_independent_business_arrival"] is True
    assert validate_app_evidence_record(record) == ()


def test_long_page_series_forbids_stitched_primary_and_arrival() -> None:
    record = build_long_page_scroll_series_evidence(
        parent_capture_id="cap-parent",
        frame_capture_ids=("cap-a", "cap-b"),
        gap_codes=("scroll_overlap_uncertain",),
    )
    assert record["evidence_role"] == ROLE_LONG_PAGE_SCROLL_SERIES
    assert record["counts_as_independent_business_arrival"] is False
    assert record["forbids_stitched_as_primary"] is True
    assert role_counts_as_business_arrival(ROLE_LONG_PAGE_SCROLL_SERIES) is False
    assert "scroll_overlap_uncertain" in validate_app_evidence_record(record)


def test_stitched_overview_is_derived_only() -> None:
    record = build_stitched_overview_derived_evidence(
        parent_capture_id="cap-parent",
        source_frame_capture_ids=("cap-a", "cap-b"),
        derived_artifact_id="art-stitch",
    )
    assert record["evidence_role"] == ROLE_STITCHED_OVERVIEW_DERIVED
    assert role_is_derived_only(ROLE_STITCHED_OVERVIEW_DERIVED) is True
    assert record["forbids_as_judge_primary"] is True
    assert validate_app_evidence_record(record) == ()


def test_ad_outbound_triplet_requires_source_landing_return() -> None:
    incomplete = build_ad_outbound_triplet_evidence(
        source_capture_id="cap-src",
        external_activity_kind="browser",
    )
    assert incomplete["complete"] is False
    gaps = validate_app_evidence_record(incomplete)
    assert "ad_outbound_landing_missing" in gaps
    assert "ad_outbound_return_missing" in gaps

    complete = build_ad_outbound_triplet_evidence(
        source_capture_id="cap-src",
        landing_capture_id="cap-land",
        return_capture_id="cap-ret",
        external_activity_kind="browser",
    )
    assert complete["complete"] is True
    assert complete["landing"]["evidence_role"] == ROLE_AD_OUTBOUND_LANDING
    assert complete["landing"]["counts_as_business_coverage"] is False
    assert validate_app_evidence_record(complete) == ()


def test_checkout_chain_rejects_outside_package_and_unknown_step() -> None:
    bad = build_checkout_chain_step_evidence(
        step_key="unknown_step",
        capture_id="cap-1",
        chain_id="chain-1",
        step_index=0,
        in_target_package=False,
        high_risk_confirm=True,
    )
    gaps = validate_app_evidence_record(bad)
    assert "checkout_step_key_unknown" in gaps
    assert "checkout_step_outside_target_package" in gaps
    assert "checkout_high_risk_confirm_not_advanced" in gaps

    ok = build_checkout_chain_step_evidence(
        step_key="in_app_cashier",
        capture_id="cap-2",
        chain_id="chain-1",
        step_index=2,
        in_target_package=True,
    )
    assert ok["complete"] is True
    assert validate_app_evidence_record(ok) == ()


def test_player_wake_before_capture_hint_is_not_business_arrival() -> None:
    hint = build_player_wake_before_capture_hint()
    assert hint["evidence_role"] == ROLE_PLAYER_SCRUB_SERIES
    assert hint["wake_player_controls_before_capture"] is True
    assert hint["counts_as_independent_business_arrival"] is False
    bounds = hint["normalized_bounds"]
    assert 0.0 < float(bounds["left"]) < float(bounds["right"]) < 1.0
    assert 0.0 < float(bounds["top"]) < float(bounds["bottom"]) < 1.0


def test_player_scrub_requires_before_during_after() -> None:
    incomplete = build_player_scrub_series_evidence(
        parent_capture_id="cap-parent",
        before_capture_id="cap-before",
        during_capture_ids=(),
        after_capture_id="",
    )
    assert incomplete["evidence_role"] == ROLE_PLAYER_SCRUB_SERIES
    assert incomplete["single_frame_insufficient"] is True
    gaps = validate_app_evidence_record(incomplete)
    assert "player_scrub_during_missing" in gaps
    assert "player_scrub_after_missing" in gaps

    complete = build_player_scrub_series_evidence(
        parent_capture_id="cap-parent",
        before_capture_id="cap-before",
        during_capture_ids=("cap-mid-1", "cap-mid-2"),
        after_capture_id="cap-after",
        control_semantic="进度条拖动",
    )
    assert complete["protocol_version"] == APP_EVIDENCE_ROLE_CONTRACT_VERSION
    assert complete["complete"] is True
    assert validate_app_evidence_record(complete) == ()
