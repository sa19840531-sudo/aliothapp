"""平台质检能力进展 Excel 导出。

该报表衡量的是平台在单次任务中触达、执行和补强了哪些能力，不计算产品通过率。
工作簿使用最小 OOXML 结构生成，避免为只读导出引入额外运行时依赖。
"""

from __future__ import annotations

import io
import re
import zipfile
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, timezone
from typing import Any
from xml.sax.saxutils import escape

from services.api.app.application.task_progress import (
    page_type_display_name,
    rule_package_fallback,
    severity_display,
    trigger_display_fallback,
)

_FINAL_RULE_STATUSES = {"passed", "failed", "not_detected", "not_applicable"}
_STRENGTHEN_RULE_STATUSES = {
    "detect_error",
    "evidence_insufficient",
    "manual_review",
}
_UNREACHED_RULE_STATUSES = {"coverage_gap", "not_executed", "pending"}
_TASK_TERMINATION_LABELS = {
    "max_steps_reached": "运行步数达到上限（MAX_STEPS_REACHED）",
    "continuation_limit_reached": "连续续跑达到上限（CONTINUATION_LIMIT_REACHED）",
    "run_timeout": "运行超时（RUN_TIMEOUT）",
    "stuck_detected": "检测到重复停滞（STUCK_DETECTED）",
}
_INVALID_XML_CHARS = re.compile(
    "[\x00-\x08\x0B\x0C\x0E-\x1F\uD800-\uDFFF\uFFFE\uFFFF]"
)
_REPORT_TIMEZONE = timezone(timedelta(hours=8), name="Asia/Shanghai")

_STYLE_NORMAL = 0
_STYLE_TITLE = 1
_STYLE_SUBTITLE = 2
_STYLE_SECTION = 3
_STYLE_HEADER = 4
_STYLE_BODY = 5
_STYLE_BODY_WRAP = 6
_STYLE_INTEGER = 7
_STYLE_PERCENT = 8
_STYLE_COMPLETE = 9
_STYLE_STRENGTHEN = 10
_STYLE_RUNNING = 11
_STYLE_UNREACHED = 12
_STYLE_META_LABEL = 13
_STYLE_DATETIME = 14
_STYLE_FAILED = 15


@dataclass(frozen=True)
class _Cell:
    value: Any = None
    style: int = _STYLE_BODY


@dataclass(frozen=True)
class _SheetSpec:
    name: str
    rows: list[list[_Cell]]
    widths: list[float]
    freeze_rows: int = 0
    auto_filter: str | None = None
    merges: list[str] = field(default_factory=list)
    row_heights: dict[int, float] = field(default_factory=dict)


def build_platform_progress_snapshot(
    *,
    context: dict[str, Any],
    all_rules: list[dict[str, Any]],
    rule_items: list[dict[str, Any]],
    timeline: dict[str, Any],
    generated_at: datetime | None = None,
) -> dict[str, Any]:
    """把任务读模型归一化为平台能力进展快照。"""
    generated_at = generated_at or datetime.now(UTC)
    task_termination = _task_termination_reason(context, timeline)
    rule_rows = _build_rule_rows(
        all_rules,
        rule_items,
        timeline,
        task_termination=task_termination,
    )
    page_rows = _build_page_rows(rule_items, timeline)
    rule_counts = _count_values(rule["platform_progress"] for rule in rule_rows)
    exploration_summary = timeline.get("exploration_summary") or {}
    actual_pages = [page for page in page_rows if page.get("page_capture_id")]
    inspected_pages = [
        page
        for page in actual_pages
        if page["platform_progress"] in {"质检完成", "完成待补强", "质检失败"}
    ]
    strengthened_pages = [
        page
        for page in actual_pages
        if page["platform_progress"] in {"完成待补强", "质检失败"}
    ]
    total_rules = len(rule_rows)
    unreached_rules = rule_counts.get("未触达", 0)
    reached_rules = total_rules - unreached_rules
    gaps = _build_gap_rows(rule_rows, page_rows, timeline.get("exploration_actions") or [])
    task_config = context.get("task_config")
    task_config = task_config if isinstance(task_config, dict) else {}
    return {
        "meta": {
            "task_id": str(context.get("task_id") or ""),
            "task_name": str(
                task_config.get("task_name")
                or context.get("task_name")
                or context.get("target_url")
                or context.get("task_id")
                or ""
            ),
            "target_url": str(context.get("target_url") or ""),
            "task_status": str(context.get("status") or ""),
            "run_id": str(context.get("run_id") or ""),
            "run_status": str(context.get("run_status") or ""),
            "ruleset_code": str(context.get("ruleset_code") or ""),
            "ruleset_version": context.get("ruleset_version"),
            "generated_at": generated_at,
        },
        "metrics": {
            "total_rules": total_rules,
            "reached_rules": reached_rules,
            "completed_rules": rule_counts.get("已完成验证", 0),
            "strengthen_rules": rule_counts.get("已触达待补强", 0),
            "running_rules": rule_counts.get("执行中", 0),
            "unreached_rules": unreached_rules,
            "rule_reach_rate": reached_rules / total_rules if total_rules else 0,
            "actual_pages": len(actual_pages),
            "inspected_pages": len(inspected_pages),
            "completed_pages": sum(
                page["platform_progress"] == "质检完成" for page in actual_pages
            ),
            "strengthen_pages": len(strengthened_pages),
            "exploration_actions": int(exploration_summary.get("action_total") or 0),
            "exploration_gaps": int(exploration_summary.get("inactive_action_count") or 0),
        },
        "rules": rule_rows,
        "pages": page_rows,
        "gaps": gaps,
    }


def build_platform_progress_workbook(snapshot: dict[str, Any]) -> bytes:
    """生成可直接下载的 XLSX 字节。"""
    sheets = [
        _summary_sheet(snapshot),
        _rules_sheet(snapshot["rules"]),
        _pages_sheet(snapshot["pages"]),
        _gaps_sheet(snapshot["gaps"]),
    ]
    return _package_workbook(sheets, generated_at=snapshot["meta"]["generated_at"])


