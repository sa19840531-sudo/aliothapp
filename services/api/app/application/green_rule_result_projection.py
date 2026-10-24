"""绿色第4版八条规则的白名单结果展示；只核关联，不重判业务材料。"""

import json
from collections.abc import Mapping

from services.api.app.application.green_rule_catalog import validate_green_rule_configuration
from services.api.app.application.task_progress import (
    humanize_qc_text,
    rule_display_label,
    rule_display_status,
    rule_final_display_status,
)
from services.api.app.application.task_rule_inventory import inventory_execution_contract_hash
from services.api.app.domain.evidence import EvidenceInputSet
from services.api.app.domain.protocol import stable_protocol_hash

_GREEN_RESULT_RULE_KEYS = frozenset({"102", "s72", "41", "62", "s35", "s49", "s73", "32"})


def selected_green_rule_ids(task_config):
    return set(selected_green_rule_keys(task_config))


def selected_green_rule_keys(task_config):
    """只用于选择额外读取；损坏的旧配置不能破坏普通任务结果读取。"""
    inventory = task_config.get("rule_inventory") if isinstance(task_config, Mapping) else None
    rules = inventory.get("rules") if isinstance(inventory, Mapping) else None
    if not isinstance(rules, list):
        return {}
    result = {}
    for rule in rules:
        config = rule.get("judge_config") if isinstance(rule, Mapping) else None
        contract = config.get("green_rule_contract") if isinstance(config, Mapping) else None
        if (
            isinstance(contract, Mapping)
            and contract.get("schema_version") == "green-rule-contract.v2"
            and isinstance(contract.get("rule_key"), str)
            and contract.get("rule_key") in _GREEN_RESULT_RULE_KEYS
        ):
            if isinstance(rule.get("rule_id"), str) and rule["rule_id"]:
                result[rule["rule_id"]] = contract["rule_key"]
    return result


def object_value(value):
    value = json.loads(value) if isinstance(value, str) else value
    if not isinstance(value, Mapping):
        raise ValueError("结果关联载荷必须为对象")
    return dict(value)


def string_list(value):
    value = json.loads(value) if isinstance(value, str) else value
    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        raise ValueError("结果引用必须为字符串数组")
    return list(dict.fromkeys(value))


def green_result_identity(rule):
    contract = validate_green_rule_configuration(rule)
    if contract is None or contract["rule_key"] not in _GREEN_RESULT_RULE_KEYS:
        return None
    return {
        "schema_version": "green-rule-result.v1",
        "rule_key": contract["rule_key"],
        "rule_contract_hash": inventory_execution_contract_hash(rule),
        "linkage_status": "input_mismatch",
        "linkage_notice": "正式结果关联不完整，暂不展示分项结论",
        "execution_status": None,
        "review_status": None,
        "review_reason": None,
        "missing_evidence": [],
        "subitems": [],
    }


