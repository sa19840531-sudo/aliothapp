"""正式流程报告只消费同 Run/Epoch 的精确输入和已完成规则结果。"""

import json

from services.api.app.domain.evidence import EvidenceInputSet
from services.api.app.domain.journey.dsl import parse_journey_execution_plan
from services.api.app.domain.journey.rule_selection import contract_checkpoints
from services.api.app.domain.journey.validation import stable_hash
from services.api.app.infrastructure.db.journey_input_repository import (
    load_journey_judge_input,
    load_journey_selection,
)


def _object(value):
    return json.loads(value) if isinstance(value, str) else dict(value)


async def load_journey_report_binding(
    connection,
    *,
    task_id,
    agent_run_id=None,
    epoch=None,
    source_epoch=None,
):
    plans = await connection.fetch(
        "SELECT plan.plan_payload,task.finalization_epoch "
        "FROM qc_journey_execution_plan AS plan JOIN qc_task AS task "
        "ON task.id=plan.task_id WHERE task.id=$1::uuid "
        "AND plan.schema_version='journey-execution-plan.v2' "
        "AND (task.finalization_agent_run_id IS NULL "
        "OR plan.agent_run_id=task.finalization_agent_run_id)",
        task_id,
    )
    gaps, results = 0, []
    for row in plans:
        plan = parse_journey_execution_plan(_object(row["plan_payload"]))
        evidence_epoch = source_epoch if source_epoch is not None else epoch
        closed_epoch = evidence_epoch is not None and row["finalization_epoch"] != evidence_epoch
        from services.api.app.infrastructure.db.journey_epoch import closed_journey_epoch_is_bound

        closed_allowed = closed_epoch and await closed_journey_epoch_is_bound(
            connection,
            task_id=task_id,
            agent_run_id=plan.agent_run_id,
            epoch=evidence_epoch,
        )
        if (
            (agent_run_id is not None and plan.agent_run_id != agent_run_id)
            or (closed_epoch and not closed_allowed)
            or (
                epoch is not None
                and evidence_epoch is not None
                and epoch not in (evidence_epoch, evidence_epoch + 1)
            )
        ):
            raise ValueError("正式流程报告 Run 或 Epoch 不一致")
        rows = await connection.fetch(
            "SELECT contract.selection_hash,contract.rule_contract_hash,inputs.input_set_payload,"
            "execution.id::text AS execution_id,execution.execution_status,execution.review_status,"
            "execution.judge_output FROM qc_journey_input_contract AS contract "
            "JOIN qc_task AS task ON task.id=contract.task_id "
            "AND (task.finalization_epoch=contract.finalization_epoch OR $4::boolean) "
            "JOIN qc_evidence_input_set AS inputs ON inputs.input_set_id=contract.input_set_id "
            "JOIN qc_aggregate_inspection_work_item AS work "
            "ON work.evidence_input_set_id=inputs.input_set_id "
            "AND work.finalization_epoch=contract.finalization_epoch AND work.status='succeeded' "
            "JOIN qc_rule_execution AS execution ON execution.aggregate_work_item_id=work.id "
            "AND execution.evidence_input_set_id=inputs.input_set_id "
            "WHERE contract.execution_plan_id=$1 AND contract.task_id=$2::uuid "
            "AND contract.agent_run_id=$3::uuid "
            "AND contract.finalization_epoch=coalesce($5::integer,task.finalization_epoch)",
            plan.execution_plan_id,
            task_id,
            plan.agent_run_id,
            bool(closed_allowed),
            evidence_epoch,
        )
        consumed = set()
        gate_checkpoints = set()
        has_rule_selection = False
        for item in rows:
            if (
                item["execution_status"] not in ("pass", "fail")
                or item["review_status"] not in (None, "")
                or item["rule_contract_hash"] not in plan.rule_contract_hashes
            ):
                continue
            input_set = EvidenceInputSet.from_dict(_object(item["input_set_payload"]))
            contract = await load_journey_judge_input(
                connection,
                input_set=input_set,
                allow_closed_epoch=bool(closed_allowed),
            )
            output = _object(item["judge_output"])
            journey = output.get("wrapper_payload", {}).get("journey", {})
            checks = journey.get("checkpoints", ())
            selected = contract_checkpoints(contract, plan) if contract is not None else ()
            if (
                contract is None
                or journey.get("execution_plan_id") != plan.execution_plan_id
                or [entry.get("checkpoint_id") for entry in checks]
                != [checkpoint.checkpoint_id for checkpoint in selected]
                or any(entry.get("status") not in ("pass", "fail") for entry in checks)
                or any(
                    entry.get("bindings") != frozen["bindings"]
                    or entry.get("recovery") != frozen.get("recovery")
                    for entry, frozen in zip(checks, contract["checkpoints"], strict=True)
                )
            ):
                continue
            consumed.add(item["rule_contract_hash"])
            has_rule_selection |= contract.get("schema_version") == "journey-input-contract.v2"
            gate_checkpoints.update(
                entry["checkpoint_id"] for entry in checks
                if contract.get("schema_version") == "journey-input-contract.v1"
                or entry.get("quality_gate_status") in ("pass", "fail")
            )
            results.append(
                {
                    "execution_plan_id": plan.execution_plan_id,
                    "scenario_key": plan.scenario_key,
                    "rule_contract_hash": item["rule_contract_hash"],
                    "input_set_id": input_set.input_set_id,
                    "input_set_hash": input_set.input_set_hash,
                    "selection_hash": item["selection_hash"],
                    "execution_id": item["execution_id"],
                    "execution_status": item["execution_status"],
                    "checkpoints": list(checks),
                }
            )
        # 历史合同沿用原路径；局部规则之外的完整义务由既有正式观测事实补齐。
        complete_material = True
        if has_rule_selection:
            complete_material = await load_journey_selection(
                connection, task_id=task_id, agent_run_id=plan.agent_run_id,
                epoch=evidence_epoch if evidence_epoch is not None else row["finalization_epoch"],
                rule_hash=plan.rule_contract_hashes[0], allow_closed_epoch=bool(closed_allowed),
                legacy_full_plan=True,
            )
            if complete_material is not None:
                gate_checkpoints.update(await _observed_gate_checkpoints(
                    connection, complete_material["contract"],
                ))
        if (
            not plan.rule_contract_hashes or consumed != set(plan.rule_contract_hashes)
            or (has_rule_selection and (
                gate_checkpoints != {item.checkpoint_id for item in plan.checkpoints}
                or complete_material is None
            ))
        ):
            gaps += 1
    payload = {
        "schema_version": "h5-journey-report.v1",
        "task_id": task_id,
        "agent_run_id": agent_run_id,
        "finalization_epoch": epoch,
        "source_finalization_epoch": source_epoch if source_epoch is not None else epoch,
        "gap_count": gaps,
        "results": results,
    }
    return {**payload, "snapshot_hash": stable_hash(payload)}


