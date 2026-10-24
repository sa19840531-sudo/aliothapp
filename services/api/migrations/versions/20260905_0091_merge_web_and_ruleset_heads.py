"""合并 Web V2 与规则种子迁移分支。"""

from __future__ import annotations

revision = "20260905_0091"
down_revision = ("20260905_0090", "20260904_0084")
branch_labels = None
depends_on = None


def upgrade() -> None:
    """合并迁移分支，不修改业务数据。"""


def downgrade() -> None:
    """拆分迁移分支，不修改业务数据。"""
