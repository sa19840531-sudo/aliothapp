"""恢复启用未成年红线标记检查（alioth_36），规则数 69 → 70。

Revision ID: 20260909_0105
Revises: 20260909_0104
Create Date: 2026-09-09

2026-09-04 对齐 61 条规则表时把 alioth_36 停用（迁移 0084）；09-09 立川拍板恢复：纯视觉口径，
命中只标记红线转人工，不自动判不过。种子重播不改既有行 status，所以由本迁移显式启用并恢复
适用页型与步骤；判定要点、补采与复用声明随种子重播更新。红线人名单库仍未发布，
「看起来像红线人」按视觉特征提示人工处置。
"""

from __future__ import annotations

from alembic import op

revision = "20260909_0105"
down_revision = "20260909_0104"
branch_labels = None
depends_on = None

_RULESET_FILTER = """
    FROM qc_ruleset AS ruleset
    WHERE rule.ruleset_id = ruleset.id
      AND ruleset.ruleset_code = 'alioth_first_ruleset'
      AND ruleset.version = 1
"""

_STEPS = (
    '[{"note":"产品首页及内容列表前两屏","action":"navigate",'
    '"target":"产品首页及内容列表前两屏","expected_page_type":"home"}]'
)


def upgrade() -> None:
    """启用 alioth_36 并恢复页型与步骤；规则集数量改为 70。"""

    op.execute(
        f"""
        UPDATE qc_rule AS rule
        SET
            status = 'enabled',
            page_type = 'home',
            applicable_page_types = '["home","content_list","video_list"]'::jsonb,
            steps = '{_STEPS}'::jsonb,
            updated_at = now()
        {_RULESET_FILTER}
          AND rule.rule_code = 'alioth_36'
        """
    )
    op.execute(
        """
        UPDATE qc_ruleset
        SET rule_count = 70
        WHERE ruleset_code = 'alioth_first_ruleset'
          AND version = 1
        """
    )


def downgrade() -> None:
    op.execute(
        """
        UPDATE qc_ruleset
        SET rule_count = 69
        WHERE ruleset_code = 'alioth_first_ruleset'
          AND version = 1
        """
    )
    op.execute(
        f"""
        UPDATE qc_rule AS rule
        SET
            status = 'disabled',
            page_type = NULL,
            applicable_page_types = '[]'::jsonb,
            steps = '[]'::jsonb,
            updated_at = now()
        {_RULESET_FILTER}
          AND rule.rule_code = 'alioth_36'
        """
    )
