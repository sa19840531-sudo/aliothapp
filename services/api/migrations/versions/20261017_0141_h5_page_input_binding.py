"""允许严格绑定的 H5 页面输入，保留 Web 序列与历史不可改写保护。"""

from alembic import op

revision = "20261017_0141"
down_revision = "20261016_0140"
branch_labels = None
depends_on = None

_H5_BINDING = """
        IF NEW.evidence_input_set_id IS NOT NULL AND EXISTS (
            SELECT 1 FROM qc_inspection_work_item work
            JOIN qc_task task ON task.id=work.task_id
            WHERE work.id=NEW.work_item_id AND lower(task.target_platform)='h5'
        ) THEN
            IF NOT EXISTS (
                SELECT 1 FROM qc_inspection_work_item work
                JOIN qc_evidence_input_set inputs ON inputs.input_set_id=NEW.evidence_input_set_id
                  AND inputs.task_id=work.task_id
                  AND inputs.source_agent_run_id=work.producer_agent_run_id
                  AND inputs.finalization_epoch=work.finalization_epoch
                  AND inputs.ruleset_snapshot_hash=work.ruleset_snapshot_hash
                  AND inputs.scope='page' AND inputs.rule_id=NEW.rule_id
                  AND inputs.rule_code=NEW.rule_code AND inputs.rule_contract_hash=NEW.contract_hash
                JOIN qc_evidence_input_set_member binding ON binding.input_set_id=inputs.input_set_id
                  AND binding.task_id=work.task_id AND binding.capture_id=work.page_capture_id
                  AND binding.ordinal=1
                JOIN qc_capture capture ON capture.id=binding.capture_id
                  AND capture.task_id=work.task_id AND capture.agent_run_id=work.producer_agent_run_id
                  AND capture.capture_status='captured' AND capture.capture_role=binding.capture_role
                WHERE work.id=NEW.work_item_id
                  AND (SELECT count(*) FROM qc_evidence_input_set_member
                       WHERE input_set_id=inputs.input_set_id)=1
            ) THEN RAISE EXCEPTION 'H5_PAGE_RULE_BINDING' USING ERRCODE='23514'; END IF;
            RETURN NEW;
        END IF;
"""


def _guard_sql(h5_binding: str) -> str:
    return """
    CREATE OR REPLACE FUNCTION qc_guard_web_work_rule_input()
    RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
        IF TG_OP='UPDATE' AND OLD.evidence_input_set_id IS DISTINCT FROM NEW.evidence_input_set_id THEN
            RAISE EXCEPTION 'WEB_SEQUENCE_BINDING_IMMUTABLE' USING ERRCODE='23514';
        END IF;
        __H5_BINDING__
        IF NEW.evidence_input_set_id IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM qc_inspection_work_item work
            JOIN qc_evidence_input_set inputs ON inputs.task_id=work.task_id
                AND inputs.source_agent_run_id=work.producer_agent_run_id
                AND inputs.finalization_epoch=work.finalization_epoch
            JOIN qc_web_evidence_sequence sequence ON sequence.sequence_id=inputs.web_evidence_sequence_id
                AND sequence.anchor_capture_id=work.page_capture_id
            WHERE work.id=NEW.work_item_id AND inputs.input_set_id=NEW.evidence_input_set_id
                AND inputs.scope='web_sequence' AND inputs.rule_id=NEW.rule_id
                AND inputs.rule_contract_hash=NEW.contract_hash
        ) THEN RAISE EXCEPTION 'WEB_SEQUENCE_RULE_BINDING' USING ERRCODE='23514'; END IF;
        RETURN NEW;
    END $$;
    """.replace("__H5_BINDING__", h5_binding)


def upgrade() -> None:
    op.execute(_guard_sql(_H5_BINDING))


def downgrade() -> None:
    # 已使用的新合同不能被旧 Web-only 约束重新解释，不删绑定或历史来降级。
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (
                SELECT 1 FROM qc_inspection_work_item_rule member
                JOIN qc_inspection_work_item work ON work.id=member.work_item_id
                JOIN qc_task task ON task.id=work.task_id
                WHERE lower(task.target_platform)='h5' AND member.evidence_input_set_id IS NOT NULL
            ) THEN RAISE EXCEPTION 'H5_PAGE_BINDING_DOWNGRADE_BLOCKED' USING ERRCODE='23514'; END IF;
        END $$;
    """)
    op.execute(_guard_sql(""))
