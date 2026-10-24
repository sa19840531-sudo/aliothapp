"""任务详情过程可视化聚合服务：把落库的过程态拼装成新版任务详情页所需读模型。

数据来源（全部已由写侧产生，本服务只读拼装）：

- ``qc_task_page_node`` + ``qc_page_capture``：页面识别时间线（含识别指标/截图）；
- ``qc_rule_execution`` + ``qc_rule``：规则执行结果（执行态→展示态映射见
  ``task_progress.py``，候选未执行补"待执行/执行中"行）；
- ``qc_task_log``：执行日志与导出；
- ``qc_evidence_artifact``：截图证据分组（checksum 去重 + 业务页／遮挡代表图 + 关联规则）与打包下载。

没有页面节点行的旧任务/排队任务，时间线按 ``task_config.journey_steps`` 现场推导，
读侧永远可用。
"""

import base64
import hashlib
import io
import json
import zipfile
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any

from services.api.app.application.h5_evidence_inventory import (
    project_h5_evidence_preflight,
)
from services.api.app.application.h5_terminal_projection import (
    h5_target_coverage_ready as _h5_target_coverage_ready,
)
from services.api.app.application.h5_terminal_projection import (
    latest_h5_frontier as _latest_h5_frontier,
)
from services.api.app.application.h5_terminal_projection import (
    project_h5_terminal as _h5_terminal_projection,
)
from services.api.app.application.page_exploration_summary import (
    build_page_exploration_summary,
    count_pending_frontier_actions,
    extract_page_type_assessment_context,
    extract_page_type_assessment_scope_page_types,
    project_h5_page_type_assessments,
    project_page_evidence_results,
)
from services.api.app.application.page_graph import PageGraphService
from services.api.app.application.page_type_judgements import (
    group_judgements,
    judgement_from_mapping,
)
from services.api.app.application.platform_progress_export import (
    build_platform_progress_snapshot,
    build_platform_progress_workbook,
)
from services.api.app.application.report_insight import (
    display_status_group,
    display_status_group_labels,
)
from services.api.app.application.report_rule_inventory import project_task_rule_inventory
from services.api.app.application.report_text_policy import maybe_scrub_app_report_payload
from services.api.app.application.task_progress import (
    PAGE_NODE_STATUS_LABELS,
    RULE_DISPLAY_COVERAGE_GAP,
    RULE_DISPLAY_LABELS,
    RULE_DISPLAY_PENDING,
    RULE_DISPLAY_RUNNING,
    effective_rule_execution_reason,
    human_verdict_label,
    humanize_qc_text,
    normalize_journey_steps,
    page_type_display_name,
    rollup_display_status,
    rule_display_label,
    rule_display_status,
    rule_execution_counts_as_completed,
    rule_execution_counts_as_used,
    rule_final_display_status,
    rule_package_fallback,
    rule_requires_human_confirmation,
    severity_display,
    trigger_display_fallback,
    whole_task_progress_percent,
)
from services.api.app.application.task_rule_inventory import requires_task_rule_inventory
from services.api.app.application.web_evidence_bundle import build_web_evidence_bundle
from services.api.app.application.web_logical_page import (
    build_web_logical_pages,
    resolve_web_logical_page_role,
    web_logical_page_display_url,
)
from services.api.app.domain.iam.access_scope import AccessScope
from services.api.app.domain.inspection.rule_reuse import (
    invalidation_conditions,
    invalidation_label,
    reuse_scope_label,
)
from services.api.app.domain.page.registry import get_default_page_type_registry
from services.api.app.domain.protocol import stable_protocol_hash
from services.api.app.domain.task import TaskExecutionMode, task_execution_mode_from_config
from services.api.app.domain.task.closure_outcome import (
    CLOSURE_OUTCOME_PROTOCOL_VERSION,
    completion_gate_dimension,
    evidence_dimension,
    exploration_dimension,
    overall_summary,
    rule_dimension,
)
from services.api.app.domain.task.inspection_scope import (
    inspection_scope_page_types,
    page_type_in_inspection_scope,
)

_TERMINAL_TASK_STATUSES = {"done", "failed", "canceled", "review"}
_UNVISITED_PENDING_SUMMARY = "页面未访问，规则未执行"
_TIMELINE_ATTENTION_STATUSES = {
    "failed",
    "detect_error",
    "evidence_insufficient",
    "manual_review",
    "not_executed",
}
_PAGE_INSPECTION_STATUS_LABELS = {
    "captured": "已采集",
    "detecting": "识别中",
    "matching": "规则匹配中",
    "evaluating": "规则执行中",
    "completed": "页面检查完成",
    "degraded": "页面检查完成 · 需复核",
    "failed": "页面检查失败",
}


def _decode_screenshot_frames(
    content: bytes,
    *,
    content_type: str | None,
) -> list[bytes]:
    """把普通 PNG 或截图序列证据统一解码为可展示帧。"""
    media_type = str(content_type or "").split(";", 1)[0].strip().casefold()
    if media_type != "application/json":
        return [content]
    try:
        payload = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("截图时序包不是有效 JSON") from exc
    frames = payload.get("frames") if isinstance(payload, dict) else None
    if not isinstance(frames, list) or not frames:
        raise ValueError("截图时序包缺少 frames")
    decoded: list[bytes] = []
    for frame in frames:
        if not isinstance(frame, dict) or frame.get("encoding") != "base64":
            raise ValueError("截图时序帧编码无效")
        data = frame.get("data")
        if not isinstance(data, str):
            raise ValueError("截图时序帧缺少数据")
        try:
            decoded.append(base64.b64decode(data, validate=True))
        except ValueError as exc:
            raise ValueError("截图时序帧 Base64 无效") from exc
    return decoded


class TaskInsightNotFoundError(LookupError):
    """任务不存在。"""


