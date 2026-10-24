"""规则测试记录补一列：第二个位置的页面文字。

Revision ID: 20260912_0112
Revises: 20260912_0111
Create Date: 2026-09-11

- 规则测试页给「比两个位置价格」「比操作前后截图」这类规则补了另一组证据的输入框；
  第二个位置的页面文字存这一列，操作前的截图和页面截图一起记在 screenshots 里
  （capture_role = before），不另加列。
"""

from __future__ import annotations

from alembic import op

revision = "20260912_0112"
down_revision = "20260912_0111"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE qc_rule_test_record
        ADD COLUMN second_location_text text NOT NULL DEFAULT ''
        """
    )
    op.execute(
        """
        COMMENT ON COLUMN qc_rule_test_record.second_location_text IS
        '比两个位置价格的规则用：第二个位置的页面文字（页面文字列是第一个位置）'
        """
    )


def downgrade() -> None:
    op.execute("ALTER TABLE qc_rule_test_record DROP COLUMN IF EXISTS second_location_text")
