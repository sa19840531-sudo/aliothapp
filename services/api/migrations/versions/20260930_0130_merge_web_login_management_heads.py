"""合并 Web 登录证据与管理端任务可见性迁移分支。"""

from __future__ import annotations

revision = "20260930_0130"
down_revision = ("20260930_0129", "20260930_0128")
branch_labels = None
depends_on = None


def upgrade() -> None:
    """两条迁移历史均执行后汇合，不修改业务数据。"""


def downgrade() -> None:
    """仅拆分迁移末端，不撤销两侧业务变更。"""
