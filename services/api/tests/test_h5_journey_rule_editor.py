"""第22行配置丢失及分项材料断线复现，使用既有暂停合同而非原站验收。"""

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from services.api.app.api.v1.rules import RuleBusinessPayload
from services.api.app.application.h5_shortflows import H5ShortflowError, freeze_h5_shortflows
from services.api.app.application.rule_admin import merge_rule_business_fields
from services.api.app.application.task_rule_inventory import build_task_rule_inventory
from services.api.app.domain.journey import parse_journey_definition
from services.api.tests.test_h5_shortflow_activation import definition_payload, task_config


def contract():
    definition = parse_journey_definition(definition_payload())
    return {
        "definition_id": definition.definition_id,
        "version": definition.version,
        "content_hash": definition.content_hash,
        "workflow_skill": "h5-player-workflow",
        "scenario_key": "pause",
        "evidence_conditions": [
            {
                "checkpoint_id": "ready",
                "requirements": [
                    item.to_dict() for item in definition.checkpoints[0].evidence_requirements
                ],
            }
        ],
    }


def configured_task(value):
    config = task_config()
    rule = deepcopy(config["rule_inventory"]["rules"][0])
    rule["judge_config"]["capability_config"]["journey_rule_contract"] = value
    config["rule_inventory"] = build_task_rule_inventory(
        ruleset_id="ruleset",
        ruleset_version=1,
        ruleset_snapshot_hash="rules",
        target_platform="h5",
        channel="api",
        task_config=config,
        rules=[rule],
    )
    return config


def test_business_payload_roundtrips_version_scene_and_evidence_conditions():
    payload = RuleBusinessPayload.model_validate(
        dict(
            name="暂停反馈",
            check_point="连续观察",
            applicable_page_types=["video_detail"],
            evidence_input_scope="journey",
            journey_rule_contract=contract(),
        )
    )
    assert payload.model_dump()["journey_rule_contract"] == contract()


def test_business_merge_preserves_gate_review_and_original_row():
    row = dict(
        input_contract={"target_clients": ["H5"], "quality_gate": {"min_screenshot_count": 2}},
        judge_config={"thresholds": {"confidence": 0.9}, "capability_config": {}},
        manual_review_policy={"min_confidence": 0.9},
    )
    original = deepcopy(row)
    result = merge_rule_business_fields(
        row, evidence_input_scope="journey", journey_rule_contract=contract()
    )
    assert result["judge_config"]["capability_config"]["journey_rule_contract"] == contract()
    assert result["input_contract"] == original["input_contract"]
    assert result["judge_config"]["thresholds"] == original["judge_config"]["thresholds"]
    assert result["manual_review_policy"] == original["manual_review_policy"]
    assert row == original


@pytest.mark.parametrize("fault", ["budget", "locator", "count", "empty_scene", "unknown_field"])
def test_editor_rejects_untrusted_system_or_invalid_condition_fields(fault):
    value = contract()
    if fault == "count":
        value["evidence_conditions"][0]["requirements"][0]["minimum_count"] = True
    elif fault == "empty_scene":
        value["scenario_key"] = " "
    else:
        value[fault] = 200
    with pytest.raises(ValidationError):
        RuleBusinessPayload.model_validate(
            dict(
                name="暂停",
                check_point="连续观察",
                applicable_page_types=["video_detail"],
                journey_rule_contract=value,
            )
        )


@pytest.mark.parametrize(
    "fault", ["scene", "definition", "hash", "skill", "checkpoint", "unsupported_count"]
)
async def test_rule_selection_cannot_ignore_configured_contract_before_budget(fault):
    value = contract()
    if fault == "scene":
        value["scenario_key"] = "fullscreen"
    elif fault == "definition":
        value["definition_id"] = "foreign-player"
    elif fault == "hash":
        value["content_hash"] = "0" * 64
    elif fault == "skill":
        value["workflow_skill"] = "h5-payment-workflow"
    elif fault == "checkpoint":
        value["evidence_conditions"][0]["checkpoint_id"] = "foreign"
    else:
        value["evidence_conditions"][0]["requirements"][0]["minimum_count"] = 2
    repo = SimpleNamespace(
        get=AsyncMock(return_value=parse_journey_definition(definition_payload()))
    )
    with pytest.raises(H5ShortflowError):
        await freeze_h5_shortflows(task_config=configured_task(value), repository=repo)


