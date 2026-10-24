"""Web V2 新任务恢复十菜单预算，历史五菜单预算保持冻结。"""

from __future__ import annotations

from alembic import op

revision = "20260905_0090"
down_revision = "20260904_0089"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """仅修改新建预算的默认值，不重写既有任务。"""

    op.execute(
        """
        ALTER TABLE qc_web_exploration_budget
        ALTER COLUMN menu_limit SET DEFAULT 10
        """
    )


def downgrade() -> None:
    """回退新建预算默认值，历史数据仍保持原值。"""

    op.execute(
        """
        ALTER TABLE qc_web_exploration_budget
        ALTER COLUMN menu_limit SET DEFAULT 5
        """
    )