class TaskInsightService:
    """新版任务详情页聚合读取服务。"""

    def __init__(
        self,
        *,
        repository: Any,
        evidence_store_factory: Callable[[], Any] | None = None,
        page_graph_service: PageGraphService | None = None,
        web_evidence_repository: Any | None = None,
        web_policy_repository: Any | None = None,
        rule_inventory_repository: Any | None = None,
    ) -> None:
        self._repository = repository
        self._evidence_store_factory = evidence_store_factory
        self._evidence_store: Any | None = None
        self._page_graph_service = page_graph_service or PageGraphService(
            repository=(
                getattr(repository, "page_graph_repository", None)
                or getattr(repository, "_page_graph_repository", None)
            )
        )
        self._web_evidence_repository = web_evidence_repository
        self._web_policy_repository = web_policy_repository
        self._rule_inventory_repository = rule_inventory_repository

    async def page_graph(
        self,
        task_id: str,
        *,
        access_scope: AccessScope | None = None,
    ) -> dict[str, Any]:
        """返回当前权威 Run/Epoch 的页面状态图。"""

        context = await self._require_context(task_id, access_scope=access_scope)
        return await self._page_graph_service.get_graph(
            task_id=task_id,
            agent_run_id=context.get("run_id"),
            finalization_epoch=_optional_nonnegative_int(
                context.get("finalization_epoch")
            ),
        )

    async def page_recovery_route(
        self,
        task_id: str,
        *,
        source_state_id: str | None = None,
        target_identity: str | None = None,
        limit: int = 20,
        access_scope: AccessScope | None = None,
    ) -> dict[str, Any]:
        """返回只读恢复候选；调用方仍必须逐步重新验证。"""

        context = await self._require_context(task_id, access_scope=access_scope)
        return await self._page_graph_service.get_recovery_route(
            task_id=task_id,
            agent_run_id=context.get("run_id"),
            finalization_epoch=_optional_nonnegative_int(
                context.get("finalization_epoch")
            ),
            source_state_id=source_state_id,
            target_identity=target_identity,
            limit=limit,
        )

    # ---------- 页面识别时间线 ----------

    async def page_timeline(
        self,
        task_id: str,
        *,
        access_scope: AccessScope | None = None,
    ) -> dict[str, Any]:
        context = await self._require_context(task_id, access_scope=access_scope)
        run_id = context.get("run_id")
        task_running = context["status"] == "running"
        is_h5_v2 = _is_h5_platform_context(context)
        is_web_v2 = _is_web_v2_platform_context(context)
        target_platform = _optional_text(context.get("target_platform"))
        is_app = (
            target_platform.casefold() == "app"
            if target_platform is not None
            else _legacy_app_platform_fallback(context)
        )
        execution_mode = task_execution_mode_from_config(context.get("task_config"))
        h5_work_items: list[dict[str, Any]] = []
        h5_assessments: list[dict[str, Any]] = []
        h5_inventory: list[dict[str, Any]] = []
        h5_full_closure: dict[str, Any] = {}
        h5_coverage: dict[str, Any] | None = None
        h5_coverage_reader = getattr(self._repository, "fetch_h5_coverage_snapshot", None)
        required_page_types = extract_page_type_assessment_scope_page_types(
            context.get("working_memory")
        )
        h5_visual_navigation_usage: dict[str, Any] = {}
        surface_session_summary: dict[str, Any] = {}
        frontier: dict[str, Any] | None = None
        if is_h5_v2 or is_web_v2:
            if run_id is not None:
                session_reader = getattr(
                    self._repository,
                    "fetch_surface_session_summary",
                    None,
                )
                if callable(session_reader):
                    session_summary = await session_reader(
                        task_id=task_id,
                        agent_run_id=run_id,
                    )
                    if isinstance(session_summary, dict):
                        surface_session_summary = session_summary
                if is_h5_v2:
                    h5_work_items = await self._repository.list_h5_v2_work_items(
                        task_id=task_id, agent_run_id=run_id
                    )
                exploration_actions = await self._repository.list_h5_action_executions(
                    task_id=task_id, agent_run_id=run_id
                )
                page_rows = await self._repository.list_h5_page_captures(
                    task_id=task_id, agent_run_id=run_id
                )
                h5_assessments = await self._repository.list_h5_page_type_assessments(
                    task_id=task_id, agent_run_id=run_id
                )
                if is_h5_v2:
                    h5_inventory = await self._repository.list_h5_evidence_inventory(
                        task_id=task_id,
                        agent_run_id=run_id,
                    )
                    visual_usage_reader = getattr(
                        self._repository,
                        "fetch_h5_visual_navigation_usage",
                        None,
                    )
                    if callable(visual_usage_reader):
                        usage = await visual_usage_reader(
                            task_id=task_id,
                            agent_run_id=run_id,
                        )
                        if isinstance(usage, dict):
                            h5_visual_navigation_usage = dict(usage)
                    if callable(h5_coverage_reader):
                        h5_coverage = await h5_coverage_reader(
                            task_id=task_id, agent_run_id=run_id
                        )
            else:
                exploration_actions = []
                page_rows = []
            nodes = [_h5_capture_node(row) for row in page_rows]
            assessments = project_h5_page_type_assessments(h5_assessments)
            frontier = _latest_h5_frontier(h5_work_items) if is_h5_v2 else None
            if is_h5_v2 and h5_coverage is None:
                if callable(h5_coverage_reader):
                    # 权威读取没有本轮快照时不得用历史 Frontier/Assessment 重建完成态。
                    h5_coverage = {
                        "authority_source": "unavailable",
                        "reason_code": "H5_CURRENT_COVERAGE_SNAPSHOT_UNAVAILABLE",
                        "required_page_types": list(required_page_types),
                        "covered_page_types": [],
                        "missing_page_types": list(required_page_types),
                        "scopes": [],
                        "assessment_scope_ready": False,
                        "frontier_terminal": False,
                        "completion_ready": False,
                    }
                else:
                    h5_coverage = _legacy_h5_coverage_projection(
                        frontier=frontier,
                        assessments=assessments,
                    )
        else:
            nodes = (
                await self._repository.list_page_nodes(agent_run_id=run_id)
                if run_id is not None
                else []
            )
            page_rows = await self._repository.list_page_capture_inspection_rows(
                task_id=task_id, agent_run_id=run_id
            )
            exploration_actions = await self._repository.list_exploration_actions(
                agent_run_id=run_id
            )
            assessments, assessment_scope_complete = extract_page_type_assessment_context(
                context.get("working_memory")
            )
        nodes = [node for node in nodes if not _is_nested_detail_return_revisit(node)]
        web_identity_error = None
        if is_web_v2:
            from services.api.app.application.web_insight_identity import (
                load_web_insight_identities,
            )
            identities, web_identity_error = await load_web_insight_identities(
                self._repository, task_id=task_id, agent_run_id=run_id,
            )
            nodes = _web_logical_page_nodes(
                nodes, assessments=assessments, trusted_identities=identities,
            )
        if not nodes and not is_h5_v2 and not is_web_v2:
            nodes = self._derive_nodes_from_journey(context)
        if is_h5_v2 or is_web_v2:
            assessment_scope_complete = bool(h5_assessments)
        # 经过的页面只留在上面的路线节点里，不进页面汇总和完成结论
        inspection_scope = inspection_scope_page_types(context.get("task_config"))
        scoped_assessments = [
            item
            for item in assessments
            if page_type_in_inspection_scope(inspection_scope, item.get("page_type"))
        ]
        summary_page_rows = (
            [row for row in page_rows if row.get("capture_role") != "before_action"]
            if is_h5_v2 or is_web_v2
            else page_rows
        )
        if is_h5_v2:
            summary_page_rows = _h5_page_rows_with_assessment_diagnostics(
                summary_page_rows,
                assessments,
            )
        exploration_summary = build_page_exploration_summary(
            pages=summary_page_rows,
            actions=exploration_actions,
            page_type_assessments=assessments,
            assessment_scope_complete=assessment_scope_complete,
            visual_navigation_mode=(
                _h5_visual_navigation_mode(h5_work_items) if is_h5_v2 else None
            ),
            visual_model_usage=(
                h5_visual_navigation_usage if is_h5_v2 else None
            ),
            required_page_types=(
                h5_coverage.get("required_page_types")
                if is_h5_v2 and h5_coverage is not None
                else required_page_types
            ),
            inspection_scope=inspection_scope,
        )
        if is_h5_v2:
            before_action_rows = [
                row
                for row in page_rows
                if row.get("capture_role") == "before_action"
                and page_type_in_inspection_scope(inspection_scope, row.get("page_type"))
            ]
            if before_action_rows:
                exploration_summary["evidence_page_results"] = (
                    project_page_evidence_results(before_action_rows)
                )
        if is_h5_v2 and execution_mode is TaskExecutionMode.FULL_INSPECTION:
            h5_full_closure = (
                await self._repository.fetch_h5_full_inspection_closure(
                    task_id=task_id, agent_run_id=run_id
                )
                if run_id is not None
                else {}
            )
        if is_h5_v2:
            terminal = _h5_terminal_projection(
                work_items=h5_work_items,
                frontier=frontier,
                assessments=scoped_assessments,
                inventory=h5_inventory,
                full_inspection_closure=h5_full_closure,
                action_count=len(exploration_actions),
                action_ids={
                    action_id
                    for item in exploration_actions
                    if (action_id := _optional_text(item.get("action_execution_id")))
                },
                assessment_scope_ready=h5_coverage.get("assessment_scope_ready") is True,
                coverage_ready=_h5_target_coverage_ready(h5_coverage),
            )
            exploration_summary.update(terminal)
        if execution_mode is TaskExecutionMode.FULL_INSPECTION:
            # APP 缺少当时的候选事实时保留未知，不能用现行规则补写历史口径。
            expected_counts = (
                {} if is_app else await self._repository.count_rules_by_page_type(task_id=task_id)
            )
            # APP 结论可能由独立 Judge Run 写入，再按当前页面 Capture 集合关联。
            execution_rows = await self._repository.list_rule_execution_rows(
                task_id=task_id, agent_run_id=None if is_app else run_id
            )
        else:
            expected_counts = {}
            execution_rows = []
        executions_by_capture: dict[str, list[dict[str, Any]]] = {}
        for row in execution_rows:
            capture_id = row.get("page_capture_id")
            if capture_id:
                executions_by_capture.setdefault(capture_id, []).append(row)

        current_seq = self._resolve_current_node_seq(nodes, task_running)
        items = []
        for node in nodes:
            capture_id = node.get("page_capture_id")
            capture_ids = node.get("capture_ids")
            if not isinstance(capture_ids, list | tuple):
                capture_ids = (capture_id,) if capture_id else ()
            node_executions = [
                execution
                for version_capture_id in capture_ids
                for execution in executions_by_capture.get(version_capture_id, [])
            ]
            if _is_internal_transition_node(node, node_executions):
                continue
            display_statuses = [
                rule_final_display_status(
                    row["execution_status"],
                    row.get("execution_reason"),
                    human_verdict=row.get("human_verdict"),
                    is_bootstrap=bool(row.get("is_bootstrap")),
                )
                for row in node_executions
            ]
            attention_items = [
                _timeline_attention_item(row)
                for row in node_executions
                if rule_final_display_status(
                    row["execution_status"],
                    row.get("execution_reason"),
                    human_verdict=row.get("human_verdict"),
                    is_bootstrap=bool(row.get("is_bootstrap")),
                )
                in _TIMELINE_ATTENTION_STATUSES
            ]
            rollup = rollup_display_status(display_statuses)
            candidate_total = node.get("candidate_rule_total")
            scope_excluded = bool(
                is_app and node.get("page_type") in get_default_page_type_registry().all_codes
                and not page_type_in_inspection_scope(inspection_scope, node["page_type"])
            )
            if _is_inactive_capture_node(node) or scope_excluded:
                candidate_total = len(node_executions)
            elif candidate_total is None and node.get("page_type"):
                candidate_total = expected_counts.get(node["page_type"])
            inspection = {
                "lifecycle_status": node.get("inspection_lifecycle_status"),
                "status": node.get("inspection_status"),
                "status_label": _page_inspection_status_label(
                    status=node.get("inspection_status"),
                    failure_code=node.get("inspection_failure_code"),
                ),
                "failure_stage": node.get("inspection_failure_stage"),
                "failure_code": node.get("inspection_failure_code"),
                "failure_message": node.get("inspection_failure_message"),
                "failure_retryable": node.get("inspection_failure_retryable"),
                "abandon_reason": node.get("inspection_abandon_reason"),
            }
            if (
                scope_excluded and not node_executions
                and not inspection["failure_code"] and not inspection["failure_message"]
                and inspection["status"] not in {"failed", "degraded"}
                and not inspection["failure_stage"] and not inspection["abandon_reason"]
            ):
                # 范围排除是提交配置事实；不改历史记录，也不遮蔽已存检查故障。
                inspection.update(
                    status="degraded", status_label="本次质检范围外",
                    failure_stage="scope_matching",
                    failure_code="PAGE_TYPE_OUTSIDE_INSPECTION_SCOPE",
                    failure_message="该页型未包含在本任务选择的质检范围内，仅保留页面证据。",
                    failure_retryable=False,
                )
            is_current = task_running and node["node_seq"] == current_seq
            status_code, status_label = self._node_display_status(
                str(node.get("status") or "pending"), is_current=is_current
            )
            confidence = node.get("detection_confidence")
            items.append(
                {
                    "node_seq": node["node_seq"],
                    "source": node.get("source") or "journey",
                    "step_name": node.get("step_name"),
                    "page_role": node.get("page_role"),
                    "page_type": node.get("page_type"),
                    "page_type_name": (
                        "弹窗页面"
                        if is_web_v2 and node.get("page_type") == "popup_ad"
                        else page_type_display_name(node.get("page_type"))
                    ),
                    "page_state": node.get("page_state"),
                    "status": status_code,
                    "status_label": status_label,
                    "is_current": is_current,
                    "page_capture_id": capture_id,
                    **(
                        {"capture_role": node.get("capture_role")}
                        if is_h5_v2
                        else {}
                    ),
                    "logical_page_id": node.get("logical_page_id"),
                    "canonical_url": node.get("canonical_url"),
                    "capture_ids": list(capture_ids),
                    "capture_count": len(capture_ids),
                    "primary_capture_id": node.get("primary_capture_id") or capture_id,
                    "capture_versions": node.get("capture_versions") or [],
                    "classification_status": node.get("classification_status"),
                    "raw_page_type": node.get("raw_page_type"),
                    "proposed_page_type": node.get("proposed_page_type"),
                    "observed_page_type": node.get("observed_page_type"),
                    "expected_page_type": node.get("expected_page_type"),
                    "conflict_reason_code": node.get("conflict_reason_code"),
                    "diagnostic_message": node.get("diagnostic_message"),
                    "classification_confidence": node.get(
                        "classification_confidence"
                    ),
                    "classification_review_reason": node.get(
                        "classification_review_reason"
                    ),
                    "url": node.get("url"),
                    "package_name": node.get("package_name"),
                    "activity": node.get("activity"),
                    "width": node.get("width"),
                    "height": node.get("height"),
                    "screenshot_artifact_id": node.get("screenshot_artifact_id"),
                    "navigation_retry": node.get("navigation_retry"),
                    "page_settle_diagnostics": node.get(
                        "page_settle_diagnostics"
                    ),
                    "detection": {
                        "status": node.get("detection_status"),
                        "confidence": float(confidence) if confidence is not None else None,
                        "confidence_source": node.get("detection_confidence_source"),
                        "model_page_type": node.get("detection_model_page_type"),
                        "model_confidence": (
                            float(node["detection_model_confidence"])
                            if node.get("detection_model_confidence") is not None
                            else None
                        ),
                        "duration_ms": node.get("detection_duration_ms"),
                    },
                    "inspection": inspection,
                    "parent_page_capture_id": node.get("parent_page_capture_id"),
                    "element_count": node.get("element_count"),
                    "rule_progress": {
                        "executed": len(node_executions),
                        "total": candidate_total,
                    },
                    "result_rollup": rollup,
                    "result_rollup_label": (
                        rule_display_label(rollup) if rollup is not None else None
                    ),
                    "result_counts": _count_by(display_statuses),
                    "attention_items": attention_items,
                    "current_rule_code": node.get("current_rule_code"),
                    "first_visited_at": node.get("first_visited_at"),
                    "captured_at": node.get("captured_at"),
                    "finished_at": node.get("finished_at"),
                }
            )
        result = {
            "task_id": task_id,
            "task_status": context["status"],
            "run_id": run_id,
            "journey_steps": _journey_step_names(context),
            "current_node_seq": current_seq,
            "exploration_summary": exploration_summary,
            "exploration_actions": exploration_actions,
            "nodes": items,
            **({"web_identity_error": web_identity_error} if is_web_v2 else {}),
            "execution_mode": execution_mode.value,
            "surface_session_summary": surface_session_summary,
        }
        if is_web_v2:
            from services.api.app.application.web_report_explanation import (
                load_web_report_explanation,
            )

            result["web_report_explanation"] = await load_web_report_explanation(
                self._repository, task_id=task_id, agent_run_id=run_id, access_scope=access_scope,
            )
        if is_h5_v2:
            result.update(
                {
                    "frontier": frontier or {},
                    "browser_recovery": _h5_browser_recovery_projection(
                        h5_work_items
                    ),
                    "h5_model_contract": _h5_model_contract_projection(
                        work_items=h5_work_items,
                        actions=exploration_actions,
                    ),
                    "h5_assessments": assessments,
                    "h5_evidence_inventory": _h5_inventory_projection(h5_inventory),
                    "h5_evidence_preflight": project_h5_evidence_preflight(
                        inventory_items=h5_inventory,
                        rule_executions=execution_rows,
                        closure=h5_full_closure,
                    ),
                    "h5_coverage": h5_coverage,
                    "terminal_reason": terminal["terminal_reason"],
                    "full_inspection_closure": h5_full_closure,
                }
            )
        return maybe_scrub_app_report_payload(
            result,
            target_url=context.get("target_url"),
            task_config=(
                context.get("task_config")
                if isinstance(context.get("task_config"), dict)
                else None
            ),
        )

    # ---------- 规则执行结果 ----------

    async def rule_results(
        self,
        task_id: str,
        *,
        display_status: str | None = None,
        page_type: str | None = None,
        access_scope: AccessScope | None = None,
    ) -> dict[str, Any]:
        context = await self._require_context(task_id, access_scope=access_scope)
        items, inventory_summary = await self._assemble_rule_projection(task_id, context, access_scope)
        filtered = [
            item
            for item in items
            if (display_status is None or item["display_status"] == display_status)
            and (page_type is None or item["bound_page_type"] == page_type)
        ]
        return maybe_scrub_app_report_payload(
            {
                "task_id": task_id,
                "task_status": context["status"],
                "run_id": context.get("run_id"),
                "total": len(filtered),
                "status_counts": _count_by([item["display_status"] for item in items]),
                "items": filtered,
                **inventory_summary,
            },
            target_url=context.get("target_url"),
            task_config=(
                context.get("task_config")
                if isinstance(context.get("task_config"), dict)
                else None
            ),
        )

    # ---------- 规则证据缺口（补采请求台账，只记录不执行） ----------

    async def evidence_gaps(
        self,
        task_id: str,
        *,
        access_scope: AccessScope | None = None,
    ) -> dict[str, Any]:
        """任务里所有「证据不足」判定声明的结构化缺口（不分 Run）；只读，不触发任何补采。"""
        context = await self._require_context(task_id, access_scope=access_scope)
        rows = await self._repository.list_evidence_gap_rows(
            task_id=task_id,
            agent_run_id=None,
            access_scope=access_scope,
        )
        items: list[dict[str, Any]] = []
        for row in rows:
            gap = row.get("evidence_gap")
            if isinstance(gap, str):
                try:
                    gap = json.loads(gap)
                except ValueError:
                    gap = None
            if not isinstance(gap, dict):
                continue
            items.append(
                {
                    "execution_id": row.get("execution_id"),
                    "rule_id": row.get("rule_id"),
                    "rule_code": row.get("rule_code"),
                    "rule_name": row.get("rule_name"),
                    "category": row.get("category"),
                    "severity": row.get("rule_severity"),
                    "execution_status": row.get("execution_status"),
                    "page_capture_id": row.get("page_capture_id"),
                    "logical_page_key": row.get("logical_page_key"),
                    "state_fingerprint": row.get("state_fingerprint"),
                    "created_at": row.get("created_at"),
                    "gap": gap,
                    "route_plan": _recapture_route_plan(row),
                }
            )
        gaps = [item["gap"] for item in items]
        return maybe_scrub_app_report_payload(
            {
                "protocol_version": "evidence-gap-list.v1",
                "task_id": task_id,
                "task_status": context["status"],
                "run_id": context.get("run_id"),
                "total": len(items),
                "phase_counts": _count_by([str(gap.get("overlay_phase") or "") for gap in gaps]),
                "policy_counts": _count_by(
                    [str(gap.get("recapture_policy") or "") for gap in gaps]
                ),
                "disposition_counts": _count_by(
                    [str(gap.get("disposition") or "") for gap in gaps]
                ),
                # 当前阶段：缺口和将要提出的请求只记录，运行面不执行补采。
                "execution_policy": "recorded_only",
                # 补采选路结果分布：没规划出路线时按原因分档，别只给一个总数。
                "route_plan_counts": _count_by(
                    [str(item["route_plan"]["state"]) for item in items]
                ),
                "route_blocked_reasons": _count_by(
                    [
                        str(item["route_plan"].get("reason_label") or "")
                        for item in items
                        if item["route_plan"].get("reason_label")
                    ]
                ),
                "items": items,
            },
            target_url=context.get("target_url"),
            task_config=(
                context.get("task_config")
                if isinstance(context.get("task_config"), dict)
                else None
            ),
        )

    # ---------- 收口结论：四个维度分别给出结果 ----------

    async def closure_outcome(
        self,
        task_id: str,
        *,
        access_scope: AccessScope | None = None,
    ) -> dict[str, Any]:
        """页面探索、证据充分、规则结果、最终完成检查各自出一个结论，只读不改状态。"""
        context = await self._require_context(task_id, access_scope=access_scope)
        run_id = context.get("run_id")
        facts = await self._repository.fetch_closure_outcome_facts(
            task_id=task_id,
            agent_run_id=run_id,
        )
        gate = facts.get("gate") or {}
        block_reason_codes = _closure_block_reason_codes(gate.get("block_reasons"))
        request_counts = facts.get("supplement_request_counts") or {}
        open_request_count = sum(
            int(count)
            for status, count in request_counts.items()
            if status in {"pending", "routed", "running"}
        )
        settled_request_count = sum(
            int(count)
            for status, count in request_counts.items()
            if status not in {"pending", "routed", "running"}
        )
        evidence_facts = facts.get("evidence") or {}
        items, inventory_summary = await self._assemble_rule_projection(task_id, context, access_scope)
        if inventory_summary:
            items = [item for item in items if item["item_type"] == "execution"]
        group_counts = _count_by(
            [display_status_group(item["display_status"]) for item in items]
        )
        dimensions = [
            exploration_dimension(
                exploration_terminal=gate.get("exploration_terminal"),
                pending_action_count=int(gate.get("pending_action_count") or 0),
                pending_capture_commit_count=int(gate.get("pending_capture_commit_count") or 0),
                background_execution_count=int(gate.get("background_execution_count") or 0),
                visited_page_type_count=int(facts.get("visited_page_type_count") or 0),
            ),
            evidence_dimension(
                gap_count=int(evidence_facts.get("gap_count") or 0),
                presentation_violation_count=int(
                    evidence_facts.get("presentation_violation_count") or 0
                ),
                open_request_count=open_request_count,
                settled_request_count=settled_request_count,
            ),
            rule_dimension(
                total=len(items),
                group_counts=group_counts,
                group_labels=display_status_group_labels(),
            ),
            completion_gate_dimension(
                gate_passed=gate.get("gate_passed"),
                block_reason_codes=block_reason_codes,
                evaluated_at=gate.get("evaluated_at"),
                nonterminal_work_item_count=int(
                    gate.get("nonterminal_enforced_work_item_count") or 0
                ),
                open_review_count=int(gate.get("open_review_count") or 0),
            ),
        ]
        return maybe_scrub_app_report_payload(
            {
                "protocol_version": CLOSURE_OUTCOME_PROTOCOL_VERSION,
                "task_id": task_id,
                "task_status": context["status"],
                "run_id": run_id,
                "summary": overall_summary(dimensions),
                "supplement_request_counts": dict(request_counts),
                "dimensions": [item.to_dict() for item in dimensions],
            },
            target_url=context.get("target_url"),
            task_config=(
                context.get("task_config")
                if isinstance(context.get("task_config"), dict)
                else None
            ),
        )

    # ---------- 页面类型判断与改判台账（只记录、不改正式识别） ----------

    async def page_type_judgements(
        self,
        task_id: str,
        *,
        access_scope: AccessScope | None = None,
    ) -> dict[str, Any]:
        """每个页面的全部页型判断、按记录算出的裁定，以及与正式识别结果的差异。"""
        context = await self._require_context(task_id, access_scope=access_scope)
        rows = await self._repository.list_page_type_judgement_rows(
            task_id=task_id,
            agent_run_id=context.get("run_id"),
            access_scope=access_scope,
        )
        page_facts: dict[str, dict[str, Any]] = {}
        judgements = []
        for row in rows:
            try:
                judgement = judgement_from_mapping(row)
            except (KeyError, ValueError, TypeError):
                continue
            judgements.append(judgement)
            page_facts.setdefault(
                judgement.page_capture_id,
                {
                    "formal_page_type": row.get("formal_page_type"),
                    "page_seq": row.get("page_seq"),
                    "page_slug": row.get("page_slug"),
                    "page_url": row.get("page_url") or "",
                    "logical_page_key": row.get("logical_page_key"),
                },
            )
        items: list[dict[str, Any]] = []
        for page in group_judgements(judgements):
            facts = page_facts.get(page.page_capture_id, {})
            resolution = page.resolution
            formal_page_type = facts.get("formal_page_type")
            items.append(
                {
                    "page_capture_id": page.page_capture_id,
                    "page_seq": facts.get("page_seq"),
                    "page_slug": facts.get("page_slug"),
                    "page_url": facts.get("page_url") or "",
                    "logical_page_key": facts.get("logical_page_key"),
                    "formal_page_type": formal_page_type,
                    "resolution": resolution.to_dict(),
                    # 裁定与正式识别不一致（冲突→未知，或改判后的类型不同）才需要人看
                    "differs_from_formal": resolution.page_type != formal_page_type,
                    "judgements": [item.to_dict() for item in page.judgements],
                }
            )
        statuses = [item["resolution"]["status"] for item in items]
        return maybe_scrub_app_report_payload(
            {
                "protocol_version": "page-type-judgement-list.v1",
                "task_id": task_id,
                "task_status": context["status"],
                "run_id": context.get("run_id"),
                "total": len(items),
                "status_counts": _count_by(statuses),
                "conflicting_count": statuses.count("conflicting"),
                "revised_count": statuses.count("revised"),
                "differs_count": sum(1 for item in items if item["differs_from_formal"]),
                # 当前阶段：判断只记录、裁定只展示，正式页型仍由识别链路决定。
                "resolution_policy": "recorded_only",
                "items": items,
            },
            target_url=context.get("target_url"),
            task_config=(
                context.get("task_config")
                if isinstance(context.get("task_config"), dict)
                else None
            ),
        )

    # ---------- 规则结论复用台账（来源、依据与失效条件，只读） ----------

    async def rule_reuse(
        self,
        task_id: str,
        *,
        access_scope: AccessScope | None = None,
    ) -> dict[str, Any]:
        """任务里沿用了别处结论的规则判定（不分 Run）；带来源页面、复用范围、依据与失效条件。"""
        context = await self._require_context(task_id, access_scope=access_scope)
        rows = await self._repository.list_rule_reuse_rows(
            task_id=task_id,
            agent_run_id=None,
            access_scope=access_scope,
        )
        items: list[dict[str, Any]] = []
        for row in rows:
            reuse = row.get("reuse")
            if isinstance(reuse, str):
                try:
                    reuse = json.loads(reuse)
                except ValueError:
                    reuse = None
            if not isinstance(reuse, dict):
                continue
            scope = str(reuse.get("scope") or "")
            declared_conditions = reuse.get("invalidate_on")
            conditions = (
                [str(item) for item in declared_conditions]
                if isinstance(declared_conditions, list) and declared_conditions
                else list(invalidation_conditions(scope))
            )
            basis = reuse.get("basis")
            items.append(
                {
                    "execution_id": row.get("execution_id"),
                    "rule_id": row.get("rule_id"),
                    "rule_code": row.get("rule_code"),
                    "rule_name": row.get("rule_name"),
                    "category": row.get("category"),
                    "severity": row.get("rule_severity"),
                    "execution_status": row.get("execution_status"),
                    "result_summary": row.get("result_summary"),
                    "page_capture_id": row.get("page_capture_id"),
                    "page_type": row.get("page_type") or "",
                    "page_url": row.get("page_url") or "",
                    "scope": scope,
                    "scope_label": reuse_scope_label(scope),
                    "reuse_key": str(reuse.get("reuse_key") or ""),
                    "reason": str(reuse.get("reason") or ""),
                    "invalidate_on": [
                        {"code": code, "label": invalidation_label(code)} for code in conditions
                    ],
                    "basis": basis if isinstance(basis, dict) else {},
                    "source": {
                        "execution_id": reuse.get("source_execution_id"),
                        "page_capture_id": reuse.get("source_page_capture_id"),
                        "page_type": (
                            row.get("source_page_type") or reuse.get("source_page_type") or ""
                        ),
                        "page_url": (
                            row.get("source_page_url") or reuse.get("source_page_url") or ""
                        ),
                        "execution_status": (
                            row.get("source_execution_status")
                            or reuse.get("source_execution_status")
                        ),
                    },
                    "created_at": row.get("created_at"),
                }
            )
        return maybe_scrub_app_report_payload(
            {
                "protocol_version": "rule-reuse-list.v1",
                "task_id": task_id,
                "task_status": context["status"],
                "run_id": context.get("run_id"),
                "total": len(items),
                "scope_counts": _count_by([item["scope"] for item in items]),
                "status_counts": _count_by([str(item["execution_status"] or "") for item in items]),
                # 复用的结论与真实执行分开呈现：这里只有沿用来的，真实执行看规则结果列表。
                "presentation": "reused_only",
                "items": items,
            },
            target_url=context.get("target_url"),
            task_config=(
                context.get("task_config")
                if isinstance(context.get("task_config"), dict)
                else None
            ),
        )

    # ---------- 命中规则统计与最近异常 ----------

    async def stats(
        self,
        task_id: str,
        *,
        recent_limit: int = 10,
        access_scope: AccessScope | None = None,
    ) -> dict[str, Any]:
        context = await self._require_context(task_id, access_scope=access_scope)
        items, inventory_summary = await self._assemble_rule_projection(task_id, context, access_scope)
        executed = [item for item in items if item["item_type"] == "execution"]
        quality_executions = [
            item
            for item in executed
            if rule_execution_counts_as_used(
                item["execution_status"],
                item["execution_reason"],
                is_bootstrap=item["is_bootstrap"],
                machine_display_status=item.get("machine_display_status"),
            )
        ]
        counts = _count_by([item["display_status"] for item in (executed if inventory_summary else items)])
        exploration_actions = await self._repository.list_exploration_actions(
            agent_run_id=context.get("run_id")
        )
        frontier_pending = count_pending_frontier_actions(exploration_actions)
        finished = sum(
            rule_execution_counts_as_completed(
                item["execution_status"],
                item["execution_reason"],
                is_bootstrap=item["is_bootstrap"],
                result_summary=item["result_summary"],
            )
            for item in quality_executions
        )
        exploration_only = (
            task_execution_mode_from_config(context.get("task_config"))
            is TaskExecutionMode.EXPLORATION_ONLY
        )
        total = (
            0
            if exploration_only
            else max(
                int(context.get("ruleset_rule_total") or 0),
                len(quality_executions if inventory_summary else items),
            )
        )
        run_loop_guard = context.get("run_loop_guard") or {}
        # 冻结 Run 的时长别名整体优先，避免任务 max_duration_seconds 覆盖 Run timeout_minutes。
        progress_loop_guard = (
            run_loop_guard
            if any(key in run_loop_guard for key in ("max_duration_seconds", "timeout_minutes"))
            else context.get("loop_guard")
        )
        progress_percent = whole_task_progress_percent(
            str(context["status"]),
            started_at=context.get("started_at"),
            loop_guard=progress_loop_guard,
            run_status=context.get("run_status"),
        )
        anomalies = [
            item
            for item in executed
            if item["display_status"]
            in {"detect_error", "evidence_insufficient", "manual_review", "not_executed"}
        ]
        anomalies.sort(key=lambda item: item.get("finished_at") or _EPOCH, reverse=True)
        return maybe_scrub_app_report_payload(
            {
                "task_id": task_id,
                "task_status": context["status"],
                "total": total,
                "triggered": len(quality_executions),
                "finished": finished,
                "frontier_pending": frontier_pending,
                "progress_percent": progress_percent,
                "status_counts": counts,
                "status_labels": dict(RULE_DISPLAY_LABELS),
                **inventory_summary,
                "recent_anomalies": [
                    {
                        "rule_code": item["rule_code"],
                        "rule_name": item["rule_name"],
                        "display_status": item["display_status"],
                        "display_status_label": item["display_status_label"],
                        "severity_display": item["severity_display"],
                        "bound_page_type": item["bound_page_type"],
                        "bound_page_type_name": item["bound_page_type_name"],
                        "problem_desc": item["problem_desc"],
                        "occurred_at": item["finished_at"],
                    }
                    for item in anomalies[:recent_limit]
                ],
            },
            target_url=context.get("target_url"),
            task_config=(
                context.get("task_config")
                if isinstance(context.get("task_config"), dict)
                else None
            ),
        )

    async def app_stage(
        self,
        task_id: str,
        *,
        access_scope: AccessScope | None = None,
    ) -> dict[str, Any]:
        """工作台 App 任务的 Bootstrap / Work / Gate 只读摘要。"""
        context = await self._require_context(task_id, access_scope=access_scope)
        target_platform = _optional_text(context.get("target_platform"))
        is_app = (
            target_platform.casefold() == "app"
            if target_platform is not None
            else _legacy_app_platform_fallback(context)
        )
        if not is_app:
            return {"applicable": False, "task_id": task_id}
        execution_mode = task_execution_mode_from_config(context.get("task_config"))
        stage = await self._repository.fetch_app_stage(task_id)
        bootstrap = stage.get("bootstrap")
        if isinstance(bootstrap, dict):
            bootstrap = {
                **bootstrap,
                "ready_at": _as_iso(bootstrap.get("ready_at")),
                "updated_at": _as_iso(bootstrap.get("updated_at")),
            }
        gate = stage.get("gate")
        if isinstance(gate, dict):
            reasons = gate.get("block_reasons")
            if isinstance(reasons, str):
                try:
                    reasons = json.loads(reasons)
                except json.JSONDecodeError:
                    reasons = []
            if not isinstance(reasons, list):
                reasons = []
            gate = {
                **gate,
                "block_reasons": [str(item) for item in reasons],
                "evaluated_at": _as_iso(gate.get("evaluated_at")),
            }
        evidence_index = stage.get("evidence_index")
        app_exploration = _app_exploration_projection(stage.get("app_exploration"))
        return {
            "applicable": True,
            "task_id": task_id,
            "task_status": context.get("status"),
            "execution_mode": execution_mode.value,
            "bootstrap": bootstrap,
            "exploration_work": stage.get("exploration_work") or {},
            "gate": gate,
            "inspection_summary": stage.get("inspection_summary") or {},
            "publication_projection": stage.get("publication_projection")
            or {
                "current_stage": "not_started",
                "publication_count": 0,
                "status_counts": {},
                "stage_counts": {
                    "sealed": 0,
                    "delivered": 0,
                    "claimed": 0,
                    "judged": 0,
                },
                "sealed_count": 0,
                "delivered_count": 0,
                "claimed_count": 0,
                "judged_count": 0,
                "outbox_pending_count": 0,
                "outbox_dead_letter_count": 0,
                "inbox_pending_count": 0,
                "inbox_dead_letter_count": 0,
                "needs_review_count": 0,
                "failed_count": 0,
                "items": [],
            },
            "app_exploration": app_exploration,
            "exploration_status": (
                evidence_index.get("status") if isinstance(evidence_index, dict) else None
            ),
            "evidence_index": evidence_index,
        }

    async def evidence_index(
        self,
        task_id: str,
        *,
        access_scope: AccessScope | None = None,
    ) -> dict[str, Any]:
        """只读回传已封存的探索 Evidence Index。"""
        await self._require_context(task_id, access_scope=access_scope)
        index = await self._repository.fetch_evidence_index(task_id)
        if index is None:
            raise TaskInsightNotFoundError("任务尚未封存 Evidence Index")
        return index

    async def web_evidence(
        self,
        task_id: str,
        *,
        access_scope: AccessScope | None = None,
    ) -> dict[str, object]:
        """返回当前 Web V2 main Run 的脱敏证据闭包。"""

        if not isinstance(access_scope, AccessScope):
            raise TaskInsightNotFoundError("Web V2 证据包需要明确的任务读取范围")
        context = await self._require_context(task_id, access_scope=access_scope)
        if not _is_web_v2_evidence_context(context):
            raise TaskInsightNotFoundError("当前任务不是独立 Web V2 探索")
        agent_run_id = _optional_text(context.get("run_id"))
        if agent_run_id is None:
            raise TaskInsightNotFoundError("当前 Web V2 任务尚无 main Run")
        repository = self._web_evidence_repository
        if repository is None:
            from services.api.app.infrastructure.db.web_capture_evidence_repository import (
                PostgresWebCaptureEvidenceRepository,
            )

            repository = PostgresWebCaptureEvidenceRepository()
            self._web_evidence_repository = repository
        snapshot = await repository.list_for_task(
            task_id=task_id,
            agent_run_id=agent_run_id,
            access_scope=access_scope,
        )
        if snapshot is None:
            raise TaskInsightNotFoundError("当前 main Run 不存在唯一 Web V2 预算与会话")
        if (
            _optional_text(snapshot.get("task_id")) != task_id
            or _optional_text(snapshot.get("agent_run_id")) != agent_run_id
        ):
            raise TaskInsightNotFoundError("Web V2 证据包与当前任务或 Run 不一致")
        from services.api.app.application.web_legacy_run_audit import LegacyWebRunAuditAdapter
        from services.api.app.domain.exploration.web_run_policy_snapshot import WebRunPolicyError
        from services.api.app.infrastructure.db.web_run_policy_snapshot_repository import (
            PostgresWebRunPolicySnapshotRepository,
        )

        policy_repository = self._web_policy_repository or PostgresWebRunPolicySnapshotRepository()
        readiness_profile = None
        policy_error = None
        policy_audit = {}
        try:
            policy_snapshot = await policy_repository.read(
                task_id=task_id, agent_run_id=agent_run_id
            )
            if policy_snapshot is None:
                raise WebRunPolicyError("WEB_POLICY_HISTORY_MISSING: 原 Run 无完整冻结正文")
            if policy_snapshot.task_id != task_id or policy_snapshot.agent_run_id != agent_run_id:
                raise WebRunPolicyError("WEB_POLICY_OWNERSHIP_INVALID: 原 Run 策略归属不符")
            readiness_profile = LegacyWebRunAuditAdapter.load(policy_snapshot).readiness_profile
            policy_audit = {
                "payload_sha256": policy_snapshot.payload_sha256,
                "guide_version": policy_snapshot.payload["guide_version"],
                "readiness_profile_version": readiness_profile.version,
            }
        except WebRunPolicyError as exc:
            policy_error = str(exc).split(":", 1)[0]
        bundle = build_web_evidence_bundle(
            snapshot, readiness_profile=readiness_profile, policy_error=policy_error
        )
        bundle["policy"] = {"status": "unavailable" if policy_error else "verified", **policy_audit}
        formal_pages = bundle.get("formal_pages")
        pages = formal_pages if isinstance(formal_pages, list) else []
        capabilities = sorted(
            {
                str(capability)
                for page in pages
                if isinstance(page, Mapping)
                for capability in (page.get("product_capabilities") or [])
                if isinstance(capability, str) and capability
            }
        )
        bundle["product_capability_summary"] = {
            "capabilities": capabilities,
            "formal_page_count": len(pages),
            "source": "formal_capture_assessments",
        }
        return bundle

    # ---------- 持久化质检工作项 ----------

    async def inspection_work_items(
        self,
        task_id: str,
        *,
        control_mode: str | None = None,
        access_scope: AccessScope | None = None,
    ) -> dict[str, Any]:
        """投影工作项、规则成员与 Attempt，供工作台排障和验收读取。"""
        context = await self._require_context(task_id, access_scope=access_scope)
        rows = await self._repository.list_inspection_work_item_rows(
            task_id=task_id,
            control_mode=control_mode,
        )
        work_items = [dict(item) for item in rows["work_items"]]
        rules_by_work_item: dict[str, list[dict[str, Any]]] = {}
        for rule in rows["rules"]:
            item = dict(rule)
            work_item_id = str(item.pop("work_item_id"))
            rules_by_work_item.setdefault(work_item_id, []).append(item)
        attempts_by_work_item: dict[str, list[dict[str, Any]]] = {}
        for attempt in rows["attempts"]:
            item = dict(attempt)
            work_item_id = str(item.pop("work_item_id"))
            attempts_by_work_item.setdefault(work_item_id, []).append(item)

        for item in work_items:
            work_item_id = str(item["work_item_id"])
            item["rules"] = rules_by_work_item.get(work_item_id, [])
            item["attempts"] = attempts_by_work_item.get(work_item_id, [])

        statuses = [str(item["status"]) for item in work_items]
        rule_statuses = [
            str(rule["status"]) for rules in rules_by_work_item.values() for rule in rules
        ]
        attempt_statuses = [
            str(attempt["status"])
            for attempts in attempts_by_work_item.values()
            for attempt in attempts
        ]
        terminal_statuses = {
            "succeeded",
            "partial",
            "failed",
            "cancelled",
            "superseded",
        }
        return {
            "task_id": task_id,
            "task_status": context["status"],
            "run_id": context.get("run_id"),
            "control_mode": control_mode,
            "summary": {
                "work_item_count": len(work_items),
                "enforced_count": sum(item["control_mode"] == "enforced" for item in work_items),
                "shadow_count": sum(item["control_mode"] == "shadow" for item in work_items),
                "nonterminal_count": sum(
                    status not in terminal_statuses and item["control_mode"] == "enforced"
                    for item, status in zip(work_items, statuses, strict=True)
                ),
                "failed_count": statuses.count("failed"),
                "active_session_failed_count": sum(
                    item["status"] == "failed"
                    and item["session_dependency"] == "active_session"
                    and item.get("last_error_code") == "INSPECTION_ACTIVE_SESSION_LOST"
                    for item in work_items
                ),
                "status_counts": _count_by(statuses),
                "rule_status_counts": _count_by(rule_statuses),
                "attempt_status_counts": _count_by(attempt_statuses),
            },
            "items": work_items,
        }

    # ---------- 执行日志 ----------

    async def logs(
        self,
        task_id: str,
        *,
        level: str | None = None,
        page: int = 1,
        page_size: int = 100,
        access_scope: AccessScope | None = None,
    ) -> dict[str, Any]:
        await self._require_context(task_id, access_scope=access_scope)
        offset = (max(page, 1) - 1) * page_size
        items, total = await self._repository.list_logs(
            task_id=task_id, level=level, limit=page_size, offset=offset
        )
        return {
            "task_id": task_id,
            "total": total,
            "page": max(page, 1),
            "page_size": page_size,
            "items": items,
        }

    async def logs_export_text(
        self,
        task_id: str,
        *,
        access_scope: AccessScope | None = None,
    ) -> str:
        await self._require_context(task_id, access_scope=access_scope)
        items, _ = await self._repository.list_logs(
            task_id=task_id, level=None, limit=None, offset=0
        )
        lines = []
        for item in items:
            ts = item.get("ts")
            stamp = ts.strftime("%Y-%m-%d %H:%M:%S") if isinstance(ts, datetime) else str(ts)
            lines.append(f"[{stamp}] {item.get('level'):5s} {item.get('message')}")
        return "\n".join(lines) + ("\n" if lines else "")

    # ---------- 截图证据 ----------

    async def screenshots(
        self,
        task_id: str,
        *,
        access_scope: AccessScope | None = None,
    ) -> dict[str, Any]:
        context = await self._require_context(task_id, access_scope=access_scope)
        run_id = context.get("run_id")
        artifacts = await self._repository.list_screenshot_artifacts(
            task_id=task_id, agent_run_id=run_id
        )
        related_rules = await self._related_rules_by_artifact(task_id, run_id)
        is_h5 = str(context.get("target_platform") or "").casefold() == "h5"
        groups: dict[str, dict[str, Any]] = {}
        target_platform = str(context.get("target_platform") or "").casefold()
        for artifact in artifacts:
            if (
                artifact.get("capture_purpose") == "web_ad_page"
                and artifact.get("screenshot_purpose") != "ad_landing"
            ):
                continue
            capture_role = _optional_text(artifact.get("capture_role"))
            gallery_visible = not is_h5 or capture_role in {None, "after_action"}
            page_type = artifact.get("page_type") or "unknown"
            page_title = _optional_text(artifact.get("page_title"))
            logical_identity = _optional_text(
                artifact.get("logical_identity_fingerprint")
            )
            logical_state = _optional_text(artifact.get("logical_state_fingerprint"))
            if target_platform == "app":
                # 截图证据展示按包+Activity+页型聚合，不用视觉页名拆组。
                # 上方分类「最新／推荐／国产」等同屏网格若按页名分组会刷出多组相同图。
                page_group_key = "page-group:" + stable_protocol_hash(
                    {
                        "platform": "app",
                        "page_type": str(page_type).casefold(),
                        "package_name": str(artifact.get("package_name") or "").casefold(),
                        "activity": str(artifact.get("activity") or "").casefold(),
                    }
                )
                type_label = page_type_display_name(page_type) or str(page_type)
                page_display_name = type_label
            else:
                page_group_key = "page-group:" + stable_protocol_hash(
                    {
                        "platform": context.get("target_platform") or "unknown",
                        "page_title": (
                            None
                            if is_h5 and logical_identity
                            else (page_title or "").casefold()
                        ),
                        "page_type": str(page_type).casefold(),
                        "package_name": str(artifact.get("package_name") or "").casefold(),
                        "activity": str(artifact.get("activity") or "").casefold(),
                        "logical_identity": logical_identity if is_h5 else None,
                        "url": (
                            None
                            if is_h5 and logical_identity
                            else str(artifact.get("url") or "").casefold()
                        ),
                    }
                )
                page_display_name = f"{page_title or '未命名页面'}-{page_type}"
            group = groups.setdefault(
                page_group_key,
                {
                    "page_group_key": page_group_key,
                    "page_title": page_title,
                    "page_display_name": page_display_name,
                    "page_type": artifact.get("page_type"),
                    "page_type_name": page_type_display_name(artifact.get("page_type"))
                    or "未识别页面",
                    "total": 0,
                    "items": [],
                    "_seen_checksums": {},
                    "_seen_logical_states": {},
                },
            )
            group["total"] += 1
            logical_state_key = (
                _h5_gallery_variant_key(artifact)
                if is_h5 and gallery_visible and logical_identity and logical_state
                else None
            )
            seen_logical_states = group["_seen_logical_states"]
            duplicate_representative: dict[str, Any] | None = None
            if logical_state_key and logical_state_key in seen_logical_states:
                duplicate_representative = seen_logical_states[logical_state_key]
                duplicate_representative["duplicate_count"] += 1
                duplicate_representative["related_rules"] = _merge_related_rules(
                    duplicate_representative["related_rules"],
                    related_rules.get(artifact["artifact_id"], []),
                )
                gallery_visible = False
            checksum = artifact.get("checksum")
            seen = group["_seen_checksums"]
            if duplicate_representative is None and checksum and checksum in seen:
                duplicate_representative = seen[checksum]
                duplicate_representative["duplicate_count"] += 1
                if not is_h5:
                    continue
                duplicate_representative["related_rules"] = _merge_related_rules(
                    duplicate_representative["related_rules"],
                    related_rules.get(artifact["artifact_id"], []),
                )
                gallery_visible = False
            item = {
                "artifact_id": artifact["artifact_id"],
                "page_capture_id": artifact.get("page_capture_id"),
                "captured_at": artifact.get("captured_at") or artifact.get("created_at"),
                "package_name": artifact.get("package_name"),
                "activity": artifact.get("activity"),
                "width": artifact.get("width"),
                "height": artifact.get("height"),
                "inspection_lifecycle_status": artifact.get("inspection_lifecycle_status"),
                "inspection_failure_code": artifact.get("inspection_failure_code"),
                "inspection_failure_message": artifact.get("inspection_failure_message"),
                "content_type": artifact.get("content_type"),
                "size_bytes": artifact.get("size_bytes"),
                "duplicate_count": 1,
                "related_rules": related_rules.get(artifact["artifact_id"], []),
                "capture_role": capture_role,
                "capture_purpose": _optional_text(artifact.get("capture_purpose")),
                "gallery_visible": gallery_visible,
                "duplicate_of_artifact_id": (
                    duplicate_representative.get("artifact_id")
                    if duplicate_representative is not None
                    else None
                ),
                # 探索过程展示：截图质量=这一帧最上层是谁、底页是否可见、是否诊断材料、页面是否稳定
                "page_type": _optional_text(artifact.get("page_type")),
                "page_state": _optional_text(artifact.get("page_state")),
                "occlusion_kind": _screenshot_occlusion_kind(artifact),
                "related_frames": [],
                "presentation": _compact_capture_presentation(artifact.get("presentation")),
                "settle": _compact_settle_diagnostics(artifact.get("settle_diagnostics")),
            }
            if checksum and (not is_h5 or gallery_visible):
                seen[checksum] = item
            if logical_state_key and gallery_visible:
                seen_logical_states[logical_state_key] = item
            group["items"].append(item)
        for group in groups.values():
            group.pop("_seen_checksums", None)
            group.pop("_seen_logical_states", None)
            if not is_h5:
                group["items"] = _collapse_screenshot_occlusion_clusters(group["items"])
                # 组内展示上限：开屏／弹层／稳定合计最多 2 张代表图，其余并入首张 related。
                group["items"] = _cap_screenshot_representatives(group["items"], max_items=2)
        return {
            "task_id": task_id,
            "run_id": run_id,
            "groups": list(groups.values()),
        }

    async def screenshots_archive(
        self,
        task_id: str,
        *,
        access_scope: AccessScope | None = None,
    ) -> tuple[str, bytes]:
        """打包当前 Run 截图；普通图去重，Web 广告角色图逐一保留。"""
        context = await self._require_context(task_id, access_scope=access_scope)
        run_id = context.get("run_id")
        artifacts = await self._repository.list_screenshot_artifacts(
            task_id=task_id, agent_run_id=run_id
        )
        store = self._resolve_evidence_store()
        buffer = io.BytesIO()
        seen_checksums: set[str] = set()
        with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for artifact in artifacts:
                checksum = artifact.get("checksum")
                is_web_ad = artifact.get("capture_purpose") == "web_ad_page"
                if checksum and checksum in seen_checksums and not is_web_ad:
                    continue
                if checksum:
                    seen_checksums.add(checksum)
                content = await store.read(artifact["storage_path"])
                page_type = artifact.get("page_type") or "page"
                page_seq = artifact.get("page_seq") or 0
                ad_role = ""
                if is_web_ad:
                    ad_role = {
                        "ad_landing": "_landing",
                        "ad_landing_consistency": "_pair",
                    }.get(artifact.get("screenshot_purpose"), "_source")
                frames = _decode_screenshot_frames(
                    content,
                    content_type=artifact.get("content_type"),
                )
                for frame_index, frame in enumerate(frames):
                    frame_suffix = f"_frame-{frame_index:02d}" if len(frames) > 1 else ""
                    name = (
                        f"{page_seq:02d}_{page_type}{ad_role}_{artifact['artifact_id'][:8]}"
                        f"{frame_suffix}.png"
                    )
                    archive.writestr(name, frame)
        return f"task_{task_id}_screenshots.zip", buffer.getvalue()

    # ---------- 证据回读 ----------

    async def evidence_file(
        self, artifact_id: str, *, original: bool = False,
    ) -> dict[str, Any] | None:
        """读取单个证据文件内容与元数据；不存在返回 None。"""
        artifact = await self._repository.fetch_evidence_artifact(artifact_id)
        if artifact is None:
            return None
        store = self._resolve_evidence_store()
        content = await store.read(artifact["storage_path"])
        content_type = artifact.get("content_type")
        if original:
            checksum = "sha256:" + hashlib.sha256(content).hexdigest()
            if artifact.get("checksum") != checksum:
                raise ValueError("EVIDENCE_ORIGINAL_CHECKSUM_MISMATCH")
        if artifact["artifact_type"] == "screenshot" and not original:
            # timed_capture/page_coverage 为保留完整审计帧，会把截图序列保存成
            # application/json。浏览器 <img> 不能直接显示 JSON，因此证据回读统一
            # 返回首帧 PNG；原始时序包仍原样保存在证据存储中供 Judge 使用。
            content = _decode_screenshot_frames(
                content,
                content_type=content_type,
            )[0]
            content_type = "image/png"
        return {
            "artifact_id": artifact["artifact_id"],
            "artifact_type": artifact["artifact_type"],
            "content_type": content_type,
            "content": content,
            **({"checksum": artifact["checksum"], "representation": "original"}
               if original else {}),
        }

    async def visited_page_types(self, task_id: str) -> list[str]:
        return await self._repository.list_task_page_types(task_id=task_id)

    async def platform_progress_export(
        self,
        task_id: str,
        *,
        access_scope: AccessScope | None = None,
    ) -> tuple[str, bytes]:
        """导出当前任务的平台能力进展快照，不计算产品通过率。"""
        context = await self._require_context(task_id, access_scope=access_scope)
        all_rules = await self._repository.list_applicable_rules(
            task_id=task_id,
            page_types=None,
        )
        rule_items = await self._assemble_rule_items(task_id, context, access_scope=access_scope)
        timeline = await self.page_timeline(task_id, access_scope=access_scope)
        generated_at = datetime.now(UTC)
        snapshot = build_platform_progress_snapshot(
            context=context,
            all_rules=all_rules,
            rule_items=rule_items,
            timeline=timeline,
            generated_at=generated_at,
        )
        content = build_platform_progress_workbook(snapshot)
        timestamp = generated_at.strftime("%Y%m%d_%H%M%S")
        filename = f"alioth_platform_progress_{task_id}_{timestamp}.xlsx"
        return filename, content

    # ---------- 内部拼装 ----------

    async def _assemble_rule_items(
        self, task_id: str, context: dict[str, Any], *, access_scope: AccessScope | None = None,
    ) -> list[dict[str, Any]]:
        items, _ = await self._assemble_rule_projection(task_id, context, access_scope)
        return items

    async def _assemble_rule_projection(
        self, task_id: str, context: dict[str, Any], access_scope: AccessScope | None,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        items = await self._assemble_base_rule_items(task_id, context)
        from services.api.app.application.green_rule_result_projection import (
            apply_green_results,
            selected_green_rule_keys,
        )

        green_reader = getattr(self._repository, "read_green_rule_results", None)
        green_ids = selected_green_rule_keys(context.get("task_config"))
        selected = [item["execution_id"] for item in items
                    if item.get("execution_id") and item.get("rule_id") in green_ids]
        if selected and callable(green_reader):
            projections = await green_reader(task_id=task_id, execution_ids=selected, access_scope=access_scope)
            apply_green_results(items, projections, expected_rules=green_ids)
        is_h5 = str(context.get("target_platform") or "").casefold() == "h5"
        reader = getattr(self._rule_inventory_repository, "load_snapshot", None)
        if is_h5 and not callable(reader):
            from services.api.app.infrastructure.db.task_rule_inventory_repository import (
                PostgresTaskRuleInventoryRepository,
            )

            reader = PostgresTaskRuleInventoryRepository().load_snapshot
        if not callable(reader) or not requires_task_rule_inventory(
            context.get("target_platform"), context.get("task_config") or {},
        ):
            return items, {}
        kwargs = {"access_scope": access_scope} if access_scope is not None else {}
        if is_h5:
            kwargs["agent_run_id"] = context.get("run_id")
        snapshot = await reader(task_id=task_id, **kwargs)
        if is_h5 and snapshot and snapshot.get("inventory_status") == "frozen":
            items = [item for item in items if item["item_type"] == "execution"]
        return project_task_rule_inventory(
            items, snapshot or {"inventory_status": "unavailable", "unavailable_reason": "task_inventory_not_found"},
            ruleset_rule_total=(
                (context.get("task_config") or {}).get("ruleset_rule_total")
                if is_h5 else context.get("ruleset_rule_total")
            ),
        )

    async def _assemble_base_rule_items(
        self, task_id: str, context: dict[str, Any]
    ) -> list[dict[str, Any]]:
        if (
            task_execution_mode_from_config(context.get("task_config"))
            is TaskExecutionMode.EXPLORATION_ONLY
        ):
            # 页面探索任务只提供证据和页面轨迹，不读取也不推导正式规则结果。
            return []
        run_id = context.get("run_id")
        task_running = context["status"] == "running"
        task_terminal = context["status"] in _TERMINAL_TASK_STATUSES
        # 按质检规则统一口径：规则结果不分 Run（H5/App 的判定结果写在独立判定 Run 下）
        execution_rows = await self._repository.list_rule_execution_rows(
            task_id=task_id, agent_run_id=None
        )
        nodes = (
            await self._repository.list_page_nodes(agent_run_id=run_id)
            if run_id is not None
            else []
        )
        if not nodes:
            nodes = self._derive_nodes_from_journey(context)
        current_seq = self._resolve_current_node_seq(nodes, task_running)
        current_rule_code = next(
            (
                node.get("current_rule_code")
                for node in nodes
                if node["node_seq"] == current_seq and node.get("current_rule_code")
            ),
            None,
        )

        items: list[dict[str, Any]] = []
        executed_keys: set[tuple[str, str | None]] = set()
        for row in execution_rows:
            bound_page_type = row.get("capture_page_type")
            executed_keys.add((row["rule_id"], bound_page_type))
            machine_display = rule_display_status(
                row["execution_status"],
                row.get("execution_reason"),
                is_bootstrap=bool(row.get("is_bootstrap")),
                error_code=row.get("error_code"),
            )
            display = rule_final_display_status(
                row["execution_status"],
                row.get("execution_reason"),
                human_verdict=row.get("human_verdict"),
                is_bootstrap=bool(row.get("is_bootstrap")),
                error_code=row.get("error_code"),
            )
            items.append(
                self._rule_item(
                    item_type="execution",
                    rule_row=row,
                    display=display,
                    machine_display=machine_display,
                    bound_page_type=bound_page_type,
                    execution_row=row,
                )
            )

        scope_nodes = [node for node in nodes if not _is_inactive_capture_node(node)]
        scope_page_types = [str(node["page_type"]) for node in scope_nodes if node.get("page_type")]
        if scope_page_types:
            applicable = await self._repository.list_applicable_rules(
                task_id=task_id, page_types=sorted(set(scope_page_types))
            )
            for node in scope_nodes:
                if task_terminal and node.get("candidate_rule_total") is not None:
                    # 新链路已经持久化精确候选集合；终态不再按页面类型扩成整页规则。
                    continue
                node_page_type = node.get("page_type")
                if not node_page_type:
                    continue
                for rule in applicable:
                    if node_page_type not in rule["applicable_page_types"]:
                        continue
                    if (rule["rule_id"], node_page_type) in executed_keys:
                        continue
                    if task_running and rule["rule_code"] == current_rule_code:
                        display = RULE_DISPLAY_RUNNING
                    elif task_terminal:
                        display = RULE_DISPLAY_COVERAGE_GAP
                    else:
                        display = RULE_DISPLAY_PENDING
                    item = self._rule_item(
                        item_type="candidate",
                        rule_row=rule,
                        display=display,
                        machine_display=display,
                        bound_page_type=node_page_type,
                        execution_row=None,
                    )
                    if task_terminal:
                        item["result_summary"] = _UNVISITED_PENDING_SUMMARY
                    items.append(item)
                    executed_keys.add((rule["rule_id"], node_page_type))
        return items

    def _rule_item(
        self,
        *,
        item_type: str,
        rule_row: dict[str, Any],
        display: str,
        machine_display: str,
        bound_page_type: str | None,
        execution_row: dict[str, Any] | None,
    ) -> dict[str, Any]:
        severity = (
            (execution_row or {}).get("severity")
            or rule_row.get("rule_severity")
            or rule_row.get("severity")
        )
        applicable = rule_row.get("applicable_page_types") or []
        review_status = (execution_row or {}).get("review_status")
        execution = execution_row or {}
        confirmable = not bool(execution.get("is_bootstrap")) and rule_requires_human_confirmation(
            machine_display,
            execution.get("execution_reason"),
            result_summary=execution.get("result_summary"),
        )
        if machine_display == "manual_review" and not confirmable and not execution.get("is_bootstrap"):
            # 旧记录可能只在摘要里保存模型故障，不能让历史人工结论覆盖检测异常。
            machine_display = "detect_error"
            display = "detect_error"
        return {
            "item_type": item_type,
            "execution_id": (execution_row or {}).get("execution_id"),
            "rule_id": rule_row["rule_id"],
            "rule_code": rule_row["rule_code"],
            "rule_name": rule_row["rule_name"],
            "category": rule_row.get("category"),
            "rule_package": rule_row.get("rule_package")
            or rule_package_fallback(rule_row.get("category")),
            "trigger_display": rule_row.get("trigger_display")
            or trigger_display_fallback(applicable),
            "bound_page_type": bound_page_type,
            "bound_page_type_name": page_type_display_name(bound_page_type),
            "applicable_page_types": applicable,
            "display_status": display,
            "display_status_label": rule_display_label(display),
            "machine_display_status": machine_display,
            "machine_display_status_label": rule_display_label(machine_display),
            "execution_status": (execution_row or {}).get("execution_status"),
            "judge_strategy": (execution_row or {}).get("judge_strategy"),
            "confidence": (execution_row or {}).get("confidence"),
            "severity": severity,
            "severity_display": severity_display(severity),
            "result_summary": humanize_qc_text((execution_row or {}).get("result_summary")),
            "problem_desc": humanize_qc_text((execution_row or {}).get("problem_desc")),
            "execution_reason": effective_rule_execution_reason(
                execution.get("execution_status"),
                execution.get("execution_reason"),
                error_code=execution.get("error_code"),
                judge_input_source=(_json_object(execution.get("judge_input")) or {}).get("source"),
            ),
            "capture_failure_code": (execution_row or {}).get("capture_failure_code"),
            "review_status": review_status,
            "human_verdict": (execution_row or {}).get("human_verdict"),
            "human_verdict_note": (execution_row or {}).get("human_verdict_note"),
            "human_verdict_at": (execution_row or {}).get("human_verdict_at"),
            "human_verdict_label": human_verdict_label((execution_row or {}).get("human_verdict")),
            "confirmable": confirmable,
            "confirm_status": (
                None
                if not confirmable
                else (
                    "confirmed"
                    if (execution_row or {}).get("human_verdict") is not None
                    else "pending"
                )
            ),
            "evidence_artifact_ids": (execution_row or {}).get("evidence_artifact_ids") or [],
            "page_capture_id": (execution_row or {}).get("page_capture_id"),
            "package_name": (execution_row or {}).get("capture_package_name"),
            "activity": (execution_row or {}).get("capture_activity"),
            "width": (execution_row or {}).get("capture_width"),
            "height": (execution_row or {}).get("capture_height"),
            "is_bootstrap": bool((execution_row or {}).get("is_bootstrap")),
            "finished_at": (execution_row or {}).get("finished_at"),
        }

    async def _related_rules_by_artifact(
        self, task_id: str, run_id: str | None
    ) -> dict[str, list[dict[str, Any]]]:
        execution_rows = await self._repository.list_rule_execution_rows(
            task_id=task_id, agent_run_id=run_id
        )
        related: dict[str, list[dict[str, Any]]] = {}
        for row in execution_rows:
            display = rule_final_display_status(
                row["execution_status"],
                row.get("execution_reason"),
                human_verdict=row.get("human_verdict"),
                is_bootstrap=bool(row.get("is_bootstrap")),
            )
            for artifact_id in row.get("evidence_artifact_ids") or []:
                related.setdefault(artifact_id, []).append(
                    {
                        "rule_code": row["rule_code"],
                        "rule_name": row["rule_name"],
                        "display_status": display,
                        "display_status_label": rule_display_label(display),
                    }
                )
        return related

    def _derive_nodes_from_journey(self, context: dict[str, Any]) -> list[dict[str, Any]]:
        """无节点行时（排队中/旧任务）按任务配置现场推导计划节点。"""
        steps = normalize_journey_steps(_journey_step_names(context))
        return [
            {
                "node_seq": index,
                "source": "journey",
                "step_name": step.step_name,
                "page_type": step.page_type,
                "status": "pending",
                "page_capture_id": None,
                "candidate_rule_total": None,
                "current_rule_code": None,
            }
            for index, step in enumerate(steps, start=1)
        ]

    def _resolve_current_node_seq(
        self, nodes: list[dict[str, Any]], task_running: bool
    ) -> int | None:
        if not task_running or not nodes:
            return None
        with_rule = [node for node in nodes if node.get("current_rule_code")]
        if with_rule:
            return max(node["node_seq"] for node in with_rule)
        visited = [node for node in nodes if node.get("page_capture_id")]
        if visited:
            return max(node["node_seq"] for node in visited)
        pending = [node for node in nodes if node.get("status") == "pending"]
        if pending:
            return min(node["node_seq"] for node in pending)
        return None

    def _node_display_status(self, status: str, *, is_current: bool) -> tuple[str, str]:
        if is_current and status in {"pending", "visiting", "recognized"}:
            return "running", PAGE_NODE_STATUS_LABELS["visiting"]
        if status == "visiting":
            return "running", PAGE_NODE_STATUS_LABELS["visiting"]
        return status, PAGE_NODE_STATUS_LABELS.get(status, status)

    async def _require_context(
        self,
        task_id: str,
        *,
        access_scope: AccessScope | None = None,
    ) -> dict[str, Any]:
        if access_scope is None:
            context = await self._repository.fetch_task_context(task_id)
        else:
            context = await self._repository.fetch_task_context(
                task_id,
                access_scope=access_scope,
            )
        if context is None:
            raise TaskInsightNotFoundError(f"未找到任务：{task_id}")
        return context

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


_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def _count_by(display_statuses: list[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for status in display_statuses:
        counts[status] = counts.get(status, 0) + 1
    return counts


# 补采请求走到这几个状态说明路线已经定下来并派出去了。
_ROUTE_PLANNED_REQUEST_STATUSES = frozenset({"routed", "running", "satisfied"})


def _recapture_route_plan(row: dict[str, Any]) -> dict[str, Any]:
    """这条缺口的补采路线规划到哪一步了；没规划出来要说清楚为什么。"""

    from services.api.app.application.evidence_recovery_route import explain_route_reason

    status = str(row.get("request_status") or "").strip()
    if not status:
        return {"state": "未提补采请求"}
    reason_code = str(row.get("request_resolution_reason") or "").strip()
    if status in _ROUTE_PLANNED_REQUEST_STATUSES:
        return {"state": "已规划出路线", "request_status": status}
    if not reason_code:
        return {"state": "还没选路", "request_status": status}
    explained = explain_route_reason(reason_code)
    return {"state": "没规划出路线", "request_status": status, **explained}


def _app_exploration_projection(value: Any) -> dict[str, Any] | None:
    """投影 App 探索闭环状态，区分业务结果与会话技术收尾失败。"""
    if not isinstance(value, dict):
        return None
    result = dict(value)
    outcome = result.get("outcome")
    session_status = result.get("surface_session_status")
    technical_failure = result.get("technical_failure")
    if session_status == "lost":
        status = "close_failed" if outcome == "exploration_completed" else "technical_failed"
    elif isinstance(technical_failure, dict) and outcome is None:
        status = "technical_failed"
    else:
        status = outcome
    result["status"] = status
    for key in ("started_at", "completed_at"):
        result[key] = _as_iso(result.get(key))
    return result


def _page_inspection_status_label(*, status: object, failure_code: object) -> str | None:
    if str(failure_code or "").strip() == "SUPPLEMENTAL_EVIDENCE_DISABLED":
        return "证据不足"
    return _PAGE_INSPECTION_STATUS_LABELS.get(str(status or ""))


def _timeline_attention_item(row: dict[str, Any]) -> dict[str, Any]:
    """把页面内待关注规则完整展开，保留业务结论与可诊断系统原因。"""
    display_status = rule_final_display_status(
        row.get("execution_status"),
        row.get("execution_reason"),
        human_verdict=row.get("human_verdict"),
        is_bootstrap=bool(row.get("is_bootstrap")),
    )
    diagnostic_code = _optional_text(row.get("diagnostic_code") or row.get("error_code"))
    return {
        "execution_id": row.get("execution_id"),
        "rule_code": row.get("rule_code"),
        "rule_name": row.get("rule_name"),
        "display_status": display_status,
        "display_status_label": rule_display_label(display_status),
        "result_summary": humanize_qc_text(_optional_text(row.get("result_summary"))),
        "problem_desc": humanize_qc_text(_optional_text(row.get("problem_desc"))),
        "review_reason": humanize_qc_text(_optional_text(row.get("review_reason"))),
        "error_message": humanize_qc_text(_optional_text(row.get("error_message"))),
        "diagnostic_code": diagnostic_code,
        "diagnostic_message": _attention_diagnostic_message(
            row,
            diagnostic_code=diagnostic_code,
        ),
    }


# 语义判定异常码后缀（Python 异常类名 casefold）→ 人可读说法；词表外走通用兜底。
_SEMANTIC_EXCEPTION_LABELS = {
    "timeout": "响应超时",
    "timeouterror": "响应超时",
    "timeoutexception": "响应超时",
    "apitimeouterror": "响应超时",
    "connecttimeout": "连接超时",
    "readtimeout": "读取超时",
    "writetimeout": "写入超时",
    "pooltimeout": "连接池超时",
    "connecterror": "连接失败",
    "connectionerror": "连接失败",
    "apiconnectionerror": "连接失败",
    "sslerror": "安全握手失败",
    "ratelimiterror": "请求限流",
    "cancellederror": "请求被取消",
}


def _attention_diagnostic_message(row: dict[str, Any], *, diagnostic_code: str | None) -> str:
    if diagnostic_code == "semantic_provider_temporarily_unavailable":
        return "模型 API 暂时不可用，自动判定未完成，已转人工复核。"
    if diagnostic_code and diagnostic_code.startswith("semantic_provider_http_"):
        status = diagnostic_code.removeprefix("semantic_provider_http_")
        label = {
            "401": "认证失败",
            "403": "访问被拒绝",
            "408": "请求超时",
            "429": "请求限流",
            "500": "服务内部错误",
            "502": "网关错误",
            "503": "服务暂时不可用",
            "504": "网关超时",
        }.get(status, "请求失败")
        return f"模型 API 返回 HTTP {status}（{label}），自动判定未完成。"
    if diagnostic_code and diagnostic_code.startswith("semantic_provider_"):
        exception_name = diagnostic_code.removeprefix("semantic_provider_")
        label = _SEMANTIC_EXCEPTION_LABELS.get(exception_name, f"{exception_name}，网络或服务异常")
        return f"模型 API 调用失败（{label}），自动判定未完成。"
    for key in (
        "error_message",
        "problem_desc",
        "review_reason",
        "result_summary",
    ):
        if value := _optional_text(row.get(key)):
            return humanize_qc_text(value) or value
    return "规则未形成可自动确认的结论，请查看执行日志与证据。"


def _optional_text(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _optional_nonnegative_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _h5_gallery_variant_key(artifact: Mapping[str, Any]) -> str | None:
    """按业务可见状态选代表图，忽略公告、倒计时等瞬时内容变化。"""

    logical_identity = _optional_text(artifact.get("logical_identity_fingerprint"))
    if logical_identity is None:
        return None
    signals = _json_object(artifact.get("logical_state_signals")) or {}
    return stable_protocol_hash(
        {
            "logical_identity": logical_identity,
            "page_type": _optional_text(artifact.get("page_type")),
            "page_state": _optional_text(artifact.get("page_state")),
            "selected_tab": _optional_text(signals.get("selected_tab")),
        }
    )


def _is_inactive_capture_node(node: dict[str, Any]) -> bool:
    """替代或放弃采集只保留审计，不推导页面候选规则。"""
    return node.get("inspection_lifecycle_status") in {
        "superseded",
        "abandoned",
        "evidence_only",
    }


def _is_internal_transition_node(
    node: dict[str, Any], node_executions: list[dict[str, Any]]
) -> bool:
    """主时间线隐藏无规则结果的清障与恢复快照，失败节点仍保留诊断入口。"""
    if node_executions or not _is_inactive_capture_node(node):
        return False
    if node.get("candidate_rule_total") not in {None, 0}:
        return False
    return not (
        node.get("inspection_status") == "failed"
        or node.get("inspection_failure_code")
        or node.get("inspection_failure_message")
    )


def _is_nested_detail_return_revisit(node: dict[str, Any]) -> bool:
    """详情返回后的列表复访是导航审计，不重复展示为独立质检页面。"""
    return (
        node.get("inspection_lifecycle_status") == "abandoned"
        and node.get("inspection_abandon_reason") == "nested_detail_return_revisit"
    )


def _journey_step_names(context: dict[str, Any]) -> list[str]:
    raw = (context.get("task_config") or {}).get("journey_steps")
    if not isinstance(raw, list):
        return []
    return [str(item) for item in raw if str(item).strip()]


def _as_iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.isoformat()
    text = str(value).strip()
    return text or None


def _is_h5_platform_context(context: dict[str, Any]) -> bool:
    """仅将正式 H5/Web 平台且锁定 V2 的任务交给 H5 V2 读模型。"""
    target_platform = _optional_text(context.get("target_platform"))
    if target_platform is not None:
        config = context.get("task_config")
        workflow_version = _optional_text(
            config.get("workflow_version") if isinstance(config, dict) else None
        )
        return (
            target_platform.casefold() in {"h5", "web"}
            and workflow_version is not None
            and workflow_version.casefold() == "v2"
        )
    return False


def _is_web_v2_platform_context(context: dict[str, Any]) -> bool:
    """判断是否为独立 Web V2；读模型不额外依赖执行模式或浏览器配置。"""

    if (_optional_text(context.get("target_platform")) or "").casefold() != "web":
        return False
    config = context.get("task_config")
    return isinstance(config, dict) and (
        (_optional_text(config.get("workflow_version")) or "").casefold() == "v2"
    )


def _is_web_v2_evidence_context(context: dict[str, Any]) -> bool:
    """只接受具备冻结浏览器配置的独立 Web V2 任务。"""

    if (_optional_text(context.get("target_platform")) or "").casefold() != "web":
        return False
    config = context.get("task_config")
    if not isinstance(config, dict):
        return False
    if (_optional_text(config.get("workflow_version")) or "").casefold() != "v2":
        return False
    execution_mode = (
        _optional_text(config.get("execution_mode")) or ""
    ).casefold()
    if execution_mode not in {"exploration_only", "full_inspection"}:
        return False
    target = config.get("_task_target")
    if isinstance(target, dict):
        if (_optional_text(target.get("platform")) or "").casefold() != "web":
            return False
        web = target.get("web")
        return isinstance(web, dict) and bool(
            _optional_text(web.get("browser_profile_id"))
        )
    if execution_mode != "exploration_only":
        return False
    exploration_target = config.get("web_exploration_target")
    return isinstance(exploration_target, dict) and bool(
        _optional_text(exploration_target.get("browser_profile_id"))
    )


def _legacy_app_platform_fallback(context: dict[str, Any]) -> bool:
    """仅用于 target_platform 为 NULL 的历史任务平台兼容。"""
    config = context.get("task_config") or {}
    product = config.get("product") if isinstance(config.get("product"), dict) else {}
    product_code = _optional_text(product.get("product_code") or config.get("product_code"))
    target = config.get("_task_target") if isinstance(config.get("_task_target"), dict) else {}
    legacy_platform = _optional_text(target.get("platform"))
    return bool(
        (legacy_platform is not None and legacy_platform.casefold() == "app")
        or (product_code is not None and product_code.casefold() == "app-qc")
        or not _optional_text(context.get("target_url"))
    )


def _h5_capture_node(row: dict[str, Any]) -> dict[str, Any]:
    """把正式 H5 Capture 投影为工作台既有节点展示合同。"""
    return {
        "node_seq": row.get("page_seq") or 0,
        "source": "h5_v2_capture",
        "step_name": row.get("page_type"),
        "page_role": row.get("page_role"),
        "page_type": row.get("page_type"),
        "page_state": row.get("page_state"),
        "status": "recognized" if row.get("detection_status") == "recognized" else "captured",
        "page_capture_id": row.get("page_capture_id"),
        "capture_role": row.get("capture_role"),
        "candidate_rule_total": None,
        "current_rule_code": None,
        "first_visited_at": row.get("captured_at"),
        "finished_at": row.get("captured_at"),
        "url": row.get("url"),
        "captured_at": row.get("captured_at"),
        "navigation_retry": row.get("navigation_retry"),
        "page_settle_diagnostics": row.get("page_settle_diagnostics"),
        "detection_status": row.get("detection_status"),
        "detection_confidence": row.get("detection_confidence"),
        "detection_confidence_source": None,
        "detection_model_page_type": None,
        "detection_model_confidence": None,
        "detection_duration_ms": row.get("detection_duration_ms"),
        "element_count": row.get("element_count"),
        "parent_page_capture_id": row.get("parent_page_capture_id"),
        "inspection_lifecycle_status": row.get("inspection_lifecycle_status"),
        "inspection_status": row.get("inspection_status"),
        "inspection_failure_stage": row.get("inspection_failure_stage"),
        "inspection_failure_code": row.get("inspection_failure_code"),
        "inspection_failure_message": row.get("inspection_failure_message"),
        "inspection_failure_retryable": row.get("inspection_failure_retryable"),
        "inspection_abandon_reason": row.get("inspection_abandon_reason"),
        "screenshot_artifact_id": row.get("screenshot_artifact_id"),
        "capture_purpose": row.get("capture_purpose"),
        "menu_entry_id": row.get("menu_entry_id"),
        "menu_entry_ids": row.get("menu_entry_ids"),
    }


def _web_logical_page_nodes(
    nodes: list[dict[str, Any]],
    *,
    assessments: list[dict[str, Any]],
    trusted_identities=None,
) -> list[dict[str, Any]]:
    """把同一可信身份的 Web Capture 聚合为一个工作台逻辑页面节点。"""

    # 首导航的空白审计节点没有页面内容，不进入 Web 页面路线或截图展示。
    nodes = [
        node for node in nodes
        if str(node.get("url") or "").partition("#")[0].casefold() != "about:blank"
    ]
    logical_pages = build_web_logical_pages(nodes, trusted_identities=trusted_identities)
    nodes_by_capture_id = {
        capture_id: node
        for node in nodes
        if (capture_id := _optional_text(node.get("page_capture_id"))) is not None
    }
    assessment_by_capture_id = {
        capture_id: assessment
        for assessment in assessments
        if (capture_id := _optional_text(assessment.get("page_capture_id"))) is not None
    }
    consumed_capture_ids: set[str] = set()
    result: list[dict[str, Any]] = []
    for logical_page in logical_pages:
        primary = nodes_by_capture_id.get(logical_page.primary_capture_id)
        if primary is None:
            continue
        versions = [
            nodes_by_capture_id[capture_id]
            for capture_id in logical_page.capture_ids
            if capture_id in nodes_by_capture_id
        ]
        consumed_capture_ids.update(logical_page.capture_ids)
        item = dict(primary)
        assessment = assessment_by_capture_id.get(logical_page.primary_capture_id, {})
        if not assessment:
            assessment = next(
                (
                    assessment_by_capture_id[capture_id]
                    for capture_id in reversed(logical_page.capture_ids)
                    if capture_id in assessment_by_capture_id
                ),
                {},
            )
        classified_version = next(
            (version for version in reversed(versions) if version.get("page_type")),
            primary,
        )
        inspected_version = next(
            (
                version
                for version in reversed(versions)
                if version.get("inspection_status") is not None
            ),
            primary,
        )
        for field_name in (
            "page_type",
            "detection_status",
            "detection_confidence",
            "detection_confidence_source",
            "detection_model_page_type",
            "detection_model_confidence",
            "detection_duration_ms",
        ):
            if item.get(field_name) is None:
                item[field_name] = classified_version.get(field_name)
        for field_name in (
            "inspection_lifecycle_status",
            "inspection_status",
            "inspection_failure_stage",
            "inspection_failure_code",
            "inspection_failure_message",
            "inspection_failure_retryable",
            "inspection_abandon_reason",
        ):
            if item.get(field_name) is None:
                item[field_name] = inspected_version.get(field_name)
        item.update(
            node_seq=min(int(version.get("node_seq") or 0) for version in versions),
            logical_page_id=logical_page.logical_page_id,
            canonical_url=web_logical_page_display_url(logical_page.canonical_url),
            url=web_logical_page_display_url(logical_page.canonical_url),
            capture_ids=logical_page.capture_ids,
            capture_count=len(logical_page.capture_ids),
            primary_capture_id=logical_page.primary_capture_id,
            menu_entry_ids=logical_page.menu_entry_ids,
            capture_versions=[_web_capture_version(version) for version in versions],
            classification_status=assessment.get("classification_status"),
            raw_page_type=assessment.get("raw_page_type"),
            proposed_page_type=assessment.get("proposed_page_type"),
            observed_page_type=assessment.get("observed_page_type"),
            expected_page_type=assessment.get("expected_page_type"),
            conflict_reason_code=assessment.get("conflict_reason_code"),
            diagnostic_message=assessment.get("diagnostic_message"),
            classification_confidence=assessment.get("confidence"),
            classification_review_reason=(
                assessment.get("conflict_reason_code")
                or assessment.get("reason_code")
            ),
            page_role="home" if logical_page.home_role_verified else resolve_web_logical_page_role(
                canonical_url=logical_page.canonical_url,
                menu_entry_ids=logical_page.menu_entry_ids,
                asserted_roles=(
                    *(
                        version.get("page_role")
                        for version in versions
                    ),
                    *(
                        assessment_by_capture_id.get(capture_id, {}).get("page_role")
                        for capture_id in logical_page.capture_ids
                    ),
                ),
            ),
        )
        result.append(item)

    for node in nodes:
        capture_id = _optional_text(node.get("page_capture_id"))
        if capture_id is not None and capture_id in consumed_capture_ids:
            continue
        item = dict(node)
        if trusted_identities is not None:
            item.pop("logical_page_id", None)
            item.pop("canonical_url", None)
            item["classification_review_reason"] = trusted_identities.get(capture_id, {}).get(
                "logical_page_identity_status", "WEB_LOGICAL_ID_UNTRUSTED",
            )
        item["capture_ids"] = (capture_id,) if capture_id is not None else ()
        item["capture_count"] = len(item["capture_ids"])
        item["primary_capture_id"] = capture_id
        item["capture_versions"] = [_web_capture_version(item)] if capture_id else []
        result.append(item)
    return sorted(result, key=lambda item: int(item.get("node_seq") or 0))


def _web_capture_version(node: dict[str, Any]) -> dict[str, Any]:
    """输出工作台可展示的最小 Capture 历史，不复制大块证据内容。"""

    return {
        "page_capture_id": node.get("page_capture_id"),
        "capture_purpose": node.get("capture_purpose"),
        "captured_at": node.get("captured_at"),
        "page_type": node.get("page_type"),
        "inspection_status": node.get("inspection_status"),
        "screenshot_artifact_id": node.get("screenshot_artifact_id"),
    }


def _h5_page_rows_with_assessment_diagnostics(
    page_rows: list[dict[str, Any]],
    assessments: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    assessments_by_capture = {
        capture_id: assessment
        for assessment in assessments
        if (capture_id := _optional_text(assessment.get("page_capture_id"))) is not None
    }
    result: list[dict[str, Any]] = []
    for row in page_rows:
        item = dict(row)
        assessment = assessments_by_capture.get(_optional_text(item.get("page_capture_id")) or "")
        if assessment is not None:
            detection = assessment.get("detection_result")
            detection = detection if isinstance(detection, dict) else {}
            logical_surface = detection.get("logical_surface")
            if (
                not isinstance(item.get("logical_surface"), dict)
                and isinstance(logical_surface, dict)
            ):
                item["logical_surface"] = dict(logical_surface)
            if item.get("confidence_source") is None:
                item["confidence_source"] = detection.get("confidence_source")
        if assessment is not None and _h5_assessment_marks_safety_blocked(assessment):
            detection = assessment.get("detection_result")
            detection = detection if isinstance(detection, dict) else {}
            item["page_state"] = (
                _optional_text(detection.get("page_state"))
                or _optional_text(assessment.get("page_state"))
                or item.get("page_state")
            )
            item["detection_status"] = (
                _optional_text(detection.get("detection_status"))
                or _optional_text(assessment.get("detection_status"))
                or item.get("detection_status")
            )
            item["page_detection"] = {
                "detection_status": item.get("detection_status"),
                "page_type": detection.get("page_type"),
                "page_state": item.get("page_state"),
                "risk_flags": detection.get("risk_flags") or [],
                "conflicts": detection.get("conflicts") or [],
                "reason": detection.get("reason"),
                "rule_matching_allowed": detection.get("rule_matching_allowed"),
            }
        result.append(item)
    return result


def _h5_assessment_marks_safety_blocked(assessment: dict[str, Any]) -> bool:
    detection = assessment.get("detection_result")
    detection = detection if isinstance(detection, dict) else {}
    reason_codes = assessment.get("reason_codes")
    reasons = [str(item) for item in reason_codes] if isinstance(reason_codes, list) else []
    risk_flags = detection.get("risk_flags")
    risk_flags = [str(item) for item in risk_flags] if isinstance(risk_flags, list) else []
    conflicts = detection.get("conflicts")
    conflicts = [str(item) for item in conflicts] if isinstance(conflicts, list) else []
    return bool(
        assessment.get("status") == "blocked_by_safety"
        or "PAGE_TYPE_ASSESSMENT_BLOCKED_BY_SAFETY" in reasons
        or "safety_interstitial" in {flag.casefold() for flag in risk_flags}
        or "target_blocked_by_safety_interstitial" in {
            conflict.casefold() for conflict in conflicts
        }
    )


def _h5_browser_recovery_projection(
    work_items: list[dict[str, Any]],
) -> dict[str, Any]:
    """将 Work 内恢复事实投影为任务级、去重且可验收的审计摘要。"""
    attempts: list[dict[str, Any]] = []
    recoveries: list[dict[str, Any]] = []
    seen_attempts: set[str] = set()
    seen_recoveries: set[str] = set()

    for work in work_items:
        attempt_groups = (
            work.get("browser_recovery_parent_attempt_audit"),
            work.get("browser_recovery_attempt_history"),
            (work.get("browser_recovery_attempt"),),
        )
        for group in attempt_groups:
            if not isinstance(group, (list, tuple)):
                continue
            for value in group:
                if not isinstance(value, dict) or not value:
                    continue
                identity = _browser_recovery_fact_identity(
                    value,
                    fallback=(
                        value.get("failed_work_id"),
                        value.get("recovery_attempt_count"),
                        value.get("failure_stage"),
                    ),
                )
                if identity in seen_attempts:
                    continue
                seen_attempts.add(identity)
                attempts.append(dict(value))

        recovery = work.get("browser_recovery")
        if isinstance(recovery, dict) and recovery:
            identity = _browser_recovery_fact_identity(
                recovery,
                fallback=(
                    recovery.get("failed_work_id"),
                    recovery.get("recovery_attempt_count"),
                    recovery.get("target_session_id"),
                ),
            )
            if identity not in seen_recoveries:
                seen_recoveries.add(identity)
                recoveries.append(dict(recovery))

    attempts.sort(
        key=lambda item: (
            str(item.get("started_at") or ""),
            _safe_int(item.get("recovery_attempt_count")),
        )
    )
    completed = [item for item in attempts if item.get("status") == "completed"]
    in_progress = [item for item in attempts if item.get("status") == "started"]
    successful = [item for item in completed if item.get("reason_code") is None]
    failed = [item for item in completed if item.get("reason_code") is not None]
    equivalent = [
        item
        for item in recoveries
        if item.get("equivalent") is True and item.get("reason_code") is None
    ]
    recovered_work_ids = {
        str(item.get("failed_work_id"))
        for item in equivalent
        if item.get("failed_work_id")
    }
    unresolved_attempts = [
        item
        for item in completed
        if item.get("failed_work_id")
        and str(item.get("failed_work_id")) not in recovered_work_ids
    ]
    if in_progress:
        status = "recovering"
    elif attempts and (unresolved_attempts or not equivalent):
        status = "failed"
    elif equivalent:
        status = "recovered"
    else:
        status = "not_needed"
    return {
        "status": status,
        "attempt_count": len(attempts),
        "completed_attempt_count": len(completed),
        "successful_attempt_count": len(successful),
        "failed_attempt_count": len(failed),
        "in_progress_attempt_count": len(in_progress),
        "recovery_count": len(recoveries),
        "equivalent_recovery_count": len(equivalent),
        "attempts": attempts,
        "recoveries": recoveries,
    }


_H5_MODEL_CONTRACT_ERROR_CODES = frozenset(
    {
        "H5_EXPLORER_OUTPUT_INVALID",
        "H5_EXPLORER_PREMATURE_TERMINAL",
        "H5_EXPLORER_MODEL_UNAVAILABLE",
    }
)


def _h5_model_contract_projection(
    *,
    work_items: list[dict[str, Any]],
    actions: list[dict[str, Any]],
) -> dict[str, Any]:
    """汇总模型合同失败后的降级动作与未解决 Work，不暴露模型原文。"""
    fallback_actions: list[dict[str, Any]] = []
    reason_codes: set[str] = set()
    for action in actions:
        parameters = action.get("parameters")
        if not isinstance(parameters, dict):
            continue
        fallback = parameters.get("explorer_decision_fallback")
        if (
            not isinstance(fallback, dict)
            or fallback.get("schema_version") != "h5-explorer-decision-fallback.v1"
        ):
            continue
        reason_code = _optional_text(fallback.get("reason_code"))
        if reason_code not in _H5_MODEL_CONTRACT_ERROR_CODES:
            continue
        reason_codes.add(reason_code)
        fallback_actions.append(
            {
                "action_execution_id": action.get("action_execution_id"),
                "execution_status": action.get("execution_status"),
                "source_page_capture_id": action.get("source_page_capture_id"),
                "result_page_capture_id": action.get("result_page_capture_id"),
                "reason_code": reason_code,
            }
        )

    error_work_items: list[dict[str, Any]] = []
    for work in work_items:
        error_code = _optional_text(work.get("last_error_code"))
        status = _optional_text(work.get("status"))
        if error_code not in _H5_MODEL_CONTRACT_ERROR_CODES or status not in {
            "retry_wait",
            "failed",
            "bounded",
            "budget_exhausted",
            "blocked",
            "no_progress",
        }:
            continue
        reason_codes.add(error_code)
        error_work_items.append(
            {
                "work_id": work.get("work_id"),
                "status": status,
                "error_code": error_code,
            }
        )

    if any(item["status"] == "retry_wait" for item in error_work_items):
        status = "retrying"
    elif error_work_items:
        status = "failed"
    elif fallback_actions:
        status = "degraded"
    else:
        status = "not_observed"
    return {
        "schema_version": "h5-model-contract-audit.v1",
        "status": status,
        "fallback_action_count": len(fallback_actions),
        "error_work_count": len(error_work_items),
        "reason_codes": sorted(reason_codes),
        "fallback_actions": fallback_actions,
        "error_work_items": error_work_items,
    }


def _browser_recovery_fact_identity(
    value: dict[str, Any],
    *,
    fallback: tuple[object, ...],
) -> str:
    payload_hash = _optional_text(value.get("payload_hash"))
    if payload_hash:
        return payload_hash
    return "|".join(str(item or "") for item in fallback)


def _h5_visual_navigation_mode(work_items: list[dict[str, Any]]) -> str | None:
    """只消费 Work 元数据中已解析的有效模式，不从候选或执行结果反推。"""
    for work in reversed(work_items):
        mode = _optional_text(work.get("h5_visual_coordinate_mode"))
        if mode in {"shadow", "enforced"}:
            return mode
    return None


def _h5_inventory_projection(items: list[dict[str, Any]]) -> dict[str, Any]:
    statuses = [str(item.get("status") or "unknown") for item in items]
    return {
        "item_count": len(items),
        "pass_count": statuses.count("pass"),
        "insufficient_count": statuses.count("insufficient"),
        "failed_count": statuses.count("failed"),
        "items": items,
    }


def _safe_int(value: Any) -> int:
    """把正式读模型计数收敛为非负整数。"""
    if isinstance(value, bool):
        return 0
    try:
        return max(int(value or 0), 0)
    except (TypeError, ValueError):
        return 0


def _legacy_h5_coverage_projection(
    *,
    frontier: dict[str, Any] | None,
    assessments: list[dict[str, Any]],
) -> dict[str, Any]:
    """历史任务显式标注为 legacy；读侧可展示，但新完成门禁不会采用。"""
    ready = _h5_assessment_scope_ready(frontier=frontier, assessments=assessments)
    business_page_types = frozenset(
        get_default_page_type_registry().coverage_required_codes
    )
    covered = sorted(
        {
            str(item.get("page_type"))
            for item in assessments
            if (
                item.get("status") == "covered"
                and item.get("page_type") in business_page_types
            )
        }
    )
    required = covered
    return {
        "revision": None,
        "authority_source": "legacy_frontier_derived",
        "required_page_types": required,
        "covered_page_types": covered,
        "missing_page_types": sorted(set(required) - set(covered)),
        "scopes": [],
        "assessment_scope_ready": ready,
        "frontier_terminal": False,
        "completion_ready": ready,
        "schema_version": "legacy",
        "payload_hash": None,
    }


def _h5_assessment_scope_ready(
    *, frontier: dict[str, Any] | None, assessments: list[dict[str, Any]]
) -> bool:
    """只用 Frontier 明示范围和正式 Assessment 身份证明评估范围闭合。"""
    snapshot = frontier or {}
    expected_page_types: set[str] = set()
    covered = snapshot.get("covered_page_types")
    if isinstance(covered, list | tuple | set):
        expected_page_types.update(text for item in covered if (text := _optional_text(item)))
    relevant_assessments = [
        item for item in assessments if _optional_text(item.get("page_type")) in expected_page_types
    ]
    assessed_page_types = {
        text for item in relevant_assessments if (text := _optional_text(item.get("page_type")))
    }
    catalog_hashes = [_optional_text(item.get("catalog_sha256")) for item in relevant_assessments]
    return bool(
        expected_page_types
        and expected_page_types.issubset(assessed_page_types)
        and catalog_hashes
        and all(catalog_hashes)
        and len(set(catalog_hashes)) == 1
    )


_PRESENTATION_COMPACT_KEYS = (
    "protocol_version",
    "topmost_layer_kind",
    "base_page_visibility",
    "blocking_scope",
    "mutation_mode",
    "overlay_semantic_kind",
    "hit_test_status",
)


_STARTUP_OCCLUSION_STATES = frozenset({"startup_ad", "countdown", "splash"})
_OVERLAY_OCCLUSION_STATES = frozenset(
    {"popup", "modal", "overlay", "dialog", "system_dialog"}
)


def _screenshot_occlusion_kind(artifact: Mapping[str, Any]) -> str:
    page_type = str(artifact.get("page_type") or "").strip().casefold()
    page_state = str(artifact.get("page_state") or "").strip().casefold()
    if page_type in _STARTUP_OCCLUSION_STATES or page_state in _STARTUP_OCCLUSION_STATES:
        return "startup"
    if page_state in _OVERLAY_OCCLUSION_STATES:
        return "overlay"
    return "stable"


def _related_screenshot_frame(item: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "artifact_id": item["artifact_id"],
        "page_capture_id": item.get("page_capture_id"),
        "captured_at": item.get("captured_at"),
        "capture_role": item.get("capture_role"),
        "page_state": item.get("page_state"),
        "duplicate_count": int(item.get("duplicate_count") or 1),
    }


def _collapse_screenshot_occlusion_clusters(
    items: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """同一业务页：开屏／弹层／同 page_type 稳定页各只留 1 张代表图，其余进 related_frames。"""

    if len(items) <= 1:
        return items
    buckets: dict[str, list[dict[str, Any]]] = {}
    order: list[str] = []
    for item in items:
        kind = str(item.get("occlusion_kind") or "stable")
        if kind in {"startup", "overlay"}:
            key = kind
        else:
            # 稳定业务页：同 page_type 只展一张，避免同首页网格刷屏。
            page_type = str(item.get("page_type") or "unknown").strip().casefold() or "unknown"
            key = f"stable:{page_type}"
        if key not in buckets:
            order.append(key)
            buckets[key] = []
        buckets[key].append(item)
    collapsed: list[dict[str, Any]] = []
    for key in order:
        cluster = buckets[key]
        head = dict(cluster[0])
        head["related_frames"] = [_related_screenshot_frame(extra) for extra in cluster[1:]]
        collapsed.append(head)
    return collapsed


def _cap_screenshot_representatives(
    items: list[dict[str, Any]],
    *,
    max_items: int = 2,
) -> list[dict[str, Any]]:
    """组内代表图最多 max_items 张；超出并入第一张 related_frames。"""

    if max_items < 1 or len(items) <= max_items:
        return items
    kept = [dict(item) for item in items[:max_items]]
    overflow_frames: list[dict[str, Any]] = []
    for extra in items[max_items:]:
        overflow_frames.append(_related_screenshot_frame(extra))
        for frame in extra.get("related_frames") or []:
            if isinstance(frame, Mapping):
                overflow_frames.append(dict(frame))
    head_related = list(kept[0].get("related_frames") or [])
    head_related.extend(overflow_frames)
    kept[0]["related_frames"] = head_related
    return kept


def _json_object(value: Any) -> dict[str, Any] | None:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return None
    return value if isinstance(value, dict) else None


def _compact_capture_presentation(value: Any) -> dict[str, Any] | None:
    """采集元数据里持久化的展示层评估 → 工作台只需要的几个判断字段。"""
    payload = _json_object(value)
    if not payload:
        return None
    compact = {
        key: payload.get(key) for key in _PRESENTATION_COMPACT_KEYS if payload.get(key) is not None
    }
    operations = payload.get("diagnostic_operations")
    if isinstance(operations, list):
        compact["diagnostic_operations"] = [
            {"kind": str(item.get("kind") or ""), "source": str(item.get("source") or "")}
            for item in operations[:10]
            if isinstance(item, dict) and item.get("kind")
        ]
    uncertainty = payload.get("uncertainty")
    if isinstance(uncertainty, list):
        compact["uncertainty"] = [str(item) for item in uncertainty[:10] if str(item).strip()]
    return compact or None


def _compact_settle_diagnostics(value: Any) -> dict[str, Any] | None:
    """采集管线的页面稳定性判定：结论与阻塞/动态原因，供截图质量展示。"""
    payload = _json_object(value)
    if not payload:
        return None
    return {
        "outcome": _optional_text(payload.get("outcome")),
        "blocking_reasons": [
            str(item) for item in (payload.get("blocking_reasons") or [])[:10] if str(item).strip()
        ]
        if isinstance(payload.get("blocking_reasons"), list)
        else [],
        "dynamic_reasons": [
            str(item) for item in (payload.get("dynamic_reasons") or [])[:10] if str(item).strip()
        ]
        if isinstance(payload.get("dynamic_reasons"), list)
        else [],
    }


def _merge_related_rules(
    current: list[dict[str, Any]],
    additional: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """合并同一 H5 逻辑状态重复 Capture 上绑定的规则引用。"""

    merged: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for item in (*current, *additional):
        if not isinstance(item, dict):
            continue
        identity = (
            str(item.get("execution_id") or ""),
            str(item.get("rule_code") or ""),
            str(item.get("execution_status") or ""),
        )
        if identity in seen:
            continue
        seen.add(identity)
        merged.append(item)
    return merged


def _closure_block_reason_codes(value: Any) -> list[str]:
    """门禁快照里的阻断原因可能是 jsonb 文本，统一成字符串列表。"""

    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return []
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item).strip()]