def validate_green_execution(*, task, rule, row):
    """读取精确正式Work/Attempt/Packet，拒绝同规则其他Run或旧合同借用。"""
    contract = validate_green_rule_configuration(rule)
    execution, packet = object_value(row["execution_payload"]), object_value(row["packet_payload"])
    output = object_value(execution["judge_output"])
    expected_hash = inventory_execution_contract_hash(rule)
    if contract["rule_key"] == "41" and task.get("target_platform") != "app":
        raise ValueError("绿色41仅允许读取App任务的正式外链检查")
    if (
        execution.get("task_id") != task["task_id"]
        or execution.get("rule_id") != rule["rule_id"]
        or row.get("consumer_task_id") != task["task_id"]
        or packet.get("task_id") != task["task_id"]
        or packet.get("rule_id") != rule["rule_id"]
        or packet.get("id") != execution.get("observation_packet_id")
        or packet.get("created_by_agent_run_id") != execution.get("agent_run_id")
    ):
        raise ValueError("结果或观察包不属于当前任务/规则/消费Run")
    if contract["input_kind"] != "page":
        raw, allowed, captures = _aggregate(
            task, rule, row, execution, packet, output, expected_hash, contract
        )
        missing = string_list(object_value(output["wrapper_payload"])["green"]["missing_evidence"])
    else:
        raw, allowed, captures = _page(task, rule, row, execution, packet, output, expected_hash)
        missing = string_list(output["missing_evidence"])
    if (
        not isinstance(raw, list)
        or not all(isinstance(item, Mapping) for item in raw)
        or [item.get("key", item.get("name")) for item in raw] != contract["subchecks"]
    ):
        raise ValueError("绿色分项与冻结合同不一致")
    review = object_value(row["review_payload"]) if row.get("review_payload") else None
    if review is None and execution.get("review_status") is not None:
        raise ValueError("执行声明复核状态，但缺少当前正式复核记录")
    if review and (
        review.get("task_id") != task["task_id"]
        or review.get("rule_execution_id") != execution["id"]
        or review.get("status") not in {"pending", "approved", "rejected", "needs_recheck"}
    ):
        raise ValueError("复核记录与正式执行不一致")
    return {
        "contract": contract,
        "execution": execution,
        "raw_subitems": raw,
        "allowed_artifact_ids": allowed,
        "capture_ids": captures,
        "missing_evidence": missing,
        "review_status": review["status"] if review else None,
        "review_reason": humanize_qc_text(review.get("review_reason")) if review else None,
    }


def _aggregate(task, rule, row, execution, packet, output, expected_hash, contract):
    inputs = EvidenceInputSet.from_dict(object_value(row["input_set_payload"]))
    work, attempt = (
        object_value(row["aggregate_work_payload"]),
        object_value(row["aggregate_attempt_payload"]),
    )
    expected = {
        "task_id": task["task_id"],
        "producer_agent_run_id": task["source_run_id"],
        "rule_id": rule["rule_id"],
        "rule_code": rule["rule_code"],
        "rule_contract_hash": expected_hash,
        "evidence_input_set_id": inputs.input_set_id,
        "input_hash": inputs.input_set_hash,
        "scope": "task",
        "scope_key": contract["input_kind"],
        "finalization_epoch": task["read_epoch"],
    }
    if any(work.get(key) != value for key, value in expected.items()) or (
        inputs.task_id != task["task_id"]
        or inputs.source_agent_run_id != task["source_run_id"]
        or inputs.rule_id != rule["rule_id"]
        or inputs.rule_code != rule["rule_code"]
        or inputs.rule_contract_hash != expected_hash
        or inputs.finalization_epoch != task["read_epoch"]
        or inputs.scope.value != "task"
        or inputs.scope_key != contract["input_kind"]
        or row.get("stored_input_hash") != inputs.input_set_hash
        or row.get("stored_members") != [member.to_dict() for member in inputs.members]
    ):
        raise ValueError("聚合输入跨任务、生产Run、周期或冻结合同")
    state = (
        "evidence_insufficient"
        if execution["execution_status"] == "evidence_insufficient"
        else "succeeded"
    )
    wrapper = object_value(output["wrapper_payload"])
    allowed = list(
        dict.fromkeys(artifact for member in inputs.members for artifact in member.artifact_ids)
    )
    if (
        work.get("status") != state
        or attempt.get("status") != state
        or attempt.get("work_item_id") != work.get("id")
        or attempt.get("task_id") != task["task_id"]
        or attempt.get("attempt_no") != work.get("attempt_count")
        or attempt.get("consumer_agent_run_id") != execution.get("agent_run_id")
        or attempt.get("input_hash") != inputs.input_set_hash
        or attempt.get("result_hash") != work.get("result_hash")
        or object_value(attempt.get("result_payload")) != output
        or object_value(work.get("result_payload")) != output
        or work.get("result_hash") != stable_protocol_hash(output)
        or object_value(packet.get("packet_payload")) != wrapper
        or execution.get("aggregate_work_item_id") != work.get("id")
        or packet.get("aggregate_work_item_id") != work.get("id")
        or any(
            source.get("evidence_input_set_id") != inputs.input_set_id
            for source in (execution, packet)
        )
        or any(
            string_list(source.get("evidence_artifact_ids")) != allowed
            for source in (execution, packet, output)
        )
        or object_value(execution["judge_input"])
        != {
            "evidence_input_set_id": inputs.input_set_id,
            "input_hash": inputs.input_set_hash,
            "scope": "task",
            "scope_key": inputs.scope_key,
        }
        or any(
            wrapper.get(key) != value
            for key, value in {
                "protocol_version": "aggregate-observation-packet.v1",
                "scope": "task",
                "scope_key": inputs.scope_key,
                "input_set_id": inputs.input_set_id,
            }.items()
        )
        or output.get("execution_status") != execution.get("execution_status")
    ):
        raise ValueError("聚合正式结果与最终Attempt、Packet或输入摘要不一致")
    green = object_value(wrapper["green"])
    if (
        green.get("schema_version") != "green-rule-contract.v2"
        or green.get("rule_key") != contract["rule_key"]
        or green.get("input_kind") != inputs.scope_key
        or green.get("task_scope_is_coverage") is not False
        or not isinstance(green.get("evidence_fingerprint"), str)
        or not green["evidence_fingerprint"]
    ):
        raise ValueError("聚合绿色输出未绑定既定输入合同")
    return green["subitems"], allowed, [member.capture_id for member in inputs.members]


