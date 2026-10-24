"""建立任务提交幂等事实与初始 Run 原子身份。

Revision ID: 20260818_0068
Revises: 20260818_0067
Create Date: 2026-08-18
"""

from __future__ import annotations

from alembic import op

revision = "20260818_0068"
down_revision = "20260818_0067"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """新增不可变 Submission Fact；历史任务无需回填伪幂等键。"""

    op.execute(
        """
        CREATE TABLE qc_task_submission (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            task_id uuid NOT NULL UNIQUE
                REFERENCES qc_task(id) ON DELETE CASCADE,
            initial_agent_run_id uuid NOT NULL UNIQUE,
            tenant_id uuid NOT NULL,
            submitter_id uuid NOT NULL,
            submission_kind varchar(64) NOT NULL,
            idempotency_key varchar(128) NOT NULL,
            request_hash varchar(64) NOT NULL,
            request_payload jsonb NOT NULL,
            committed_at timestamptz NOT NULL DEFAULT now(),
            created_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT fk_qc_task_submission_initial_run
                FOREIGN KEY (task_id, initial_agent_run_id)
                REFERENCES qc_agent_run(task_id, id)
                ON DELETE CASCADE DEFERRABLE INITIALLY DEFERRED,
            CONSTRAINT uq_qc_task_submission_idempotency
                UNIQUE (
                    tenant_id, submitter_id, submission_kind, idempotency_key
                ),
            CONSTRAINT ck_qc_task_submission_kind CHECK (
                submission_kind ~ '^[a-z][a-z0-9_.:-]{0,63}$'
            ),
            CONSTRAINT ck_qc_task_submission_key CHECK (
                idempotency_key ~ '^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$'
            ),
            CONSTRAINT ck_qc_task_submission_hash CHECK (
                request_hash ~ '^[0-9a-f]{64}$'
            ),
            CONSTRAINT ck_qc_task_submission_payload CHECK (
                request_payload->>'protocol_version' =
                    'task-submission-request.v1'
            )
        )
        """
    )
    op.execute(
        """
        CREATE INDEX idx_qc_task_submission_pending_projection
        ON qc_task_submission (committed_at, initial_agent_run_id, task_id)
        """
    )


def downgrade() -> None:
    """已有提交审计时拒绝静默丢失幂等身份。"""

    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM qc_task_submission) THEN
                RAISE EXCEPTION
                    'cannot downgrade task submission idempotency while facts exist';
            END IF;
        END;
        $$
        """
    )
    op.execute("DROP TABLE IF EXISTS qc_task_submission")
