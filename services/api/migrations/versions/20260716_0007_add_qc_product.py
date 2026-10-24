"""新增产品表 qc_product。

Revision ID: 20260716_0007
Revises: 20260711_0006
Create Date: 2026-07-16

仅建表结构；基础 72 条产品数据由 scripts/seed_products.py 幂等写入
（与 scripts/seed_first_ruleset.py 同一约定：迁移建表、种子脚本灌数据）。
"""

from __future__ import annotations

from alembic import op

revision = "20260716_0007"
down_revision = "20260711_0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE qc_product (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            project_code varchar(64) NOT NULL,
            product_name varchar(255) NOT NULL,
            h5_url text,
            status varchar(32) NOT NULL DEFAULT 'enabled',
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT uq_qc_product_project_code UNIQUE (project_code),
            CONSTRAINT ck_qc_product_status CHECK (
                status IN ('enabled', 'disabled')
            )
        )
        """
    )
    op.execute(
        """
        CREATE INDEX idx_qc_product_status_created
        ON qc_product (status, created_at DESC)
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_qc_product_updated_at
        BEFORE UPDATE ON qc_product
        FOR EACH ROW EXECUTE FUNCTION set_updated_at()
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_qc_product_updated_at ON qc_product")
    op.execute("DROP INDEX IF EXISTS idx_qc_product_status_created")
    op.execute("DROP TABLE IF EXISTS qc_product")
