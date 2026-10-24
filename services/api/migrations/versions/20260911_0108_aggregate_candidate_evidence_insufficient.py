"""聚合候选增加「证据不足」终态：应检但没有可用证据时转人工，不再借用「不适用」或卡在收口。

Revision ID: 20260911_0108
Revises: 20260911_0107
Create Date: 2026-09-11

- 聚合规则在封存前就判定没有可用证据（全部被弹窗遮挡/诊断材料、适用页面没采到、旅程或环境矩阵成员不够、
  范围计划与证据冲突）时，候选记为 ``evidence_insufficient``，同一事务写一条证据不足的规则执行和待复核项。
- 原来这些情况要么记「不适用」（被遮挡时，掩盖了应检规则），要么记 ``needs_review``（收口门禁算未结算、
  又没有可处理的复核项，任务停在收口出不来）。
"""

from __future__ import annotations

from alembic import op

revision = "20260911_0108"
down_revision = "20260911_0107"
branch_labels = None
depends_on = None


def _replace_constraints(*, terminal_statuses: str, all_statuses: str) -> None:
    op.execute(
        f"""
        ALTER TABLE qc_aggregate_inspection_candidate
        DROP CONSTRAINT IF EXISTS ck_qc_aggregate_candidate_status,
        DROP CONSTRAINT IF EXISTS ck_qc_aggregate_candidate_links,
        ADD CONSTRAINT ck_qc_aggregate_candidate_status CHECK (
            status IN ({all_statuses})
        ),
        ADD CONSTRAINT ck_qc_aggregate_candidate_links CHECK (
            (status = 'published'
                AND evidence_input_set_id IS NOT NULL
                AND work_item_id IS NOT NULL
                AND finished_at IS NULL)
            OR (status = 'completed'
                AND evidence_input_set_id IS NOT NULL
                AND work_item_id IS NOT NULL
                AND finished_at IS NOT NULL)
            OR (status IN ({terminal_statuses})
                AND finished_at IS NOT NULL)
            OR (status = 'expected' AND finished_at IS NULL)
        )
        """
    )


def upgrade() -> None:
    _replace_constraints(
        terminal_statuses="'not_applicable', 'needs_review', 'failed', 'evidence_insufficient'",
        all_statuses=(
            "'expected', 'published', 'completed', 'not_applicable', "
            "'needs_review', 'failed', 'evidence_insufficient'"
        ),
    )


def downgrade() -> None:
    # 旧约束没有这个状态；退回最接近的 needs_review，规则执行与复核项保留不动
    op.execute(
        """
        UPDATE qc_aggregate_inspection_candidate
        SET status = 'needs_review', updated_at = now()
        WHERE status = 'evidence_insufficient'
        """
    )
    _replace_constraints(
        terminal_statuses="'not_applicable', 'needs_review', 'failed'",
        all_statuses=(
            "'expected', 'published', 'completed', 'not_applicable', 'needs_review', 'failed'"
        ),
    )
