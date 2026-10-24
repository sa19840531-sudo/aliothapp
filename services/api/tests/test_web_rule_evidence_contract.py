"""Web 规则序列合同必须在受权限保护的真实配置入口显式声明。"""

from copy import deepcopy

import pytest
from pydantic import ValidationError

from services.api.app.api.v1.rules import RulePayload


def _payload(web_evidence: dict, *, clients: list[str] | None = None) -> dict:
    return {
        "ruleset_id": "ruleset-1",
        "rule_code": "WEB_LOADING_SEQUENCE",
        "name": "持续加载判定",
        "category": "interaction",
        "executor_type": "llm",
        "check_point": "根据真实有界时间序列判定加载状态",
        "applicable_page_types": ["content_list"],
        "input_contract": {
            "target_clients": ["WEB"] if clients is None else clients,
            "web_evidence": web_evidence,
        },
    }


def _contract() -> dict:
    return {
        "schema_version": "web-rule-evidence.v1",
        "rule_version": "1",
        "accepted_roles": ["failure_sequence"],
        "required_artifact_types": ["screenshot", "dom"],
        "minimum_samples": 2,
        "minimum_duration_ms": 1000,
        "requires_action": True,
        "accept_unclassified": True,
    }


@pytest.mark.parametrize("executor", ["rule_engine", "hybrid"])
def test_sequence_contract_rejects_unsupported_executor(executor):
    payload = _payload(_contract())
    payload["executor_type"] = executor
    with pytest.raises(ValidationError, match="Web"):
        RulePayload(**payload)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("minimum_samples", 1),
        ("minimum_samples", True),
        ("minimum_duration_ms", -1),
        ("rule_version", ""),
        ("accepted_roles", ["unknown"]),
        ("required_artifact_types", []),
        ("requires_action", "yes"),
        ("accept_unclassified", "yes"),
    ],
)
def test_rules_configuration_rejects_malformed_web_evidence(field, value):
    contract = _contract()
    contract[field] = value
    with pytest.raises(ValidationError, match="Web"):
        RulePayload(**_payload(contract))


@pytest.mark.parametrize("clients", [[], ["H5"], ["WEB", "H5"], ["APP"]])
def test_web_sequence_contract_cannot_change_shared_rule_default(clients):
    with pytest.raises(ValidationError, match="Web"):
        RulePayload(**_payload(_contract(), clients=clients))


def test_rules_configuration_preserves_explicit_web_contract():
    payload = RulePayload(**_payload(_contract()))
    assert payload.input_contract["web_evidence"] == _contract()


def test_rule_configuration_preserves_supported_local_structure_declaration():
    contract = _contract()
    contract["local_structure"] = {
        "schema_version": "web-local-structure-rule.v1",
        "scope": "current_document_light_dom",
        "predicate": "native_video_element_present",
        "reuse_policy": "same_material_context",
    }
    assert RulePayload(**_payload(contract)).input_contract["web_evidence"] == contract


@pytest.mark.parametrize(
    "field,value",
    [
        ("predicate", "arbitrary_selector"),
        ("scope", "whole_product"),
        ("reuse_policy", "once_per_site"),
    ],
)
def test_rule_configuration_rejects_unsupported_local_structure(field, value):
    contract = _contract()
    contract["local_structure"] = {
        "schema_version": "web-local-structure-rule.v1",
        "scope": "current_document_light_dom",
        "predicate": "native_video_element_present",
        "reuse_policy": "same_material_context",
    }
    contract["local_structure"][field] = value
    with pytest.raises(ValidationError, match="Web"):
        RulePayload(**_payload(contract))


