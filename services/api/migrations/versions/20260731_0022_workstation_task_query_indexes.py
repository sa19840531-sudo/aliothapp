"""为工作台任务列表的分页查询补充组合索引。

Revision ID: 20260731_0022
Revises: 20260727_0021
Create Date: 2026-07-31
"""

from __future__ import annotations

from alembic import op

revision = "20260731_0022"
down_revision = "20260727_0021"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE INDEX idx_qc_agent_run_task_created
        ON qc_agent_run (task_id, created_at DESC)
        """
    )
    op.execute(
        """
        CREATE INDEX idx_qc_rule_execution_task_run
        ON qc_rule_execution (task_id, agent_run_id)
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX idx_qc_rule_execution_task_run")
    op.execute("DROP INDEX idx_qc_agent_run_task_created")
