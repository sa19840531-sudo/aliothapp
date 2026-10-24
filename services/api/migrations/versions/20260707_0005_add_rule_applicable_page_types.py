"""新增规则适用页面类型列表字段。

Revision ID: 20260707_0005
Revises: 20260707_0004
Create Date: 2026-07-07

说明：早期注释误认为字段已在 20260706_0001 建表时创建，
实际核心建表并无此列。本迁移补齐 applicable_page_types，
保证全新库可升级到后续依赖该字段的 revision（如 0023）。
"""

from __future__ import annotations

from alembic import op

revision = "20260707_0005"
down_revision = "20260707_0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE qc_rule
        ADD COLUMN IF NOT EXISTS applicable_page_types jsonb NOT NULL DEFAULT '[]'::jsonb
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_qc_rule_applicable_page_types
        ON qc_rule USING gin (applicable_page_types)
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_qc_rule_applicable_page_types")
    op.execute("ALTER TABLE qc_rule DROP COLUMN IF EXISTS applicable_page_types")
