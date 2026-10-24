"""增加 App 探索 Evidence Index 及封存不可变门禁。

Revision ID: 20260811_0046
Revises: 20260811_0045
Create Date: 2026-08-11
"""

from __future__ import annotations

from alembic import op

revision = "20260811_0046"
down_revision = "20260811_0045"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE qc_app_evidence_index (
            index_id varchar(96) PRIMARY KEY,
            task_id uuid NOT NULL REFERENCES qc_task(id) ON DELETE CASCADE,
            agent_run_id uuid NOT NULL
                REFERENCES qc_agent_run(id) ON DELETE RESTRICT,
            build_id varchar(255) NOT NULL,
            surface_session_id varchar(128) NOT NULL,
            protocol_version varchar(64) NOT NULL,
            status varchar(32) NOT NULL DEFAULT 'building',
            outcome varchar(64),
            terminal_reason_code varchar(128),
            content_sha256 varchar(128),
            evidence_gaps jsonb NOT NULL DEFAULT '[]'::jsonb,
            sealed_at timestamptz,
            created_at timestamptz NOT NULL,
            updated_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT uq_qc_app_evidence_index_run UNIQUE (agent_run_id),
            CONSTRAINT fk_qc_app_evidence_index_session FOREIGN KEY (
                task_id, surface_session_id
            ) REFERENCES qc_surface_session (task_id, session_id)
                ON DELETE RESTRICT,
            CONSTRAINT ck_qc_app_evidence_index_status CHECK (
                status IN ('building', 'sealed')
            ),
            CONSTRAINT ck_qc_app_evidence_index_outcome CHECK (
                outcome IS NULL OR outcome IN (
                    'exploration_completed', 'evidence_insufficient'
                )
            ),
            CONSTRAINT ck_qc_app_evidence_index_gaps CHECK (
                jsonb_typeof(evidence_gaps) = 'array'
            ),
            CONSTRAINT ck_qc_app_evidence_index_sealed CHECK (
                (
                    status = 'building'
                    AND outcome IS NULL
                    AND terminal_reason_code IS NULL
                    AND content_sha256 IS NULL
                    AND sealed_at IS NULL
                )
                OR (
                    status = 'sealed'
                    AND outcome IS NOT NULL
                    AND terminal_reason_code IS NOT NULL
                    AND content_sha256 IS NOT NULL
                    AND sealed_at IS NOT NULL
                )
            )
        )
        """
    )
    op.execute(
        """
        CREATE TABLE qc_app_evidence_index_entry (
            entry_id bigserial PRIMARY KEY,
            index_id varchar(96) NOT NULL
                REFERENCES qc_app_evidence_index(index_id) ON DELETE CASCADE,
            task_id uuid NOT NULL REFERENCES qc_task(id) ON DELETE CASCADE,
            sequence_no integer NOT NULL,
            capture_id uuid NOT NULL
                REFERENCES qc_capture(id) ON DELETE RESTRICT,
            evidence_manifest_id varchar(96) NOT NULL
                REFERENCES qc_evidence_manifest(manifest_id) ON DELETE RESTRICT,
            evidence_manifest_version integer NOT NULL,
            semantic_role varchar(32) NOT NULL,
            surface_phase varchar(128),
            exploration_work_id varchar(128),
            action_execution_id varchar(96)
                REFERENCES qc_action_execution(action_execution_id)
                ON DELETE RESTRICT,
            candidate_id varchar(128),
            page_identity varchar(255),
            captured_at timestamptz NOT NULL,
            metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
            created_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT uq_qc_app_evidence_index_entry_sequence
                UNIQUE (index_id, sequence_no),
            CONSTRAINT uq_qc_app_evidence_index_entry_reference UNIQUE (
                index_id, capture_id, evidence_manifest_id
            ),
            CONSTRAINT ck_qc_app_evidence_index_entry_sequence CHECK (
                sequence_no > 0 AND evidence_manifest_version > 0
            ),
            CONSTRAINT ck_qc_app_evidence_index_entry_role CHECK (
                semantic_role IN (
                    'bootstrap', 'surface_before', 'surface_after',
                    'surface_reobserve', 'action_before', 'action_after',
                    'final_observation'
                )
            ),
            CONSTRAINT ck_qc_app_evidence_index_entry_metadata CHECK (
                jsonb_typeof(metadata) = 'object'
            )
        )
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION qc_guard_app_evidence_index_entry()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        DECLARE
            index_record qc_app_evidence_index%ROWTYPE;
            capture_task_id uuid;
            manifest_task_id uuid;
            manifest_capture_id uuid;
            manifest_version integer;
            manifest_quality_status varchar(32);
        BEGIN
            IF TG_OP = 'UPDATE' AND NEW.index_id <> OLD.index_id THEN
                RAISE EXCEPTION 'Evidence Index Entry 禁止移动到其他 Index';
            END IF;
            SELECT * INTO index_record
            FROM qc_app_evidence_index
            WHERE index_id = COALESCE(NEW.index_id, OLD.index_id)
            FOR SHARE;

            IF index_record.status = 'sealed' THEN
                RAISE EXCEPTION 'Evidence Index 已封存，禁止增删改 Entry';
            END IF;
            IF TG_OP = 'DELETE' THEN
                RETURN OLD;
            END IF;

            SELECT
                capture.task_id,
                manifest.task_id,
                manifest.capture_id,
                manifest.manifest_version,
                manifest.quality_status
            INTO
                capture_task_id,
                manifest_task_id,
                manifest_capture_id,
                manifest_version,
                manifest_quality_status
            FROM qc_capture AS capture
            INNER JOIN qc_evidence_manifest AS manifest
                ON manifest.manifest_id = NEW.evidence_manifest_id
            WHERE capture.id = NEW.capture_id;

            IF capture_task_id IS DISTINCT FROM index_record.task_id
               OR manifest_task_id IS DISTINCT FROM index_record.task_id
               OR manifest_capture_id IS DISTINCT FROM NEW.capture_id
               OR manifest_version IS DISTINCT FROM NEW.evidence_manifest_version THEN
                RAISE EXCEPTION 'Evidence Index Entry 的 Capture 或 Manifest 归属不一致';
            END IF;
            IF manifest_quality_status <> 'pass' THEN
                RAISE EXCEPTION 'Evidence Index Entry 只能引用 PASS Manifest';
            END IF;
            NEW.task_id := index_record.task_id;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_qc_app_evidence_index_entry_guard
        BEFORE INSERT OR UPDATE OR DELETE
        ON qc_app_evidence_index_entry
        FOR EACH ROW EXECUTE FUNCTION qc_guard_app_evidence_index_entry()
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION qc_guard_app_evidence_index_header()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF TG_OP = 'INSERT' THEN
                IF NOT EXISTS (
                    SELECT 1
                    FROM qc_agent_run AS run
                    WHERE run.id = NEW.agent_run_id
                      AND run.task_id = NEW.task_id
                ) THEN
                    RAISE EXCEPTION 'Evidence Index 的 Agent Run 与任务归属不一致';
                END IF;
                RETURN NEW;
            END IF;
            IF TG_OP = 'DELETE' AND OLD.status = 'sealed' THEN
                RAISE EXCEPTION 'Evidence Index 已封存，禁止删除';
            END IF;
            IF TG_OP = 'DELETE' THEN
                RETURN OLD;
            END IF;
            IF OLD.status = 'sealed' THEN
                IF NEW.index_id = OLD.index_id
                   AND NEW.task_id = OLD.task_id
                   AND NEW.agent_run_id = OLD.agent_run_id
                   AND NEW.build_id = OLD.build_id
                   AND NEW.surface_session_id = OLD.surface_session_id
                   AND NEW.protocol_version = OLD.protocol_version
                   AND NEW.status = OLD.status
                   AND NEW.outcome = OLD.outcome
                   AND NEW.terminal_reason_code = OLD.terminal_reason_code
                   AND NEW.content_sha256 = OLD.content_sha256
                   AND NEW.evidence_gaps = OLD.evidence_gaps
                   AND NEW.sealed_at = OLD.sealed_at
                   AND NEW.created_at = OLD.created_at THEN
                    RETURN OLD;
                END IF;
                RAISE EXCEPTION 'Evidence Index 已封存，禁止修改';
            END IF;
            IF NEW.status = 'sealed' THEN
                IF NEW.content_sha256 IS NULL OR NEW.sealed_at IS NULL THEN
                    RAISE EXCEPTION '封存必须提供 content_sha256 和 sealed_at';
                END IF;
                IF EXISTS (
                    SELECT 1
                    FROM qc_capture AS capture
                    LEFT JOIN LATERAL (
                        SELECT
                            manifest.quality_status
                        FROM qc_evidence_manifest AS manifest
                        WHERE manifest.capture_id = capture.id
                        ORDER BY manifest.manifest_version DESC
                        LIMIT 1
                    ) AS latest ON TRUE
                    WHERE capture.task_id = NEW.task_id
                      AND capture.surface_session_id = NEW.surface_session_id
                      AND capture.agent_run_id = NEW.agent_run_id
                      AND capture.capture_status = 'captured'
                      AND latest.quality_status IS DISTINCT FROM 'pass'
                ) THEN
                    RAISE EXCEPTION '存在尚未 PASS 的最新 Manifest，禁止封存';
                END IF;
            END IF;
            NEW.updated_at := now();
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_qc_app_evidence_index_header_guard
        BEFORE INSERT OR UPDATE OR DELETE ON qc_app_evidence_index
        FOR EACH ROW EXECUTE FUNCTION qc_guard_app_evidence_index_header()
        """
    )
    op.execute(
        """
        CREATE INDEX idx_qc_app_evidence_index_task_status
        ON qc_app_evidence_index (task_id, status, created_at)
        """
    )
    op.execute(
        """
        CREATE INDEX idx_qc_app_evidence_index_entry_capture
        ON qc_app_evidence_index_entry (capture_id, evidence_manifest_id)
        """
    )
    op.execute(
        """
        COMMENT ON TABLE qc_app_evidence_index IS
        '每个 App Exploration Run 唯一且封存后不可变的证据索引头'
        """
    )
    op.execute(
        """
        COMMENT ON TABLE qc_app_evidence_index_entry IS
        '按采集时间和语义角色排序的 PASS Evidence Manifest 引用'
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_qc_app_evidence_index_header_guard ON qc_app_evidence_index")
    op.execute("DROP FUNCTION IF EXISTS qc_guard_app_evidence_index_header()")
    op.execute(
        "DROP TRIGGER IF EXISTS trg_qc_app_evidence_index_entry_guard "
        "ON qc_app_evidence_index_entry"
    )
    op.execute("DROP FUNCTION IF EXISTS qc_guard_app_evidence_index_entry()")
    op.execute("DROP INDEX IF EXISTS idx_qc_app_evidence_index_entry_capture")
    op.execute("DROP INDEX IF EXISTS idx_qc_app_evidence_index_task_status")
    op.execute("DROP TABLE IF EXISTS qc_app_evidence_index_entry")
    op.execute("DROP TABLE IF EXISTS qc_app_evidence_index")