async def test_configured_rule_still_uses_published_checkpoint_and_frozen_hash():
    config = configured_task(contract())
    repo = SimpleNamespace(
        get=AsyncMock(return_value=parse_journey_definition(definition_payload()))
    )
    frozen = await freeze_h5_shortflows(task_config=config, repository=repo)
    assert frozen["h5_shortflows"][0]["definition"] == definition_payload()
    assert frozen["h5_shortflows"][0]["rule_contract_hashes"] == [
        config["rule_inventory"]["rules"][0]["contract_hash"]
    ]


@pytest.mark.parametrize(
    "fault", [None, "missing_definition", "wrong_hash", "shared_platform", "readonly_account"]
)
def test_public_business_patch_uses_published_source_and_preserves_system_fields(fault):
    from services.api.tests.test_rules_api import FakeRuleRepository
    from services.api.tests.test_rules_business_api import RULE_ID, _admin, _client, _rule, _ruleset

    original = _rule(status="disabled")
    if fault == "shared_platform":
        original["input_contract"]["target_clients"] = ["H5", "APP"]
    repo = FakeRuleRepository(rulesets=[_ruleset()], rules=[original])
    repo.get_journey_definition = AsyncMock(
        return_value=None
        if fault == "missing_definition"
        else parse_journey_definition(definition_payload())
    )
    value = contract()
    if fault == "wrong_hash":
        value["content_hash"] = "0" * 64
    api = _client(repo, ["qcRules"]) if fault == "readonly_account" else _admin(repo)
    response = api.patch(
        f"/api/v1/rules/{RULE_ID}",
        json=dict(
            name=original["name"],
            check_point=original["check_point"],
            applicable_page_types=original["applicable_page_types"],
            evidence_input_scope="journey",
            journey_rule_contract=value,
        ),
    )
    if fault:
        assert response.status_code == (403 if fault == "readonly_account" else 422)
        assert not any(call == "update_rule" for call, _ in repo.calls)
    else:
        assert response.status_code == 200, response.text
        saved = response.json()
        assert saved["judge_config"]["capability_config"]["journey_rule_contract"] == value
        assert saved["judge_config"]["llm_checks"] == original["judge_config"]["llm_checks"]
        assert saved["input_contract"] == original["input_contract"]
        assert saved["manual_review_policy"] == original["manual_review_policy"]


def test_new_page_rule_cannot_smuggle_journey_config_or_turn_into_500():
    from services.api.tests.test_rules_api import FakeRuleRepository
    from services.api.tests.test_rules_business_api import RULESET_ID, _admin, _ruleset

    repo = FakeRuleRepository(rulesets=[_ruleset()], rules=[])
    response = _admin(repo).post(
        f"/api/v1/rulesets/{RULESET_ID}/rules",
        json=dict(
            name="暂停",
            check_point="观察",
            applicable_page_types=["video_detail"],
            evidence_input_scope="page",
            journey_rule_contract=contract(),
        ),
    )
    assert response.status_code == 422
    assert not any(call == "create_rule" for call, _ in repo.calls)


@pytest.mark.parametrize("value", ["malformed", {"definition_id": "missing"}])
def test_full_create_also_rejects_invalid_journey_contract(value):
    from services.api.app.application.rule_admin import rule_payload_from_row
    from services.api.tests.test_rules_api import FakeRuleRepository
    from services.api.tests.test_rules_business_api import _admin, _rule, _ruleset

    repo = FakeRuleRepository(rulesets=[_ruleset()], rules=[])
    repo.get_journey_definition = AsyncMock(return_value=None)
    payload = rule_payload_from_row(_rule(status="disabled"))
    payload["judge_config"]["capability_config"] = {
        "evidence_input_scope": "journey",
        "journey_rule_contract": value,
    }
    response = _admin(repo).post("/api/v1/rules", json=payload)
    assert response.status_code == 422
    assert not any(call == "create_rule" for call, _ in repo.calls)


def test_unavailable_published_version_cannot_block_safe_disable():
    from services.api.tests.test_rules_api import FakeRuleRepository
    from services.api.tests.test_rules_business_api import RULE_ID, _admin, _rule, _ruleset

    row = _rule()
    row["judge_config"]["capability_config"] = {
        "evidence_input_scope": "journey",
        "journey_rule_contract": contract(),
    }
    repo = FakeRuleRepository(rulesets=[_ruleset()], rules=[row])
    repo.get_journey_definition = AsyncMock(return_value=None)
    response = _admin(repo).patch(f"/api/v1/rules/{RULE_ID}/status", json={"status": "disabled"})
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "disabled"
    repo.get_journey_definition.assert_not_called()
