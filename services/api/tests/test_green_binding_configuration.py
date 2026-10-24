"""新配置通过现有构造入口校验并参与冻结摘要，不能改变默认或历史定义。"""

import copy

import pytest

from services.api.app.application.green_rule_catalog import build_green_rule_payload
from services.api.app.application.inspection_work_items import build_inspection_rule_contract_hash


def binding():
    return {
        "schema_version": "green-shared-bindings.v1",
        "subjects": [
            {
                "subject_id": "module/request",
                "fields": {
                    "temporal": {
                        "artifact_type": "temporal_trace",
                        "capture_role": "after_action",
                        "schema_version": "native-temporal.v1",
                        "path": ["observations"],
                    }
                },
            }
        ],
    }


def test_existing_constructor_accepts_valid_binding_and_freezes_its_entire_contract():
    original = binding()
    rule = build_green_rule_payload("s72", shared_bindings=original)
    configured = copy.deepcopy(rule)
    original["subjects"][0]["fields"]["temporal"]["path"] = ["caller_override"]
    assert rule == configured
    assert rule["status"] == "disabled"
    assert build_inspection_rule_contract_hash(rule) != build_inspection_rule_contract_hash(
        build_green_rule_payload("s72"),
    )
    rule["judge_config"]["green_shared_bindings"]["subjects"][0]["subject_id"] = "other"
    assert build_inspection_rule_contract_hash(rule) != build_inspection_rule_contract_hash(
        configured
    )


@pytest.mark.parametrize("invalid", [[], {}, {"schema_version": "untrusted"}])
def test_constructor_rejects_invalid_bindings_before_any_publication(invalid):
    with pytest.raises(ValueError):
        build_green_rule_payload("s72", shared_bindings=invalid)


@pytest.mark.parametrize("key", ["32", "41", "s72", "s35", "s49", "s73", "62"])
def test_content_policy_cannot_be_attached_to_unrelated_rule(key):
    with pytest.raises(ValueError, match="102"):
        build_green_rule_payload(key, content_policies={})


def test_default_constructor_remains_without_guessed_policies_or_binding():
    for key in ("102", "32", "41", "s72", "s35", "s49", "s73", "62"):
        config = build_green_rule_payload(key)["judge_config"]
        assert not set(config) & {
            "green_shared_bindings",
            "green_content_policies",
            "green_content_scope_contract",
        }
