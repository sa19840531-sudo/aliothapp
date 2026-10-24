"""把冻结规则的未应用需求送回既有 App 探索，不制造执行或不适用结论。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from services.api.app.application.aggregate_rule_config import (
    resolve_rule_evidence_input_configuration,
)
from services.api.app.application.app_rule_action_readiness import (
    AppRuleActionReadiness,
    app_rule_action_requirement,
    assess_app_rule_action_readiness,
)
from services.api.app.application.capture_plan import derive_session_dependency
from services.api.app.application.evidence_presentation import (
    app_presentation_ready_for_rule,
    derive_app_presentation,
)
from services.api.app.application.rule_auth_gate import (
    resolve_blocked_auth_requirement_from_session_auth,
)
from services.api.app.domain.rule.url_scope import candidate_rule_matches_page_url

APP_RULE_COVERAGE_FACT_KEY = "app_rule_inventory_coverage"


def project_app_rule_coverage_facts(
    inventory: Mapping[str, Any],
    *,
    scope: Mapping[str, object],
    completed_rule_ids: set[str],
    captures: Sequence[Mapping[str, Any]],
    pending_inspection_work_count: int = 0,
    action_facts: Mapping[str, Any] | None = None,
) -> dict[str, object]:
    """静态与聚合规则可凭可信输入交收口；活会话规则须实际完成判定。"""

    pending: list[dict[str, object]] = []
    deferred: list[str] = []
    for rule in inventory["rules"]:
        if rule.get("applicability") != "applicable":
            continue
        rule_id = str(rule["rule_id"])
        if rule_id in completed_rule_ids:
            continue
        contract = _mapping(rule.get("input_contract"))
        config = _mapping(rule.get("judge_config"))
        aggregate = resolve_rule_evidence_input_configuration(config).is_aggregate
        dependency = derive_session_dependency(input_contract=contract, judge_config=config)
        pages = [str(page) for page in rule.get("applicable_page_types") or []]
        matched = [capture for capture in captures if _capture_matches(rule, capture, action_facts)]
        # snapshot／aggregate 的正式工作项在封存后创建，不能要求封存前先有 Judge。
        # 这里只承诺可交接输入，不补执行计数；正式 Publication 和 Gate 仍逐项验证。
        if matched and (aggregate or dependency == "snapshot"):
            deferred.append(rule_id)
            continue
        capability = _mapping(config.get("capability_config"))
        required_states = _mapping(config.get("engine_config")).get("required_page_states")
        page_observed = any(capture.get("page_type") in pages for capture in captures)
        reason = "rule_evidence_incomplete" if page_observed else "rule_page_not_observed"
        if (
            page_observed
            and required_states
            and not any(
                capture.get("page_type") in pages and capture.get("page_state") in required_states
                for capture in captures
            )
        ):
            reason = "rule_page_state_not_observed"
        action_readiness = _action_readiness(rule, action_facts)
        if page_observed and not action_readiness.ready:
            reason = "rule_action_evidence_incomplete"
        requirement = action_readiness.to_exploration_requirement()
        pending.append(
            {
                "rule_id": rule_id,
                "rule_code": str(rule.get("rule_code") or ""),
                "check_point": str(rule.get("check_point") or rule.get("name") or "")[:240],
                "status": "pending",
                "target_page_types": pages,
                "required_capture_roles": list(contract.get("required_capture_roles") or []),
                "required_page_states": list(required_states)
                if isinstance(required_states, list)
                else [],
                "requires_journey": capability.get("requires_journey") is True,
                "auth_requirement": contract.get("auth_requirement"),
                "session_dependency": "aggregate" if aggregate else dependency,
                "reason_code": reason,
                **({"required_action_evidence": requirement} if requirement else {}),
            }
        )
    return {
        "protocol_version": "app-rule-coverage-facts.v1",
        "source": "frozen_task_rule_inventory_and_current_run_evidence",
        "task_id": scope["task_id"],
        "agent_run_id": scope["agent_run_id"],
        "session_id": scope["session_id"],
        "finalization_epoch": scope["finalization_epoch"],
        "inventory_hash": inventory["inventory_hash"],
        "pending_rule_count": len(pending),
        "pending_inspection_work_count": pending_inspection_work_count,
        "pending_rules": pending,
        "deferred_rule_ids": deferred,
        "evidence_gaps": pending,
    }


def _capture_matches(
    rule: Mapping[str, Any], capture: Mapping[str, Any],
    action_facts: Mapping[str, Any] | None = None,
) -> bool:
    if capture.get("page_type") not in (rule.get("applicable_page_types") or []):
        return False
    contract = _mapping(rule.get("input_contract"))
    requirement = contract.get("auth_requirement")
    session_auth = capture.get("session_auth")
    if requirement and (
        session_auth not in {"guest", "authenticated"}
        or requirement == resolve_blocked_auth_requirement_from_session_auth(str(session_auth))
    ):
        return False
    config = _mapping(rule.get("judge_config"))
    states = _mapping(config.get("engine_config")).get("required_page_states")
    if isinstance(states, list) and states and capture.get("page_state") not in states:
        return False
    if not app_presentation_ready_for_rule(
        derive_app_presentation(
            page_state=capture.get("page_state"),
            overlay_signature=capture.get("overlay_signature"),
            surface_facts=_mapping(capture.get("surface_facts")),
        ),
        rule,
        page_state=capture.get("page_state"),
    ):
        return False
    if not _action_readiness(rule, action_facts, capture_id=capture.get("capture_id")).ready:
        return False
    return candidate_rule_matches_page_url(
        page_url=capture.get("page_url"), url_pattern=rule.get("url_pattern")
    )[0]


def _mapping(value: object) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _action_readiness(rule, action_facts, *, capture_id=None) -> AppRuleActionReadiness:
    requirement = app_rule_action_requirement(rule)
    if requirement is None:
        return AppRuleActionReadiness(ready=True)
    if action_facts is None:
        return AppRuleActionReadiness(
            ready=False, requirement=requirement, missing_evidence=("business_action_evidence",)
        )
    return assess_app_rule_action_readiness(
        rule, **{**action_facts, **({"primary_capture_id": capture_id} if capture_id else {})}
    )
