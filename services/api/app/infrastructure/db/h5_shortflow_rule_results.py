"""在既有短流程只读快照内核验正式规则结果和原始材料的精确关联。"""

from services.api.app.application.h5_shortflow_rule_results import (
    object_value,
    project_rule_execution,
    result_identity,
    validate_result_input,
)
from services.api.app.domain.evidence import EvidenceInputSet
from services.api.app.infrastructure.db.journey_epoch import closed_journey_epoch_is_bound
from services.api.app.infrastructure.db.journey_input_repository import load_journey_judge_input


async def read_shortflow_rule_results(connection, *, plan, task, checkpoint_views):
    rows = await connection.fetch(
        """SELECT contract.input_set_id,contract.rule_contract_hash,contract.selection_hash,
                  contract.finalization_epoch,contract.contract_payload,
                  inputs.input_set_payload,inputs.input_set_hash AS stored_input_hash,
                  inputs.rule_id::text,inputs.rule_code,rule.name AS rule_name,
                  to_jsonb(work) AS work_payload,to_jsonb(execution) AS execution_payload,
                  to_jsonb(packet) AS packet_payload,to_jsonb(attempt) AS attempt_payload
           FROM qc_journey_input_contract contract
           JOIN qc_evidence_input_set inputs ON inputs.input_set_id=contract.input_set_id
             AND inputs.task_id=contract.task_id
             AND inputs.source_agent_run_id=contract.agent_run_id
             AND inputs.finalization_epoch=contract.finalization_epoch
             AND inputs.rule_contract_hash=contract.rule_contract_hash
             AND inputs.scope='journey' AND inputs.scope_key='journey_observation'
           JOIN qc_rule rule ON rule.id=inputs.rule_id
           LEFT JOIN qc_aggregate_inspection_work_item work
             ON work.evidence_input_set_id=inputs.input_set_id
           LEFT JOIN qc_rule_execution execution ON execution.aggregate_work_item_id=work.id
           LEFT JOIN qc_observation_packet packet ON packet.id=execution.observation_packet_id
           LEFT JOIN qc_aggregate_inspection_attempt attempt ON attempt.work_item_id=work.id
             AND attempt.attempt_no=work.attempt_count
           WHERE contract.execution_plan_id=$1 AND contract.task_id=$2::uuid
             AND contract.agent_run_id=$3::uuid
           ORDER BY contract.rule_contract_hash,contract.created_at,contract.input_set_id""",
        plan.execution_plan_id,
        plan.task_id,
        plan.agent_run_id,
    )
    results = []
    for row in rows:
        if row["rule_contract_hash"] not in plan.rule_contract_hashes:
            continue
        result = result_identity(row)
        results.append(result)
        try:
            input_set = EvidenceInputSet.from_dict(object_value(row["input_set_payload"]))
            frozen = object_value(row["contract_payload"])
            validate_result_input(row=row, plan=plan, input_set=input_set, contract=frozen)
            closed = input_set.finalization_epoch != task["finalization_epoch"]
            if task["finalization_agent_run_id"] not in (None, plan.agent_run_id) or (
                closed
                and not await closed_journey_epoch_is_bound(
                    connection,
                    task_id=plan.task_id,
                    agent_run_id=plan.agent_run_id,
                    epoch=input_set.finalization_epoch,
                )
            ):
                raise ValueError("流程规则不是当前或完成门禁明确绑定的证据周期")
            current = await load_journey_judge_input(
                connection,
                input_set=input_set,
                allow_closed_epoch=closed,
            )
            if current is None or current != frozen:
                result.update(
                    linkage_status="evidence_changed",
                    linkage_notice="原判定的输入或原件已变化，不能沿用旧结论",
                )
                continue
            if row["work_payload"] is None or row["execution_payload"] is None:
                result.update(
                    linkage_status="missing_result",
                    input_hash=input_set.input_set_hash,
                    linkage_notice="证据输入已封存，尚无已关联的正式规则判定",
                )
                continue
            result.update(
                project_rule_execution(
                    row=row,
                    plan=plan,
                    input_set=input_set,
                    contract=current,
                    checkpoint_views=checkpoint_views,
                )
            )
        except (ValueError, TypeError, KeyError, AttributeError):
            # 读侧只报告关联缺口，不修补输入、不采用最近一条历史成功结果。
            continue
    linked = {
        result["rule_contract_hash"] for result in results if result["linkage_status"] == "linked"
    }
    complete = bool(plan.rule_contract_hashes) and linked == set(plan.rule_contract_hashes)
    return {
        "rule_results_status": "available" if complete else "partial" if linked else "unavailable",
        "rule_results_notice": None
        if complete
        else "尚无完整关联的规则判定；材料合格不等于规则通过",
        "rule_results": results,
    }
