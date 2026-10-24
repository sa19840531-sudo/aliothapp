"""现有 V2 台账到前端的断线复现，不把受控材料当原站验收。"""

from copy import deepcopy
from hashlib import sha256
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zipfile import ZipFile

import pytest

from services.api.app.api.v1.rules import RuleBusinessPayload
from services.api.app.application.rule_admin import merge_rule_business_fields
from services.api.app.domain.journey import parse_journey_definition
from services.api.tests.test_h5_shortflow_activation import definition_payload


def test_business_editor_preserves_explicit_journey_target():
    request = RuleBusinessPayload.model_validate(
        dict(
            name="暂停反馈",
            check_point="连续观测暂停反馈",
            applicable_page_types=["video_detail"],
            evidence_input_scope="journey",
        )
    )
    assert request.model_dump()["evidence_input_scope"] == "journey"


def test_enabled_rule_scope_edit_cannot_skip_executable_guard():
    from services.api.tests.test_rules_api import FakeRuleRepository
    from services.api.tests.test_rules_business_api import RULE_ID, _admin, _rule, _ruleset

    repository = FakeRuleRepository(
        rulesets=[_ruleset()], rules=[_rule(judge_config={}, executor_type="llm")]
    )
    response = _admin(repository).patch(
        f"/api/v1/rules/{RULE_ID}",
        json=dict(
            name="暂停反馈",
            check_point="连续观测",
            applicable_page_types=["video_detail"],
            evidence_input_scope="journey",
        ),
    )
    assert response.status_code == 422
    assert not any(call == "update_rule" for call, _ in repository.calls)


def test_new_journey_rule_is_disabled_and_h5_only_without_gate_relaxation():
    from services.api.tests.test_rules_api import FakeRuleRepository
    from services.api.tests.test_rules_business_api import RULESET_ID, _admin, _ruleset

    repository = FakeRuleRepository(rulesets=[_ruleset()], rules=[])
    response = _admin(repository).post(
        f"/api/v1/rulesets/{RULESET_ID}/rules",
        json=dict(
            name="暂停反馈",
            check_point="连续观测",
            applicable_page_types=["video_detail"],
            evidence_input_scope="journey",
        ),
    )
    assert response.status_code == 201, response.text
    assert response.json()["status"] == "disabled"
    assert response.json()["input_contract"]["target_clients"] == ["H5"]
    assert response.json()["manual_review_policy"]["min_confidence"] == 0.82


def test_journey_business_patch_keeps_protection_and_does_not_modify_source():
    row = dict(
        judge_config={
            "capability_config": {"semantic_judge": "semantic_rule_judge_tool"},
            "thresholds": {"confidence": 0.9},
        },
        input_contract={
            "target_clients": ["H5"],
            "quality_gate": {"allow_empty_visible_text": False},
        },
        manual_review_policy={"min_confidence": 0.9},
    )
    original = deepcopy(row)
    result = merge_rule_business_fields(row, evidence_input_scope="journey")
    assert (
        result["judge_config"]["capability_config"]["evidence_scope_key"] == "journey_observation"
    )
    assert result["judge_config"]["thresholds"] == original["judge_config"]["thresholds"]
    assert result["input_contract"] == original["input_contract"]
    assert row == original


@pytest.mark.parametrize("scope", ["task", "environment_matrix", "web_sequence"])
def test_business_patch_cannot_erase_other_aggregate_contract(scope):
    row = {"judge_config": {"capability_config": {"evidence_input_scope": scope}}}
    with pytest.raises(ValueError):
        merge_rule_business_fields(row, evidence_input_scope="page")


@pytest.mark.parametrize("clients", [[], ["APP"], ["H5", "APP"], ["H5", "WEB"]])
def test_journey_editor_does_not_change_shared_platform_rule_routing(clients):
    row = {"input_contract": {"target_clients": clients}, "judge_config": {}}
    with pytest.raises(ValueError):
        merge_rule_business_fields(row, evidence_input_scope="journey")


