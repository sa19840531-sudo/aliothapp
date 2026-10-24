"""冻结分项定位只消费同一次规则输入的原绑定，不按最新截图猜证据。"""

from copy import deepcopy

import pytest


def result():
    return {
        "execution_id": "execution",
        "input_set_id": "inputs",
        "execution_plan_id": "plan",
        "scenario_key": "pause",
        "execution_status": "fail",
        "checkpoints": [
            {
                "checkpoint_id": "pause",
                "status": "fail",
                "checks": {"paused": False},
                "missing": [],
                "bindings": [
                    {
                        "role": role,
                        "capture_id": role,
                        "action_execution_id": "action",
                        "manifest_id": "manifest-" + role,
                        "manifest_version": 2,
                        "artifact_ids": [role + "-png", role + "-dom"],
                    }
                    for role in ("before_action", "after_action")
                ],
            }
        ],
    }


def project(value, **kwargs):
    from services.api.app.application.h5_journey_result_read import project_journey_result

    return project_journey_result(
        value,
        execution_id="execution",
        input_set_id="inputs",
        artifact_ids=[
            role + suffix
            for role in ("before_action", "after_action")
            for suffix in ("-png", "-dom")
        ],
        **kwargs,
    )


def test_frozen_failed_checkpoint_keeps_specific_action_and_original_members():
    value = result()
    original = deepcopy(value)
    projected = project(value)
    assert projected["checkpoints"][0]["bindings"] == value["checkpoints"][0]["bindings"]
    assert projected["checkpoints"][0]["checks"] == {"paused": False}
    assert projected["checkpoints"][0]["integrity"] == "bound"
    assert value == original


@pytest.mark.parametrize(
    "fault",
    [
        "execution",
        "input",
        "foreign_artifact",
        "duplicate_role",
        "duplicate_checkpoint",
        "same_capture",
    ],
)
def test_cross_execution_or_ambiguous_material_cannot_create_clickable_binding(fault):
    value = result()
    checkpoint = value["checkpoints"][0]
    if fault == "execution":
        value["execution_id"] = "foreign"
    elif fault == "input":
        value["input_set_id"] = "foreign"
    elif fault == "foreign_artifact":
        checkpoint["bindings"][0]["artifact_ids"] = ["foreign-png"]
    elif fault == "duplicate_role":
        checkpoint["bindings"][1]["role"] = "before_action"
    elif fault == "duplicate_checkpoint":
        value["checkpoints"].append(deepcopy(checkpoint))
    else:
        checkpoint["bindings"][1]["capture_id"] = checkpoint["bindings"][0]["capture_id"]
    assert project(value) is None


def test_review_checkpoint_remains_review_and_missing_material_has_no_bindings():
    value = result()
    value["execution_status"] = "evidence_insufficient"
    value["checkpoints"][0].update(
        status="evidence_insufficient", missing=["measurement:paused"], bindings=[]
    )
    projected = project(value)
    assert projected["checkpoints"][0]["status"] == "evidence_insufficient"
    assert projected["checkpoints"][0]["integrity"] == "missing"
    assert projected["checkpoints"][0]["bindings"] == []


@pytest.mark.parametrize("field", ["checkpoint_id", "role", "capture_id"])
def test_malformed_result_identity_is_not_hashable_or_clickable(field):
    value = result()
    if field == "checkpoint_id":
        value["checkpoints"][0][field] = {"forged": True}
    else:
        value["checkpoints"][0]["bindings"][0][field] = {"forged": True}
    assert project(value) is None


def test_binding_projection_does_not_publish_unregistered_runtime_fields():
    value = result()
    value["checkpoints"][0]["bindings"][0]["locator"] = {"selector": "private"}
    projected = project(value)
    assert "locator" not in projected["checkpoints"][0]["bindings"][0]


def test_malformed_missing_reasons_cannot_break_checkpoint_display():
    value = result()
    value["checkpoints"][0]["missing"] = {"private": "object"}
    assert project(value) is None
