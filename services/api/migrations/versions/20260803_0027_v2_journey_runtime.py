"""增加 V2 Journey 的可恢复执行游标与步骤审计。

Revision ID: 20260803_0027
Revises: 20260803_0026
Create Date: 2026-08-03
"""

from __future__ import annotations

from alembic import op

revision = "20260803_0027"
down_revision = "20260803_0026"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE qc_journey_execution (
            execution_id varchar(96) PRIMARY KEY,
            execution_plan_id varchar(96) NOT NULL
                REFERENCES qc_journey_execution_plan(execution_plan_id)
                ON DELETE RESTRICT,
            task_id uuid NOT NULL REFERENCES qc_task(id) ON DELETE CASCADE,
            session_id varchar(128) NOT NULL,
            agent_run_id uuid NOT NULL
                REFERENCES qc_agent_run(id) ON DELETE RESTRICT,
            surface_kind varchar(32) NOT NULL,
            execution_epoch integer NOT NULL,
            protocol_version varchar(64) NOT NULL,
            spec_hash varchar(128) NOT NULL,
            spec_payload jsonb NOT NULL,
            status varchar(32) NOT NULL DEFAULT 'running',
            next_step_index integer NOT NULL DEFAULT 0,
            current_step_attempt_count integer NOT NULL DEFAULT 0,
            current_capture_id uuid NOT NULL
                REFERENCES qc_page_capture(id) ON DELETE RESTRICT,
            gap_count integer NOT NULL DEFAULT 0,
            row_version bigint NOT NULL DEFAULT 0,
            deadline timestamptz NOT NULL,
            error_code varchar(128),
            error_message text,
            started_at timestamptz NOT NULL DEFAULT now(),
            finished_at timestamptz,
            updated_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT uq_qc_journey_execution_identity UNIQUE (
                task_id, execution_plan_id, session_id, execution_epoch
            ),
            CONSTRAINT ck_qc_journey_execution_surface CHECK (
                surface_kind IN ('web', 'android', 'ios')
            ),
            CONSTRAINT ck_qc_journey_execution_status CHECK (
                status IN (
                    'running', 'succeeded', 'completed_with_gaps',
                    'failed', 'cancelled'
                )
            ),
            CONSTRAINT ck_qc_journey_execution_numbers CHECK (
                execution_epoch >= 0
                AND next_step_index >= 0
                AND current_step_attempt_count >= 0
                AND gap_count >= 0
                AND row_version >= 0
            ),
            CONSTRAINT ck_qc_journey_execution_payload CHECK (
                jsonb_typeof(spec_payload) = 'object'
            ),
            CONSTRAINT ck_qc_journey_execution_error CHECK (
                (status = 'failed' AND error_code IS NOT NULL)
                OR (status <> 'failed' AND error_code IS NULL)
            ),
            CONSTRAINT ck_qc_journey_execution_finished CHECK (
                (status = 'running' AND finished_at IS NULL)
                OR (status <> 'running' AND finished_at IS NOT NULL)
            )
        )
        """
    )
    op.execute(
        """
        CREATE TABLE qc_journey_step_attempt (
            attempt_id varchar(96) PRIMARY KEY,
            execution_id varchar(96) NOT NULL
                REFERENCES qc_journey_execution(execution_id) ON DELETE CASCADE,
            task_id uuid NOT NULL REFERENCES qc_task(id) ON DELETE CASCADE,
            step_id varchar(128) NOT NULL,
            step_index integer NOT NULL,
            attempt_no integer NOT NULL,
            status varchar(32) NOT NULL,
            protocol_version varchar(64) NOT NULL,
            payload_hash varchar(128) NOT NULL,
            attempt_payload jsonb NOT NULL,
            source_capture_id uuid NOT NULL
                REFERENCES qc_page_capture(id) ON DELETE RESTRICT,
            result_capture_id uuid
                REFERENCES qc_page_capture(id) ON DELETE RESTRICT,
            action_execution_id varchar(96),
            error_code varchar(128),
            error_message text,
            retryable boolean NOT NULL DEFAULT false,
            started_at timestamptz NOT NULL,
            finished_at timestamptz NOT NULL,
            persisted_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT uq_qc_journey_step_attempt_order UNIQUE (
                execution_id, step_id, attempt_no
            ),
            CONSTRAINT ck_qc_journey_step_attempt_status CHECK (
                status IN ('succeeded', 'failed', 'skipped')
            ),
            CONSTRAINT ck_qc_journey_step_attempt_numbers CHECK (
                step_index >= 0 AND attempt_no > 0
            ),
            CONSTRAINT ck_qc_journey_step_attempt_payload CHECK (
                jsonb_typeof(attempt_payload) = 'object'
            ),
            CONSTRAINT ck_qc_journey_step_attempt_error CHECK (
                (status = 'failed' AND error_code IS NOT NULL)
                OR status <> 'failed'
            ),
            CONSTRAINT ck_qc_journey_step_attempt_time CHECK (
                finished_at >= started_at
            )
        )
        """
    )
    op.execute(
        """
        CREATE INDEX idx_qc_journey_execution_task_status
        ON qc_journey_execution (task_id, status, started_at)
        """
    )
    op.execute(
        """
        CREATE INDEX idx_qc_journey_step_attempt_execution
        ON qc_journey_step_attempt (
            execution_id, step_index, attempt_no
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS qc_journey_step_attempt")
    op.execute("DROP TABLE IF EXISTS qc_journey_execution")