def test_catalogue_uses_supported_published_version_without_locators():
    from services.api.app.application.h5_shortflow_read import definition_summary

    definition = parse_journey_definition(definition_payload())
    result = definition_summary(definition)
    assert result["content_hash"] == definition.content_hash
    assert result["workflow_skill"] == "h5-player-workflow"
    assert result["checkpoints"] == [{"checkpoint_id": "ready", "depends_on": []}]
    assert "expected" not in str(result)
    bad = definition_payload()
    bad["checkpoints"][0]["expected"] = {"order_created": True}
    assert definition_summary(parse_journey_definition(bad)) is None


def snapshot():
    content = b"controlled-original-frame"
    artifact = dict(
        artifact_id="frame",
        artifact_type="screenshot",
        checksum="sha256:" + sha256(content).hexdigest(),
        content_type="image/png",
        size_bytes=len(content),
    )
    return dict(
        task_id="task",
        snapshot_hash="f" * 64,
        plans=[
            dict(
                execution_plan_id="plan",
                checkpoints=[
                    dict(
                        checkpoint_id="ready",
                        attempts=[
                            dict(
                                attempt_id="a",
                                bindings=[
                                    dict(
                                        role="before_action",
                                        capture_id="before",
                                        integrity="current",
                                        artifacts=[artifact],
                                    ),
                                    dict(
                                        role="after_action",
                                        capture_id="after",
                                        integrity="current",
                                        artifacts=[artifact],
                                    ),
                                ],
                            )
                        ],
                    )
                ],
            )
        ],
    ), content


async def test_group_export_preserves_roles_even_with_same_checksum():
    from services.api.app.application.h5_shortflow_read import build_shortflow_archive

    view, content = snapshot()
    reader = SimpleNamespace(evidence_file=AsyncMock(return_value={"content": content}))
    result = await build_shortflow_archive(view, "plan", reader)
    with ZipFile(BytesIO(result)) as archive:
        assert len(archive.namelist()) == 3
        assert any("before_action" in name for name in archive.namelist())
        assert any("after_action" in name for name in archive.namelist())
        assert b'"snapshot_hash"' in archive.read("receipt.json")
    reader.evidence_file.assert_called_with("frame", original=True)


async def test_export_unknown_media_keeps_original_binary_not_fake_png():
    from services.api.app.application.h5_shortflow_read import build_shortflow_archive

    view, content = snapshot()
    view["plans"][0]["checkpoints"][0]["attempts"][0]["bindings"][0]["artifacts"][0][
        "content_type"
    ] = None
    reader = SimpleNamespace(evidence_file=AsyncMock(return_value={"content": content}))
    with ZipFile(BytesIO(await build_shortflow_archive(view, "plan", reader))) as archive:
        assert any(name.endswith(".bin") for name in archive.namelist())


@pytest.mark.parametrize(
    "fault",
    [
        "foreign_plan",
        "missing_binding",
        "changed_manifest",
        "checksum",
        "path_role",
        "negative_size",
        "oversize",
    ],
)
async def test_group_export_fails_closed_not_partial_success(fault):
    from services.api.app.application.h5_shortflow_read import build_shortflow_archive

    view, content = snapshot()
    plan_id = "foreign" if fault == "foreign_plan" else "plan"
    bindings = view["plans"][0]["checkpoints"][0]["attempts"][0]["bindings"]
    if fault == "missing_binding":
        bindings.clear()
    if fault == "changed_manifest":
        bindings[0]["integrity"] = "manifest_changed"
    if fault == "checksum":
        content = b"broken"
    if fault == "path_role":
        bindings[0]["role"] = "../../../outside"
    if fault == "negative_size":
        bindings[0]["artifacts"][0]["size_bytes"] = -1
    if fault == "oversize":
        bindings[0]["artifacts"][0]["size_bytes"] = 65 * 1024 * 1024
    reader = SimpleNamespace(evidence_file=AsyncMock(return_value={"content": content}))
    with pytest.raises(ValueError):
        await build_shortflow_archive(view, plan_id, reader)
