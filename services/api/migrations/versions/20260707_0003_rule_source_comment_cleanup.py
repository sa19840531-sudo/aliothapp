"""同步规则相关字段 COMMENT。

Revision ID: 20260707_0003
Revises: 20260706_0002
Create Date: 2026-07-07
"""

from __future__ import annotations

from alembic import op

revision = "20260707_0003"
down_revision = "20260706_0002"
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
        '规则集来源引用，如导入文件路径、外部系统编号或 API 请求编号'
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
        '判定配置 JSON，见 JSONB 数据字典'
        """
    )
