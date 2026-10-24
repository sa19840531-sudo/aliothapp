"""规则证据选择只缩小判定输入，完整执行计划与安全门禁独立保留。"""

from dataclasses import replace

from services.api.app.domain.journey.checkpoints import JourneyEvidenceRequirement
from services.api.app.domain.journey.validation import JourneyValidationError


def select_rule_checkpoints(plan, evidence_conditions):
    """显式检查点使用规则义务，传递依赖保留发布合同的完整义务。"""
    by_id = {item.checkpoint_id: item for item in plan.checkpoints}
    requested = {}
    for condition in evidence_conditions:
        checkpoint_id = condition["checkpoint_id"]
        if checkpoint_id in requested or checkpoint_id not in by_id:
            raise JourneyValidationError("规则检查点重复或未包含在冻结执行计划中")
        checkpoint = by_id[checkpoint_id]
        supported = {item.role: item for item in checkpoint.evidence_requirements}
        requirements = tuple(
            JourneyEvidenceRequirement(
                role=item["role"], artifact_types=tuple(item["artifact_types"]),
                minimum_count=item.get("minimum_count", 1),
                capture_phase=item.get("capture_phase", "after_action"),
            )
            for item in condition["requirements"]
        )
        if not requirements or len({item.role for item in requirements}) != len(requirements):
            raise JourneyValidationError("规则检查点的证据义务不能为空或重复")
        for item in requirements:
            baseline = supported.get(item.role)
            if baseline is None or (
                baseline.capture_phase != item.capture_phase
                or item.minimum_count > baseline.minimum_count
                or not set(item.artifact_types) <= set(baseline.artifact_types)
            ):
                raise JourneyValidationError("冻结执行计划不能提供该规则证据义务")
        requested[checkpoint_id] = requirements
    if not requested:
        raise JourneyValidationError("规则至少需要选择一个检查点")
    closure = set(requested)
    for checkpoint in reversed(plan.checkpoints):
        if checkpoint.checkpoint_id in closure:
            closure.update(checkpoint.depends_on)
    # 一个检查点同时作为显式目标与前置依赖时，前置依赖义务不能被材料勾选削弱。
    dependencies = {
        dependency
        for checkpoint in plan.checkpoints
        if checkpoint.checkpoint_id in closure
        for dependency in checkpoint.depends_on
    }
    return tuple(
        replace(checkpoint, evidence_requirements=requested[checkpoint.checkpoint_id])
        if checkpoint.checkpoint_id in requested and checkpoint.checkpoint_id not in dependencies
        else checkpoint
        for checkpoint in plan.checkpoints if checkpoint.checkpoint_id in closure
    )


def rule_selection_payload(plan, evidence_conditions):
    """选择结果按原计划顺序规范化，完整依赖和材料义务共同进入封存摘要。"""
    checkpoints = select_rule_checkpoints(plan, evidence_conditions)
    requested = {item["checkpoint_id"] for item in evidence_conditions}
    return {
        "schema_version": "journey-rule-selection.v1",
        "requested_checkpoint_ids": [
            item.checkpoint_id for item in plan.checkpoints if item.checkpoint_id in requested
        ],
        "dependency_checkpoint_ids": [
            item.checkpoint_id for item in checkpoints if item.checkpoint_id not in requested
        ],
        "checkpoint_ids": [item.checkpoint_id for item in checkpoints],
        "evidence_conditions": [
            {"checkpoint_id": item.checkpoint_id,
             "requirements": [requirement.to_dict() for requirement in item.evidence_requirements]}
            for item in checkpoints
        ],
    }


def contract_checkpoints(contract, plan):
    """历史整流程合同保持原语义，新合同必须能从冻结规则重建同一选择。"""
    if contract.get("schema_version", "journey-input-contract.v1") == "journey-input-contract.v1":
        return plan.checkpoints
    if contract.get("schema_version") != "journey-input-contract.v2":
        raise JourneyValidationError("流程输入合同版本不受支持")
    conditions = contract["journey_rule_contract"]["evidence_conditions"]
    expected = rule_selection_payload(plan, conditions)
    if contract.get("rule_selection") != expected:
        raise JourneyValidationError("规则输入选择与冻结规则的依赖闭包不一致")
    return select_rule_checkpoints(plan, conditions)