def _build_rule_rows(
    all_rules: list[dict[str, Any]],
    rule_items: list[dict[str, Any]],
    timeline: dict[str, Any],
    *,
    task_termination: str,
) -> list[dict[str, Any]]:
    items_by_rule: dict[str, list[dict[str, Any]]] = {}
    for item in rule_items:
        rule_id = str(item.get("rule_id") or "")
        if rule_id:
            items_by_rule.setdefault(rule_id, []).append(item)

    visited_page_types = {
        str(result["page_type"])
        for result in (timeline.get("exploration_summary") or {}).get("page_results") or []
        if result.get("page_type") and result.get("page_capture_id")
    }
    result = []
    for index, rule in enumerate(all_rules, start=1):
        rule_id = str(rule.get("rule_id") or "")
        items = items_by_rule.get(rule_id, [])
        execution_items = [item for item in items if item.get("item_type") == "execution"]
        statuses = {str(item.get("display_status") or "") for item in items}
        platform_progress = _rule_platform_progress(statuses, execution_items)
        effective_items = [
            item
            for item in execution_items
            if item.get("display_status") not in _UNREACHED_RULE_STATUSES
        ]
        evidence_ids = {
            str(artifact_id)
            for item in execution_items
            for artifact_id in item.get("evidence_artifact_ids") or []
            if artifact_id
        }
        touched_pages = sorted(
            {
                str(item.get("bound_page_type_name") or item.get("bound_page_type"))
                for item in effective_items
                if item.get("bound_page_type_name") or item.get("bound_page_type")
            }
        )
        finished_values = [
            value
            for item in execution_items
            if (value := _as_datetime(item.get("finished_at"))) is not None
        ]
        applicable_page_types = [
            str(value) for value in rule.get("applicable_page_types") or [] if value
        ]
        is_triggered = platform_progress != "未触达"
        reason = _rule_progress_reason(
            platform_progress=platform_progress,
            items=items,
            applicable_page_types=applicable_page_types,
            visited_page_types=visited_page_types,
            task_termination=task_termination,
        )
        result.append(
            {
                "index": index,
                "rule_id": rule_id,
                "rule_code": str(rule.get("rule_code") or ""),
                "rule_name": str(rule.get("rule_name") or ""),
                "rule_package": str(
                    rule.get("rule_package") or rule_package_fallback(rule.get("category"))
                ),
                "severity": severity_display(rule.get("severity")) or str(
                    rule.get("severity") or ""
                ),
                "applicable_page_types": "、".join(
                    page_type_display_name(page_type) or page_type
                    for page_type in applicable_page_types
                ),
                "trigger_display": str(
                    rule.get("trigger_display")
                    or trigger_display_fallback(applicable_page_types)
                ),
                "trigger_status": "已触发" if is_triggered else "未触发",
                "platform_progress": platform_progress,
                "effective_attempts": len(effective_items),
                "execution_records": len(execution_items),
                "evidence_count": len(evidence_ids),
                "touched_pages": "、".join(touched_pages),
                "last_finished_at": max(finished_values) if finished_values else None,
                "untriggered_reason": reason if not is_triggered else "",
                "progress_note": reason if is_triggered else "",
                "gap_reason": reason,
            }
        )
    return result


def _rule_platform_progress(
    statuses: set[str],
    execution_items: list[dict[str, Any]],
) -> str:
    if statuses & _STRENGTHEN_RULE_STATUSES:
        return "已触达待补强"
    if "running" in statuses:
        return "执行中"
    if any(item.get("display_status") in _FINAL_RULE_STATUSES for item in execution_items):
        if statuses & _UNREACHED_RULE_STATUSES:
            return "已触达待补强"
        return "已完成验证"
    return "未触达"


def _rule_progress_reason(
    *,
    platform_progress: str,
    items: list[dict[str, Any]],
    applicable_page_types: list[str],
    visited_page_types: set[str],
    task_termination: str,
) -> str:
    if platform_progress == "已完成验证":
        return ""
    reasons = []
    for item in items:
        if item.get("display_status") not in (
            _STRENGTHEN_RULE_STATUSES | _UNREACHED_RULE_STATUSES
        ):
            continue
        reason = _rule_item_reason(item, task_termination=task_termination)
        if reason and str(reason) not in reasons:
            reasons.append(str(reason))
    if reasons:
        return "；".join(reasons[:3])
    if platform_progress == "执行中":
        return "当前仍在执行，导出内容为生成时快照"
    if applicable_page_types and not (set(applicable_page_types) & visited_page_types):
        page_names = "、".join(
            page_type_display_name(page_type) or page_type
            for page_type in applicable_page_types
        )
        return _with_task_termination(
            f"未发现或未访问适用页面：{page_names}",
            task_termination,
        )
    visited_names = "、".join(
        page_type_display_name(page_type) or page_type
        for page_type in applicable_page_types
        if page_type in visited_page_types
    )
    return _with_task_termination(
        f"已访问适用页面（{visited_names or '适用页面'}），"
        "但未记录该规则命中候选或进入执行链",
        task_termination,
    )


def _rule_item_reason(item: dict[str, Any], *, task_termination: str) -> str:
    status = str(item.get("display_status") or "")
    execution_reason = str(item.get("execution_reason") or "").strip().lower()
    failure_code = str(item.get("capture_failure_code") or "").strip()
    if status == "not_executed":
        termination = (
            _termination_label(failure_code)
            or _termination_label(execution_reason)
            or task_termination
        )
        if execution_reason in {
            "run_budget_exhausted",
            "max_steps_reached",
            "continuation_limit_reached",
            "run_timeout",
        }:
            return _with_task_termination(
                "已匹配候选，但未进入规则判定",
                termination or "运行预算终止",
            )
        if execution_reason == "candidate_not_executed":
            return _with_task_termination(
                "已匹配候选，但任务结束前未执行",
                termination,
            )
    reason = item.get("problem_desc") or item.get("result_summary")
    if reason:
        return _with_task_termination(
            str(reason),
            task_termination if status in _UNREACHED_RULE_STATUSES else "",
        )
    if status == "pending":
        return "已匹配候选，任务仍在运行，尚未进入规则判定"
    if status == "coverage_gap":
        return _with_task_termination("适用页面未完成规则覆盖", task_termination)
    if status == "not_executed":
        return _with_task_termination("已匹配候选，但规则未执行", task_termination)
    return ""


def _task_termination_reason(
    context: dict[str, Any],
    timeline: dict[str, Any],
) -> str:
    raw_reasons: list[Any] = [context.get("stop_reason")]
    raw_reasons.extend(
        (node.get("inspection") or {}).get("failure_code")
        for node in timeline.get("nodes") or []
    )
    raw_reasons.extend(
        page.get("failure_code")
        for page in (timeline.get("exploration_summary") or {}).get("page_results") or []
    )
    for reason in raw_reasons:
        label = _termination_label(reason)
        if label:
            return label
    return ""


