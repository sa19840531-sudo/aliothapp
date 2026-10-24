"""允许来源页恢复动作写入动态页面探索审计表。

运行时在执行旧页面上的候选动作前，会先创建 ``recovery`` 来源的受控跳转。
此前数据库约束只允许入口和普通发现动作，导致恢复动作已经执行但审计快照无法落库。

Revision ID: 20260723_0017
Revises: 20260722_0016
Create Date: 2026-07-23
"""

from __future__ import annotations

from alembic import op

revision = "20260723_0017"
down_revision = "20260722_0016"
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
