"""为 H5 门禁封存实际消费的 Coverage 身份，历史快照不推测回填。

Revision ID: 20260912_0110
Revises: 20260911_0109
Create Date: 2026-09-12
"""

from alembic import op

revision = "20260912_0110"
down_revision = "20260911_0109"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE qc_h5_coverage_snapshot
        ADD CONSTRAINT uq_qc_h5_coverage_task_identity
            UNIQUE (task_id, coverage_snapshot_id)
        """
    )
    op.execute(
        """
        ALTER TABLE qc_completion_gate_snapshot
        ADD COLUMN h5_coverage_snapshot_id varchar(160),
        ADD COLUMN h5_coverage_snapshot_hash varchar(128),
        ADD COLUMN h5_coverage_snapshot_epoch integer,
        ADD CONSTRAINT fk_qc_gate_h5_coverage
            FOREIGN KEY (task_id, h5_coverage_snapshot_id)
            REFERENCES qc_h5_coverage_snapshot(task_id, coverage_snapshot_id)
            DEFERRABLE INITIALLY DEFERRED,
        ADD CONSTRAINT ck_qc_gate_h5_coverage_identity CHECK (
            (h5_coverage_snapshot_id IS NULL
             AND h5_coverage_snapshot_hash IS NULL
             AND h5_coverage_snapshot_epoch IS NULL)
            OR
            (NULLIF(BTRIM(h5_coverage_snapshot_id), '') IS NOT NULL
             AND NULLIF(BTRIM(h5_coverage_snapshot_hash), '') IS NOT NULL
             AND h5_coverage_snapshot_epoch IS NOT NULL
             AND h5_coverage_snapshot_epoch >= 0
             AND h5_coverage_snapshot_epoch <= finalization_epoch)
        )
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DO $$ BEGIN
            IF EXISTS (
                SELECT 1 FROM qc_completion_gate_snapshot
                WHERE h5_coverage_snapshot_id IS NOT NULL
            ) THEN
                RAISE EXCEPTION 'H5 gate coverage bindings exist; refusing downgrade';
            END IF;
        END $$
        """
    )
    op.execute(
        """
        ALTER TABLE qc_completion_gate_snapshot
        DROP CONSTRAINT fk_qc_gate_h5_coverage,
        DROP CONSTRAINT ck_qc_gate_h5_coverage_identity,
        DROP COLUMN h5_coverage_snapshot_id,
        DROP COLUMN h5_coverage_snapshot_hash,
        DROP COLUMN h5_coverage_snapshot_epoch
        """
    )
    op.execute(
        """
        ALTER TABLE qc_h5_coverage_snapshot
        DROP CONSTRAINT uq_qc_h5_coverage_task_identity
        """
    )
