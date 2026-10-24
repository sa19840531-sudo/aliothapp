"""为 Web V2 正式报告恢复增加带 fencing 的原子租约。"""

from __future__ import annotations

from alembic import op

revision = "20260906_0093"
down_revision = "20260906_0092"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE qc_task
        ADD COLUMN finalization_recovery_lease_token uuid,
        ADD COLUMN finalization_recovery_lease_owner varchar(160),
        ADD COLUMN finalization_recovery_lease_expires_at timestamptz,
        ADD CONSTRAINT ck_qc_task_finalization_recovery_lease CHECK (
            (finalization_recovery_lease_token IS NULL
             AND finalization_recovery_lease_owner IS NULL
             AND finalization_recovery_lease_expires_at IS NULL)
            OR
            (finalization_recovery_lease_token IS NOT NULL
             AND NULLIF(BTRIM(finalization_recovery_lease_owner), '') IS NOT NULL
             AND finalization_recovery_lease_expires_at IS NOT NULL)
        )
        """
    )
    op.execute(
        """
        CREATE INDEX idx_qc_task_web_finalization_recovery_lease
        ON qc_task (finalization_retry_at, finalization_recovery_lease_expires_at)
        WHERE target_platform = 'web'
          AND finalization_recovery_state = 'active'
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_qc_task_web_finalization_recovery_lease")
    op.execute(
        """
        ALTER TABLE qc_task
        DROP CONSTRAINT IF EXISTS ck_qc_task_finalization_recovery_lease,
        DROP COLUMN IF EXISTS finalization_recovery_lease_expires_at,
        DROP COLUMN IF EXISTS finalization_recovery_lease_owner,
        DROP COLUMN IF EXISTS finalization_recovery_lease_token
        """
    )
