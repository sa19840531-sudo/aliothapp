"""测试平台能力归因契约。"""

from services.api.app.application.platform_capability import (
    attribute_rule_execution,
    attribute_target_page_coverage,
)


def test_provider_timeout_is_platform_degraded() -> None:
    attribution = attribute_rule_execution(
        source="agent",
        execution_status="manual_review",
        diagnostic_codes=("semantic_provider_timeout",),
        result_summary="模型判定失败",
    )

    assert attribution.outcome_class == "platform_degraded"
    assert attribution.capability_issue_code == "semantic_provider_timeout"


def test_legacy_platform_failure_summary_remains_attributable() -> None:
    attribution = attribute_rule_execution(
        source="agent",
        execution_status="manual_review",
        result_summary="模型 API 返回 HTTP 500（服务内部错误），未生成自动判定结论。",
    )

    assert attribution.outcome_class == "platform_degraded"
    assert attribution.capability_issue_code == "semantic_provider_http_500"


def test_genuine_manual_review_is_not_platform_degraded() -> None:
    attribution = attribute_rule_execution(
        source="agent",
        execution_status="manual_review",
        result_summary="页面视觉语义存在歧义，需人工确认。",
    )

    assert attribution.outcome_class == "human_review_required"
    assert attribution.capability_issue_code is None


def test_evidence_gap_and_not_applicable_are_distinct() -> None:
    evidence_gap = attribute_rule_execution(
        source="agent",
        execution_status="evidence_insufficient",
    )
    not_applicable = attribute_rule_execution(
        source="agent",
        execution_status="skipped",
    )

    assert evidence_gap.outcome_class == "evidence_gap"
    assert not_applicable.outcome_class == "not_applicable"


def test_bootstrap_gap_is_not_a_business_judgement() -> None:
    attribution = attribute_rule_execution(
        source="bootstrap",
        execution_status="skipped",
    )

    assert attribution.outcome_class == "coverage_gap"


def test_page_interaction_failure_is_platform_degraded() -> None:
    attribution = attribute_target_page_coverage(
        status="failed",
        failure_codes=("CLICK_TARGET_OBSTRUCTED",),
    )

    assert attribution.outcome_class == "platform_degraded"
    assert attribution.capability_issue_codes == ("CLICK_TARGET_OBSTRUCTED",)


def test_target_page_not_found_is_coverage_gap() -> None:
    attribution = attribute_target_page_coverage(
        status="not_found",
    )

    assert attribution.outcome_class == "coverage_gap"
    assert attribution.capability_issue_codes == ()
