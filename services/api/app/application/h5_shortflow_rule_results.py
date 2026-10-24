"""短流程正式判定的白名单读投影，不重判、不补采、不改变完成门禁。"""

import json

from services.api.app.application.task_progress import humanize_qc_text
from services.api.app.domain.journey.rule_selection import contract_checkpoints
from services.api.app.domain.journey.validation import stable_hash
from services.api.app.domain.protocol import stable_protocol_hash


def object_value(value):
    """只接受对象，避免异常判定载荷通过默认转换混入公开结果。"""
    parsed = json.loads(value) if isinstance(value, str) else value
    if not isinstance(parsed, dict):
        raise ValueError("流程判定载荷必须为对象")
    return parsed


def result_identity(row):
    return {
        "rule_id": row["rule_id"],
        "rule_code": row["rule_code"],
        "rule_name": row["rule_name"] or row["rule_code"],
        "rule_contract_hash": row["rule_contract_hash"],
        "execution_id": None,
        "input_set_id": row["input_set_id"],
        "input_hash": None,
        "execution_status": None,
        "result_summary": None,
        "problem_desc": None,
        "review_status": None,
        "review_reason": None,
        "finalization_epoch": row["finalization_epoch"],
        "linkage_status": "input_mismatch",
        "linkage_notice": "规则输入关联不完整，不能作为当前流程结论",
        "checkpoints": [],
    }


def validate_result_input(*, row, plan, input_set, contract):
    """同时验证存储列、冻结载荷和成员，不能只凭同一截图或规则编号关联。"""
    if (
        input_set.task_id != plan.task_id
        or input_set.source_agent_run_id != plan.agent_run_id
        or input_set.rule_id != row["rule_id"]
        or input_set.rule_code != row["rule_code"]
        or input_set.rule_contract_hash != row["rule_contract_hash"]
        or input_set.rule_contract_hash not in plan.rule_contract_hashes
        or input_set.input_set_id != row["input_set_id"]
        or input_set.input_set_hash != row["stored_input_hash"]
        or input_set.finalization_epoch != row["finalization_epoch"]
        or input_set.scope.value != "journey"
        or input_set.scope_key != "journey_observation"
        or contract.get("task_id") != plan.task_id
        or contract.get("agent_run_id") != plan.agent_run_id
        or contract.get("epoch") != input_set.finalization_epoch
        or contract.get("rule_contract_hash") != input_set.rule_contract_hash
        or contract.get("plan") != plan.to_dict()
        or stable_hash(contract) != row["selection_hash"]
    ):
        raise ValueError("流程规则输入跨任务、运行、版本或输入摘要")
    frozen = contract.get("checkpoints")
    selected = contract_checkpoints(contract, plan)
    if not isinstance(frozen, list) or [item.get("checkpoint_id") for item in frozen] != [
        checkpoint.checkpoint_id for checkpoint in selected
    ]:
        raise ValueError("流程规则检查点闭包不一致")
    expected = []
    for checkpoint in frozen:
        if len(checkpoint["bindings"]) != 2 or len(checkpoint["manifests"]) != 2:
            raise ValueError("流程规则前后材料不完整")
        for binding, manifest in zip(checkpoint["bindings"], checkpoint["manifests"], strict=True):
            expected.append(
                (
                    binding["capture_id"],
                    binding["role"],
                    manifest["manifest_id"],
                    manifest["manifest_version"],
                    manifest["manifest_hash"],
                    tuple(binding["artifact_ids"]),
                )
            )
    actual = [
        (
            member.capture_id,
            member.capture_role,
            member.manifest_id,
            member.manifest_version,
            member.manifest_hash,
            member.artifact_ids,
        )
        for member in input_set.members
    ]
    if actual != expected:
        raise ValueError("流程规则输入成员与冻结材料不一致")


