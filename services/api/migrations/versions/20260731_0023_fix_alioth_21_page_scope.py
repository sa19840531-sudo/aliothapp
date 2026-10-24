"""把首页弹窗蒙层规则限制在首页。

Revision ID: 20260731_0023
Revises: 20260731_0022
Create Date: 2026-07-31
"""

from __future__ import annotations

from alembic import op

revision = "20260731_0023"
down_revision = "20260731_0022"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE qc_rule AS rule
        SET
            page_type = 'home',
            applicable_page_types = '["home"]'::jsonb,
            updated_at = now()
        FROM qc_ruleset AS ruleset
        WHERE rule.ruleset_id = ruleset.id
          AND ruleset.ruleset_code = 'alioth_first_ruleset'
          AND ruleset.version = 1
          AND rule.rule_code = 'alioth_21'
        """
    )


def downgrade() -> None:
    op.execute(
        """
        UPDATE qc_rule AS rule
        SET
            page_type = NULL,
            applicable_page_types = '["home", "startup_ad"]'::jsonb,
            updated_at = now()
        FROM qc_ruleset AS ruleset
        WHERE rule.ruleset_id = ruleset.id
          AND ruleset.ruleset_code = 'alioth_first_ruleset'
          AND ruleset.version = 1
          AND rule.rule_code = 'alioth_21'
        """
    )
