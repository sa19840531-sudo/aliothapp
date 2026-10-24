"""任务级质检报告生成用例。"""

from collections import Counter
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Protocol

from services.api.app.application.platform_capability import (
    ExecutionCapabilityAttribution,
    attribute_rule_execution,
    attribute_target_page_coverage,
)
from services.api.app.application.report_text_policy import maybe_scrub_app_report_payload
from services.api.app.application.task_query import (
    EvidenceArtifactSummary,
    RuleExecutionCompleteness,
    RuleExecutionSummary,
    TaskDetail,
    TaskTargetPageCoverage,
    UnresolvedCandidateRule,
)
from services.api.app.domain.iam.access_scope import AccessScope
from services.api.app.infrastructure.device.cloud_phone import (
    same_label_risk_acceptance_payload,
)

REPORT_STATUSES = (
    "pass",
    "fail",
    "warning",
    "manual_review",
    "evidence_insufficient",
    "skipped",
    "error",
)
"""报告汇总使用的规则执行状态顺序，与 qc_rule_execution.execution_status 保持一致。"""

ATTENTION_STATUSES = {
    "fail",
    "warning",
    "manual_review",
    "evidence_insufficient",
    "error",
}
"""报告中需要优先关注的规则执行状态。"""

_SAFETY_BLOCKED_PAGE_STATES = {
    "automation_blocked",
    "bot_detection",
    "captcha_blocked",
    "safety_interstitial",
    "security_check",
}
_SAFETY_BLOCKED_RISK_FLAGS = {
    "automation_blocked",
    "bot_detection",
    "captcha_blocked",
    "deterministic_safety_interstitial",
    "safety_interstitial",
}
_TARGET_ACCESS_BLOCKED_CODE = "target_blocked_by_safety_interstitial"


class ReportTaskQueryService(Protocol):
    """报告生成依赖的任务查询服务契约。"""

    async def get_task_detail(
        self, task_id: str, *, access_scope: AccessScope | None = None
    ) -> TaskDetail | None:
        """按任务 ID 查询任务详情。"""

    async def list_rule_executions(
        self,
        task_id: str,
        *,
        agent_run_id: str | None = None,
        access_scope: AccessScope | None = None,
    ) -> list[RuleExecutionSummary]:
        """按任务 ID 查询规则执行结果。"""

    async def list_evidence_artifacts(
        self,
        task_id: str,
        *,
        agent_run_id: str | None = None,
        access_scope: AccessScope | None = None,
    ) -> list[EvidenceArtifactSummary]:
        """按任务 ID 查询证据附件摘要。"""

    async def get_rule_execution_completeness(
        self,
        *,
        task_id: str,
        agent_run_id: str,
        access_scope: AccessScope | None = None,
    ) -> RuleExecutionCompleteness:
        """查询当前 Run 的候选规则执行完整性。"""

    async def list_task_target_page_coverage(
        self,
        *,
        task_id: str,
        agent_run_id: str | None = None,
        access_scope: AccessScope | None = None,
    ) -> list[TaskTargetPageCoverage]:
        """查询任务目标页面的覆盖事实。"""


class ReportGenerationTargetNotFoundError(ValueError):
    """目标质检任务不存在，无法生成报告。"""


