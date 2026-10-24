"""放开公司管理员角色，并把默认租户命名为体验中心。

Revision ID: 20260907_0097
Revises: 20260907_0096
Create Date: 2026-09-07

分公司接入后，每家公司需要一个只管本公司账号的管理员角色；平台管理员继续
承担总部（体验中心）跨公司视角。默认租户承接全部历史数据，正是体验中心本身，
这里只在它仍沿用迁移种子名时改名，避免覆盖运营手工改过的名字。
"""

from __future__ import annotations

from alembic import op

revision = "20260907_0097"
down_revision = "20260907_0096"
branch_labels = None
depends_on = None

DEFAULT_TENANT_ID = "00000000-0000-0000-0000-000000000001"
SEED_TENANT_NAME = "默认租户（历史数据归属）"
HEADQUARTERS_NAME = "体验中心"
COMPANY_ADMIN_ROLE = "公司管理员"
LEGACY_ROLES = ("平台管理员", "审计员", "规则配置员")


def upgrade() -> None:
    """扩展角色约束并给默认租户一个运营可读的名字。"""

    op.execute("ALTER TABLE qc_user DROP CONSTRAINT IF EXISTS ck_qc_user_role")
    op.execute(
        f"""
        ALTER TABLE qc_user
            ADD CONSTRAINT ck_qc_user_role CHECK (
                role IN ('平台管理员', '{COMPANY_ADMIN_ROLE}', '审计员', '规则配置员')
            )
        """
    )
    op.execute(
        f"""
        UPDATE qc_tenant
        SET display_name = '{HEADQUARTERS_NAME}',
            updated_at = now()
        WHERE id = '{DEFAULT_TENANT_ID}'::uuid
          AND display_name = '{SEED_TENANT_NAME}'
        """
    )


def downgrade() -> None:
    """公司管理员回落为审计员（同为本公司只读范围），再收紧角色约束。"""

    op.execute(
        f"""
        UPDATE qc_user
        SET role = '审计员',
            updated_at = now()
        WHERE role = '{COMPANY_ADMIN_ROLE}'
        """
    )
    op.execute("ALTER TABLE qc_user DROP CONSTRAINT IF EXISTS ck_qc_user_role")
    roles = ", ".join(f"'{role}'" for role in LEGACY_ROLES)
    op.execute(
        f"""
        ALTER TABLE qc_user
            ADD CONSTRAINT ck_qc_user_role CHECK (
                role IN ({roles})
            )
        """
    )
    op.execute(
        f"""
        UPDATE qc_tenant
        SET display_name = '{SEED_TENANT_NAME}',
            updated_at = now()
        WHERE id = '{DEFAULT_TENANT_ID}'::uuid
          AND display_name = '{HEADQUARTERS_NAME}'
        """
    )