def _termination_label(reason: Any) -> str:
    normalized = str(reason or "").strip().lower()
    return _TASK_TERMINATION_LABELS.get(normalized, "")


def _with_task_termination(reason: str, task_termination: str) -> str:
    if not task_termination:
        return reason
    return f"{reason}；任务终止：{task_termination}"


def _build_page_rows(
    rule_items: list[dict[str, Any]],
    timeline: dict[str, Any],
) -> list[dict[str, Any]]:
    nodes = timeline.get("nodes") or []
    node_by_capture = {
        str(node["page_capture_id"]): node for node in nodes if node.get("page_capture_id")
    }
    executions_by_capture: dict[str, list[dict[str, Any]]] = {}
    for item in rule_items:
        capture_id = item.get("page_capture_id")
        if capture_id and item.get("item_type") == "execution":
            executions_by_capture.setdefault(str(capture_id), []).append(item)

    result: list[dict[str, Any]] = []
    used_capture_ids: set[str] = set()
    page_results = (timeline.get("exploration_summary") or {}).get("page_results") or []
    for page in page_results:
        capture_id = str(page.get("page_capture_id") or "")
        node = node_by_capture.get(capture_id, {})
        used_capture_ids.add(capture_id)
        result.append(
            _page_row(
                index=len(result) + 1,
                page=page,
                node=node,
                executions=executions_by_capture.get(capture_id, []),
            )
        )
    for node in nodes:
        capture_id = str(node.get("page_capture_id") or "")
        if capture_id and capture_id in used_capture_ids:
            continue
        page = {
            "page_capture_id": capture_id or None,
            "page_seq": node.get("node_seq"),
            "page_type": node.get("page_type"),
            "page_url": node.get("url"),
            "page_title": node.get("step_name"),
            "lifecycle_status": (node.get("inspection") or {}).get("lifecycle_status"),
            "inspection_status": (node.get("inspection") or {}).get("status"),
            "failure_message": (node.get("inspection") or {}).get("failure_message"),
            "abandon_reason": (node.get("inspection") or {}).get("abandon_reason"),
        }
        result.append(
            _page_row(
                index=len(result) + 1,
                page=page,
                node=node,
                executions=executions_by_capture.get(capture_id, []),
            )
        )
    return result


def _page_row(
    *,
    index: int,
    page: dict[str, Any],
    node: dict[str, Any],
    executions: list[dict[str, Any]],
) -> dict[str, Any]:
    capture_id = str(page.get("page_capture_id") or "")
    lifecycle = str(page.get("lifecycle_status") or "unknown")
    inspection_status = str(page.get("inspection_status") or "")
    node_status = str(node.get("status") or "")
    progress = _page_platform_progress(
        has_capture=bool(capture_id),
        lifecycle=lifecycle,
        inspection_status=inspection_status,
        node_status=node_status,
    )
    rule_progress = node.get("rule_progress") or {}
    executed = len(
        [
            item
            for item in executions
            if item.get("display_status") not in _UNREACHED_RULE_STATUSES
        ]
    )
    if not executions and rule_progress.get("executed") is not None:
        executed = int(rule_progress.get("executed") or 0)
    total = rule_progress.get("total")
    completion_rate = executed / int(total) if total not in (None, 0) else None
    page_type = str(page.get("page_type") or node.get("page_type") or "")
    failure_reason = str(
        page.get("failure_message")
        or page.get("abandon_reason")
        or (node.get("inspection") or {}).get("failure_message")
        or (node.get("inspection") or {}).get("abandon_reason")
        or ""
    )
    if not failure_reason and progress == "未访问":
        failure_reason = "计划页面尚未访问"
    return {
        "index": index,
        "page_seq": page.get("page_seq") or node.get("node_seq"),
        "page_name": str(
            page.get("page_title")
            or node.get("step_name")
            or page_type_display_name(page_type)
            or "未识别页面"
        ),
        "page_type": page_type_display_name(page_type) or page_type or "未识别",
        "source": _page_source_label(node.get("source"), has_capture=bool(capture_id)),
        "platform_progress": progress,
        "detection_status": str(
            (node.get("detection") or {}).get("status")
            or page.get("detection_status")
            or ""
        ),
        "inspection_status": str(
            (node.get("inspection") or {}).get("status_label") or inspection_status
        ),
        "executed_rules": executed,
        "candidate_rules": int(total) if total is not None else None,
        "completion_rate": completion_rate,
        "captured_at": _as_datetime(node.get("captured_at") or page.get("captured_at")),
        "url": str(page.get("page_url") or node.get("url") or ""),
        "gap_reason": failure_reason,
        "page_capture_id": capture_id,
    }


def _page_platform_progress(
    *,
    has_capture: bool,
    lifecycle: str,
    inspection_status: str,
    node_status: str,
) -> str:
    if not has_capture:
        if node_status == "running":
            return "访问中"
        if node_status == "failed":
            return "访问失败"
        return "未访问"
    if lifecycle == "superseded":
        return "已替代"
    if lifecycle == "abandoned":
        return "已放弃"
    if inspection_status == "completed":
        return "质检完成"
    if inspection_status == "degraded":
        return "完成待补强"
    if inspection_status == "failed":
        return "质检失败"
    if inspection_status in {"captured", "detecting", "matching", "evaluating"}:
        return "质检中"
    return "待质检"


