"""冻结平台局部脱敏计划，并复用原证据访问审计保留操作链。"""

from alembic import op

revision = "20261023_0146"
down_revision = "20261023_0145"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        CREATE TABLE qc_evidence_redaction_plan (
          id uuid PRIMARY KEY,
          artifact_id uuid NOT NULL,
          task_id uuid NOT NULL,
          tenant_id varchar(64) NOT NULL,
          plan_hash varchar(64) NOT NULL,
          plan_payload jsonb NOT NULL,
          created_by varchar(64) NOT NULL,
          created_at timestamptz NOT NULL,
          CONSTRAINT ck_evidence_redaction_plan_hash CHECK (plan_hash ~ '^[0-9a-f]{64}$'),
          CONSTRAINT ck_evidence_redaction_plan_payload CHECK (COALESCE(
            jsonb_typeof(plan_payload)='object'
            AND plan_payload->>'policy_version'='evidence-local-mask.v1'
            AND plan_payload->>'redaction_id'=id::text
            AND plan_payload->>'tenant_id'=tenant_id
            AND plan_payload->>'plan_hash'=plan_hash
            AND plan_payload->'source'->>'artifact_id'=artifact_id::text
            AND plan_payload->'source'->>'task_id'=task_id::text
            AND plan_payload->'source'->>'source_hash' ~ '^[0-9a-f]{64}$'
            AND plan_payload->'source'->>'original_checksum' ~ '^sha256:[0-9a-f]{64}$'
            AND plan_payload->>'derived_checksum' ~ '^sha256:[0-9a-f]{64}$'
            AND jsonb_typeof(plan_payload->'spec')='object'
            AND jsonb_typeof(plan_payload->'derived_size_bytes')='number'
            AND (plan_payload->>'derived_size_bytes')::bigint >= 0
            AND length(plan_payload->>'derived_content_type') > 0
            AND plan_payload->>'reviewed_by'=created_by
            AND (plan_payload->>'created_at')::timestamptz=created_at
            AND jsonb_typeof(plan_payload->'business_reviewable')='boolean', false))
        )
    """)
    op.execute("""
        CREATE INDEX idx_evidence_redaction_artifact
        ON qc_evidence_redaction_plan (artifact_id, created_at DESC, id)
    """)
    op.execute("""
        COMMENT ON TABLE qc_evidence_redaction_plan IS
        '不可变局部脱敏计划及派生摘要；不存字节，不替换原件，不作为判定输入'
    """)
    op.execute("""
        CREATE FUNCTION qc_reject_evidence_redaction_plan_mutation()
        RETURNS trigger AS $$ BEGIN
          RAISE EXCEPTION 'qc_evidence_redaction_plan is immutable';
        END; $$ LANGUAGE plpgsql
    """)
    op.execute("""
        CREATE TRIGGER trg_evidence_redaction_plan_immutable
        BEFORE UPDATE OR DELETE ON qc_evidence_redaction_plan
        FOR EACH ROW EXECUTE FUNCTION qc_reject_evidence_redaction_plan_mutation()
    """)
    op.execute("""
        ALTER TABLE qc_evidence_download_audit
          ADD COLUMN operation varchar(32),
          ADD COLUMN redaction_id uuid,
          ADD COLUMN redaction_plan_hash varchar(64),
          ADD COLUMN source_hash varchar(64),
          ADD CONSTRAINT ck_evidence_access_operation CHECK (
            operation IS NULL OR operation IN (
              'download','export','redaction_preview','redaction_create',
              'redaction_read','redaction_list')),
          ADD CONSTRAINT ck_evidence_redaction_audit_hash CHECK (
            (redaction_plan_hash IS NULL OR redaction_plan_hash ~ '^[0-9a-f]{64}$')
            AND (source_hash IS NULL OR source_hash ~ '^[0-9a-f]{64}$')),
          ADD CONSTRAINT ck_evidence_redaction_audit_create CHECK (
            operation IS DISTINCT FROM 'redaction_create' OR result<>'allowed'
            OR (redaction_id IS NOT NULL AND redaction_plan_hash IS NOT NULL
                AND source_hash IS NOT NULL AND original_checksum IS NOT NULL
                AND derived_checksum IS NOT NULL AND task_id IS NOT NULL))
    """)
    op.execute("""
        CREATE INDEX idx_evidence_access_redaction
        ON qc_evidence_download_audit (redaction_id, created_at DESC)
        WHERE redaction_id IS NOT NULL
    """)
    op.execute("""
        COMMENT ON COLUMN qc_evidence_download_audit.operation IS
        '下载、导出及局部脱敏预览/创建/读取/列表；NULL 表示升级前历史记录'
    """)
    op.execute("""
        COMMENT ON COLUMN qc_evidence_download_audit.export_mode IS
        'original 原件；redacted 脱敏派生副本，按 policy_version 与计划标识解释'
    """)


def downgrade():
    op.execute("""
        DO $$ BEGIN
          IF EXISTS (SELECT 1 FROM qc_evidence_redaction_plan)
             OR EXISTS (SELECT 1 FROM qc_evidence_download_audit
                         WHERE operation IS NOT NULL OR redaction_id IS NOT NULL
                            OR redaction_plan_hash IS NOT NULL OR source_hash IS NOT NULL) THEN
            RAISE EXCEPTION 'cannot remove evidence redaction while plan or audit rows exist';
          END IF;
        END $$
    """)
    op.execute("DROP INDEX idx_evidence_access_redaction")
    op.execute("""
        ALTER TABLE qc_evidence_download_audit
          DROP CONSTRAINT ck_evidence_access_operation,
          DROP CONSTRAINT ck_evidence_redaction_audit_hash,
          DROP CONSTRAINT ck_evidence_redaction_audit_create,
          DROP COLUMN operation, DROP COLUMN redaction_id,
          DROP COLUMN redaction_plan_hash, DROP COLUMN source_hash
    """)
    op.execute("DROP TABLE qc_evidence_redaction_plan")
    op.execute("DROP FUNCTION qc_reject_evidence_redaction_plan_mutation()")
    op.execute("""
        COMMENT ON COLUMN qc_evidence_download_audit.export_mode IS
          'original 原件；redacted 保守遮盖派生副本，仅用于追溯'
    """)