def _sequence():
    common = {
        "task_id": "task-1",
        "agent_run_id": "run-1",
        "session_id": "web:run-1",
        "finalization_epoch": 2,
        "evidence_mode": "natural",
        "artifact_types": ["screenshot", "dom"],
        "artifacts_verified": True,
        "target_id": "target-1",
        "occurrence_id": "occurrence-1",
        "page_state": "loading",
    }
    return {
        "task_id": "task-1",
        "agent_run_id": "run-1",
        "session_id": "web:run-1",
        "finalization_epoch": 2,
        "target_platform": "web",
        "workflow_version": "v2",
        "session_owner": "web-v2-workflow",
        "target_id": "target-1",
        "occurrence_id": "occurrence-1",
        "purpose": "failure_sequence",
        "frames": [
            {
                **common,
                "capture_id": "before-1",
                "capture_role": "before_action",
                "captured_at": "2026-09-10T00:00:00+00:00",
                "action_execution_id": "action-1",
                "parent_capture_id": "source-1",
                "target_id": "source",
                "page_state": "stable",
            },
            {
                **common,
                "capture_id": "after-1",
                "capture_role": "after_action",
                "captured_at": "2026-09-10T00:00:10+00:00",
                "action_execution_id": "action-1",
                "parent_capture_id": "before-1",
            },
            {
                **common,
                "capture_id": "before-2",
                "capture_role": "before_action",
                "captured_at": "2026-09-10T00:00:11+00:00",
                "action_execution_id": "action-2",
                "parent_capture_id": "after-1",
            },
            {
                **common,
                "capture_id": "after-2",
                "capture_role": "after_action",
                "captured_at": "2026-09-10T00:00:12+00:00",
                "action_execution_id": "action-2",
                "parent_capture_id": "before-2",
            },
        ],
    }


def _validate(sequence, *, duration=1000, roles=None):
    from services.api.app.application.web_rule_evidence_contract import (
        read_web_rule_evidence_contract,
    )

    raw = _contract()
    raw["minimum_duration_ms"] = duration
    if roles:
        raw["accepted_roles"] = roles
        raw["accept_unclassified"] = False
    contract = read_web_rule_evidence_contract(_payload(raw)["input_contract"])
    return contract.validate_sequence(
        sequence, task_id="task-1", agent_run_id="run-1", finalization_epoch=2
    )


def test_loading_sequence_can_be_judged_but_cannot_prove_stable_exposure():
    result = _validate(_sequence())
    assert result.status == "admissible"
    assert result.sample_capture_ids == ("after-1", "after-2")
    assert result.duration_ms == 2000
    assert _validate(_sequence(), roles=["stable_page"]).status == "evidence_insufficient"


def test_navigation_before_frame_does_not_count_as_loading_duration():
    assert _validate(_sequence(), duration=3000).status == "evidence_insufficient"


@pytest.mark.parametrize(
    "mutation",
    [
        "one_sample",
        "missing_before",
        "unordered",
        "cross_run",
        "cross_target",
        "cross_occurrence",
        "diagnostic",
        "missing_file",
        "h5",
        "wrong_epoch",
        "broken_parent",
        "wrong_owner",
        "duplicate_sample",
    ],
)
def test_sequence_rejects_unbound_or_insufficient_material(mutation):
    value = deepcopy(_sequence())
    if mutation == "one_sample":
        value["frames"] = value["frames"][:2]
    elif mutation == "missing_before":
        value["frames"].pop(2)
    elif mutation == "unordered":
        value["frames"][3]["captured_at"] = "2026-09-09T00:00:00+00:00"
    elif mutation == "cross_run":
        value["frames"][3]["agent_run_id"] = "other-run"
    elif mutation == "cross_target":
        value["frames"][3]["target_id"] = "other-target"
    elif mutation == "cross_occurrence":
        value["frames"][3]["occurrence_id"] = "other-occurrence"
    elif mutation == "diagnostic":
        value["frames"][3]["evidence_mode"] = "diagnostic"
    elif mutation == "missing_file":
        value["frames"][3]["artifacts_verified"] = False
    elif mutation == "h5":
        value["target_platform"] = "h5"
    elif mutation == "wrong_epoch":
        value["finalization_epoch"] = 1
    elif mutation == "broken_parent":
        value["frames"][3]["parent_capture_id"] = "before-1"
    elif mutation == "wrong_owner":
        value["session_owner"] = "h5-v2-workflow"
    elif mutation == "duplicate_sample":
        value["frames"][3]["capture_id"] = "after-1"
    assert _validate(value).status == "evidence_insufficient"