def _build_gap_rows(
    rule_rows: list[dict[str, Any]],
    page_rows: list[dict[str, Any]],
    exploration_actions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    gaps = []
    for rule in rule_rows:
        if rule["platform_progress"] == "已完成验证":
            continue
        gaps.append(
            {
                "kind": "规则",
                "object_code": rule["rule_code"],
                "object_name": rule["rule_name"],
                "stage": rule["platform_progress"],
                "page_type": rule["applicable_page_types"],
                "reason": rule["gap_reason"],
                "retryable": "",
                "suggestion": _rule_suggestion(rule["platform_progress"]),
                "occurred_at": rule["last_finished_at"],
            }
        )
    page_gap_progresses = {
        "完成待补强",
        "质检失败",
        "未访问",
        "访问失败",
        "已放弃",
    }
    for page in page_rows:
        if page["platform_progress"] not in page_gap_progresses:
            continue
        gaps.append(
            {
                "kind": "页面",
                "object_code": page["page_capture_id"] or str(page["page_seq"] or ""),
                "object_name": page["page_name"],
                "stage": page["platform_progress"],
                "page_type": page["page_type"],
                "reason": page["gap_reason"],
                "retryable": "",
                "suggestion": "补充页面访问、识别或规则执行证据",
                "occurred_at": page["captured_at"],
            }
        )
    for action in exploration_actions:
        status = str(action.get("status") or "")
        if status == "explored":
            continue
        failure = (
            action.get("failure_message")
            or action.get("failure_reason")
            or action.get("purpose")
            or "探索动作尚未完成"
        )
        gaps.append(
            {
                "kind": "探索动作",
                "object_code": str(action.get("exploration_action_id") or ""),
                "object_name": str(action.get("target") or action.get("action") or ""),
                "stage": _action_status_label(status),
                "page_type": page_type_display_name(action.get("source_page_type"))
                or str(action.get("source_page_type") or ""),
                "reason": str(failure),
                "retryable": _yes_no(action.get("failure_retryable")),
                "suggestion": _action_suggestion(status),
                "occurred_at": _as_datetime(
                    action.get("updated_at") or action.get("created_at")
                ),
            }
        )
    return gaps


def _summary_sheet(snapshot: dict[str, Any]) -> _SheetSpec:
    meta = snapshot["meta"]
    metrics = snapshot["metrics"]
    applied_rules = [
        rule for rule in snapshot["rules"] if rule["platform_progress"] != "未触达"
    ]
    untriggered_rules = [
        rule for rule in snapshot["rules"] if rule["trigger_status"] == "未触发"
    ]
    rows = [
        [_Cell("Alioth 平台质检能力进展报告", _STYLE_TITLE)],
        [
            _Cell(
                "本报告衡量平台规则触达、页面质检与探索补强进展；"
                "不计算、也不代表受检产品通过率。",
                _STYLE_SUBTITLE,
            )
        ],
        [],
        [_Cell("导出范围", _STYLE_SECTION)],
        [
            _Cell("任务名称", _STYLE_META_LABEL),
            _Cell(meta["task_name"], _STYLE_BODY_WRAP),
            _Cell(),
            _Cell(),
            _Cell("任务状态", _STYLE_META_LABEL),
            _Cell(meta["task_status"]),
            _Cell("生成时间（北京时间）", _STYLE_META_LABEL),
            _Cell(meta["generated_at"], _STYLE_DATETIME),
        ],
        [
            _Cell("规则集", _STYLE_META_LABEL),
            _Cell(meta["ruleset_code"]),
            _Cell("版本", _STYLE_META_LABEL),
            _Cell(meta["ruleset_version"], _STYLE_INTEGER),
            _Cell("Run ID", _STYLE_META_LABEL),
            _Cell(meta["run_id"], _STYLE_BODY_WRAP),
            _Cell(),
            _Cell(),
        ],
        [
            _Cell("目标地址", _STYLE_META_LABEL),
            _Cell(meta["target_url"], _STYLE_BODY_WRAP),
        ],
        [],
        [_Cell("平台能力进展", _STYLE_SECTION)],
        [
            _Cell("规则总数", _STYLE_META_LABEL),
            _Cell(metrics["total_rules"], _STYLE_INTEGER),
            _Cell("已触达规则", _STYLE_META_LABEL),
            _Cell(metrics["reached_rules"], _STYLE_INTEGER),
            _Cell("已完成验证", _STYLE_META_LABEL),
            _Cell(metrics["completed_rules"], _STYLE_INTEGER),
            _Cell("规则触达率", _STYLE_META_LABEL),
            _Cell(metrics["rule_reach_rate"], _STYLE_PERCENT),
        ],
        [
            _Cell("已触达待补强", _STYLE_META_LABEL),
            _Cell(metrics["strengthen_rules"], _STYLE_INTEGER),
            _Cell("执行中", _STYLE_META_LABEL),
            _Cell(metrics["running_rules"], _STYLE_INTEGER),
            _Cell("未触达规则", _STYLE_META_LABEL),
            _Cell(metrics["unreached_rules"], _STYLE_INTEGER),
            _Cell("实际采集页面", _STYLE_META_LABEL),
            _Cell(metrics["actual_pages"], _STYLE_INTEGER),
        ],
        [
            _Cell("已质检页面", _STYLE_META_LABEL),
            _Cell(metrics["inspected_pages"], _STYLE_INTEGER),
            _Cell("页面质检完成", _STYLE_META_LABEL),
            _Cell(metrics["completed_pages"], _STYLE_INTEGER),
            _Cell("页面待补强", _STYLE_META_LABEL),
            _Cell(metrics["strengthen_pages"], _STYLE_INTEGER),
            _Cell("探索动作", _STYLE_META_LABEL),
            _Cell(metrics["exploration_actions"], _STYLE_INTEGER),
        ],
        [],
    ]
    applied_section_row = len(rows) + 1
    rows.append(
        [_Cell(f"本次已应用规则（{len(applied_rules)} 条）", _STYLE_SECTION)]
    )
    applied_header_row = len(rows) + 1
    rows.append(
        [
            _Cell("应用序号", _STYLE_HEADER),
            _Cell("规则编码", _STYLE_HEADER),
            _Cell("规则名称", _STYLE_HEADER),
            _Cell("平台进展", _STYLE_HEADER),
            _Cell("已触达页面", _STYLE_HEADER),
            _Cell("有效触达次数", _STYLE_HEADER),
            _Cell("证据数量", _STYLE_HEADER),
            _Cell("最近完成时间", _STYLE_HEADER),
        ]
    )
    applied_data_rows: list[int] = []
    for applied_index, rule in enumerate(applied_rules, start=1):
        applied_data_rows.append(len(rows) + 1)
        rows.append(
            [
                _Cell(applied_index, _STYLE_INTEGER),
                _Cell(rule["rule_code"]),
                _Cell(rule["rule_name"], _STYLE_BODY_WRAP),
                _Cell(
                    rule["platform_progress"],
                    _progress_style(rule["platform_progress"]),
                ),
                _Cell(rule["touched_pages"], _STYLE_BODY_WRAP),
                _Cell(rule["effective_attempts"], _STYLE_INTEGER),
                _Cell(rule["evidence_count"], _STYLE_INTEGER),
                _Cell(rule["last_finished_at"], _STYLE_DATETIME),
            ]
        )
    no_applied_rule_row: int | None = None
    if not applied_rules:
        no_applied_rule_row = len(rows) + 1
        rows.append(
            [
                _Cell(
                    "当前快照尚无已应用规则；全部规则及未触达原因请查看“规则进展”页签。",
                    _STYLE_BODY_WRAP,
                )
            ]
        )

    rows.append([])
    untriggered_section_row = len(rows) + 1
    rows.append(
        [_Cell(f"本次未触发规则（{len(untriggered_rules)} 条）", _STYLE_SECTION)]
    )
    untriggered_header_row = len(rows) + 1
    rows.append(
        [
            _Cell("未触发序号", _STYLE_HEADER),
            _Cell("规则编码", _STYLE_HEADER),
            _Cell("规则名称", _STYLE_HEADER),
            _Cell("规则包", _STYLE_HEADER),
            _Cell("触发条件", _STYLE_HEADER),
            _Cell("适用页面", _STYLE_HEADER),
            _Cell("平台进展", _STYLE_HEADER),
            _Cell("未触发原因", _STYLE_HEADER),
        ]
    )
    untriggered_data_rows: list[int] = []
    for untriggered_index, rule in enumerate(untriggered_rules, start=1):
        untriggered_data_rows.append(len(rows) + 1)
        rows.append(
            [
                _Cell(untriggered_index, _STYLE_INTEGER),
                _Cell(rule["rule_code"]),
                _Cell(rule["rule_name"], _STYLE_BODY_WRAP),
                _Cell(rule["rule_package"], _STYLE_BODY_WRAP),
                _Cell(rule["trigger_display"], _STYLE_BODY_WRAP),
                _Cell(rule["applicable_page_types"], _STYLE_BODY_WRAP),
                _Cell(
                    rule["platform_progress"],
                    _progress_style(rule["platform_progress"]),
                ),
                _Cell(rule["untriggered_reason"], _STYLE_BODY_WRAP),
            ]
        )
    no_untriggered_rule_row: int | None = None
    if not untriggered_rules:
        no_untriggered_rule_row = len(rows) + 1
        rows.append([_Cell("当前快照没有未触发规则。", _STYLE_BODY_WRAP)])

    rows.append([])
    explanation_section_row = len(rows) + 1
    rows.append([_Cell("口径说明", _STYLE_SECTION)])
    explanation_first_row = len(rows) + 1
    rows.append(
        [
            _Cell(
                "“已完成验证”表示平台已对该规则形成自动质检结论，"
                "无论该结论是通过、不通过或确认不适用；"
                "“已触达待补强”表示规则已经进入执行，但证据、识别或自动判定能力仍需补强。",
                _STYLE_BODY_WRAP,
            )
        ],
    )
    explanation_second_row = len(rows) + 1
    rows.append(
        [
            _Cell(
                "“未触达”包含适用页面未发现、页面未访问、规则未执行或运行预算终止。"
                "“未触发原因”会区分适用页面未访问、候选未命中、候选已匹配但未执行，"
                "并在有记录时附带任务终止原因。",
                _STYLE_BODY_WRAP,
            )
        ],
    )
    merges = [
        "A1:H1",
        "A2:H2",
        "A4:H4",
        "B5:D5",
        "F6:H6",
        "B7:H7",
        "A9:H9",
        f"A{applied_section_row}:H{applied_section_row}",
        f"A{untriggered_section_row}:H{untriggered_section_row}",
        f"A{explanation_section_row}:H{explanation_section_row}",
        f"A{explanation_first_row}:H{explanation_first_row}",
        f"A{explanation_second_row}:H{explanation_second_row}",
    ]
    if no_applied_rule_row is not None:
        merges.append(f"A{no_applied_rule_row}:H{no_applied_rule_row}")
    if no_untriggered_rule_row is not None:
        merges.append(f"A{no_untriggered_rule_row}:H{no_untriggered_rule_row}")
    row_heights = {
        1: 30,
        2: 28,
        7: 32,
        applied_header_row: 28,
        untriggered_header_row: 28,
        explanation_first_row: 44,
        explanation_second_row: 44,
    }
    row_heights.update({row: 32 for row in applied_data_rows})
    row_heights.update({row: 44 for row in untriggered_data_rows})
    if no_applied_rule_row is not None:
        row_heights[no_applied_rule_row] = 32
    if no_untriggered_rule_row is not None:
        row_heights[no_untriggered_rule_row] = 32
    return _SheetSpec(
        name="能力总览",
        rows=rows,
        widths=[14, 20, 28, 18, 24, 16, 20, 22],
        auto_filter=(
            f"A{applied_header_row}:H{applied_header_row + len(applied_rules)}"
            if applied_rules
            else None
        ),
        merges=merges,
        row_heights=row_heights,
    )


def _rules_sheet(rules: list[dict[str, Any]]) -> _SheetSpec:
    headers = [
        "序号",
        "规则编码",
        "规则名称",
        "规则包",
        "严重级别",
        "触发条件",
        "适用页面类型",
        "执行触发状态",
        "平台进展",
        "有效触达次数",
        "执行记录数",
        "证据数量",
        "已触达页面",
        "最近完成时间",
        "未触发原因",
        "待补强 / 进展说明",
    ]
    rows = [[_Cell(value, _STYLE_HEADER) for value in headers]]
    for rule in rules:
        rows.append(
            [
                _Cell(rule["index"], _STYLE_INTEGER),
                _Cell(rule["rule_code"]),
                _Cell(rule["rule_name"], _STYLE_BODY_WRAP),
                _Cell(rule["rule_package"]),
                _Cell(rule["severity"]),
                _Cell(rule["trigger_display"], _STYLE_BODY_WRAP),
                _Cell(rule["applicable_page_types"], _STYLE_BODY_WRAP),
                _Cell(
                    rule["trigger_status"],
                    (
                        _STYLE_COMPLETE
                        if rule["trigger_status"] == "已触发"
                        else _STYLE_UNREACHED
                    ),
                ),
                _Cell(
                    rule["platform_progress"],
                    _progress_style(rule["platform_progress"]),
                ),
                _Cell(rule["effective_attempts"], _STYLE_INTEGER),
                _Cell(rule["execution_records"], _STYLE_INTEGER),
                _Cell(rule["evidence_count"], _STYLE_INTEGER),
                _Cell(rule["touched_pages"], _STYLE_BODY_WRAP),
                _Cell(rule["last_finished_at"], _STYLE_DATETIME),
                _Cell(rule["untriggered_reason"], _STYLE_BODY_WRAP),
                _Cell(rule["progress_note"], _STYLE_BODY_WRAP),
            ]
        )
    end_row = max(1, len(rows))
    return _SheetSpec(
        name="规则进展",
        rows=rows,
        widths=[8, 18, 28, 18, 12, 28, 28, 16, 18, 14, 14, 12, 24, 21, 56, 52],
        freeze_rows=1,
        auto_filter=f"A1:P{end_row}",
        row_heights={1: 28},
    )


def _pages_sheet(pages: list[dict[str, Any]]) -> _SheetSpec:
    headers = [
        "序号",
        "页面序号",
        "页面名称",
        "页面类型",
        "页面来源",
        "平台进展",
        "识别状态",
        "质检状态",
        "已执行规则",
        "候选规则",
        "规则完成度",
        "采集时间",
        "页面地址",
        "待补强说明",
        "页面采集 ID",
    ]
    rows = [[_Cell(value, _STYLE_HEADER) for value in headers]]
    for page in pages:
        rows.append(
            [
                _Cell(page["index"], _STYLE_INTEGER),
                _Cell(page["page_seq"], _STYLE_INTEGER),
                _Cell(page["page_name"], _STYLE_BODY_WRAP),
                _Cell(page["page_type"]),
                _Cell(page["source"]),
                _Cell(
                    page["platform_progress"],
                    _progress_style(page["platform_progress"]),
                ),
                _Cell(page["detection_status"]),
                _Cell(page["inspection_status"]),
                _Cell(page["executed_rules"], _STYLE_INTEGER),
                _Cell(page["candidate_rules"], _STYLE_INTEGER),
                _Cell(page["completion_rate"], _STYLE_PERCENT),
                _Cell(page["captured_at"], _STYLE_DATETIME),
                _Cell(page["url"], _STYLE_BODY_WRAP),
                _Cell(page["gap_reason"], _STYLE_BODY_WRAP),
                _Cell(page["page_capture_id"], _STYLE_BODY_WRAP),
            ]
        )
    end_row = max(1, len(rows))
    return _SheetSpec(
        name="页面进展",
        rows=rows,
        widths=[8, 10, 24, 18, 14, 18, 16, 18, 14, 12, 14, 21, 42, 44, 38],
        freeze_rows=1,
        auto_filter=f"A1:O{end_row}",
        row_heights={1: 28},
    )


def _gaps_sheet(gaps: list[dict[str, Any]]) -> _SheetSpec:
    headers = [
        "类型",
        "对象标识",
        "对象名称",
        "当前阶段",
        "页面类型",
        "原因",
        "是否可重试",
        "建议方向",
        "时间",
    ]
    rows = [[_Cell(value, _STYLE_HEADER) for value in headers]]
    for gap in gaps:
        rows.append(
            [
                _Cell(gap["kind"]),
                _Cell(gap["object_code"], _STYLE_BODY_WRAP),
                _Cell(gap["object_name"], _STYLE_BODY_WRAP),
                _Cell(gap["stage"], _progress_style(gap["stage"])),
                _Cell(gap["page_type"], _STYLE_BODY_WRAP),
                _Cell(gap["reason"], _STYLE_BODY_WRAP),
                _Cell(gap["retryable"]),
                _Cell(gap["suggestion"], _STYLE_BODY_WRAP),
                _Cell(gap["occurred_at"], _STYLE_DATETIME),
            ]
        )
    end_row = max(1, len(rows))
    return _SheetSpec(
        name="待补强项",
        rows=rows,
        widths=[14, 24, 28, 18, 22, 52, 14, 38, 21],
        freeze_rows=1,
        auto_filter=f"A1:I{end_row}",
        row_heights={1: 28},
    )


def _package_workbook(sheets: list[_SheetSpec], *, generated_at: datetime) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", _content_types_xml(len(sheets)))
        archive.writestr("_rels/.rels", _root_relationships_xml())
        archive.writestr("docProps/app.xml", _app_properties_xml(sheets))
        archive.writestr("docProps/core.xml", _core_properties_xml(generated_at))
        archive.writestr("xl/workbook.xml", _workbook_xml(sheets))
        archive.writestr("xl/_rels/workbook.xml.rels", _workbook_relationships_xml(sheets))
        archive.writestr("xl/styles.xml", _styles_xml())
        for index, sheet in enumerate(sheets, start=1):
            archive.writestr(
                f"xl/worksheets/sheet{index}.xml",
                _worksheet_xml(sheet),
            )
    return buffer.getvalue()


def _worksheet_xml(sheet: _SheetSpec) -> str:
    max_columns = max((len(row) for row in sheet.rows), default=1)
    max_rows = max(len(sheet.rows), 1)
    last_cell = f"{_column_name(max_columns)}{max_rows}"
    columns = "".join(
        f'<col min="{index}" max="{index}" width="{width}" customWidth="1"/>'
        for index, width in enumerate(sheet.widths, start=1)
    )
    row_xml = []
    for row_index, row in enumerate(sheet.rows, start=1):
        height = sheet.row_heights.get(row_index)
        height_attrs = f' ht="{height}" customHeight="1"' if height else ""
        cells = "".join(
            _cell_xml(row_index, column_index, cell)
            for column_index, cell in enumerate(row, start=1)
            if cell.value is not None and cell.value != ""
        )
        row_xml.append(f'<row r="{row_index}"{height_attrs}>{cells}</row>')
    pane = ""
    if sheet.freeze_rows:
        pane = (
            f'<pane ySplit="{sheet.freeze_rows}" topLeftCell="A{sheet.freeze_rows + 1}" '
            'activePane="bottomLeft" state="frozen"/>'
        )
    auto_filter = (
        f'<autoFilter ref="{escape(sheet.auto_filter)}"/>' if sheet.auto_filter else ""
    )
    merges = ""
    if sheet.merges:
        merge_cells = "".join(
            f'<mergeCell ref="{escape(cell_range)}"/>' for cell_range in sheet.merges
        )
        merges = f'<mergeCells count="{len(sheet.merges)}">{merge_cells}</mergeCells>'
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f'<dimension ref="A1:{last_cell}"/>'
        '<sheetViews>'
        f'<sheetView workbookViewId="0" showGridLines="0">{pane}</sheetView>'
        '</sheetViews>'
        '<sheetFormatPr defaultRowHeight="20"/>'
        f"<cols>{columns}</cols>"
        f"<sheetData>{''.join(row_xml)}</sheetData>"
        f"{auto_filter}{merges}"
        '<pageMargins left="0.3" right="0.3" top="0.5" bottom="0.5" '
        'header="0.2" footer="0.2"/>'
        "</worksheet>"
    )


