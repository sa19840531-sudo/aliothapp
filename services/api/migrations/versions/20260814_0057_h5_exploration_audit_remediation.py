"""持久化 H5 页面评估与证据库存事实。

Revision ID: 20260814_0057
Revises: 20260814_0056
Create Date: 2026-08-12
"""

from __future__ import annotations

from alembic import op

revision = "20260814_0057"
down_revision = "20260814_0056"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """建立 H5 审计事实，并将跨表身份归属收敛为数据库约束。"""
    op.execute(
        """
        CREATE UNIQUE INDEX uq_qc_agent_run_task_id_id
        ON qc_agent_run (task_id, id)
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX uq_qc_page_capture_task_id_id
        ON qc_page_capture (task_id, id)
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX uq_qc_capture_task_id_id
        ON qc_capture (task_id, id)
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX uq_qc_action_execution_task_session_id
        ON qc_action_execution (task_id, session_id, action_execution_id)
        """
    )
    op.execute(
        """
        CREATE TABLE qc_h5_page_type_assessment (
            assessment_record_id varchar(160) PRIMARY KEY,
            assessment_id varchar(160) NOT NULL,
            task_id uuid NOT NULL REFERENCES qc_task(id) ON DELETE CASCADE,
            agent_run_id uuid NOT NULL,
            session_id varchar(128) NOT NULL,
            page_capture_id uuid NOT NULL,
            detection_status varchar(64) NOT NULL,
            page_type text NOT NULL,
            page_state text,
            status text NOT NULL,
            reason_codes jsonb NOT NULL DEFAULT '[]'::jsonb,
            safety_summary jsonb NOT NULL DEFAULT '{}'::jsonb,
            assessment_payload jsonb NOT NULL,
            schema_version varchar(64) NOT NULL,
            payload_hash varchar(128) NOT NULL,
            row_version bigint NOT NULL DEFAULT 0,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT fk_qc_h5_assessment_session
                FOREIGN KEY (task_id, session_id)
                REFERENCES qc_surface_session(task_id, session_id)
                ON DELETE CASCADE,
            CONSTRAINT fk_qc_h5_assessment_agent_run
                FOREIGN KEY (task_id, agent_run_id)
                REFERENCES qc_agent_run(task_id, id)
                ON DELETE RESTRICT,
            CONSTRAINT fk_qc_h5_assessment_capture
                FOREIGN KEY (task_id, page_capture_id)
                REFERENCES qc_page_capture(task_id, id)
                ON DELETE RESTRICT,
            CONSTRAINT uq_qc_h5_assessment_capture_schema
                UNIQUE (page_capture_id, schema_version),
            CONSTRAINT ck_qc_h5_assessment_status CHECK (status IN (
                'covered', 'requires_auth', 'blocked_by_safety', 'not_found',
                'evidence_insufficient', 'not_applicable'
            )),
            CONSTRAINT ck_qc_h5_assessment_json CHECK (
                jsonb_typeof(reason_codes) = 'array'
                AND jsonb_typeof(safety_summary) = 'object'
                AND jsonb_typeof(assessment_payload) = 'object'
            ),
            CONSTRAINT ck_qc_h5_assessment_row_version CHECK (row_version >= 0)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE qc_h5_evidence_inventory_item (
            inventory_item_id varchar(160) PRIMARY KEY,
            task_id uuid NOT NULL REFERENCES qc_task(id) ON DELETE CASCADE,
            agent_run_id uuid NOT NULL,
            session_id varchar(128) NOT NULL,
            action_execution_id varchar(96) NOT NULL,
            before_capture_id uuid,
            after_capture_id uuid,
            page_type text,
            page_state text,
            status text NOT NULL,
            reason_codes jsonb NOT NULL DEFAULT '[]'::jsonb,
            required_artifacts jsonb NOT NULL DEFAULT '[]'::jsonb,
            found_artifacts jsonb NOT NULL DEFAULT '[]'::jsonb,
            verification_summary jsonb NOT NULL DEFAULT '{}'::jsonb,
            published_inspection_count integer NOT NULL DEFAULT 0,
            schema_version varchar(64) NOT NULL,
            payload_hash varchar(128) NOT NULL,
            row_version bigint NOT NULL DEFAULT 0,
            verified_at timestamptz,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT fk_qc_h5_inventory_session
                FOREIGN KEY (task_id, session_id)
                REFERENCES qc_surface_session(task_id, session_id)
                ON DELETE CASCADE,
            CONSTRAINT fk_qc_h5_inventory_agent_run
                FOREIGN KEY (task_id, agent_run_id)
                REFERENCES qc_agent_run(task_id, id)
                ON DELETE RESTRICT,
            CONSTRAINT fk_qc_h5_inventory_action
                FOREIGN KEY (task_id, session_id, action_execution_id)
                REFERENCES qc_action_execution(task_id, session_id, action_execution_id)
                ON DELETE RESTRICT,
            CONSTRAINT fk_qc_h5_inventory_before_capture
                FOREIGN KEY (task_id, before_capture_id)
                REFERENCES qc_capture(task_id, id)
                ON DELETE RESTRICT,
            CONSTRAINT fk_qc_h5_inventory_after_capture
                FOREIGN KEY (task_id, after_capture_id)
                REFERENCES qc_capture(task_id, id)
                ON DELETE RESTRICT,
            CONSTRAINT uq_qc_h5_inventory_action UNIQUE (action_execution_id),
            CONSTRAINT ck_qc_h5_inventory_status CHECK (
                status IN ('pass', 'insufficient', 'failed')
            ),
            CONSTRAINT ck_qc_h5_inventory_json CHECK (
                jsonb_typeof(reason_codes) = 'array'
                AND jsonb_typeof(required_artifacts) = 'array'
                AND jsonb_typeof(found_artifacts) = 'array'
                AND jsonb_typeof(verification_summary) = 'object'
            ),
            CONSTRAINT ck_qc_h5_inventory_counts CHECK (
                published_inspection_count >= 0 AND row_version >= 0
            )
        )
        """
    )
    op.execute(
        """
        CREATE INDEX idx_qc_h5_assessment_task_run_updated
        ON qc_h5_page_type_assessment (task_id, agent_run_id, updated_at DESC)
        """
    )
    op.execute(
        """
        CREATE INDEX idx_qc_h5_assessment_capture
        ON qc_h5_page_type_assessment (
            page_capture_id, schema_version, updated_at DESC
        )
        """
    )
    op.execute(
        """
        CREATE INDEX idx_qc_h5_inventory_task_run_status
        ON qc_h5_evidence_inventory_item (task_id, agent_run_id, status, updated_at DESC)
        """
    )
    op.execute(
        """
        CREATE INDEX idx_qc_h5_inventory_action
        ON qc_h5_evidence_inventory_item (action_execution_id, updated_at DESC)
        """
    )


