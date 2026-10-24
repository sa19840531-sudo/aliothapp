"""工作台报告页面用例。"""

from datetime import datetime
from typing import Protocol

from services.api.app.application.report_generation import (
    ReportGenerationService,
    ReportGenerationTargetNotFoundError,
)
from services.api.app.application.report_insight import (
    ReportInsightNotFoundError,
    _is_h5_v2_final_snapshot,
)
from services.api.app.application.workstation_tasks import (
    WorkstationTaskListFilter,
    WorkstationTaskService,
)
from services.api.app.domain.iam.access_scope import AccessScope

# 报告列表只列有报告可看的任务：成功 / 生成中 / 生成失败（2.1 原型报告状态三态）；
# 排队未开始、已取消、仅探索、没有规则的任务没有报告，不进报告列表
REPORT_LIST_STATUSES = "ready,generating,failed"


class FinalReportSnapshotRepository(Protocol):
    """Worker 终态报告快照查询契约。"""

    async def get_final_report(
        self,
        task_id: str,
        *,
        agent_run_id: str | None = None,
        access_scope: AccessScope | None = None,
    ) -> dict | None:
        """读取任务指定 Run 或最近一次 Run 的终态报告。"""

    async def agent_run_exists(
        self,
        *,
        task_id: str,
        agent_run_id: str,
        access_scope: AccessScope | None = None,
    ) -> bool:
        """校验 Agent Run 是否属于指定任务。"""


class PageExplorationReportReader(Protocol):
    """报告动态生成时读取页面探索持久化摘要。"""

    async def page_timeline(
        self,
        task_id: str,
        *,
        access_scope: AccessScope | None = None,
    ) -> dict:
        """读取任务页面时间线。"""


class ReportInsightReader(Protocol):
    """未封存报告复用报告中心的业务结果与诊断事实。"""

    async def report_insight(
        self,
        task_id: str,
        *,
        access_scope: AccessScope | None = None,
    ) -> dict:
        """读取任务报告中心投影，不修改已封存正文。"""


