"""规则集按期拆分：1 期 40 条 / 2 期 21 条 / 3 期瑞升 12 条，任务可以同时跑多个规则集。

Revision ID: 20260911_0107
Revises: 20260911_0106
Create Date: 2026-09-11

- 原「玉衡平台首期质检规则集」（alioth_first_ruleset）只留在线规则表 730 批的 40 条，改名「1期质检规则集」；
  830 批 20 条 + 页面元素对齐（alioth_spec_28）挪到新建的 2 期规则集，已停用的 +44 注册（alioth_1）、
  首页红线人（alioth_35）原本就是二批规则，一并挪过去、保持停用；运营 1 组拆出的 11 条 + 未成年红线标记
  （alioth_36）挪到新建的 3 期瑞升规则集。只改规则归属（rule.id 不变），历史判定照常可查。
- 任务仍然只锁一个规则集：选了多个时由提交入口建一个「组合规则集」（member_ruleset_ids 记成员），
  组合的规则 = 成员规则集的规则之和。执行链路里按规则集取规则的地方统一改用 ``qc_ruleset_scope()``，
  普通规则集返回它自己，组合规则集返回成员列表；快照哈希、版本等防护照旧按任务锁的那一个规则集走。
"""

from __future__ import annotations

from alembic import op

revision = "20260911_0107"
down_revision = "20260911_0106"
branch_labels = None
depends_on = None

_FIRST = "alioth_first_ruleset"
_PHASE2 = "alioth_phase2_ruleset"
_RUISHENG = "alioth_ruisheng_ruleset"

# 在线规则表 830 批 20 条 + 页面元素对齐；alioth_1 / alioth_35 是已停用的二批旧规则
_PHASE2_RULES = (
    "alioth_spec_17",
    "alioth_spec_18",
    "alioth_spec_24",
    "alioth_spec_27",
    "alioth_spec_31",
    "alioth_spec_35",
    "alioth_spec_37",
    "alioth_spec_38",
    "alioth_spec_48",
    "alioth_15",
    "alioth_spec_72",
    "alioth_33",
    "alioth_spec_new_20",
    "alioth_spec_new_24",
    "alioth_spec_new_25",
    "alioth_spec_new_27",
    "alioth_spec_new_29",
    "alioth_44",
    "alioth_spec_new_4",
    "alioth_47",
    "alioth_spec_28",
    "alioth_1",
    "alioth_35",
)
# 运营 1 组（瑞升产品）巡检表拆出的 11 条 + 未成年红线标记
_RUISHENG_RULES = tuple(f"alioth_ops_{index}" for index in range(1, 12)) + ("alioth_36",)


def _codes_sql(codes: tuple[str, ...]) -> str:
    return ", ".join(f"'{code}'" for code in codes)


