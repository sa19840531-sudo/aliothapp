"""记录通过完整绑定校验的 Web 正式报告，避免重复恢复扫描。"""

from __future__ import annotations

from alembic import op

revision = "20260906_0094"
down_revision = "20260906_0093"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE qc_task
        DROP CONSTRAINT ck_qc_task_finalization_error_streak,
        DROP CONSTRAINT ck_qc_task_finalization_recovery_state,
        ADD CONSTRAINT ck_qc_task_finalization_recovery_state CHECK (
            finalization_recovery_state IN ('active', 'quarantined', 'verified')
        ),
        ADD CONSTRAINT ck_qc_task_finalization_error_streak CHECK (
            finalization_same_error_count >= 0
            AND finalization_recovery_generation >= 0
            AND (
                finalization_recovery_state IN ('active', 'verified')
                OR (
                    finalization_error_signature IS NOT NULL
                    AND finalization_same_error_count >= 3
                    AND finalization_quarantined_at IS NOT NULL
                )
            )
        )
        """
    )


def downgrade() -> None:
    op.execute(
        """
        UPDATE qc_task
        SET finalization_recovery_state = 'active'
        WHERE finalization_recovery_state = 'verified'
        """
    )
    op.execute(
        """
        ALTER TABLE qc_task
        DROP CONSTRAINT ck_qc_task_finalization_error_streak,
        DROP CONSTRAINT ck_qc_task_finalization_recovery_state,
        ADD CONSTRAINT ck_qc_task_finalization_recovery_state CHECK (
            finalization_recovery_state IN ('active', 'quarantined')
        ),
        ADD CONSTRAINT ck_qc_task_finalization_error_streak CHECK (
            finalization_same_error_count >= 0
            AND finalization_recovery_generation >= 0
            AND (
                finalization_recovery_state = 'active'
                OR (
                    finalization_error_signature IS NOT NULL
                    AND finalization_same_error_count >= 3
                    AND finalization_quarantined_at IS NOT NULL
                )
            )
        )
        """
    )
