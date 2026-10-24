"""规则局部输入、依赖闭包和完整流程门禁的可复用合同回归。"""

from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from services.api.app.application.aggregate_inspection_execution import evaluate_aggregate_context
from services.api.app.domain.journey import parse_journey_definition, parse_journey_execution_plan
from services.api.app.domain.journey.definitions import build_run_journey_execution_plan
from services.api.app.domain.journey.rule_selection import (
    contract_checkpoints,
    rule_selection_payload,
    select_rule_checkpoints,
)
from services.api.app.domain.journey.validation import stable_hash
from services.api.tests.test_h5_checkpoint_consumption import journey_context


def selection_context(*, dependency=False, extra_material=False):
    context = journey_context()
    material = deepcopy(context.journey_input)
    original = material["contract"]["plan"]["checkpoints"][0]
    if extra_material:
        original["evidence_requirements"][0]["artifact_types"].append("dom")
    other = {**deepcopy(original), "id": "unrelated", "depends_on": []}
    if dependency:
        original["depends_on"] = ["unrelated"]
    definition = parse_journey_definition({
        "schema_version": "journey-dsl.v2", "definition_id": "selection",
        "version": 1, "name": "检查点选择", "surface_kinds": ["web"],
        "checkpoints": [other, original] if dependency else [original, other],
    })
    plan = build_run_journey_execution_plan(
        definition, task_id="task-1", agent_run_id="run-1", scenario_key="player",
        parameters={}, rule_contract_hashes=("contract-hash",),
    )
    conditions = [{"checkpoint_id": "paused", "requirements": [
        {"role": "after_pause", "artifact_types": ["media_state"]},
    ]}]
    material["contract"].update(
        schema_version="journey-input-contract.v2", plan=plan.to_dict(),
        journey_rule_contract={
            "definition_id": definition.definition_id, "version": definition.version,
            "content_hash": definition.content_hash, "workflow_skill": "h5-player-workflow",
            "scenario_key": "player", "evidence_conditions": conditions,
        },
        rule_selection=rule_selection_payload(plan, conditions),
    )
    return replace(context, journey_input=material), plan, conditions


def test_rule_checkpoint_selection_excludes_unrelated_and_preserves_original_plan():
    context, plan, conditions = selection_context()
    original = plan.to_dict()
    selected = select_rule_checkpoints(plan, conditions)
    assert [item.checkpoint_id for item in selected] == ["paused"]
    assert selected[0].evidence_requirements[0].artifact_types == ("media_state",)
    assert plan.to_dict() == original
    assert len(context.journey_input["contract"]["plan"]["checkpoints"]) == 2


def test_transitive_dependencies_keep_full_requirements_even_if_explicitly_narrowed():
    _, plan, conditions = selection_context(dependency=True)
    conditions.append({"checkpoint_id": "unrelated", "requirements": [
        {"role": "after_pause", "artifact_types": ["media_state"]},
    ]})
    selected = select_rule_checkpoints(plan, conditions)
    assert [item.checkpoint_id for item in selected] == ["unrelated", "paused"]
    assert selected[0].evidence_requirements == plan.checkpoints[0].evidence_requirements
    assert len(selected[0].evidence_requirements) == 2


@pytest.mark.parametrize(
    "fault", ["empty", "unknown", "duplicate", "role", "type", "phase", "count"],
)
def test_rule_selection_rejects_unsupported_or_ambiguous_material_obligations(fault):
    _, plan, conditions = selection_context()
    if fault == "empty":
        conditions = []
    elif fault == "unknown":
        conditions[0]["checkpoint_id"] = "foreign"
    elif fault == "duplicate":
        conditions.append(deepcopy(conditions[0]))
    else:
        requirement = conditions[0]["requirements"][0]
        if fault == "role":
            requirement["role"] = "foreign"
        elif fault == "type":
            requirement["artifact_types"] = ["dom"]
        elif fault == "phase":
            requirement["capture_phase"] = "before_action"
        else:
            requirement["minimum_count"] = 3
    with pytest.raises(ValueError):
        select_rule_checkpoints(plan, conditions)