def _cell_xml(row: int, column: int, cell: _Cell) -> str:
    reference = f"{_column_name(column)}{row}"
    value = cell.value
    if isinstance(value, bool):
        return f'<c r="{reference}" s="{cell.style}" t="b"><v>{int(value)}</v></c>'
    if isinstance(value, datetime):
        serial = _excel_datetime_serial(value)
        return f'<c r="{reference}" s="{cell.style}"><v>{serial:.10f}</v></c>'
    if isinstance(value, (int, float)):
        return f'<c r="{reference}" s="{cell.style}"><v>{value}</v></c>'
    text = escape(_clean_xml_text(str(value)))
    preserve = ' xml:space="preserve"' if text != text.strip() else ""
    return (
        f'<c r="{reference}" s="{cell.style}" t="inlineStr">'
        f"<is><t{preserve}>{text}</t></is></c>"
    )


def _content_types_xml(sheet_count: int) -> str:
    sheet_overrides = "".join(
        '<Override PartName="/xl/worksheets/sheet'
        f'{index}.xml" ContentType="application/vnd.openxmlformats-officedocument.'
        'spreadsheetml.worksheet+xml"/>'
        for index in range(1, sheet_count + 1)
    )
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.'
        'relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-'
        'officedocument.spreadsheetml.sheet.main+xml"/>'
        '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-'
        'officedocument.spreadsheetml.styles+xml"/>'
        '<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-'
        'package.core-properties+xml"/>'
        '<Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-'
        'officedocument.extended-properties+xml"/>'
        f"{sheet_overrides}</Types>"
    )


