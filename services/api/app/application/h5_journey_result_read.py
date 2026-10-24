"""冻结分项结果的最小读投影；身份缺失时不提供可点击的猜测材料。"""

import json
from copy import deepcopy


def _object(value):
    return json.loads(value) if isinstance(value, str) else value


def project_journey_result(value, *, execution_id, input_set_id, artifact_ids):
    if not isinstance(value, dict) or (
        value.get("execution_id") != execution_id
        or not input_set_id
        or value.get("input_set_id") != input_set_id
        or not value.get("execution_plan_id")
        or not isinstance(value.get("checkpoints"), list)
        or not value["checkpoints"]
    ):
        return None
    seen = set()
    projected = deepcopy(value)
    for checkpoint in projected["checkpoints"]:
        if not isinstance(checkpoint, dict) or (
            not isinstance(checkpoint.get("checkpoint_id"), str)
            or not checkpoint["checkpoint_id"]
            or checkpoint["checkpoint_id"] in seen
            or checkpoint.get("status")
            not in ("pass", "fail", "evidence_insufficient", "needs_review")
            or not isinstance(checkpoint.get("checks", {}), dict)
            or any(type(item) is not bool for item in checkpoint.get("checks", {}).values())
            or not isinstance(checkpoint.get("missing", []), list)
            or any(not isinstance(item, str) for item in checkpoint.get("missing", []))
        ):
            return None
        seen.add(checkpoint["checkpoint_id"])
        checkpoint.setdefault("checks", {})
        checkpoint.setdefault("missing", [])
        bindings = checkpoint.get("bindings", [])
        if not bindings:
            if checkpoint["status"] in ("pass", "fail"):
                return None
            checkpoint.update(bindings=[], integrity="missing")
            continue
        if not isinstance(bindings, list) or len(bindings) != 2:
            return None
        for binding in bindings:
            if (
                not isinstance(binding, dict)
                or any(
                    not isinstance(binding.get(key), str) or not binding[key]
                    for key in ("role", "capture_id", "action_execution_id", "manifest_id")
                )
                or type(binding.get("manifest_version")) is not int
                or binding["manifest_version"] < 1
                or not isinstance(binding.get("artifact_ids"), list)
                or not binding["artifact_ids"]
                or any(not isinstance(item, str) or not item for item in binding["artifact_ids"])
                or len(set(binding["artifact_ids"])) != len(binding["artifact_ids"])
                or not set(binding["artifact_ids"]) <= set(artifact_ids)
            ):
                return None
        if {item["role"] for item in bindings} != {
            "before_action",
            "after_action",
        } or len({item["capture_id"] for item in bindings}) != 2:
            return None
        if len({item["action_execution_id"] for item in bindings}) != 1:
            return None
        checkpoint["bindings"] = [
            {
                key: item[key]
                for key in (
                    "role",
                    "capture_id",
                    "action_execution_id",
                    "manifest_id",
                    "manifest_version",
                    "artifact_ids",
                    "quality_status",
                    "independent",
                )
                if key in item
            }
            for item in bindings
        ]
        checkpoint["integrity"] = "bound"
    # 仅返回登记的展示字段，不传播模型附带的任意 HTML、URL 或运行时对象。
    return {
        key: projected.get(key)
        for key in (
            "execution_id",
            "input_set_id",
            "execution_plan_id",
            "scenario_key",
            "execution_status",
        )
    } | {
        "checkpoints": [
            {
                key: item.get(key)
                for key in (
                    "checkpoint_id",
                    "status",
                    "checks",
                    "missing",
                    "bindings",
                    "integrity",
                )
            }
            for item in projected["checkpoints"]
        ]
    }


def project_live_journey_result(row):
    """预览只读既有封存输入，分项绑定须与真实合同一致，不改判定状态。"""
    from services.api.app.domain.evidence import EvidenceInputSet
    from services.api.app.domain.journey.validation import stable_hash

    try:
        wrapper = _object(row.get("journey_wrapper"))
        contract = _object(row.get("journey_contract_payload"))
        inputs = EvidenceInputSet.from_dict(_object(row.get("journey_input_payload")))
        journey = wrapper["journey"]
        if (
            wrapper.get("scope") != "journey"
            or wrapper.get("input_set_id") != inputs.input_set_id
            or inputs.input_set_id != row.get("evidence_input_set_id")
            or inputs.task_id != row.get("journey_task_id")
            or inputs.rule_id != row.get("rule_id")
            or inputs.source_agent_run_id != contract["agent_run_id"]
            or inputs.rule_contract_hash != contract["rule_contract_hash"]
            or inputs.finalization_epoch != contract["epoch"]
            or contract["task_id"] != inputs.task_id
            or stable_hash(contract) != row.get("journey_selection_hash")
            or journey["execution_plan_id"] != contract["plan"]["execution_plan_id"]
            or [item["checkpoint_id"] for item in journey["checkpoints"]]
            != [item["checkpoint_id"] for item in contract["checkpoints"]]
            or any(
                item.get("bindings", []) != frozen["bindings"]
                for item, frozen in zip(
                    journey["checkpoints"], contract["checkpoints"], strict=True
                )
            )
        ):
            return None
        return project_journey_result(
            {
                **journey,
                "execution_id": row["execution_id"],
                "input_set_id": inputs.input_set_id,
                "scenario_key": contract["plan"]["scenario_key"],
                "execution_status": row["execution_status"],
            },
            execution_id=row["execution_id"],
            input_set_id=inputs.input_set_id,
            artifact_ids=row.get("evidence_artifact_ids") or [],
        )
    except (ValueError, TypeError, KeyError, AttributeError):
        return None
