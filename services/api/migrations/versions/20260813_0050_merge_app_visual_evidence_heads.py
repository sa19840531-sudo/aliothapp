"""合并 App 视觉阶段事实与证据身份门禁迁移分支。

Revision ID: 20260813_0050
Revises: 20260812_0049, 20260813_0049
Create Date: 2026-08-13
"""

from __future__ import annotations

revision = "20260813_0050"
down_revision = ("20260812_0049", "20260813_0049")
branch_labels = None
depends_on = None


def upgrade() -> None:
    """仅合并迁移血缘，不新增数据库变更。"""


def downgrade() -> None:
    """仅拆分迁移血缘，不回滚父迁移的数据结构。"""
