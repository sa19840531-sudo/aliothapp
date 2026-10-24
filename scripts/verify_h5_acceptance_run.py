"""校验一次 H5 页面探索验收运行是否满足 W46/W47 闭环口径。"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

TERMINAL_TASK_STATUSES = {"done", "review", "failed", "canceled"}
SUCCESS_TASK_STATUSES = {"done"}
TERMINAL_RUN_STATUSES = {"completed", "failed", "canceled"}
NORMAL_RULE_STATUSES = {"pass", "fail", "warning", "manual_review"}


@dataclass(frozen=True)
class AcceptanceCheck:
    """单项验收检查结果。"""

    code: str
    passed: bool
    message: str


def load_json_file(path: str | Path) -> dict[str, Any]:
    with Path(path).open(encoding="utf-8") as file:
        payload = json.load(file)
    if not isinstance(payload, dict):
        raise ValueError(f"{path} 必须是 JSON object")
    return payload


def load_json_url(url: str, *, access_token: str | None = None) -> dict[str, Any]:
    headers = {"Accept": "application/json"}
    if access_token:
        headers["Authorization"] = f"Bearer {access_token}"
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{url} 必须返回 JSON object")
    return payload


def login_for_access_token(
    *,
    base_url: str,
    username: str,
    password: str,
) -> str:
    normalized_base = base_url.rstrip("/")
    body = json.dumps(
        {"username": username, "password": password},
        ensure_ascii=False,
    ).encode("utf-8")
    request = urllib.request.Request(
        f"{normalized_base}/api/v1/auth/login",
        data=body,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("登录接口必须返回 JSON object")
    token = _text(payload.get("token") or payload.get("access_token"))
    if not token:
        raise ValueError("登录接口未返回 token")
    return token


def fetch_api_payloads(
    *,
    base_url: str,
    task_id: str,
    agent_run_id: str,
    access_token: str | None = None,
) -> tuple[
    dict[str, Any],
    dict[str, Any],
    dict[str, Any],
    dict[str, Any],
    dict[str, Any],
    dict[str, Any],
]:
    normalized_base = base_url.rstrip("/")
    encoded_task_id = urllib.parse.quote(task_id, safe="")
    encoded_run_id = urllib.parse.quote(agent_run_id, safe="")
    return (
        load_json_url(
            f"{normalized_base}/api/v1/workstation/tasks/{encoded_task_id}",
            access_token=access_token,
        ),
        load_json_url(
            f"{normalized_base}/api/v1/agents/runs/{encoded_run_id}",
            access_token=access_token,
        ),
        load_json_url(
            f"{normalized_base}/api/v1/tasks/{encoded_task_id}/rule-executions"
            f"?agent_run_id={encoded_run_id}",
            access_token=access_token,
        ),
        load_json_url(
            f"{normalized_base}/api/v1/workstation/reports/{encoded_task_id}"
            f"?agent_run_id={encoded_run_id}",
            access_token=access_token,
        ),
        load_json_url(
            f"{normalized_base}/api/v1/workstation/tasks/{encoded_task_id}/page-timeline",
            access_token=access_token,
        ),
        load_json_url(
            f"{normalized_base}/api/v1/workstation/tasks/{encoded_task_id}/inspection-work-items",
            access_token=access_token,
        ),
    )


def verify_acceptance_payloads(
    *,
    task: dict[str, Any],
    run: dict[str, Any],
    rule_executions: dict[str, Any],
    report: dict[str, Any],
    timeline: dict[str, Any] | None = None,
    inspection_work: dict[str, Any] | None = None,
    popup_action_traces: dict[str, Any] | None = None,
    expect_popup_chain: bool = False,
    min_popup_layers: int = 2,
    require_business_completion: bool = False,
) -> list[AcceptanceCheck]:
    rules = _items(rule_executions)
    checks = [
        _check_task_terminal(task),
        _check_run_terminal(task, run),
        _check_report_status(task, report),
        _check_rule_execution_evidence(rules),
        _check_no_bootstrap_duplicate(rules),
        _check_usage_cost_trace(run),
        _check_page_exploration(task, report),
        _check_popup_chain(
            run,
            report,
            popup_action_traces=popup_action_traces,
            expect_popup_chain=expect_popup_chain,
            min_closed_layers=min_popup_layers,
        ),
    ]
    if timeline is not None:
        checks.extend(
            (
                _check_report_timeline_terminal_consistency(report, timeline),
                _check_h5_coverage(task, timeline),
                _check_h5_inventory_authority(task, timeline),
                _check_h5_delivery_closure(task, timeline),
            )
        )
    if inspection_work is not None:
        checks.append(_check_inspection_work_terminal(task, inspection_work))
    if require_business_completion:
        checks.append(_check_business_completion(task, report))
    return checks


def _check_business_completion(task: dict[str, Any], report: dict[str, Any]) -> AcceptanceCheck:
    """真实业务验收只接受 Gate 放行且正式报告已完成的任务。"""

    summary = report.get("summary") if isinstance(report.get("summary"), dict) else {}
    failures: list[str] = []
    if _text(task.get("status")) != "done":
        failures.append(f"task.status={_text(task.get('status')) or '<empty>'}")
    if task.get("completion_gate_passed") is not True:
        failures.append("completion_gate_passed=false")
    if report.get("is_final") is not True:
        failures.append("report.is_final=false")
    if _text(report.get("report_stage")) != "final":
        failures.append(f"report_stage={_text(report.get('report_stage')) or '<empty>'}")
    if _text(summary.get("business_completion_status")) != "complete":
        failures.append(
            "business_completion_status="
            f"{_text(summary.get('business_completion_status')) or '<empty>'}"
        )
    if failures:
        return AcceptanceCheck(
            "BUSINESS_COMPLETION",
            False,
            "业务验收未完成：" + ", ".join(failures),
        )
    return AcceptanceCheck(
        "BUSINESS_COMPLETION",
        True,
        "Completion Gate、任务终态与正式报告均已完成",
    )


def _check_report_timeline_terminal_consistency(
    report: dict[str, Any],
    timeline: dict[str, Any],
) -> AcceptanceCheck:
    """确认报告与时间线使用同一份 H5 终态合同。"""
    page_exploration = report.get("page_exploration")
    exploration_summary = timeline.get("exploration_summary")
    if not isinstance(page_exploration, dict) or not isinstance(exploration_summary, dict):
        return AcceptanceCheck(
            "H5_TERMINAL_CONSISTENCY",
            False,
            "报告或 Timeline 缺少 H5 探索终态摘要",
        )

    missing = [
        f"report.{field}"
        for field in ("complete", "quality_status", "requires_review")
        if field not in page_exploration
    ]
    missing.extend(
        f"timeline.{field}"
        for field in (
            "complete",
            "quality_status",
            "requires_review",
            "terminal_reason",
        )
        if field not in exploration_summary
    )
    if "terminal_reason" not in page_exploration:
        missing.append("report.terminal_reason")
    if "terminal_reason" not in timeline:
        missing.append("timeline.terminal_reason")
    if missing:
        return AcceptanceCheck(
            "H5_TERMINAL_CONSISTENCY",
            False,
            f"H5 终态合同字段缺失：{', '.join(missing)}",
        )

    pairs = {
        "complete": (
            page_exploration.get("complete"),
            exploration_summary.get("complete"),
        ),
        "quality_status": (
            _text(page_exploration.get("quality_status")),
            _text(exploration_summary.get("quality_status")),
        ),
        "requires_review": (
            page_exploration.get("requires_review"),
            exploration_summary.get("requires_review"),
        ),
        "terminal_reason": (
            _text(page_exploration.get("terminal_reason")),
            _text(exploration_summary.get("terminal_reason")),
        ),
    }
    if _text(exploration_summary.get("terminal_reason")) != _text(timeline.get("terminal_reason")):
        pairs["timeline_terminal_reason"] = (
            _text(exploration_summary.get("terminal_reason")),
            _text(timeline.get("terminal_reason")),
        )
    mismatches = [
        f"{field}: report={values[0]!r} timeline={values[1]!r}"
        for field, values in pairs.items()
        if values[0] != values[1]
    ]
    if mismatches:
        return AcceptanceCheck(
            "H5_TERMINAL_CONSISTENCY",
            False,
            f"报告与 Timeline 终态不一致：{'; '.join(mismatches)}",
        )
    return AcceptanceCheck(
        "H5_TERMINAL_CONSISTENCY",
        True,
        "报告与 Timeline 使用一致的 H5 终态",
    )


def _check_h5_coverage(task: dict[str, Any], timeline: dict[str, Any]) -> AcceptanceCheck:
    coverage = timeline.get("h5_coverage")
    if not isinstance(coverage, dict):
        return AcceptanceCheck("H5_COVERAGE", False, "Timeline 缺少 H5 coverage snapshot")
    authority = _text(coverage.get("authority_source"))
    revision = coverage.get("revision")
    payload_hash = _text(coverage.get("payload_hash"))
    missing = coverage.get("missing_page_types")
    if authority != "h5_page_type_assessment" or not isinstance(revision, int) or not payload_hash:
        return AcceptanceCheck(
            "H5_COVERAGE",
            False,
            "Coverage 必须来自页面评估并携带 revision/hash",
        )
    if _text(task.get("status")) == "done" and (
        coverage.get("completion_ready") is not True or missing
    ):
        return AcceptanceCheck(
            "H5_COVERAGE",
            False,
            f"done 任务仍有覆盖缺口：missing={missing or []}",
        )
    return AcceptanceCheck(
        "H5_COVERAGE",
        True,
        f"Coverage revision={revision}，missing={missing or []}",
    )


def _check_h5_inventory_authority(
    task: dict[str, Any], timeline: dict[str, Any]
) -> AcceptanceCheck:
    inventory = timeline.get("h5_evidence_inventory")
    items = inventory.get("items") if isinstance(inventory, dict) else None
    if not isinstance(items, list):
        return AcceptanceCheck("H5_INVENTORY_AUTHORITY", False, "Timeline 缺少 Inventory")
    invalid = [
        str(item.get("inventory_item_id") or "<unknown>")
        for item in items
        if isinstance(item, dict)
        and item.get("status") == "pass"
        and (
            item.get("authority_source") != "pass_manifest"
            or not item.get("before_manifest_id")
            or not item.get("after_manifest_id")
        )
    ]
    if invalid:
        return AcceptanceCheck(
            "H5_INVENTORY_AUTHORITY",
            False,
            f"PASS Inventory 未绑定 PASS Manifest：{', '.join(invalid[:5])}",
        )
    if (
        _text(task.get("status")) == "done"
        and timeline.get("execution_mode") != "exploration_only"
        and not items
    ):
        return AcceptanceCheck("H5_INVENTORY_AUTHORITY", False, "done 任务 Inventory 为空")
    return AcceptanceCheck("H5_INVENTORY_AUTHORITY", True, "Inventory 权威引用完整")


def _check_h5_delivery_closure(task: dict[str, Any], timeline: dict[str, Any]) -> AcceptanceCheck:
    closure = timeline.get("full_inspection_closure")
    if not isinstance(closure, dict) or not closure:
        if timeline.get("execution_mode") == "exploration_only":
            return AcceptanceCheck(
                "H5_DELIVERY_CLOSURE",
                True,
                "纯探索任务不要求规则发布闭环",
            )
        return AcceptanceCheck(
            "H5_DELIVERY_CLOSURE",
            False,
            "完整质检 Timeline 缺少 Publication/Outbox/Inbox/Work/Gate 闭环事实",
        )
    projection = closure.get("publication_projection")
    projection = projection if isinstance(projection, dict) else {}
    pending = sum(
        int(projection.get(key) or 0)
        for key in (
            "outbox_pending_count",
            "outbox_dead_letter_count",
            "inbox_pending_count",
            "inbox_dead_letter_count",
        )
    )
    if pending or int(closure.get("nonterminal_inspection_count") or 0):
        return AcceptanceCheck(
            "H5_DELIVERY_CLOSURE",
            False,
            "Publication/Outbox/Inbox/Inspection Work 尚未闭合",
        )
    if _text(task.get("status")) == "done" and closure.get("gate_passed") is not True:
        return AcceptanceCheck(
            "H5_DELIVERY_CLOSURE",
            False,
            f"Completion Gate 未放行：{closure.get('gate_block_reasons') or []}",
        )
    return AcceptanceCheck("H5_DELIVERY_CLOSURE", True, "发布、投递、Work 与 Gate 已闭合")


def _check_inspection_work_terminal(
    task: dict[str, Any], inspection_work: dict[str, Any]
) -> AcceptanceCheck:
    summary = inspection_work.get("summary")
    summary = summary if isinstance(summary, dict) else {}
    nonterminal = int(summary.get("nonterminal_count") or 0)
    if _text(task.get("status")) in TERMINAL_TASK_STATUSES and nonterminal:
        return AcceptanceCheck(
            "INSPECTION_WORK_TERMINAL",
            False,
            f"终态任务仍有 {nonterminal} 个非终态 Work",
        )
    return AcceptanceCheck("INSPECTION_WORK_TERMINAL", True, "Inspection Work 已终态")


def _items(payload: dict[str, Any]) -> list[dict[str, Any]]:
    raw_items = payload.get("items", payload.get("rule_executions", []))
    if not isinstance(raw_items, list):
        return []
    return [item for item in raw_items if isinstance(item, dict)]


def _check_task_terminal(task: dict[str, Any]) -> AcceptanceCheck:
    status = _text(task.get("status"))
    if status not in TERMINAL_TASK_STATUSES:
        return AcceptanceCheck(
            "TASK_TERMINAL",
            False,
            f"任务尚未进入终态：status={status or '<empty>'}",
        )
    if status == "failed" and not _text(task.get("failure_reason")):
        return AcceptanceCheck("TASK_TERMINAL", False, "失败任务必须有 failure_reason")
    return AcceptanceCheck("TASK_TERMINAL", True, f"任务终态有效：{status}")


def _check_run_terminal(task: dict[str, Any], run: dict[str, Any]) -> AcceptanceCheck:
    task_status = _text(task.get("status"))
    run_status = _text(run.get("status"))
    if run_status not in TERMINAL_RUN_STATUSES:
        return AcceptanceCheck(
            "RUN_TERMINAL",
            False,
            f"Agent Run 尚未进入终态：status={run_status or '<empty>'}",
        )
    if task_status == "done" and run_status != "completed":
        return AcceptanceCheck(
            "RUN_TERMINAL",
            False,
            f"H5 验收要求 completed Run：run={run_status}",
        )
    if task_status == "review" and run_status == "failed":
        termination_reason = _text(run.get("termination_reason"))
        if termination_reason != "incomplete":
            return AcceptanceCheck(
                "RUN_TERMINAL",
                False,
                "review 任务的 failed Run 必须明确以 incomplete 收口："
                f"termination_reason={termination_reason or '<empty>'}",
            )
        return AcceptanceCheck(
            "RUN_TERMINAL",
            True,
            "Run 已按证据不足边界收口：failed/incomplete → review",
        )
    return AcceptanceCheck("RUN_TERMINAL", True, f"Run 终态有效：{run_status}")


def _check_report_status(task: dict[str, Any], report: dict[str, Any]) -> AcceptanceCheck:
    task_status = _text(task.get("status"))
    report_status = _text(task.get("report_status"))
    report_stage = _text(report.get("report_stage"))
    summary = report.get("summary") if isinstance(report.get("summary"), dict) else {}
    overall_status = _text(summary.get("overall_status"))
    if task_status == "done" and report_status != "ready":
        return AcceptanceCheck(
            "REPORT_READY",
            False,
            f"done 任务报告必须 ready：report_status={report_status or '<empty>'}",
        )
    if task_status == "review" and report_status != "ready":
        return AcceptanceCheck(
            "REPORT_READY",
            False,
            f"review 任务必须形成可复核报告：report_status={report_status or '<empty>'}",
        )
    if task_status == "done" and overall_status == "incomplete":
        return AcceptanceCheck("REPORT_READY", False, "H5 验收不能生成 incomplete 报告")
    expected_report_stage = {
        "done": "final",
        "review": "final_review",
    }.get(task_status)
    if expected_report_stage and report_stage != expected_report_stage:
        return AcceptanceCheck(
            "REPORT_READY",
            False,
            "报告阶段与任务终态不一致："
            f"task_status={task_status} "
            f"expected={expected_report_stage} "
            f"actual={report_stage or '<empty>'}",
        )
    return AcceptanceCheck("REPORT_READY", True, "报告终态与任务终态一致")


def _check_rule_execution_evidence(rules: list[dict[str, Any]]) -> AcceptanceCheck:
    if not rules:
        return AcceptanceCheck("RULE_EVIDENCE", False, "没有规则执行结果")
    missing = []
    for rule in rules:
        status = _text(rule.get("execution_status"))
        if status not in NORMAL_RULE_STATUSES:
            continue
        artifacts = rule.get("evidence_artifact_ids")
        if not isinstance(artifacts, list | tuple) or not artifacts:
            missing.append(_rule_identity(rule, "缺少 evidence_artifact_ids"))
            continue
        if _text(rule.get("page_capture_id")):
            continue
        if not (
            _text(rule.get("evidence_input_set_id")) and _text(rule.get("observation_packet_id"))
        ):
            missing.append(
                _rule_identity(
                    rule,
                    "缺少页面锚点，且任务级 EvidenceInputSet/ObservationPacket 不完整",
                )
            )
    if missing:
        sample = "; ".join(missing[:5])
        return AcceptanceCheck(
            "RULE_EVIDENCE",
            False,
            f"正常规则结果必须绑定页面级或任务级证据：{sample}",
        )
    return AcceptanceCheck("RULE_EVIDENCE", True, "正常规则结果均绑定可追溯证据")


def _check_no_bootstrap_duplicate(rules: list[dict[str, Any]]) -> AcceptanceCheck:
    real_result_codes = {
        _text(rule.get("rule_code") or rule.get("rule_id"))
        for rule in rules
        if _text(rule.get("source")) != "bootstrap" and _text(rule.get("page_capture_id"))
    }
    duplicated = sorted(
        {
            _text(rule.get("rule_code") or rule.get("rule_id"))
            for rule in rules
            if _text(rule.get("source")) == "bootstrap"
            and _text(rule.get("rule_code") or rule.get("rule_id")) in real_result_codes
        }
    )
    if duplicated:
        return AcceptanceCheck(
            "NO_BOOTSTRAP_DUPLICATE",
            False,
            f"同一规则同时存在真实结果和 bootstrap 结果：{', '.join(duplicated[:10])}",
        )
    return AcceptanceCheck("NO_BOOTSTRAP_DUPLICATE", True, "未发现 bootstrap 重复结果")


def _check_usage_cost_trace(run: dict[str, Any]) -> AcceptanceCheck:
    usage = run.get("usage") if isinstance(run.get("usage"), dict) else {}
    token_total = (
        usage.get("total_tokens") or usage.get("input_tokens") or usage.get("output_tokens")
    )
    cost_total = usage.get("total_cost")
    if token_total is None:
        return AcceptanceCheck("USAGE_COST_TRACE", False, "Run 缺少 token 使用量")
    if cost_total is None:
        return AcceptanceCheck("USAGE_COST_TRACE", False, "Run 缺少成本字段 total_cost")
    return AcceptanceCheck("USAGE_COST_TRACE", True, "Token 与成本字段可追溯")


def _check_page_exploration(task: dict[str, Any], report: dict[str, Any]) -> AcceptanceCheck:
    page_exploration = report.get("page_exploration")
    if page_exploration is None:
        return AcceptanceCheck("PAGE_EXPLORATION", False, "报告缺少 page_exploration")
    if not isinstance(page_exploration, dict):
        return AcceptanceCheck("PAGE_EXPLORATION", False, "page_exploration 结构非法")
    coverage = page_exploration.get("coverage")
    coverage = coverage if isinstance(coverage, dict) else {}
    summary = report.get("summary")
    summary = summary if isinstance(summary, dict) else {}
    if coverage.get("acceptance_complete") is True and (
        page_exploration.get("complete") is not True or summary.get("task_goal_completed") is False
    ):
        return AcceptanceCheck(
            "PAGE_EXPLORATION",
            False,
            "动态覆盖声称完成，但页面/Frontier 或任务目标尚未收口",
        )
    if page_exploration.get("complete") is False and not (
        _text(task.get("status")) == "review"
        and _text(page_exploration.get("quality_status")) == "needs_review"
    ):
        return AcceptanceCheck("PAGE_EXPLORATION", False, "页面探索摘要仍为 incomplete")
    page_total = page_exploration.get("page_total")
    if page_exploration.get("tracking_available") is True and page_total in {None, 0}:
        return AcceptanceCheck("PAGE_EXPLORATION", False, "开启探索追踪但 page_total 为空")
    return AcceptanceCheck("PAGE_EXPLORATION", True, "页面探索摘要可用于验收")


def _check_popup_chain(
    run: dict[str, Any],
    report: dict[str, Any],
    *,
    popup_action_traces: dict[str, Any] | None,
    expect_popup_chain: bool,
    min_closed_layers: int,
) -> AcceptanceCheck:
    phase = _find_popup_chain_phase(report)
    if phase is None and not expect_popup_chain:
        return AcceptanceCheck("POPUP_CHAIN_CLOSURE", True, "未要求连续弹窗专项验收")
    if phase is None:
        return _check_popup_chain_from_runtime_trace(
            run,
            report,
            popup_action_traces=popup_action_traces,
            min_closed_layers=min_closed_layers,
        )

    steps = _popup_chain_steps(phase)
    closed_steps = [
        step for step in steps if _is_popup_close_step(step) and _has_popup_close_evidence(step)
    ]
    if len(closed_steps) < min_closed_layers:
        return AcceptanceCheck(
            "POPUP_CHAIN_CLOSURE",
            False,
            f"连续弹窗关闭证据不足：closed_layers={len(closed_steps)} required={min_closed_layers}",
        )

    status = _text(
        phase.get("status")
        or phase.get("phase_status")
        or phase.get("execution_status")
        or phase.get("closure_status"),
    )
    reason_code = _text(phase.get("reason_code") or phase.get("finish_reason"))
    stable_end = bool(
        phase.get("stable_end") or phase.get("popup_chain_cleared") or phase.get("final_stable"),
    )
    if status not in {"completed", "done", "pass", "success"} and not stable_end:
        return AcceptanceCheck(
            "POPUP_CHAIN_CLOSURE",
            False,
            f"连续弹窗未进入稳定终态：status={status or '<empty>'} "
            f"reason={reason_code or '<empty>'}",
        )

    return AcceptanceCheck(
        "POPUP_CHAIN_CLOSURE",
        True,
        f"连续弹窗关闭链路有效：closed_layers={len(closed_steps)}",
    )


_POPUP_CHAIN_FAILURE_CODES = frozenset(
    {
        "PAGE_OBSTRUCTION_RECOVERY_EXHAUSTED",
        "PAGE_OBSTRUCTION_CLEAR_LIMIT_REACHED",
    }
)


def _check_popup_chain_from_runtime_trace(
    run: dict[str, Any],
    report: dict[str, Any],
    *,
    popup_action_traces: dict[str, Any] | None = None,
    min_closed_layers: int,
) -> AcceptanceCheck:
    persisted_result = _check_popup_chain_from_persisted_actions(
        report,
        popup_action_traces=popup_action_traces,
        min_closed_layers=min_closed_layers,
    )
    if persisted_result is not None:
        return persisted_result
    page_results = _page_results(report)
    if not page_results:
        return AcceptanceCheck(
            "POPUP_CHAIN_CLOSURE",
            False,
            "W47 连续弹窗验收缺少 popup_chain 阶段和页面探索结果",
        )

    clear_steps = _obstruction_clear_steps(run)
    if not clear_steps:
        return AcceptanceCheck(
            "POPUP_CHAIN_CLOSURE",
            False,
            "W47 连续弹窗验收缺少清障动作 trace",
        )

    result_by_superseded = {
        _text(item.get("supersedes_page_capture_id")): item
        for item in page_results
        if _text(item.get("supersedes_page_capture_id"))
    }
    closed_layers = []
    missing_evidence = []
    for step in clear_steps:
        source_capture_id = _text(step.get("supersedes_page_capture_id"))
        result_page = result_by_superseded.get(source_capture_id)
        if result_page is None:
            missing_evidence.append(source_capture_id or "<empty>")
            continue
        if not _page_artifacts(result_page):
            missing_evidence.append(source_capture_id or "<empty>")
            continue
        closed_layers.append(result_page)

    if missing_evidence:
        return AcceptanceCheck(
            "POPUP_CHAIN_CLOSURE",
            False,
            f"连续弹窗关闭缺少后继 Capture 或 Artifact：{', '.join(missing_evidence[:5])}",
        )
    if len(closed_layers) < min_closed_layers:
        return AcceptanceCheck(
            "POPUP_CHAIN_CLOSURE",
            False,
            f"连续弹窗清障层数不足：closed_layers={len(closed_layers)} "
            f"required={min_closed_layers}",
        )

    terminal_page = _terminal_popup_chain_page(closed_layers)
    terminal_state = _text(terminal_page.get("page_state"))
    failure_code = _text(terminal_page.get("failure_code"))
    if (
        terminal_state in {"popup", "modal", "overlay", "dialog"}
        or failure_code in _POPUP_CHAIN_FAILURE_CODES
    ):
        return AcceptanceCheck(
            "POPUP_CHAIN_CLOSURE",
            False,
            "连续弹窗清障未到稳定页："
            f"closed_layers={len(closed_layers)} "
            f"terminal_state={terminal_state or '<empty>'} "
            f"failure_code={failure_code or '<empty>'}",
        )

    return AcceptanceCheck(
        "POPUP_CHAIN_CLOSURE",
        True,
        f"连续弹窗真实清障链路有效：closed_layers={len(closed_layers)}",
    )


def _check_popup_chain_from_persisted_actions(
    report: dict[str, Any],
    *,
    popup_action_traces: dict[str, Any] | None = None,
    min_closed_layers: int,
) -> AcceptanceCheck | None:
    """用 H5 V2 持久化 Action/Capture/Artifact 核验连续弹窗关闭。"""

    page_results = _page_results(report)
    evidence_page_results = _evidence_page_results(report)
    page_by_id = {
        _text(item.get("page_capture_id")): item
        for item in [*page_results, *evidence_page_results]
        if _text(item.get("page_capture_id"))
    }
    page_exploration = report.get("page_exploration")
    if not isinstance(page_exploration, dict):
        return None
    raw_actions = page_exploration.get("action_results")
    if not isinstance(raw_actions, list):
        return None
    ordered_actions = [item for item in raw_actions if isinstance(item, dict)]
    ordered_actions.sort(key=_action_command_sequence)
    popup_actions = []
    for item in ordered_actions:
        parameters = item.get("parameters")
        if not isinstance(parameters, dict):
            continue
        if _text(parameters.get("target_mode")) != "popup_dismissal":
            continue
        popup_actions.append(item)
    if not popup_actions:
        return None
    chains: list[list[dict[str, Any]]] = []
    current_chain: list[dict[str, Any]] = []
    missing_evidence: list[str] = []
    previous_result_id: str | None = None
    previous_action_sequence = 0
    for action in popup_actions:
        action_id = _text(action.get("exploration_action_id")) or "<empty>"
        requested_id = _text(action.get("requested_source_capture_id"))
        result_id = _text(action.get("result_page_capture_id"))
        action_sequence = _action_command_sequence(action)
        result_page = _persisted_action_result_page(
            action,
            page_by_id=page_by_id,
            require_popup_requested=True,
        )
        if result_page is None:
            missing_evidence.append(action_id)
            continue
        if current_chain and requested_id != previous_result_id:
            bridge_exists = any(
                previous_action_sequence < _action_command_sequence(candidate) < action_sequence
                and _text(candidate.get("result_page_capture_id")) == requested_id
                and _persisted_action_result_page(
                    candidate,
                    page_by_id=page_by_id,
                    require_popup_requested=False,
                )
                is not None
                for candidate in ordered_actions
            )
            if not bridge_exists:
                missing_evidence.append(action_id)
                continue
            chains.append(current_chain)
            current_chain = []
        current_chain.append(action)
        previous_result_id = result_id
        previous_action_sequence = action_sequence
    if current_chain:
        chains.append(current_chain)

    if missing_evidence:
        return AcceptanceCheck(
            "POPUP_CHAIN_CLOSURE",
            False,
            "连续弹窗持久化动作缺少完成态、请求页/动作前/动作后 Capture、"
            "Artifact 或父子连续性："
            f"{', '.join(missing_evidence[:5])}",
        )
    max_closed_layers = max(
        (
            sum(
                _persisted_popup_action_layer_count(
                    action,
                    page_by_id=page_by_id,
                    popup_action_traces=popup_action_traces,
                )
                for action in chain
            )
            for chain in chains
        ),
        default=0,
    )
    if max_closed_layers < min_closed_layers:
        return AcceptanceCheck(
            "POPUP_CHAIN_CLOSURE",
            False,
            f"连续弹窗清障层数不足：max_closed_layers={max_closed_layers} "
            f"required={min_closed_layers}",
        )
    for chain in chains:
        terminal_action = chain[-1]
        terminal_page = page_by_id[_text(terminal_action.get("result_page_capture_id"))]
        terminal_state = _text(terminal_page.get("page_state"))
        failure_code = _text(terminal_page.get("failure_code"))
        if terminal_state in {"popup", "modal", "overlay", "dialog"}:
            terminal_sequence = _action_command_sequence(terminal_action)
            terminal_result_id = _text(terminal_action.get("result_page_capture_id"))
            recovery_action = next(
                (
                    candidate
                    for candidate in ordered_actions
                    if _action_command_sequence(candidate) > terminal_sequence
                    and _text(candidate.get("requested_source_capture_id")) == terminal_result_id
                    and _text(candidate.get("action")) in {"back", "goto"}
                    and _persisted_action_result_page(
                        candidate,
                        page_by_id=page_by_id,
                        require_popup_requested=True,
                    )
                    is not None
                ),
                None,
            )
            if recovery_action is not None:
                terminal_page = page_by_id[_text(recovery_action.get("result_page_capture_id"))]
                terminal_state = _text(terminal_page.get("page_state"))
                failure_code = _text(terminal_page.get("failure_code"))
        if (
            terminal_state in {"popup", "modal", "overlay", "dialog"}
            or failure_code in _POPUP_CHAIN_FAILURE_CODES
        ):
            return AcceptanceCheck(
                "POPUP_CHAIN_CLOSURE",
                False,
                "连续弹窗持久化清障未到稳定页："
                f"chain_layers={len(chain)} "
                f"terminal_state={terminal_state or '<empty>'} "
                f"failure_code={failure_code or '<empty>'}",
            )
    return AcceptanceCheck(
        "POPUP_CHAIN_CLOSURE",
        True,
        "连续弹窗持久化清障链路有效："
        f"chain_count={len(chains)} max_closed_layers={max_closed_layers}",
    )


def _persisted_popup_action_layer_count(
    action: dict[str, Any],
    *,
    page_by_id: dict[str, dict[str, Any]],
    popup_action_traces: dict[str, Any] | None,
) -> int:
    """按结果 Capture 绑定的 action_trace 证据统计一次清障内的真实点击层数。"""
    if not popup_action_traces:
        return 1
    result_page = page_by_id.get(_text(action.get("result_page_capture_id")))
    if result_page is None:
        return 1
    artifact_ids = result_page.get("artifact_ids")
    if not isinstance(artifact_ids, list | tuple):
        return 1
    layer_indexes: set[int] = set()
    for artifact_id in artifact_ids:
        trace = popup_action_traces.get(_text(artifact_id))
        if not isinstance(trace, list):
            continue
        for entry in trace:
            if not isinstance(entry, dict):
                continue
            raw_index = entry.get("obstruction_chain_index")
            if isinstance(raw_index, bool) or not isinstance(raw_index, int):
                continue
            if raw_index > 0 and _text(entry.get("action")) in {"click", "tap"}:
                layer_indexes.add(raw_index)
    return max(len(layer_indexes), 1)


def _obstruction_clear_steps(run: dict[str, Any]) -> list[dict[str, Any]]:
    steps = []
    trace = run.get("action_trace")
    if not isinstance(trace, list):
        return steps
    for item in trace:
        if not isinstance(item, dict):
            continue
        if _text(item.get("tool_name")) != "page_capture_tool":
            continue
        if _text(item.get("status")) != "ok":
            continue
        arguments = _json_object(item.get("arguments"))
        if arguments.get("clear_obstructions") is not True:
            continue
        if _text(arguments.get("trigger_action")) not in {"click", "tap"}:
            continue
        steps.append(arguments)
    return steps


def _page_results(report: dict[str, Any]) -> list[dict[str, Any]]:
    page_exploration = report.get("page_exploration")
    if not isinstance(page_exploration, dict):
        return []
    results = page_exploration.get("page_results")
    if not isinstance(results, list):
        return []
    return [item for item in results if isinstance(item, dict)]


def _evidence_page_results(report: dict[str, Any]) -> list[dict[str, Any]]:
    page_exploration = report.get("page_exploration")
    if not isinstance(page_exploration, dict):
        return []
    results = page_exploration.get("evidence_page_results")
    if not isinstance(results, list):
        return []
    return [item for item in results if isinstance(item, dict)]


def _action_command_sequence(action: dict[str, Any]) -> int:
    value = action.get("command_sequence")
    if isinstance(value, bool):
        return 0
    try:
        sequence = int(value)
    except (TypeError, ValueError):
        return 0
    return sequence if sequence > 0 else 0


def _persisted_action_result_page(
    action: dict[str, Any],
    *,
    page_by_id: dict[str | None, dict[str, Any]],
    require_popup_requested: bool,
) -> dict[str, Any] | None:
    requested_id = _text(action.get("requested_source_capture_id"))
    source_id = _text(action.get("source_page_capture_id"))
    result_id = _text(action.get("result_page_capture_id"))
    requested_page = page_by_id.get(requested_id)
    source_page = page_by_id.get(source_id)
    result_page = page_by_id.get(result_id)
    if (
        _text(action.get("status")) != "explored"
        or _text(action.get("execution_status")) != "completed"
        or _action_command_sequence(action) <= 0
        or requested_page is None
        or source_page is None
        or result_page is None
        or (
            require_popup_requested
            and _text(requested_page.get("page_state"))
            not in {"popup", "modal", "overlay", "dialog"}
        )
        or not _page_artifacts(requested_page)
        or not _page_artifacts(source_page)
        or not _page_artifacts(result_page)
        or _text(source_page.get("parent_page_capture_id")) != requested_id
        or _text(result_page.get("parent_page_capture_id")) != source_id
    ):
        return None
    return result_page


def _page_artifacts(page: dict[str, Any]) -> list[object]:
    artifacts = page.get("artifact_ids") or page.get("evidence_artifact_ids")
    if isinstance(artifacts, list | tuple):
        return list(artifacts)
    return []


def _terminal_popup_chain_page(pages: list[dict[str, Any]]) -> dict[str, Any]:
    return max(pages, key=lambda item: int(item.get("page_seq") or 0))


def _json_object(value: object) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str):
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _find_popup_chain_phase(report: dict[str, Any]) -> dict[str, Any] | None:
    for phase in _iter_nested_dicts(report):
        phase_code = _text(
            phase.get("phase")
            or phase.get("phase_key")
            or phase.get("phase_type")
            or phase.get("surface_phase")
            or phase.get("page_type"),
        )
        if phase_code == "popup_chain":
            return phase
    return None


def _popup_chain_steps(phase: dict[str, Any]) -> list[dict[str, Any]]:
    for key in (
        "steps",
        "actions",
        "layers",
        "close_steps",
        "closure_steps",
        "close_attempts",
    ):
        value = phase.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


def _is_popup_close_step(step: dict[str, Any]) -> bool:
    action = _text(
        step.get("action")
        or step.get("action_type")
        or step.get("operation")
        or step.get("step_type"),
    )
    target = _text(step.get("target") or step.get("target_role"))
    return action in {"click", "tap", "close", "popup_close"} or target == "close"


def _has_popup_close_evidence(step: dict[str, Any]) -> bool:
    before_capture_id = _text(
        step.get("before_capture_id")
        or step.get("source_capture_id")
        or step.get("page_capture_id"),
    )
    after_capture_id = _text(
        step.get("after_capture_id")
        or step.get("result_capture_id")
        or step.get("next_capture_id"),
    )
    artifacts = step.get("evidence_artifact_ids") or step.get("artifact_ids")
    has_artifacts = isinstance(artifacts, list | tuple) and bool(artifacts)
    return bool(before_capture_id and after_capture_id and has_artifacts)


def _iter_nested_dicts(payload: object) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        nested = [payload]
        for value in payload.values():
            nested.extend(_iter_nested_dicts(value))
        return nested
    if isinstance(payload, list):
        nested = []
        for item in payload:
            nested.extend(_iter_nested_dicts(item))
        return nested
    return []


def _rule_identity(rule: dict[str, Any], reason: str) -> str:
    rule_code = _text(rule.get("rule_code") or rule.get("rule_id")) or "<unknown>"
    status = _text(rule.get("execution_status")) or "<unknown>"
    return f"{rule_code}[{status}] {reason}"


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _print_checks(checks: list[AcceptanceCheck]) -> None:
    passed_count = sum(1 for item in checks if item.passed)
    print(f"H5 acceptance checks: {passed_count}/{len(checks)} passed")
    for check in checks:
        marker = "PASS" if check.passed else "FAIL"
        print(f"[{marker}] {check.code}: {check.message}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", help="Alioth 服务地址，例如 http://127.0.0.1")
    parser.add_argument("--task-id", help="任务 ID；配合 --base-url 使用")
    parser.add_argument("--agent-run-id", help="Agent Run ID；配合 --base-url 使用")
    parser.add_argument("--access-token", help="访问受保护 API 的 Bearer Token")
    parser.add_argument("--username", help="登录用户名；配合 --password-env 使用")
    parser.add_argument("--password-env", help="保存登录密码的环境变量名")
    parser.add_argument("--task-json", help="任务详情 JSON 文件")
    parser.add_argument("--run-json", help="Agent Run JSON 文件")
    parser.add_argument("--rules-json", help="规则执行列表 JSON 文件")
    parser.add_argument("--report-json", help="报告详情 JSON 文件")
    parser.add_argument("--timeline-json", help="H5 页面 Timeline JSON 文件")
    parser.add_argument("--inspection-work-json", help="Inspection Work JSON 文件")
    parser.add_argument(
        "--expect-popup-chain",
        action="store_true",
        help="启用 W47 连续弹窗关闭链路专项验收",
    )
    parser.add_argument(
        "--min-popup-layers",
        type=int,
        default=2,
        help="连续弹窗专项验收要求的最少关闭层数，默认 2",
    )
    parser.add_argument(
        "--allow-review",
        action="store_true",
        help="只校验技术收口，允许 review/预览报告；默认要求业务验收完成",
    )
    args = parser.parse_args(argv)

    try:
        if args.base_url:
            if not args.task_id or not args.agent_run_id:
                parser.error("--base-url 需要同时提供 --task-id 和 --agent-run-id")
            access_token = _resolve_access_token(
                base_url=args.base_url,
                access_token=args.access_token,
                username=args.username,
                password_env=args.password_env,
            )
            task, run, rule_executions, report, timeline, inspection_work = fetch_api_payloads(
                base_url=args.base_url,
                task_id=args.task_id,
                agent_run_id=args.agent_run_id,
                access_token=access_token,
            )
        else:
            required = (
                args.task_json,
                args.run_json,
                args.rules_json,
                args.report_json,
                args.timeline_json,
                args.inspection_work_json,
            )
            if not all(required):
                parser.error("离线模式需要 task/run/rules/report/timeline/inspection-work JSON")
            task = load_json_file(args.task_json)
            run = load_json_file(args.run_json)
            rule_executions = load_json_file(args.rules_json)
            report = load_json_file(args.report_json)
            timeline = load_json_file(args.timeline_json)
            inspection_work = load_json_file(args.inspection_work_json)
    except (OSError, ValueError, urllib.error.URLError) as exc:
        print(f"加载验收数据失败：{exc}", file=sys.stderr)
        return 2

    checks = verify_acceptance_payloads(
        task=task,
        run=run,
        rule_executions=rule_executions,
        report=report,
        timeline=timeline,
        inspection_work=inspection_work,
        expect_popup_chain=args.expect_popup_chain,
        min_popup_layers=args.min_popup_layers,
        require_business_completion=not args.allow_review,
    )
    _print_checks(checks)
    return 0 if all(item.passed for item in checks) else 1


def _resolve_access_token(
    *,
    base_url: str,
    access_token: str | None,
    username: str | None,
    password_env: str | None,
) -> str | None:
    token = _text(access_token)
    if token:
        return token
    if not username and not password_env:
        return None
    if not username or not password_env:
        raise ValueError("--username 和 --password-env 必须同时提供")
    password = os.getenv(password_env)
    if not password:
        raise ValueError(f"环境变量 {password_env} 未设置或为空")
    return login_for_access_token(
        base_url=base_url,
        username=username,
        password=password,
    )


if __name__ == "__main__":
    raise SystemExit(main())
