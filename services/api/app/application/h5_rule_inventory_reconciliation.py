"""H5 未触发规则按现有页面、状态和工作台账解释，不生成虚假执行。"""

from collections.abc import Mapping
from typing import Any

from services.api.app.domain.rule.url_scope import candidate_rule_matches_page_url


def explain_h5_rule_trigger_gap(
    rule: Mapping[str, Any], snapshot: Mapping[str, Any], work_rules: list[dict[str, Any]],
) -> tuple[str, str]:
    """复用候选筛选条件，区分缺页面、缺状态和已有证据却未发布。"""
    if work_rules:
        return "inspection_result_pending", "规则已有工作项，但尚未产生当前运行的有效判定。"
    capability = (rule.get("judge_config") or {}).get("capability_config") or {}
    if capability.get("evidence_input_scope", "page") != "page":
        return "aggregate_work_not_published", "聚合规则尚未形成工作项，需核对收口发布与证据集合。"
    captures = snapshot.get("capture_facts")
    if not isinstance(captures, list):
        return "trigger_facts_unavailable", "缺少当前运行的触发事实，不能认定规则不适用。"
    matching = [
        capture for capture in captures
        if capture.get("page_type") in (rule.get("applicable_page_types") or [])
    ]
    if not matching:
        return "required_page_not_observed", "未取得该规则适用页面的可信证据，规则未触发。"
    matching = [
        capture for capture in matching
        if candidate_rule_matches_page_url(
            page_url=capture.get("url"), url_pattern=rule.get("url_pattern"),
        )[0]
    ]
    if not matching:
        return "rule_url_scope_not_matched", "已有同类页面，但实际网址未满足该规则的地址条件。"
    from services.api.app.agents.tools.rule_tools import _rule_matches_required_page_state

    if not any(
        _rule_matches_required_page_state(dict(rule), str(capture.get("page_state") or "stable"))
        for capture in matching
    ):
        return "required_page_state_not_observed", "已到达同类页面，但未取得规则要求的页面状态。"
    return (
        "inspection_work_not_published",
        "已有匹配页面与状态，但规则工作项未发布，需补查发布链路。",
    )
