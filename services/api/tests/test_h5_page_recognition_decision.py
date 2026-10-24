"""H5 页面类型识别的服务端二次裁决测试。"""

from __future__ import annotations

from services.api.app.agents.tools.page_detection_tools import (
    _apply_h5_recognition_guide_decision,
)
from services.api.app.application.h5_page_recognition_decision import (
    evaluate_h5_page_recognition,
)
from services.api.app.domain.page.catalog import load_default_page_type_catalog
from services.api.app.domain.page.registry import recognition_signal_references


def _signal_ids(page_type: str, field_name: str) -> tuple[str, ...]:
    definition = load_default_page_type_catalog().require_code(page_type)
    return tuple(
        str(item["signal_id"]) for item in recognition_signal_references(definition)[field_name]
    )


def test_model_recognition_requires_all_positive_signals_and_confusable_checks() -> None:
    home = load_default_page_type_catalog().require_code("home")

    decision = evaluate_h5_page_recognition(
        catalog=load_default_page_type_catalog(),
        detection_status="recognized",
        page_type="home",
        matched_required_positive_signal_ids=_signal_ids("home", "required_positive_signals"),
        matched_supporting_signal_ids=(),
        matched_strong_negative_signal_ids=(),
        excluded_confusable_page_types=home.confusable_with,
    )

    assert decision.accepted is True
    assert decision.status == "accepted"
    assert decision.reason_code == "recognition_guide_satisfied"
    assert decision.missing_required_positive_signal_ids == ()
    assert decision.unresolved_confusable_page_types == ()


def test_model_recognition_without_required_positive_signal_is_downgraded() -> None:
    home = load_default_page_type_catalog().require_code("home")

    decision = evaluate_h5_page_recognition(
        catalog=load_default_page_type_catalog(),
        detection_status="recognized",
        page_type="home",
        matched_required_positive_signal_ids=_signal_ids("home", "required_positive_signals")[:1],
        matched_supporting_signal_ids=(),
        matched_strong_negative_signal_ids=(),
        excluded_confusable_page_types=home.confusable_with,
    )

    assert decision.accepted is False
    assert decision.status == "downgraded"
    assert decision.reason_code == "required_positive_signal_missing"
    assert len(decision.missing_required_positive_signal_ids) == 1
    assert decision.missing_evidence_actions == home.missing_evidence_actions


def test_model_recognition_with_strong_negative_signal_is_downgraded() -> None:
    home = load_default_page_type_catalog().require_code("home")

    decision = evaluate_h5_page_recognition(
        catalog=load_default_page_type_catalog(),
        detection_status="recognized",
        page_type="home",
        matched_required_positive_signal_ids=_signal_ids("home", "required_positive_signals"),
        matched_supporting_signal_ids=(),
        matched_strong_negative_signal_ids=_signal_ids("home", "strong_negative_signals")[:1],
        excluded_confusable_page_types=home.confusable_with,
    )

    assert decision.accepted is False
    assert decision.reason_code == "strong_negative_signal_matched"
    assert decision.matched_strong_negative_signal_ids


def test_model_recognition_must_resolve_every_confusable_page_type() -> None:
    home = load_default_page_type_catalog().require_code("home")

    decision = evaluate_h5_page_recognition(
        catalog=load_default_page_type_catalog(),
        detection_status="recognized",
        page_type="home",
        matched_required_positive_signal_ids=_signal_ids("home", "required_positive_signals"),
        matched_supporting_signal_ids=(),
        matched_strong_negative_signal_ids=(),
        excluded_confusable_page_types=home.confusable_with[:-1],
    )

    assert decision.accepted is False
    assert decision.reason_code == "confusable_page_type_unresolved"
    assert decision.unresolved_confusable_page_types == (home.confusable_with[-1],)


def test_deterministic_authority_is_audited_without_model_signal_claims() -> None:
    decision = evaluate_h5_page_recognition(
        catalog=load_default_page_type_catalog(),
        detection_status="recognized",
        page_type="home",
        matched_required_positive_signal_ids=(),
        matched_supporting_signal_ids=(),
        matched_strong_negative_signal_ids=(),
        excluded_confusable_page_types=(),
        deterministic_authority_source="trusted_structure",
    )

    assert decision.accepted is True
    assert decision.status == "accepted"
    assert decision.authority_source == "trusted_structure"
    assert decision.reason_code == "deterministic_authority_accepted"


def test_unknown_signal_reference_is_rejected() -> None:
    home = load_default_page_type_catalog().require_code("home")

    decision = evaluate_h5_page_recognition(
        catalog=load_default_page_type_catalog(),
        detection_status="recognized",
        page_type="home",
        matched_required_positive_signal_ids=(
            *_signal_ids("home", "required_positive_signals"),
            "home.required_positive_signals.unknown",
        ),
        matched_supporting_signal_ids=(),
        matched_strong_negative_signal_ids=(),
        excluded_confusable_page_types=home.confusable_with,
    )

    assert decision.accepted is False
    assert decision.reason_code == "recognition_guide_reference_invalid"
    assert decision.invalid_signal_ids == ("home.required_positive_signals.unknown",)


def test_signal_reference_cannot_be_reported_under_another_category() -> None:
    home = load_default_page_type_catalog().require_code("home")
    required = _signal_ids("home", "required_positive_signals")

    decision = evaluate_h5_page_recognition(
        catalog=load_default_page_type_catalog(),
        detection_status="recognized",
        page_type="home",
        matched_required_positive_signal_ids=required,
        matched_supporting_signal_ids=_signal_ids("home", "strong_negative_signals")[:1],
        matched_strong_negative_signal_ids=(),
        excluded_confusable_page_types=home.confusable_with,
    )

    assert decision.accepted is False
    assert decision.reason_code == "recognition_guide_reference_invalid"


def test_h5_guide_downgrade_preserves_model_conclusion_for_audit() -> None:
    result = _apply_h5_recognition_guide_decision(
        {
            "detection_status": "recognized",
            "page_type": "home",
            "confidence": 0.94,
            "confidence_source": "model_self_report",
            "model_page_type": "home",
            "model_confidence": 0.94,
            "model_detection_status": "recognized",
            "model_reason": "模型认为这是首页",
            "reason": "模型认为这是首页",
            "matched_catalog_signals": {},
            "risk_flags": [],
            "missing_sources": [],
            "conflicts": [],
            "rule_matching_allowed": True,
        },
        catalog=load_default_page_type_catalog(),
    )

    assert result["detection_status"] == "ambiguous"
    assert result["page_type"] is None
    assert result["confidence"] == 0.0
    assert result["rule_matching_allowed"] is False
    assert result["model_detection_status"] == "recognized"
    assert result["model_page_type"] == "home"
    assert result["model_reason"] == "模型认为这是首页"
    assert result["recognition_guide_audit"]["reason_code"] == ("required_positive_signal_missing")
    assert "recognition_guide_evidence" in result["missing_sources"]
