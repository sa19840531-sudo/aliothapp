"""为页面采集增加 Agent Run 归属。

Revision ID: 20260716_0008
Revises: 20260716_0007
Create Date: 2026-07-16
"""

from __future__ import annotations

from alembic import op

revision = "20260716_0008"
down_revision = "20260716_0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE qc_page_capture ADD COLUMN agent_run_id uuid")
    op.execute(
        """
        UPDATE qc_page_capture capture
        SET agent_run_id = run.id
        FROM qc_agent_run run
        WHERE capture.agent_run_id IS NULL
          AND capture.task_id = run.task_id
          AND capture.metadata->>'agent_run_id' = run.id::text
        """
    )
    op.execute(
        """
        ALTER TABLE qc_page_capture
        ADD CONSTRAINT fk_qc_page_capture_agent_run
        FOREIGN KEY (agent_run_id)
        REFERENCES qc_agent_run(id)
        ON DELETE SET NULL
        """
    )
    op.execute(
        """
        COMMENT ON COLUMN qc_page_capture.agent_run_id
        IS '产生该页面采集记录的 Agent Run ID'
        """
    )
    op.execute(
        """
        CREATE INDEX idx_qc_page_capture_agent_run
        ON qc_page_capture (agent_run_id, page_seq)
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_qc_page_capture_agent_run")
    op.execute(
        """
        ALTER TABLE qc_page_capture
        DROP CONSTRAINT IF EXISTS fk_qc_page_capture_agent_run
        """
    )
    op.execute("ALTER TABLE qc_page_capture DROP COLUMN IF EXISTS agent_run_id")
