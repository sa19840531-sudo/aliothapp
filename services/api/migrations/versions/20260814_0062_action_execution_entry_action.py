"""允许入口动作在动作台账里以「无父采集」留痕。

H5/Web 首跳导航发生在任何采集之前，父采集只能是入口哨兵（``h5-entry:``、
``agent-entry:``），它不指向任何已落库 Capture。采集侧早已认这件事——
首跳的 before Capture 以 ``parent_capture_id = NULL`` 落库当根证据；
台账侧却要求 ``requested_source_capture_id`` 非空，导致首跳无法入账，
V2 链路第一个动作就失败。此处把两侧语义对齐：
父采集为空 ⟺ 入口动作 ⟺ before Capture 是根证据。
"""

from __future__ import annotations

from alembic import op

revision = "20260814_0062"
down_revision = "20260814_0061"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE qc_action_execution
        ALTER COLUMN requested_source_capture_id DROP NOT NULL
        """
    )


def downgrade() -> None:
    # 入口动作行无法在旧合同下表达，存在此类事实时必须停止回滚。
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1
                FROM qc_action_execution
                WHERE requested_source_capture_id IS NULL
            ) THEN
                RAISE EXCEPTION
                    '存在入口动作台账行（无父采集），不能降级 0061';
            END IF;
        END
        $$;
        """
    )
    op.execute(
        """
        ALTER TABLE qc_action_execution
        ALTER COLUMN requested_source_capture_id SET NOT NULL
        """
    )
