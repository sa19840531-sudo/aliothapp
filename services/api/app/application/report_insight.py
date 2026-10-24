"""报告中心读模型与人工确认用例：摘要 / 确认清单 / 全量明细 / HTML 导出。

新版报告中心是"单任务报告页"：机器细粒度执行态（``task_progress.py``）在
报告读侧统一归并为六类展示态——通过 / 未通过 / 本产品不适用 / 计划外未覆盖 /
检测异常 / 其他；仅已执行且业务结论待复核的规则进入人工确认清单，由人工给出终审
（通过/未通过/忽略）。检测缺口不能靠人工补成完成；有缺口的导出标为复核报告。导出件为体验平台兼容
格式（黑金样式 + ``__edit_data__`` 数据载体，见 ``experience_report_export.py``），
只保留未通过条目；报告状态始终采用任务业务状态。
"""

import asyncio
import json
import re
from collections.abc import Callable
from datetime import datetime
from typing import Any

from services.api.app.application.experience_report_export import (
    build_platform_issue,
    render_experience_report,
)
from services.api.app.application.green_report_result_projection import (
    green_report_has_unresolved_results,
    green_report_item_is_resolved,
    project_green_confirmation_result,
    project_open_green_report_results,
    restrict_open_green_report_completion,
)
from services.api.app.application.h5_evidence_inventory import (
    project_h5_evidence_preflight,
)
from services.api.app.application.h5_run_failure import classify_h5_run_failure
from services.api.app.application.h5_terminal_projection import (
    h5_target_coverage_ready,
    latest_h5_frontier,
    project_h5_terminal,
)
from services.api.app.application.page_exploration_summary import (
    apply_app_page_type_applicability,
    build_page_exploration_summary,
    extract_page_type_assessment_context,
    extract_page_type_assessment_scope_page_types,
    project_h5_page_type_assessments,
)
from services.api.app.application.report_execution_eligibility import qualify_report_execution
from services.api.app.application.report_export_evidence import (
    collect_export_evidence,
    export_evidence_note,
    referenced_artifact_ids,
)
from services.api.app.application.report_rule_inventory import (
    append_report_rule_inventory,
    project_report_rule_inventory,
)
from services.api.app.application.report_text_policy import (
    maybe_scrub_app_report_payload,
)
from services.api.app.application.task_progress import (
    HUMAN_VERDICT_LABELS,
    RULE_EVIDENCE_GAP_REASONS,
    effective_rule_execution_reason,
    human_verdict_label,
    humanize_qc_text,
    page_type_display_name,
    rule_display_label,
    rule_display_status,
    rule_execution_counts_as_completed,
    rule_execution_counts_as_used,
    rule_final_display_status,
    rule_package_fallback,
    rule_requires_human_confirmation,
    severity_display,
    trigger_display_fallback,
)
from services.api.app.application.task_rule_inventory import requires_task_rule_inventory
from services.api.app.application.web_final_report_binding import (
    WebFinalReportBindingError,
    WebFinalReportBindingExpectation,
    is_web_v2_final_contract,
    validate_web_final_report_binding,
)
from services.api.app.application.web_logical_page import (
    build_web_logical_pages,
    resolve_web_logical_page_role,
    web_logical_page_display_url,
)
from services.api.app.domain.iam.access_scope import AccessScope
from services.api.app.domain.task.inspection_scope import (
    inspection_scope_page_types,
    page_type_in_inspection_scope,
)

VALID_HUMAN_VERDICTS = ("pass", "fail", "ignored")

_TERMINAL_TASK_STATUSES = {"done", "failed", "canceled", "review"}
_TERMINAL_RULE_EXECUTION_STATUSES = {
    "pass",
    "fail",
    "warning",
    "manual_review",
    "evidence_insufficient",
    "error",
    "skipped",
}
_EXPORT_IMAGE_MAX_BYTES = 400_000
_EXPORT_IMAGE_MAX_COUNT = 12

_DISTRIBUTION_ORDER = (
    "passed",
    "failed",
    "not_applicable",
    "coverage_gap",
    "detect_error",
    "other",
)

_DISTRIBUTION_LABELS = {
    "passed": "通过",
    "failed": "未通过",
    "not_applicable": "本产品不适用",
    "coverage_gap": "计划外未覆盖",
    "detect_error": "检测异常",
    "other": "其他",
}

# 机器细粒度展示态 → 报告六类展示态。未列出的细粒度态（证据不足 / 转人工 /
# 未执行 / 过程态等）一律落入「其他」，保证读侧不漏项。
_DISPLAY_STATUS_GROUPS = {
    "passed": "passed",
    "failed": "failed",
    "not_detected": "not_applicable",
    "not_applicable": "not_applicable",
    "coverage_gap": "coverage_gap",
    "detect_error": "detect_error",
}

# 运行被弹窗问题终止时的原始错误串（kind:closed/limit），报告需要把它变成可见发现。
_POPUP_WALL_ERROR = re.compile(r"(popup_layer_limit_exceeded|popup_dismissal_stalled):(\d+)/(\d+)")


class ReportInsightNotFoundError(LookupError):
    """任务 / 执行记录不存在。"""


class ReportConfirmationError(ValueError):
    """人工确认动作非法（范围外 / 结论非法）。"""


class ReportExportBlockedError(RuntimeError):
    """导出被门禁拦截：仍有待人工确认规则。"""


