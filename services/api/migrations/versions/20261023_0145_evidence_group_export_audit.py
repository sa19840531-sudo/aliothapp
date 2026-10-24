"""扩展既有不可变下载审计，记录整组导出模式、失败和原件派生追溯。"""

from alembic import op

revision = "20261023_0145"
down_revision = "20261022_0144"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        ALTER TABLE qc_evidence_download_audit
          DROP CONSTRAINT ck_qc_evidence_download_result,
          DROP CONSTRAINT ck_qc_evidence_download_denied_reason,
          ADD COLUMN export_request_id varchar(64),
          ADD COLUMN task_id varchar(64),
          ADD COLUMN execution_plan_id text,
          ADD COLUMN snapshot_hash varchar(64),
          ADD COLUMN export_mode varchar(16),
          ADD COLUMN policy_version varchar(64),
          ADD COLUMN original_checksum varchar(71),
          ADD COLUMN derived_checksum varchar(71),
          ADD COLUMN archive_checksum varchar(71),
          ADD COLUMN failed_artifact_id varchar(64),
          ADD CONSTRAINT ck_qc_evidence_download_result CHECK (
            result IN ('allowed','denied','failed')),
          ADD CONSTRAINT ck_qc_evidence_download_denied_reason CHECK (
            (result IN ('denied','failed') AND denied_reason IS NOT NULL)
            OR (result='allowed' AND denied_reason IS NULL)),
          ADD CONSTRAINT ck_qc_evidence_export_metadata CHECK (
            export_mode IS NULL OR COALESCE(
              export_mode IN ('original','redacted') AND export_request_id=ticket_id
              AND task_id IS NOT NULL AND execution_plan_id IS NOT NULL
              AND snapshot_hash ~ '^[0-9a-f]{64}$' AND policy_version IS NOT NULL
              AND (result<>'allowed' OR (
                original_checksum ~ '^sha256:[0-9a-f]{64}$'
                AND derived_checksum ~ '^sha256:[0-9a-f]{64}$'
                AND archive_checksum ~ '^sha256:[0-9a-f]{64}$')), false))
    """)
    op.execute("""
        CREATE INDEX idx_qc_evidence_export_request
          ON qc_evidence_download_audit(export_request_id)
          WHERE export_request_id IS NOT NULL
    """)
    op.execute("""
        COMMENT ON COLUMN qc_evidence_download_audit.export_mode IS
          'original 原件；redacted 保守遮盖派生副本，仅用于追溯'
    """)
    op.execute("""
        COMMENT ON COLUMN qc_evidence_download_audit.denied_reason IS
          '拒绝或失败的固定原因码，不保存存储路径或异常原文'
    """)


def downgrade():
    op.execute("""
        DO $$ BEGIN
          IF EXISTS (SELECT 1 FROM qc_evidence_download_audit
                     WHERE export_request_id IS NOT NULL OR result='failed') THEN
            RAISE EXCEPTION 'cannot remove evidence export audit while export rows exist';
          END IF;
        END $$
    """)
    op.execute("DROP INDEX idx_qc_evidence_export_request")
    op.execute("""
        ALTER TABLE qc_evidence_download_audit
          DROP CONSTRAINT ck_qc_evidence_export_metadata,
          DROP CONSTRAINT ck_qc_evidence_download_result,
          DROP CONSTRAINT ck_qc_evidence_download_denied_reason,
          DROP COLUMN export_request_id, DROP COLUMN task_id,
          DROP COLUMN execution_plan_id, DROP COLUMN snapshot_hash, DROP COLUMN export_mode,
          DROP COLUMN policy_version, DROP COLUMN original_checksum, DROP COLUMN derived_checksum,
          DROP COLUMN archive_checksum, DROP COLUMN failed_artifact_id,
          ADD CONSTRAINT ck_qc_evidence_download_result CHECK (result IN ('allowed','denied')),
          ADD CONSTRAINT ck_qc_evidence_download_denied_reason CHECK (
            (result='denied' AND denied_reason IS NOT NULL)
            OR (result='allowed' AND denied_reason IS NULL))
    """)
