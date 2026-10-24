"""拆分 App Publication 历史混合失败码。

Revision ID: 20260823_0076
Revises: 20260822_0075
Create Date: 2026-08-23

旧版本把未识别页型和活会话规则未完成都写成
active_session_or_page_type_unavailable，导致任务洞察无法区分真实阻断。
本迁移按 Publication Rule 的持久化事实回填，不猜测页面业务语义。
"""

from __future__ import annotations

from alembic import op

revision = "20260823_0076"
down_revision = "20260822_0075"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """有未解决活会话规则时标为规则阻断，否则标为页型不可用。"""

    op.execute(
        """
        UPDATE qc_app_inspection_publication_entry AS entry
        SET failure_code = CASE
                WHEN EXISTS (
                    SELECT 1
                    FROM qc_app_inspection_publication_rule AS member
                    WHERE member.publication_id = entry.publication_id
                      AND member.entry_id = entry.entry_id
                      AND member.session_dependency = 'active_session'
                      AND member.disposition = 'needs_review'
                )
                THEN 'active_session_rules_unresolved'
                ELSE 'page_type_unavailable'
            END,
            failure_message = CASE
                WHEN EXISTS (
                    SELECT 1
                    FROM qc_app_inspection_publication_rule AS member
                    WHERE member.publication_id = entry.publication_id
                      AND member.entry_id = entry.entry_id
                      AND member.session_dependency = 'active_session'
                      AND member.disposition = 'needs_review'
                )
                THEN 'App Session 关闭前仍有活会话规则未形成可信终态'
                ELSE 'App Capture 页面类型不可识别，无法匹配候选规则'
            END,
            updated_at = now()
        WHERE entry.failure_code = 'active_session_or_page_type_unavailable'
        """
    )


def downgrade() -> None:
    """回退旧代码时恢复其唯一可识别的混合失败码。"""

    op.execute(
        """
        UPDATE qc_app_inspection_publication_entry
        SET failure_code = 'active_session_or_page_type_unavailable',
            failure_message = 'App 活会话规则未完成或页面类型不可识别',
            updated_at = now()
        WHERE failure_code IN (
            'active_session_rules_unresolved',
            'page_type_unavailable'
        )
        """
    )
