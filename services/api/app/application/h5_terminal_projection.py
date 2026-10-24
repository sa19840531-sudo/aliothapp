"""H5 V2 任务时间线与报告共用的终态只读投影。"""

from typing import Any


def latest_h5_frontier(work_items: list[dict[str, Any]]) -> dict[str, Any] | None:
    """返回最新 Work 携带的 Frontier 快照。"""
    for work in reversed(work_items):
        frontier = work.get("frontier")
        if isinstance(frontier, dict) and frontier:
            return dict(frontier)
    return None


def h5_target_coverage_ready(coverage: dict[str, Any] | None) -> bool:
    """按持久化 Coverage 快照判断目标页型范围是否闭合。"""
    if not isinstance(coverage, dict):
        return False
    required_page_types = coverage.get("required_page_types")
    missing_page_types = coverage.get("missing_page_types")
    return (
        coverage.get("completion_ready") is True
        and coverage.get("assessment_scope_ready") is True
        and isinstance(required_page_types, list | tuple)
        and bool(required_page_types)
        and isinstance(missing_page_types, list | tuple)
        and not missing_page_types
    )


def project_h5_terminal(
    *,
    work_items: list[dict[str, Any]],
    frontier: dict[str, Any] | None,
    assessments: list[dict[str, Any]],
    inventory: list[dict[str, Any]],
    full_inspection_closure: dict[str, Any] | None = None,
    action_count: int = 0,
    action_ids: set[str] | None = None,
    assessment_scope_ready: bool = False,
    coverage_ready: bool | None = None,
) -> dict[str, Any]:
    """从同一 Run 的持久化事实生成唯一 H5 完成结论。"""
    nonterminal_statuses = {"pending", "leased", "running", "retry_wait"}
    review_status_reasons = {
        "bounded": "work_bounded",
        "budget_exhausted": "work_budget_exhausted",
        "blocked": "work_blocked",
        "no_progress": "work_no_progress",
    }
    technical_status_reasons = {"failed": "work_failed"}
    nonterminal_work = sum(
        str(item.get("status") or "") in nonterminal_statuses for item in work_items
    )
    pending_frontier = _safe_int((frontier or {}).get("pending_count"))
    assessment_statuses = {str(item.get("status") or "") for item in assessments}
    inventory_statuses = {str(item.get("status") or "") for item in inventory}
    work_statuses = {str(item.get("status") or "") for item in work_items}
    work_reason_text = " ".join(
        str(value or "")
        for item in work_items
        for value in (
            item.get("last_error_code"),
            item.get("last_error_message"),
            (item.get("terminal_payload") or {}).get("reason")
            if isinstance(item.get("terminal_payload"), dict)
            else None,
        )
    ).casefold()
    closure = full_inspection_closure or {}
    terminal_reason: str | None = None
    quality_status: str | None = None
    if "auth" in work_reason_text:
        terminal_reason = "authentication_required"
    elif "recovery" in work_reason_text:
        terminal_reason = "recovery_failed"
    elif nonterminal_work or pending_frontier:
        terminal_reason = "exploration_incomplete"
        quality_status = "in_progress"
    elif "cancelled" in work_statuses:
        terminal_reason = "work_cancelled"
        quality_status = "needs_review"
    elif failed_status := next(
        (status for status in technical_status_reasons if status in work_statuses), None
    ):
        terminal_reason = technical_status_reasons[failed_status]
        quality_status = "technical_failed"
    elif review_status := next(
        (status for status in review_status_reasons if status in work_statuses), None
    ):
        terminal_reason = review_status_reasons[review_status]
    elif _safe_int((frontier or {}).get("blocked_count")) > 0:
        terminal_reason = "frontier_blocked"
    elif _safe_int((frontier or {}).get("failed_count")) > 0:
        terminal_reason = "frontier_failed"
    elif not work_items:
        terminal_reason = "exploration_not_started"
    elif work_statuses == {"superseded"}:
        terminal_reason = "recovery_history_incomplete"
    elif not assessment_scope_ready:
        terminal_reason = "assessment_scope_incomplete"
    elif coverage_ready is False:
        terminal_reason = "coverage_scope_incomplete"
    elif "requires_auth" in assessment_statuses:
        terminal_reason = "authentication_required"
    elif "blocked_by_safety" in assessment_statuses:
        terminal_reason = "blocked_by_safety"
    elif assessment_statuses.intersection({"not_found", "evidence_insufficient"}):
        terminal_reason = "evidence_insufficient"
    elif "failed" in inventory_statuses:
        terminal_reason = "evidence_failed"
        quality_status = "technical_failed"
    elif "insufficient" in inventory_statuses:
        terminal_reason = "evidence_insufficient"
    elif not _inventory_ready(
        inventory=inventory,
        action_count=action_count,
        action_ids=action_ids,
    ):
        terminal_reason = "evidence_inventory_incomplete"
    elif _safe_int(closure.get("failed_inspection_count")) > 0:
        terminal_reason = "inspection_failed"
        quality_status = "technical_failed"
    elif _safe_int(closure.get("nonterminal_inspection_count")) > 0:
        terminal_reason = "inspection_pending"
    elif (
        closure.get("gate_snapshot_id") is not None
        and closure.get("gate_passed") is not True
    ):
        reasons = closure.get("gate_block_reasons")
        terminal_reason = (
            str(reasons[0])
            if isinstance(reasons, list) and reasons
            else "completion_gate_pending"
        )

    complete = terminal_reason is None
    if quality_status is None:
        quality_status = "complete" if complete else "needs_review"
    return {
        "complete": complete,
        "quality_status": quality_status,
        "requires_review": quality_status == "needs_review",
        "terminal_reason": terminal_reason,
    }


def _safe_int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    try:
        return max(int(value or 0), 0)
    except (TypeError, ValueError):
        return 0


def _inventory_ready(
    *,
    inventory: list[dict[str, Any]],
    action_count: int,
    action_ids: set[str] | None,
) -> bool:
    inventory_ids = [
        item_id
        for item in inventory
        if (item_id := _optional_text(item.get("action_execution_id")))
    ]
    if action_count == 0:
        return not inventory
    if len(inventory_ids) != action_count or len(set(inventory_ids)) != action_count:
        return False
    if any(str(item.get("status") or "") != "pass" for item in inventory):
        return False
    return action_ids is None or set(inventory_ids) == action_ids


def _optional_text(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None
