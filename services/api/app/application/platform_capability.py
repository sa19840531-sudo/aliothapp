"""平台质检能力归因：区分业务结论、人工判断、证据缺口与平台降级。"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

AUTOMATIC_DECISION_STATUSES = frozenset({"pass", "fail", "warning"})

_PLATFORM_DIAGNOSTIC_PREFIXES = (
    "capability_",
    "deterministic_dependency_",
    "judge_provider_",
    "llm_provider_",
    "platform_",
    "semantic_provider_",
)

_PLATFORM_DIAGNOSTIC_CODES = frozenset(
    {
        "prompt_input_token_budget",
        "semantic_batch_single_failed",
        "semantic_judge_unavailable",
    }
)

_SUPPRESSED_EVIDENCE_CODES = frozenset(
    {"supplemental_evidence_disabled", "supplemental_evidence_not_ready"}
)

_PLATFORM_PAGE_FAILURE_PREFIXES = (
    "BROWSER_",
    "CLICK_TARGET_",
    "PAGE_CAPTURE_",
    "PAGE_DETECTION_",
    "PAGE_OBSTRUCTION_",
    "POPUP_",
    "STARTUP_AD_",
    "TOPMOST_",
)

_LEGACY_PLATFORM_SUMMARY_MARKERS = (
    (
        "语义规则判定超过独立墙钟预算",
        "semantic_provider_timeout",
    ),
    (
        "模型 API 返回 HTTP 500",
        "semantic_provider_http_500",
    ),
    (
        "质检平台尚未配置错别字词典或白名单",
        "capability_typo_dictionary_unavailable",
    ),
)


@dataclass(frozen=True)
class ExecutionCapabilityAttribution:
    """单条规则执行对平台能力的稳定归因。"""

    outcome_class: str
    capability_issue_code: str | None = None


@dataclass(frozen=True)
class PageCapabilityAttribution:
    """目标页面覆盖对平台能力的稳定归因。"""

    outcome_class: str
    capability_issue_codes: tuple[str, ...] = ()


def attribute_rule_execution(
    *,
    source: str,
    execution_status: str,
    diagnostic_codes: Iterable[str] = (),
    result_summary: str | None = None,
) -> ExecutionCapabilityAttribution:
    """把规则终态归入互斥能力口径，避免把平台故障伪装成业务人工复核。"""

    diagnostic_codes = tuple(diagnostic_codes)
    if execution_status == "skipped" and any(
        str(code or "").strip().lower() in _SUPPRESSED_EVIDENCE_CODES
        for code in diagnostic_codes
    ):
        return ExecutionCapabilityAttribution(outcome_class="evidence_gap")
    if source == "bootstrap":
        return ExecutionCapabilityAttribution(
            outcome_class=("coverage_gap" if execution_status == "skipped" else "execution_gap")
        )

    platform_issue_code = _platform_issue_code(
        diagnostic_codes=diagnostic_codes,
        result_summary=result_summary,
    )
    if platform_issue_code is not None or execution_status == "error":
        return ExecutionCapabilityAttribution(
            outcome_class="platform_degraded",
            capability_issue_code=platform_issue_code or "rule_execution_error",
        )
    if execution_status in AUTOMATIC_DECISION_STATUSES:
        return ExecutionCapabilityAttribution(outcome_class="automatic_decision")
    if execution_status == "manual_review":
        return ExecutionCapabilityAttribution(outcome_class="human_review_required")
    if execution_status == "evidence_insufficient":
        return ExecutionCapabilityAttribution(outcome_class="evidence_gap")
    if execution_status == "skipped":
        return ExecutionCapabilityAttribution(outcome_class="not_applicable")
    return ExecutionCapabilityAttribution(outcome_class="judged")


def attribute_target_page_coverage(
    *,
    status: str,
    failure_codes: Iterable[str] = (),
) -> PageCapabilityAttribution:
    """区分目标已到达、覆盖缺口与浏览器平台能力降级。"""

    normalized_codes = tuple(
        dict.fromkeys(
            str(code or "").strip().upper() for code in failure_codes if str(code or "").strip()
        )
    )
    platform_codes = tuple(
        code for code in normalized_codes if code.startswith(_PLATFORM_PAGE_FAILURE_PREFIXES)
    )
    if status == "reached":
        return PageCapabilityAttribution(outcome_class="reached")
    if platform_codes:
        return PageCapabilityAttribution(
            outcome_class="platform_degraded",
            capability_issue_codes=platform_codes,
        )
    if status == "failed":
        return PageCapabilityAttribution(
            outcome_class="platform_degraded",
            capability_issue_codes=normalized_codes or ("PAGE_EXPLORATION_FAILED",),
        )
    if status == "blocked":
        return PageCapabilityAttribution(
            outcome_class="platform_degraded",
            capability_issue_codes=normalized_codes or ("PAGE_EXPLORATION_BLOCKED",),
        )
    if status in {"not_found", "not_reached"}:
        return PageCapabilityAttribution(outcome_class="coverage_gap")
    return PageCapabilityAttribution(outcome_class="pending")


def _platform_issue_code(
    *,
    diagnostic_codes: Iterable[str],
    result_summary: str | None,
) -> str | None:
    normalized_codes = tuple(
        normalized for code in diagnostic_codes if (normalized := str(code or "").strip())
    )
    for code in normalized_codes:
        lowered = code.lower()
        if lowered in _PLATFORM_DIAGNOSTIC_CODES or lowered.startswith(
            _PLATFORM_DIAGNOSTIC_PREFIXES
        ):
            return lowered

    summary = str(result_summary or "")
    for marker, issue_code in _LEGACY_PLATFORM_SUMMARY_MARKERS:
        if marker in summary:
            return issue_code
    return None
