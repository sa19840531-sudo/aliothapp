"""合同冲突终结的批次，仅允许逐条可重证已正式提交的历史来源。"""

import json

from services.api.app.application.rule_execution import RuleExecutionLeaseConflictError


async def assert_committed_terminal_source(connection, scope, source):
    row = await connection.fetchrow(
        "SELECT execution.judge_output FROM qc_inspection_work_item work "
        "JOIN qc_inspection_work_item_rule member ON member.work_item_id=work.id "
        "JOIN qc_inspection_attempt attempt ON attempt.id=$7::uuid AND attempt.work_item_id=work.id "
        "JOIN qc_rule_execution execution ON execution.id=member.rule_execution_id "
        "AND execution.task_id=work.task_id AND execution.page_capture_id=work.page_capture_id "
        "AND execution.rule_id=member.rule_id AND execution.source_idempotency_key IS NULL "
        "WHERE work.id=$1::uuid AND work.task_id=$2::uuid AND work.producer_agent_run_id=$3::uuid "
        "AND work.finalization_epoch=$4 AND work.page_capture_id=$5::uuid AND member.rule_id=$6::uuid "
        "AND work.status='failed' AND work.last_error_code='WEB_RULE_CONTRACT_INCOMPATIBLE' "
        "AND attempt.status='terminal_error' AND attempt.error_code='WEB_RULE_CONTRACT_INCOMPATIBLE' "
        "AND attempt.consumer_agent_run_id=work.consumer_agent_run_id AND attempt.consumer_agent_run_id=$8::uuid "
        "AND attempt.attempt_no=work.attempt_count AND member.contract_hash=$9 "
        "AND member.evidence_input_set_id IS NOT DISTINCT FROM $10::text "
        "AND member.observation_packet_id IS NOT DISTINCT FROM $11::uuid "
        "AND member.status=$12",
        scope["work_item_id"],
        scope["task_id"],
        scope["producer_agent_run_id"],
        scope["finalization_epoch"],
        scope["page_capture_id"],
        scope["rule_id"],
        source["attempt_id"],
        source["consumer_agent_run_id"],
        scope["contract_hash"],
        scope["evidence_input_set_id"],
        source["observation_packet_id"],
        source["execution_status"]
        if source["execution_status"] in {"error", "skipped", "evidence_insufficient"}
        else "written",
    )
    if row is None:
        raise RuleExecutionLeaseConflictError("Web 失败批次来源没有逐条正式写入关联")
    output = (
        json.loads(row["judge_output"])
        if isinstance(row["judge_output"], str)
        else row["judge_output"]
    )
    provenance = output.get("web_composite_provenance", {})
    if any(
        provenance.get(key) != scope[key]
        for key in (
            "task_id",
            "producer_agent_run_id",
            "finalization_epoch",
            "page_capture_id",
            "rule_id",
        )
    ) or source not in provenance.get("sources", []):
        raise RuleExecutionLeaseConflictError("Web 失败批次来源未被同合同正式审计闭包认证")
