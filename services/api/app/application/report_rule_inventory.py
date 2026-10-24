"""报告全规则清单投影：适用性处置与真实规则执行分开计数。"""

from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
from html import escape
from typing import Any

from services.api.app.application.task_progress import (
    page_type_display_name,
    rule_display_label,
    rule_execution_counts_as_used,
    rule_package_fallback,
    severity_display,
    trigger_display_fallback,
)
from services.api.app.application.task_rule_inventory import (
    assess_rule_application,
    inventory_execution_contract_hash,
)

_LEAD_ORDER = ("failed", "detect_error", "other", "coverage_gap", "passed", "not_applicable")
_AGGREGATE_NA_REASON = "AGGREGATE_RULE_SCOPE_NOT_APPLICABLE"
_LABELS = {
    "passed": "通过",
    "failed": "未通过",
    "not_applicable": "本产品不适用",
    "coverage_gap": "计划外未覆盖",
    "detect_error": "检测异常",
    "other": "其他",
}
_REASONS = {
    "platform_not_supported": "已核对目标平台：规则声明不支持本任务平台，未进行规则判定。",
    "channel_not_supported": "已核对任务渠道：规则不适用于本任务渠道，未进行规则判定。",
    "inspection_scope_excluded": (
        "已核对任务检查范围：规则适用页型未被本任务选择，未执行；这不表示产品不适用。"
    ),
    "auth_requirement_not_met": "未取得规则所需的登录证据，适用性尚未证明，未进行规则判定。",
}


