"""规则覆盖提示须保留真实规则条件，并避免重复放大模型输入。"""

import json
from types import MappingProxyType

from services.api.app.agents.explorer.maf_agent import _app_coverage_for_agent
from services.api.app.domain.protocol import freeze_json_mapping, thaw_json_mapping


def _coverage():
    pending = [
        {
            "rule_id": "rule-popup",
            "rule_code": "alioth_21",
            "check_point": "弹窗前后截图应存在真实变化，缺少截图时证据不足。",
            "status": "pending",
            "target_page_types": ["home", "video_list"],
            "required_capture_roles": ["before", "primary"],
            "required_page_states": ["popup", "modal"],
            "requires_journey": False,
            "auth_requirement": "authenticated",
            "session_dependency": "active_session",
            "reason_code": "rule_page_state_not_observed",
        }
    ]
    return freeze_json_mapping(
        {
            "stable_page_reached": True,
            "covered_page_types": ["home"],
            "app_rule_inventory_coverage": {
                "protocol_version": "app-rule-coverage-facts.v1",
                "task_id": "task-1",
                "agent_run_id": "run-1",
                "session_id": "app:task-1",
                "finalization_epoch": 2,
                "inventory_hash": "sha256:frozen",
                "pending_rule_count": 1,
                "pending_inspection_work_count": 0,
                "pending_rules": pending,
                "evidence_gaps": pending,
                "deferred_rule_ids": ["snapshot-1"],
            },
        },
        "coverage",
    )


def test_projection_preserves_rule_requirements_as_json_objects_without_mutation():
    coverage = _coverage()
    original = thaw_json_mapping(coverage)
    result = _app_coverage_for_agent(coverage)
    serialized = json.loads(json.dumps(result, ensure_ascii=False))
    rules = serialized["app_rule_inventory_coverage"]
    assert rules["pending_rules"] == original["app_rule_inventory_coverage"]["pending_rules"]
    assert isinstance(rules["pending_rules"][0], dict)
    assert "evidence_gaps" not in rules
    assert rules["deferred_rule_ids"] == ["snapshot-1"]
    assert rules["inventory_hash"] == "sha256:frozen"
    assert rules["finalization_epoch"] == 2
    assert serialized["covered_page_types"] == ["home"]
    assert thaw_json_mapping(coverage) == original
    assert isinstance(coverage, MappingProxyType)
    result["app_rule_inventory_coverage"]["pending_rules"][0]["required_page_states"].clear()
    assert thaw_json_mapping(coverage) == original


def test_projection_keeps_distinct_evidence_gaps_and_unknown_coverage_fields():
    coverage = thaw_json_mapping(_coverage())
    distinct = {"target_page_types": ["payment"], "missing_capture_roles": ["after"]}
    coverage["app_rule_inventory_coverage"]["evidence_gaps"].append(distinct)
    coverage["future_coverage_field"] = {"required": True}
    original = freeze_json_mapping(coverage, "coverage")
    projected = _app_coverage_for_agent(original)
    assert projected == coverage
    assert projected["app_rule_inventory_coverage"]["evidence_gaps"][-1] == distinct


def test_projection_reduces_wire_size_with_all_71_rule_conditions_intact():
    coverage = thaw_json_mapping(_coverage())
    facts = coverage["app_rule_inventory_coverage"]
    template = facts["pending_rules"][0]
    facts["pending_rules"] = [dict(template, rule_id=f"rule-{n}") for n in range(71)]
    facts["evidence_gaps"] = facts["pending_rules"]
    facts["pending_rule_count"] = 71
    frozen = freeze_json_mapping(coverage, "coverage")
    projected = _app_coverage_for_agent(frozen)
    old_wire = json.dumps(thaw_json_mapping(frozen), ensure_ascii=False).encode("utf-8")
    new_wire = json.dumps(projected, ensure_ascii=False).encode("utf-8")
    assert len(new_wire) < len(old_wire) * 0.6
    assert projected["app_rule_inventory_coverage"]["pending_rules"] == facts["pending_rules"]
