"""流程规则复用聚合 Judge 的结果协议，精确消费冻结检查点输入。"""

from services.api.app.application.aggregate_inspection_execution import (
    AggregateJudgement,
    _artifact_ids,
    _insufficient,
)
from services.api.app.application.checkpoint_judgement import judge_checkpoint
from services.api.app.application.h5_checkpoint_observation import checkpoint_facts
from services.api.app.domain.journey.dsl import parse_journey_execution_plan
from services.api.app.domain.journey.rule_selection import contract_checkpoints


def evaluate_journey_context(context):
    material = context.journey_input
    if material is None:
        return _insufficient(
            context,
            code="JOURNEY_CHECKPOINT_INPUT_MISSING",
            reason="缺少已冻结并经实际制品复核的流程检查点输入",
        )
    contract = material["contract"]
    plan = parse_journey_execution_plan(contract["plan"])
    try:
        checkpoints = contract_checkpoints(contract, plan)
    except (ValueError, KeyError, TypeError):
        return _insufficient(
            context, code="JOURNEY_RULE_SELECTION_MISMATCH", reason="冻结规则证据选择被改写"
        )
    if (
        plan.task_id != context.input_set.task_id
        or plan.agent_run_id != context.input_set.source_agent_run_id
        or context.input_set.rule_contract_hash not in plan.rule_contract_hashes
        or contract["epoch"] != context.input_set.finalization_epoch
        or contract["rule_contract_hash"] != context.input_set.rule_contract_hash
    ):
        return _insufficient(
            context, code="JOURNEY_RULE_CONTRACT_MISMATCH", reason="流程与规则输入合同串属"
        )
    member_refs = {
        (item.capture_id, item.manifest_id, item.manifest_version, item.manifest_hash)
        for item in context.input_set.members
    }
    frozen_refs = {
        (
            item["capture_id"],
            item["manifest_id"],
            item["manifest_version"],
            manifest["manifest_hash"],
        )
        for checkpoint in contract["checkpoints"]
        for item, manifest in zip(checkpoint["bindings"], checkpoint["manifests"], strict=True)
    }
    frozen_checkpoint_ids = [item["checkpoint_id"] for item in contract["checkpoints"]]
    if member_refs != frozen_refs or frozen_checkpoint_ids != [
        item.checkpoint_id for item in checkpoints
    ]:
        return _insufficient(
            context, code="JOURNEY_INPUT_CLOSURE_MISMATCH", reason="流程证据闭包不完整"
        )
    results = []
    by_id = {item["checkpoint_id"]: item for item in contract["checkpoints"]}
    original_by_id = {item.checkpoint_id: item for item in plan.checkpoints}
    completed = set()
    gate_completed = set()
    for checkpoint in checkpoints:
        frozen = by_id.get(checkpoint.checkpoint_id)
        observed = material["observations"].get(checkpoint.checkpoint_id)
        if frozen is None or observed is None or not set(checkpoint.depends_on) <= completed:
            results.append(
                {
                    "checkpoint_id": checkpoint.checkpoint_id,
                    "status": "evidence_insufficient",
                    "missing": ["checkpoint_or_dependency_missing"],
                }
            )
            continue
        result = judge_checkpoint(
            checkpoint,
            before=observed["before"],
            after=observed["after"],
            manifests=frozen["manifests"],
            series=observed.get("series", ()),
            action_receipt=frozen.get("action_receipt"),
        )
        if contract.get("schema_version") == "journey-input-contract.v2":
            gate_result = judge_checkpoint(
                original_by_id[checkpoint.checkpoint_id],
                before=observed["before"], after=observed["after"],
                manifests=frozen["manifests"], series=observed.get("series", ()),
                action_receipt=frozen.get("action_receipt"),
            )
            if not set(checkpoint.depends_on) <= gate_completed:
                gate_result["status"] = "evidence_insufficient"
                gate_result["missing"].append("dependency_quality_gate_incomplete")
            result["quality_gate_status"] = gate_result["status"]
            result["quality_gate_missing"] = gate_result["missing"]
        recovery = frozen.get("recovery", {})
        for dependency in checkpoint.depends_on:
            prior = material["observations"].get(dependency, {}).get("after", {})
            previous, current = checkpoint_facts(prior), checkpoint_facts(observed["before"])
            changed = prior.get("url") != observed["before"].get("url") or any(
                previous.get(key) != current.get(key)
                for key in ("document_time_origin_ms", "media_instance_id")
            )
            if changed and not recovery.get(dependency, {}).get("restorations"):
                result["status"] = "evidence_insufficient"
                result["missing"].append("dependency_instance_changed_without_restoration")
                if "quality_gate_status" in result:
                    result["quality_gate_status"] = "evidence_insufficient"
                    result["quality_gate_missing"].append(
                        "dependency_instance_changed_without_restoration"
                    )
        if recovery:
            result["recovery"] = recovery
        result["bindings"] = frozen["bindings"]
        results.append(result)
        if result["status"] == "pass":
            completed.add(checkpoint.checkpoint_id)
        if result.get("quality_gate_status") == "pass":
            gate_completed.add(checkpoint.checkpoint_id)
    if any(item["status"] == "evidence_insufficient" for item in results):
        verdict = _insufficient(
            context,
            code="JOURNEY_CHECKPOINT_EVIDENCE_INSUFFICIENT",
            reason="流程存在未完成的检查点观测或证据义务",
        )
        from dataclasses import replace

        return replace(
            verdict,
            wrapper_payload={
                **verdict.wrapper_payload,
                "journey": {
                    "execution_plan_id": plan.execution_plan_id,
                    "checkpoints": results,
                },
            },
        )
    status = "pass" if all(item["status"] == "pass" for item in results) else "fail"
    return AggregateJudgement(
        execution_status=status,
        result_summary="冻结流程检查点全部满足" if status == "pass" else "流程检查点实测不满足预期",
        problem_desc=None if status == "pass" else "实际测量与冻结检查点预期矛盾",
        confidence=1.0,
        review_status=None,
        review_reason=None,
        error_code=None,
        wrapper_payload={
            "protocol_version": "aggregate-observation-packet.v1",
            "scope": "journey",
            "scope_key": context.lease.scope_key,
            "input_set_id": context.input_set.input_set_id,
            "journey": {"execution_plan_id": plan.execution_plan_id, "checkpoints": results},
        },
        evidence_artifact_ids=_artifact_ids(context.input_set),
    )
