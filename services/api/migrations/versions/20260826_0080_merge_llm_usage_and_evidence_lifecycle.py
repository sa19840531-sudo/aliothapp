"""合并 LLM 用量审计与证据生命周期迁移分支。

Revision ID: 20260826_0080
Revises: 20260826_0078, 20260826_0079
Create Date: 2026-08-27
"""

from __future__ import annotations

revision = "20260826_0080"
down_revision = ("20260826_0078", "20260826_0079")
branch_labels = None
depends_on = None


def upgrade() -> None:
    """仅合并迁移血缘，不新增数据库变更。"""


def downgrade() -> None:
    """仅拆分迁移血缘，不回滚父迁移的数据结构。"""
