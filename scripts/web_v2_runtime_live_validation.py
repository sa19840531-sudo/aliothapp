"""无静态白名单运行的实况身份、预算与证据校验。"""

from collections import Counter


def validate_runtime_live_site(site, context):
    """候选终态可审计，不要求每个菜单都必须被错误地认作详情页。"""
    from scripts.web_v2_six_site_shadow import (
        _safe_int,
        _valid_uuid_id,
        _validate_live_site_evidence_bundle,
        _validate_report_evidence_identity,
    )

    errors = []
    prefix = site.target_url
    summary = site.exploration_summary
    ledger = summary.get("menu_ledger", [])
    discoveries = summary.get("runtime_discovered_ledger", [])
    if not isinstance(ledger, list) or not isinstance(discoveries, list):
        return [f"{prefix}: runtime 账本必须为数组"]
    rows = [*ledger, *(row.get("execution", row) for row in discoveries if isinstance(row, dict))]
    if any(not isinstance(row, dict) for row in rows):
        return [f"{prefix}: runtime 账本含无效行"]
    ids = [row.get("entry_id") for row in rows]
    if len(ids) != len(set(ids)) or any(not value for value in ids):
        errors.append(f"{prefix}: runtime 账本身份重复或缺失")
    slots = [
        row.get("execution_slot") for row in discoveries if row.get("execution_slot") is not None
    ]
    if any(type(slot) is not int or slot <= 0 for slot in slots) or len(slots) != len(set(slots)):
        errors.append(f"{prefix}: runtime 槽位必须为唯一正整数")
    if "menu_limit" not in summary or summary.get("menu_limit") is not None:
        errors.append(f"{prefix}: menu_limit 必须显式为 NULL")
    if summary.get("frontier_remaining") != 0:
        errors.append(f"{prefix}: frontier_remaining 必须为 0")
    if _safe_int(summary.get("menu_limit_skipped_count")) != 0:
        errors.append(f"{prefix}: menu_limit_skipped_count 必须为 0")
    if _safe_int(summary.get("menu_total")) != len(rows):
        errors.append(f"{prefix}: runtime 总数与完整账本不一致")
    if dict(Counter(row.get("status") for row in rows)) != summary.get("status_counts"):
        errors.append(f"{prefix}: runtime 状态计数与完整账本不一致")
    for row in rows:
        if row.get("status") in {"pending", "home_pending", "menu_reserved", "detail_reserved"}:
            errors.append(f"{prefix}: runtime 存在未结算动作")
        if row.get("error_code") in {"WEB_MENU_ALLOWLIST_DRIFTED", "WEB_MENU_LIMIT_SKIPPED"}:
            errors.append(f"{prefix}: 新运行被旧白名单/计数门禁阻断")
        if row.get("menu_action_count") or row.get("status") == "home_baseline":
            try:
                same_origin = context.contains_url_origin(str(row.get("canonical_url") or ""))
            except ValueError:
                same_origin = False
            if not same_origin:
                errors.append(f"{prefix}: canonical_url 越过同源边界或无效")
    if not _valid_uuid_id(site.task_id) or not _valid_uuid_id(site.run_id):
        errors.append(f"{prefix}: Task/Run 身份无效")
    if site.failure_reason or site.task_status not in {"done", "completed"}:
        errors.append(f"{prefix}: 实况非成功终态 {site.task_status}")
    if site.report_stage != "final" or not site.is_final:
        errors.append(f"{prefix}: 报告尚未正式封存")
    menus = sum(_safe_int(row.get("menu_action_count")) for row in rows)
    details = sum(row.get("status") == "detail_captured" for row in rows)
    if _safe_int(summary.get("executed_same_origin_count")) != menus:
        errors.append(f"{prefix}: runtime 动作总数不一致")
    errors.extend(
        _validate_live_site_evidence_bundle(
            target_url=prefix,
            task_id=site.task_id,
            run_id=site.run_id,
            site_id=context.site_id,
            expected_menu_count=menus,
            expected_detail_count=details,
            bundle=site.evidence_bundle,
            allow_count_limit_skips=False,
        )
    )
    errors.extend(
        _validate_report_evidence_identity(
            site,
            require_navigation_identity=True,
            catalog_entry_ids=frozenset(entry.entry_id for entry in context.entries),
        )
    )
    return errors
