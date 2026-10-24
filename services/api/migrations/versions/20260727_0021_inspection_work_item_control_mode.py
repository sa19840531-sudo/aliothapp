"""区分影子工作项与正式调度工作项。

Revision ID: 20260727_0021
Revises: 20260727_0020
Create Date: 2026-07-27
"""

from __future__ import annotations

from alembic import op

revision = "20260727_0021"
down_revision = "20260727_0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE qc_inspection_work_item
        ADD COLUMN control_mode varchar(32) NOT NULL DEFAULT 'enforced'
        """
    )
    op.execute(
        """
        ALTER TABLE qc_inspection_work_item
        DROP CONSTRAINT uq_qc_inspection_work_item_identity
        """
    )
    op.execute(
        """
        ALTER TABLE qc_inspection_work_item
        ADD CONSTRAINT uq_qc_inspection_work_item_identity UNIQUE (
            task_id, page_capture_id, batch_key, input_hash, control_mode
        )
        """
    )
    op.execute(
        """
        ALTER TABLE qc_inspection_work_item
        ADD CONSTRAINT ck_qc_inspection_work_item_control_mode CHECK (
            control_mode IN ('shadow', 'enforced')
        )
        """
    )
    op.execute("DROP INDEX idx_qc_inspection_work_item_claim")
    op.execute(
        """
        CREATE INDEX idx_qc_inspection_work_item_claim
        ON qc_inspection_work_item (
            priority, next_attempt_at, created_at
        )
        WHERE control_mode = 'enforced'
          AND status IN ('pending', 'retry_wait')
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DELETE FROM qc_inspection_work_item AS shadow
        USING qc_inspection_work_item AS enforced
        WHERE shadow.control_mode = 'shadow'
          AND enforced.control_mode = 'enforced'
          AND shadow.task_id = enforced.task_id
          AND shadow.page_capture_id = enforced.page_capture_id
          AND shadow.batch_key = enforced.batch_key
          AND shadow.input_hash = enforced.input_hash
        """
    )
    op.execute(
        """
        UPDATE qc_inspection_work_item
        SET control_mode = 'enforced'
        WHERE control_mode = 'shadow'
        """
    )
    op.execute("DROP INDEX idx_qc_inspection_work_item_claim")
    op.execute(
        """
        ALTER TABLE qc_inspection_work_item
        DROP CONSTRAINT ck_qc_inspection_work_item_control_mode
        """
    )
    op.execute(
        """
        ALTER TABLE qc_inspection_work_item
        DROP CONSTRAINT uq_qc_inspection_work_item_identity
        """
    )
    op.execute(
        """
        ALTER TABLE qc_inspection_work_item
        DROP COLUMN control_mode
        """
    )
    op.execute(
        """
        ALTER TABLE qc_inspection_work_item
        ADD CONSTRAINT uq_qc_inspection_work_item_identity UNIQUE (
            task_id, page_capture_id, batch_key, input_hash
        )
        """
    )
    op.execute(
        """
        CREATE INDEX idx_qc_inspection_work_item_claim
        ON qc_inspection_work_item (
            priority, next_attempt_at, created_at
        )
        WHERE status IN ('pending', 'retry_wait')
        """
    )
