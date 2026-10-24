"""允许 Web V2 在共享覆盖快照表中保存独立权威来源。

Revision ID: 20260901_0082
Revises: 20260831_0081
Create Date: 2026-09-01
"""

from __future__ import annotations

from alembic import op

revision = "20260901_0082"
down_revision = "20260831_0081"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE qc_h5_coverage_snapshot
        DROP CONSTRAINT ck_qc_h5_coverage_authority,
        ADD CONSTRAINT ck_qc_h5_coverage_authority CHECK (
            authority_source IN (
                'h5_page_type_assessment',
                'web_page_type_assessment'
            )
        )
        """
    )
    op.execute(
        """
        COMMENT ON TABLE qc_h5_coverage_snapshot IS
        '存储 H5 与 Web 页面类型评估产生的版本化覆盖快照'
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1
                FROM qc_h5_coverage_snapshot
                WHERE authority_source = 'web_page_type_assessment'
            ) THEN
                RAISE EXCEPTION
                    'qc_h5_coverage_snapshot contains Web rows; refusing destructive downgrade';
            END IF;
        END
        $$
        """
    )
    op.execute(
        """
        ALTER TABLE qc_h5_coverage_snapshot
        DROP CONSTRAINT ck_qc_h5_coverage_authority,
        ADD CONSTRAINT ck_qc_h5_coverage_authority CHECK (
            authority_source = 'h5_page_type_assessment'
        )
        """
    )
    op.execute(
        """
        COMMENT ON TABLE qc_h5_coverage_snapshot IS
        'H5 页面类型评估产生的版本化覆盖快照'
        """
    )