class WorkstationReportService:
    """工作台报告页面服务。"""

    def __init__(
        self,
        *,
        task_service: WorkstationTaskService,
        report_generation_service: ReportGenerationService,
        final_report_snapshot_repository: FinalReportSnapshotRepository,
        page_exploration_reader: PageExplorationReportReader | None = None,
        report_insight_reader: ReportInsightReader | None = None,
    ) -> None:
        self._task_service = task_service
        self._report_generation_service = report_generation_service
        self._final_report_snapshot_repository = final_report_snapshot_repository
        self._page_exploration_reader = page_exploration_reader
        self._report_insight_reader = report_insight_reader

    async def list_reports(
        self,
        *,
        page: int,
        page_size: int,
        access_scope: AccessScope | None = None,
        task_name: str | None = None,
        product_name: str | None = None,
        department_name: str | None = None,
        target_platform: str | None = None,
        created_from: datetime | None = None,
        created_to: datetime | None = None,
        report_status: str = REPORT_LIST_STATUSES,
    ) -> dict:
        task_result = await self._task_service.list_tasks(
            WorkstationTaskListFilter(
                task_name=task_name,
                product_name=product_name,
                department_name=department_name,
                target_platform=target_platform,
                report_status=report_status,
                created_from=created_from,
                created_to=created_to,
                page=page,
                page_size=page_size,
            ),
            access_scope=access_scope,
        )
        return {
            "items": [
                {
                    "task_id": item.task_id,
                    "task_name": item.task_name,
                    "report_status": item.report_status,
                    "report_availability": item.report_availability,
                    "report_unavailable_reason": (
                        "WEB_REPORT_NO_PERSISTED_FACTS"
                        if item.report_availability == "none"
                        and _enum_text(item.target_platform) == "web"
                        and _enum_text(item.execution_mode) == "full_inspection"
                        else None
                    ),
                    "overall_status": _overall_status_from_report_status(item.report_status),
                    "attention_required": None,
                    "generated_at": item.created_at.isoformat(),
                    "department_name": item.product.department_name,
                    "product_code": item.product.product_code,
                    "product_name": item.product.product_name,
                    "target_platform": getattr(item.target_platform, "value", item.target_platform),
                }
                for item in task_result.items
            ],
            "total": task_result.total,
            "page": task_result.page,
            "page_size": task_result.page_size,
        }

    async def get_report(
        self,
        task_id: str,
        *,
        agent_run_id: str | None = None,
        access_scope: AccessScope | None = None,
    ) -> dict:
        snapshot = await self._final_report_snapshot_repository.get_final_report(
            task_id,
            agent_run_id=agent_run_id,
            access_scope=access_scope,
        )
        if snapshot is not None:
            if _is_unbound_h5_v2_preview(snapshot, task_id=task_id):
                return await self._with_h5_report_facts(
                    task_id, snapshot, access_scope=access_scope,
                )
            return snapshot
        if agent_run_id is not None and not await (
            self._final_report_snapshot_repository.agent_run_exists(
                task_id=task_id,
                agent_run_id=agent_run_id,
                access_scope=access_scope,
            )
        ):
            raise ReportGenerationTargetNotFoundError(
                f"任务 {task_id} 不存在 Agent Run：{agent_run_id}"
            )
        page_exploration = await self._load_page_exploration(
            task_id,
            access_scope=access_scope,
        )
        task_detail = await self._task_service.get_task_detail(
            task_id,
            access_scope=access_scope,
        )
        if _is_web_v2_full_inspection(task_detail) and (
            str(getattr(task_detail, "report_availability", "none")) == "none"
            and page_exploration is None
        ):
            raise ReportGenerationTargetNotFoundError(
                f"Web v2 任务尚无可预览的持久化报告事实：{task_id}"
            )
        try:
            report = await self._report_generation_service.generate_task_report(
                task_id,
                agent_run_id=agent_run_id,
                page_exploration=page_exploration,
                access_scope=access_scope,
            )
            if _is_web_v2_full_inspection(task_detail):
                return _with_report_availability(report, availability="preview")
            if _is_h5_v2_full_inspection(task_detail):
                if _is_h5_review_report_state(task_detail):
                    report = _with_h5_review_availability(report)
                return await self._with_h5_report_facts(
                    task_id, report, access_scope=access_scope,
                )
            return report
        except ReportGenerationTargetNotFoundError:
            raise

    async def _with_h5_report_facts(
        self,
        task_id: str,
        report: dict,
        *,
        access_scope: AccessScope | None,
    ) -> dict:
        """旧 H5 Run 正文只作预览，业务摘要统一采用报告中心的持久化事实。"""

        if self._report_insight_reader is None:
            return report
        try:
            insight = await self._report_insight_reader.report_insight(
                task_id, access_scope=access_scope,
            )
        except ReportInsightNotFoundError as exc:
            raise ReportGenerationTargetNotFoundError(str(exc)) from exc
        source_summary = insight.get("summary") or {}
        report_run_id = _optional_text(report.get("agent_run_id"))
        insight_run_id = _optional_text(source_summary.get("run_id"))
        # 显式查询历史 Run 时，不得把当前 Run 的页面和收口事实混入旧正文。
        if report_run_id and insight_run_id and report_run_id != insight_run_id:
            return report
        if insight.get("report_availability") == "final":
            # 两次读取之间可能刚完成封存；只能返回 Gate 正文，不能给旧预览贴终版标记。
            frozen = await self._final_report_snapshot_repository.get_final_report(
                task_id, agent_run_id=report_run_id, access_scope=access_scope,
            )
            if isinstance(frozen, dict) and _is_h5_v2_final_snapshot(frozen, task_id=task_id):
                return frozen
            insight = {
                **insight,
                "summary": {**source_summary, "report_stage": "final_review"},
                "report_availability": "preview", "is_final": False,
                "export_ready": False, "export_allowed": False,
                "incomplete_reasons": ["H5_REPORT_NOT_FINALIZED"],
            }
        return _with_h5_insight_projection(report, insight)

    async def _with_web_stage_durations(
        self,
        task_id: str,
        report: dict,
        *,
        agent_run_id: str | None,
        access_scope: AccessScope | None,
    ) -> dict:
        task_detail = await self._task_service.get_task_detail(
            task_id,
            access_scope=access_scope,
        )
        if not _is_web_v2_full_inspection(task_detail):
            return report
        durations = getattr(task_detail, "stage_durations", None)
        detail_run_id = _optional_text(getattr(task_detail, "run_id", None))
        if durations is None or (
            agent_run_id is not None and detail_run_id != agent_run_id
        ):
            reader = getattr(self._task_service, "get_web_stage_durations", None)
            if not callable(reader):
                return report
            durations = await reader(
                task_id,
                agent_run_id=agent_run_id,
                access_scope=access_scope,
            )
        if durations is None:
            return report
        payload = durations.to_dict() if hasattr(durations, "to_dict") else dict(durations)
        enriched = dict(report)
        enriched["stage_durations"] = payload
        return enriched

    async def _with_page_exploration(
        self,
        task_id: str,
        report: dict,
        *,
        access_scope: AccessScope | None,
    ) -> dict:
        if isinstance(report.get("page_exploration"), dict):
            return report
        page_exploration = await self._load_page_exploration(
            task_id,
            access_scope=access_scope,
        )
        if page_exploration is None:
            return report
        enriched = dict(report)
        enriched["page_exploration"] = page_exploration
        return enriched

    async def _load_page_exploration(
        self,
        task_id: str,
        *,
        access_scope: AccessScope | None,
    ) -> dict | None:
        if self._page_exploration_reader is None:
            return None
        timeline = await self._page_exploration_reader.page_timeline(
            task_id,
            access_scope=access_scope,
        )
        summary = timeline.get("exploration_summary")
        return dict(summary) if isinstance(summary, dict) else None