class ReportInsightService:
    """报告中心聚合服务。"""

    def __init__(
        self,
        *,
        repository: Any,
        final_report_snapshot_repository: Any | None = None,
        evidence_store_factory: Callable[[], Any] | None = None,
        rule_inventory_repository: Any | None = None,
    ) -> None:
        self._repository = repository
        self._final_report_snapshot_repository = final_report_snapshot_repository
        self._evidence_store_factory = evidence_store_factory
        self._evidence_store: Any | None = None
        self._rule_inventory_repository = rule_inventory_repository

    async def report_insight(
        self,
        task_id: str,
        *,
        access_scope: AccessScope | None = None,
    ) -> dict[str, Any]:
        snapshot_reader = getattr(
            self._final_report_snapshot_repository,
            "get_final_report",
            None,
        )
        if callable(snapshot_reader):
            snapshot = await snapshot_reader(
                task_id,
                **_scope_kwargs(access_scope),
            )
            if isinstance(snapshot, dict):
                if is_web_v2_final_contract(snapshot):
                    try:
                        validate_web_final_report_binding(
                            snapshot,
                            expectation=WebFinalReportBindingExpectation(task_id=task_id),
                        )
                    except WebFinalReportBindingError as exc:
                        raise ReportInsightNotFoundError(
                            f"Web 正式报告绑定校验失败：{task_id}"
                        ) from exc
                    frozen = _bound_final_snapshot_insight(
                        snapshot,
                        target_platform="web",
                        execution_reasons=await self._frozen_execution_reasons(
                            snapshot, task_id=task_id, access_scope=access_scope
                        ),
                    )
                    await self._attach_rule_rulesets(frozen["rule_items"])
                    await self._attach_ruleset_rule_total(frozen, task_id, access_scope)
                    await self._attach_rule_inventory(frozen, task_id, access_scope)
                    return frozen
                if _is_h5_v2_final_snapshot(snapshot, task_id=task_id):
                    frozen = _bound_final_snapshot_insight(
                        snapshot,
                        target_platform="h5",
                        execution_reasons=await self._frozen_execution_reasons(
                            snapshot, task_id=task_id, access_scope=access_scope
                        ),
                    )
                    await self._attach_rule_rulesets(frozen["rule_items"])
                    await self._attach_ruleset_rule_total(frozen, task_id, access_scope)
                    await self._attach_rule_inventory(frozen, task_id, access_scope)
                    return frozen
        context = await self._require_context(task_id, access_scope=access_scope)
        run_id = context.get("run_id")
        scope_kwargs = _scope_kwargs(access_scope)
        target_platform = str(context.get("target_platform") or "").strip().casefold()
        task_config = context.get("task_config")
        workflow_version = (
            str(task_config.get("workflow_version") or "").strip().casefold()
            if isinstance(task_config, dict)
            else ""
        )
        is_h5_v2 = target_platform == "h5" and workflow_version == "v2"
        is_web_v2 = target_platform == "web" and workflow_version == "v2"
        is_app_v2_full = (
            target_platform in {"app", "android"}
            and workflow_version == "v2"
            and isinstance(task_config, dict)
            and task_config.get("execution_mode") in (None, "full_inspection")
        )
        h5_work_items: list[dict[str, Any]] = []
        h5_inventory: list[dict[str, Any]] = []
        h5_full_closure: dict[str, Any] = {}
        h5_coverage: dict[str, Any] | None = None
        if (
            (is_web_v2 or is_h5_v2)
            and context.get("finalization_state") == "sealed"
            and context.get("completion_gate_passed") is True
            and context.get("sealed_report_bound") is True
        ):
            if callable(snapshot_reader):
                raise ReportInsightNotFoundError(
                    f"{target_platform.upper()} 正式报告绑定存在但不可读取不可变快照："
                    f"{task_id}"
                )
        rows, recognized_pages, page_rows, exploration_actions = await asyncio.gather(
            # 按质检规则统一口径：规则结果不分 Run；页面与探索动作仍按当前 Run
            self._repository.list_rule_execution_rows(
                task_id=task_id,
                agent_run_id=None,
                **scope_kwargs,
            ),
            self._repository.count_recognized_pages(
                task_id=task_id,
                agent_run_id=run_id,
                **scope_kwargs,
            ),
            self._repository.list_page_capture_inspection_rows(
                task_id=task_id,
                agent_run_id=run_id,
                **scope_kwargs,
            ),
            self._repository.list_exploration_actions(
                agent_run_id=run_id,
                **scope_kwargs,
            ),
        )
        rows = [qualify_report_execution(row) for row in rows]
        assessments, assessment_scope_complete = extract_page_type_assessment_context(
            context.get("working_memory")
        )
        required_page_types = extract_page_type_assessment_scope_page_types(
            context.get("working_memory")
        )
        if run_id is not None and (target_platform == "h5" or is_web_v2):
            h5_page_reader = getattr(
                self._repository,
                "list_h5_page_captures",
                None,
            )
            h5_action_reader = getattr(
                self._repository,
                "list_h5_action_executions",
                None,
            )
            if callable(h5_page_reader) and callable(h5_action_reader):
                h5_pages, h5_actions = await asyncio.gather(
                    h5_page_reader(task_id=task_id, agent_run_id=run_id),
                    h5_action_reader(task_id=task_id, agent_run_id=run_id),
                )
                page_rows = [
                    item for item in h5_pages if item.get("capture_role") != "before_action"
                ]
                exploration_actions = h5_actions
        if is_h5_v2 or is_web_v2:
            assessments = []
            assessment_scope_complete = False
            h5_assessment_reader = getattr(
                self._repository,
                "list_h5_page_type_assessments",
                None,
            )
            if run_id is not None and callable(h5_assessment_reader):
                h5_assessments = await h5_assessment_reader(
                    task_id=task_id,
                    agent_run_id=run_id,
                )
                assessments = project_h5_page_type_assessments(h5_assessments)
                assessment_scope_complete = bool(h5_assessments)
        # 经过的页面不进报告的页面列表、页数和完成结论
        inspection_scope = inspection_scope_page_types(task_config)
        page_rows = [
            row
            for row in page_rows
            if page_type_in_inspection_scope(inspection_scope, row.get("page_type"))
        ]
        assessments = [
            item
            for item in assessments
            if page_type_in_inspection_scope(inspection_scope, item.get("page_type"))
        ]
        if target_platform == "h5":
            coverage_reader = getattr(self._repository, "fetch_h5_coverage_snapshot", None)
            if callable(coverage_reader) and run_id is not None:
                h5_coverage = await coverage_reader(
                    task_id=task_id,
                    agent_run_id=run_id,
                )
                if isinstance(h5_coverage, dict):
                    required_page_types = h5_coverage.get("required_page_types")
        if is_h5_v2 and run_id is not None:
            inventory_reader = getattr(
                self._repository,
                "list_h5_evidence_inventory",
                None,
            )
            closure_reader = getattr(
                self._repository,
                "fetch_h5_full_inspection_closure",
                None,
            )
            work_reader = getattr(
                self._repository,
                "list_h5_v2_work_items",
                None,
            )
            if callable(work_reader):
                loaded_work_items = await work_reader(
                    task_id=task_id,
                    agent_run_id=run_id,
                )
                if isinstance(loaded_work_items, list):
                    h5_work_items = loaded_work_items
            if callable(inventory_reader):
                loaded_inventory = await inventory_reader(
                    task_id=task_id,
                    agent_run_id=run_id,
                )
                if isinstance(loaded_inventory, list):
                    h5_inventory = loaded_inventory
            if callable(closure_reader):
                loaded_closure = await closure_reader(
                    task_id=task_id,
                    agent_run_id=run_id,
                )
                if isinstance(loaded_closure, dict):
                    h5_full_closure = loaded_closure
        web_coverage: dict[str, Any] | None = None
        if target_platform == "web" and run_id is not None:
            web_coverage_reader = getattr(
                self._repository,
                "fetch_web_coverage_snapshot",
                None,
            )
            if callable(web_coverage_reader):
                loaded_web_coverage = await web_coverage_reader(
                    task_id=task_id,
                    agent_run_id=run_id,
                )
                if isinstance(loaded_web_coverage, dict):
                    web_coverage = dict(loaded_web_coverage)
                    required_page_types = web_coverage.get("required_page_types")
        web_logical_pages: list[dict[str, Any]] = []
        if is_web_v2:
            from services.api.app.application.web_insight_identity import (
                load_web_insight_identities,
            )
            identities, web_identity_error = await load_web_insight_identities(self._repository, task_id=task_id, agent_run_id=run_id)
            web_identity_diagnostics = [
                {"capture_id": str(row.get("page_capture_id") or ""),
                 "reason_code": identities.get(str(row.get("page_capture_id")), {}).get("logical_page_identity_status") or web_identity_error or "WEB_LOGICAL_ID_UNTRUSTED"}
                for row in page_rows
                if identities.get(str(row.get("page_capture_id")), {}).get("logical_page_identity_status") != "verified"
            ]
            if web_identity_diagnostics and not web_identity_error:
                web_identity_error = web_identity_diagnostics[0]["reason_code"]
            page_rows, web_logical_pages = _web_report_logical_pages(
                page_rows,
                assessments,
                trusted_identities=identities,
            )
            recognized_pages = sum(
                item.get("classification_status") == "classified"
                for item in web_logical_pages
        )
        page_exploration = build_page_exploration_summary(
            pages=page_rows,
            actions=exploration_actions,
            page_type_assessments=assessments,
            assessment_scope_complete=assessment_scope_complete,
            required_page_types=required_page_types,
            inspection_scope=inspection_scope,
        )
        if is_h5_v2:
            evidence_preflight = project_h5_evidence_preflight(
                inventory_items=h5_inventory,
                rule_executions=rows,
                closure=h5_full_closure,
            )
            page_exploration["evidence_preflight"] = evidence_preflight
            if evidence_preflight["status"] in {
                "evidence_insufficient",
                "evidence_integrity_failed",
            }:
                page_exploration.update(
                    complete=False,
                    quality_status=(
                        "technical_failed"
                        if evidence_preflight["status"] == "evidence_integrity_failed"
                        else "needs_review"
                    ),
                    requires_review=True,
                    terminal_reason=evidence_preflight["status"],
                )
            terminal = project_h5_terminal(
                work_items=h5_work_items,
                frontier=latest_h5_frontier(h5_work_items),
                assessments=assessments,
                inventory=h5_inventory,
                full_inspection_closure=h5_full_closure,
                action_count=len(exploration_actions),
                action_ids={
                    action_id
                    for item in exploration_actions
                    if (
                        action_id := _optional_snapshot_text(
                            item.get("action_execution_id")
                        )
                    )
                },
                assessment_scope_ready=(
                    isinstance(h5_coverage, dict)
                    and h5_coverage.get("assessment_scope_ready") is True
                ),
                coverage_ready=h5_target_coverage_ready(h5_coverage),
            )
            page_exploration.update(terminal)
            failure_facts = classify_h5_run_failure(
                context, page_rows, rows, inventory=h5_inventory, actions=exploration_actions,
            )
            if failure_facts.completely_failed:
                page_exploration.update(
                    complete=False, quality_status="technical_failed", requires_review=False,
                    terminal_reason=failure_facts.technical_failure_reason,
                )
        if web_coverage is not None:
            page_exploration["web_coverage"] = web_coverage
        if is_web_v2:
            page_exploration["logical_page_total"] = len(web_logical_pages)
        popup_wall = _parse_popup_wall(context.get("run_error"))
        if popup_wall is not None:
            page_exploration["popup_wall"] = popup_wall
        if str(context.get("target_platform") or "").casefold() == "app":
            stage_reader = getattr(self._repository, "fetch_app_stage", None)
            if callable(stage_reader):
                app_stage = await stage_reader(task_id)
                app_exploration = app_stage.get("app_exploration")
                coverage = (
                    app_exploration.get("coverage_summary")
                    if isinstance(app_exploration, dict)
                    else None
                )
                if isinstance(coverage, dict):
                    apply_app_page_type_applicability(
                        page_exploration,
                        app_coverage=coverage,
                    )
                    complete = (
                        coverage.get("coverage_complete") is True
                        and coverage.get("visual_quality_satisfied") is True
                        and app_exploration.get("outcome") == "exploration_completed"
                    )
                    page_exploration.update(
                        tracking_available=True,
                        complete=complete,
                        quality_status=("complete" if complete else "needs_review"),
                        requires_review=not complete,
                        app_coverage=dict(coverage),
                    )
        items = [
            self._rule_item(row, web_coverage=web_coverage)
            for row in rows
        ]
        await project_open_green_report_results(
            repository=self._repository,
            context=context,
            task_id=task_id,
            items=items,
            display_group=_display_group,
            access_scope=access_scope,
        )
        await self._attach_rule_rulesets(items)
        distribution_counts = {status: 0 for status in _DISTRIBUTION_ORDER}
        for item in items:
            distribution_counts[item["display_status"]] += 1
        quality_executions = [
            item
            for item in items
            if rule_execution_counts_as_used(
                item["execution_status"],
                item["execution_reason"],
                is_bootstrap=item["is_bootstrap"],
                machine_display_status=item.get("machine_display_status"),
            )
        ]
        total = len(quality_executions)
        passed = sum(item["display_status"] == "passed" for item in quality_executions)
        failed = sum(item["display_status"] == "failed" for item in quality_executions)
        confirmed_not_applicable = sum(
            item["display_status"] == "not_applicable" for item in quality_executions
        )
        # warning 在报告业务分布中已归入 failed 桶，表示已完成但不通过，不能重复累计。
        qc_success_count = sum(
            item["display_status"] in {"passed", "failed", "not_applicable"}
            and green_report_item_is_resolved(item)
            for item in quality_executions
        )
        qc_success_rate = round(qc_success_count / total * 100, 1) if total else 0.0
        conclusive_count = passed + failed
        pass_rate = round(passed / conclusive_count * 100, 1) if conclusive_count else 0.0
        coverage_gap = distribution_counts["coverage_gap"]
        candidate_count = len(items) - coverage_gap
        business_result_count = sum(
            not item["is_coverage_gap"] and rule_execution_counts_as_completed(
                item["execution_status"], item["execution_reason"],
                is_bootstrap=item["is_bootstrap"], result_summary=item["result_summary"],
            )
            for item in quality_executions
        )
        completed_candidate_count = sum(
            not item["is_coverage_gap"] and green_report_item_is_resolved(item)
            and rule_execution_counts_as_completed(
                item["execution_status"], item["execution_reason"],
                is_bootstrap=item["is_bootstrap"], result_summary=item["result_summary"],
            )
            for item in quality_executions
        )
        confirm_items = [item for item in items if item["confirmable"]]
        confirmed = [
            item for item in confirm_items
            if item["human_verdict"] is not None and green_report_item_is_resolved(item)
        ]
        pending = len(confirm_items) - len(confirmed)
        task_config = context.get("task_config") or {}
        is_terminal = context["status"] in _TERMINAL_TASK_STATUSES
        report_stage = (
            "final"
            if context.get("finalization_state") == "sealed"
            and context.get("sealed_report_bound") is True
            and (
                target_platform not in {"web", "h5"}
                or context.get("completion_gate_passed") is True
            )
            else ("final_review" if is_terminal else "preview")
        )
        app_availability = context.get("app_report_availability")
        if is_app_v2_full:
            # 当前任务、Run 和封存绑定由仓储统一校验，旧宽泛标记不能替代正式门禁。
            if app_availability not in {"none", "preview", "final"}:
                app_availability = "none"
            report_stage = (
                "final" if app_availability == "final"
                else ("final_review" if is_terminal else "preview")
            )
        insight = {
            "task_id": task_id,
            "summary": {
                "task_name": task_config.get("task_name") or context["target_url"],
                "task_id": task_id,
                "target_url": context["target_url"],
                "task_status": context["status"],
                "target_platform": target_platform,
                "started_at": context.get("started_at"),
                "finished_at": context.get("finished_at"),
                "created_at": context.get("created_at"),
                "generated_at": context.get("finished_at"),
                "report_stage": report_stage,
                "page_recognized_count": recognized_pages,
                "total_rules": total,
                "evaluated_rules": completed_candidate_count,
                "candidate_rule_count": candidate_count,
                "completed_candidate_rules": completed_candidate_count,
                "full_rule_pass_rate": (round(passed / len(items) * 100, 1) if items else 0.0),
                "qc_success_rate": qc_success_rate,
                "pass_rate": pass_rate,
                "candidate_execution_coverage": (
                    round(completed_candidate_count / candidate_count * 100, 1)
                    if candidate_count
                    else 0.0
                ),
                "ruleset_candidate_coverage": (
                    round(candidate_count / len(items) * 100, 1) if items else 0.0
                ),
                "confirmed_not_applicable_rules": confirmed_not_applicable,
                "coverage_gap_rules": coverage_gap,
                "run_id": run_id,
                "ruleset_code": context.get("ruleset_code"),
                "ruleset_version": context.get("ruleset_version"),
                # 组合规则集的编码没有可读性，页面显示名称（如「1期质检规则集 + 2期质检规则集」）
                "ruleset_name": context.get("ruleset_name"),
                "ruleset_rule_total": context.get("ruleset_rule_total"),
            },
            "qc_success_rate": qc_success_rate,
            "pass_rate": pass_rate,
            "page_exploration": page_exploration,
            "distribution": [
                {
                    "status": status,
                    "label": _DISTRIBUTION_LABELS[status],
                    "count": distribution_counts[status],
                    "percent": (
                        round(distribution_counts[status] / len(items) * 100, 1) if items else 0.0
                    ),
                }
                for status in _DISTRIBUTION_ORDER
            ],
            "confirm_progress": {
                "total": len(confirm_items),
                "confirmed": len(confirmed),
                "pending": pending,
                "completed": pending == 0,
            },
            "export_ready": (
                pending == 0
                and total > 0
                and (not is_h5_v2 or completed_candidate_count > 0)
                and (target_platform != "web" or report_stage == "final")
            ),
            "confirm_items": confirm_items,
            "rule_items": items,
        }
        if is_app_v2_full:
            insight["report_availability"] = app_availability
            insight["is_final"] = app_availability == "final"
            insight["export_ready"] = (
                app_availability == "final" and insight["export_ready"]
            )
            insight["export_allowed"] = insight["export_ready"]
            insight["report_unavailable_reason"] = (
                "APP_REPORT_NO_BUSINESS_RESULTS" if app_availability == "none" else None
            )
            insight["incomplete_reasons"] = (
                [] if app_availability == "final" else [
                    "APP_REPORT_NO_BUSINESS_RESULTS" if app_availability == "none"
                    else "APP_REPORT_NOT_FINALIZED"
                ]
            )
        if is_h5_v2 or is_web_v2:
            has_persisted_facts = bool(
                page_rows
                or exploration_actions
                or rows
                or web_coverage
                or h5_coverage
                or h5_inventory
                or h5_work_items
            )
            if is_h5_v2:
                # 未执行、缺证和技术异常仅供过程诊断，不能成为业务报告已经生成的证据。
                has_persisted_facts = business_result_count > 0
            availability = (
                "final"
                if report_stage == "final"
                else ("preview" if has_persisted_facts else "none")
            )
            web_preview = _build_web_preview_status(
                context=context,
                logical_pages=web_logical_pages,
                rule_rows=rows,
                web_coverage=web_coverage,
                popup_wall=popup_wall,
                pending_confirmations=pending,
            )
            insight["report_availability"] = availability
            insight["is_final"] = availability == "final"
            insight["export_allowed"] = (
                availability == "final" if is_web_v2 else insight["export_ready"]
            )
            if is_web_v2 and availability != "final":
                insight["export_ready"] = False
            if is_web_v2:
                insight["report_unavailable_reason"] = (
                    "WEB_REPORT_NO_PERSISTED_FACTS" if availability == "none" else None
                )
                insight["incomplete_reasons"] = (
                    []
                    if availability == "final"
                    else _web_incomplete_reasons(availability, web_preview)
                )
                insight["web_preview"] = web_preview
            else:
                insight["report_unavailable_reason"] = (
                    "H5_REPORT_NO_BUSINESS_RESULTS" if availability == "none" else None
                )
                insight["incomplete_reasons"] = (
                    [] if availability == "final" else (
                        ["H5_REPORT_NO_BUSINESS_RESULTS"] if availability == "none"
                        else ["H5_REPORT_NOT_FINALIZED"]
                    )
                )
        if is_web_v2:
            insight["web_logical_pages"] = web_logical_pages
            insight["web_identity_error"] = web_identity_error
            insight["web_identity_diagnostics"] = web_identity_diagnostics
            from services.api.app.application.web_report_explanation import (
                load_web_report_explanation,
            )

            insight["web_report_explanation"] = await load_web_report_explanation(
                self._repository, task_id=task_id, agent_run_id=run_id, access_scope=access_scope,
            )
        await self._attach_rule_inventory(insight, task_id, access_scope, context=context)
        restrict_open_green_report_completion(insight)
        return maybe_scrub_app_report_payload(
            insight,
            target_url=context.get("target_url"),
            task_config=task_config if isinstance(task_config, dict) else None,
        )

    async def confirm_rule(
        self,
        task_id: str,
        execution_id: str,
        *,
        verdict: str,
        note: str | None = None,
        operator: str | None = None,
        access_scope: AccessScope | None = None,
    ) -> dict[str, Any]:
        if verdict not in VALID_HUMAN_VERDICTS:
            raise ReportConfirmationError(f"非法确认结论：{verdict}")
        fetch_kwargs = _scope_kwargs(access_scope)
        if access_scope is not None:
            fetch_kwargs["task_id"] = task_id
        row = await self._repository.fetch_rule_execution(execution_id, **fetch_kwargs)
        if row is None or row["task_id"] != task_id:
            raise ReportInsightNotFoundError(f"任务 {task_id} 下不存在执行记录：{execution_id}")
        row = qualify_report_execution(row)
        display = rule_display_status(
            row["execution_status"],
            row.get("execution_reason"),
            is_bootstrap=bool(row.get("is_bootstrap")),
        )
        if not rule_requires_human_confirmation(
            display, row.get("execution_reason"), result_summary=row.get("result_summary")
        ) or bool(row.get("is_bootstrap")):
            raise ReportConfirmationError(
                f"规则 {row['rule_code']} 当前为「{rule_display_label(display)}」，无需人工确认"
            )
        confirmation_context = row.get("green_confirmation_context")
        if isinstance(confirmation_context, dict):
            item = self._rule_item(row)
            await project_green_confirmation_result(
                repository=self._repository,
                context=confirmation_context,
                task_id=task_id,
                item=item,
                display_group=_display_group,
                access_scope=access_scope,
            )
            if item.get("green_result") is not None and not item["confirmable"]:
                raise ReportConfirmationError("绿色规则仍有材料缺口或正式关联待核对，不能确认完成")
        updated = await self._repository.update_human_verdict(
            execution_id=execution_id,
            verdict=verdict,
            note=note,
            operator=access_scope.actor_user_id if access_scope is not None else operator,
            **(
                {"task_id": task_id, "access_scope": access_scope}
                if access_scope is not None
                else {}
            ),
        )
        if updated is None:
            raise ReportInsightNotFoundError(f"执行记录不存在：{execution_id}")
        updated["human_verdict_label"] = HUMAN_VERDICT_LABELS.get(verdict, verdict)
        updated["rule_code"] = row["rule_code"]
        return updated

    async def export_html(
        self,
        task_id: str,
        *,
        access_scope: AccessScope | None = None,
    ) -> tuple[str, str]:
        """导出体验平台兼容报告；仍有待确认规则时抛 ``ReportExportBlockedError``。

        导出件按对方 2026-08-06 口径只收「最终不通过」三类：AI 直判不通过、
        AI 判忽略但人工二次确认不通过、AI 判异常/证据不足但人工二次确认不通过；
        通过、人工确认通过/不适用、未执行/未覆盖等异常残留都不进导出件。
        正文为体验平台同款样式，数据内嵌在 ``__edit_data__``，平台「导入」可直接续编。
        """
        insight = await self.report_insight(task_id, access_scope=access_scope)
        if green_report_has_unresolved_results(insight["rule_items"]):
            raise ReportExportBlockedError("绿色规则仍有缺证、待复核或关联待核对，暂不能导出报告")
        report_availability = insight.get("report_availability")
        if (
            insight["summary"].get("target_platform") in {"app", "android"}
            and report_availability is not None
            and report_availability != "final"
        ):
            raise ReportExportBlockedError("预览报告尚未通过门禁并封存，暂不能导出报告")
        if (
            insight["summary"].get("target_platform") == "web"
            and report_availability is not None
            and report_availability != "final"
        ):
            raise ReportExportBlockedError("预览报告尚未通过门禁并封存，暂不能导出报告")
        pending = insight["confirm_progress"]["pending"]
        if (
            insight["summary"].get("target_platform") == "h5"
            and insight.get("export_allowed") is False
        ):
            raise ReportExportBlockedError("尚无真实规则质检结果，暂不能导出业务报告")
        if pending > 0:
            raise ReportExportBlockedError(f"仍有 {pending} 条规则待人工确认，暂不能导出报告")
        rule_items = [
            item for item in insight["rule_items"] if item["display_status"] == "failed"
        ]
        omissions: dict[str, str] = {}
        images = await self._collect_export_images(
            insight,
            rule_items,
            access_scope=access_scope,
            omissions=omissions,
        )
        issues = []
        for index, item in enumerate(rule_items, start=1):
            artifact_ids = referenced_artifact_ids([item])
            issues.append(
                build_platform_issue(
                    item,
                    issue_id=index,
                    image_data_uris=[images[aid] for aid in artifact_ids if aid in images],
                    evidence_note=export_evidence_note(artifact_ids, images, omissions),
                )
            )
        summary = insight["summary"]
        is_h5_review = (
            summary.get("target_platform") == "h5"
            and (
                report_availability == "preview"
                or summary.get("report_stage") == "final_review"
            )
        )
        execution_gap_count = sum(
            bool(item.get("execution_id"))
            and rule_execution_counts_as_used(
                item.get("execution_status"),
                item.get("execution_reason"),
                is_bootstrap=bool(item.get("is_bootstrap")),
                machine_display_status=item.get("machine_display_status"),
            )
            and item["machine_display_status"]
            in {
                "not_executed",
                "coverage_gap",
                "detect_error",
                "evidence_insufficient",
                "pending",
                "running",
            }
            for item in insight["rule_items"]
        )
        actual_rule_ids = {
            item["rule_id"]
            for item in insight["rule_items"]
            if item.get("execution_id")
            and rule_execution_counts_as_used(
                item.get("execution_status"),
                item.get("execution_reason"),
                is_bootstrap=bool(item.get("is_bootstrap")),
                machine_display_status=item.get("machine_display_status"),
            )
        }
        unexecuted_rule_count = len(
            {
                item["rule_id"]
                for item in insight["rule_items"]
                if item["rule_id"] not in actual_rule_ids
                and not (
                    item.get("inventory_source") in {"frozen", "legacy_verified"}
                    and item.get("applicability") in {"not_applicable", "out_of_scope"}
                )
            }
        )
        is_review_report = (
            is_h5_review
            or execution_gap_count > 0
            or unexecuted_rule_count > 0
            or summary.get("rule_inventory_complete") is False
        )
        generated_at = summary.get("generated_at")
        report_date = (
            generated_at.strftime("%Y-%m-%d")
            if isinstance(generated_at, datetime)
            else datetime.now().strftime("%Y-%m-%d")
        )
        html_text = render_experience_report(
            title=(
                f"{summary.get('task_name') or task_id} "
                f"{'质检复核报告' if is_review_report else '质检报告'}"
            ),
            author="玉衡自动化质检",
            date=report_date,
            issues=issues,
            inspection_note=(
                f"有 {execution_gap_count} 次检测未形成结论、{unexecuted_rule_count} 条规则未实际判定。下列问题仅包含已形成的未通过结论，"
                "未执行、缺证与检测异常不作为业务通过或未通过，本报告不代表检测已完整完成。"
                if is_review_report
                else ""
            ),
        )
        if summary.get("target_platform") == "web":
            from services.api.app.application.web_report_explanation_export import (
                append_web_report_explanation,
            )

            html_text = append_web_report_explanation(html_text, insight.get("web_report_explanation"))
        html_text = append_report_rule_inventory(html_text, insight)
        return f"task_{task_id}_report.html", html_text

    # ---------- 内部 ----------

    async def _attach_h5_rule_inventory(
        self, insight: dict[str, Any], task_id: str, access_scope: AccessScope | None,
    ) -> None:
        from services.api.app.application.report_rule_inventory import project_report_rule_inventory
        from services.api.app.infrastructure.db.task_rule_inventory_repository import (
            PostgresTaskRuleInventoryRepository,
        )

        reader = self._rule_inventory_repository or PostgresTaskRuleInventoryRepository()
        snapshot = await reader.load_snapshot(
            task_id=task_id, agent_run_id=insight["summary"].get("run_id"),
            access_scope=access_scope,
        )
        project_report_rule_inventory(insight, snapshot)

    async def _frozen_execution_reasons(
        self,
        snapshot: dict[str, Any],
        *,
        task_id: str,
        access_scope: AccessScope | None,
    ) -> dict[str, str | None]:
        """历史快照缺执行原因时，按原执行 ID 补读原因，其他冻结事实保持原样。"""
        executions = [
            {key: result.get(key) for key in (
                "execution_id", "execution_status", "started_at", "finished_at"
            )}
            for result in snapshot.get("rule_results") or []
            if isinstance(result, dict)
            and result.get("execution_id")
            and "execution_reason" not in result
            and result.get("execution_status") in {"skipped", "manual_review"}
            and result.get("finished_at")
        ]
        reader = getattr(self._repository, "list_rule_execution_reasons", None)
        agent_run_id = snapshot.get("agent_run_id")
        if not executions or not agent_run_id or not callable(reader):
            return {}
        return await reader(
            task_id=task_id,
            executions=executions,
            agent_run_id=agent_run_id,
            **_scope_kwargs(access_scope),
        )

    async def _attach_rule_rulesets(self, items: list[dict[str, Any]]) -> None:
        """给每条规则结果带上规则所属的规则集（1期 / 2期 / 3期瑞升…），报告页按期分组展示。

        只是展示用的归属，不改结论；查不到（旧库、快照里的规则已删）时两个键都是 None。
        """
        reader = getattr(self._repository, "list_rule_rulesets", None)
        rule_ids = sorted({str(item.get("rule_id") or "") for item in items} - {""})
        rulesets = await reader(rule_ids=rule_ids) if callable(reader) and rule_ids else {}
        for item in items:
            ruleset = rulesets.get(str(item.get("rule_id") or "")) or {}
            item["ruleset_code"] = item.get("ruleset_code") or ruleset.get("ruleset_code")
            item["ruleset_name"] = item.get("ruleset_name") or ruleset.get("ruleset_name")

    def _rule_item(
        self,
        row: dict[str, Any],
        *,
        web_coverage: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        is_coverage_gap = _is_web_coverage_gap_row(
            row,
            web_coverage=web_coverage,
        )
        machine_display = (
            "coverage_gap"
            if is_coverage_gap
            else rule_display_status(
            row["execution_status"],
            row.get("execution_reason"),
            is_bootstrap=bool(row.get("is_bootstrap")),
                error_code=row.get("error_code"),
            )
        )
        severity = row.get("severity") or row.get("rule_severity")
        if machine_display == "manual_review" and not rule_requires_human_confirmation(
            machine_display, row.get("execution_reason"), result_summary=row.get("result_summary")
        ):
            machine_display = "detect_error"
        verdict = row.get("human_verdict")
        has_business_result = machine_display in {
            "passed", "failed", "not_detected", "not_applicable", "manual_review"
        } and (machine_display != "manual_review" or rule_requires_human_confirmation(
            machine_display, row.get("execution_reason"), result_summary=row.get("result_summary")
        ))
        display = _display_group(
            rule_final_display_status(
            row["execution_status"],
            row.get("execution_reason"),
            human_verdict=verdict,
            is_bootstrap=bool(row.get("is_bootstrap")),
                error_code=row.get("error_code"),
            ) if has_business_result else machine_display
        )
        confirmable = not bool(row.get("is_bootstrap")) and rule_requires_human_confirmation(
            machine_display, row.get("execution_reason"), result_summary=row.get("result_summary")
        )
        applicable = row.get("applicable_page_types") or []
        from services.api.app.application.h5_journey_result_read import project_live_journey_result

        return {
            "item_type": "execution",
            "execution_id": row["execution_id"],
            "rule_id": row["rule_id"],
            "rule_code": row["rule_code"],
            "rule_name": row["rule_name"],
            "category": row.get("category"),
            "rule_package": row.get("rule_package") or rule_package_fallback(row.get("category")),
            "trigger_display": row.get("trigger_display") or trigger_display_fallback(applicable),
            "bound_page_type": row.get("capture_page_type"),
            "bound_page_type_name": page_type_display_name(row.get("capture_page_type")),
            "display_status": display,
            "display_status_label": _DISTRIBUTION_LABELS[display],
            "machine_display_status": machine_display,
            "machine_display_status_label": rule_display_label(machine_display),
            "execution_status": row.get("execution_status"),
            "execution_reason": effective_rule_execution_reason(
                row["execution_status"],
                row.get("execution_reason"),
                error_code=row.get("error_code"),
                judge_input_source=_judge_input_source(row.get("judge_input")),
            ),
            "severity": severity,
            "severity_display": severity_display(severity),
            "confidence": row.get("confidence"),
            "result_summary": humanize_qc_text(row.get("result_summary")),
            "problem_desc": humanize_qc_text(row.get("problem_desc")),
            "check_point": row.get("check_point"),
            "evidence_artifact_ids": row.get("evidence_artifact_ids") or [],
            "evidence_input_set_id": row.get("evidence_input_set_id"),
            "journey_result": project_live_journey_result(row),
            "missing_evidence": row.get("missing_evidence") or [],
            "page_capture_id": row.get("page_capture_id"),
            "is_bootstrap": bool(row.get("is_bootstrap")),
            "is_coverage_gap": is_coverage_gap,
            "finished_at": row.get("finished_at"),
            "confirmable": confirmable,
            "human_verdict": verdict,
            "human_verdict_label": human_verdict_label(verdict),
            "human_verdict_note": row.get("human_verdict_note"),
            "human_verdict_by": row.get("human_verdict_by"),
            "human_verdict_at": row.get("human_verdict_at"),
            "confirm_status": (
                None if not confirmable else ("confirmed" if verdict is not None else "pending")
            ),
        }

    async def _collect_export_images(
        self,
        insight: dict[str, Any],
        confirm_items: list[dict[str, Any]],
        *,
        access_scope: AccessScope | None = None,
        omissions: dict[str, str] | None = None,
    ) -> dict[str, str]:
        """为规则内嵌已有多张截图；未内嵌材料由正文明确说明。"""
        if omissions is None:
            omissions = {}
        if (
            insight.get("report_availability") == "final"
            and insight["summary"].get("target_platform") in {"web", "h5"}
        ):
            return await self._collect_frozen_export_images(insight, confirm_items, omissions=omissions)
        artifact_ids = referenced_artifact_ids(confirm_items)
        run_id = insight["summary"].get("run_id")
        try:
            artifacts = await self._repository.list_screenshot_artifacts(
                task_id=insight["task_id"],
                agent_run_id=run_id,
                **_scope_kwargs(access_scope),
            )
        except Exception:  # noqa: BLE001 - 导出容忍证据查询失败
            omissions.update({artifact_id: "材料目录查询失败" for artifact_id in artifact_ids})
            return {}
        meta_by_id = {
            str(item["artifact_id"]): item
            for item in artifacts
        }
        return await collect_export_evidence(
            artifact_ids, meta_by_id, self._resolve_evidence_store(),
            omissions=omissions, max_bytes=_EXPORT_IMAGE_MAX_BYTES,
            max_count=_EXPORT_IMAGE_MAX_COUNT,
        )

    async def _collect_frozen_export_images(
        self,
        insight: dict[str, Any],
        confirm_items: list[dict[str, Any]],
        *,
        omissions: dict[str, str] | None = None,
    ) -> dict[str, str]:
        """正式导出仅使用正文冻结证据，并在读取后核验 SHA-256。"""
        if omissions is None:
            omissions = {}
        frozen_by_id: dict[str, dict[str, Any]] = {}
        for entry in insight.get("evidence_entries") or []:
            if not isinstance(entry, dict):
                continue
            artifact_id = str(entry.get("artifact_id") or "")
            if artifact_id:
                frozen_by_id[artifact_id] = entry
        return await collect_export_evidence(
            referenced_artifact_ids(confirm_items), frozen_by_id, self._resolve_evidence_store(),
            omissions=omissions, max_bytes=_EXPORT_IMAGE_MAX_BYTES,
            max_count=_EXPORT_IMAGE_MAX_COUNT, require_checksum=True,
        )

    def _resolve_evidence_store(self) -> Any:
        if self._evidence_store is None:
            if self._evidence_store_factory is not None:
                self._evidence_store = self._evidence_store_factory()
            else:
                from services.api.app.core.config import get_settings
                from services.api.app.infrastructure.storage.factory import (
                    build_evidence_artifact_store,
                )

                self._evidence_store = build_evidence_artifact_store(
                    get_settings().evidence_storage
                )
        return self._evidence_store

    async def _attach_ruleset_rule_total(
        self,
        insight: dict[str, Any],
        task_id: str,
        access_scope: AccessScope | None,
    ) -> None:
        """定稿快照里没存「规则总数」（建任务时的启用规则条数），从任务上补；查不到就不给。"""

        context = await self._repository.fetch_task_context(
            task_id,
            **_scope_kwargs(access_scope),
        )
        total = context.get("ruleset_rule_total") if isinstance(context, dict) else None
        insight["summary"]["ruleset_rule_total"] = total

    async def _attach_rule_inventory(
        self, insight: dict[str, Any], task_id: str, access_scope: AccessScope | None,
        *, context: dict[str, Any] | None = None,
    ) -> None:
        """使用同任务权威清单补候选；执行质检率与不可变执行事实保持原口径。"""
        if insight["summary"].get("target_platform") == "h5":
            await self._attach_h5_rule_inventory(insight, task_id, access_scope)
            return
        reader = getattr(self._rule_inventory_repository, "load_snapshot", None)
        if not callable(reader):
            return
        if context is None:
            context = await self._repository.fetch_task_context(task_id, **_scope_kwargs(access_scope))
        if not isinstance(context, dict) or not requires_task_rule_inventory(
            context.get("target_platform"), context.get("task_config") or {},
        ):
            return
        snapshot = await reader(task_id=task_id, **_scope_kwargs(access_scope))
        project_report_rule_inventory(insight, snapshot or {
            "inventory_status": "unavailable", "unavailable_reason": "task_inventory_not_found",
        })
        await self._attach_rule_rulesets(insight["rule_items"])

    async def _require_context(
        self,
        task_id: str,
        *,
        access_scope: AccessScope | None = None,
    ) -> dict[str, Any]:
        context = await self._repository.fetch_task_context(
            task_id,
            **_scope_kwargs(access_scope),
        )
        if context is None:
            raise ReportInsightNotFoundError(f"未找到任务：{task_id}")
        return context


def _build_web_preview_status(
    *,
    context: dict[str, Any],
    logical_pages: list[dict[str, Any]],
    rule_rows: list[dict[str, Any]],
    web_coverage: dict[str, Any] | None,
    popup_wall: dict[str, Any] | None,
    pending_confirmations: int,
) -> dict[str, Any]:
    """汇总预览报告的可信事实、复核项、阻断和收口缺口。"""

    completed_rules = sum(
        str(row.get("execution_status") or "") in _TERMINAL_RULE_EXECUTION_STATUSES
        for row in rule_rows
    )
    review_pages = [
        page
        for page in logical_pages
        if page.get("classification_status") != "classified" or not page.get("page_type")
    ]
    coverage = web_coverage or {}
    missing_page_types = sorted(
        str(value) for value in coverage.get("missing_page_types") or [] if value
    )
    unrecognized_page_types = sorted(
        str(value) for value in coverage.get("unrecognized_page_types") or [] if value
    )
    gate_gaps: list[str] = []
    if context.get("completion_gate_passed") is not True:
        gate_gaps.append("completion_gate_not_passed")
    if context.get("sealed_report_bound") is not True:
        gate_gaps.append("report_binding_pending")
    if missing_page_types:
        gate_gaps.append("required_page_types_missing")
    if unrecognized_page_types or review_pages:
        gate_gaps.append("page_type_review_pending")
    if pending_confirmations:
        gate_gaps.append("manual_confirmation_pending")
    obstructions: list[dict[str, Any]] = []
    if popup_wall is not None:
        obstructions.append({"kind": "popup_wall", **popup_wall})
    for page in review_pages:
        obstructions.append(
            {
                "kind": "page_type_review",
                "logical_page_id": page.get("logical_page_id"),
                "canonical_url": page.get("canonical_url"),
                "reason": page.get("review_reason") or page.get("classification_status"),
            }
        )
    return {
        "trusted_capture_count": sum(
            bool(page.get("primary_capture_id")) for page in logical_pages
        ),
        "inspection_progress": {
            "total": len(rule_rows),
            "completed": completed_rules,
            "pending": max(len(rule_rows) - completed_rules, 0),
        },
        "page_type_review": {
            "total": len(logical_pages),
            "pending": len(review_pages),
            "completed": max(len(logical_pages) - len(review_pages), 0),
        },
        "obstructions": obstructions,
        "completion_gate": {
            "gate_passed": context.get("completion_gate_passed") is True,
            "report_bound": context.get("sealed_report_bound") is True,
            "missing_page_types": missing_page_types,
            "unrecognized_page_types": unrecognized_page_types,
            "pending_confirmations": pending_confirmations,
            "gaps": gate_gaps,
        },
    }


def _web_incomplete_reasons(availability: str, web_preview: dict[str, Any]) -> list[str]:
    if availability == "none":
        return ["WEB_REPORT_NO_PERSISTED_FACTS"]
    reasons = ["WEB_REPORT_NOT_FINALIZED"]
    reason_codes = {
        "completion_gate_not_passed": "WEB_COMPLETION_GATE_NOT_PASSED",
        "report_binding_pending": "WEB_REPORT_BINDING_PENDING",
        "required_page_types_missing": "WEB_REQUIRED_PAGE_TYPES_MISSING",
        "page_type_review_pending": "WEB_PAGE_TYPE_REVIEW_PENDING",
        "manual_confirmation_pending": "WEB_MANUAL_CONFIRMATION_PENDING",
    }
    gate = web_preview.get("completion_gate")
    if isinstance(gate, dict):
        for gap in gate.get("gaps") or []:
            code = reason_codes.get(str(gap))
            if code is not None and code not in reasons:
                reasons.append(code)
    return reasons


def _is_h5_v2_final_snapshot(snapshot: dict[str, Any], *, task_id: str) -> bool:
    task = snapshot.get("task") if isinstance(snapshot.get("task"), dict) else {}
    task_config = (
        task.get("task_config") if isinstance(task.get("task_config"), dict) else {}
    )
    completion_gate = (
        snapshot.get("completion_gate")
        if isinstance(snapshot.get("completion_gate"), dict)
        else {}
    )
    agent_run_id = str(snapshot.get("agent_run_id") or "").strip()
    report_binding_version = snapshot.get("report_binding_version")
    finalization_epoch = completion_gate.get("finalization_epoch")
    return (
        str(task.get("task_id") or "") == task_id
        and str(task.get("target_platform") or "").strip().casefold() == "h5"
        and str(task_config.get("workflow_version") or "").strip().casefold() == "v2"
        and task_config.get("execution_mode") == "full_inspection"
        and bool(agent_run_id)
        and snapshot.get("report_id") == f"task:{task_id}:run:{agent_run_id}"
        and isinstance(report_binding_version, int)
        and not isinstance(report_binding_version, bool)
        and report_binding_version > 0
        and snapshot.get("report_stage") == "final"
        and snapshot.get("is_final") is True
        and bool(str(completion_gate.get("gate_snapshot_id") or "").strip())
        and isinstance(finalization_epoch, int)
        and not isinstance(finalization_epoch, bool)
        and finalization_epoch >= 0
        and completion_gate.get("gate_passed") is True
    )


def _bound_final_snapshot_insight(
    snapshot: dict[str, Any],
    *,
    target_platform: str,
    execution_reasons: dict[str, str | None] | None = None,
) -> dict[str, Any]:
    """把 Gate 绑定的不可变正文投影成报告中心读模型。"""

    task = snapshot.get("task") if isinstance(snapshot.get("task"), dict) else {}
    frozen_summary = (
        snapshot.get("summary") if isinstance(snapshot.get("summary"), dict) else {}
    )
    page_exploration = (
        snapshot.get("page_exploration")
        if isinstance(snapshot.get("page_exploration"), dict)
        else {}
    )
    evidence_by_execution: dict[str, list[str]] = {}
    for evidence in snapshot.get("evidence_entries") or []:
        if not isinstance(evidence, dict):
            continue
        execution_id = _optional_snapshot_text(evidence.get("rule_execution_id"))
        artifact_id = _optional_snapshot_text(evidence.get("artifact_id"))
        if execution_id is not None and artifact_id is not None:
            evidence_by_execution.setdefault(execution_id, []).append(artifact_id)

    page_type_by_capture = _snapshot_page_types(page_exploration)
    rule_items: list[dict[str, Any]] = []
    distribution_counts = {status: 0 for status in _DISTRIBUTION_ORDER}
    for result in snapshot.get("rule_results") or []:
        if not isinstance(result, dict):
            continue
        execution_id = str(result.get("execution_id") or "")
        execution_status = str(result.get("execution_status") or "error")
        is_bootstrap = str(result.get("source") or "") == "bootstrap"
        execution_reason = (
            result["execution_reason"] if "execution_reason" in result
            else (execution_reasons or {}).get(execution_id)
        )
        # 既有冻结诊断码也能证明模型调用故障，无需把缺原因误读为有效业务结论。
        if not str(execution_reason or "").strip():
            execution_reason = next(
                (code for code in result.get("diagnostic_codes") or []
                 if str(code).startswith("semantic_provider_")
                 or str(code).strip().lower() in RULE_EVIDENCE_GAP_REASONS),
                None,
            )
        execution_reason = effective_rule_execution_reason(
            execution_status, execution_reason,
            error_code=next((
                str(code) for code in result.get("diagnostic_codes") or []
                if str(code).strip().lower() in RULE_EVIDENCE_GAP_REASONS
            ), None),
        )
        machine_display = rule_display_status(
            execution_status,
            execution_reason,
            is_bootstrap=is_bootstrap,
        )
        reason_unverified = (
            execution_status == "skipped" and not is_bootstrap
            and "execution_reason" not in result
            and execution_id not in (execution_reasons or {})
        )
        if reason_unverified:
            machine_display = "evidence_insufficient"
        display = _display_group(machine_display)
        distribution_counts[display] += 1
        severity = result.get("severity")
        capture_id = _optional_snapshot_text(result.get("page_capture_id"))
        page_type = page_type_by_capture.get(capture_id) if capture_id is not None else None
        direct_evidence_ids = (
            _snapshot_text_list(result.get("evidence_artifact_ids"))
            if target_platform == "h5"
            else []
        )
        rule_items.append(
            {
                "execution_id": execution_id,
                "rule_id": str(result.get("rule_id") or ""),
                "rule_code": str(result.get("rule_code") or ""),
                "rule_name": str(result.get("rule_name") or result.get("rule_code") or ""),
                "category": result.get("category"),
                "rule_package": result.get("rule_package")
                or rule_package_fallback(result.get("category")),
                "trigger_display": result.get("trigger_display")
                or trigger_display_fallback(result.get("applicable_page_types") or []),
                "bound_page_type": page_type,
                "bound_page_type_name": page_type_display_name(page_type),
                "display_status": display,
                "display_status_label": _DISTRIBUTION_LABELS[display],
                "machine_display_status": machine_display,
                "machine_display_status_label": (
                    "无法判定（历史执行原因缺失）" if reason_unverified
                    else rule_display_label(machine_display)
                ),
                "execution_status": execution_status,
                "execution_reason": execution_reason,
                "severity": severity,
                "severity_display": severity_display(severity),
                "confidence": result.get("confidence"),
                "result_summary": humanize_qc_text(result.get("result_summary")),
                "problem_desc": humanize_qc_text(result.get("problem_desc")),
                "check_point": result.get("check_point"),
                "evidence_artifact_ids": (
                    direct_evidence_ids
                    or evidence_by_execution.get(execution_id, [])
                ),
                "evidence_input_set_id": (
                    _optional_snapshot_text(result.get("evidence_input_set_id"))
                    if target_platform == "h5"
                    else None
                ),
                "missing_evidence": (
                    _snapshot_text_list(result.get("missing_evidence"))
                    if target_platform == "h5"
                    else []
                ),
                "page_capture_id": capture_id,
                "is_bootstrap": is_bootstrap,
                "is_coverage_gap": display == "coverage_gap",
                "finished_at": result.get("finished_at"),
                "confirmable": False,
                "human_verdict": None,
                "human_verdict_label": None,
                "human_verdict_note": None,
                "human_verdict_by": result.get("human_verdict_by"),
                "human_verdict_at": result.get("human_verdict_at"),
                "confirm_status": None,
            }
        )

    total = len(rule_items)
    passed = distribution_counts["passed"]
    failed = distribution_counts["failed"]
    not_applicable = distribution_counts["not_applicable"]
    conclusive = passed + failed
    qc_success_rate = round((passed + failed + not_applicable) / total * 100, 1) if total else 0.0
    pass_rate = round(passed / conclusive * 100, 1) if conclusive else 0.0
    recognized_pages = int(
        frozen_summary.get("page_recognized_count")
        or page_exploration.get("recognized_pages")
        or page_exploration.get("recognized_page_count")
        or 0
    )
    generated_at = snapshot.get("generated_at") or task.get("finished_at")
    summary = {
        "task_name": task.get("task_name") or task.get("target_url"),
        "task_id": str(task.get("task_id") or ""),
        "target_url": task.get("target_url"),
        "task_status": task.get("status"),
        "target_platform": target_platform,
        "started_at": task.get("started_at"),
        "finished_at": task.get("finished_at"),
        "created_at": task.get("created_at"),
        "generated_at": generated_at,
        "report_stage": "final",
        "page_recognized_count": recognized_pages,
        "total_rules": total,
        "full_rule_pass_rate": round(passed / total * 100, 1) if total else 0.0,
        "qc_success_rate": qc_success_rate,
        "pass_rate": pass_rate,
        "candidate_execution_coverage": _snapshot_number(
            frozen_summary.get("candidate_execution_coverage"),
            0.0,
        ),
        "ruleset_candidate_coverage": _snapshot_number(
            frozen_summary.get("ruleset_candidate_coverage"),
            0.0,
        ),
        "confirmed_not_applicable_rules": distribution_counts["not_applicable"],
        "coverage_gap_rules": distribution_counts["coverage_gap"],
        "run_id": snapshot.get("agent_run_id"),
        "ruleset_code": task.get("ruleset_code"),
        "ruleset_version": task.get("ruleset_version"),
        "ruleset_name": task.get("ruleset_name"),
    }
    frozen_logical_pages = snapshot.get("web_logical_pages")
    if not isinstance(frozen_logical_pages, list):
        frozen_logical_pages = page_exploration.get("web_logical_pages")
    insight = {
        "task_id": summary["task_id"],
        "summary": summary,
        "qc_success_rate": qc_success_rate,
        "pass_rate": pass_rate,
        "page_exploration": page_exploration,
        "distribution": [
            {
                "status": status,
                "label": _DISTRIBUTION_LABELS[status],
                "count": distribution_counts[status],
                "percent": (round(distribution_counts[status] / total * 100, 1) if total else 0.0),
            }
            for status in _DISTRIBUTION_ORDER
        ],
        "confirm_progress": {"total": 0, "confirmed": 0, "pending": 0, "completed": True},
        "export_ready": True,
        "confirm_items": [],
        "rule_items": rule_items,
        "report_availability": "final",
        "is_final": True,
        "export_allowed": True,
        "incomplete_reasons": [],
        "evidence_entries": [
            dict(item) for item in snapshot.get("evidence_entries") or [] if isinstance(item, dict)
        ],
    }
    if target_platform == "h5" and "h5_journeys" in snapshot:
        from services.api.app.application.journey_report_binding import verified_journey_report

        insight["h5_journeys"] = verified_journey_report(
            snapshot["h5_journeys"],
            task_id=summary["task_id"],
            agent_run_id=snapshot.get("agent_run_id"),
            epoch=(snapshot.get("completion_gate") or {}).get("finalization_epoch"),
            source_epoch=((snapshot.get("page_exploration") or {}).get("h5_coverage") or {}).get(
                "finalization_epoch"
            ),
        )
        from services.api.app.application.h5_journey_result_read import project_journey_result

        for item in rule_items:
            matching = [result for result in insight["h5_journeys"]["results"]
                        if result.get("execution_id") == item["execution_id"]]
            item["journey_result"] = project_journey_result(
                matching[0], execution_id=item["execution_id"],
                input_set_id=item["evidence_input_set_id"],
                artifact_ids=item["evidence_artifact_ids"],
            ) if len(matching) == 1 else None
    if target_platform == "web":
        insight["web_logical_pages"] = frozen_logical_pages or []
        insight["web_report_explanation"] = page_exploration.get(
            "web_report_explanation"
        )
        insight["web_identity_diagnostics"] = page_exploration.get(
            "web_identity_diagnostics", []
        )
    return insight


def _web_final_snapshot_insight(snapshot: dict[str, Any]) -> dict[str, Any]:
    """兼容既有 Web 冻结报告调用入口，并复用统一快照投影。"""

    return _bound_final_snapshot_insight(snapshot, target_platform="web")


def _snapshot_page_types(page_exploration: dict[str, Any]) -> dict[str, str]:
    result: dict[str, str] = {}
    for item in page_exploration.get("page_results") or []:
        if not isinstance(item, dict):
            continue
        capture_id = _optional_snapshot_text(item.get("page_capture_id"))
        page_type = _optional_snapshot_text(item.get("page_type"))
        if capture_id is not None and page_type is not None:
            result[capture_id] = page_type
    return result


def _snapshot_number(value: object, fallback: float) -> float:
    if isinstance(value, bool):
        return fallback
    if isinstance(value, int | float):
        return float(value)
    return fallback


def _optional_snapshot_text(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None


def _snapshot_text_list(value: object) -> list[str]:
    if not isinstance(value, list | tuple):
        return []
    return [text for item in value if (text := _optional_snapshot_text(item)) is not None]


def _is_web_coverage_gap_row(
    row: dict[str, Any],
    *,
    web_coverage: dict[str, Any] | None,
) -> bool:
    """校验无 Capture Gap 的快照、来源键和 Judge 输入三重身份。"""

    if not isinstance(web_coverage, dict):
        return False
    if row.get("execution_status") != "evidence_insufficient":
        return False
    if row.get("page_capture_id") is not None:
        return False
    snapshot_id = str(web_coverage.get("coverage_snapshot_id") or "")
    judge_input = row.get("judge_input")
    if not snapshot_id or not isinstance(judge_input, dict):
        return False
    missing_page_type = str(judge_input.get("missing_page_type") or "")
    rule_code = str(row.get("rule_code") or "")
    expected_source_key = (
        f"web-coverage-gap:{snapshot_id}:{missing_page_type}:{rule_code}"
    )
    return (
        bool(missing_page_type)
        and missing_page_type in (web_coverage.get("missing_page_types") or [])
        and row.get("source_idempotency_key") == expected_source_key
        and judge_input.get("schema_version") == "web-coverage-gap.v1"
        and judge_input.get("source") == "web_coverage_gap"
        and judge_input.get("coverage_snapshot_id") == snapshot_id
        and judge_input.get("reason") == "required_page_type_not_discovered"
    )


def _web_report_logical_pages(
    page_rows: list[dict[str, Any]],
    assessments: list[dict[str, Any]],
    *, trusted_identities=None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """报告按逻辑页选择主版本；生命周期状态不按 URL 与业务页合并。"""

    rows_by_capture_id = {
        str(row.get("page_capture_id")): row
        for row in page_rows
        if row.get("page_capture_id") and row.get("capture_role") != "supplemental"
    }
    assessment_by_capture_id = {
        str(item.get("page_capture_id")): item
        for item in assessments
        if item.get("page_capture_id")
    }
    lifecycle_capture_ids = {
        capture_id
        for capture_id, row in rows_by_capture_id.items()
        if (
            assessment_by_capture_id.get(capture_id, {}).get("coverage_group") == "lifecycle"
            or assessment_by_capture_id.get(capture_id, {}).get("final_page_type") == "age_gate"
            or row.get("page_type") == "age_gate"
        )
    }
    logical_pages = build_web_logical_pages(
        [
            row
            for capture_id, row in rows_by_capture_id.items()
            if capture_id not in lifecycle_capture_ids
        ],
        trusted_identities=trusted_identities,
    )
    primary_rows: list[dict[str, Any]] = []
    ordered_projections: list[tuple[int, dict[str, Any]]] = []

    def project(
        *,
        logical_page_id: str,
        canonical_url: str,
        menu_entry_ids: tuple[str, ...],
        capture_ids: tuple[str, ...],
        primary_capture_id: str,
        home_role_verified: bool,
        forced_page_role: str | None = None,
    ) -> None:
        primary = rows_by_capture_id.get(primary_capture_id)
        if primary is None:
            return
        assessment = assessment_by_capture_id.get(primary_capture_id, {})
        if not assessment:
            assessment = next(
                (
                    assessment_by_capture_id[capture_id]
                    for capture_id in reversed(capture_ids)
                    if capture_id in assessment_by_capture_id
                ),
                {},
            )
        final_page_type = (
            assessment.get("final_page_type")
            or primary.get("page_type")
        )
        proposed_page_type = (
            assessment.get("proposed_page_type")
            or assessment.get("page_type")
            or final_page_type
        )
        classification_status = (
            assessment.get("classification_status")
            or ("classified" if final_page_type else "needs_review")
        )
        primary_row = dict(primary)
        primary_row["page_type"] = final_page_type
        primary_row["url"] = web_logical_page_display_url(canonical_url)
        primary_rows.append(primary_row)
        gallery = []
        inspection_status = primary.get("inspection_status")
        for capture_id in capture_ids:
            version = rows_by_capture_id.get(capture_id)
            if version is None:
                continue
            if inspection_status is None and version.get("inspection_status") is not None:
                inspection_status = version.get("inspection_status")
            gallery.append(
                {
                    "capture_id": capture_id,
                    "capture_purpose": version.get("capture_purpose")
                    or version.get("capture_role")
                    or "web_page_ready_baseline",
                    "captured_at": version.get("captured_at"),
                    "screenshot_artifact_id": version.get("screenshot_artifact_id"),
                    "artifact_ids": list(version.get("artifact_ids") or []),
                    "is_primary": capture_id == primary_capture_id,
                }
            )
        supplemental = primary.get("supplemental_full_page")
        if not isinstance(supplemental, dict):
            supplemental = {
                "status": "not_attempted",
                "capture_id": None,
                "screenshot_artifact_id": None,
                "reason_code": None,
            }
        coverage_group = assessment.get("coverage_group")
        if coverage_group not in {"business", "lifecycle", "neither"}:
            coverage_group = (
                "lifecycle"
                if final_page_type == "age_gate"
                else "neither"
                if final_page_type == "unknown"
                else "business"
            )
        projection = {
            "logical_page_id": logical_page_id,
            "canonical_url": web_logical_page_display_url(canonical_url),
            "menu_entry_ids": list(menu_entry_ids),
            "capture_count": len(capture_ids),
            "primary_capture_id": primary_capture_id,
            "primary_screenshot_artifact_id": primary.get("screenshot_artifact_id"),
            "supplemental_full_page": dict(supplemental),
            "page_role": forced_page_role or (
                "home" if home_role_verified else resolve_web_logical_page_role(
                    canonical_url=canonical_url,
                    menu_entry_ids=menu_entry_ids,
                    asserted_roles=(
                        assessment.get("page_role"),
                        *(
                            rows_by_capture_id[capture_id].get("page_role")
                            for capture_id in capture_ids
                            if capture_id in rows_by_capture_id
                        ),
                    )
                )
            ),
            "page_type": final_page_type,
            "page_type_name": page_type_display_name(final_page_type),
            "coverage_group": coverage_group,
            "classification_status": classification_status,
            "raw_page_type": assessment.get("raw_page_type"),
            "proposed_page_type": proposed_page_type,
            "observed_page_type": assessment.get("observed_page_type"),
            "expected_page_type": assessment.get("expected_page_type"),
            "conflict_reason_code": assessment.get("conflict_reason_code"),
            "diagnostic_message": assessment.get("diagnostic_message"),
            "confidence": assessment.get("confidence"),
            "review_reason": (
                assessment.get("conflict_reason_code") or assessment.get("reason_code")
            ),
            "inspection_status": inspection_status,
            "evidence_gallery": gallery,
        }
        try:
            order = int(primary.get("page_seq") or 0)
        except (TypeError, ValueError):
            order = 0
        ordered_projections.append((order, projection))

    for logical_page in logical_pages:
        project(
            logical_page_id=logical_page.logical_page_id,
            canonical_url=logical_page.canonical_url,
            menu_entry_ids=logical_page.menu_entry_ids,
            capture_ids=logical_page.capture_ids,
            primary_capture_id=logical_page.primary_capture_id,
            home_role_verified=logical_page.home_role_verified,
        )
    for capture_id in sorted(lifecycle_capture_ids):
        row = rows_by_capture_id[capture_id]
        identity = (
            trusted_identities.get(capture_id, {})
            if trusted_identities is not None
            else {}
        )
        if (
            trusted_identities is not None
            and identity.get("logical_page_identity_status") != "verified"
        ):
            continue
        raw_url = str(
            identity.get("canonical_url")
            or row.get("url")
            or row.get("page_url")
            or ""
        )
        if not raw_url:
            continue
        project(
            logical_page_id=f"web-lifecycle:{capture_id}",
            canonical_url=raw_url,
            menu_entry_ids=(),
            capture_ids=(capture_id,),
            primary_capture_id=capture_id,
            home_role_verified=False,
            forced_page_role="lifecycle",
        )
    ordered_projections.sort(key=lambda item: item[0])
    ordered_ids = {
        item[1]["primary_capture_id"]: index
        for index, item in enumerate(ordered_projections)
    }
    primary_rows.sort(key=lambda row: ordered_ids.get(str(row.get("page_capture_id")), 0))
    return primary_rows, [item[1] for item in ordered_projections]


def _judge_input_source(value: Any) -> str | None:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return None
    source = value.get("source") if isinstance(value, dict) else None
    return source if isinstance(source, str) else None


def _scope_kwargs(access_scope: AccessScope | None) -> dict[str, Any]:
    """只有 API 明确传入主体时才向仓储下沉 scope。

    Worker/历史单测仍可走无主体的内部读路径；所有用户报告路由均由
    ``require_access_scope`` 注入非空 scope，并因此强制使用对象级 SQL 过滤。
    """

    return {"access_scope": access_scope} if access_scope is not None else {}


def _parse_popup_wall(run_error: Any) -> dict[str, Any] | None:
    """把弹窗终止串解析为报告可展示的结构化发现（不是平台故障，是产品体验事实）。"""
    match = _POPUP_WALL_ERROR.search(str(run_error or ""))
    if match is None:
        return None
    kind = match.group(1)
    closed_count = int(match.group(2))
    limit = int(match.group(3))
    if kind == "popup_dismissal_stalled":
        note = (
            f"同一个弹窗连续 {closed_count} 次尝试关闭未见效（上限 {limit} 次），"
            "该页探索被迫中止。该弹窗可能是关闭入口异常的广告，或身份凭证卡等业务弹窗，"
            "建议结合截图人工复核。"
        )
    else:
        note = (
            f"同一页面连续处理 {closed_count} 层广告弹窗后仍在弹出（上限 {limit} 层），"
            "该页探索被迫中止。广告弹窗密度过高本身属于广告体验问题，建议结合截图人工复核。"
        )
    return {
        "kind": kind,
        "closed_count": closed_count,
        "limit": limit,
        "note": note,
    }


def display_status_group(display_status: str) -> str:
    """公开入口：把细粒度展示态归并为报告六类展示态，供任务详情读侧复用。"""

    return _display_group(display_status)


def display_status_group_labels() -> dict[str, str]:
    """公开入口：六类展示态的中文名。"""

    return dict(_DISTRIBUTION_LABELS)


def _display_group(display_status: str) -> str:
    """把机器细粒度展示态归并为报告业务要求的六类展示态。

    2026-09-01 口径（在 08-31 会议四态基础上按展示需要细分）：
    通过 / 未通过 / 本产品不适用 / 计划外未覆盖 / 检测异常 / 其他。
    入参是叠加人工终审后的细粒度展示态（人工终审优先于机器结论）；
    未触发与确认不适用归「本产品不适用」，运行未覆盖归「计划外未覆盖」，
    执行故障（代码问题）归「检测异常」，证据不足 / 转人工 / 未执行等
    没有确定结论的一律归「其他」。质检成功率只统计前三类（AI 已出
    确定结果），后三类整体对应会议口径里的「识别异常」红段。
    """
    return _DISPLAY_STATUS_GROUPS.get(display_status, "other")
