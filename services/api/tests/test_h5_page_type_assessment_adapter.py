"""H5 页面识别到页面类型评估合同的适配测试。"""

from datetime import UTC, datetime

import pytest

from services.api.app.application.h5_page_type_assessment_adapter import (
    adapt_h5_page_detection_to_assessment,
)

_NOW = datetime(2026, 8, 10, 18, 0, tzinfo=UTC)


def test_recognized_business_page_builds_covered_assessment_payload() -> None:
    result = adapt_h5_page_detection_to_assessment(
        task_id="task-1",
        agent_run_id="run-1",
        detection_result={
            "page_capture_id": "capture-home",
            "detection_status": "recognized",
            "page_type": "home",
            "page_state": "stable",
            "confidence": 0.92,
            "evidence_artifact_ids": ["artifact-dom", "artifact-shot"],
        },
        assessed_at=_NOW,
    )

    assert result.execution_status == "accepted"
    assert result.page_type_assessment == {
        "page_type": "home",
        "status": "covered",
        "reason_code": "h5_page_detection_covered",
        "page_capture_ids": ["capture-home"],
        "evidence_artifact_ids": ["artifact-dom", "artifact-shot"],
        "exploration_action_ids": [],
        "page_states": ["stable"],
    }


def test_low_confidence_business_page_is_evidence_insufficient() -> None:
    result = adapt_h5_page_detection_to_assessment(
        task_id="task-1",
        agent_run_id="run-1",
        detection_result={
            "page_capture_id": "capture-home",
            "detection_status": "recognized",
            "page_type": "home",
            "page_state": "stable",
            "confidence": 0.42,
            "evidence_artifact_ids": ["artifact-home"],
        },
        assessed_at=_NOW,
    )

    assert result.execution_status == "accepted"
    assert result.page_type_assessment is not None
    assert result.page_type_assessment["status"] == "evidence_insufficient"
    assert result.page_type_assessment["reason_code"] == "h5_page_detection_low_confidence"


@pytest.mark.parametrize(
    ("proof", "status", "confidence", "bound", "allowed"),
    [
        ("trusted", "recognized", 0.72, True, True),
        ("trusted", "recognized", 0.69, True, False),
        ("uncertain", "recognized", 0.95, True, False),
        ("trusted", "ambiguous", 0.95, True, False),
        ("trusted", "recognized", 0.95, False, False),
    ],
)
def test_visual_navigation_never_bypasses_page_coverage_gate(
    proof: str,
    status: str,
    confidence: float,
    bound: bool,
    allowed: bool,
) -> None:
    """视觉转换只能贡献转换证明，不能替代实际 after 页型及身份门禁。"""
    module = __import__(
        "services.api.app.application.h5_page_type_assessment_adapter",
        fromlist=["assess_after_visual_navigation"],
    )

    assessment = module.assess_after_visual_navigation(
        logical_transition_proof=proof,
        detection_status=status,
        page_type_confidence=confidence,
        identity_bound=bound,
    )

    assert assessment.coverage_allowed is allowed


def test_spa_assessment_requires_trusted_logical_transition() -> None:
    identity = "sha256:" + "1" * 64
    result = adapt_h5_page_detection_to_assessment(
        task_id="task-1",
        agent_run_id="run-1",
        detection_result={
            "page_capture_id": "capture-profile",
            "detection_status": "recognized",
            "page_type": "profile",
            "page_state": "stable",
            "confidence": 0.92,
            "evidence_artifact_ids": ["artifact-profile"],
            "logical_surface": {
                "protocol_version": "logical_surface.v1",
                "shadow": False,
                "render_profile": {
                    "kind": "dom_spa",
                    "status": "confirmed",
                    "confidence": 0.94,
                },
                "identity_fingerprint": identity,
                "logical_transition_proof": "uncertain",
                "page_type_binding": {
                    "page_type": "profile",
                    "identity_fingerprint": identity,
                    "page_type_confidence": 0.92,
                    "coverage_allowed": False,
                    "reason_code": "logical_transition_untrusted",
                },
            },
        },
        assessed_at=_NOW,
    )

    assert result.execution_status == "accepted"
    assert result.page_type_assessment is not None
    assert result.page_type_assessment["status"] == "evidence_insufficient"
    assert result.page_type_assessment["reason_code"] == ("h5_logical_transition_untrusted")


def test_spa_assessment_rejects_page_type_bound_to_another_identity() -> None:
    result = adapt_h5_page_detection_to_assessment(
        task_id="task-1",
        agent_run_id="run-1",
        detection_result={
            "page_capture_id": "capture-profile",
            "detection_status": "recognized",
            "page_type": "profile",
            "page_state": "stable",
            "confidence": 0.92,
            "evidence_artifact_ids": ["artifact-profile"],
            "logical_surface": {
                "protocol_version": "logical_surface.v1",
                "shadow": False,
                "render_profile": {
                    "kind": "flutter_web",
                    "status": "confirmed",
                    "confidence": 0.96,
                },
                "identity_fingerprint": "sha256:" + "1" * 64,
                "logical_transition_proof": "trusted",
                "page_type_binding": {
                    "page_type": "profile",
                    "identity_fingerprint": "sha256:" + "2" * 64,
                    "page_type_confidence": 0.92,
                    "coverage_allowed": True,
                },
            },
        },
        assessed_at=_NOW,
    )

    assert result.page_type_assessment is not None
    assert result.page_type_assessment["status"] == "evidence_insufficient"
    assert result.page_type_assessment["reason_code"] == "h5_logical_identity_unbound"


