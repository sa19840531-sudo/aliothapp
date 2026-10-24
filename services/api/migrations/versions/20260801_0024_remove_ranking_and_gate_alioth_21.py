"""移除榜单页规则并限制首页弹窗蒙层规则的触发状态。

Revision ID: 20260801_0024
Revises: 20260731_0023
Create Date: 2026-08-01
"""

from __future__ import annotations

from alembic import op

revision = "20260801_0024"
down_revision = "20260731_0023"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE qc_rule AS rule
        SET
            judge_config = jsonb_set(
                rule.judge_config,
                '{engine_config,required_page_states}',
                '["popup", "modal", "overlay", "dialog"]'::jsonb,
                true
            ),
            updated_at = now()
        FROM qc_ruleset AS ruleset
        WHERE rule.ruleset_id = ruleset.id
          AND ruleset.ruleset_code = 'alioth_first_ruleset'
          AND ruleset.version = 1
          AND rule.rule_code = 'alioth_21'
        """
    )
    op.execute(
        """
        UPDATE qc_ruleset
        SET rule_count = 39
        WHERE ruleset_code = 'alioth_first_ruleset'
          AND version = 1
        """
    )
    op.execute(
        """
        UPDATE qc_rule AS rule
        SET
            status = 'disabled',
            page_type = NULL,
            applicable_page_types = '[]'::jsonb,
            steps = '[]'::jsonb,
            updated_at = now()
        FROM qc_ruleset AS ruleset
        WHERE rule.ruleset_id = ruleset.id
          AND ruleset.ruleset_code = 'alioth_first_ruleset'
          AND ruleset.version = 1
          AND rule.rule_code = 'alioth_39'
        """
    )


def downgrade() -> None:
    op.execute(
        """
        UPDATE qc_ruleset
        SET rule_count = 40
        WHERE ruleset_code = 'alioth_first_ruleset'
          AND version = 1
        """
    )
    op.execute(
        """
        UPDATE qc_rule AS rule
        SET
            status = 'enabled',
            page_type = 'ranking',
            applicable_page_types = '["ranking"]'::jsonb,
            steps = '[{"action":"navigate","target":"排行榜","expected_page_type":"ranking","note":"排行榜"}]'::jsonb,
            updated_at = now()
        FROM qc_ruleset AS ruleset
        WHERE rule.ruleset_id = ruleset.id
          AND ruleset.ruleset_code = 'alioth_first_ruleset'
          AND ruleset.version = 1
          AND rule.rule_code = 'alioth_39'
        """
    )
    op.execute(
        """
        UPDATE qc_rule AS rule
        SET
            judge_config = rule.judge_config #- '{engine_config,required_page_states}',
            updated_at = now()
        FROM qc_ruleset AS ruleset
        WHERE rule.ruleset_id = ruleset.id
          AND ruleset.ruleset_code = 'alioth_first_ruleset'
          AND ruleset.version = 1
          AND rule.rule_code = 'alioth_21'
        """
    )
