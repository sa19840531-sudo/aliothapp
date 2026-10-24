"""需求表 #5：App 页面稳定 quorum 与网络能力缺口回归。"""

from __future__ import annotations

from services.api.app.application.app_capture_readiness_diagnosis import (
    APP_CAPTURE_READINESS_DIAGNOSIS_VERSION,
    APP_PAGE_STABILITY_ASSESSMENT_VERSION,
    SettleReadinessProfile,
    build_app_capture_readiness_diagnosis,
    build_app_network_capability_record,
    build_app_page_stability_assessment,
    build_settle_shadow_manifest_audit,
    compare_app_stability_frames,
    resolve_app_ui_structure_status,
)


def test_network_gap_never_forges_network_stable() -> None:
    missing = build_app_network_capability_record(network_observable=None)
    assert missing["status"] == "unknown"
    assert missing["network_stable"] is False
    assert missing["forged_network_stable"] is False
    assert "network_lifecycle_unavailable" in missing["capability_gaps"]

    closed = build_app_network_capability_record(network_observable=False)
    assert closed["status"] == "not_observable"
    assert closed["network_stable"] is False
    assert "network_lifecycle_unavailable" in closed["capability_gaps"]

    # 即使声明可观测，无 idle 证明也不得 network_stable=true。
    open_ch = build_app_network_capability_record(network_observable=True)
    assert open_ch["status"] == "observable"
    assert open_ch["network_stable"] is False
    assert open_ch["https_lifecycle_assumed"] is False


def test_frame_comparison_marks_change_and_keeps_digests() -> None:
    first = compare_app_stability_frames(
        current_image_signature="sha256:" + ("a" * 64),
        prior_frame_digests=(),
    )
    assert first["status"] == "insufficient_samples"
    assert first["frame_digests"] == ["sha256:" + ("a" * 64)]

    same = compare_app_stability_frames(
        current_image_signature="sha256:" + ("a" * 64),
        prior_frame_digests=first["frame_digests"],
    )
    assert same["status"] == "stable"
    assert same["matched_previous"] is True

    changed = compare_app_stability_frames(
        current_image_signature="sha256:" + ("b" * 64),
        prior_frame_digests=same["frame_digests"],
    )
    assert changed["status"] == "changed"
    assert changed["matched_previous"] is False
    assert "consecutive_frame_digest_changed" in changed["reason_codes"]


def test_ui_structure_sparse_is_unavailable_not_forged() -> None:
    sparse = resolve_app_ui_structure_status(
        {"trusted_accessibility_controls": {"bottom_navigation": [], "node_count": 0}}
    )
    assert sparse["status"] == "unavailable"
    assert sparse["meaningful"] is False

    rich = resolve_app_ui_structure_status(
        {
            "trusted_accessibility_controls": {
                "bottom_navigation": [{"label": "首页"}],
                "bottom_navigation_count": 1,
            }
        }
    )
    assert rich["status"] == "available"
    assert rich["meaningful"] is True


def test_page_stability_records_network_gap_and_blocks_on_frame_change() -> None:
    sig_a = "sha256:" + ("a" * 64)
    sig_b = "sha256:" + ("b" * 64)
    first = build_app_page_stability_assessment(
        package_name="com.example.app",
        activity=".Home",
        page_state="stable",
        has_confirmed_visual_identity=True,
        image_signature=sig_a,
        network_observable=None,
        surface_facts={
            "trusted_accessibility_controls": {
                "bottom_navigation": [{"label": "首页"}],
            }
        },
    )
    assert first["protocol_version"] == APP_PAGE_STABILITY_ASSESSMENT_VERSION
    assert first["stability_status"] == "stable_candidate"
    assert first["screenshot_permitted"] is True
    assert first["network_stable"] is False
    assert "network_lifecycle_unavailable" in first["capability_gaps"]
    assert first["formal_settle_oracle"] is False
    assert first["gate_consumes_settle"] is False

    moved = build_app_page_stability_assessment(
        package_name="com.example.app",
        activity=".Home",
        page_state="stable",
        has_confirmed_visual_identity=True,
        image_signature=sig_b,
        prior_frame_digests=first["frame_digests"],
        network_observable=None,
    )
    assert moved["stability_status"] == "transitional"
    assert moved["stable_business_candidate"] is False
    assert "consecutive_frame_digest_changed" in moved["reason_codes"]


def test_diagnosis_v4_exposes_capability_gaps_and_never_network_stable() -> None:
    diagnosis = build_app_capture_readiness_diagnosis(
        page_state="stable",
        has_confirmed_visual_identity=True,
        package_name="com.example.app",
        activity=".Home",
        image_signature="sha256:" + ("c" * 64),
        profile=SettleReadinessProfile.ACTION_AFTER,
        surface_facts={
            "trusted_accessibility_controls": {
                "bottom_navigation": [{"label": "首页"}],
            }
        },
    )
    assert diagnosis["protocol_version"] == APP_CAPTURE_READINESS_DIAGNOSIS_VERSION
    assert diagnosis["network_observable"] == "not_observable"
    assert diagnosis["network_stable"] is False
    assert "network_lifecycle_unavailable" in diagnosis["capability_gaps"]
    assert diagnosis["ready_for_business_evidence"] is True
    assert diagnosis["settle_shadow"]["network_stable"] is False
    assert diagnosis["settle_shadow"]["gate_consumes_settle"] is False

    audit = build_settle_shadow_manifest_audit(diagnosis)
    assert audit["network_stable"] is False
    assert "network_lifecycle_unavailable" in audit["capability_gaps"]
