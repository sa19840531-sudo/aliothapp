"""修复已跳过来源恢复约束迁移的数据库实例。

部分本地库可能已经标记到 ``20260724_0017``，但动态探索动作来源约束仍停留在
只允许 ``entry/discovered`` 的旧定义，导致 ``recovery`` 来源的恢复动作无法写入审计表。
本迁移作为当前 head 之后的幂等修复，确保升级到最新 head 的实例都具备一致约束。

Revision ID: 20260725_0018
Revises: 20260724_0017
Create Date: 2026-07-25
"""

from __future__ import annotations

from alembic import op

revision = "20260725_0018"
down_revision = "20260724_0017"
branch_labels = None
depends_on = None

_CONSTRAINT_NAME = "ck_qc_page_exploration_action_origin"


def upgrade() -> None:
    op.execute(
        f"""
        ALTER TABLE qc_page_exploration_action
        DROP CONSTRAINT IF EXISTS {_CONSTRAINT_NAME}
        """
    )
    op.execute(
        f"""
        ALTER TABLE qc_page_exploration_action
        ADD CONSTRAINT {_CONSTRAINT_NAME}
        CHECK (origin IN ('entry', 'discovered', 'recovery'))
        """
    )


def downgrade() -> None:
    op.execute(
        """
        UPDATE qc_page_exploration_action
        SET origin = 'discovered'
        WHERE origin = 'recovery'
        """
    )
    op.execute(
        f"""
        ALTER TABLE qc_page_exploration_action
        DROP CONSTRAINT IF EXISTS {_CONSTRAINT_NAME}
        """
    )
    op.execute(
        f"""
        ALTER TABLE qc_page_exploration_action
        ADD CONSTRAINT {_CONSTRAINT_NAME}
        CHECK (origin IN ('entry', 'discovered'))
        """
    )