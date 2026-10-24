"""允许 Completion Gate 正式封存取消任务。

Revision ID: 20260805_0037
Revises: 20260805_0036
"""

from alembic import op

revision = "20260805_0037"
down_revision = "20260805_0036"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE qc_task
            DROP CONSTRAINT ck_qc_task_execution_status,
            DROP CONSTRAINT ck_qc_task_finalization_legacy_status
        """
    )
    op.execute(
        """
        ALTER TABLE qc_task
            ADD CONSTRAINT ck_qc_task_execution_status CHECK (
                execution_status IS NULL OR execution_status IN (
                    'completed', 'completed_with_gaps', 'needs_review',
                    'failed', 'canceled'
                )
            ),
            ADD CONSTRAINT ck_qc_task_finalization_legacy_status CHECK (
                finalization_state <> 'sealed'
                OR (execution_status IN ('completed', 'completed_with_gaps') AND status = 'done')
                OR (execution_status = 'needs_review' AND status = 'review')
                OR (execution_status = 'failed' AND status = 'failed')
                OR (execution_status = 'canceled' AND status = 'canceled')
            )
        """
    )
    op.execute(
        """
        ALTER TABLE qc_report_gate_binding
            DROP CONSTRAINT ck_qc_report_gate_execution,
            ADD CONSTRAINT ck_qc_report_gate_execution CHECK (
                execution_status IN (
                    'completed', 'completed_with_gaps', 'needs_review',
                    'failed', 'canceled'
                )
            )
        """
    )


def downgrade() -> None:
    op.execute(
        "DELETE FROM qc_report_gate_binding WHERE execution_status = 'canceled'"
    )
    op.execute(
        """
        UPDATE qc_task
        SET execution_status = 'failed', status = 'failed'
        WHERE execution_status = 'canceled'
        """
    )
    op.execute(
        """
        ALTER TABLE qc_report_gate_binding
            DROP CONSTRAINT ck_qc_report_gate_execution,
            ADD CONSTRAINT ck_qc_report_gate_execution CHECK (
                execution_status IN (
                    'completed', 'completed_with_gaps', 'needs_review', 'failed'
                )
            )
        """
    )
    op.execute(
        """
        ALTER TABLE qc_task
            DROP CONSTRAINT ck_qc_task_execution_status,
            DROP CONSTRAINT ck_qc_task_finalization_legacy_status,
            ADD CONSTRAINT ck_qc_task_execution_status CHECK (
                execution_status IS NULL OR execution_status IN (
                    'completed', 'completed_with_gaps', 'needs_review', 'failed'
                )
            ),
            ADD CONSTRAINT ck_qc_task_finalization_legacy_status CHECK (
                finalization_state <> 'sealed'
                OR (execution_status IN ('completed', 'completed_with_gaps') AND status = 'done')
                OR (execution_status = 'needs_review' AND status = 'review')
                OR (execution_status = 'failed' AND status = 'failed')
            )
        """
    )
