"""增加滚动覆盖证据和质检影子对比摘要表。

Revision ID: 20260721_0015
Revises: 20260720_0014
Create Date: 2026-07-21
"""

from __future__ import annotations

from alembic import op

revision = "20260721_0015"
down_revision = "20260720_0014"
branch_labels = None
depends_on = None

_BASE_TYPES = (
    "'screenshot', 'dom', 'visible_text', 'ocr_text', 'har', "
    "'console_log', 'media_state', 'ai_io', 'report'"
)
_RULE_ENGINE_TYPES = "'action_trace', 'ui_snapshot', 'temporal_trace'"


def upgrade() -> None:
    op.execute(
        "ALTER TABLE qc_evidence_artifact "
        "DROP CONSTRAINT ck_qc_evidence_artifact_type"
    )
    op.execute(
        f"""
        ALTER TABLE qc_evidence_artifact
        ADD CONSTRAINT ck_qc_evidence_artifact_type CHECK (
            artifact_type IN ({_BASE_TYPES}, {_RULE_ENGINE_TYPES}, 'page_coverage')
        )
        """
    )
    op.execute(
        """
        CREATE TABLE qc_pipeline_shadow_comparison (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            task_id uuid NOT NULL REFERENCES qc_task(id) ON DELETE CASCADE,
            agent_run_id uuid REFERENCES qc_agent_run(id) ON DELETE SET NULL,
            page_capture_id uuid REFERENCES qc_page_capture(id) ON DELETE SET NULL,
            rule_id uuid REFERENCES qc_rule(id) ON DELETE SET NULL,
            rule_code varchar(128),
            comparison_mode varchar(32) NOT NULL,
            legacy_conclusion varchar(32),
            candidate_conclusion varchar(32),
            difference_type varchar(64) NOT NULL,
            legacy_duration_ms integer,
            candidate_duration_ms integer,
            legacy_input_tokens integer,
            candidate_input_tokens integer,
            legacy_evidence_artifact_ids jsonb NOT NULL DEFAULT '[]'::jsonb,
            candidate_evidence_artifact_ids jsonb NOT NULL DEFAULT '[]'::jsonb,
            summary jsonb NOT NULL DEFAULT '{}'::jsonb,
            created_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT ck_qc_pipeline_shadow_mode CHECK (
                comparison_mode IN ('historical_replay', 'same_state_dual_capture')
            ),
            CONSTRAINT ck_qc_pipeline_shadow_difference CHECK (
                difference_type IN (
                    'match', 'conclusion_mismatch', 'evidence_mismatch',
                    'candidate_only', 'legacy_only', 'error'
                )
            ),
            CONSTRAINT ck_qc_pipeline_shadow_metrics CHECK (
                (legacy_duration_ms IS NULL OR legacy_duration_ms >= 0)
                AND (candidate_duration_ms IS NULL OR candidate_duration_ms >= 0)
                AND (legacy_input_tokens IS NULL OR legacy_input_tokens >= 0)
                AND (candidate_input_tokens IS NULL OR candidate_input_tokens >= 0)
            )
        )
        """
    )
    op.execute(
        "CREATE INDEX idx_qc_pipeline_shadow_task "
        "ON qc_pipeline_shadow_comparison (task_id, created_at)"
    )
    op.execute(
        "CREATE INDEX idx_qc_pipeline_shadow_rule "
        "ON qc_pipeline_shadow_comparison (rule_code, difference_type, created_at)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS qc_pipeline_shadow_comparison")
    op.execute(
        "ALTER TABLE qc_evidence_artifact "
        "DROP CONSTRAINT ck_qc_evidence_artifact_type"
    )
    op.execute(
        f"""
        ALTER TABLE qc_evidence_artifact
        ADD CONSTRAINT ck_qc_evidence_artifact_type CHECK (
            artifact_type IN ({_BASE_TYPES}, {_RULE_ENGINE_TYPES})
        )
        """
    )