def test_local_rule_can_conclude_when_unselected_checkpoint_has_no_observations():
    context, _, _ = selection_context()
    result = evaluate_aggregate_context(context)
    assert result.execution_status == "pass"
    checkpoints = result.wrapper_payload["journey"]["checkpoints"]
    assert [item["checkpoint_id"] for item in checkpoints] == ["paused"]
    assert checkpoints[0]["quality_gate_status"] == "pass"


def test_local_material_choice_does_not_erase_full_checkpoint_quality_gap():
    context, _, _ = selection_context(extra_material=True)
    result = evaluate_aggregate_context(context)
    assert result.execution_status == "pass"
    checkpoint = result.wrapper_payload["journey"]["checkpoints"][0]
    assert checkpoint["quality_gate_status"] == "evidence_insufficient"
    assert "evidence_role:after_pause" in checkpoint["quality_gate_missing"]


def test_missing_dependency_cannot_be_removed_by_rule_checkpoint_selection():
    context, _, _ = selection_context(dependency=True)
    result = evaluate_aggregate_context(context)
    assert result.execution_status == "evidence_insufficient"
    assert result.error_code == "JOURNEY_INPUT_CLOSURE_MISMATCH"


def test_frozen_rule_selection_hash_covers_closure_and_material_requirements():
    context, plan, _ = selection_context()
    contract = deepcopy(context.journey_input["contract"])
    original_hash = stable_hash(contract)
    contract["rule_selection"]["checkpoint_ids"].append("unrelated")
    assert stable_hash(contract) != original_hash
    with pytest.raises(ValueError):
        contract_checkpoints(contract, plan)


def test_legacy_v1_keeps_full_plan_semantics_when_rule_selection_exists():
    context, _, _ = selection_context()
    material = deepcopy(context.journey_input)
    material["contract"]["schema_version"] = "journey-input-contract.v1"
    verdict = evaluate_aggregate_context(replace(context, journey_input=material))
    assert verdict.execution_status == "evidence_insufficient"


async def test_legacy_frozen_input_revalidation_explicitly_uses_original_full_plan():
    from services.api.app.infrastructure.db.journey_input_repository import load_journey_judge_input

    context = journey_context()
    contract = {**context.journey_input["contract"], "schema_version": "journey-input-contract.v1"}
    connection = SimpleNamespace(fetchrow=AsyncMock(return_value={
        "selection_hash": stable_hash(contract), "contract_payload": contract,
    }))
    loader = AsyncMock(return_value={"contract": contract})
    with patch(
        "services.api.app.infrastructure.db.journey_input_repository.load_journey_selection",
        loader,
    ):
        result = await load_journey_judge_input(connection, input_set=context.input_set)
    assert result == contract
    assert loader.await_args.kwargs["legacy_full_plan"] is True


async def test_partial_rule_report_keeps_global_journey_completion_gap():
    from services.api.app.infrastructure.db.journey_report_binding import (
        load_journey_report_binding,
    )

    context, plan, _ = selection_context()
    verdict = evaluate_aggregate_context(context)
    contract = context.journey_input["contract"]

    async def fetch(sql, *args):
        if "plan.plan_payload" in sql:
            return [{"plan_payload": plan.to_dict(), "finalization_epoch": 2}]
        return [{
            "selection_hash": stable_hash(contract), "rule_contract_hash": "contract-hash",
            "input_set_payload": context.input_set.to_dict(), "execution_id": "execution",
            "execution_status": verdict.execution_status, "review_status": None,
            "judge_output": {"wrapper_payload": verdict.wrapper_payload},
        }]

    connection = SimpleNamespace(fetch=fetch)
    with (
        patch("services.api.app.infrastructure.db.journey_report_binding.load_journey_judge_input",
              AsyncMock(return_value=contract)),
        patch("services.api.app.infrastructure.db.journey_report_binding.load_journey_selection",
              AsyncMock(return_value=None)) as full_selection,
    ):
        report = await load_journey_report_binding(connection, task_id="task-1")
    assert len(report["results"]) == 1
    assert report["results"][0]["execution_status"] == "pass"
    assert report["gap_count"] == 1
    assert full_selection.await_args.kwargs["legacy_full_plan"] is True


