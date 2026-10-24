"""扩展确定性规则引擎所需证据类型。

Revision ID: 20260720_0014
Revises: 20260720_0013
Create Date: 2026-07-20
"""

from __future__ import annotations

from alembic import op

revision = "20260720_0014"
down_revision = "20260720_0013"
branch_labels = None
depends_on = None

_BASE_TYPES = (
    "'screenshot', 'dom', 'visible_text', 'ocr_text', 'har', "
    "'console_log', 'media_state', 'ai_io', 'report'"
)
_RULE_ENGINE_TYPES = "'action_trace', 'ui_snapshot', 'temporal_trace'"


def upgrade() -> None:
    op.execute("ALTER TABLE qc_evidence_artifact DROP CONSTRAINT ck_qc_evidence_artifact_type")
    op.execute(
        f"""
        ALTER TABLE qc_evidence_artifact
        ADD CONSTRAINT ck_qc_evidence_artifact_type CHECK (
            artifact_type IN ({_BASE_TYPES}, {_RULE_ENGINE_TYPES})
        )
        """
    )


def downgrade() -> None:
    op.execute("ALTER TABLE qc_evidence_artifact DROP CONSTRAINT ck_qc_evidence_artifact_type")
    op.execute(
        f"""
        ALTER TABLE qc_evidence_artifact
        ADD CONSTRAINT ck_qc_evidence_artifact_type CHECK (
            artifact_type IN ({_BASE_TYPES})
        )
        """
    )
