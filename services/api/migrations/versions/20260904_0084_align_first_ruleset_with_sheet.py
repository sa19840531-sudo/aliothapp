"""规则集对齐 2026-09-04 规则表：停用表内已删除的 3 条，规则数改为 61。

Revision ID: 20260904_0084
Revises: 20260901_0083
Create Date: 2026-09-04

规则表（质检规范在线表格）已定稿 61 条。种子 CSV 同步改为这 61 条并全部启用（含新建的 alioth_spec_28 元素对齐与层次感）；
库里多出的 +44 注册（alioth_1）、首页红线人（alioth_35）、未成年红线标记（alioth_36）
三条在种子中已移除，重播种子不会再触碰它们，因此由本迁移显式停用。
执行记录与规则表存在外键（ON DELETE RESTRICT），历史判定必须保留，所以只停用不删行。
"""

from __future__ import annotations

from alembic import op

revision = "20260904_0084"
down_revision = "20260901_0083"
branch_labels = None
depends_on = None

_RULESET_FILTER = """
    FROM qc_ruleset AS ruleset
    WHERE rule.ruleset_id = ruleset.id
      AND ruleset.ruleset_code = 'alioth_first_ruleset'
      AND ruleset.version = 1
"""

_REMOVED = {
    "alioth_1": {
        "page_type": "profile",
        "applicable_page_types": '["profile","settings"]',
        "steps": (
            '[{"note":"个人中心/设置中的注册登录入口","action":"navigate",'
            '"target":"个人中心/设置中的注册登录入口","expected_page_type":"profile"},'
            '{"note":"查找手机号注册通道并尝试 +44 号段","action":"operate",'
            '"target":"查找手机号注册通道并尝试 +44 号段"}]'
        ),
    },
    "alioth_35": {
        "page_type": "home",
        "applicable_page_types": '["home","content_list","video_list"]',
        "steps": (
            '[{"note":"产品首页及内容列表前两屏","action":"navigate",'
            '"target":"产品首页及内容列表前两屏","expected_page_type":"home"}]'
        ),
    },
    "alioth_36": {
        "page_type": "home",
        "applicable_page_types": '["home","content_list","video_list"]',
        "steps": (
            '[{"note":"产品首页及内容列表前两屏","action":"navigate",'
            '"target":"产品首页及内容列表前两屏","expected_page_type":"home"}]'
        ),
    },
}


def upgrade() -> None:
    for rule_code in _REMOVED:
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
              AND rule.rule_code = '{rule_code}'
            """
        )
    op.execute(
        """
        UPDATE qc_ruleset
        SET rule_count = 61
        WHERE ruleset_code = 'alioth_first_ruleset'
          AND version = 1
        """
    )


def downgrade() -> None:
    op.execute(
        """
        UPDATE qc_ruleset
        SET rule_count = 62
        WHERE ruleset_code = 'alioth_first_ruleset'
          AND version = 1
        """
    )
    for rule_code, original in _REMOVED.items():
        op.execute(
            f"""
            UPDATE qc_rule AS rule
            SET
                status = 'enabled',
                page_type = '{original["page_type"]}',
                applicable_page_types = '{original["applicable_page_types"]}'::jsonb,
                steps = '{original["steps"]}'::jsonb,
                updated_at = now()
            {_RULESET_FILTER}
              AND rule.rule_code = '{rule_code}'
            """
        )