async def test_full_legacy_report_keeps_original_zero_gap_when_material_is_complete():
    from services.api.app.infrastructure.db.journey_report_binding import (
        load_journey_report_binding,
    )

    context = journey_context()
    contract = {**context.journey_input["contract"], "schema_version": "journey-input-contract.v1"}
    plan = parse_journey_execution_plan(contract["plan"])
    verdict = evaluate_aggregate_context(context)

    async def fetch(sql, *args):
        if "plan.plan_payload" in sql:
            return [{"plan_payload": plan.to_dict(), "finalization_epoch": 2}]
        return [{
            "selection_hash": stable_hash(contract), "rule_contract_hash": "contract-hash",
            "input_set_payload": context.input_set.to_dict(), "execution_id": "execution",
            "execution_status": verdict.execution_status, "review_status": None,
            "judge_output": {"wrapper_payload": verdict.wrapper_payload},
        }]

    connection = SimpleNamespace(fetch=fetch)
    with (
        patch("services.api.app.infrastructure.db.journey_report_binding.load_journey_judge_input",
              AsyncMock(return_value=contract)),
        patch("services.api.app.infrastructure.db.journey_report_binding.load_journey_selection",
              AsyncMock(side_effect=AssertionError("旧合同不新增整流程复核"))),
    ):
        report = await load_journey_report_binding(connection, task_id="task-1")
    assert len(report["results"]) == 1
    assert report["gap_count"] == 0


@pytest.mark.parametrize("fault", [None, "missing", "manifest", "checkpoint", "observation_id"])
async def test_independent_full_obligation_facts_allow_unselected_complete_checkpoint(fault):
    from services.api.app.infrastructure.db.journey_report_binding import (
        load_journey_report_binding,
    )

    context, plan, _ = selection_context()
    verdict = evaluate_aggregate_context(context)
    contract = context.journey_input["contract"]
    full_contract = {
        **deepcopy(contract), "schema_version": "journey-input-contract.v1",
        "task_id": "task-1", "agent_run_id": "run-1",
    }
    full_contract["checkpoints"][0]["attempt_id"] = "selected-attempt"
    other = deepcopy(full_contract["checkpoints"][0])
    other.update(checkpoint_id="unrelated", attempt_id="other-attempt")
    full_contract["checkpoints"].append(other)
    checkpoint = plan.checkpoints[1]
    bindings = [{
        **binding, "execution_plan_id": plan.execution_plan_id,
        "checkpoint_id": "unrelated", "checkpoint_hash": stable_hash(checkpoint.to_dict()),
        "manifest_hash": manifest["manifest_hash"],
    } for binding, manifest in zip(other["bindings"], other["manifests"], strict=True)]
    if fault == "manifest":
        bindings[0]["manifest_version"] += 1
    elif fault == "checkpoint":
        bindings[0]["checkpoint_hash"] = "0" * 64
    observation = {
        "observation_id": "checkpoint-observation:" + stable_hash({
            "attempt_id": "other-attempt", "bindings": bindings, "observed": True,
        }), "attempt_id": "other-attempt", "bindings": bindings,
    }
    if fault == "observation_id":
        observation["observation_id"] = "forged"

    async def fetch(sql, *args):
        if "plan.plan_payload" in sql:
            return [{"plan_payload": plan.to_dict(), "finalization_epoch": 2}]
        if "qc_journey_checkpoint_observation" in sql:
            return [] if fault == "missing" else [observation]
        return [{
            "selection_hash": stable_hash(contract), "rule_contract_hash": "contract-hash",
            "input_set_payload": context.input_set.to_dict(), "execution_id": "execution",
            "execution_status": verdict.execution_status, "review_status": None,
            "judge_output": {"wrapper_payload": verdict.wrapper_payload},
        }]

    connection = SimpleNamespace(fetch=fetch)
    with (
        patch("services.api.app.infrastructure.db.journey_report_binding.load_journey_judge_input",
              AsyncMock(return_value=contract)),
        patch("services.api.app.infrastructure.db.journey_report_binding.load_journey_selection",
              AsyncMock(return_value={"contract": full_contract})),
    ):
        report = await load_journey_report_binding(connection, task_id="task-1")
    assert len(report["results"]) == 1
    assert report["gap_count"] == (0 if fault is None else 1)