class ReportGenerationService:
    """基于任务详情与规则执行结果生成结构化质检报告。"""

    def __init__(
        self,
        *,
        query_service: ReportTaskQueryService,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._query_service = query_service
        self._clock = clock or (lambda: datetime.now(UTC))

    async def generate_task_report(
        self,
        task_id: str,
        *,
        agent_run_id: str | None = None,
        finalized_at: datetime | None = None,
        terminal_task_status: str | None = None,
        termination_reason: str | None = None,
        page_exploration: dict | None = None,
        access_scope: AccessScope | None = None,
    ) -> dict:
        """生成指定任务的结构化报告。"""
        scope_kwargs = _scope_kwargs(access_scope)
        task = await self._query_service.get_task_detail(task_id, **scope_kwargs)
        if task is None:
            raise ReportGenerationTargetNotFoundError(f"未找到质检任务：{task_id}")
        effective_agent_run_id = agent_run_id or task.run_id
        # 按质检规则统一口径：规则结果不分 Run（H5/App 的判定结果写在独立判定 Run 下）
        executions = await self._query_service.list_rule_executions(
            task_id,
            agent_run_id=None,
            **scope_kwargs,
        )
        evidence_artifacts = await self._query_service.list_evidence_artifacts(
            task_id,
            agent_run_id=effective_agent_run_id,
            **scope_kwargs,
        )
        completeness = (
            await self._query_service.get_rule_execution_completeness(
                task_id=task_id,
                agent_run_id=effective_agent_run_id,
                **scope_kwargs,
            )
            if effective_agent_run_id is not None
            else None
        )
        target_page_coverage = await self._query_service.list_task_target_page_coverage(
            task_id=task_id,
            agent_run_id=effective_agent_run_id,
            **scope_kwargs,
        )
        task_payload = _task_to_dict(task)
        if finalized_at is not None:
            task_payload["status"] = terminal_task_status or _resolve_terminal_task_status(
                executions
            )
            task_payload["finished_at"] = finalized_at.isoformat()
        is_final = finalized_at is not None
        target_page_results = _build_target_page_results(
            target_page_coverage,
            is_terminal=(is_final or task.status in {"review", "done", "failed", "canceled"}),
        )
        include_h5_evidence_identity = (
            str(task.target_platform or "").strip().casefold() == "h5"
        )
        report = {
            "report_version": "v2",
            "report_stage": "final" if is_final else "preview",
            "is_final": is_final,
            "agent_run_id": effective_agent_run_id,
            "termination_reason": termination_reason,
            "generated_at": self._clock().isoformat(),
            "task": task_payload,
            "summary": _build_summary(
                executions,
                target_platform=task.target_platform,
                completeness=completeness,
                terminal_task_status=terminal_task_status,
                termination_reason=termination_reason,
                page_exploration=page_exploration,
                target_page_results=target_page_results,
            ),
            "page_exploration": page_exploration,
            "target_page_coverage": target_page_results,
            "groups": _build_groups(executions),
            "attention_results": [
                _rule_execution_to_dict(
                    item,
                    include_h5_evidence_identity=include_h5_evidence_identity,
                )
                for item in executions
                if item.execution_status in ATTENTION_STATUSES
                or _execution_capability_attribution(item).outcome_class == "evidence_gap"
            ],
            "rule_results": [
                _rule_execution_to_dict(
                    item,
                    include_h5_evidence_identity=include_h5_evidence_identity,
                )
                for item in executions
            ],
            "evidence_entries": [_evidence_artifact_to_dict(item) for item in evidence_artifacts],
        }
        # App 报告硬门禁：禁止“灰产/黑产”字样进入最终/预览报告载荷
        return maybe_scrub_app_report_payload(
            report,
            target_platform=task.target_platform,
            target_url=task.target_url,
        )


def _scope_kwargs(access_scope: AccessScope | None) -> dict[str, AccessScope]:
    """报告 HTTP 请求有 scope 时把同一主体传到每个下游查询。"""

    return {"access_scope": access_scope} if access_scope is not None else {}


def _build_summary(
    executions: list[RuleExecutionSummary],
    *,
    target_platform: str | None = None,
    completeness: RuleExecutionCompleteness | None = None,
    terminal_task_status: str | None = None,
    termination_reason: str | None = None,
    page_exploration: dict | None = None,
    target_page_results: list[dict] | None = None,
) -> dict:
    capability_attributions = [_execution_capability_attribution(item) for item in executions]
    # 历史补采拒绝保存为 skipped，但结构化诊断明确表明缺证，汇总不能计为不适用。
    status_counts = Counter(
        "evidence_insufficient"
        if item.execution_status == "skipped" and attribution.outcome_class == "evidence_gap"
        else item.execution_status
        for item, attribution in zip(executions, capability_attributions, strict=True)
    )
    outcome_counts = Counter(attribution.outcome_class for attribution in capability_attributions)
    platform_issue_counts = Counter(
        attribution.capability_issue_code
        for attribution in capability_attributions
        if attribution.capability_issue_code is not None
    )
    summary = {status: status_counts.get(status, 0) for status in REPORT_STATUSES}
    summary["total_rules"] = len(executions)
    summary["attention_required"] = sum(
        status_counts.get(status, 0) for status in ATTENTION_STATUSES
    )
    web_coverage_gaps = {
        item.execution_id
        for item in executions
        if _is_web_coverage_gap_execution(
            item,
            target_platform=target_platform,
            page_exploration=page_exploration,
        )
    }
    summary["evaluated_rules"] = sum(
        item.source == "agent" and item.execution_id not in web_coverage_gaps
        for item in executions
    )
    summary["unresolved_candidate_rules"] = sum(
        item.source == "bootstrap" and item.execution_status == "evidence_insufficient"
        for item in executions
    )
    finalized_unresolved_items = [
        UnresolvedCandidateRule(
            page_capture_id=item.page_capture_id,
            rule_code=item.rule_code,
        )
        for item in executions
        if item.source == "bootstrap"
        and item.execution_status == "evidence_insufficient"
        and item.page_capture_id is not None
    ]
    finalized_unresolved_codes = [
        item.rule_code
        for item in executions
        if item.source == "bootstrap" and item.execution_status == "evidence_insufficient"
    ]
    summary["non_candidate_rules"] = sum(
        item.source == "bootstrap" and item.execution_status == "skipped" for item in executions
    )
    # skipped 同时承载“Judge 明确不适用”和“运行未覆盖到”两种完全不同的含义。
    # 报告保留旧字段兼容性，同时提供互斥口径，避免把覆盖缺口误读为业务不适用。
    summary["confirmed_not_applicable_rules"] = sum(
        item.source == "agent" and item.execution_status == "skipped"
        and attribution.outcome_class == "not_applicable"
        for item, attribution in zip(executions, capability_attributions, strict=True)
    )
    summary["coverage_gap_rules"] = (
        summary["non_candidate_rules"] + len(web_coverage_gaps)
    )
    summary["full_rule_pass_rate"] = (
        round(summary["pass"] * 100 / summary["total_rules"], 2) if summary["total_rules"] else 0.0
    )
    conclusive_rules = summary["pass"] + summary["fail"] + summary["warning"]
    summary["pass_rate"] = (
        round(summary["pass"] * 100 / conclusive_rules, 2) if conclusive_rules else 0.0
    )
    # warning 已形成明确规则结论，属于有效判定但不属于通过；只有 Agent 明确判定为
    # 不适用的 skipped 才表示完成。bootstrap skipped 是覆盖缺口，不能制造“假完成”。
    qc_success_rules = (
        summary["pass"]
        + summary["fail"]
        + summary["warning"]
        + summary["confirmed_not_applicable_rules"]
    )
    qc_metric_rules = summary["total_rules"]
    summary["qc_success_rate"] = (
        round(qc_success_rules * 100 / qc_metric_rules, 2) if qc_metric_rules else 0.0
    )
    candidate_rules = summary["evaluated_rules"] + summary["unresolved_candidate_rules"]
    summary["candidate_execution_coverage"] = (
        round(summary["evaluated_rules"] * 100 / candidate_rules, 2) if candidate_rules else 0.0
    )
    if completeness is not None:
        has_candidate_query = completeness.candidate_query_count > 0
        unresolved_items = (
            list(completeness.unresolved_candidate_items)
            if has_candidate_query
            else finalized_unresolved_items
        )
        unresolved_codes = list(dict.fromkeys(item.rule_code for item in unresolved_items))
        if has_candidate_query and not unresolved_items:
            unresolved_codes = list(completeness.unresolved_rule_codes)
        elif not has_candidate_query:
            unresolved_codes = list(dict.fromkeys(finalized_unresolved_codes))
        legacy_unresolved_count = (
            len(finalized_unresolved_codes) - len(finalized_unresolved_items)
            if not has_candidate_query
            else 0
        )
        unresolved_candidate_count = (
            len(unresolved_items) + legacy_unresolved_count
            if not has_candidate_query
            else completeness.unresolved_candidate_count
        )
        completed_candidate_count = (
            completeness.completed_candidate_count
            if has_candidate_query
            else summary["evaluated_rules"]
        )
        candidate_rule_count = (
            completeness.candidate_rule_count
            if has_candidate_query
            else completed_candidate_count + unresolved_candidate_count
        )
        represented_items = {(item.page_capture_id, item.rule_code) for item in executions}
        summary["attention_required"] += sum(
            (item.page_capture_id, item.rule_code) not in represented_items
            for item in unresolved_items
        )
        summary["candidate_query_count"] = completeness.candidate_query_count
        summary["candidate_rule_count"] = candidate_rule_count
        summary["completed_candidate_rules"] = completed_candidate_count
        summary["unresolved_candidate_rules"] = unresolved_candidate_count
        summary["unresolved_candidate_rule_codes"] = unresolved_codes
        summary["unresolved_candidate_items"] = [
            {
                "page_capture_id": item.page_capture_id,
                "rule_code": item.rule_code,
            }
            for item in unresolved_items
        ]
        summary["candidate_execution_coverage"] = (
            round(
                completed_candidate_count * 100 / candidate_rule_count,
                2,
            )
            if candidate_rule_count
            else 0.0
        )
        summary["candidate_execution_complete"] = has_candidate_query and not unresolved_codes
    technical_execution_complete = bool(
        summary.get(
            "candidate_execution_complete",
            summary["total_rules"] > 0 and summary["unresolved_candidate_rules"] == 0,
        )
    )
    evaluated_rule_count = summary["evaluated_rules"]
    automatic_decision_count = outcome_counts["automatic_decision"]
    platform_degraded_count = outcome_counts["platform_degraded"]
    summary["technical_execution_complete"] = technical_execution_complete
    summary["technical_completion_status"] = (
        "complete" if technical_execution_complete else "incomplete"
    )
    summary["automatic_decision_rules"] = automatic_decision_count
    summary["automatic_decision_rate"] = _percentage(
        automatic_decision_count,
        evaluated_rule_count,
    )
    summary["platform_degraded_rules"] = platform_degraded_count
    summary["platform_degradation_rate"] = _percentage(
        platform_degraded_count,
        evaluated_rule_count,
    )
    summary["human_review_required_rules"] = outcome_counts["human_review_required"]
    summary["evidence_gap_rules"] = outcome_counts["evidence_gap"]
    summary["classified_evidence_gap_rules"] = sum(
        attribution.outcome_class == "evidence_gap" and bool(item.missing_evidence)
        for item, attribution in zip(
            executions,
            capability_attributions,
            strict=True,
        )
    )
    summary["unclassified_evidence_gap_rules"] = (
        summary["evidence_gap_rules"] - summary["classified_evidence_gap_rules"]
    )
    summary["not_applicable_rules"] = outcome_counts["not_applicable"]
    target_page_results = target_page_results or []
    target_status_counts = Counter(str(item.get("status") or "") for item in target_page_results)
    target_outcome_counts = Counter(
        str(item.get("outcome_class") or "") for item in target_page_results
    )
    platform_page_issue_counts = Counter(
        str(code)
        for item in target_page_results
        for code in item.get("capability_issue_codes", [])
        if str(code)
    )
    target_page_total = len(target_page_results)
    reached_target_pages = target_status_counts["reached"]
    summary["target_page_total"] = target_page_total
    summary["reached_target_pages"] = reached_target_pages
    summary["failed_target_pages"] = target_status_counts["failed"]
    summary["blocked_target_pages"] = target_status_counts["blocked"]
    summary["not_found_target_pages"] = target_status_counts["not_found"]
    summary["pending_target_pages"] = (
        target_status_counts["pending"] + target_status_counts["pending_discovery"]
    )
    summary["target_page_coverage_rate"] = _percentage(
        reached_target_pages,
        target_page_total,
    )
    summary["platform_degraded_target_pages"] = target_outcome_counts["platform_degraded"]
    summary["platform_target_degradation_rate"] = _percentage(
        summary["platform_degraded_target_pages"],
        target_page_total,
    )
    summary["platform_rule_issue_codes"] = dict(sorted(platform_issue_counts.items()))
    summary["platform_page_issue_codes"] = dict(sorted(platform_page_issue_counts.items()))
    target_access_blockage = _target_access_blockage(page_exploration)
    if target_access_blockage["blocked_page_count"] > 0:
        summary["target_access_status"] = "blocked_by_safety_interstitial"
        summary["target_access_blocked"] = True
        summary["target_access_blocked_page_count"] = target_access_blockage["blocked_page_count"]
        summary["target_access_block_reason_codes"] = [_TARGET_ACCESS_BLOCKED_CODE]
        summary["target_access_blocked_page_capture_ids"] = target_access_blockage[
            "page_capture_ids"
        ]
        platform_page_issue_counts[_TARGET_ACCESS_BLOCKED_CODE] += target_access_blockage[
            "blocked_page_count"
        ]
        summary["platform_page_issue_codes"] = dict(sorted(platform_page_issue_counts.items()))
    combined_platform_issue_counts = platform_issue_counts + platform_page_issue_counts
    summary["platform_issue_codes"] = dict(sorted(combined_platform_issue_counts.items()))
    summary["platform_capability_status"] = (
        "unassessed"
        if (
            evaluated_rule_count == 0
            and target_page_total == 0
            and summary.get("target_access_blocked") is not True
        )
        else (
            "degraded"
            if platform_degraded_count
            or summary["platform_degraded_target_pages"]
            or summary.get("target_access_blocked") is True
            else "healthy"
        )
    )
    summary["task_goal_completed"] = (
        None if target_page_total == 0 else reached_target_pages == target_page_total
    )
    summary["task_goal_completion_status"] = _task_goal_completion_status(
        target_page_total=target_page_total,
        reached_target_pages=reached_target_pages,
        pending_target_pages=summary["pending_target_pages"],
    )
    summary["overall_status"] = _resolve_overall_status(
        summary,
        target_platform=target_platform,
        terminal_task_status=terminal_task_status,
        termination_reason=termination_reason,
        page_exploration=page_exploration,
    )
    summary["business_completion_status"] = _business_completion_status(summary["overall_status"])
    result = {
        "total_rules": summary["total_rules"],
        **{status: summary[status] for status in REPORT_STATUSES},
        "attention_required": summary["attention_required"],
        "evaluated_rules": summary["evaluated_rules"],
        "unresolved_candidate_rules": summary["unresolved_candidate_rules"],
        "non_candidate_rules": summary["non_candidate_rules"],
        "confirmed_not_applicable_rules": summary["confirmed_not_applicable_rules"],
        "coverage_gap_rules": summary["coverage_gap_rules"],
        "full_rule_pass_rate": summary["full_rule_pass_rate"],
        "qc_success_rate": summary["qc_success_rate"],
        "pass_rate": summary["pass_rate"],
        "candidate_execution_coverage": summary["candidate_execution_coverage"],
        "technical_execution_complete": summary["technical_execution_complete"],
        "technical_completion_status": summary["technical_completion_status"],
        "business_completion_status": summary["business_completion_status"],
        "automatic_decision_rules": summary["automatic_decision_rules"],
        "automatic_decision_rate": summary["automatic_decision_rate"],
        "platform_degraded_rules": summary["platform_degraded_rules"],
        "platform_degradation_rate": summary["platform_degradation_rate"],
        "human_review_required_rules": summary["human_review_required_rules"],
        "evidence_gap_rules": summary["evidence_gap_rules"],
        "classified_evidence_gap_rules": summary["classified_evidence_gap_rules"],
        "unclassified_evidence_gap_rules": summary["unclassified_evidence_gap_rules"],
        "not_applicable_rules": summary["not_applicable_rules"],
        "platform_issue_codes": summary["platform_issue_codes"],
        "platform_capability_status": summary["platform_capability_status"],
        "target_page_total": summary["target_page_total"],
        "reached_target_pages": summary["reached_target_pages"],
        "failed_target_pages": summary["failed_target_pages"],
        "blocked_target_pages": summary["blocked_target_pages"],
        "not_found_target_pages": summary["not_found_target_pages"],
        "pending_target_pages": summary["pending_target_pages"],
        "target_page_coverage_rate": summary["target_page_coverage_rate"],
        "platform_degraded_target_pages": summary["platform_degraded_target_pages"],
        "platform_target_degradation_rate": summary["platform_target_degradation_rate"],
        "platform_rule_issue_codes": summary["platform_rule_issue_codes"],
        "platform_page_issue_codes": summary["platform_page_issue_codes"],
        "task_goal_completed": summary["task_goal_completed"],
        "task_goal_completion_status": summary["task_goal_completion_status"],
        "overall_status": summary["overall_status"],
    }
    if completeness is not None:
        result.update(
            {
                "candidate_query_count": summary["candidate_query_count"],
                "candidate_rule_count": summary["candidate_rule_count"],
                "completed_candidate_rules": summary["completed_candidate_rules"],
                "unresolved_candidate_rule_codes": summary["unresolved_candidate_rule_codes"],
                "unresolved_candidate_items": summary["unresolved_candidate_items"],
                "candidate_execution_complete": summary["candidate_execution_complete"],
            }
        )
    if summary.get("target_access_blocked") is True:
        result.update(
            {
                "target_access_status": summary["target_access_status"],
                "target_access_blocked": summary["target_access_blocked"],
                "target_access_blocked_page_count": summary["target_access_blocked_page_count"],
                "target_access_block_reason_codes": summary["target_access_block_reason_codes"],
                "target_access_blocked_page_capture_ids": summary[
                    "target_access_blocked_page_capture_ids"
                ],
            }
        )
    return result


def _resolve_overall_status(
    summary: dict,
    *,
    target_platform: str | None = None,
    terminal_task_status: str | None = None,
    termination_reason: str | None = None,
    page_exploration: dict | None = None,
) -> str:
    page_requires_review = (
        page_exploration is not None and page_exploration.get("requires_review") is True
    )
    if summary.get("target_access_blocked") is True:
        return "incomplete"
    if terminal_task_status == "failed":
        return "error"
    if terminal_task_status == "review" and termination_reason not in {
        None,
        "completed",
    }:
        return "incomplete"
    if page_exploration is not None and page_exploration.get("complete") is False:
        return "incomplete"
    if terminal_task_status == "review" and (
        page_exploration is None or page_exploration.get("complete") is not True
    ):
        return "incomplete"
    if summary.get("candidate_execution_complete") is False and not page_requires_review:
        return "incomplete"
    if summary["unresolved_candidate_rules"] > 0 and not page_requires_review:
        return "incomplete"
    if (
        summary["total_rules"] == 0 or summary["total_rules"] == summary["skipped"]
    ) and not page_requires_review:
        return "incomplete"
    if summary["error"] > 0:
        return "error"
    if summary["fail"] > 0:
        return "fail"
    if (
        str(target_platform or "").casefold() == "web"
        and summary["total_rules"] > 0
        and summary["coverage_gap_rules"] == summary["total_rules"]
    ):
        return "unknown"
    if summary["manual_review"] > 0 or summary["evidence_insufficient"] > 0:
        return "manual_review"
    if page_requires_review:
        return "manual_review"
    if summary["warning"] > 0:
        return "warning"
    return "pass"


def _is_web_coverage_gap_execution(
    execution: RuleExecutionSummary,
    *,
    target_platform: str | None,
    page_exploration: dict | None,
) -> bool:
    """只把正式 Web Coverage Gap 身份计为覆盖缺口。"""

    if str(target_platform or "").casefold() != "web":
        return False
    if execution.execution_status != "evidence_insufficient":
        return False
    if execution.page_capture_id is not None:
        return False
    coverage = (
        page_exploration.get("web_coverage")
        if isinstance(page_exploration, dict)
        else None
    )
    if not isinstance(coverage, Mapping):
        return False
    snapshot_id = str(coverage.get("coverage_snapshot_id") or "")
    gap_rule_codes = coverage.get("gap_rule_codes")
    judge_input = execution.judge_input
    if not snapshot_id or not isinstance(judge_input, Mapping):
        return False
    missing_page_type = str(judge_input.get("missing_page_type") or "")
    expected_source_key = (
        f"web-coverage-gap:{snapshot_id}:{missing_page_type}:{execution.rule_code}"
    )
    return (
        isinstance(gap_rule_codes, (list, tuple))
        and execution.rule_code in gap_rule_codes
        and bool(missing_page_type)
        and missing_page_type in (coverage.get("missing_page_types") or [])
        and execution.source_idempotency_key == expected_source_key
        and judge_input.get("schema_version") == "web-coverage-gap.v1"
        and judge_input.get("source") == "web_coverage_gap"
        and judge_input.get("coverage_snapshot_id") == snapshot_id
        and judge_input.get("missing_page_type") == missing_page_type
        and judge_input.get("reason") == "required_page_type_not_discovered"
    )


def _resolve_terminal_task_status(executions: list[RuleExecutionSummary]) -> str:
    """按规则结果计算任务最终状态，与状态存储的人工复核条件保持一致。"""
    requires_review = any(
        item.execution_status in {"manual_review", "evidence_insufficient"}
        or item.review_status in {"pending", "needs_recheck"}
        or _execution_capability_attribution(item).outcome_class == "evidence_gap"
        for item in executions
    )
    return "review" if requires_review else "done"


def _build_groups(executions: list[RuleExecutionSummary]) -> dict:
    by_status = {status: [] for status in REPORT_STATUSES}
    by_severity: dict[str, list[str]] = {}
    for item in executions:
        by_status.setdefault(item.execution_status, []).append(item.rule_code)
        if item.severity:
            by_severity.setdefault(item.severity, []).append(item.rule_code)
    return {
        "by_status": by_status,
        "by_severity": by_severity,
    }


def _task_to_dict(task: TaskDetail) -> dict:
    payload = {
        "task_id": task.task_id,
        "target_url": task.target_url,
        "ruleset_id": task.ruleset_id,
        "ruleset_code": task.ruleset_code,
        "ruleset_version": task.ruleset_version,
        "channel": task.channel,
        "status": task.status,
        "priority": task.priority,
        "run_id": task.run_id,
        "created_at": _datetime_to_string(task.created_at),
        "started_at": _datetime_to_string(task.started_at),
        "finished_at": _datetime_to_string(task.finished_at),
        "last_error": task.last_error,
        "remark": task.remark,
    }
    risk_acceptance = same_label_risk_acceptance_payload(task.remark)
    if risk_acceptance is not None:
        payload["same_label_risk_acceptance"] = risk_acceptance
    return payload


def _rule_execution_to_dict(
    item: RuleExecutionSummary,
    *,
    include_h5_evidence_identity: bool = False,
) -> dict:
    attribution = _execution_capability_attribution(item)
    payload = {
        "execution_id": item.execution_id,
        "rule_id": item.rule_id,
        "rule_code": item.rule_code,
        "rule_name": item.rule_name,
        "page_capture_id": item.page_capture_id,
        "execution_status": item.execution_status,
        "outcome_class": attribution.outcome_class,
        "capability_issue_code": attribution.capability_issue_code,
        "diagnostic_codes": list(item.diagnostic_codes),
        "missing_evidence": list(item.missing_evidence),
        "judge_strategy": item.judge_strategy,
        "confidence": item.confidence,
        "severity": item.severity,
        "result_summary": item.result_summary,
        "problem_desc": item.problem_desc,
        "review_status": item.review_status,
        "source": item.source,
        "started_at": _datetime_to_string(item.started_at),
        "finished_at": _datetime_to_string(item.finished_at),
    }
    if include_h5_evidence_identity:
        payload.update(
            evidence_artifact_ids=list(item.evidence_artifact_ids),
            evidence_input_set_id=item.evidence_input_set_id,
        )
    return payload


def _execution_capability_attribution(
    item: RuleExecutionSummary,
) -> ExecutionCapabilityAttribution:
    return attribute_rule_execution(
        source=item.source,
        execution_status=item.execution_status,
        diagnostic_codes=item.diagnostic_codes,
        result_summary=item.result_summary,
    )


def _percentage(numerator: int, denominator: int) -> float:
    return round(numerator * 100 / denominator, 2) if denominator else 0.0


def _business_completion_status(overall_status: str) -> str:
    return {
        "pass": "passed",
        "fail": "failed",
        "warning": "warning",
        "manual_review": "review_required",
        "incomplete": "incomplete",
        "error": "error",
    }.get(overall_status, overall_status)


def _target_access_blockage(page_exploration: dict | None) -> dict[str, list[str] | int]:
    if not isinstance(page_exploration, Mapping):
        return {"blocked_page_count": 0, "page_capture_ids": []}
    page_results = page_exploration.get("page_results")
    blocked_capture_ids: list[str] = []
    if isinstance(page_results, list):
        for item in page_results:
            if not isinstance(item, Mapping):
                continue
            if not _is_safety_blocked_page_result(item):
                continue
            capture_id = str(item.get("page_capture_id") or "").strip()
            if capture_id and capture_id not in blocked_capture_ids:
                blocked_capture_ids.append(capture_id)
    coverage = page_exploration.get("coverage")
    coverage = coverage if isinstance(coverage, Mapping) else {}
    gaps = coverage.get("gaps")
    if isinstance(gaps, list):
        for item in gaps:
            if not isinstance(item, Mapping):
                continue
            if str(item.get("status") or "").strip().casefold() != "blocked_by_safety":
                continue
            for capture_id in _strings(item.get("page_capture_ids")):
                if capture_id not in blocked_capture_ids:
                    blocked_capture_ids.append(capture_id)
    return {
        "blocked_page_count": len(blocked_capture_ids),
        "page_capture_ids": blocked_capture_ids,
    }


def _is_safety_blocked_page_result(item: Mapping[str, object]) -> bool:
    page_state = str(item.get("page_state") or "").strip().casefold()
    risk_flags = {flag.casefold() for flag in _strings(item.get("risk_flags"))}
    target_access_status = str(item.get("target_access_status") or "").strip().casefold()
    conflicts = {conflict.casefold() for conflict in _strings(item.get("conflicts"))}
    return bool(
        target_access_status == "blocked_by_safety_interstitial"
        or page_state in _SAFETY_BLOCKED_PAGE_STATES
        or risk_flags & _SAFETY_BLOCKED_RISK_FLAGS
        or _TARGET_ACCESS_BLOCKED_CODE in conflicts
    )


def _strings(value: object) -> list[str]:
    if isinstance(value, str):
        return [value] if value.strip() else []
    if not isinstance(value, (list, tuple, set)):
        return []
    return [text for item in value if (text := str(item or "").strip())]


def _build_target_page_results(
    coverage: list[TaskTargetPageCoverage],
    *,
    is_terminal: bool,
) -> list[dict]:
    results: list[dict] = []
    for item in coverage:
        status = _target_page_status(item, is_terminal=is_terminal)
        attribution = attribute_target_page_coverage(
            status=status,
            failure_codes=item.failure_codes,
        )
        results.append(
            {
                "page_type": item.page_type,
                "status": status,
                "outcome_class": attribution.outcome_class,
                "capability_issue_codes": list(attribution.capability_issue_codes),
                "capture_count": item.capture_count,
                "explored_action_count": item.explored_action_count,
                "failed_action_count": item.failed_action_count,
                "blocked_action_count": item.blocked_action_count,
                "skipped_action_count": item.skipped_action_count,
                "pending_action_count": item.pending_action_count,
                "failure_codes": list(item.failure_codes),
            }
        )
    return results


def _target_page_status(
    coverage: TaskTargetPageCoverage,
    *,
    is_terminal: bool,
) -> str:
    if coverage.capture_count > 0:
        return "reached"
    if coverage.pending_action_count > 0:
        return "pending"
    if coverage.failed_action_count > 0:
        return "failed"
    if coverage.blocked_action_count > 0:
        return "blocked"
    if coverage.explored_action_count > 0 or coverage.skipped_action_count > 0:
        return "not_reached"
    return "not_found" if is_terminal else "pending_discovery"


def _task_goal_completion_status(
    *,
    target_page_total: int,
    reached_target_pages: int,
    pending_target_pages: int,
) -> str:
    if target_page_total == 0:
        return "unconfigured"
    if reached_target_pages == target_page_total:
        return "complete"
    if pending_target_pages > 0:
        return "in_progress"
    return "partial"


def _evidence_artifact_to_dict(item: EvidenceArtifactSummary) -> dict:
    return {
        "artifact_id": item.artifact_id,
        "page_capture_id": item.page_capture_id,
        "rule_execution_id": item.rule_execution_id,
        "artifact_type": item.artifact_type,
        "source": item.source,
        "storage_path": item.storage_path,
        "content_type": item.content_type,
        "checksum": item.checksum,
        "size_bytes": item.size_bytes,
        "metadata": item.metadata,
        "created_at": _datetime_to_string(item.created_at),
    }


def _datetime_to_string(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None
