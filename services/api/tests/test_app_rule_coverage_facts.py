"""冻结规则反驱动探索的真实需求与封存交接边界。"""

from copy import deepcopy

from services.api.app.application.app_rule_coverage_facts import project_app_rule_coverage_facts
from services.api.app.application.bounded_exploration_scheduler import (
    evidence_gap_page_types_from_facts,
)
from services.api.app.application.exploration_scheduler_facts import (
    merge_exploration_scheduler_facts,
)

SCOPE = {"task_id": "t", "agent_run_id": "r", "session_id": "s", "finalization_epoch": 2}


def _rule(code="static", *, pages=None, active=False, aggregate=False, auth=None):
    capability = {"requires_journey": active}
    if aggregate:
        capability.update(evidence_input_scope="task", evidence_scope_key="official_group_entry")
    return {
        "rule_id": code,
        "rule_code": code,
        "name": "规则",
        "check_point": "确认入口可用",
        "applicability": "applicable",
        "applicable_page_types": pages or ["payment"],
        "input_contract": {"auth_requirement": auth},
        "judge_config": {"capability_config": capability},
    }


def _facts(rules, *, completed=(), captures=(), pending=0):
    return project_app_rule_coverage_facts(
        {"rules": rules, "inventory_hash": "frozen-hash"},
        scope=SCOPE,
        completed_rule_ids=set(completed),
        captures=captures,
        pending_inspection_work_count=pending,
    )


def test_unobserved_frozen_rule_drives_existing_evidence_gap_scheduler():
    facts = _facts([_rule()])
    assert facts["pending_rule_count"] == 1
    assert evidence_gap_page_types_from_facts(
        {"app_rule_inventory_coverage": facts}, known_page_types=("home", "payment")
    ) == ("payment",)
    assert facts["pending_rules"][0]["check_point"] == "确认入口可用"


def test_static_and_aggregate_inputs_can_defer_until_sealed_judge_without_fake_execution():
    facts = _facts(
        [_rule(), _rule("aggregate", aggregate=True)],
        captures=[{"page_type": "payment", "page_state": "stable"}],
    )
    assert facts["pending_rule_count"] == 0
    assert facts["deferred_rule_ids"] == ["static", "aggregate"]
    assert "execution_id" not in str(facts)


def test_active_rule_keeps_real_demand_until_judge_concludes():
    rules = [_rule(active=True)]
    captures = [{"page_type": "payment", "page_state": "stable"}]
    pending = _facts(rules, captures=captures, pending=1)
    assert pending["pending_rule_count"] == 1
    assert pending["pending_inspection_work_count"] == 1
    assert pending["pending_rules"][0]["reason_code"] == "rule_evidence_incomplete"
    assert _facts(rules, captures=captures, completed=["static"])["pending_rule_count"] == 0


def test_platform_and_explicit_scope_exclusions_do_not_drive_capture():
    unsupported, excluded = _rule("web"), _rule("excluded")
    unsupported["applicability"] = "not_applicable"
    excluded["applicability"] = "out_of_scope"
    assert _facts([unsupported, excluded])["pending_rules"] == []


def test_auth_unknown_and_guest_cannot_prepare_authenticated_rule():
    rule = _rule(auth="authenticated")
    for state in (None, "unknown", "guest"):
        assert (
            _facts([rule], captures=[{"page_type": "payment", "session_auth": state}])[
                "pending_rule_count"
            ]
            == 1
        )
    assert (
        _facts([rule], captures=[{"page_type": "payment", "session_auth": "authenticated"}])[
            "pending_rule_count"
        ]
        == 0
    )


def test_wrong_page_state_or_url_does_not_prepare_input():
    rule = _rule()
    rule["url_pattern"] = "app://correct/"
    rule["judge_config"]["engine_config"] = {"required_page_states": ["popup"]}
    rule["judge_config"]["evidence_layer_target"] = "overlay"
    for page_url, state in (("app://wrong/", "popup"), ("app://correct/", "stable")):
        assert (
            _facts(
                [rule],
                captures=[{"page_type": "payment", "page_url": page_url, "page_state": state}],
            )["pending_rule_count"]
            == 1
        )
    missing_popup = _facts(
        [rule],
        captures=[{"page_type": "payment", "page_url": "app://correct/", "page_state": "stable"}],
    )["pending_rules"][0]
    assert missing_popup["required_page_states"] == ["popup"]
    assert missing_popup["reason_code"] == "rule_page_state_not_observed"
    before = deepcopy(rule)
    assert (
        _facts(
            [rule],
            captures=[
                {"page_type": "payment", "page_url": "app://correct/", "page_state": "popup"}
            ],
        )["pending_rule_count"]
        == 0
    )
    assert rule == before


def test_missing_current_rule_facts_removes_stale_prior_scope():
    merged = merge_exploration_scheduler_facts(
        {"app_rule_inventory_coverage": {"pending_rule_count": 0}, "covered_page_types": ["home"]},
        {"rule_evidence_requests": []},
        surface="app",
    )
    assert "app_rule_inventory_coverage" not in merged
    assert merged["covered_page_types"] == ["home"]


def test_obstructed_static_rule_stays_pending_until_visible_input_exists():
    rule = _rule()
    blocked = {"page_type": "payment", "page_state": "popup"}
    facts = _facts([rule], captures=[blocked])
    assert facts["pending_rule_count"] == 1
    assert facts["deferred_rule_ids"] == []
    assert facts["pending_rules"][0]["reason_code"] == "rule_evidence_incomplete"
    ready = _facts([rule], captures=[blocked, {"page_type": "payment", "page_state": "stable"}])
    assert ready["pending_rule_count"] == 0
    assert ready["deferred_rule_ids"] == ["static"]
