"""仅扩展 checkpoint 错误诊断字段；恢复载荷白名单保持不变。"""

from alembic import op

revision = "20260916_0121"
down_revision = "20260916_0120"
branch_labels = None
depends_on = None

_REFERENCES = """'parent_capture_id','capture_id','menu_ledger_id','session_id',
    'idempotency_key','obstruction_fingerprint','surface_phase_run_id',
    'candidate_href_index','finalization_epoch','current_url','requested_url','final_url'"""
_AUDIT = "'raw_error_code','coordination_stage','cause_type','retryable'"


def _reference_constraint(*, audit):
    fields = _REFERENCES + ("," + _AUDIT if audit else "")
    return f"""ALTER TABLE qc_web_run_stage_checkpoint
        ADD CONSTRAINT ck_qc_web_checkpoint_reference_only CHECK (
            cursor_payload - ARRAY[{_REFERENCES}]::text[] = '{{}}'::jsonb
            AND last_error_detail - ARRAY[{fields}]::text[] = '{{}}'::jsonb)"""


def upgrade():
    op.execute("""ALTER TABLE qc_web_run_stage_checkpoint
        DROP CONSTRAINT ck_qc_web_checkpoint_reference_only""")
    op.execute(_reference_constraint(audit=True))
    op.execute("""ALTER TABLE qc_web_run_stage_checkpoint
        ADD CONSTRAINT ck_qc_web_checkpoint_error_audit CHECK (
            (NOT last_error_detail ? 'raw_error_code' OR (
                jsonb_typeof(last_error_detail->'raw_error_code') = 'string'
                AND last_error_detail->>'raw_error_code' ~ '^[A-Za-z][A-Za-z0-9_.-]{0,127}$'))
            AND (NOT last_error_detail ? 'cause_type' OR (
                jsonb_typeof(last_error_detail->'cause_type') = 'string'
                AND last_error_detail->>'cause_type' ~ '^[A-Za-z][A-Za-z0-9_.-]{0,127}$'))
            AND (NOT last_error_detail ? 'coordination_stage' OR (
                jsonb_typeof(last_error_detail->'coordination_stage') = 'string'
                AND last_error_detail->>'coordination_stage'
                    IN ('before_capture','action','after_capture')))
            AND (NOT last_error_detail ? 'retryable' OR
                jsonb_typeof(last_error_detail->'retryable') = 'boolean'))""")


def downgrade():
    # 已产生新诊断时拒绝降级，避免为了恢复旧白名单静默删除审计字段。
    op.execute(f"""DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM qc_web_run_stage_checkpoint
            WHERE last_error_detail ?| ARRAY[{_AUDIT}]::text[]) THEN
            RAISE EXCEPTION 'WEB_CHECKPOINT_ERROR_AUDIT_DOWNGRADE_BLOCKED';
        END IF;
    END $$""")
    op.execute("""ALTER TABLE qc_web_run_stage_checkpoint
        DROP CONSTRAINT ck_qc_web_checkpoint_error_audit,
        DROP CONSTRAINT ck_qc_web_checkpoint_reference_only""")
    op.execute(_reference_constraint(audit=False))
