"""Web 六站验收六个独立维度，人工事实不得从系统确认计数推断。"""


def acceptance_dimensions(site):
    entries = site.exploration_summary.get("menu_ledger") or []
    ledger_known = bool(entries)
    from services.api.app.domain.exploration.web_exploration import WebMenuTerminalStatus

    ledger_closed = ledger_known and all(
        row.get("status") in {item.value for item in WebMenuTerminalStatus} for row in entries
    )
    identities = [row.get("logical_page_id") for row in site.logical_page_assessments]
    from scripts.web_v2_live_evidence import validate_live_site_evidence_bundle

    contract_known = bool(identities) and all(identities) and bool(site.evidence_bundle)
    contract_errors = (
        validate_live_site_evidence_bundle(
            target_url=site.target_url,
            task_id=site.task_id,
            run_id=site.run_id,
            site_id=str(site.exploration_summary.get("site_id") or ""),
            expected_menu_count=site.exploration_summary.get("executed_same_origin_count") or 0,
            bundle=site.evidence_bundle,
        )
        if contract_known
        else []
    )
    reviews = site.manual_page_reviews
    reviewed = {
        row.get("logical_page_id")
        for row in reviews
        if row.get("task_id") == site.task_id
        and row.get("run_id") == site.run_id
        and row.get("reviewed") is True
        and row.get("reviewer")
        and row.get("reviewed_at")
    }
    manual_complete = (
        bool(identities)
        and reviewed == set(identities)
        and site.task_list_report_verified
        and site.task_detail_report_verified
    )
    return {
        "contract_integrity": {
            "status": "passed"
            if contract_known and not contract_errors and site.duplicate_url_count == 0
            else "unverified",
            "evidence_contract_error_count": len(contract_errors),
            "reason": "按实际动作、身份、血缘和证据包合同校验；无完整材料不推断通过",
        },
        "actual_coverage": _actual_coverage(site, entries, ledger_closed),
        "ledger_integrity": {
            "status": "terminal_statuses_recorded"
            if ledger_closed and site.duplicate_url_count == 0
            else "unverified",
            "candidate_count": len(entries),
            "duplicate_count": site.duplicate_url_count,
            "reason": "终态统计不是完整账本合同证明；Catalog、候选与动作逐项校验见 validation_errors",
        },
        "manual_review": {
            "status": "completed" if manual_complete else "not_recorded",
            "reviewed_pages": len(reviewed),
            "reason": "本轮逐页人工记录及两个入口检查齐全"
            if manual_complete
            else "本轮缺明确逐页人工复核记录；系统确认清零与界面访问不能代替人工验收",
        },
        "server_gate": {
            "status": "passed" if site.server_completion_gate_passed is True else "not_passed",
            "pending_confirmations": site.pending_confirmation_count,
        },
        "report_binding": {
            "status": "final" if site.is_final and site.server_report_bound is True else "preview",
            "server_report_bound": site.server_report_bound,
        },
    }


def _actual_coverage(site, entries, ledger_closed):
    """冻结页型、菜单终态和质检队列分别计数，禁止跨实体相加或缩分母。"""
    from services.api.app.domain.page.web_page_catalog import WEB_PAGE_TYPE_CODES

    excluded = {"menu_limit_skipped", "cross_origin_blocked"}
    completed_entries = {"home_baseline", "detail_captured", "logical_target_reused", "menu_page_captured"}
    blocked = sum(row.get("status") not in excluded | completed_entries for row in entries)
    coverage = site.coverage_snapshot
    collections = ("required_page_types", "observed_page_types", "missing_page_types", "unrecognized_page_types")
    known = (
        coverage.get("task_id") == site.task_id
        and coverage.get("agent_run_id") == site.run_id
        and bool(coverage.get("coverage_snapshot_id"))
        and bool(coverage.get("snapshot_hash"))
        and type(coverage.get("finalization_epoch")) is int
        and coverage["finalization_epoch"] >= 0
        and all(isinstance(coverage.get(key), list) and all(isinstance(item, str) for item in coverage[key])
                and len(coverage[key]) == len(set(coverage[key])) for key in collections)
    )
    if known:
        required, observed, missing = (set(coverage[key]) for key in collections[:3])
        known = (
            bool(required) and (required | observed | missing) <= set(WEB_PAGE_TYPE_CODES)
            and missing == required - observed
            and coverage.get("coverage_status") in {"complete", "completed_with_gaps", "failed"}
            and (coverage["coverage_status"] == "failed"
                 or coverage["coverage_status"] == ("completed_with_gaps" if missing else "complete"))
        )
    queue_known = all(type(value) is int and value >= 0 for value in (
        site.inspection_total_count, site.inspection_pending_count, site.page_type_review_count
    )) and site.inspection_pending_count <= site.inspection_total_count
    complete = (
        known and not missing and not coverage["unrecognized_page_types"]
        and coverage["coverage_status"] == "complete" and ledger_closed and not blocked
        and queue_known and site.inspection_pending_count == 0 and site.page_type_review_count == 0
    )
    return {
        "status": "complete" if complete else "incomplete" if known else "unverified",
        "unit": "page_type",
        "required": len(required) if known else None,
        "completed": len(required & observed) if known else None,
        "missing": len(missing) if known else None,
        "unrecognized": len(coverage["unrecognized_page_types"]) if known else None,
        "coverage_snapshot_id": coverage.get("coverage_snapshot_id") if known else None,
        "snapshot_hash": coverage.get("snapshot_hash") if known else None,
        "finalization_epoch": coverage.get("finalization_epoch") if known else None,
        "needs_review": {"unit": "logical_page", "count": site.page_type_review_count},
        "menu_entries": {"unit": "menu_entry", "blocked": blocked,
                         "excluded": sum(row.get("status") in excluded for row in entries)},
        "inspection_queue": {"unit": "inspection_record", "total": site.inspection_total_count,
                             "pending": site.inspection_pending_count},
        "reason": "页型完成为同 Run 冻结 required 与 observed 交集；菜单排除不缩分母，队列排空不代表覆盖完成",
    }