def test_shadow_logical_surface_does_not_change_existing_assessment() -> None:
    result = adapt_h5_page_detection_to_assessment(
        task_id="task-1",
        agent_run_id="run-1",
        detection_result={
            "page_capture_id": "capture-home",
            "detection_status": "recognized",
            "page_type": "home",
            "page_state": "stable",
            "confidence": 0.92,
            "evidence_artifact_ids": ["artifact-home"],
            "logical_surface": {
                "protocol_version": "logical_surface.v1",
                "shadow": True,
                "render_profile": {
                    "kind": "dom_spa",
                    "status": "confirmed",
                    "confidence": 0.94,
                },
                "identity_fingerprint": "sha256:" + "1" * 64,
                "logical_transition_proof": "insufficient",
            },
        },
        assessed_at=_NOW,
    )

    assert result.page_type_assessment is not None
    assert result.page_type_assessment["status"] == "covered"
    assert result.page_type_assessment["reason_code"] == "h5_page_detection_covered"


def test_auth_required_business_page_is_requires_auth() -> None:
    result = adapt_h5_page_detection_to_assessment(
        task_id="task-1",
        agent_run_id="run-1",
        detection_result={
            "page_capture_id": "capture-profile",
            "detection_status": "recognized",
            "page_type": "profile",
            "page_state": "login_required",
            "confidence": 0.91,
            "risk_flags": ["authentication_required"],
            "evidence_artifact_ids": ["artifact-profile"],
        },
        assessed_at=_NOW,
    )

    assert result.execution_status == "accepted"
    assert result.page_type_assessment is not None
    assert result.page_type_assessment["status"] == "requires_auth"
    assert result.page_type_assessment["reason_code"] == "h5_page_requires_auth"


def test_home_popup_with_trusted_close_candidate_is_not_requires_auth() -> None:
    result = adapt_h5_page_detection_to_assessment(
        task_id="task-1",
        agent_run_id="run-1",
        detection_result={
            "page_capture_id": "capture-home-popup",
            "detection_status": "recognized",
            "page_type": "home",
            "page_state": "popup",
            "confidence": 0.93,
            "risk_flags": ["authentication_required", "ads_present"],
            "candidate_actions": [
                {
                    "action": "click",
                    "target": "关闭当前弹窗",
                    "target_keywords": [
                        "popup_dismissal",
                        "close_control",
                        "dialog_overlay",
                    ],
                    "risk_level": "low",
                    "purpose": "关闭当前遮挡弹窗并在同一浏览器会话重新采集",
                    "navigation_depth": 0,
                    "expected_next_page_type": "home",
                    "obstruction_signature": (
                        "h5-obstruction:"
                        "85ee927720e98ac1c7e9002bef17ff28980f548837f56b80b0c471f8a3d9b407"
                    ),
                }
            ],
            "evidence_artifact_ids": ["artifact-home-popup"],
        },
        assessed_at=_NOW,
    )

    assert result.execution_status == "accepted"
    assert result.page_type_assessment is not None
    assert result.page_type_assessment["status"] == "covered"
    assert result.page_type_assessment["reason_code"] == "h5_page_detection_covered"
    assert result.page_type_assessment["page_states"] == ["popup"]


def test_safety_blocked_business_page_is_blocked_by_safety() -> None:
    result = adapt_h5_page_detection_to_assessment(
        task_id="task-1",
        agent_run_id="run-1",
        detection_result={
            "page_capture_id": "capture-home",
            "detection_status": "recognized",
            "page_type": "home",
            "page_state": "safety_interstitial",
            "confidence": 0.91,
            "risk_flags": ["deterministic_safety_interstitial"],
            "evidence_artifact_ids": ["artifact-home"],
        },
        assessed_at=_NOW,
    )

    assert result.execution_status == "accepted"
    assert result.page_type_assessment is not None
    assert result.page_type_assessment["status"] == "blocked_by_safety"
    assert result.page_type_assessment["reason_code"] == "h5_page_blocked_by_safety"


def test_safety_blocked_without_page_type_fails_with_specific_non_retryable_error() -> None:
    result = adapt_h5_page_detection_to_assessment(
        task_id="task-1",
        agent_run_id="run-1",
        detection_result={
            "page_capture_id": "capture-safety",
            "detection_status": "ambiguous",
            "page_type": None,
            "page_state": "safety_interstitial",
            "confidence": 1,
            "risk_flags": ["safety_interstitial", "automation_blocked"],
            "evidence_artifact_ids": ["artifact-safety"],
        },
        assessed_at=_NOW,
    )

    assert result.execution_status == "blocked"
    assert result.error_code == "PAGE_TYPE_ASSESSMENT_BLOCKED_BY_SAFETY"
    assert result.retryable is False


