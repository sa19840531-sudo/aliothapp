"""H5 现场交互模式支持页内栏目导航。

Revision ID: 20260924_0127
Revises: 20260924_0126
Create Date: 2026-09-24
"""

from __future__ import annotations

from alembic import op

revision = "20260924_0127"
down_revision = "20260924_0126"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE qc_h5_interaction_pattern
        DROP CONSTRAINT ck_qc_h5_interaction_pattern_kind
        """
    )
    op.execute(
        """
        ALTER TABLE qc_h5_interaction_pattern
        ADD CONSTRAINT ck_qc_h5_interaction_pattern_kind
        CHECK (pattern_kind IN ('bottom_navigation', 'page_tab_navigation'))
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DELETE FROM qc_h5_interaction_pattern
        WHERE pattern_kind = 'page_tab_navigation'
        """
    )
    op.execute(
        """
        ALTER TABLE qc_h5_interaction_pattern
        DROP CONSTRAINT ck_qc_h5_interaction_pattern_kind
        """
    )
    op.execute(
        """
        ALTER TABLE qc_h5_interaction_pattern
        ADD CONSTRAINT ck_qc_h5_interaction_pattern_kind
        CHECK (pattern_kind IN ('bottom_navigation'))
        """
    )
