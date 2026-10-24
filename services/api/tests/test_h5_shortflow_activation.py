"""公开短流程入口缺失复现，以及版本、规则、权限和重复场景反例。"""

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from services.api.app.api.v2.schemas import V2TaskSubmissionRequest
from services.api.app.application.task_rule_inventory import build_task_rule_inventory
from services.api.app.domain.journey import parse_journey_definition
from services.api.tests.test_journey_checkpoint_contracts import checkpoint_definition
from services.api.tests.test_v2_task_entrypoints import _h5_task_payload, _web_task_payload


def definition_payload():
    payload = checkpoint_definition("controlled-player")
    checkpoint = payload["checkpoints"][0]
    checkpoint.update(
        entry_condition={"playing": True},
        expected={"paused": True},
        exit_condition={"observation_complete": True},
    )
    checkpoint["evidence_requirements"] = [
        {"role": "after_pause", "artifact_types": ["screenshot", "dom", "action_receipt"]}
    ]
    payload["checkpoints"] = [checkpoint]
    return payload


def selection():
    definition = parse_journey_definition(definition_payload())
    return dict(
        definition_id=definition.definition_id,
        version=1,
        content_hash=definition.content_hash,
        scenario_key="pause",
        rule_ids=["rule-player"],
        workflow_skill="h5-player-workflow",
    )


def task_config():
    config = dict(
        workflow_version="v2", execution_mode="full_inspection", h5_shortflows=[selection()]
    )
    config["rule_inventory"] = build_task_rule_inventory(
        ruleset_id="ruleset",
        ruleset_version=1,
        ruleset_snapshot_hash="rules",
        target_platform="h5",
        channel="api",
        task_config=config,
        rules=[
            dict(
                rule_id="rule-player",
                rule_code="player-pause",
                applicable_page_types=["video_detail"],
                judge_config={
                    "capability_config": {
                        "evidence_input_scope": "journey",
                        "evidence_scope_key": "journey_observation",
                    }
                },
            )
        ],
    )
    config["ruleset_rule_total"] = 1
    return config


def test_public_entry_preserves_explicit_shortflow_selection():
    request = V2TaskSubmissionRequest.model_validate(
        _h5_task_payload() | {"h5_shortflows": [selection()]}
    )
    assert request.to_command().h5_shortflows[0]["definition_id"] == "controlled-player"


@pytest.mark.parametrize("fault", ["extra", "duplicate", "foreign_platform", "empty_rules"])
def test_public_entry_rejects_invalid_shortflow_without_silent_dropping(fault):
    payload = _h5_task_payload() | {"h5_shortflows": [selection()]}
    if fault == "extra":
        payload["h5_shortflows"][0]["locator"] = "#pay"
    elif fault == "duplicate":
        payload["h5_shortflows"].append(selection())
    elif fault == "foreign_platform":
        payload["target"] = _web_task_payload()["target"]
    else:
        payload["h5_shortflows"][0]["rule_ids"] = []
    with pytest.raises(ValidationError):
        V2TaskSubmissionRequest.model_validate(payload)


async def test_freeze_uses_published_definition_and_actual_frozen_rule_hash():
    from services.api.app.application.h5_shortflows import freeze_h5_shortflows

    config = task_config()
    repository = SimpleNamespace(
        get=AsyncMock(return_value=parse_journey_definition(definition_payload()))
    )
    frozen = await freeze_h5_shortflows(task_config=config, repository=repository)
    item = frozen["h5_shortflows"][0]
    assert item["definition"] == definition_payload()
    assert item["rule_contract_hashes"] == [config["rule_inventory"]["rules"][0]["contract_hash"]]
    assert len(item["workflow_skill_hash"]) == 64
    assert config == task_config()


@pytest.mark.parametrize(
    "fault",
    [
        "hash",
        "unknown_rule",
        "page_rule",
        "unsafe",
        "unsupported",
        "duplicate_rule_binding",
        "missing_inventory",
    ],
)
async def test_freeze_rejects_unmeasurable_or_ambiguous_contract_before_execution(fault):
    from services.api.app.application.h5_shortflows import H5ShortflowError, freeze_h5_shortflows

    config = task_config()
    payload = definition_payload()
    if fault == "hash":
        config["h5_shortflows"][0]["content_hash"] = "0" * 64
    elif fault == "unknown_rule":
        config["h5_shortflows"][0]["rule_ids"] = ["unknown"]
    elif fault == "page_rule":
        rule = config["rule_inventory"]["rules"][0]
        rule["judge_config"] = {}
        config["rule_inventory"] = build_task_rule_inventory(
            ruleset_id="ruleset",
            ruleset_version=1,
            ruleset_snapshot_hash="rules",
            target_platform="h5",
            channel="api",
            task_config=config,
            rules=[rule],
        )
    elif fault == "unsafe":
        payload["checkpoints"][0]["side_effect_policy"] = "approval_required"
    elif fault == "unsupported":
        payload["checkpoints"][0]["expected"] = {"order_created": False}
    elif fault == "duplicate_rule_binding":
        second = deepcopy(selection())
        second["scenario_key"] = "other"
        config["h5_shortflows"].append(second)
    else:
        config.pop("rule_inventory")
    definition = parse_journey_definition(payload)
    if fault in {"unsafe", "unsupported"}:
        config["h5_shortflows"][0]["content_hash"] = definition.content_hash
    repository = SimpleNamespace(get=AsyncMock(return_value=definition))
    with pytest.raises(H5ShortflowError):
        await freeze_h5_shortflows(task_config=config, repository=repository)


