"""统一人工终审与审核队列状态，并回填历史不一致数据。

Revision ID: 20260801_0025
Revises: 20260801_0024
Create Date: 2026-08-01
"""

from __future__ import annotations

from alembic import op

revision = "20260801_0025"
down_revision = "20260801_0024"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        WITH latest_review AS (
            SELECT DISTINCT ON (review.rule_execution_id)
                review.rule_execution_id,
                review.status,
                review.review_reason,
                review.reviewed_at,
                review.updated_at
            FROM qc_review_item AS review
            WHERE review.status IN ('approved', 'rejected', 'needs_recheck')
            ORDER BY
                review.rule_execution_id,
                review.reviewed_at DESC NULLS LAST,
                review.updated_at DESC,
                review.id DESC
        )
        UPDATE qc_rule_execution AS execution
        SET
            review_status = latest_review.status,
            review_reason = COALESCE(latest_review.review_reason, execution.review_reason),
            human_verdict = CASE latest_review.status
                WHEN 'approved' THEN COALESCE(execution.human_verdict, 'pass')
                WHEN 'rejected' THEN COALESCE(execution.human_verdict, 'fail')
                WHEN 'needs_recheck' THEN NULL
                ELSE execution.human_verdict
            END,
            human_verdict_note = CASE
                WHEN latest_review.status = 'needs_recheck' THEN NULL
                ELSE COALESCE(execution.human_verdict_note, latest_review.review_reason)
            END,
            human_verdict_at = CASE
                WHEN latest_review.status = 'needs_recheck' THEN NULL
                ELSE COALESCE(
                    execution.human_verdict_at,
                    latest_review.reviewed_at,
                    latest_review.updated_at
                )
            END,
            updated_at = now()
        FROM latest_review
        WHERE execution.id = latest_review.rule_execution_id
        """
    )
    op.execute(
        """
        WITH latest_run AS (
            SELECT DISTINCT ON (run.task_id)
                run.task_id,
                run.id AS agent_run_id,
                run.status AS run_status
            FROM qc_agent_run AS run
            ORDER BY run.task_id, run.created_at DESC, run.id DESC
        ),
        review_state AS (
            SELECT
                latest_run.task_id,
                latest_run.run_status,
                EXISTS (
                    SELECT 1
                    FROM qc_rule_execution AS execution
                    WHERE execution.task_id = latest_run.task_id
                      AND execution.agent_run_id = latest_run.agent_run_id
                      AND execution.human_verdict IS NULL
                      AND (
                          execution.execution_status IN (
                              'manual_review', 'evidence_insufficient', 'error'
                          )
                          OR execution.review_status IN ('pending', 'needs_recheck')
                      )
                ) AS has_pending_confirmation
            FROM latest_run
        )
        UPDATE qc_task AS task
        SET
            status = CASE
                WHEN review_state.has_pending_confirmation THEN 'review'
                ELSE 'done'
            END,
            finished_at = COALESCE(task.finished_at, now()),
            updated_at = now()
        FROM review_state
        WHERE task.id = review_state.task_id
          AND task.status IN ('review', 'done')
          AND review_state.run_status = 'done'
          AND task.status IS DISTINCT FROM CASE
              WHEN review_state.has_pending_confirmation THEN 'review'
              ELSE 'done'
          END
        """
    )


def downgrade() -> None:
    # 历史数据回填属于纠错操作，无法可靠恢复迁移前互相矛盾的多套状态。
    pass