def _page(task, rule, row, execution, packet, output, expected_hash):
    work, member, attempt = (
        object_value(row[name])
        for name in (
            "page_work_payload",
            "page_member_payload",
            "page_attempt_payload",
        )
    )
    payload = object_value(packet["packet_payload"])
    identity, page = object_value(payload["identity"]), object_value(payload["page"])
    if (
        work.get("task_id") != task["task_id"]
        or work.get("producer_agent_run_id") != task["source_run_id"]
        or work.get("finalization_epoch") != task["read_epoch"]
        or work.get("control_mode") != "enforced"
        or work.get("status") not in {"succeeded", "partial"}
        or work.get("page_capture_id") != execution.get("page_capture_id")
        or member.get("work_item_id") != work.get("id")
        or member.get("rule_id") != rule["rule_id"]
        or member.get("rule_code") != rule["rule_code"]
        or member.get("contract_hash") != expected_hash
        or member.get("status") not in {"written", "evidence_insufficient"}
        or member.get("rule_execution_id") != execution.get("id")
        or member.get("observation_packet_id") != packet.get("id")
        or attempt.get("work_item_id") != work.get("id")
        or attempt.get("attempt_no") != work.get("attempt_count")
        or attempt.get("consumer_agent_run_id") != execution.get("agent_run_id")
        or attempt.get("status") != "written"
        or packet.get("page_capture_id") != execution.get("page_capture_id")
        or page.get("page_capture_id") != execution.get("page_capture_id")
        or identity.get("source_agent_run_id") != task["source_run_id"]
        or identity.get("consumer_agent_run_id") != execution.get("agent_run_id")
        or object_value(execution["judge_input"]).get("expected_contract_hash") != expected_hash
        or output.get("green_rule_contract") != validate_green_rule_configuration(rule)
        or output.get("green_skill_program_sha256")
        != rule["judge_config"]["green_skill_program"]["sha256"]
        or output.get("conclusion") != execution.get("execution_status")
    ):
        raise ValueError("页面结果不属于当前正式Work、最终Attempt及冻结绿色合同")
    results = object_value(attempt["result_payload"])
    results = results.get("items", [results])
    matches = [
        item
        for item in results
        if isinstance(item, Mapping)
        and item.get("rule_code") == rule["rule_code"]
        and item.get("observation_packet_id") == packet["id"]
    ]
    if len(matches) != 1 or matches[0].get("execution_status") != execution.get("execution_status"):
        raise ValueError("页面最终Attempt没有精确正式结果")
    item = matches[0]
    recovered = (
        item.get("recovered") is True
        or results
        and object_value(attempt["result_payload"]).get("recovered_from_rule_executions") is True
    )
    if recovered:
        if item.get("execution_id") != execution["id"] or item.get("rule_id") != rule["rule_id"]:
            raise ValueError("恢复Attempt未精确引用正式执行")
    elif (
        item.get("rule_id") != rule["rule_id"]
        or item.get("page_capture_id") != execution["page_capture_id"]
        or string_list(item.get("evidence_artifact_ids"))
        != string_list(execution["evidence_artifact_ids"])
        or any(
            object_value(item.get("judge_output")).get(key) != output.get(key)
            for key in (
                "green_rule_contract",
                "green_skill_program_sha256",
                "conclusion",
                "subresults",
                "missing_evidence",
            )
        )
    ):
        raise ValueError("页面最终Attempt分项或判据与正式执行不一致")
    if not execution.get("evidence_input_set_id") and attempt.get("input_hash") != work.get(
        "input_hash"
    ):
        raise ValueError("页面Attempt输入摘要没有精确绑定")
    allowed = string_list(execution["evidence_artifact_ids"])
    if string_list(packet["evidence_artifact_ids"]) != allowed:
        raise ValueError("页面执行和Packet材料引用不一致")
    roles = identity.get("capture_roles") or {"primary": execution["page_capture_id"]}
    if not isinstance(roles, Mapping) or roles.get("primary") != execution["page_capture_id"]:
        raise ValueError("页面primary角色未精确关联")
    if (
        execution.get("evidence_input_set_id")
        or member.get("evidence_input_set_id")
        or row.get("input_set_payload")
    ):
        inputs = EvidenceInputSet.from_dict(object_value(row["input_set_payload"]))
        if (
            inputs.task_id != task["task_id"]
            or inputs.source_agent_run_id != task["source_run_id"]
            or inputs.rule_id != rule["rule_id"]
            or inputs.rule_code != rule["rule_code"]
            or inputs.scope.value not in {"page", "web_sequence"}
            or inputs.rule_contract_hash != expected_hash
            or inputs.finalization_epoch != task["read_epoch"]
            or inputs.input_set_id != member.get("evidence_input_set_id")
            or inputs.input_set_id != execution.get("evidence_input_set_id")
            or (
                packet.get("evidence_input_set_id") is not None
                and packet["evidence_input_set_id"] != inputs.input_set_id
            )
            or inputs.input_set_hash != row.get("stored_input_hash")
            or row.get("stored_members") != [item.to_dict() for item in inputs.members]
            or not set(allowed).issubset(
                {ref for item in inputs.members for ref in item.artifact_ids}
            )
        ):
            raise ValueError("页面正式InputSet关联不一致")
    return output["subresults"], allowed, list(dict.fromkeys(roles.values()))


