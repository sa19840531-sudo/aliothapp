"""扩展页面探索自由文本原因字段。

Revision ID: 20260717_0012
Revises: 20260717_0011
Create Date: 2026-07-17
"""

from __future__ import annotations

from alembic import op

revision = "20260717_0012"
down_revision = "20260717_0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE qc_page_exploration_action
            ALTER COLUMN failure_reason TYPE text
        """
    )
    op.execute(
        """
        ALTER TABLE qc_page_capture
            ALTER COLUMN inspection_abandon_reason TYPE text
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE qc_page_exploration_action
            ALTER COLUMN failure_reason TYPE varchar(128)
            USING left(failure_reason, 128)
        """
    )
    op.execute(
        """
        ALTER TABLE qc_page_capture
            ALTER COLUMN inspection_abandon_reason TYPE varchar(128)
            USING left(inspection_abandon_reason, 128)
        """
    )
