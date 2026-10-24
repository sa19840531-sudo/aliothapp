"""统一根任务状态版本与数据库迁移防线。

Revision ID: 20260805_0034
Revises: 20260804_0037
Create Date: 2026-08-05
"""

from __future__ import annotations

from alembic import op

revision = "20260805_0034"
down_revision = "20260804_0037"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE qc_task
        ADD COLUMN row_version bigint NOT NULL DEFAULT 0,
        ADD CONSTRAINT ck_qc_task_row_version CHECK (row_version >= 0)
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION qc_guard_task_status_transition()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF NEW.status IS NOT DISTINCT FROM OLD.status THEN
                NEW.row_version := OLD.row_version;
                RETURN NEW;
            END IF;

            IF OLD.finalization_state = 'sealed' THEN
                RAISE EXCEPTION '任务已经 sealed，拒绝状态写回'
                    USING ERRCODE = '40001';
            END IF;

            IF NOT (
                (OLD.status = 'queued' AND NEW.status IN ('running', 'failed', 'canceled'))
                OR (
                    OLD.status = 'running'
                    AND NEW.status IN ('review', 'done', 'failed', 'canceled')
                )
                OR (OLD.status = 'review' AND NEW.status IN ('done', 'failed'))
                OR (
                    OLD.status = 'done'
                    AND NEW.status = 'review'
                    AND OLD.finalization_state <> 'sealed'
                )
            ) THEN
                RAISE EXCEPTION '非法任务状态转换：% -> %', OLD.status, NEW.status
                    USING ERRCODE = '23514';
            END IF;

            NEW.row_version := OLD.row_version + 1;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_qc_task_status_transition
        BEFORE UPDATE OF status, row_version ON qc_task
        FOR EACH ROW
        EXECUTE FUNCTION qc_guard_task_status_transition()
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS trg_qc_task_status_transition ON qc_task"
    )
    op.execute("DROP FUNCTION IF EXISTS qc_guard_task_status_transition()")
    op.execute(
        """
        ALTER TABLE qc_task
        DROP CONSTRAINT IF EXISTS ck_qc_task_row_version,
        DROP COLUMN IF EXISTS row_version
        """
    )