def project_report_rule_inventory(
    insight: dict[str, Any],
    snapshot: dict[str, Any] | None,
) -> None:
    """仅补充可审计清单行，不生成执行 ID，不改变已有执行质检率。"""

    if snapshot is None:
        return
    summary = insight["summary"]
    status = snapshot.get("inventory_status") or "unavailable"
    summary["rule_inventory_status"] = status
    inventory = snapshot.get("inventory")
    if not isinstance(inventory, dict) or status not in {"frozen", "legacy_verified"}:
        summary["rule_inventory_complete"] = False
        summary["rule_inventory_pending_rules"] = summary.get("ruleset_rule_total") or 0
        summary["rule_inventory_unavailable_reason"] = snapshot.get("unavailable_reason")
        return
    rules = inventory.get("rules") or []
    items = insight["rule_items"]
    by_rule: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in items:
        item["item_type"] = "execution"
        by_rule[str(item["rule_id"])].append(item)
    work_by_rule: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for work in snapshot.get("work_rules") or []:
        work_by_rule[str(work.get("rule_id") or "")].append(work)
    aggregate_dispositions = _aggregate_dispositions(snapshot)
    pending = 0
    selected_ids = set()
    owner_execution_ids = {str(item["execution_id"]) for item in snapshot.get("executions") or []}
    verified_execution_ids = set()
    execution_facts = {str(item["execution_id"]): item for item in snapshot.get("executions") or []}
    for rule in rules:
        rule_id = str(rule["rule_id"])
        selected_ids.add(rule_id)
        application = assess_rule_application(
            rule,
            target_platform=inventory["target_platform"],
            channel=inventory["channel"],
            inspection_scope=inventory["inspection_scope"],
            blocked_auth_requirement=snapshot.get("auth_gate"),
        )
        existing = by_rule.get(rule_id) or []
        owner_existing = [
            item
            for item in existing
            if str(item.get("execution_id")) in owner_execution_ids and _actual_execution(item)
        ]
        if inventory["target_platform"] == "h5" and status == "frozen":
            expected_contract = inventory_execution_contract_hash(rule)
            owner_existing = [
                item
                for item in owner_existing
                if execution_facts[str(item["execution_id"])].get("proof_contract_hash")
                == expected_contract
            ]
        verified_execution_ids.update(str(item["execution_id"]) for item in owner_existing)
        disposition = aggregate_dispositions.get(rule_id) if not owner_existing else None
        if disposition is not None and application["applicability"] in {"applicable", "unproven"}:
            application = {
                "applicability": "not_applicable",
                "reason_code": _AGGREGATE_NA_REASON,
            }
        else:
            disposition = None
        audit = {
            **application,
            "rule_code": rule.get("rule_code") or "",
            "rule_name": rule.get("name") or rule.get("rule_name") or rule.get("rule_code") or "",
            "category": rule.get("category"),
            "check_point": rule.get("check_point"),
            "rule_package": rule.get("rule_package") or rule_package_fallback(rule.get("category")),
            "trigger_display": rule.get("trigger_display")
            or trigger_display_fallback(rule.get("applicable_page_types") or []),
            "inventory_source": status,
            "inventory_hash": inventory.get("inventory_hash"),
            "rule_contract_hash": rule.get("contract_hash"),
            "source_agent_run_id": snapshot.get("agent_run_id"),
            "finalization_epoch": snapshot.get("finalization_epoch"),
        }
        audit.update({key: rule[key] for key in ("ruleset_code", "ruleset_name") if rule.get(key)})
        for item in existing:
            item.update(audit)
        if owner_existing:
            if (
                (inventory["target_platform"] != "h5"
                 or application["applicability"] in {"applicable", "unproven"})
                and (application["applicability"] == "unproven"
                     or not all(_execution_conclusive(item) for item in owner_existing))
            ):
                pending += 1
            continue
        item = _candidate(rule, application, snapshot, work_by_rule.get(rule_id) or [])
        trigger_reason = item["execution_reason"]
        item.update(audit)
        if disposition is not None:
            explanation = (
                "已核对产品适用范围："
                + disposition["reason"].rstrip("。")
                + "。范围依据："
                + "、".join(disposition["evidence_refs"])
                + "。未进行 AI 判定。"
            )
            item.update(result_summary=explanation, problem_desc=explanation)
        if inventory["target_platform"] == "h5":
            item["reason_code"] = trigger_reason
        if existing and application["applicability"] in {"applicable", "unproven"}:
            item.update(
                machine_display_status="evidence_insufficient",
                machine_display_status_label=rule_display_label("evidence_insufficient"),
                display_status="other",
                display_status_label=_LABELS["other"],
                execution_reason="execution_lineage_unverified",
                reason_code="execution_lineage_unverified",
                result_summary="已有执行记录未通过当前主运行或收口轮次关联核验，不能据此认定本次范围已完成；原始记录保留在详情中。",
                problem_desc="已有执行记录未通过当前主运行或收口轮次关联核验，不能据此认定本次范围已完成；原始记录保留在详情中。",
            )
        items.append(item)
        by_rule[rule_id].append(item)
        if application["applicability"] in {"applicable", "unproven"}:
            pending += 1
    unexpected_ids = set(by_rule) - selected_ids
    summary.update(
        {
            "ruleset_rule_total": len(rules),
            "rule_inventory_total": len(rules),
            "used_rule_count": sum(
                any(
                    _actual_execution(item)
                    and (inventory["target_platform"] != "h5"
                         or str(item["execution_id"]) in verified_execution_ids)
                    for item in group
                ) for group in by_rule.values()
            ),
            "rule_inventory_pending_rules": pending,
            "scope_excluded_rules": sum(
                rule.get("applicability") == "out_of_scope" for rule in rules
            ),
            "scope_not_applicable_rules": sum(
                rule.get("applicability") == "not_applicable" for rule in rules
            ),
            "rule_inventory_complete": pending == 0 and not unexpected_ids,
            "rule_inventory_unexpected_rule_ids": sorted(unexpected_ids),
        }
    )
    counts = {key: 0 for key in _LABELS}
    for group in by_rule.values():
        lead = _rule_lead(group)
        counts[lead["display_status"]] += 1
    insight["distribution"] = [
        {
            "status": key,
            "label": _LABELS[key],
            "count": count,
            "percent": round(count / len(by_rule) * 100, 1) if by_rule else 0.0,
        }
        for key, count in counts.items()
    ]