def downgrade() -> None:
    """仅允许在没有新审计事实时回退，避免静默销毁证据。"""
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM qc_h5_page_type_assessment) THEN
                RAISE EXCEPTION
                    'qc_h5_page_type_assessment contains rows; refusing destructive downgrade';
            END IF;
            IF EXISTS (SELECT 1 FROM qc_h5_evidence_inventory_item) THEN
                RAISE EXCEPTION
                    'qc_h5_evidence_inventory_item contains rows; refusing destructive downgrade';
            END IF;
        END
        $$
        """
    )
    op.execute("DROP INDEX IF EXISTS idx_qc_h5_inventory_action")
    op.execute("DROP INDEX IF EXISTS idx_qc_h5_inventory_task_run_status")
    op.execute("DROP INDEX IF EXISTS idx_qc_h5_assessment_capture")
    op.execute("DROP INDEX IF EXISTS idx_qc_h5_assessment_task_run_updated")
    op.execute("DROP TABLE qc_h5_evidence_inventory_item")
    op.execute("DROP TABLE qc_h5_page_type_assessment")
    op.execute("DROP INDEX uq_qc_action_execution_task_session_id")
    op.execute("DROP INDEX uq_qc_capture_task_id_id")
    op.execute("DROP INDEX uq_qc_page_capture_task_id_id")
    op.execute("DROP INDEX uq_qc_agent_run_task_id_id")