def project_rule_execution(*, row, plan, input_set, contract, checkpoint_views):
    """正式执行、Work、观察包和冻结合同全部一致后才发布分项结论。"""
    work = object_value(row["work_payload"])
    execution = object_value(row["execution_payload"])
    packet = object_value(row["packet_payload"])
    attempt = object_value(row["attempt_payload"])
    output = object_value(execution["judge_output"])
    wrapper = object_value(output["wrapper_payload"])
    judge_input = object_value(execution["judge_input"])
    for key, expected in {
        "task_id": plan.task_id,
        "producer_agent_run_id": plan.agent_run_id,
        "rule_id": input_set.rule_id,
        "rule_code": input_set.rule_code,
        "rule_contract_hash": input_set.rule_contract_hash,
        "evidence_input_set_id": input_set.input_set_id,
        "input_hash": input_set.input_set_hash,
        "scope": "journey",
        "scope_key": "journey_observation",
        "finalization_epoch": input_set.finalization_epoch,
    }.items():
        if work.get(key) != expected:
            raise ValueError("流程规则工作项身份不一致")
    completed_status = (
        "evidence_insufficient"
        if execution.get("execution_status") == "evidence_insufficient"
        else "succeeded"
    )
    if (
        work.get("status") != completed_status
        or attempt.get("status") != completed_status
        or attempt.get("task_id") != plan.task_id
        or attempt.get("work_item_id") != work["id"]
        or attempt.get("input_hash") != input_set.input_set_hash
        or attempt.get("result_hash") != work.get("result_hash")
        or attempt.get("result_payload") != work.get("result_payload")
        or not attempt.get("consumer_agent_run_id")
    ):
        raise ValueError("流程规则工作项没有正式完成")
    for source, run_field in ((execution, "agent_run_id"), (packet, "created_by_agent_run_id")):
        if any(
            source.get(key) != expected
            for key, expected in {
                "task_id": plan.task_id,
                "rule_id": input_set.rule_id,
                run_field: attempt["consumer_agent_run_id"],
                "evidence_input_set_id": input_set.input_set_id,
                "aggregate_work_item_id": work["id"],
            }.items()
        ):
            raise ValueError("流程规则结果或观察包跨运行、规则或输入")
    expected_artifacts = list(
        dict.fromkeys(artifact for member in input_set.members for artifact in member.artifact_ids)
    )
    if (
        execution.get("observation_packet_id") != packet.get("id")
        or judge_input
        != {
            "evidence_input_set_id": input_set.input_set_id,
            "input_hash": input_set.input_set_hash,
            "scope": "journey",
            "scope_key": "journey_observation",
        }
        or object_value(work["result_payload"]) != output
        or work.get("result_hash") != stable_protocol_hash(output)
        or object_value(packet["packet_payload"]) != wrapper
        or any(
            source.get("evidence_artifact_ids") != expected_artifacts
            for source in (execution, packet, output)
        )
        or any(
            output.get(key) != execution.get(key)
            for key in ("execution_status", "result_summary", "problem_desc")
        )
        or wrapper.get("protocol_version") != "aggregate-observation-packet.v1"
        or wrapper.get("scope") != "journey"
        or wrapper.get("scope_key") != "journey_observation"
        or wrapper.get("input_set_id") != input_set.input_set_id
    ):
        raise ValueError("流程规则结果摘要、输入或观察包不一致")
    journey = object_value(wrapper.get("journey"))
    items = journey.get("checkpoints")
    selected = contract_checkpoints(contract, plan)
    if (
        journey.get("execution_plan_id") != plan.execution_plan_id
        or not isinstance(items, list)
        or [item.get("checkpoint_id") for item in items]
        != [checkpoint.checkpoint_id for checkpoint in selected]
    ):
        raise ValueError("流程分项结果与冻结计划不一致")
    views = {}
    plan_checkpoint_ids = {checkpoint.checkpoint_id for checkpoint in plan.checkpoints}
    for view in checkpoint_views:
        checkpoint_id = view["checkpoint_id"]
        if checkpoint_id not in plan_checkpoint_ids or checkpoint_id in views:
            raise ValueError("流程检查点审阅视图串属或重复")
        views[checkpoint_id] = view
    # 完整台账保留全部检查点；规则结果只按冻结选择的身份关联，不能按数组位置猜配。
    checkpoints = [
        _checkpoint_projection(
            checkpoint, item, frozen, views[checkpoint.checkpoint_id],
            input_set.finalization_epoch,
        )
        for checkpoint, item, frozen in zip(
            selected, items, contract["checkpoints"], strict=True
        )
    ]
    statuses = [item["status"] for item in checkpoints]
    expected_status = (
        "evidence_insufficient"
        if "evidence_insufficient" in statuses
        else "pass"
        if all(value == "pass" for value in statuses)
        else "fail"
    )
    if execution["execution_status"] != expected_status:
        raise ValueError("流程总结果与已保存分项状态矛盾")
    result = result_identity(row)
    result.update(
        execution_id=execution["id"],
        input_hash=input_set.input_set_hash,
        execution_status=execution["execution_status"],
        result_summary=_display_text(execution.get("result_summary")),
        problem_desc=_display_text(execution.get("problem_desc")),
        review_status=execution.get("review_status"),
        review_reason=_display_text(execution.get("review_reason")),
        linkage_status="linked",
        linkage_notice=None,
        checkpoints=checkpoints,
    )
    return result