@pytest.mark.parametrize("legacy", [False, True])
async def test_loader_selects_only_required_attempts_before_material_binding(legacy):
    from services.api.app.infrastructure.db.journey_input_repository import load_journey_selection

    context, plan, _ = selection_context()
    rule_contract = context.journey_input["contract"]["journey_rule_contract"]
    rules = [{"contract_hash": "contract-hash", "applicability": "applicable", "judge_config": {
        "capability_config": {"journey_rule_contract": rule_contract},
    }}]

    async def fetch(sql, *args):
        if "plan_payload" in sql:
            return [{"plan_payload": plan.to_dict()}]
        if "qc_journey_input_contract" in sql:
            return []
        return [{"checkpoint_id": "unrelated", "status": "gap"}, {
            "checkpoint_id": "paused", "status": "bound", "turn_payload": {
                "coordination": {"interaction": {"action_execution_id": "selected-action"}},
            },
        }]

    connection = SimpleNamespace(fetch=fetch, fetchrow=AsyncMock(return_value=None))
    with patch(
        "services.api.app.infrastructure.db.journey_input_repository."
        "read_frozen_task_rule_inventory_on_connection", AsyncMock(return_value={"rules": rules}),
    ):
        assert await load_journey_selection(
            connection, task_id="task-1", agent_run_id="run-1", epoch=2,
            rule_hash="contract-hash", legacy_full_plan=legacy,
        ) is None
    if legacy:
        connection.fetchrow.assert_not_awaited()
    else:
        assert connection.fetchrow.await_args.args[1] == "selected-action"


@pytest.mark.parametrize("fault", ["malformed", "unknown_checkpoint", "unsupported_material"])
async def test_invalid_single_rule_selection_becomes_own_gap_without_raising(fault):
    from services.api.app.infrastructure.db.journey_input_repository import load_journey_selection

    context, plan, _ = selection_context()
    value = deepcopy(context.journey_input["contract"]["journey_rule_contract"])
    if fault == "malformed":
        value = "invalid"
    elif fault == "unknown_checkpoint":
        value["evidence_conditions"][0]["checkpoint_id"] = "foreign"
    else:
        value["evidence_conditions"][0]["requirements"][0]["artifact_types"] = ["dom"]

    async def fetch(sql, *args):
        if "plan_payload" in sql:
            return [{"plan_payload": plan.to_dict()}]
        if "qc_journey_input_contract" in sql:
            return []
        raise AssertionError("非法规则不能进入制品读取")

    connection = SimpleNamespace(fetch=fetch)
    inventory = {"rules": [{"contract_hash": "contract-hash", "applicability": "applicable",
                           "judge_config": {"capability_config": {
                               "journey_rule_contract": value,
                           }}}]}
    with patch(
        "services.api.app.infrastructure.db.journey_input_repository."
        "read_frozen_task_rule_inventory_on_connection", AsyncMock(return_value=inventory),
    ):
        assert await load_journey_selection(
            connection, task_id="task-1", agent_run_id="run-1", epoch=2, rule_hash="contract-hash",
        ) is None
