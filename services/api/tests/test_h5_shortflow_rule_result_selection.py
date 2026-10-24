"""只读结果输入遵守同一规则选择闭包；伪造新摘要不能扩大或缩小范围。"""

from copy import deepcopy

import pytest

from services.api.app.application.h5_shortflow_rule_results import validate_result_input
from services.api.app.domain.journey import parse_journey_execution_plan
from services.api.app.domain.journey.validation import stable_hash
from services.api.tests.test_h5_checkpoint_consumption import journey_context
from services.api.tests.test_journey_rule_selection import selection_context


def input_fixture(*, legacy=False, dependency=False):
    if legacy:
        context = journey_context()
        plan = parse_journey_execution_plan(context.journey_input["contract"]["plan"])
    else:
        context, plan, _ = selection_context(dependency=dependency)
    contract = deepcopy(context.journey_input["contract"])
    contract.update(task_id=plan.task_id, agent_run_id=plan.agent_run_id)
    if legacy:
        contract["schema_version"] = "journey-input-contract.v1"
    for checkpoint in contract["checkpoints"]:
        for binding, manifest in zip(checkpoint["bindings"], checkpoint["manifests"], strict=True):
            binding["artifact_ids"] = [item["artifact_id"] for item in manifest["artifacts"]]
    input_set = context.input_set
    row = {
        "rule_id": input_set.rule_id, "rule_code": input_set.rule_code,
        "rule_contract_hash": input_set.rule_contract_hash,
        "input_set_id": input_set.input_set_id, "stored_input_hash": input_set.input_set_hash,
        "finalization_epoch": input_set.finalization_epoch, "selection_hash": stable_hash(contract),
    }
    return row, plan, input_set, contract


@pytest.mark.parametrize("legacy", [False, True])
def test_current_subset_and_legacy_full_plan_inputs_keep_their_original_scope(legacy):
    row, plan, input_set, contract = input_fixture(legacy=legacy)
    validate_result_input(row=row, plan=plan, input_set=input_set, contract=contract)


@pytest.mark.parametrize("fault", ["selection", "extra", "omitted_dependency", "version", "rule"])
def test_damaged_subset_or_cross_input_cannot_pass_with_a_recomputed_selection_hash(fault):
    # 依赖反例的夹具声明 unrelated → paused，但冻结材料仅有 paused，本身已漏传递依赖。
    row, plan, input_set, contract = input_fixture(dependency=fault == "omitted_dependency")
    if fault == "selection":
        contract["rule_selection"]["checkpoint_ids"].append("unrelated")
    elif fault == "extra":
        extra = deepcopy(contract["checkpoints"][0])
        extra["checkpoint_id"] = "unrelated"
        contract["checkpoints"].append(extra)
    elif fault == "version":
        contract["schema_version"] = "journey-input-contract.unknown"
    elif fault == "rule":
        contract["rule_contract_hash"] = "another-rule-contract"
    row["selection_hash"] = stable_hash(contract)
    with pytest.raises(ValueError):
        validate_result_input(row=row, plan=plan, input_set=input_set, contract=contract)
