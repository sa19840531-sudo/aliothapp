"""逐规则应用清单保持完整、不可变，并把缺认证与真实不适用分开。"""

from __future__ import annotations

import copy

import pytest

from services.api.app.application.task_rule_inventory import (
    TaskRuleInventoryError,
    assess_rule_application,
    build_task_rule_inventory,
    inventory_candidate_rules,
    parse_task_rule_inventory,
    requires_task_rule_inventory,
)


def _rule(index: int, **changes: object) -> dict:
    return {
        "rule_id": f"rule-{index}",
        "rule_code": f"code-{index}",
        "name": f"规则{index}",
        "category": "visual",
        "executor_type": "hybrid",
        "severity": "high",
        "applicable_page_types": ["home"],
        "input_contract": {},
        "judge_config": {},
        "check_point": "检查内容",
        "steps": [],
        "sort_order": index,
        "channel": None,
        **changes,
    }


def _inventory(rules: list[dict], **changes: object) -> dict:
    return build_task_rule_inventory(
        ruleset_id="ruleset-1",
        ruleset_version=1,
        ruleset_snapshot_hash="snapshot-1",
        target_platform="app",
        channel="workbench",
        task_config={"inspection_scope": {"page_types": ["home"]}},
        rules=rules,
        **changes,
    )


def test_inventory_applies_all_73_rules_before_filtering_and_never_claims_auth_na() -> None:
    rules = [_rule(index) for index in range(73)]
    for index in range(2):
        rules[index]["input_contract"] = {"target_clients": ["H5", "WEB"]}
    for index in range(2, 15):
        rules[index]["applicable_page_types"] = ["video_detail"]
    for index in range(15, 19):
        rules[index]["input_contract"] = {"auth_requirement": "authenticated"}
    frozen = _inventory(rules)

    assert frozen["rule_count"] == len(frozen["rules"]) == 73
    assert sum(rule["applicability"] == "not_applicable" for rule in frozen["rules"]) == 2
    assert sum(rule["applicability"] == "out_of_scope" for rule in frozen["rules"]) == 13
    assert sum(rule["applicability"] == "applicable" for rule in frozen["rules"]) == 58
    current = [
        assess_rule_application(
            rule,
            target_platform="app",
            channel="workbench",
            inspection_scope=["home"],
            blocked_auth_requirement="authenticated",
        )
        for rule in frozen["rules"]
    ]
    assert sum(row["applicability"] == "unproven" for row in current) == 4
    assert all(row["reason_code"] == "auth_requirement_not_met" for row in current[15:19])
    assert (
        len(
            inventory_candidate_rules(
                frozen, page_type="home", page_url=None, blocked_auth_requirement="authenticated"
            )
        )
        == 54
    )
    assert (
        inventory_candidate_rules(
            frozen, page_type="video_detail", page_url=None, blocked_auth_requirement=None
        )
        == []
    )


def test_freeze_is_deep_copied_and_hash_is_order_stable() -> None:
    rules = [_rule(1), _rule(2)]
    first = _inventory(rules)
    second = _inventory(list(reversed(rules)))
    rules[0]["input_contract"]["auth_requirement"] = "authenticated"
    assert first == second
    assert first["rules"][0]["input_contract"] == {}
    assert (
        parse_task_rule_inventory(
            {
                "rule_inventory": first,
                "ruleset_rule_total": 2,
                "inspection_scope": {"page_types": ["home"]},
            }
        )
        == first
    )


@pytest.mark.parametrize("mutation", ["rule", "hash", "count", "duplicate", "scope", "total"])
def test_invalid_inventory_is_rejected_instead_of_using_current_rules(mutation: str) -> None:
    config = {
        "rule_inventory": copy.deepcopy(_inventory([_rule(1), _rule(2)])),
        "ruleset_rule_total": 2,
        "inspection_scope": {"page_types": ["home"]},
    }
    inventory = config["rule_inventory"]
    if mutation == "rule":
        inventory["rules"][0]["input_contract"] = {"target_clients": ["WEB"]}
    elif mutation == "hash":
        inventory["inventory_hash"] = "0" * 64
    elif mutation == "count":
        inventory["rule_count"] = 3
    elif mutation == "duplicate":
        inventory["rules"][1]["rule_id"] = inventory["rules"][0]["rule_id"]
    elif mutation == "scope":
        config["inspection_scope"] = {"page_types": ["video_detail"]}
    else:
        config["ruleset_rule_total"] = 73
    with pytest.raises(TaskRuleInventoryError):
        parse_task_rule_inventory(config)


def test_candidate_snapshot_respects_route_page_and_aggregate_constraints() -> None:
    rules = [
        _rule(1, url_pattern="^https://example.com/home$"),
        _rule(2, judge_config={"capability_config": {"evidence_input_scope": "task"}}),
        _rule(3, applicable_page_types=["video_detail"]),
        _rule(4, channel="schedule"),
    ]
    inventory = _inventory(rules)
    assert [
        rule["rule_id"]
        for rule in inventory_candidate_rules(
            inventory,
            page_type="home",
            page_url="https://example.com/home",
            blocked_auth_requirement=None,
        )
    ] == ["rule-1"]
    assert (
        inventory_candidate_rules(
            inventory,
            page_type="home",
            page_url="https://example.com/other",
            blocked_auth_requirement=None,
        )
        == []
    )


def test_inventory_is_required_for_app_and_h5_v2_formal_inspection() -> None:
    assert requires_task_rule_inventory("app", {"workflow_version": "v2"})
    assert requires_task_rule_inventory("h5", {"workflow_version": "v2"})
    assert not requires_task_rule_inventory(
        "app",
        {
            "workflow_version": "v2",
            "execution_mode": "exploration_only",
        },
    )
    assert parse_task_rule_inventory({}) is None
    with pytest.raises(TaskRuleInventoryError):
        _inventory([_rule(1), _rule(1)])
