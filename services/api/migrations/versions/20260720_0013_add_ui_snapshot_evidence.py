"""扩展结构化 UI 快照证据类型。"""

from alembic import op

revision = "20260720_0013"
down_revision = "20260717_0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE qc_evidence_artifact
        DROP CONSTRAINT IF EXISTS ck_qc_evidence_artifact_type
        """
    )
    op.execute(
        """
        ALTER TABLE qc_evidence_artifact
        ADD CONSTRAINT ck_qc_evidence_artifact_type CHECK (
            artifact_type IN (
                'screenshot', 'dom', 'visible_text', 'ocr_text', 'har',
                'console_log', 'media_state', 'ui_snapshot', 'ai_io', 'report'
            )
        )
        """
    )
    op.execute(
        """
        COMMENT ON COLUMN qc_evidence_artifact.artifact_type IS
        '证据类型；ui_snapshot 为浏览器直接采集的有界渲染事实'
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DELETE FROM qc_evidence_artifact
        WHERE artifact_type = 'ui_snapshot'
        """
    )
    op.execute(
        """
        ALTER TABLE qc_evidence_artifact
        DROP CONSTRAINT IF EXISTS ck_qc_evidence_artifact_type
        """
    )
    op.execute(
        """
        ALTER TABLE qc_evidence_artifact
        ADD CONSTRAINT ck_qc_evidence_artifact_type CHECK (
            artifact_type IN (
                'screenshot', 'dom', 'visible_text', 'ocr_text', 'har',
                'console_log', 'media_state', 'ai_io', 'report'
            )
        )
        """
    )
