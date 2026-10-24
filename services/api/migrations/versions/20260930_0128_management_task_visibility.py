"""管理端任务软删除和公司名称唯一约束。

Revision ID: 20260930_0128
Revises: 20260924_0127
Create Date: 2026-09-30
"""

from alembic import op

revision = "20260930_0128"
down_revision = "20260924_0127"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """保留质检材料；历史重名必须先人工核对，不自动改名。"""

    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT lower(btrim(display_name))
                FROM qc_tenant
                GROUP BY lower(btrim(display_name)) HAVING count(*) > 1
            ) THEN
                RAISE EXCEPTION '公司名称存在重复，请核对重名公司后再执行迁移';
            END IF;
        END $$
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_qc_tenant_display_name_normalized "
        "ON qc_tenant (lower(btrim(display_name)))"
    )
    op.execute("ALTER TABLE qc_task ADD COLUMN IF NOT EXISTS deleted_at timestamptz")
    op.execute(
        "ALTER TABLE qc_task ADD COLUMN IF NOT EXISTS deleted_by uuid "
        "REFERENCES qc_user(id) ON DELETE SET NULL"
    )
    op.execute("COMMENT ON COLUMN qc_task.deleted_at IS '管理端删除时间，原始质检材料保留'")
    op.execute("COMMENT ON COLUMN qc_task.deleted_by IS '执行管理端任务删除的操作人'")
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_qc_task_visible_created "
        "ON qc_task (created_at DESC) WHERE deleted_at IS NULL"
    )


def downgrade() -> None:
    """回退后隐藏任务重新可见，质检材料没有被删除。"""

    op.execute("DROP INDEX idx_qc_task_visible_created")
    op.execute("ALTER TABLE qc_task DROP COLUMN deleted_by")
    op.execute("ALTER TABLE qc_task DROP COLUMN deleted_at")
    op.execute("DROP INDEX uq_qc_tenant_display_name_normalized")