async def test_unselected_task_never_reads_definitions():
    from services.api.app.application.h5_shortflows import freeze_h5_shortflows

    repository = SimpleNamespace(get=AsyncMock())
    assert await freeze_h5_shortflows(task_config={}, repository=repository) == {}
    repository.get.assert_not_awaited()


@pytest.mark.parametrize("selected_in_exit", [False, True])
async def test_registered_payment_display_can_freeze_without_claiming_order_state(selected_in_exit):
    from services.api.app.application.h5_shortflows import freeze_h5_shortflows
    from services.api.tests.test_h5_payment_checkpoint import payment_case

    checkpoint = payment_case()[0]
    payload = definition_payload()
    payload["checkpoints"] = [checkpoint.to_dict()]
    if selected_in_exit:
        payload["checkpoints"][0]["expected"] = {"payment_display_visible": True}
        payload["checkpoints"][0]["exit_condition"]["payment_plan_selected"] = True
    definition = parse_journey_definition(payload)
    config = task_config()
    config["h5_shortflows"][0].update(
        content_hash=definition.content_hash, workflow_skill="h5-payment-workflow"
    )
    frozen = await freeze_h5_shortflows(
        task_config=config, repository=SimpleNamespace(get=AsyncMock(return_value=definition))
    )
    assert frozen["h5_shortflows"][0]["workflow_skill"] == "h5-payment-workflow"
    expected = frozen["h5_shortflows"][0]["definition"]["checkpoints"][0]["expected"]
    assert "order_created" not in expected


def test_shipped_templates_have_measurable_obligations_and_no_unknown_rules():
    import json
    from pathlib import Path

    from services.api.app.application.h5_checkpoint_observation import supports_checkpoint

    path = Path(__file__).parents[3] / "docs/data/journey_definitions/h5_shortflows_v2.json"
    definitions = [
        parse_journey_definition(item) for item in json.loads(path.read_text(encoding="utf-8"))
    ]
    assert len(definitions) == 2
    for definition in definitions:
        assert all(supports_checkpoint(item) for item in definition.checkpoints)
        assert all(item.side_effect_policy == "read_only" for item in definition.checkpoints)
        assert all("order_created" not in item.expected for item in definition.checkpoints)


async def test_production_provider_activates_opt_in_before_creating_capture_runtime(monkeypatch):
    from services.api.app.core.config import Settings
    from services.api.app.infrastructure.db import h5_shortflow_activation as activation
    from services.api.tests.test_h5_runtime_composition import _h5_record, _runtime_module

    record = _h5_record()
    record.task_config["h5_shortflows"] = [selection()]
    events = []

    async def activate(**kwargs):
        assert kwargs["task_id"] == record.task_id
        assert kwargs["agent_run_id"] == record.run_id
        events.append("activation")

    def capture(**kwargs):
        events.append("capture")
        return SimpleNamespace(
            h5_capture_recovery=object(),
            h5_exploration_coordinator=object(),
            surface_driver=object(),
            surface_sessions=object(),
            session_command_gate=object(),
            trusted_ui_snapshot_reader=object(),
        )

    monkeypatch.setattr(activation, "activate_h5_shortflows_for_run", activate)
    runtime = (
        await _runtime_module()
        .PostgresH5RuntimeProvider(
            worker_instance_id="controlled",
            capture_runtime_factory=capture,
        )
        .build(record=record, settings=Settings())
    )
    assert events == ["activation", "capture"]
    assert runtime.turn_runner._adaptive_journey._planner._shortflow_delegate_factory.__name__ == (
        "build_h5_shortflow_agent"
    )


def test_post_action_only_fact_cannot_be_admitted_as_entry_condition():
    from services.api.app.application.h5_checkpoint_observation import supports_checkpoint

    payload = definition_payload()
    payload["checkpoints"][0]["entry_condition"] = {"payment_plan_selected": True}
    assert not supports_checkpoint(parse_journey_definition(payload).checkpoints[0])


@pytest.mark.parametrize(
    "field", ["player_visible", "paused", "media_ready", "current_time", "document_time_origin_ms"]
)
def test_missing_native_measurement_remains_unknown_not_false_or_null_fact(field):
    from services.api.app.application.h5_checkpoint_observation import checkpoint_facts

    snapshot = dict(
        document={}, elements=[dict(tag="video", h5_media_instance_id="media", media={})]
    )
    assert field not in checkpoint_facts(snapshot)
