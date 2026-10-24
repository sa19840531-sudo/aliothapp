"""为任务收口恢复增加持久化轮转与退避字段。

Revision ID: 20260901_0083
Revises: 20260901_0082
Create Date: 2026-09-01
"""

from __future__ import annotations

from alembic import op

revision = "20260901_0083"
down_revision = "20260901_0082"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE qc_task
        ADD COLUMN finalization_retry_at timestamptz,
        ADD COLUMN finalization_retry_count integer NOT NULL DEFAULT 0,
        ADD COLUMN finalization_last_error text,
        ADD CONSTRAINT ck_qc_task_finalization_retry_count CHECK (
            finalization_retry_count >= 0
        )
        """
    )
    op.execute(
        """
        CREATE INDEX idx_qc_task_finalization_retry_due
        ON qc_task (finalization_retry_at NULLS FIRST, updated_at, id)
        WHERE finalization_state IN (
            'open', 'exploration_draining', 'session_draining', 'sealed'
        )
        """
    )
    op.execute(
        """
        COMMENT ON COLUMN qc_task.finalization_retry_at IS
        'Completion Gate 或正式报告绑定允许再次恢复的最早时间'
        """
    )
    op.execute(
        """
        COMMENT ON COLUMN qc_task.finalization_retry_count IS
        '当前 finalization epoch 内已被恢复器领取的次数'
        """
    )
    op.execute(
        """
        COMMENT ON COLUMN qc_task.finalization_last_error IS
        '最近一次收口恢复异常摘要；正常阻塞不写入异常'
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_qc_task_finalization_retry_due")
    op.execute(
        """
        ALTER TABLE qc_task
        DROP CONSTRAINT IF EXISTS ck_qc_task_finalization_retry_count,
        DROP COLUMN IF EXISTS finalization_last_error,
        DROP COLUMN IF EXISTS finalization_retry_count,
        DROP COLUMN IF EXISTS finalization_retry_at
        """
    )
