"""未封存报告消费既有绿色只读投影；不修改快照、执行或人工审计。"""

from collections.abc import Mapping

from services.api.app.application.green_rule_result_projection import (
    apply_green_results,
    green_result_has_material_gap,
    selected_green_rule_keys,
)


def report_uses_frozen_results(context):
    """沿报告现有封存语义；App全检的旧宽泛旗标不能替代正式报告资格。"""
    config = context.get("task_config")
    config = config if isinstance(config, Mapping) else {}
    platform = str(context.get("target_platform") or "").strip().casefold()
    app_full = (
        platform in {"app", "android"}
        and str(config.get("workflow_version") or "").strip().casefold() == "v2"
        and config.get("execution_mode") in (None, "full_inspection")
    )
    if app_full:
        return context.get("app_report_availability") == "final"
    return (
        context.get("finalization_state") == "sealed"
        and context.get("sealed_report_bound") is True
        and (platform not in {"web", "h5"} or context.get("completion_gate_passed") is True)
    )


async def project_open_green_report_results(
    *, repository, context, task_id, items, display_group, access_scope=None
):
    """只为已有正式执行读取；损坏或缺失关联使用既有失联占位，不生成候选结果。"""
    if report_uses_frozen_results(context):
        return
    expected = selected_green_rule_keys(context.get("task_config"))
    selected = [
        item for item in items if item.get("execution_id") and item.get("rule_id") in expected
    ]
    if not selected:
        return
    reader = getattr(repository, "read_green_rule_results", None)
    projections = (
        await reader(
            task_id=task_id,
            execution_ids=list(dict.fromkeys(item["execution_id"] for item in selected)),
            **({"access_scope": access_scope} if access_scope is not None else {}),
        )
        if callable(reader)
        else {}
    )
    _apply_report_results(selected, projections, expected, display_group)


async def project_green_confirmation_result(
    *, repository, context, task_id, item, display_group, access_scope
):
    """确认仅复用原审核对象范围；不将审核身份当作任务或原图读取权限。"""
    if report_uses_frozen_results(context):
        return
    expected = selected_green_rule_keys(context.get("task_config"))
    if item.get("rule_id") not in expected:
        return
    reader = getattr(repository, "read_green_confirmation_results", None)
    projections = (
        await reader(
            task_id=task_id,
            execution_ids=[item["execution_id"]],
            access_scope=access_scope,
        )
        if callable(reader) and access_scope is not None
        else {}
    )
    _apply_report_results([item], projections, expected, display_group)


def _apply_report_results(selected, projections, expected, display_group):
    for item in selected:
        original_machine = {
            key: item.get(key)
            for key in ("machine_display_status", "machine_display_status_label")
        }
        apply_green_results([item], projections, expected_rules=expected)
        # 当前复核可以限制公开结论，机器原状态和原人工结论仍作为审计保留。
        item.update(original_machine)
        green = item["green_result"]
        if green_result_has_material_gap(green):
            item.update(confirmable=False, confirm_status=None)
            if item["display_status"] == "passed":
                item["display_status"] = "evidence_insufficient"
        elif green.get("review_status") in {"pending", "needs_recheck"} and item["confirmable"]:
            item["confirm_status"] = "pending"
        item["display_status"] = display_group(item["display_status"])
        if green["linkage_status"] != "linked":
            item["display_status_label"] = "关联待核对"
        elif item["display_status"] == "other":
            item["display_status_label"] = (
                "证据不足" if green_result_has_material_gap(green) else "待复核"
            )


def green_report_item_is_resolved(item):
    green = item.get("green_result")
    return not isinstance(green, Mapping) or (
        not green_result_has_material_gap(green)
        and green.get("review_status") not in {"pending", "needs_recheck"}
        and item.get("display_status") in {"passed", "failed", "not_applicable"}
    )


def green_report_has_unresolved_results(items):
    return any(not green_report_item_is_resolved(item) for item in items)


def restrict_open_green_report_completion(insight):
    """未封存的绿色缺项独立限制完成和导出，不能依赖人工确认清单为空解锁。"""
    if not any(isinstance(item.get("green_result"), Mapping) for item in insight["rule_items"]):
        return
    incomplete = green_report_has_unresolved_results(insight["rule_items"])
    insight["green_results_incomplete"] = incomplete
    insight["green_results_notice"] = (
        "绿色规则仍有缺证、待复核或关联待核对，材料与复核完成前不能导出报告"
        if incomplete else None
    )
    if incomplete:
        insight["confirm_progress"]["completed"] = False
        insight["export_ready"] = False
        if "export_allowed" in insight:
            insight["export_allowed"] = False
