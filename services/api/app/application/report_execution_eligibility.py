"""依据冻结的真实页面事实复核旧报告结论资格，不改写历史执行或人工审计。"""

import json
from typing import Any

from services.api.app.application.observation_surface_guard import (
    target_document_unavailability_reason,
)

TARGET_DOCUMENT_UNAVAILABLE_REASON = "target_document_unavailable"


def qualify_report_execution(row: dict[str, Any]) -> dict[str, Any]:
    """错误文档不能作为业务结论；缺少冻结事实时保留原记录，不猜测历史页面。"""
    if row.get("judge_strategy") == "rule_engine":
        return row
    judge_input = _mapping(row.get("judge_input"))
    tool_results = judge_input.get("tool_results")
    if isinstance(tool_results, list) and any(
        isinstance(result, dict) and result.get("tool_name") == "technical_error_text"
        for result in tool_results
    ):
        return row
    page = _mapping(judge_input.get("page"))
    facts = _mapping(judge_input.get("fact_summary"))
    title, text = page.get("title"), facts.get("visible_text_excerpt")
    if not isinstance(title, str) or not isinstance(text, str):
        return row
    reason = target_document_unavailability_reason(title=title, visible_text=text)
    if reason is None:
        return row
    qualified = dict(row)
    qualified["execution_reason"] = TARGET_DOCUMENT_UNAVAILABLE_REASON
    original_summary = str(row.get("result_summary") or "").strip()
    qualified["result_summary"] = (
        f"{reason}。原始机器执行状态为 {row.get('execution_status') or '未记录'}；"
        "本条不计为业务通过、不通过或不适用，需获取有效业务页面后重新质检。"
        + (f"历史机器说明：{original_summary}" if original_summary else "")
    )
    qualified["missing_evidence"] = list(
        dict.fromkeys([*(row.get("missing_evidence") or []), "有效的被检业务正文"])
    )
    return qualified


def _mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return {}
    return value if isinstance(value, dict) else {}
