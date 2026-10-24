"""增加 App 探索技术失败审计事实。

Revision ID: 20260814_0059
Revises: 20260814_0058
Create Date: 2026-08-14
"""

from __future__ import annotations

from alembic import op

revision = "20260814_0059"
down_revision = "20260814_0058"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """建立技术失败事实，并保存可恢复 Result 的封存前上下文。"""
    op.execute(
        """
        CREATE TABLE qc_app_exploration_result_intent (
            agent_run_id uuid PRIMARY KEY,
            task_id uuid NOT NULL,
            surface_session_id varchar(128) NOT NULL,
            result_context_sha256 varchar(71) NOT NULL,
            coverage_summary jsonb NOT NULL,
            started_at timestamptz NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT fk_qc_app_result_intent_run
                FOREIGN KEY (task_id, agent_run_id)
                REFERENCES qc_agent_run(task_id, id)
                ON DELETE CASCADE,
            CONSTRAINT fk_qc_app_result_intent_session
                FOREIGN KEY (task_id, surface_session_id)
                REFERENCES qc_surface_session(task_id, session_id)
                ON DELETE CASCADE,
            CONSTRAINT ck_qc_app_result_intent_session CHECK (
                BTRIM(surface_session_id) <> ''
            ),
            CONSTRAINT ck_qc_app_result_intent_hash CHECK (
                result_context_sha256 ~ '^sha256:[0-9a-f]{64}$'
            ),
            CONSTRAINT ck_qc_app_result_intent_coverage CHECK (
                jsonb_typeof(coverage_summary) = 'object'
            )
        )
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION guard_qc_app_result_intent_immutable()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF NEW.agent_run_id IS DISTINCT FROM OLD.agent_run_id
               OR NEW.task_id IS DISTINCT FROM OLD.task_id
               OR NEW.surface_session_id IS DISTINCT FROM OLD.surface_session_id
               OR NEW.result_context_sha256 IS DISTINCT FROM OLD.result_context_sha256
               OR NEW.coverage_summary IS DISTINCT FROM OLD.coverage_summary
               OR NEW.started_at IS DISTINCT FROM OLD.started_at
               OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
                RAISE EXCEPTION 'App Result Intent 已写入，禁止修改';
            END IF;
            RETURN OLD;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_qc_app_result_intent_immutable
        BEFORE UPDATE ON qc_app_exploration_result_intent
        FOR EACH ROW EXECUTE FUNCTION guard_qc_app_result_intent_immutable()
        """
    )
    op.execute(
        """
        COMMENT ON TABLE qc_app_exploration_result_intent IS
        'Evidence Index 封存前的不可变 Result 恢复上下文；禁止恢复机猜测覆盖结果'
        """
    )
    op.execute(
        """
        CREATE TABLE qc_app_exploration_failure (
            failure_id uuid PRIMARY KEY,
            task_id uuid NOT NULL REFERENCES qc_task(id) ON DELETE CASCADE,
            agent_run_id uuid NOT NULL,
            surface_session_id varchar(128) NOT NULL,
            protocol_version varchar(64) NOT NULL,
            phase varchar(64) NOT NULL,
            reason_code varchar(160) NOT NULL,
            error_type varchar(160) NOT NULL,
            retryable boolean NOT NULL,
            observed_at timestamptz NOT NULL,
            failure_sha256 varchar(80) NOT NULL,
            metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
            recovery_state varchar(32) NOT NULL DEFAULT 'pending',
            sealed_index_id varchar(160),
            result_id uuid,
            session_status varchar(32),
            reconciled_at timestamptz,
            row_version bigint NOT NULL DEFAULT 0,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT fk_qc_app_failure_run
                FOREIGN KEY (task_id, agent_run_id)
                REFERENCES qc_agent_run(task_id, id)
                ON DELETE CASCADE,
            -- Bootstrap 在 Session 行创建前也可能失败；这里只保留计划中的
            -- surface_session_id，由恢复机对账真实 Session。
            CONSTRAINT fk_qc_app_failure_index
                FOREIGN KEY (sealed_index_id)
                REFERENCES qc_app_evidence_index(index_id)
                ON DELETE RESTRICT,
            CONSTRAINT fk_qc_app_failure_result
                FOREIGN KEY (result_id)
                REFERENCES qc_app_exploration_result(agent_run_id)
                ON DELETE RESTRICT,
            CONSTRAINT uq_qc_app_failure_run UNIQUE (agent_run_id),
            CONSTRAINT ck_qc_app_failure_text CHECK (
                BTRIM(surface_session_id) <> ''
                AND BTRIM(phase) <> ''
                AND BTRIM(reason_code) <> ''
                AND BTRIM(error_type) <> ''
            ),
            CONSTRAINT ck_qc_app_failure_metadata CHECK (
                jsonb_typeof(metadata) = 'object'
            ),
            CONSTRAINT ck_qc_app_failure_protocol CHECK (
                protocol_version = 'app-exploration-failure.v1'
            ),
            CONSTRAINT ck_qc_app_failure_hash CHECK (
                failure_sha256 ~ '^sha256:[0-9a-f]{64}$'
            ),
            CONSTRAINT ck_qc_app_failure_recovery CHECK (
                recovery_state IN (
                    'pending', 'evidence_sealed', 'no_evidence', 'reconciled'
                )
                AND (
                    (recovery_state = 'pending' AND reconciled_at IS NULL)
                    OR (recovery_state <> 'pending' AND reconciled_at IS NOT NULL)
                )
                AND (
                    recovery_state <> 'evidence_sealed'
                    OR sealed_index_id IS NOT NULL
                )
                AND (
                    recovery_state <> 'no_evidence'
                    OR (sealed_index_id IS NULL AND result_id IS NULL)
                )
            ),
            CONSTRAINT ck_qc_app_failure_row_version CHECK (row_version >= 0)
        )
        """
    )
    op.execute(
        """
        CREATE INDEX idx_qc_app_failure_recovery
        ON qc_app_exploration_failure (recovery_state, observed_at, failure_id)
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_qc_app_exploration_failure_updated_at
        BEFORE UPDATE ON qc_app_exploration_failure
        FOR EACH ROW EXECUTE FUNCTION set_updated_at()
        """
    )
    op.execute(
        """
        COMMENT ON TABLE qc_app_exploration_failure IS
        'App 探索技术失败事实；不得替代 exploration_completed/evidence_insufficient 业务结果'
        """
    )


def downgrade() -> None:
    """存在失败或 Result 恢复事实时拒绝破坏性回退。"""
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM qc_app_exploration_failure)
               OR EXISTS (SELECT 1 FROM qc_app_exploration_result_intent) THEN
                RAISE EXCEPTION
                    '存在 App 探索失败或 Result 恢复事实，禁止静默清理后降级 0058';
            END IF;
        END
        $$
        """
    )
    op.execute(
        """
        DROP TRIGGER IF EXISTS trg_qc_app_exploration_failure_updated_at
        ON qc_app_exploration_failure
        """
    )
    op.execute("DROP INDEX IF EXISTS idx_qc_app_failure_recovery")
    op.execute("DROP TABLE IF EXISTS qc_app_exploration_failure")
    op.execute(
        """
        DROP TRIGGER IF EXISTS trg_qc_app_result_intent_immutable
        ON qc_app_exploration_result_intent
        """
    )
    op.execute("DROP FUNCTION IF EXISTS guard_qc_app_result_intent_immutable()")
    op.execute("DROP TABLE IF EXISTS qc_app_exploration_result_intent")
