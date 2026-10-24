"""为 App 动作 Capture 增加业务幂等身份。"""

from alembic import op

revision = "20260806_0038"
down_revision = "20260805_0036"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 历史重复数据必须先被发现并人工处置，不能让唯一索引创建失败后只留下半截迁移状态。
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1
                FROM qc_capture
                WHERE action_execution_id IS NOT NULL
                  AND capture_role IN ('before_action', 'after_action')
                GROUP BY action_execution_id, capture_role
                HAVING count(*) > 1
            ) THEN
                RAISE EXCEPTION
                    'qc_capture contains duplicate action_execution_id/capture_role rows';
            END IF;
        END
        $$;
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX uq_qc_capture_action_role
            ON qc_capture (action_execution_id, capture_role)
            WHERE action_execution_id IS NOT NULL
              AND capture_role IN ('before_action', 'after_action')
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_qc_capture_action_role")