def project_green_subitems(validated, artifacts):
    """仅使用仓储已核验的同正式材料；截图展示缺口不改业务结论与业务缺证。"""
    by_id = {item["artifact_id"]: item for item in artifacts}
    allowed = set(validated["allowed_artifact_ids"])
    if allowed != set(by_id):
        raise ValueError("正式材料缺少可信关联")
    result = []
    for item in validated["raw_subitems"]:
        status, evaluated = item.get("status"), item.get("evaluated")
        if (
            status not in {"pass", "fail", "skipped", "manual_review", "evidence_insufficient"}
            or type(evaluated) is not bool
        ):
            raise ValueError("绿色分项状态无效")
        confidence = item.get("confidence")
        if (status in {"pass", "fail"} and not evaluated) or (
            status == "evidence_insufficient" and evaluated
        ):
            raise ValueError("绿色分项未判状态不一致")
        if (not evaluated and confidence is not None) or (
            confidence is not None
            and (type(confidence) not in (int, float) or not 0 <= confidence <= 1)
        ):
            raise ValueError("绿色分项置信度无效")
        captures = string_list(item.get("capture_ids", []))
        if set(captures) - set(validated["capture_ids"]):
            raise ValueError("分项采集跨正式输入")
        refs = string_list(item.get("evidence_refs", []))
        if validated["contract"]["input_kind"] != "page":
            refs = [
                key
                for key, artifact in by_id.items()
                if evaluated
                and artifact["capture_id"] in captures
                and artifact["artifact_type"] in {"ui_snapshot", "action_trace"}
            ]
        elif refs:
            captures = list(dict.fromkeys(by_id[key]["capture_id"] for key in refs if key in by_id))
        if set(refs) - allowed or (evaluated and not refs):
            raise ValueError("分项判据跨正式材料")
        # 输入Capture只说明材料范围；展示图必须关联本分项实际引用的判据Capture。
        criterion_captures = {by_id[key]["capture_id"] for key in refs}
        screenshots = [
            key
            for key, artifact in by_id.items()
            if artifact["capture_id"] in criterion_captures and artifact["png_verified"]
        ]
        result.append(
            {
                "key": item.get("key", item.get("name")),
                "status": status,
                "evaluated": evaluated,
                "confidence": confidence,
                "reason": humanize_qc_text(item.get("reason")),
                "missing_evidence": string_list(
                    item.get("missing_evidence", item.get("missing", []))
                ),
                "evidence_artifact_ids": refs,
                "capture_ids": captures,
                "screenshot_artifact_ids": screenshots,
                "evidence_notice": None
                if screenshots
                else "正式判据中暂无可展示原始PNG；不据此改变机器结论",
            }
        )
    return result


