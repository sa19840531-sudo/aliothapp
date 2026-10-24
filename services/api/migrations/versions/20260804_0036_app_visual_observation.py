"""增加不可变 App Screenshot Understanding 视觉事实。

Revision ID: 20260804_0036
Revises: 20260804_0035
Create Date: 2026-08-04
"""

from __future__ import annotations

from alembic import op

revision = "20260804_0036"
down_revision = "20260804_0035"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE qc_app_visual_observation (
            observation_id uuid PRIMARY KEY,
            task_id uuid NOT NULL REFERENCES qc_task(id) ON DELETE CASCADE,
            session_id varchar(128) NOT NULL,
            capture_id uuid NOT NULL UNIQUE
                REFERENCES qc_app_capture(capture_id) ON DELETE CASCADE,
            screenshot_artifact_id uuid NOT NULL
                REFERENCES qc_evidence_artifact(id) ON DELETE RESTRICT,
            contract_version varchar(64) NOT NULL,
            provider_id varchar(128) NOT NULL,
            model_id varchar(128) NOT NULL,
            result_hash varchar(64) NOT NULL,
            visual_page_type varchar(128),
            visual_summary text NOT NULL,
            key_elements jsonb NOT NULL DEFAULT '[]'::jsonb,
            candidate_targets jsonb NOT NULL DEFAULT '[]'::jsonb,
            created_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT fk_qc_app_visual_observation_session
                FOREIGN KEY (task_id, session_id)
                REFERENCES qc_surface_session(task_id, session_id)
                ON DELETE CASCADE,
            CONSTRAINT ck_qc_app_visual_observation_result_hash CHECK (
                result_hash ~ '^[0-9a-f]{64}$'
            ),
            CONSTRAINT ck_qc_app_visual_observation_key_elements CHECK (
                jsonb_typeof(key_elements) = 'array'
            ),
            CONSTRAINT ck_qc_app_visual_observation_candidates CHECK (
                jsonb_typeof(candidate_targets) = 'array'
            )
        )
        """
    )
    op.execute(
        """
        CREATE INDEX idx_qc_app_visual_observation_task_session
        ON qc_app_visual_observation (task_id, session_id, created_at)
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM qc_app_visual_observation) THEN
                RAISE EXCEPTION '存在 App 视觉观察事实，不能回滚 Screenshot Understanding';
            END IF;
        END;
        $$
        """
    )
    op.execute("DROP INDEX IF EXISTS idx_qc_app_visual_observation_task_session")
    op.execute("DROP TABLE IF EXISTS qc_app_visual_observation")
