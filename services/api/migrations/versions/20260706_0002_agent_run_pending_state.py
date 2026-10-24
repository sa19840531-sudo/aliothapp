"""补充 Agent Run pending 状态。

Revision ID: 20260706_0002
Revises: 20260706_0001
Create Date: 2026-07-06
"""

from __future__ import annotations

from alembic import op

revision = "20260706_0002"
down_revision = "20260706_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE qc_agent_run DROP CONSTRAINT ck_qc_agent_run_status")
    op.execute("ALTER TABLE qc_agent_run ALTER COLUMN started_at DROP NOT NULL")
    op.execute(
        """
        ALTER TABLE qc_agent_run
        ADD CONSTRAINT ck_qc_agent_run_status CHECK (
            status IN ('pending', 'running', 'done', 'failed', 'aborted', 'canceled')
        )
        """
    )


def downgrade() -> None:
    op.execute(
        """
        UPDATE qc_agent_run
        SET status = 'running',
            started_at = COALESCE(started_at, created_at)
        WHERE status = 'pending'
        """
    )
    op.execute("ALTER TABLE qc_agent_run DROP CONSTRAINT ck_qc_agent_run_status")
    op.execute("ALTER TABLE qc_agent_run ALTER COLUMN started_at SET NOT NULL")
    op.execute(
        """
        ALTER TABLE qc_agent_run
        ADD CONSTRAINT ck_qc_agent_run_status CHECK (
            status IN ('running', 'done', 'failed', 'aborted', 'canceled')
        )
        """
    )
