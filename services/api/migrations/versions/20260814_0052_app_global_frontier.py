"""增加 App 任务级全局 Frontier 事实表。

Revision ID: 20260814_0052
Revises: 20260814_0051
Create Date: 2026-08-13
"""

from __future__ import annotations

from alembic import op

revision = "20260814_0052"
down_revision = "20260814_0051"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE qc_app_exploration_frontier (
            frontier_item_id varchar(96) PRIMARY KEY,
            task_id uuid NOT NULL REFERENCES qc_task(id) ON DELETE CASCADE,
            agent_run_id uuid NOT NULL
                REFERENCES qc_agent_run(id) ON DELETE CASCADE,
            surface_session_id varchar(128) NOT NULL,
            source_work_id varchar(96) NOT NULL
                REFERENCES qc_v2_work_item(work_id) ON DELETE RESTRICT,
            source_capture_id uuid NOT NULL
                REFERENCES qc_capture(id) ON DELETE RESTRICT,
            source_page_identity text NOT NULL,
            candidate_fingerprint varchar(128) NOT NULL,
            candidate_id varchar(128) NOT NULL,
            semantic_target text NOT NULL,
            action_type varchar(32) NOT NULL,
            risk_level varchar(16) NOT NULL,
            navigation_path jsonb NOT NULL DEFAULT '[]'::jsonb,
            priority integer NOT NULL DEFAULT 100,
            protocol_version varchar(64) NOT NULL,
            payload_hash varchar(128) NOT NULL,
            candidate_payload jsonb NOT NULL,
            status varchar(32) NOT NULL DEFAULT 'pending',
            attempt_count integer NOT NULL DEFAULT 0,
            max_attempts integer NOT NULL DEFAULT 3,
            next_attempt_at timestamptz NOT NULL DEFAULT now(),
            lease_owner varchar(255),
            lease_token uuid,
            lease_expires_at timestamptz,
            row_version bigint NOT NULL DEFAULT 0,
            result_work_id varchar(96)
                REFERENCES qc_v2_work_item(work_id) ON DELETE SET NULL,
            result_capture_id uuid
                REFERENCES qc_capture(id) ON DELETE SET NULL,
            last_error_code varchar(128),
            last_error_message text,
            discovered_at timestamptz NOT NULL,
            started_at timestamptz,
            finished_at timestamptz,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT uq_qc_app_frontier_run_fingerprint UNIQUE (
                agent_run_id, candidate_fingerprint
            ),
            CONSTRAINT fk_qc_app_frontier_session FOREIGN KEY (
                task_id, surface_session_id
            ) REFERENCES qc_surface_session (task_id, session_id)
                ON DELETE RESTRICT,
            CONSTRAINT ck_qc_app_frontier_status CHECK (
                status IN (
                    'pending', 'leased', 'running', 'retry_wait',
                    'visited', 'blocked', 'failed', 'cancelled', 'superseded'
                )
            ),
            CONSTRAINT ck_qc_app_frontier_action CHECK (
                action_type IN ('click', 'scroll', 'back', 'observe')
            ),
            CONSTRAINT ck_qc_app_frontier_risk CHECK (
                risk_level IN ('low', 'medium')
            ),
            CONSTRAINT ck_qc_app_frontier_json CHECK (
                jsonb_typeof(navigation_path) = 'array'
                AND jsonb_typeof(candidate_payload) = 'object'
            ),
            CONSTRAINT ck_qc_app_frontier_numbers CHECK (
                priority >= 0
                AND attempt_count >= 0
                AND max_attempts > 0
                AND attempt_count <= max_attempts
                AND row_version >= 0
            ),
            CONSTRAINT ck_qc_app_frontier_lease CHECK (
                (
                    status IN ('leased', 'running')
                    AND lease_owner IS NOT NULL
                    AND lease_token IS NOT NULL
                    AND lease_expires_at IS NOT NULL
                )
                OR (
                    status NOT IN ('leased', 'running')
                    AND lease_owner IS NULL
                    AND lease_token IS NULL
                    AND lease_expires_at IS NULL
                )
            ),
            CONSTRAINT ck_qc_app_frontier_finished CHECK (
                (
                    status IN ('visited', 'blocked', 'failed', 'cancelled', 'superseded')
                    AND finished_at IS NOT NULL
                )
                OR (
                    status NOT IN ('visited', 'blocked', 'failed', 'cancelled', 'superseded')
                    AND finished_at IS NULL
                )
            )
        )
        """
    )
    op.execute(
        """
        CREATE INDEX idx_qc_app_frontier_claim
        ON qc_app_exploration_frontier (
            task_id, agent_run_id, surface_session_id,
            status, next_attempt_at, priority, discovered_at
        )
        """
    )
    op.execute(
        """
        CREATE INDEX idx_qc_app_frontier_expired_lease
        ON qc_app_exploration_frontier (lease_expires_at)
        WHERE status IN ('leased', 'running')
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION qc_guard_app_exploration_frontier()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1
                FROM qc_agent_run AS run
                INNER JOIN qc_surface_session AS session
                    ON session.task_id = run.task_id
                   AND session.session_id = NEW.surface_session_id
                INNER JOIN qc_v2_work_item AS work
                    ON work.work_id = NEW.source_work_id
                   AND work.task_id = run.task_id
                   AND work.session_id = session.session_id
                   AND work.work_type = 'exploration'
                INNER JOIN qc_capture AS capture
                    ON capture.id = NEW.source_capture_id
                   AND capture.task_id = run.task_id
                   AND capture.agent_run_id = run.id
                   AND capture.surface_session_id = session.session_id
                   AND capture.surface_kind = session.surface_kind
                   AND capture.capture_status = 'captured'
                WHERE run.id = NEW.agent_run_id
                  AND run.task_id = NEW.task_id
                  AND session.surface_kind IN ('android', 'ios')
            ) THEN
                RAISE EXCEPTION 'App Frontier 的 Run、Session、Work 或 Capture 归属不一致';
            END IF;
            IF NEW.protocol_version IS DISTINCT FROM 'app-frontier-item.v1'
               OR NEW.payload_hash !~ '^[0-9a-f]{64}$'
               OR NEW.payload_hash IS DISTINCT FROM encode(
                    digest(
                        convert_to(NEW.candidate_payload::text, 'UTF8'),
                        'sha256'
                    ),
                    'hex'
               )
               OR NEW.candidate_payload->>'protocol_version'
                    IS DISTINCT FROM NEW.protocol_version
               OR NEW.candidate_payload->>'frontier_item_id'
                    IS DISTINCT FROM NEW.frontier_item_id
               OR NEW.candidate_payload->>'task_id' IS DISTINCT FROM NEW.task_id::text
               OR NEW.candidate_payload->>'agent_run_id'
                    IS DISTINCT FROM NEW.agent_run_id::text
               OR NEW.candidate_payload->>'session_id'
                    IS DISTINCT FROM NEW.surface_session_id
               OR NEW.candidate_payload->>'source_work_id'
                    IS DISTINCT FROM NEW.source_work_id
               OR NEW.candidate_payload->>'source_capture_id'
                    IS DISTINCT FROM NEW.source_capture_id::text
               OR NEW.candidate_payload->>'source_page_identity'
                    IS DISTINCT FROM NEW.source_page_identity
               OR NEW.candidate_payload->>'candidate_fingerprint'
                    IS DISTINCT FROM NEW.candidate_fingerprint
               OR NEW.candidate_payload->>'surface_kind'
                    IS DISTINCT FROM (
                        SELECT session.surface_kind
                        FROM qc_surface_session AS session
                        WHERE session.task_id = NEW.task_id
                          AND session.session_id = NEW.surface_session_id
                    )
               OR NEW.candidate_payload->'candidate'->>'candidate_id'
                    IS DISTINCT FROM NEW.candidate_id
               OR NEW.candidate_payload->'candidate'->>'semantic_target'
                    IS DISTINCT FROM NEW.semantic_target
               OR NEW.candidate_payload->'candidate'->>'action_type'
                    IS DISTINCT FROM NEW.action_type
               OR NEW.candidate_payload->'candidate'->>'risk_level'
                    IS DISTINCT FROM NEW.risk_level
               OR NEW.candidate_payload->'navigation_path'
                    IS DISTINCT FROM NEW.navigation_path
               OR (CASE
                    WHEN COALESCE(NEW.candidate_payload->>'priority', '')
                        ~ '^[0-9]+$'
                    THEN (NEW.candidate_payload->>'priority')::integer
                        IS DISTINCT FROM NEW.priority
                    ELSE true
                  END)
               OR (CASE
                    WHEN NEW.candidate_payload ? 'discovered_at'
                    THEN (NEW.candidate_payload->>'discovered_at')::timestamptz
                        IS DISTINCT FROM NEW.discovered_at
                    ELSE true
                  END) THEN
                RAISE EXCEPTION 'App Frontier 候选 payload 与投影字段不一致';
            END IF;
            IF NEW.result_work_id IS NOT NULL AND NOT EXISTS (
                SELECT 1
                FROM qc_v2_work_item AS result_work
                INNER JOIN qc_agent_run AS run
                    ON run.id = NEW.agent_run_id
                   AND run.task_id = result_work.task_id
                WHERE result_work.work_id = NEW.result_work_id
                  AND result_work.task_id = NEW.task_id
                  AND result_work.session_id = NEW.surface_session_id
                  AND result_work.work_type = 'exploration'
            ) THEN
                RAISE EXCEPTION 'App Frontier result Work 归属不一致';
            END IF;
            IF NEW.result_capture_id IS NOT NULL AND NOT EXISTS (
                SELECT 1
                FROM qc_capture AS result_capture
                WHERE result_capture.id = NEW.result_capture_id
                  AND result_capture.task_id = NEW.task_id
                  AND result_capture.agent_run_id = NEW.agent_run_id
                  AND result_capture.surface_session_id = NEW.surface_session_id
                  AND result_capture.surface_kind IN ('android', 'ios')
                  AND result_capture.capture_status = 'captured'
            ) THEN
                RAISE EXCEPTION 'App Frontier result Capture 归属不一致';
            END IF;
            IF TG_OP = 'UPDATE' AND (
                NEW.frontier_item_id IS DISTINCT FROM OLD.frontier_item_id
                OR NEW.task_id IS DISTINCT FROM OLD.task_id
                OR NEW.agent_run_id IS DISTINCT FROM OLD.agent_run_id
                OR NEW.surface_session_id IS DISTINCT FROM OLD.surface_session_id
                OR NEW.source_work_id IS DISTINCT FROM OLD.source_work_id
                OR NEW.source_capture_id IS DISTINCT FROM OLD.source_capture_id
                OR NEW.source_page_identity IS DISTINCT FROM OLD.source_page_identity
                OR NEW.candidate_fingerprint IS DISTINCT FROM OLD.candidate_fingerprint
                OR NEW.candidate_id IS DISTINCT FROM OLD.candidate_id
                OR NEW.semantic_target IS DISTINCT FROM OLD.semantic_target
                OR NEW.action_type IS DISTINCT FROM OLD.action_type
                OR NEW.risk_level IS DISTINCT FROM OLD.risk_level
                OR NEW.navigation_path IS DISTINCT FROM OLD.navigation_path
                OR NEW.priority IS DISTINCT FROM OLD.priority
                OR NEW.protocol_version IS DISTINCT FROM OLD.protocol_version
                OR NEW.payload_hash IS DISTINCT FROM OLD.payload_hash
                OR NEW.candidate_payload IS DISTINCT FROM OLD.candidate_payload
                OR NEW.discovered_at IS DISTINCT FROM OLD.discovered_at
            ) THEN
                RAISE EXCEPTION 'App Frontier 候选事实禁止修改';
            END IF;
            IF TG_OP = 'UPDATE' AND NEW.status IS DISTINCT FROM OLD.status AND NOT (
                (OLD.status IN ('pending', 'retry_wait', 'leased', 'running')
                    AND NEW.status = 'cancelled')
                OR (OLD.status IN ('pending', 'retry_wait', 'leased', 'running')
                    AND NEW.status = 'failed')
                OR (OLD.status IN ('pending', 'retry_wait', 'leased', 'running')
                    AND NEW.status = 'superseded')
                OR (OLD.status IN ('pending', 'retry_wait', 'leased', 'running')
                    AND NEW.status = 'leased')
                OR (OLD.status = 'leased' AND NEW.status = 'running')
                OR (OLD.status = 'running' AND NEW.status IN (
                    'retry_wait', 'visited', 'blocked'
                ))
            ) THEN
                RAISE EXCEPTION 'App Frontier 状态迁移不受支持';
            END IF;
            IF TG_OP = 'UPDATE'
               AND NEW.row_version NOT IN (OLD.row_version, OLD.row_version + 1) THEN
                RAISE EXCEPTION 'App Frontier row_version 只能保持或单调增加 1';
            END IF;
            RETURN NEW;
        END;
        $$;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_qc_app_exploration_frontier_guard
        BEFORE INSERT OR UPDATE ON qc_app_exploration_frontier
        FOR EACH ROW EXECUTE FUNCTION qc_guard_app_exploration_frontier()
        """
    )
    op.execute(
        """
        COMMENT ON TABLE qc_app_exploration_frontier IS
        'App Run 内跨页面 sibling 候选的 PostgreSQL 事实库存'
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM qc_app_exploration_frontier) THEN
                RAISE EXCEPTION 'qc_app_exploration_frontier 已有事实，禁止静默降级';
            END IF;
        END;
        $$
        """
    )
    op.execute(
        "DROP TRIGGER IF EXISTS trg_qc_app_exploration_frontier_guard "
        "ON qc_app_exploration_frontier"
    )
    op.execute("DROP FUNCTION IF EXISTS qc_guard_app_exploration_frontier()")
    op.execute("DROP INDEX IF EXISTS idx_qc_app_frontier_expired_lease")
    op.execute("DROP INDEX IF EXISTS idx_qc_app_frontier_claim")
    op.execute("DROP TABLE IF EXISTS qc_app_exploration_frontier")
