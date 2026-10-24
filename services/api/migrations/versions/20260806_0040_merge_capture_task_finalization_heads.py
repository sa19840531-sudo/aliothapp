"""合并远程任务封存分支与本地公共 Capture 分支，恢复唯一迁移头。"""

from __future__ import annotations

revision = "20260806_0040"
down_revision = ("20260805_0037", "20260806_0039")
branch_labels = None
depends_on = None


def upgrade() -> None:
    """仅合并迁移图，不重复执行任一分支的数据库操作。"""


def downgrade() -> None:
    """合并节点无独立数据操作，降级时由 Alembic 回到两个父节点。"""