def green_result_has_material_gap(green):
    """业务缺项与关联缺口不能由人工结论补齐；展示缺图不属于业务缺证。"""
    return (
        green.get("linkage_status") != "linked"
        or green.get("execution_status") == "evidence_insufficient"
        or bool(green.get("missing_evidence"))
        or any(
            item.get("status") == "evidence_insufficient"
            or item.get("evaluated") is False
            or bool(item.get("missing_evidence"))
            for item in green.get("subitems") or []
        )
    )


def apply_green_results(items, projections, *, expected_rules=None):
    """只同步绿色结果的实际复核状态，业务机器结论和人工终审沿用原展示规则。"""
    for item in items:
        green = projections.get(item.get("execution_id"))
        rule_key = (expected_rules or {}).get(item.get("rule_id"))
        if green is None and rule_key is not None and item.get("execution_id"):
            green = {
                "schema_version": "green-rule-result.v1",
                "rule_key": rule_key,
                "rule_contract_hash": None,
                "linkage_status": "input_mismatch",
                "linkage_notice": "冻结合同或正式结果关联待核对，暂不展示分项结论",
                "execution_status": None,
                "review_status": None,
                "review_reason": None,
                "missing_evidence": [],
                "subitems": [],
            }
        if green is None:
            continue
        item["green_result"] = green
        if green["linkage_status"] != "linked":
            item.update(
                display_status="manual_review",
                display_status_label="关联待核对",
                confirmable=False,
                confirm_status=None,
            )
            continue
        item["review_status"] = green["review_status"]
        item["review_reason"] = green["review_reason"]
        state = item["execution_status"]
        verdict = item.get("human_verdict")
        if green_result_has_material_gap(green):
            verdict = None
            item.update(confirmable=False, confirm_status=None)
            if state in {"pass", "manual_review", "skipped"}:
                state = "evidence_insufficient"
        if green["review_status"] in {"pending", "needs_recheck"}:
            verdict = None
            if state == "pass":
                state = "manual_review"
        machine = rule_display_status(state, item.get("execution_reason"))
        display = rule_final_display_status(
            state, item.get("execution_reason"), human_verdict=verdict
        )
        item.update(
            machine_display_status=machine,
            machine_display_status_label=rule_display_label(machine),
            display_status=display,
            display_status_label=rule_display_label(display),
        )
