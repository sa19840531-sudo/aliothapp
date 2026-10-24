"""合并页面状态图与 Web 正式报告迁移分支。"""

from __future__ import annotations

revision = "20260907_0095"
down_revision = ("20260906_0085", "20260906_0094")
branch_labels = None
depends_on = None


def upgrade() -> None:
    """保留两条既有迁移历史，不修改业务数据。"""


def downgrade() -> None:
    """仅拆分迁移末端，不撤销两侧业务变更。"""
