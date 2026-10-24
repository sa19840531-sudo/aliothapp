"""Web 序列专属语义投影；不改 Manifest 齐全性及稳定页面合同。"""

import json
from dataclasses import asdict

from services.api.app.application.web_rule_evidence_contract import (
    validate_web_sequence_execution,
)


def project_web_sequence_semantics(*, request, source):
    """只接收专用可信仓储返回的 InputSet 证明，不读取 Capture metadata。"""
    if request.evidence_input_set_id is None:
        return None
    proof = source.web_sequence
    rejection = {"status": "evidence_insufficient", "reason_code": "WEB_SEQUENCE_BINDING_INVALID"}
    if not isinstance(proof, dict) or any(
        (
            proof.get("input_set_id") != request.evidence_input_set_id,
            proof.get("rule_id") != request.rule_id,
            not request.expected_contract_hash,
            proof.get("rule_contract_hash") != request.expected_contract_hash,
        )
    ):
        return rejection
    # 绑定已由权威读取器及当前请求核验；后续合法不足必须保留这份精确身份供 Writer 审计。
    rejection = {**rejection, "input_set_id": proof["input_set_id"],
                 "rule_contract_hash": proof["rule_contract_hash"]}
    try:
        contract = validate_web_sequence_execution(
            request.input_contract,
            executor_type=request.executor_type,
            judge_config=request.judge_config,
        )
    except ValueError:
        return {**rejection, "reason_code": "WEB_SEQUENCE_CONTRACT_INVALID"}
    if contract is None or not isinstance(proof.get("sequence"), dict):
        return {**rejection, "reason_code": "WEB_SEQUENCE_CONTRACT_MISSING"}
    if request.input_contract.get("auth_requirement") in {"authenticated", "guest"}:
        return {**rejection, "reason_code": "WEB_RULE_ACCOUNT_STATE_UNKNOWN"}
    sequence = proof["sequence"]
    result = contract.validate_sequence(
        sequence,
        task_id=request.task_id,
        agent_run_id=request.source_agent_run_id,
        finalization_epoch=proof.get("finalization_epoch", sequence.get("finalization_epoch")),
    )
    projection = asdict(result)
    projection["sample_capture_ids"] = list(result.sample_capture_ids)
    projection.update(
        {
            "input_set_id": request.evidence_input_set_id,
            "rule_contract_hash": request.expected_contract_hash,
            "contract": contract.to_dict(),
        }
    )
    if contract.local_structure is not None:
        from services.api.app.application.web_local_structure import plan_web_local_structure_rule

        revision = sequence.get("capability_revision") or {}
        local_plan = plan_web_local_structure_rule(
            contract.local_structure,
            revision,
            rule_version=contract.rule_version,
            rule_contract_hash=request.expected_contract_hash,
        )
        if local_plan["status"] != "admissible":
            return {**rejection, "reason_code": local_plan["reason_code"]}
        projection["capability_revision"] = revision
        projection["local_structure_plan"] = local_plan
    if result.status == "admissible":
        if sequence["purpose"] == "interaction_sequence":
            projection["interaction_outcomes"] = sequence["interaction_outcomes"]
        # 独立完整帧身份不借用会截断的旧 journey；业务原文由制品解码提供。
        projection["frames"] = [
            {
                key: frame.get(key)
                for key in (
                    "capture_id",
                    "capture_role",
                    "parent_capture_id",
                    "action_execution_id",
                    "captured_at",
                    "target_id",
                    "occurrence_id",
                    "page_state",
                    "artifact_ids",
                )
            }
            for frame in sequence["frames"]
        ]
        from services.api.app.application.observation_packet import _decode_artifacts

        if len(source.web_sequence_sources) != len(projection["frames"]):
            return {**rejection, "reason_code": "WEB_SEQUENCE_FRAME_FACTS_MISSING"}
        for frame, member_source in zip(
            projection["frames"], source.web_sequence_sources, strict=True
        ):
            if member_source.page_capture_id != frame["capture_id"]:
                return {**rejection, "reason_code": "WEB_SEQUENCE_FRAME_FACTS_MISMATCH"}
            by_type = {}
            for artifact in member_source.artifacts:
                by_type.setdefault(artifact.artifact_type, []).append(artifact)
            decoded = _decode_artifacts(by_type)
            frame["facts"] = {
                key: decoded.get(key)
                for key in (
                    "visible_text",
                    "visible_text_truncated",
                    "dom_summary",
                    "ui_snapshot",
                    "media_state",
                    "network_summary",
                    "console_summary",
                )
            }
        if len(json.dumps(projection, ensure_ascii=False).encode("utf-8")) > 48_000:
            return {**rejection, "reason_code": "WEB_SEQUENCE_FACT_BUDGET_EXCEEDED"}
    return projection
