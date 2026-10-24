"""绿色第4版目录及显式合同门禁，不连接数据库、不发布规则。"""

from copy import deepcopy
from uuid import uuid4

import pytest

from services.api.app.application.green_rule_catalog import (
    GREEN_CONTRACT_VERSION,
    GREEN_INTERACTION_SCOPE_KEY,
    GREEN_TEMPORAL_SCOPE_KEY,
    GreenRuleConfigurationError,
    build_green_rule_payload,
    build_green_ruleset_manifest,
    validate_green_rule_configuration,
)
from services.api.app.application.green_rule_skills import GreenRuleSkillConfigurationError
from services.api.app.application.inspection_work_items import build_inspection_rule_contract_hash
from services.api.app.application.task_rule_inventory import build_task_rule_inventory


def test_manifest_preserves_source_order_clients_and_single_category_skills():
    from services.api.app.api.v1.rules import RulePayload, RulesetPayload

    manifest = build_green_ruleset_manifest()
    RulesetPayload.model_validate(manifest["ruleset"])
    rules = manifest["rules"]
    assert [rule["rule_code"] for rule in rules] == [
        "alioth_green_102",
        "alioth_green_s72",
        "alioth_green_41",
        "alioth_green_62",
        "alioth_green_s35",
        "alioth_green_s49",
        "alioth_green_s73",
        "alioth_green_32",
    ]
    assert manifest["ruleset"]["version"] == 4 and manifest["ruleset"]["status"] == "draft"
    for row, rule in enumerate(rules, 13):
        RulePayload.model_validate({"ruleset_id": str(uuid4()), **rule})
        contract = validate_green_rule_configuration(rule)
        assert contract["schema_version"] == GREEN_CONTRACT_VERSION
        assert contract["source_row"] == row
        assert contract["task_scope_is_coverage"] is False
        assert rule["status"] == "disabled" and rule["judge_config"]["thresholds"] == {}
        assert rule["input_contract"]["required_fields"] == []
        names = [skill["name"] for skill in rule["judge_config"]["green_skill_program"]["skills"]]
        if contract["rule_key"] in ("102", "32", "41"):
            assert rule["judge_config"]["capability_config"]["evidence_input_scope"] == "page"
        else:
            assert rule["judge_config"]["capability_config"]["evidence_input_scope"] == "task"
            assert names[-1] == "green-process-evidence"
        if contract["rule_key"] == "62":
            assert rule["category"] == "ad" and names == ["ad-rule", "green-process-evidence"]
        if contract["rule_key"] == "102":
            assert names == ["green-content-safety"] and rule["category"] == "compliance"
        if contract["rule_key"] == "41":
            assert rule["input_contract"]["target_clients"] == ["APP"]
    assert (
        rules[4]["judge_config"]["capability_config"]["evidence_scope_key"]
        == GREEN_INTERACTION_SCOPE_KEY
    )
    for index in (1, 3, 5, 6):
        assert (
            rules[index]["judge_config"]["capability_config"]["evidence_scope_key"]
            == GREEN_TEMPORAL_SCOPE_KEY
        )


@pytest.mark.parametrize("version", [1, 2, 3, 5, True, "4"])
def test_constructor_cannot_publish_old_or_guessed_future_version(version):
    with pytest.raises(GreenRuleConfigurationError):
        build_green_ruleset_manifest(version=version)


@pytest.mark.parametrize("rule_code", ["alioth_green_102", "alioth_41", "alioth_26", "alioth_7"])
def test_old_code_alone_never_activates_green_contract(rule_code):
    assert validate_green_rule_configuration({"rule_code": rule_code, "judge_config": {}}) is None


@pytest.mark.parametrize("version", [1, 2, 3])
def test_explicit_known_old_contract_is_left_on_original_chain(version):
    rule = {
        "rule_code": "alioth_green_s35",
        "judge_config": {
            "green_rule_contract": {
                "schema_version": "green-rule-contract.v1",
                "ruleset_version": version,
            },
        },
    }
    original = deepcopy(rule)
    assert validate_green_rule_configuration(rule) is None
    assert rule == original


@pytest.mark.parametrize(
    "fault", ["schema", "key", "code", "category", "scope", "clients", "source", "program"]
)
def test_explicit_v4_rejects_cross_identity_or_incomplete_frozen_configuration(fault):
    rule = build_green_rule_payload("s35")
    config = rule["judge_config"]
    if fault == "schema":
        config["green_rule_contract"]["schema_version"] = "green-rule-contract.v9"
    elif fault == "key":
        config["green_rule_contract"]["rule_key"] = "unknown"
    elif fault == "code":
        rule["rule_code"] = "alioth_41"
    elif fault == "category":
        rule["category"] = "ad"
    elif fault == "scope":
        config["capability_config"]["evidence_input_scope"] = "journey"
    elif fault == "clients":
        rule["input_contract"]["target_clients"] = ["H5"]
    elif fault == "source":
        config["green_rule_contract"]["source_row"] = 15
    else:
        config["green_skill_program"]["skills"][0]["body"] += "\n未冻结增量"
    with pytest.raises((GreenRuleConfigurationError, GreenRuleSkillConfigurationError)):
        validate_green_rule_configuration(rule)


def test_skill_program_is_inside_existing_task_inventory_and_rule_hash():
    rule = {**build_green_rule_payload("s72"), "rule_id": str(uuid4())}
    inventory = build_task_rule_inventory(
        ruleset_id=str(uuid4()),
        ruleset_version=4,
        ruleset_snapshot_hash="controlled-snapshot",
        target_platform="h5",
        channel="api",
        task_config={"workflow_version": "v2"},
        rules=[rule],
    )
    frozen = inventory["rules"][0]
    assert (
        frozen["judge_config"]["green_skill_program"] == rule["judge_config"]["green_skill_program"]
    )
    assert frozen["contract_hash"] == build_inspection_rule_contract_hash(rule)
    original_hash = frozen["contract_hash"]
    rule["judge_config"]["green_skill_program"]["skills"][0]["body"] += "\n后来的正文"
    assert original_hash != build_inspection_rule_contract_hash(rule)
    assert frozen["contract_hash"] == original_hash
    assert validate_green_rule_configuration(frozen)["rule_key"] == "s72"