def test_lifecycle_page_type_is_skipped_without_business_assessment() -> None:
    result = adapt_h5_page_detection_to_assessment(
        task_id="task-1",
        agent_run_id="run-1",
        detection_result={
            "page_capture_id": "capture-startup",
            "detection_status": "recognized",
            "page_type": "startup_ad",
            "page_state": "countdown",
            "confidence": 0.99,
            "evidence_artifact_ids": ["artifact-startup"],
        },
        assessed_at=_NOW,
    )

    assert result.execution_status == "skipped"
    assert result.page_type_assessment is None


def test_unknown_page_type_fails_closed() -> None:
    result = adapt_h5_page_detection_to_assessment(
        task_id="task-1",
        agent_run_id="run-1",
        detection_result={
            "page_capture_id": "capture-unknown",
            "detection_status": "recognized",
            "page_type": "unknown_detail",
            "page_state": "stable",
            "confidence": 0.9,
            "evidence_artifact_ids": ["artifact-unknown"],
        },
        assessed_at=_NOW,
    )

    assert result.execution_status == "blocked"
    assert result.error_code == "PAGE_TYPE_ASSESSMENT_UNKNOWN_PAGE_TYPE"
    assert result.retryable is True


def test_uncatalogued_page_skip_does_not_require_page_type_assessment() -> None:
    result = adapt_h5_page_detection_to_assessment(
        task_id="task-1",
        agent_run_id="run-1",
        detection_result={
            "page_capture_id": "capture-uncatalogued",
            "detection_status": "unrecognized",
            "page_type": None,
            "catalog_disposition": "skipped",
            "catalog_skip_reason": "uncatalogued_page_type",
        },
        assessed_at=_NOW,
    )

    assert result.execution_status == "skipped"
    assert result.page_type_assessment is None
    assert result.error_code is None


def test_trusted_popup_without_page_type_defers_assessment_until_dismissed() -> None:
    result = adapt_h5_page_detection_to_assessment(
        task_id="task-1",
        agent_run_id="run-1",
        detection_result={
            "page_capture_id": "capture-popup",
            "detection_status": "ambiguous",
            "page_type": None,
            "page_state": "popup",
            "confidence": 0.91,
            "risk_flags": ["ads_present"],
            "candidate_actions": [
                {
                    "action": "click",
                    "target": "关闭当前弹窗",
                    "target_keywords": ["popup_dismissal", "close_control"],
                    "risk_level": "low",
                    "navigation_depth": 0,
                    "obstruction_signature": "h5-obstruction:" + "a" * 64,
                }
            ],
            "evidence_artifact_ids": ["artifact-popup"],
        },
        assessed_at=_NOW,
    )

    assert result.execution_status == "skipped"
    assert result.page_type_assessment is None
    assert result.error_code is None
    assert result.retryable is False
    assert "关闭弹窗并重新采集" in (result.message or "")


@pytest.mark.parametrize(
    "metadata",
    [
        {},
        {"catalog_disposition": "skipped"},
        {
            "catalog_disposition": "skipped",
            "catalog_skip_reason": "forged_reason",
        },
    ],
)
def test_missing_page_type_without_exact_catalog_skip_fails_closed(
    metadata: dict[str, str],
) -> None:
    result = adapt_h5_page_detection_to_assessment(
        task_id="task-1",
        agent_run_id="run-1",
        detection_result={
            "page_capture_id": "capture-missing-page-type",
            "detection_status": "unrecognized",
            "page_type": None,
            **metadata,
        },
        assessed_at=_NOW,
    )

    assert result.execution_status == "blocked"
    assert result.error_code == "PAGE_TYPE_ASSESSMENT_PAGE_TYPE_REQUIRED"


def test_missing_capture_or_evidence_fails_closed() -> None:
    missing_capture = adapt_h5_page_detection_to_assessment(
        task_id="task-1",
        agent_run_id="run-1",
        detection_result={
            "detection_status": "recognized",
            "page_type": "home",
            "page_state": "stable",
            "confidence": 0.9,
            "evidence_artifact_ids": ["artifact-home"],
        },
        assessed_at=_NOW,
    )
    missing_evidence = adapt_h5_page_detection_to_assessment(
        task_id="task-1",
        agent_run_id="run-1",
        detection_result={
            "page_capture_id": "capture-home",
            "detection_status": "recognized",
            "page_type": "home",
            "page_state": "stable",
            "confidence": 0.9,
            "evidence_artifact_ids": [],
        },
        assessed_at=_NOW,
    )

    assert missing_capture.execution_status == "blocked"
    assert missing_capture.error_code == "PAGE_TYPE_ASSESSMENT_CAPTURE_REQUIRED"
    assert missing_evidence.execution_status == "blocked"
    assert missing_evidence.error_code == "PAGE_TYPE_ASSESSMENT_EVIDENCE_REQUIRED"
