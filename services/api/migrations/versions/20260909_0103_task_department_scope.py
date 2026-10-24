"""任务落库记部门：部门级数据范围的过滤列。

Revision ID: 20260909_0103
Revises: 20260908_0102
Create Date: 2026-09-09

部门账号只看本部门的任务、报告、看板与复核项，查询要按 qc_task.department_name 过滤；
之前部门只藏在 task_config.product.department_name 里。补列并从快照回填（'' 与 '-' 视为没填），
新任务由提交时的产品快照 / 账号部门写入。
"""

from __future__ import annotations

from alembic import op

revision = "20260909_0103"
down_revision = "20260908_0102"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """补任务部门列、回填并建索引。"""

    op.execute("ALTER TABLE qc_task ADD COLUMN IF NOT EXISTS department_name varchar(128)")
    op.execute(
        """
        UPDATE qc_task
        SET department_name = NULLIF(NULLIF(btrim(task_config->'product'->>'department_name'), ''), '-')
        WHERE department_name IS NULL
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_qc_task_tenant_department
            ON qc_task (tenant_id, department_name)
        """
    )


def downgrade() -> None:
    """删索引删列。"""

    op.execute("DROP INDEX IF EXISTS ix_qc_task_tenant_department")
    op.execute("ALTER TABLE qc_task DROP COLUMN IF EXISTS department_name")
