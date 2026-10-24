"""允许五类 Web Coverage 保存 v3 适用性快照。

Revision ID: 20260914_0115
Revises: 20260914_0114
"""

from alembic import op

revision = "20260914_0115"
down_revision = "20260914_0114"
branch_labels = None
depends_on = None


def _replace_applicability_constraint(*, allow_v3: bool) -> None:
    protocols = (
        "IN ('web-coverage-snapshot.v2', 'web-coverage-snapshot.v3')"
        if allow_v3
        else "= 'web-coverage-snapshot.v2'"
    )
    op.execute(
        f"""
        ALTER TABLE qc_web_coverage_snapshot
        DROP CONSTRAINT IF EXISTS ck_qc_web_coverage_applicability_snapshot,
        ADD CONSTRAINT ck_qc_web_coverage_applicability_snapshot
        CHECK (
            applicability_snapshot IS NULL
            OR (
                jsonb_typeof(applicability_snapshot) = 'object'
                AND jsonb_typeof(
                    applicability_snapshot->'required_page_types'
                ) = 'array'
                AND jsonb_typeof(
                    applicability_snapshot->'optional_page_types'
                ) = 'array'
                AND jsonb_typeof(
                    applicability_snapshot->'not_applicable_page_types'
                ) = 'array'
                AND NULLIF(
                    BTRIM(applicability_snapshot->>'applicability_mode'),
                    ''
                ) IS NOT NULL
                AND NULLIF(
                    BTRIM(applicability_snapshot->>'catalog_version'),
                    ''
                ) IS NOT NULL
                AND NULLIF(
                    BTRIM(applicability_snapshot->>'catalog_hash'),
                    ''
                ) IS NOT NULL
                AND applicability_snapshot->>'snapshot_protocol_version'
                    {protocols}
            )
        )
        """
    )


def upgrade() -> None:
    """历史 v2 快照保持有效，新五类运行允许冻结 v3 适用性。"""

    _replace_applicability_constraint(allow_v3=True)


def downgrade() -> None:
    """存在 v3 适用性事实时拒绝降回仅 v2 的约束。"""

    op.execute(
        """
        DO $$ BEGIN
          IF EXISTS (
            SELECT 1 FROM qc_web_coverage_snapshot
            WHERE applicability_snapshot->>'snapshot_protocol_version'
              = 'web-coverage-snapshot.v3'
          ) THEN
            RAISE EXCEPTION 'Cannot downgrade immutable Web applicability v3 evidence';
          END IF;
        END $$
        """
    )
    _replace_applicability_constraint(allow_v3=False)
