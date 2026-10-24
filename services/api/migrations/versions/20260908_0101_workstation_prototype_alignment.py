"""工作台对齐 2.0 原型缺的数据：产品部门/负责人/平台、产品导入记录、任务草稿。

Revision ID: 20260908_0101
Revises: 20260908_0100
Create Date: 2026-09-08

原型的产品列表有「所属部门 / 负责人 / 支持平台」，批量导入有「导入记录」，创建任务有
「保存草稿」。这些都不是展示层能凑出来的，补表补列：

- qc_product 增加 department_name / owner_name / platforms（text[]，h5/web/app）；
  已配置 H5 地址的存量产品回填 platforms = {h5}。
- qc_product_import_record 记录每次 CSV 批量导入的操作人、文件、成功/失败计数与错误明细。
- qc_task_draft 按账号保存创建任务表单草稿（payload 原样存 JSON，恢复时由前端解释）。
"""

from __future__ import annotations

from alembic import op

revision = "20260908_0101"
down_revision = "20260908_0100"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """补产品资料列、导入记录表与任务草稿表。"""

    op.execute("ALTER TABLE qc_product ADD COLUMN IF NOT EXISTS department_name varchar(128)")
    op.execute("ALTER TABLE qc_product ADD COLUMN IF NOT EXISTS owner_name varchar(128)")
    op.execute(
        "ALTER TABLE qc_product ADD COLUMN IF NOT EXISTS platforms text[] NOT NULL DEFAULT '{}'"
    )
    op.execute(
        """
        UPDATE qc_product
        SET platforms = ARRAY['h5']
        WHERE h5_url IS NOT NULL AND cardinality(platforms) = 0
        """
    )

    op.execute(
        """
        CREATE TABLE qc_product_import_record (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id uuid NOT NULL REFERENCES qc_tenant(id),
            operator_id uuid NOT NULL REFERENCES qc_user(id),
            operator_name varchar(128) NOT NULL,
            filename varchar(255) NOT NULL,
            total_rows integer NOT NULL DEFAULT 0,
            created_count integer NOT NULL DEFAULT 0,
            updated_count integer NOT NULL DEFAULT 0,
            failed_count integer NOT NULL DEFAULT 0,
            errors jsonb NOT NULL DEFAULT '[]'::jsonb,
            created_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        """
        CREATE INDEX ix_qc_product_import_record_tenant_created
            ON qc_product_import_record (tenant_id, created_at DESC)
        """
    )

    op.execute(
        """
        CREATE TABLE qc_task_draft (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id uuid NOT NULL REFERENCES qc_tenant(id),
            owner_id uuid NOT NULL REFERENCES qc_user(id),
            name varchar(200) NOT NULL,
            target_platform varchar(16) NOT NULL DEFAULT 'h5',
            payload jsonb NOT NULL DEFAULT '{}'::jsonb,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        """
        CREATE INDEX ix_qc_task_draft_owner_updated
            ON qc_task_draft (owner_id, updated_at DESC)
        """
    )


def downgrade() -> None:
    """删表删列。"""

    op.execute("DROP TABLE IF EXISTS qc_task_draft")
    op.execute("DROP TABLE IF EXISTS qc_product_import_record")
    op.execute("ALTER TABLE qc_product DROP COLUMN IF EXISTS platforms")
    op.execute("ALTER TABLE qc_product DROP COLUMN IF EXISTS owner_name")
    op.execute("ALTER TABLE qc_product DROP COLUMN IF EXISTS department_name")
