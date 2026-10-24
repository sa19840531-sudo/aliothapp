"""Journey DSL 领域契约测试。"""

import pytest

from services.api.app.domain.journey import (
    JourneyValidationError,
    build_journey_execution_plan,
    parse_journey_definition,
)


def _payload() -> dict:
    return {
        "schema_version": "journey-dsl.v1",
        "definition_id": "login-and-submit",
        "version": 1,
        "name": "登录并提交",
        "surface_kinds": ["web", "android"],
        "steps": [
            {
                "id": "open-login",
                "type": "action",
                "operation": "tap",
                "semantic_target": "登录按钮",
                "retry": {
                    "max_attempts": 2,
                    "backoff_ms": 200,
                    "retry_on": ["target_not_found"],
                },
            },
            {
                "id": "wait-form",
                "type": "wait",
                "condition": "element_visible",
                "semantic_target": "登录表单",
            },
            {
                "id": "capture-form",
                "type": "capture",
                "role": "post_action",
                "artifact_types": ["screenshot", "ui_snapshot"],
            },
            {
                "id": "assert-form",
                "type": "assert",
                "assertion": "element_visible",
                "semantic_target": "登录表单",
                "evidence_keys": ["ui_snapshot"],
            },
        ],
    }


def test_parser_builds_stable_immutable_definition() -> None:
    definition = parse_journey_definition(_payload())

    assert definition.content_hash == parse_journey_definition(_payload()).content_hash
    assert definition.version_ref.version == 1
    assert definition.steps[0].retry.max_attempts == 2
    with pytest.raises(TypeError):
        definition.steps[0].payload["operation"] = "swipe"


def test_parser_rejects_implicit_extension_fields() -> None:
    payload = _payload()
    payload["steps"][0]["selector"] = "#login"

    with pytest.raises(JourneyValidationError, match="未声明字段"):
        parse_journey_definition(payload)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("semantic_target", "#login"),
        ("semantic_target", "//button[@id='login']"),
        ("arguments", {"appium_locator": "accessibility=登录"}),
    ],
)
def test_parser_rejects_platform_specific_locators(field, value) -> None:
    payload = _payload()
    payload["steps"][0][field] = value

    with pytest.raises(JourneyValidationError, match="平台定位|语义目标"):
        parse_journey_definition(payload)


def test_parser_rejects_unknown_action_operation() -> None:
    payload = _payload()
    payload["steps"][0]["operation"] = "run_arbitrary_script"

    with pytest.raises(JourneyValidationError, match="operation 不受支持"):
        parse_journey_definition(payload)


def test_parser_rejects_invalid_wait_and_duplicate_steps() -> None:
    payload = _payload()
    payload["steps"][1].pop("condition")
    payload["steps"][1].pop("semantic_target")
    with pytest.raises(JourneyValidationError, match="condition 或 duration_ms"):
        parse_journey_definition(payload)

    payload = _payload()
    payload["steps"][1]["id"] = "open-login"
    with pytest.raises(JourneyValidationError, match="重复 id"):
        parse_journey_definition(payload)


def test_execution_plan_locks_definition_rules_and_capture_plan() -> None:
    definition = parse_journey_definition(_payload())

    plan = build_journey_execution_plan(
        definition,
        task_id="task-1",
        rule_contract_hashes=("rule-hash-2", "rule-hash-1"),
        capture_plan_id="capture-plan-1",
    )

    assert plan.journey_version.content_hash == definition.content_hash
    assert plan.steps == definition.steps
    assert plan.rule_contract_hashes == ("rule-hash-1", "rule-hash-2")
    assert plan.execution_plan_id.startswith("journey-plan:")
    assert plan.execution_plan_id == build_journey_execution_plan(
        definition,
        task_id="task-1",
        rule_contract_hashes=("rule-hash-1", "rule-hash-2"),
        capture_plan_id="capture-plan-1",
    ).execution_plan_id
