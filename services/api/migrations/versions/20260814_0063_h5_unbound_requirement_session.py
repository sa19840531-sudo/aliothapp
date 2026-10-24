"""允许 H5 无会话采集写入证据需求集。

Revision ID: 20260814_0063
Revises: 20260814_0062
Create Date: 2026-08-14
"""

from __future__ import annotations

from alembic import op

revision = "20260814_0063"
down_revision = "20260814_0062"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE qc_evidence_exit_requirement_set
        DROP CONSTRAINT IF EXISTS fk_qc_evidence_exit_requirement_session
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1
                FROM qc_evidence_exit_requirement_set
                WHERE session_id LIKE 'agent-run:%'
            ) THEN
                RAISE EXCEPTION
                    '存在无 Surface Session 的 H5 证据需求集，不能恢复 Session 外键';
            END IF;
        END
        $$;
        """
    )
    op.execute(
        """
        ALTER TABLE qc_evidence_exit_requirement_set
        ADD CONSTRAINT fk_qc_evidence_exit_requirement_session
        FOREIGN KEY (task_id, session_id)
        REFERENCES qc_surface_session (task_id, session_id)
        ON DELETE CASCADE
        """
    )
