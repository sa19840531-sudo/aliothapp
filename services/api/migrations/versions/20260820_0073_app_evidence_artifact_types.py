"""对齐 App Capture 与证据附件数据库类型白名单。"""

from __future__ import annotations

from alembic import op

revision = "20260820_0073"
down_revision = "20260820_0072"
branch_labels = None
depends_on = None


# Web 既有类型、规则引擎类型和 App Capture 合同类型必须共同保留。
_ARTIFACT_TYPES = (
    "'screenshot', 'dom', 'visible_text', 'ocr_text', 'har', "
    "'console_log', 'media_state', 'ai_io', 'report', "
    "'action_trace', 'ui_snapshot', 'temporal_trace', 'page_coverage', "
    "'ui_tree', 'view_hierarchy', 'device_log', 'network_trace', "
    "'fault_artifact'"
)
_APP_TYPES_ADDED = "'view_hierarchy', 'device_log', 'network_trace', 'fault_artifact'"


def upgrade() -> None:
    """允许 App Capture 合同中的全部制品正式写入证据表。"""

    op.execute(
        "ALTER TABLE qc_evidence_artifact DROP CONSTRAINT IF EXISTS ck_qc_evidence_artifact_type"
    )
    op.execute(
        f"""
        ALTER TABLE qc_evidence_artifact
        ADD CONSTRAINT ck_qc_evidence_artifact_type
        CHECK (artifact_type IN ({_ARTIFACT_TYPES}))
        """
    )


def downgrade() -> None:
    """存在新增 App 制品时拒绝降级，避免留下不可被旧约束接受的历史。"""

    op.execute(
        f"""
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1
                FROM qc_evidence_artifact
                WHERE artifact_type IN ({_APP_TYPES_ADDED})
            ) THEN
                RAISE EXCEPTION
                    'cannot downgrade while extended App evidence artifacts exist';
            END IF;
        END
        $$
        """
    )
    op.execute(
        "ALTER TABLE qc_evidence_artifact DROP CONSTRAINT IF EXISTS ck_qc_evidence_artifact_type"
    )
    op.execute(
        """
        ALTER TABLE qc_evidence_artifact
        ADD CONSTRAINT ck_qc_evidence_artifact_type CHECK (
            artifact_type IN (
                'screenshot', 'dom', 'visible_text', 'ocr_text', 'har',
                'console_log', 'media_state', 'ai_io', 'report',
                'action_trace', 'ui_snapshot', 'temporal_trace', 'page_coverage',
                'ui_tree'
            )
        )
        """
    )
