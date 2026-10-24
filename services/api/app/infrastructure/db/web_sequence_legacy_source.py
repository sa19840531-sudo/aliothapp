"""当前批次已认证后，精确回溯曾独立写入的稳定成员，不扩大全局参与范围。"""

from services.api.app.application.rule_execution import RuleExecutionLeaseConflictError


async def read_bound_legacy_scope(
    connection, current, *, work_item_id, attempt_id, execution_id, packet_id
):
    if current["authorized"] is not True:
        raise RuleExecutionLeaseConflictError("当前 Web 批次尚未获得来源回溯授权")
    row = await connection.fetchrow(
        "SELECT work.id::text AS work_item_id,member.rule_id::text,member.rule_code,member.contract_hash,"
        "member.evidence_input_set_id,work.consumer_agent_run_id::text "
        "FROM qc_inspection_work_item work JOIN qc_inspection_work_item_rule member ON member.work_item_id=work.id "
        "JOIN qc_inspection_attempt attempt ON attempt.id=$7::uuid AND attempt.work_item_id=work.id "
        "JOIN qc_rule_execution execution ON execution.id=member.rule_execution_id "
        "AND execution.task_id=work.task_id AND execution.page_capture_id=work.page_capture_id AND execution.rule_id=member.rule_id "
        "WHERE work.id=$1::uuid AND work.task_id=$2::uuid AND work.producer_agent_run_id=$3::uuid "
        "AND work.finalization_epoch=$4 AND work.page_capture_id=$5::uuid AND member.rule_id=$6::uuid "
        "AND work.control_mode='enforced' AND work.strategy='llm' AND work.session_dependency='snapshot' "
        "AND work.status IN ('succeeded','partial') AND attempt.status='written' "
        "AND attempt.consumer_agent_run_id=work.consumer_agent_run_id "
        "AND member.status IN ('written','error','evidence_insufficient','skipped') "
        "AND member.evidence_input_set_id IS NULL AND member.rule_execution_id=$8::uuid "
        "AND member.observation_packet_id IS NOT DISTINCT FROM $9::uuid "
        "AND execution.source_idempotency_key IS NULL",
        work_item_id,
        current["task_id"],
        current["producer_agent_run_id"],
        current["finalization_epoch"],
        current["page_capture_id"],
        current["rule_id"],
        attempt_id,
        execution_id,
        packet_id,
    )
    if row is None:
        raise RuleExecutionLeaseConflictError("Web 普通历史来源缺少精确已写成员绑定，拒绝借用")
    return {**current, **dict(row)}