def _root_relationships_xml() -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/'
        '2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
        '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/'
        'relationships/metadata/core-properties" Target="docProps/core.xml"/>'
        '<Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/'
        '2006/relationships/extended-properties" Target="docProps/app.xml"/>'
        "</Relationships>"
    )


def _workbook_xml(sheets: list[_SheetSpec]) -> str:
    sheet_nodes = "".join(
        f'<sheet name="{escape(sheet.name)}" sheetId="{index}" r:id="rId{index}"/>'
        for index, sheet in enumerate(sheets, start=1)
    )
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        f"<sheets>{sheet_nodes}</sheets>"
        '<calcPr calcId="191029" fullCalcOnLoad="1" forceFullCalc="1"/>'
        "</workbook>"
    )


def _workbook_relationships_xml(sheets: list[_SheetSpec]) -> str:
    relationships = "".join(
        '<Relationship Id="rId'
        f'{index}" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
        f'relationships/worksheet" Target="worksheets/sheet{index}.xml"/>'
        for index in range(1, len(sheets) + 1)
    )
    style_id = len(sheets) + 1
    relationships += (
        f'<Relationship Id="rId{style_id}" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/'
        'relationships/styles" Target="styles.xml"/>'
    )
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        f"{relationships}</Relationships>"
    )


def _app_properties_xml(sheets: list[_SheetSpec]) -> str:
    titles = "".join(f"<vt:lpstr>{escape(sheet.name)}</vt:lpstr>" for sheet in sheets)
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/'
        'extended-properties" xmlns:vt="http://schemas.openxmlformats.org/officeDocument/'
        '2006/docPropsVTypes">'
        "<Application>Alioth</Application>"
        f"<TitlesOfParts><vt:vector size=\"{len(sheets)}\" baseType=\"lpstr\">"
        f"{titles}</vt:vector></TitlesOfParts>"
        f'<HeadingPairs><vt:vector size="2" baseType="variant"><vt:variant><vt:lpstr>'
        f'Worksheets</vt:lpstr></vt:variant><vt:variant><vt:i4>{len(sheets)}</vt:i4>'
        "</vt:variant></vt:vector></HeadingPairs>"
        "</Properties>"
    )


