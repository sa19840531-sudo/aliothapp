"""为任务收口持久化唯一 Agent Run 所有权。

Revision ID: 20260807_0041
Revises: 20260806_0040
Create Date: 2026-08-07
"""

from __future__ import annotations

from alembic import op

revision = "20260807_0041"
down_revision = "20260806_0040"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE qc_task
        ADD COLUMN finalization_agent_run_id uuid
            REFERENCES qc_agent_run(id) ON DELETE RESTRICT
        """
    )
    op.execute(
        """
        WITH recoverable_owner AS (
            SELECT DISTINCT ON (run.task_id)
                run.task_id,
                run.id AS agent_run_id
            FROM qc_agent_run AS run
            INNER JOIN qc_task AS task ON task.id = run.task_id
            WHERE run.status IN ('done', 'failed', 'aborted', 'canceled')
              AND NOT EXISTS (
                  SELECT 1
                  FROM qc_agent_run AS active_run
                  WHERE active_run.task_id = run.task_id
                    AND active_run.status IN ('pending', 'running')
              )
            ORDER BY
                run.task_id,
                run.finished_at DESC NULLS LAST,
                run.created_at DESC,
                run.id DESC
        )
        UPDATE qc_task AS task
        SET finalization_agent_run_id = owner.agent_run_id
        FROM recoverable_owner AS owner
        WHERE task.id = owner.task_id
          AND task.finalization_state IN (
              'open', 'exploration_draining', 'session_draining', 'sealed'
          )
                    AND (
                            task.finalization_state <> 'sealed'
                            OR NOT EXISTS (
                                    SELECT 1
                                    FROM qc_report_gate_binding AS binding
                                    WHERE binding.task_id = task.id
                                        AND binding.gate_snapshot_id = task.completion_gate_snapshot_id
                                        AND binding.finalization_epoch = task.finalization_epoch
                            )
                    )
        """
    )
    op.execute(
        """
        CREATE INDEX idx_qc_task_finalization_agent_run
        ON qc_task (finalization_agent_run_id)
        WHERE finalization_agent_run_id IS NOT NULL
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_qc_task_finalization_agent_run")
    op.execute("ALTER TABLE qc_task DROP COLUMN finalization_agent_run_id")