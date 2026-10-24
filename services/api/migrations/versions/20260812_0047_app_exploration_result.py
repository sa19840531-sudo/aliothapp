"""增加不可变 App 探索结果事实表。

Revision ID: 20260812_0047
Revises: 20260811_0046
Create Date: 2026-08-12
"""

from __future__ import annotations

from alembic import op

revision = "20260812_0047"
down_revision = "20260811_0046"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE qc_app_exploration_result (
            agent_run_id uuid PRIMARY KEY
                REFERENCES qc_agent_run(id) ON DELETE RESTRICT,
            task_id uuid NOT NULL REFERENCES qc_task(id) ON DELETE CASCADE,
            surface_session_id varchar(128) NOT NULL,
            evidence_index_id varchar(96) NOT NULL UNIQUE
                REFERENCES qc_app_evidence_index(index_id) ON DELETE RESTRICT,
            protocol_version varchar(64) NOT NULL,
            outcome varchar(64) NOT NULL,
            terminal_reason_code varchar(128) NOT NULL,
            evidence_index_hash varchar(128) NOT NULL,
            coverage_summary jsonb NOT NULL,
            evidence_gaps jsonb NOT NULL,
            started_at timestamptz NOT NULL,
            completed_at timestamptz NOT NULL,
            result_sha256 varchar(128) NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT fk_qc_app_exploration_result_session FOREIGN KEY (
                task_id, surface_session_id
            ) REFERENCES qc_surface_session (task_id, session_id)
                ON DELETE RESTRICT,
            CONSTRAINT ck_qc_app_exploration_result_outcome CHECK (
                outcome IN ('exploration_completed', 'evidence_insufficient')
            ),
            CONSTRAINT ck_qc_app_exploration_result_coverage CHECK (
                jsonb_typeof(coverage_summary) = 'object'
            ),
            CONSTRAINT ck_qc_app_exploration_result_gaps CHECK (
                jsonb_typeof(evidence_gaps) = 'array'
            ),
            CONSTRAINT ck_qc_app_exploration_result_time CHECK (
                completed_at >= started_at
            ),
            CONSTRAINT ck_qc_app_exploration_result_hash CHECK (
                result_sha256 ~ '^sha256:[0-9a-f]{64}$'
                AND evidence_index_hash ~ '^sha256:[0-9a-f]{64}$'
            )
        )
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION qc_guard_app_exploration_result()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        DECLARE
            index_record qc_app_evidence_index%ROWTYPE;
        BEGIN
            IF TG_OP = 'UPDATE' AND NEW IS NOT DISTINCT FROM OLD THEN
                RETURN OLD;
            END IF;
            IF TG_OP <> 'INSERT' THEN
                RAISE EXCEPTION 'App Exploration Result 已持久化，禁止修改或删除';
            END IF;
            IF NOT EXISTS (
                SELECT 1
                FROM qc_agent_run AS run
                WHERE run.id = NEW.agent_run_id
                  AND run.task_id = NEW.task_id
            ) THEN
                RAISE EXCEPTION 'App Exploration Result 的 Agent Run 与任务归属不一致';
            END IF;
            SELECT * INTO index_record
            FROM qc_app_evidence_index
            WHERE index_id = NEW.evidence_index_id
            FOR SHARE;
            IF index_record.status IS DISTINCT FROM 'sealed'
               OR index_record.task_id IS DISTINCT FROM NEW.task_id
               OR index_record.agent_run_id IS DISTINCT FROM NEW.agent_run_id
               OR index_record.surface_session_id IS DISTINCT FROM NEW.surface_session_id
               OR index_record.outcome IS DISTINCT FROM NEW.outcome
               OR index_record.terminal_reason_code IS DISTINCT FROM NEW.terminal_reason_code
               OR index_record.content_sha256 IS DISTINCT FROM NEW.evidence_index_hash
               OR index_record.evidence_gaps IS DISTINCT FROM NEW.evidence_gaps THEN
                RAISE EXCEPTION 'App Exploration Result 与 sealed Evidence Index 不一致';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_qc_app_exploration_result_guard
        BEFORE INSERT OR UPDATE OR DELETE ON qc_app_exploration_result
        FOR EACH ROW EXECUTE FUNCTION qc_guard_app_exploration_result()
        """
    )
    op.execute(
        """
        CREATE INDEX idx_qc_app_exploration_result_task_completed
        ON qc_app_exploration_result (task_id, completed_at DESC)
        """
    )
    op.execute(
        """
        COMMENT ON TABLE qc_app_exploration_result IS
        'APP_READY 后页面探索的不可变业务结果，必须绑定同一 Run 的 sealed Evidence Index'
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP INDEX IF EXISTS idx_qc_app_exploration_result_task_completed"
    )
    op.execute(
        "DROP TRIGGER IF EXISTS trg_qc_app_exploration_result_guard "
        "ON qc_app_exploration_result"
    )
    op.execute("DROP FUNCTION IF EXISTS qc_guard_app_exploration_result()")
    op.execute("DROP TABLE IF EXISTS qc_app_exploration_result")