def _core_properties_xml(generated_at: datetime) -> str:
    stamp = generated_at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/'
        'metadata/core-properties" xmlns:dc="http://purl.org/dc/elements/1.1/" '
        'xmlns:dcterms="http://purl.org/dc/terms/" '
        'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
        "<dc:creator>Alioth</dc:creator><cp:lastModifiedBy>Alioth</cp:lastModifiedBy>"
        f'<dcterms:created xsi:type="dcterms:W3CDTF">{stamp}</dcterms:created>'
        f'<dcterms:modified xsi:type="dcterms:W3CDTF">{stamp}</dcterms:modified>'
        "</cp:coreProperties>"
    )


def _styles_xml() -> str:
    return """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <numFmts count="1"><numFmt numFmtId="164" formatCode="yyyy-mm-dd hh:mm:ss"/></numFmts>
  <fonts count="9">
    <font><sz val="11"/><color rgb="FF334155"/><name val="Microsoft YaHei"/></font>
    <font><b/><sz val="18"/><color rgb="FFFFFFFF"/><name val="Microsoft YaHei"/></font>
    <font><b/><sz val="11"/><color rgb="FFFFFFFF"/><name val="Microsoft YaHei"/></font>
    <font><b/><sz val="11"/><color rgb="FF1E3A8A"/><name val="Microsoft YaHei"/></font>
    <font><b/><sz val="11"/><color rgb="FF166534"/><name val="Microsoft YaHei"/></font>
    <font><b/><sz val="11"/><color rgb="FF92400E"/><name val="Microsoft YaHei"/></font>
    <font><b/><sz val="11"/><color rgb="FF1D4ED8"/><name val="Microsoft YaHei"/></font>
    <font><b/><sz val="11"/><color rgb="FF64748B"/><name val="Microsoft YaHei"/></font>
    <font><b/><sz val="11"/><color rgb="FFB91C1C"/><name val="Microsoft YaHei"/></font>
  </fonts>
  <fills count="10">
    <fill><patternFill patternType="none"/></fill>
    <fill><patternFill patternType="gray125"/></fill>
    <fill><patternFill patternType="solid"><fgColor rgb="FF172554"/></patternFill></fill>
    <fill><patternFill patternType="solid"><fgColor rgb="FF1D4ED8"/></patternFill></fill>
    <fill><patternFill patternType="solid"><fgColor rgb="FFEFF6FF"/></patternFill></fill>
    <fill><patternFill patternType="solid"><fgColor rgb="FFDCFCE7"/></patternFill></fill>
    <fill><patternFill patternType="solid"><fgColor rgb="FFFEF3C7"/></patternFill></fill>
    <fill><patternFill patternType="solid"><fgColor rgb="FFDBEAFE"/></patternFill></fill>
    <fill><patternFill patternType="solid"><fgColor rgb="FFF1F5F9"/></patternFill></fill>
    <fill><patternFill patternType="solid"><fgColor rgb="FFFEE2E2"/></patternFill></fill>
  </fills>
  <borders count="3">
    <border><left/><right/><top/><bottom/><diagonal/></border>
    <border><left/><right/><top/><bottom style="thin"><color rgb="FFE2E8F0"/></bottom>
      <diagonal/></border>
    <border><left style="thin"><color rgb="FFBFDBFE"/></left>
      <right style="thin"><color rgb="FFBFDBFE"/></right>
      <top style="thin"><color rgb="FFBFDBFE"/></top>
      <bottom style="thin"><color rgb="FFBFDBFE"/></bottom><diagonal/></border>
  </borders>
  <cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
  <cellXfs count="16">
    <xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>
    <xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0"
      applyFont="1" applyFill="1"><alignment vertical="center"/></xf>
    <xf numFmtId="0" fontId="0" fillId="4" borderId="0" xfId="0"
      applyFill="1"><alignment vertical="center" wrapText="1"/></xf>
    <xf numFmtId="0" fontId="2" fillId="3" borderId="0" xfId="0"
      applyFont="1" applyFill="1"><alignment vertical="center"/></xf>
    <xf numFmtId="0" fontId="2" fillId="2" borderId="2" xfId="0"
      applyFont="1" applyFill="1" applyBorder="1">
      <alignment horizontal="center" vertical="center" wrapText="1"/></xf>
    <xf numFmtId="0" fontId="0" fillId="0" borderId="1" xfId="0"
      applyBorder="1"><alignment vertical="center"/></xf>
    <xf numFmtId="0" fontId="0" fillId="0" borderId="1" xfId="0"
      applyBorder="1"><alignment vertical="top" wrapText="1"/></xf>
    <xf numFmtId="0" fontId="0" fillId="0" borderId="1" xfId="0"
      applyBorder="1"><alignment horizontal="right" vertical="center"/></xf>
    <xf numFmtId="10" fontId="0" fillId="0" borderId="1" xfId="0"
      applyNumberFormat="1" applyBorder="1"><alignment horizontal="right"/></xf>
    <xf numFmtId="0" fontId="4" fillId="5" borderId="1" xfId="0"
      applyFont="1" applyFill="1" applyBorder="1"><alignment horizontal="center"/></xf>
    <xf numFmtId="0" fontId="5" fillId="6" borderId="1" xfId="0"
      applyFont="1" applyFill="1" applyBorder="1"><alignment horizontal="center"/></xf>
    <xf numFmtId="0" fontId="6" fillId="7" borderId="1" xfId="0"
      applyFont="1" applyFill="1" applyBorder="1"><alignment horizontal="center"/></xf>
    <xf numFmtId="0" fontId="7" fillId="8" borderId="1" xfId="0"
      applyFont="1" applyFill="1" applyBorder="1"><alignment horizontal="center"/></xf>
    <xf numFmtId="0" fontId="3" fillId="4" borderId="2" xfId="0"
      applyFont="1" applyFill="1" applyBorder="1"><alignment vertical="center"/></xf>
    <xf numFmtId="164" fontId="0" fillId="0" borderId="1" xfId="0"
      applyNumberFormat="1" applyBorder="1"><alignment horizontal="center"/></xf>
    <xf numFmtId="0" fontId="8" fillId="9" borderId="1" xfId="0"
      applyFont="1" applyFill="1" applyBorder="1"><alignment horizontal="center"/></xf>
  </cellXfs>
  <cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>
</styleSheet>"""