def _checkpoint_projection(checkpoint, item, frozen, view, epoch):
    status = item.get("status")
    checks, missing = item.get("checks", {}), item.get("missing", [])
    permitted_checks = set(checkpoint.expected) | set(checkpoint.exit_condition)
    permitted_missing = {
        "post_action_sample_scope_mismatch",
        "latest_manifest_not_pass",
        "entry_or_instance_unverified",
        "readonly_contract_missing",
        "contradictory_expected_and_exit",
        "checkpoint_or_dependency_missing",
        "dependency_instance_changed_without_restoration",
        *("measurement:" + key for key in permitted_checks),
        *("multi_sample_contract:" + value.role for value in checkpoint.evidence_requirements),
        *("evidence_role:" + value.role for value in checkpoint.evidence_requirements),
    }
    if (
        status not in ("pass", "fail", "evidence_insufficient")
        or not isinstance(checks, dict)
        or not set(checks) <= permitted_checks
        or any(type(value) is not bool for value in checks.values())
        or not isinstance(missing, list)
        or any(value not in permitted_missing for value in missing)
        or item.get("recovery") != frozen.get("recovery")
        or ("bindings" in item and item["bindings"] != frozen["bindings"])
        or (status != "evidence_insufficient" and item.get("bindings") != frozen["bindings"])
    ):
        raise ValueError("流程分项包含未声明字段值或材料关联失配")
    attempts = [
        attempt
        for attempt in view["attempts"]
        if attempt["attempt_id"] == frozen["attempt_id"] and attempt["finalization_epoch"] == epoch
    ]
    if len(attempts) != 1:
        raise ValueError("流程分项没有精确执行尝试")
    attempt = attempts[0]
    references = []
    for binding, manifest in zip(frozen["bindings"], frozen["manifests"], strict=True):
        matches = [
            value
            for value in attempt["bindings"]
            if value["role"] == binding["role"] and value["capture_id"] == binding["capture_id"]
        ]
        if len(matches) != 1 or any(
            (
                matches[0]["integrity"] != "current",
                matches[0]["quality_status"] != "pass",
                matches[0]["latest_manifest_version"] != manifest["manifest_version"],
                matches[0]["action_execution_id"] != binding["action_execution_id"],
                set(binding["artifact_ids"])
                != {value["artifact_id"] for value in matches[0]["artifacts"]},
                manifest.get("session_id") != attempt["session_id"],
            )
        ):
            raise ValueError("流程分项原件已变化或跨会话")
        references.append(
            {
                "checkpoint_id": checkpoint.checkpoint_id,
                "attempt_id": attempt["attempt_id"],
                "session_id": attempt["session_id"],
                "role": binding["role"],
                "capture_id": binding["capture_id"],
                "manifest_id": manifest["manifest_id"],
                "manifest_version": manifest["manifest_version"],
                "artifact_ids": list(binding["artifact_ids"]),
            }
        )
    reason = {
        "pass": "已关联的正式检查点判定通过",
        "fail": "已关联的正式检查点实测不满足预期",
        "evidence_insufficient": "正式判定仍有证据缺口，不能视为通过",
    }[status]
    return {
        "checkpoint_id": checkpoint.checkpoint_id,
        "status": status,
        "reason": reason,
        "missing": list(missing),
        "checks": [{"name": key, "passed": value} for key, value in checks.items()],
        "evidence_refs": references,
    }


def _display_text(value):
    return humanize_qc_text(value[:2000]) if isinstance(value, str) else None
