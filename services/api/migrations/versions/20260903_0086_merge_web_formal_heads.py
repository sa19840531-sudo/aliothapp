"""合并既有观测链与 Web 探索预算链的 Alembic heads。

Revision ID: 20260903_0086
Revises: 20260901_0081, 20260902_0085
Create Date: 2026-09-03
"""

from __future__ import annotations

revision = "20260903_0086"
down_revision = ("20260901_0081", "20260902_0085")
branch_labels = None
depends_on = None


def upgrade() -> None:
    """仅合并迁移图，不修改数据库结构。"""


def downgrade() -> None:
    """仅拆分迁移图，不修改数据库结构。"""
