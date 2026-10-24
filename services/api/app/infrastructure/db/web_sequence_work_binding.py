"""Web 序列的逐规则 Work 成员封存；旧无绑定成员继续原 SQL。"""


async def insert_web_sequence_work_rule(connection, *, work_item_id, rule):
    """首次插入即绑定 InputSet，冲突时拒绝不同合同/集合覆盖。"""
    return await connection.fetchval(
        """
        INSERT INTO qc_inspection_work_item_rule AS existing (
            work_item_id, rule_id, rule_code, ordinal, contract_hash, evidence_input_set_id
        )
        SELECT work.id, rule.id, rule.rule_code, $4, $5, $6
        FROM qc_inspection_work_item work
        JOIN qc_rule rule ON rule.id=$2::uuid AND rule.ruleset_id=ANY(qc_ruleset_scope(work.ruleset_id))
        JOIN qc_evidence_input_set inputs ON inputs.input_set_id=$6
            AND inputs.task_id=work.task_id AND inputs.rule_id=rule.id
            AND inputs.source_agent_run_id=work.producer_agent_run_id
            AND inputs.finalization_epoch=work.finalization_epoch AND inputs.rule_contract_hash=$5
            AND inputs.scope='web_sequence'
        JOIN qc_web_evidence_sequence sequence ON sequence.sequence_id=inputs.web_evidence_sequence_id
            AND sequence.anchor_capture_id=work.page_capture_id
        JOIN qc_task task ON task.id=work.task_id AND task.target_platform='web'
            AND task.task_config->>'workflow_version'='v2' AND task.finalization_epoch=work.finalization_epoch
        WHERE work.id=$1::uuid AND rule.rule_code=$3 AND work.session_dependency='snapshot'
            AND work.strategy='llm'
        ON CONFLICT ON CONSTRAINT uq_qc_inspection_work_item_rule
        DO UPDATE SET updated_at=existing.updated_at
        WHERE existing.contract_hash=EXCLUDED.contract_hash
            AND existing.evidence_input_set_id=EXCLUDED.evidence_input_set_id
            AND existing.ordinal=EXCLUDED.ordinal
        RETURNING id::text
        """,
        work_item_id,
        rule.rule_id,
        rule.rule_code,
        rule.ordinal,
        rule.contract_hash,
        rule.evidence_input_set_id,
    )
