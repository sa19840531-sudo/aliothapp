"""冻结 Web 适用性，并为正式报告恢复治理预留单一前向迁移。"""

from __future__ import annotations

from alembic import op

revision = "20260906_0092"
down_revision = "20260905_0091"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """新增可空适用性快照；历史快照保持原值和原哈希不变。"""

    op.execute(
        """
        ALTER TABLE qc_web_coverage_snapshot
        ADD COLUMN applicability_snapshot jsonb,
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
                    = 'web-coverage-snapshot.v2'
            )
        )
        """
    )
    op.execute(
        """
        ALTER TABLE qc_task
        ADD COLUMN finalization_recovery_state varchar(24)
            NOT NULL DEFAULT 'active',
        ADD COLUMN finalization_error_signature varchar(64),
        ADD COLUMN finalization_same_error_count integer NOT NULL DEFAULT 0,
        ADD COLUMN finalization_quarantined_at timestamptz,
        ADD COLUMN finalization_recovery_generation integer NOT NULL DEFAULT 0,
        ADD COLUMN finalization_recovered_at timestamptz,
        ADD CONSTRAINT ck_qc_task_finalization_recovery_state CHECK (
            finalization_recovery_state IN ('active', 'quarantined')
        ),
        ADD CONSTRAINT ck_qc_task_finalization_error_streak CHECK (
            finalization_same_error_count >= 0
            AND finalization_recovery_generation >= 0
            AND (
                finalization_recovery_state = 'active'
                OR (
                    finalization_error_signature IS NOT NULL
                    AND finalization_same_error_count >= 3
                    AND finalization_quarantined_at IS NOT NULL
                )
            )
        )
        """
    )
    op.execute(
        """
        COMMENT ON COLUMN qc_task.finalization_recovery_state IS
        '正式报告收口恢复状态；相同错误达到阈值后进入隔离'
        """
    )


def downgrade() -> None:
    """仅移除新增字段；不改写任何既有覆盖快照。"""

    op.execute(
        """
        ALTER TABLE qc_web_coverage_snapshot
        DROP CONSTRAINT IF EXISTS ck_qc_web_coverage_applicability_snapshot,
        DROP COLUMN IF EXISTS applicability_snapshot
        """
    )
    op.execute(
        """
        ALTER TABLE qc_task
        DROP CONSTRAINT IF EXISTS ck_qc_task_finalization_error_streak,
        DROP CONSTRAINT IF EXISTS ck_qc_task_finalization_recovery_state,
        DROP COLUMN IF EXISTS finalization_quarantined_at,
        DROP COLUMN IF EXISTS finalization_same_error_count,
        DROP COLUMN IF EXISTS finalization_error_signature,
        DROP COLUMN IF EXISTS finalization_recovered_at,
        DROP COLUMN IF EXISTS finalization_recovery_generation,
        DROP COLUMN IF EXISTS finalization_recovery_state
        """
    )