def _aggregate_dispositions(snapshot: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """仅消费仓储已核验同源范围依据的正式不适用处置，不接受待复核或缺证候选。"""
    return {
        item["rule_id"]: item
        for item in snapshot.get("aggregate_dispositions") or []
        if isinstance(item, dict)
        and item.get("status") == "not_applicable"
        and item.get("reason_code") == _AGGREGATE_NA_REASON
        and item.get("rule_id")
        and item.get("candidate_id")
        and isinstance(item.get("reason"), str)
        and item["reason"].strip()
        and isinstance(item.get("evidence_refs"), list)
        and item["evidence_refs"]
        and all(isinstance(ref, str) and ref.strip() for ref in item["evidence_refs"])
    }


def _rule_lead(group: list[dict[str, Any]]) -> dict[str, Any]:
    # 当前权威不适用可完成范围核对，旧轮次的原始执行仍保留在明细中。
    disposition = next(
        (
            item
            for item in group
            if item.get("item_type") == "candidate"
            and item.get("reason_code") == _AGGREGATE_NA_REASON
            and item.get("display_status") == "not_applicable"
        ),
        None,
    )
    if disposition is not None:
        return disposition
    # 原始未执行留痕不能覆盖冻结清单已证明的平台、渠道或本次检查范围处置。
    if not any(_actual_execution(item) for item in group):
        scope_disposition = next(
            (
                item
                for item in group
                if item.get("item_type") == "candidate"
                and item.get("applicability") in {"not_applicable", "out_of_scope"}
            ),
            None,
        )
        if scope_disposition is not None:
            return scope_disposition
    return min(group, key=lambda item: _LEAD_ORDER.index(item["display_status"]))


def _actual_execution(item: dict[str, Any]) -> bool:
    return bool(item.get("execution_id")) and rule_execution_counts_as_used(
        item.get("execution_status"),
        item.get("execution_reason"),
        is_bootstrap=bool(item.get("is_bootstrap")),
        machine_display_status=item.get("machine_display_status"),
    )


def _execution_conclusive(item: dict[str, Any]) -> bool:
    return (
        _actual_execution(item)
        and not item.get("is_bootstrap")
        and item.get("machine_display_status")
        in {"passed", "failed", "not_detected", "not_applicable", "manual_review"}
        and item.get("display_status") in {"passed", "failed", "not_applicable"}
        and item.get("confirm_status") != "pending"
    )


def project_task_rule_inventory(
    items: list[dict[str, Any]],
    snapshot: dict[str, Any] | None,
    *,
    ruleset_rule_total: int | None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """任务使用相同清单依据，保留细粒度执行态与既有页面候选，不重复造行。"""
    execution_items = deepcopy([item for item in items if item["item_type"] == "execution"])
    for item in execution_items:
        status = item["display_status"]
        item["display_status"] = (
            "not_applicable"
            if status == "not_detected"
            else status
            if status in _LABELS
            else "other"
        )
    projection = {
        "summary": {"ruleset_rule_total": ruleset_rule_total},
        "rule_items": execution_items,
    }
    project_report_rule_inventory(projection, snapshot)
    summary = projection["summary"]
    summary.pop("ruleset_rule_total", None)
    existing: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in items:
        existing[str(item["rule_id"])].append(item)
    for projected in projection["rule_items"]:
        if projected.get("item_type") != "candidate":
            for original in existing[str(projected["rule_id"])]:
                if original.get("execution_id") == projected.get("execution_id"):
                    original.update(
                        {
                            key: projected.get(key)
                            for key in (
                                "rule_code",
                                "rule_name",
                                "category",
                                "check_point",
                                "rule_package",
                                "trigger_display",
                                "ruleset_code",
                                "ruleset_name",
                                "applicability",
                                "reason_code",
                                "inventory_source",
                                "inventory_hash",
                                "rule_contract_hash",
                                "source_agent_run_id",
                                "finalization_epoch",
                            )
                        }
                    )
            continue
        rule_id = str(projected["rule_id"])
        metadata = {
            key: projected.get(key)
            for key in (
                "applicability",
                "reason_code",
                "inventory_source",
                "inventory_hash",
                "rule_contract_hash",
                "source_agent_run_id",
                "finalization_epoch",
                "rule_code",
                "rule_name",
                "category",
                "check_point",
                "rule_package",
                "trigger_display",
                "ruleset_code",
                "ruleset_name",
            )
        }
        if rule_id in existing:
            for item in existing[rule_id]:
                item.update(metadata)
                if item["item_type"] == "candidate":
                    item.update(
                        display_status=projected["machine_display_status"],
                        display_status_label=projected["machine_display_status_label"],
                        machine_display_status=projected["machine_display_status"],
                        machine_display_status_label=projected["machine_display_status_label"],
                        result_summary=projected["result_summary"],
                        problem_desc=projected["problem_desc"],
                        execution_reason=projected["execution_reason"],
                        confirmable=False,
                        confirm_status=None,
                    )
                else:
                    item["inventory_coverage_reason"] = projected["result_summary"]
            continue
        candidate = dict(projected)
        candidate.update(
            display_status=projected["machine_display_status"],
            display_status_label=projected["machine_display_status_label"],
            judge_strategy=None,
            review_status=None,
        )
        items.append(candidate)
        existing[rule_id].append(candidate)
    return items, summary


def append_report_rule_inventory(html_text: str, insight: dict[str, Any]) -> str:
    """导出清单处置与真实执行次数，候选只展示核对依据。"""
    summary = insight["summary"]
    if "rule_inventory_status" not in summary:
        return html_text
    by_rule: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in insight["rule_items"]:
        by_rule[str(item["rule_id"])].append(item)
    rows = []
    for group in by_rule.values():
        lead = _rule_lead(group)
        count = sum(_actual_execution(item) for item in group)
        state = _LABELS[lead["display_status"]]
        if not count:
            state += (
                "（适用性核对，未进行 AI 判定）"
                if lead.get("applicability") == "not_applicable"
                else "（未进行规则判定）"
            )
        values = [
            lead.get("rule_code"),
            lead.get("rule_name"),
            state,
            count,
            lead.get("problem_desc") or lead.get("result_summary") or "",
            lead.get("reason_code") or "",
        ]
        rows.append(
            "<tr>"
            + "".join(f"<td>{escape(str('' if value is None else value))}</td>" for value in values)
            + "</tr>"
        )
    note = escape(str(summary.get("rule_inventory_unavailable_reason") or ""))
    appendix = (
        '<section class="rule-inventory"><h2>全规则应用清单</h2>'
        f"<p>清单来源：{escape(str(summary['rule_inventory_status']))}。{note}"
        "适用性核对、未执行和证据不足不代表已完成 AI 质检。</p>"
        "<table><thead><tr><th>规则编号</th><th>规则名称</th><th>处置</th><th>真实执行次数</th>"
        "<th>依据与原因</th><th>原因代码</th></tr></thead><tbody>"
        + "".join(rows)
        + "</tbody></table></section>"
    )
    return (
        html_text.replace("</body>", appendix + "</body>")
        if "</body>" in html_text
        else html_text + appendix
    )


def _candidate(
    rule: dict[str, Any],
    application: dict[str, str],
    snapshot: dict[str, Any],
    work_rules: list[dict[str, Any]],
) -> dict[str, Any]:
    applicability = application["applicability"]
    reason = application["reason_code"]
    machine = {
        "not_applicable": "not_applicable",
        "out_of_scope": "coverage_gap",
        "unproven": "evidence_insufficient",
    }.get(applicability, "not_executed" if snapshot.get("terminal") else "pending")
    if applicability == "applicable" and any(
        work.get("rule_status") in {"error", "failed"} or work.get("work_status") == "failed"
        for work in work_rules
    ):
        machine = "detect_error"
    if (
        applicability == "applicable"
        and machine != "detect_error"
        and (snapshot.get("inventory") or {}).get("target_platform") == "h5"
    ):
        from services.api.app.application.h5_rule_inventory_reconciliation import (
            explain_h5_rule_trigger_gap,
        )

        reason, h5_explanation = explain_h5_rule_trigger_gap(rule, snapshot, work_rules)
    else:
        h5_explanation = None
    display = machine if machine in {"not_applicable", "coverage_gap", "detect_error"} else "other"
    explanation = h5_explanation or _REASONS.get(reason)
    if explanation is None:
        explanation = (
            "规则在任务范围内，但任务结束时未产生该规则的执行记录，尚未完成判定。"
            if snapshot.get("terminal")
            else "规则在任务范围内，尚未产生执行记录，等待执行。"
        )
        if machine == "detect_error":
            explanation = "规则候选工作出现异常，未产生有效执行结果；保留工作台账供核对。"
    page_type = rule.get("page_type")
    return {
        "item_type": "candidate",
        "execution_id": None,
        "rule_id": rule["rule_id"],
        "rule_code": rule.get("rule_code") or "",
        "rule_name": rule.get("name") or rule.get("rule_name") or rule.get("rule_code") or "",
        "category": rule.get("category"),
        "rule_package": rule.get("rule_package") or rule_package_fallback(rule.get("category")),
        "trigger_display": rule.get("trigger_display")
        or trigger_display_fallback(rule.get("applicable_page_types") or []),
        "check_point": rule.get("check_point"),
        "ruleset_code": rule.get("ruleset_code"),
        "ruleset_name": rule.get("ruleset_name"),
        "bound_page_type": None,
        "bound_page_type_name": None,
        "applicable_page_types": rule.get("applicable_page_types") or [],
        "applicable_page_type_name": page_type_display_name(page_type),
        "display_status": display,
        "display_status_label": (
            "本平台不适用"
            if reason == "platform_not_supported"
            else "本渠道不适用"
            if reason == "channel_not_supported"
            else _LABELS[display]
        ),
        "machine_display_status": machine,
        "machine_display_status_label": rule_display_label(machine),
        "execution_status": None,
        "execution_reason": reason,
        "severity": rule.get("severity"),
        "severity_display": severity_display(rule.get("severity")),
        "confidence": None,
        "result_summary": explanation,
        "problem_desc": explanation,
        "evidence_artifact_ids": [],
        "evidence_input_set_id": None,
        "missing_evidence": [],
        "page_capture_id": None,
        "is_bootstrap": False,
        "is_coverage_gap": machine == "coverage_gap",
        "finished_at": None,
        "confirmable": False,
        "confirm_status": None,
        "human_verdict": None,
        "human_verdict_label": None,
        "human_verdict_note": None,
        "human_verdict_by": None,
        "human_verdict_at": None,
        "work_rule_facts": work_rules,
    }
