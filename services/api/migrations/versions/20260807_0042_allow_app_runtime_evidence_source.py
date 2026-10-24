"""允许 App Runtime 证据写入 qc_evidence_artifact.source。

Revision ID: 20260807_0042
Revises: 20260806_0041

App Capture / Action Capture 使用 source='app_runtime'，但建表约束未收录该枚举，
导致 Bootstrap 截图落库时 CheckViolation，任务卡在执行中。
"""

from alembic import op

revision = "20260807_0042"
down_revision = "20260806_0041"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE qc_evidence_artifact
        DROP CONSTRAINT IF EXISTS ck_qc_evidence_artifact_source
        """
    )
    op.execute(
        """
        ALTER TABLE qc_evidence_artifact
        ADD CONSTRAINT ck_qc_evidence_artifact_source CHECK (
            source IN (
                'baseline', 'rule_supplement', 'ai_call', 'report', 'app_runtime'
            )
        )
        """
    )
    op.execute(
        """
        COMMENT ON COLUMN qc_evidence_artifact.source IS
        '证据来源；app_runtime 表示 App 会话/动作运行时采集的基线类证据'
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DELETE FROM qc_evidence_artifact
        WHERE source = 'app_runtime'
        """
    )
    op.execute(
        """
        ALTER TABLE qc_evidence_artifact
        DROP CONSTRAINT IF EXISTS ck_qc_evidence_artifact_source
        """
    )
    op.execute(
        """
        ALTER TABLE qc_evidence_artifact
        ADD CONSTRAINT ck_qc_evidence_artifact_source CHECK (
            source IN ('baseline', 'rule_supplement', 'ai_call', 'report')
        )
        """
    )
