"""允许旁路证据采集写入页面生命周期审计。

Revision ID: 20260725_0019
Revises: 20260725_0018
Create Date: 2026-07-25
"""

from __future__ import annotations

from alembic import op

revision = "20260725_0019"
down_revision = "20260725_0018"
branch_labels = None
depends_on = None

_CONSTRAINT_NAME = "ck_qc_page_capture_inspection_lifecycle"


def upgrade() -> None:
    op.execute(
        f"""
        ALTER TABLE qc_page_capture
        DROP CONSTRAINT IF EXISTS {_CONSTRAINT_NAME}
        """
    )
    op.execute(
        f"""
        ALTER TABLE qc_page_capture
        ADD CONSTRAINT {_CONSTRAINT_NAME}
        CHECK (
            inspection_lifecycle_status IS NULL
            OR inspection_lifecycle_status IN (
                'active', 'superseded', 'abandoned', 'evidence_only'
            )
        )
        """
    )


def downgrade() -> None:
    op.execute(
        """
        UPDATE qc_page_capture
        SET inspection_lifecycle_status = 'abandoned',
            inspection_abandon_reason = COALESCE(
                inspection_abandon_reason,
                'evidence_only_downgrade'
            )
        WHERE inspection_lifecycle_status = 'evidence_only'
        """
    )
    op.execute(
        f"""
        ALTER TABLE qc_page_capture
        DROP CONSTRAINT IF EXISTS {_CONSTRAINT_NAME}
        """
    )
    op.execute(
        f"""
        ALTER TABLE qc_page_capture
        ADD CONSTRAINT {_CONSTRAINT_NAME}
        CHECK (
            inspection_lifecycle_status IS NULL
            OR inspection_lifecycle_status IN (
                'active', 'superseded', 'abandoned'
            )
        )
        """
    )