async def _observed_gate_checkpoints(connection, contract):
    """复用完整义务已判通过的原观测，观察标签、旧版本和串属材料均不能补门禁。"""
    rows = await connection.fetch(
        "SELECT observation.observation_id,observation.attempt_id,observation.bindings "
        "FROM qc_journey_checkpoint_observation AS observation "
        "JOIN qc_journey_checkpoint_attempt AS attempt USING(attempt_id) "
        "WHERE attempt.execution_plan_id=$1 AND attempt.task_id=$2::uuid "
        "AND attempt.agent_run_id=$3::uuid AND attempt.finalization_epoch=$4 "
        "AND observation.task_id=attempt.task_id "
        "AND observation.finalization_epoch=attempt.finalization_epoch "
        "AND observation.observed=true AND attempt.status='bound' "
        "AND attempt.observation_status='observed'",
        contract["plan"]["execution_plan_id"], contract["task_id"],
        contract["agent_run_id"], contract["epoch"],
    )
    checkpoints = {item["id"]: item for item in contract["plan"]["checkpoints"]}
    completed = set()
    for frozen in contract["checkpoints"]:
        candidates = [row for row in rows if row["attempt_id"] == frozen["attempt_id"]]
        if len(candidates) != 1:
            continue
        row = candidates[0]
        bindings = (
            json.loads(row["bindings"]) if isinstance(row["bindings"], str) else row["bindings"]
        )
        if row["observation_id"] != "checkpoint-observation:" + stable_hash({
            "attempt_id": row["attempt_id"], "bindings": bindings, "observed": True,
        }):
            continue
        checkpoint = checkpoints[frozen["checkpoint_id"]]
        if not isinstance(bindings, list) or len(bindings) != len(frozen["bindings"]):
            continue
        if all(
            isinstance(binding, dict)
            and all(binding.get(key) == value for key, value in original.items())
            and binding.get("execution_plan_id") == contract["plan"]["execution_plan_id"]
            and binding.get("checkpoint_id") == frozen["checkpoint_id"]
            and binding.get("checkpoint_hash") == stable_hash(checkpoint)
            and binding.get("manifest_hash") == manifest["manifest_hash"]
            for binding, original, manifest in zip(
                bindings, frozen["bindings"], frozen["manifests"], strict=True,
            )
        ):
            completed.add(frozen["checkpoint_id"])
    return completed


async def bind_journey_report(
    connection,
    report,
    *,
    task_id,
    agent_run_id,
    epoch,
    source_epoch=None,
):
    binding = await load_journey_report_binding(
        connection,
        task_id=task_id,
        agent_run_id=agent_run_id,
        epoch=epoch,
        source_epoch=source_epoch,
    )
    if binding["gap_count"]:
        raise ValueError("流程规则结果、输入或最新证据未闭合，禁止正式报告")
    if binding["results"]:
        report["h5_journeys"] = binding
