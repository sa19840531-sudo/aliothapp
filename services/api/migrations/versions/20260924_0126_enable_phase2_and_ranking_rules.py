"""启用 2 期 20 条质检规则与「榜单数据排序合理性检查」（alioth_39），对齐 09-04 定稿的 61 条全启用。

Revision ID: 20260924_0126
Revises: 20260919_0125
Create Date: 2026-09-24

09-04 种子已改为 61 条全启用，但种子重播只给新行用种子里的启停、不改既有行 status：
2 期这 20 条在 08-07 以「默认停用」入库，alioth_39 在 08-01 被迁移 0024 停用，所以已部署的环境里
它们一直是停用，任务一次没跑。由本迁移显式启用；适用页型、步骤、判定要点随种子重播更新，
规则集条数（1 期 40、2 期 21）早已按全启用计，不用改。alioth_1、alioth_35 是 09-04 定稿时
移出的规则（迁移 0084），保持停用。
"""

from __future__ import annotations

from alembic import op

revision = "20260924_0126"
down_revision = "20260919_0125"
branch_labels = None
depends_on = None

_RULESET_CODES = ("alioth_first_ruleset", "alioth_phase2_ruleset")
_RULE_CODES = (
    "alioth_15",
    "alioth_33",
    "alioth_44",
    "alioth_47",
    "alioth_spec_17",
    "alioth_spec_18",
    "alioth_spec_24",
    "alioth_spec_27",
    "alioth_spec_31",
    "alioth_spec_35",
    "alioth_spec_37",
    "alioth_spec_38",
    "alioth_spec_48",
    "alioth_spec_72",
    "alioth_spec_new_4",
    "alioth_spec_new_20",
    "alioth_spec_new_24",
    "alioth_spec_new_25",
    "alioth_spec_new_27",
    "alioth_spec_new_29",
    "alioth_39",
)


def _set_status(from_status: str, to_status: str) -> None:
    rulesets = ", ".join(f"'{code}'" for code in _RULESET_CODES)
    rules = ", ".join(f"'{code}'" for code in _RULE_CODES)
    op.execute(
        f"""
        UPDATE qc_rule AS rule
        SET status = '{to_status}', updated_at = now()
        FROM qc_ruleset AS ruleset
        WHERE rule.ruleset_id = ruleset.id
          AND ruleset.ruleset_code IN ({rulesets})
          AND ruleset.version = 1
          AND rule.rule_code IN ({rules})
          AND rule.status = '{from_status}'
        """
    )


def upgrade() -> None:
    """把 21 条停用规则改为启用；空库里还没有这些行时不做任何事，由种子按启用插入。"""

    _set_status("disabled", "enabled")


def downgrade() -> None:
    _set_status("enabled", "disabled")