def _progress_style(progress: str) -> int:
    if progress in {"已完成验证", "质检完成"}:
        return _STYLE_COMPLETE
    if progress in {
        "已触达待补强",
        "完成待补强",
        "待质检",
        "已放弃",
        "已阻断",
        "已跳过",
    }:
        return _STYLE_STRENGTHEN
    if progress in {"执行中", "访问中", "质检中", "待探索", "已选择"}:
        return _STYLE_RUNNING
    if progress in {"质检失败", "访问失败", "执行失败"}:
        return _STYLE_FAILED
    return _STYLE_UNREACHED


def _rule_suggestion(progress: str) -> str:
    if progress == "已触达待补强":
        return "补齐证据、识别或自动判定能力后重试"
    if progress == "执行中":
        return "等待当前任务继续执行后重新导出"
    return "优先发现适用页面并确保规则进入执行链"


def _action_status_label(status: str) -> str:
    return {
        "candidate": "待探索",
        "selected": "已选择",
        "blocked": "已阻断",
        "skipped": "已跳过",
        "failed": "执行失败",
    }.get(status, status or "未知")


def _action_suggestion(status: str) -> str:
    if status == "blocked":
        return "检查安全策略、遮挡状态和动作风险归类"
    if status == "skipped":
        return "检查动作筛选、去重和页面恢复条件"
    if status == "failed":
        return "优化目标定位、页面恢复或失败重试"
    return "继续调度该探索动作并形成结果页面"


def _page_source_label(source: Any, *, has_capture: bool) -> str:
    if source == "discovered":
        return "动态发现"
    if source == "journey":
        return "预设旅程"
    return "真实采集" if has_capture else "计划页面"


def _yes_no(value: Any) -> str:
    if value is True:
        return "是"
    if value is False:
        return "否"
    return ""


def _count_values(values: Any) -> dict[str, int]:
    counts: dict[str, int] = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    return counts


def _as_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _excel_datetime_serial(value: datetime) -> float:
    if value.tzinfo is not None:
        value = value.astimezone(_REPORT_TIMEZONE).replace(tzinfo=None)
    return (value - datetime(1899, 12, 30)).total_seconds() / 86400


def _clean_xml_text(value: str) -> str:
    return _INVALID_XML_CHARS.sub("", value)


def _column_name(index: int) -> str:
    result = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        result = chr(65 + remainder) + result
    return result