def upgrade() -> None:
    op.execute("ALTER TABLE qc_ruleset ADD COLUMN IF NOT EXISTS member_ruleset_ids uuid[]")
    op.execute("ALTER TABLE qc_ruleset DROP CONSTRAINT IF EXISTS ck_qc_ruleset_source_type")
    op.execute(
        """
        ALTER TABLE qc_ruleset
        ADD CONSTRAINT ck_qc_ruleset_source_type
        CHECK (source_type IN ('manual', 'spreadsheet', 'import', 'api', 'composite'))
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION qc_ruleset_scope(target_ruleset_id uuid)
        RETURNS uuid[]
        LANGUAGE sql
        STABLE
        AS $$
            -- 普通规则集返回自己；组合规则集返回成员规则集，任务按它取规则
            SELECT COALESCE(ruleset.member_ruleset_ids, ARRAY[ruleset.id])
            FROM qc_ruleset AS ruleset
            WHERE ruleset.id = target_ruleset_id
        $$
        """
    )

    # 新建 2 期、3 期瑞升规则集：抽样与防死循环策略沿用 1 期；全新库（还没灌种子）直接跳过，由种子脚本建
    for code, name, description, source_ref, rule_count in (
        (
            _PHASE2,
            "2期质检规则集",
            "在线规则表 8 月 30 日批次 20 条 + 页面元素对齐与层次感",
            None,
            21,
        ),
        (
            _RUISHENG,
            "3期瑞升质检规则集",
            "运营 1 组（瑞升产品）人工巡检拆出的 11 条 + 未成年红线标记",
            "https://docs.google.com/spreadsheets/d/1xdBg6rn8VX5z8tgtaVd3RIp39A5X5gA4lM-DCT2gLWc/edit?gid=0",
            12,
        ),
    ):
        source_ref_sql = f"'{source_ref}'" if source_ref else "first.source_ref"
        op.execute(
            f"""
            INSERT INTO qc_ruleset (
                ruleset_code, name, version, status, description, source_type, source_ref,
                snapshot_hash, rule_count, page_sampling_policy, loop_guard, published_at
            )
            SELECT
                '{code}', '{name}', 1, 'enabled', '{description}', 'spreadsheet', {source_ref_sql},
                NULL, {rule_count}, first.page_sampling_policy, first.loop_guard, now()
            FROM qc_ruleset AS first
            WHERE first.ruleset_code = '{_FIRST}'
              AND first.version = 1
            ON CONFLICT (ruleset_code, version) DO NOTHING
            """
        )

    for code, rules in ((_PHASE2, _PHASE2_RULES), (_RUISHENG, _RUISHENG_RULES)):
        op.execute(
            f"""
            UPDATE qc_rule AS rule
            SET ruleset_id = target.id,
                updated_at = now()
            FROM qc_ruleset AS first, qc_ruleset AS target
            WHERE first.ruleset_code = '{_FIRST}'
              AND first.version = 1
              AND target.ruleset_code = '{code}'
              AND target.version = 1
              AND rule.ruleset_id = first.id
              AND rule.rule_code IN ({_codes_sql(rules)})
              AND NOT EXISTS (
                  SELECT 1 FROM qc_rule AS existing
                  WHERE existing.ruleset_id = target.id
                    AND existing.rule_code = rule.rule_code
              )
            """
        )

    op.execute(
        f"""
        UPDATE qc_ruleset
        SET name = '1期质检规则集',
            description = '在线规则表 7 月 30 日批次 40 条（原玉衡平台首期质检规则集）',
            rule_count = 40
        WHERE ruleset_code = '{_FIRST}'
          AND version = 1
        """
    )


def downgrade() -> None:
    for code in (_PHASE2, _RUISHENG):
        op.execute(
            f"""
            UPDATE qc_rule AS rule
            SET ruleset_id = first.id,
                updated_at = now()
            FROM qc_ruleset AS first, qc_ruleset AS source
            WHERE first.ruleset_code = '{_FIRST}'
              AND first.version = 1
              AND source.ruleset_code = '{code}'
              AND source.version = 1
              AND rule.ruleset_id = source.id
            """
        )
    op.execute(
        f"""
        UPDATE qc_ruleset
        SET name = '玉衡平台首期质检规则集',
            description = '玉衡平台首期质检规则集',
            rule_count = 73
        WHERE ruleset_code = '{_FIRST}'
          AND version = 1
        """
    )
    # 被任务引用过的规则集（外键 RESTRICT）留着，只删没用过的
    op.execute(
        f"""
        DELETE FROM qc_ruleset AS ruleset
        WHERE (ruleset.ruleset_code IN ('{_PHASE2}', '{_RUISHENG}') OR ruleset.member_ruleset_ids IS NOT NULL)
          AND NOT EXISTS (SELECT 1 FROM qc_task AS task WHERE task.ruleset_id = ruleset.id)
          AND NOT EXISTS (SELECT 1 FROM qc_rule AS rule WHERE rule.ruleset_id = ruleset.id)
        """
    )
    op.execute("DROP FUNCTION IF EXISTS qc_ruleset_scope(uuid)")
    op.execute("UPDATE qc_ruleset SET source_type = 'manual' WHERE source_type = 'composite'")
    op.execute("ALTER TABLE qc_ruleset DROP CONSTRAINT IF EXISTS ck_qc_ruleset_source_type")
    op.execute(
        """
        ALTER TABLE qc_ruleset
        ADD CONSTRAINT ck_qc_ruleset_source_type
        CHECK (source_type IN ('manual', 'spreadsheet', 'import', 'api'))
        """
    )
    op.execute("ALTER TABLE qc_ruleset DROP COLUMN IF EXISTS member_ruleset_ids")
