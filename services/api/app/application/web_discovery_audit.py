"""发现审计终态的固定只读合同，不将拒绝发现误记为执行成功。"""

WEB_DISCOVERY_AUDIT_REASONS = frozenset(
    {
        "WEB_DISCOVERY_DISABLED",
        "WEB_DISCOVERY_PERSISTENCE_REQUIRED",
        "WEB_DISCOVERY_CLOSEOUT_RESERVED",
        "WEB_DISCOVERY_CANDIDATE_STALE",
        "WEB_DISCOVERY_DIRECT_URL_REQUIRED",
        "WEB_DISCOVERY_BUDGET_EXHAUSTED",
        "WEB_DISCOVERY_IDENTITY_UNTRUSTED",
        "WEB_DISCOVERY_LOGIN_BLOCKED",
        "WEB_DISCOVERY_PAYMENT_BLOCKED",
        "WEB_DISCOVERY_DOWNLOAD_BLOCKED",
        "WEB_DISCOVERY_UNAUTHORIZED_ACTION_BLOCKED",
        "WEB_DISCOVERY_UNAUTHORIZED_ACTION",
        "WEB_DISCOVERY_CROSS_ORIGIN_BLOCKED",
        "WEB_SITE_HUMAN_VERIFICATION_BLOCKED",
        "WEB_DISCOVERY_UNKNOWN_OUTCOME_BLOCKED",
    }
)


def web_discovery_is_audit_only(row):
    """只有零动作、零槽位且原因已终结的发现记录可不参与执行成功判定。"""
    return (
        row.get("source_kind") == "runtime_discovered"
        and row.get("decision") == "runtime_record_only"
        and row.get("status") == "runtime_discovered_unreviewed"
        and row.get("error_code") in WEB_DISCOVERY_AUDIT_REASONS
        and bool(row.get("discovery_source_capture_id") or row.get("source_capture_id"))
        and row.get("menu_action_count") == 0
        and row.get("detail_action_count") == 0
        and all(
            row.get(key) in (None, "")
            for key in (
                "execution_slot",
                "menu_intent_id",
                "menu_action_execution_id",
                "menu_probe_capture_id",
                "menu_ready_capture_id",
                "menu_page_type",
                "detail_intent_id",
                "detail_action_execution_id",
                "detail_probe_capture_id",
                "detail_ready_capture_id",
                "detail_page_type",
            )
        )
    )