def _overall_status_from_report_status(report_status: str) -> str:
    if report_status == "ready":
        return "generated"
    if report_status == "failed":
        return "error"
    if report_status == "generating":
        return "generating"
    return "not_started"


def _enum_text(value: object) -> str:
    return str(getattr(value, "value", value) or "").casefold()


def _optional_text(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None


def _is_web_v2_full_inspection(task_detail: object | None) -> bool:
    return bool(
        task_detail is not None
        and _enum_text(getattr(task_detail, "target_platform", None)) == "web"
        and str(getattr(task_detail, "workflow_version", "")).casefold() == "v2"
        and _enum_text(getattr(task_detail, "execution_mode", None))
        == "full_inspection"
    )


def _is_h5_v2_full_inspection(task_detail: object | None) -> bool:
    return bool(
        task_detail is not None
        and _enum_text(getattr(task_detail, "target_platform", None)) == "h5"
        and str(getattr(task_detail, "workflow_version", "")).casefold() == "v2"
        and _enum_text(getattr(task_detail, "execution_mode", None))
        == "full_inspection"
    )


def _is_h5_review_report_state(task_detail: object | None) -> bool:
    return _enum_text(getattr(task_detail, "status", None)) in {
        "done",
        "failed",
        "canceled",
        "review",
    }


def _is_unbound_h5_v2_preview(report: dict, *, task_id: str) -> bool:
    """只有通过既有 Gate 绑定契约的 H5 正文才能绕过实时事实投影。"""

    task = report.get("task") or {}
    task_config = task.get("task_config") or {}
    return bool(
        _enum_text(task.get("target_platform")) == "h5"
        and _enum_text(task_config.get("workflow_version")) == "v2"
        and _enum_text(task_config.get("execution_mode")) == "full_inspection"
        and not _is_h5_v2_final_snapshot(report, task_id=task_id)
    )


def _with_h5_insight_projection(report: dict, insight: dict) -> dict:
    """适配现有报告详情字段，不复制业务结果资格判定或改写原始证据。"""

    enriched = dict(report)
    summary = dict(report.get("summary") or {})
    source_summary = insight.get("summary") or {}
    for key in (
        "total_rules", "evaluated_rules", "completed_candidate_rules",
        "candidate_rule_count", "confirmed_not_applicable_rules", "coverage_gap_rules",
        "full_rule_pass_rate", "qc_success_rate", "pass_rate", "candidate_execution_coverage",
    ):
        if key in source_summary:
            summary[key] = source_summary[key]
    counts = {item["status"]: item["count"] for item in insight.get("distribution") or []}
    items = insight.get("rule_items") or []
    progress = insight.get("confirm_progress") or {}
    summary.update(
        **{
            "pass": counts.get("passed", 0),
            "fail": counts.get("failed", 0),
            "warning": 0,
            "manual_review": progress.get("pending", 0),
            "evidence_insufficient": sum(
                item.get("execution_status") == "evidence_insufficient" for item in items
            ),
            "skipped": counts.get("not_applicable", 0) + counts.get("coverage_gap", 0),
            "error": counts.get("detect_error", 0),
        },
        not_applicable_rules=source_summary.get("confirmed_not_applicable_rules", 0),
        non_candidate_rules=source_summary.get("coverage_gap_rules", 0),
        unresolved_candidate_rules=max(
            source_summary.get("candidate_rule_count", 0)
            - source_summary.get("completed_candidate_rules", 0), 0,
        ),
    )
    availability = insight.get("report_availability", "none")
    page_exploration = insight.get("page_exploration") or {}
    if availability == "none" or page_exploration.get("complete") is False:
        summary.update(
            technical_execution_complete=False,
            technical_completion_status="incomplete",
            candidate_execution_complete=False,
            task_goal_completed=False,
            task_goal_completion_status="incomplete",
            business_completion_status="incomplete",
            overall_status="incomplete",
        )
    if availability == "none":
        summary.update(automatic_decision_rules=0, automatic_decision_rate=0)
    summary["platform_capability_status"] = (
        "degraded" if page_exploration.get("quality_status") == "technical_failed"
        else "unassessed"
    )
    reasons = list(insight.get("incomplete_reasons") or [])
    terminal_reason = _optional_text(page_exploration.get("terminal_reason"))
    if page_exploration.get("quality_status") == "technical_failed" and terminal_reason:
        if terminal_reason not in reasons:
            reasons.append(terminal_reason)
        enriched["termination_reason"] = terminal_reason
    enriched.update(
        summary=summary,
        report_stage=source_summary.get("report_stage", "final_review"),
        report_availability=availability,
        report_unavailable_reason=insight.get("report_unavailable_reason"),
        is_final=insight.get("is_final") is True,
        export_ready=insight.get("export_ready") is True,
        export_allowed=insight.get("export_allowed") is True,
        incomplete_reasons=reasons,
        page_exploration=dict(page_exploration),
    )
    return enriched


def _with_h5_review_availability(report: dict) -> dict:
    """H5 终态复核报告可读，但不得冒充 Gate 已通过的正式报告。"""

    enriched = dict(report)
    enriched["report_stage"] = "final_review"
    enriched["report_availability"] = "preview"
    enriched["is_final"] = False
    enriched["export_allowed"] = False
    enriched["export_ready"] = False
    enriched["incomplete_reasons"] = ["H5_REPORT_NOT_FINALIZED"]
    return enriched


def _with_report_availability(report: dict, *, availability: str) -> dict:
    """统一报告三态字段；复制 payload，避免修改不可变终版快照。"""

    enriched = dict(report)
    is_final = availability == "final"
    enriched["report_availability"] = availability
    enriched["is_final"] = is_final
    enriched["export_allowed"] = is_final
    enriched["incomplete_reasons"] = [] if is_final else ["WEB_REPORT_NOT_FINALIZED"]
    return enriched
