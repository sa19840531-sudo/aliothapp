"""绿色第4版共享只读判读；端生产器尚未证明的义务逐项保留缺证。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace

from services.api.app.application.green_evidence_contracts import GreenEvidenceSet

# 这些是既定业务义务所需的原生事实，不能由模型或页面提交布尔值补齐。
_MISSING_FACTS = {
    "empty_state_meaning": (
        "same_module_request_identity",
        "actual_empty_response",
        "module_rendering",
    ),
    "load_failure_meaning": ("same_module_request_identity", "failure_rendering_and_recovery"),
    "correct_response": ("native_click_event_time", "same_module_response_causality"),
    "usable_layout": ("complete_interactive_layout", "occlusion_effect_on_use"),
    "comparable_state_stability": (
        "independent_comparable_preconditions",
        "complete_repeated_actions",
    ),
    "failure_reason": ("actual_module_fault", "failure_reason_rendering"),
    "retry_entry": ("actual_module_fault", "retry_refresh_usable_target"),
    "business_recovery": ("same_module_retry_request", "business_recovered_response"),
    "video_line_recovery": (
        "trusted_video_business_identity",
        "actual_video_fault",
        "line_recovery",
    ),
    "account_payment_support": ("trusted_account_payment_identity", "actual_account_payment_fault"),
    "payment_not_interrupted": ("trusted_payment_stage", "complete_support_interruption_window"),
    "loading_feedback": ("same_module_loading_window", "complete_progress_feedback_observation"),
    "empty_state_guidance": ("actual_empty_response", "same_module_illustration_or_guidance"),
    "business_success_feedback": (
        "trusted_business_success",
        "native_click_request_clock",
        "new_feedback_window",
    ),
    "natural_end_handoff": (
        "ad_content_media_identity",
        "natural_ad_end_event",
        "full_handoff_window",
    ),
    "manual_close_handoff": (
        "ad_content_media_identity",
        "formal_ad_close_action",
        "full_handoff_window",
    ),
    "content_preload": ("ad_content_media_identity", "content_preload_original"),
    "presented_first_frame": (
        "content_presented_event",
        "matching_first_frame_png",
        "handoff_clock",
    ),
}

_PAGE_MISSING_FACTS = {
    "102": {
        "minor_safety": (
            "minor_age_context_original",
            "controlled_minor_policy",
            "minor_independent_evaluation",
        ),
        "political_safety": (
            "political_context_original",
            "controlled_political_policy",
            "political_independent_evaluation",
        ),
        "terrorism_safety": (
            "terrorism_context_original",
            "controlled_terrorism_policy",
            "terrorism_independent_evaluation",
        ),
        "content_scope": (
            "home_and_list_scope_original",
            "actual_screen_coverage",
            "advertisement_exclusion_source",
        ),
    },
    "32": {
        "equal_width_navigation": (
            "complete_navigation_siblings",
            "navigation_usable_container",
            "sibling_bounds",
        ),
        "fit_navigation_spacing": (
            "complete_navigation_siblings",
            "text_bounds",
            "navigation_spacing",
        ),
        "overflow_scroll": (
            "complete_navigation_siblings",
            "overflow_condition",
            "native_horizontal_scroll",
        ),
        "initial_next_label": (
            "initial_scroll_and_scale",
            "next_label_text_bounds",
            "viewport_clipping",
        ),
    },
    "41": {
        "external_browser_handoff": (
            "formal_external_link_action",
            "known_outbound_target",
            "foreground_browser_component",
            "current_browser_url_strong_source",
            "builtin_webview_strong_source",
        ),
    },
}


@dataclass(frozen=True)
class GreenSubitem:
    """模块内部的分项结论；明确反例优先，缺项始终单独保留。"""

    key: str
    status: str
    reason: str
    missing: tuple[str, ...] = ()
    capture_ids: tuple[str, ...] = ()
    action_ids: tuple[str, ...] = ()

    def to_dict(self):
        return {
            "key": self.key,
            "status": self.status,
            "evaluated": self.status != "evidence_insufficient",
            "confidence": 1.0 if self.status in {"pass", "fail"} else None,
            "reason": self.reason,
            "missing": list(self.missing),
            "capture_ids": list(self.capture_ids),
            "action_ids": list(self.action_ids),
        }


def reduce_green_subitems(items):
    """只合并本模块已形成的分项，不把缺证改写成不适用或覆盖已知失败。"""
    values = tuple(items)
    if any(
        not isinstance(item, GreenSubitem)
        or item.status
        not in {
            "pass",
            "fail",
            "manual_review",
            "evidence_insufficient",
            "skipped",
        }
        or (item.status in {"pass", "skipped"} and item.missing)
        for item in values
    ):
        raise ValueError("绿色分项必须是内部结论，且缺证分项不能通过")
    missing = tuple(dict.fromkeys(value for item in values for value in item.missing))
    if any(item.status == "fail" for item in values):
        return "fail", missing
    if any(item.status == "manual_review" for item in values):
        return "manual_review", missing
    if values and all(item.status in {"pass", "skipped"} for item in values):
        return ("pass" if any(item.status == "pass" for item in values) else "skipped"), missing
    return "evidence_insufficient", missing


def evaluate_green_aggregate_context(context):
    """仅显式冻结第4版的两个 TASK 范围进入；不执行新动作或调用新 Tool。"""
    from services.api.app.application.aggregate_inspection_execution import _insufficient
    from services.api.app.application.green_process_judge import judge_green_process_subitems
    from services.api.app.application.green_rule_catalog import validate_green_rule_configuration

    rule = getattr(context, "rule_payload", None)
    try:
        contract = validate_green_rule_configuration(rule)
    except ValueError:
        contract = None
    evidence = getattr(context, "green_evidence", None)
    input_set = context.input_set
    valid = (
        isinstance(contract, Mapping)
        and contract.get("input_kind") == input_set.scope_key
        and input_set.scope.value == "task"
        and isinstance(evidence, GreenEvidenceSet)
        and evidence.verified_read
        and evidence.task_id == input_set.task_id
        and evidence.run_id == input_set.source_agent_run_id
        and evidence.epoch == input_set.finalization_epoch
        and evidence.input_set_id == input_set.input_set_id
        and evidence.input_hash == input_set.input_set_hash
        and evidence.scope_key == input_set.scope_key
        and bool(evidence.fingerprint)
        and context.lease.task_id == input_set.task_id
        and context.lease.rule_id == input_set.rule_id
        and rule.get("rule_code") == input_set.rule_code
        and context.lease.rule_contract_hash == input_set.rule_contract_hash
        and context.lease.evidence_input_set_id == input_set.input_set_id
        and context.lease.input_hash == input_set.input_set_hash
        and context.lease.finalization_epoch == input_set.finalization_epoch
        and context.lease.scope == "task"
        and context.lease.scope_key == input_set.scope_key
    )
    if not valid:
        return _insufficient(
            context,
            code="GREEN_VERIFIED_PROCESS_INPUT_MISSING",
            reason="绿色规则缺少与冻结合同一致的权威过程原件，不能采用调用方自报事实",
        )
    items = judge_green_process_subitems(
        contract=contract,
        evidence=evidence,
        required_facts=_MISSING_FACTS,
        frozen_config=rule.get("judge_config", {}),
    )
    status, missing = reduce_green_subitems(items)
    result = _insufficient(
        context,
        code="GREEN_NATIVE_PROCESS_FACTS_MISSING",
        reason="原件与正式动作已只读核验；相关端尚缺业务对象、完整窗口或原生时间因果生产器",
    )
    failures = "；".join(dict.fromkeys(item.reason for item in items if item.status == "fail"))
    reviews = "；".join(
        dict.fromkeys(item.reason for item in items if item.status == "manual_review")
    )
    if status == "pass":
        summary = "本次冻结检查范围内的绿色分项均已完成判读并符合要求"
    elif status == "skipped":
        summary = "本次冻结检查范围内的绿色分项均有可靠依据不适用"
    elif status == "fail":
        summary = "绿色规则已有明确不合格分项" + ("，其他缺项保留复核" if missing else "")
    elif status == "manual_review":
        summary = "绿色规则存在需要人工复核的分项"
    elif any(item.status == "pass" for item in items):
        summary = "绿色规则已有部分确定性结论，其他分项仍缺证"
    else:
        summary = result.result_summary
    wrapper = dict(result.wrapper_payload)
    if not missing:
        wrapper.pop("evidence_insufficient_code", None)
    return replace(
        result,
        execution_status=status,
        result_summary=summary,
        problem_desc=None
        if status in {"pass", "skipped"}
        else failures or reviews or result.problem_desc,
        confidence=1.0 if status in {"pass", "fail"} else None,
        review_status="pending" if missing or status == "manual_review" else None,
        review_reason="；".join(
            part
            for part in (reviews, "缺少必要材料：" + "、".join(missing) if missing else "")
            if part
        )
        or None,
        error_code=result.error_code if missing else None,
        wrapper_payload={
            **wrapper,
            "green": {
                "schema_version": "green-rule-contract.v2",
                "rule_key": contract["rule_key"],
                "input_kind": input_set.scope_key,
                "task_scope_is_coverage": False,
                "evidence_fingerprint": evidence.fingerprint,
                "subitems": [item.to_dict() for item in items],
                "missing_evidence": list(missing),
                "technical_execution": {
                    "verified_capture_count": len(evidence.captures),
                    "formal_completed_action_count": len(evidence.actions),
                    "verified_original_types": sorted(
                        {
                            original.artifact_type
                            for capture in evidence.captures
                            for original in capture.originals
                        }
                    ),
                    "driver_execution_is_business_success": False,
                },
            },
        },
    )


def green_page_preflight(rule_key, evidence_bundle):
    """页面规则只检查既有可信原件供给；不借旧视觉入口绕过新标准缺项。"""
    from services.api.app.infrastructure.rule_judge_evidence import TrustedEvidenceBundle

    if not isinstance(evidence_bundle, TrustedEvidenceBundle):
        return _page_gap(rule_key, "缺少既有可信原件包", ("trusted_page_originals",))
    screenshots = evidence_bundle.artifacts_of_type("screenshot")
    if not screenshots:
        return _page_gap(rule_key, "缺少原始截图", ("original_screenshot",))
    if rule_key == "102":
        return _page_gap(rule_key, "范围、广告排除、受控政策及三类风险独立判读尚缺材料", ())
    if rule_key == "32":
        return _page_gap(rule_key, "当前快照不能证明完整同级导航及初始文字几何", ())
    if rule_key == "41":
        return _page_gap(rule_key, "缺少正式外链动作与系统浏览器强来源，不做视觉身份兜底", ())
    return _page_gap(rule_key, "未声明的绿色页面规则", ("explicit_green_page_contract",))


def _page_gap(rule_key, reason, common_missing):
    subitems = [
        {
            "key": key,
            "status": "evidence_insufficient",
            "evaluated": False,
            "confidence": None,
            "reason": "该分项尚缺必要原件或已接入的独立判读，未形成业务结论",
            "missing": list(dict.fromkeys((*common_missing, *missing))),
        }
        for key, missing in _PAGE_MISSING_FACTS.get(rule_key, {}).items()
    ]
    missing = list(
        dict.fromkeys((*common_missing, *(value for item in subitems for value in item["missing"])))
    )
    return {
        "execution_status": "evidence_insufficient",
        "reason": reason,
        "missing_evidence": missing,
        "required_subitems": [item["key"] for item in subitems],
        "subitems": subitems,
        "legacy_visual_fallback_allowed": False,
    }
