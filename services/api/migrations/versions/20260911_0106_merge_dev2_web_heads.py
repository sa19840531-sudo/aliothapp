"""合并 dev_2.0 与 Web V2 探索质检迁移分支。"""

from __future__ import annotations

revision = "20260911_0106"
down_revision = ("20260909_0105", "20260910_0103")
branch_labels = None
depends_on = None


def upgrade() -> None:
    """保留两条既有迁移历史，不修改业务数据。"""


def downgrade() -> None:
    """仅拆分迁移末端，不撤销两侧业务变更。"""
