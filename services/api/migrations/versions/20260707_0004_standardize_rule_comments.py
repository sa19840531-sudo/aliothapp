"""规范化规则相关字段 COMMENT。

Revision ID: 20260707_0004
Revises: 20260707_0003
Create Date: 2026-07-07
"""

from __future__ import annotations

from alembic import op

revision = "20260707_0004"
down_revision = "20260707_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        COMMENT ON COLUMN qc_ruleset.source_ref IS
        '规则集来源引用'
        """
    )
    op.execute(
        """
        COMMENT ON COLUMN qc_rule.description IS
        '规则说明'
        """
    )
    op.execute(
        """
        COMMENT ON COLUMN qc_rule.judge_config IS
        '规则判定配置 JSON，见 JSONB 数据字典'
        """
    )


def downgrade() -> None:
    op.execute(
        """
        COMMENT ON COLUMN qc_ruleset.source_ref IS
        '规则集来源引用'
        """
    )
    op.execute(
        """
        COMMENT ON COLUMN qc_rule.description IS
        '规则说明'
        """
    )
    op.execute(
        """
        COMMENT ON COLUMN qc_rule.judge_config IS
        '规则判定配置 JSON，见 JSONB 数据字典'
        """
    )
