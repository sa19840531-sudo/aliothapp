"""反馈工单表：部门账号在工作台提交误报/漏报/规则建议/平台问题。

Revision ID: 20260908_0100
Revises: 20260908_0099
Create Date: 2026-09-08

2.0 原型的「我的反馈 / 反馈中心」需要一个真实的工单模型：提交人、所属公司、
类型、优先级、关联任务与规则、处理状态与回复。工单挂在公司（租户）与提交账号
上，任务与规则只记编号不建外键，避免历史任务清理时牵连反馈记录。
"""

from __future__ import annotations

from alembic import op

revision = "20260908_0100"
down_revision = "20260908_0099"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """建反馈工单表与编号序列。"""

    op.execute("CREATE SEQUENCE IF NOT EXISTS qc_feedback_ticket_no_seq START 1")
    op.execute(
        """
        CREATE TABLE qc_feedback_ticket (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            ticket_no varchar(32) NOT NULL,
            tenant_id uuid NOT NULL REFERENCES qc_tenant(id),
            submitter_id uuid NOT NULL REFERENCES qc_user(id),
            submitter_name varchar(128) NOT NULL,
            kind varchar(32) NOT NULL,
            priority varchar(16) NOT NULL DEFAULT 'medium',
            title varchar(200) NOT NULL,
            description text NOT NULL,
            task_id uuid NULL,
            rule_code varchar(64) NULL,
            contact varchar(128) NULL,
            status varchar(16) NOT NULL DEFAULT 'pending',
            reply text NULL,
            handled_by varchar(128) NULL,
            handled_at timestamptz NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT uq_qc_feedback_ticket_no UNIQUE (ticket_no),
            CONSTRAINT ck_qc_feedback_ticket_kind CHECK (
                kind IN ('false_positive', 'missed', 'rule_suggestion', 'platform_issue', 'other')
            ),
            CONSTRAINT ck_qc_feedback_ticket_priority CHECK (
                priority IN ('urgent', 'high', 'medium', 'low')
            ),
            CONSTRAINT ck_qc_feedback_ticket_status CHECK (
                status IN ('pending', 'processing', 'resolved', 'closed')
            )
        )
        """
    )
    op.execute(
        """
        CREATE INDEX ix_qc_feedback_ticket_tenant_created
            ON qc_feedback_ticket (tenant_id, created_at DESC)
        """
    )
    op.execute(
        """
        CREATE INDEX ix_qc_feedback_ticket_submitter_created
            ON qc_feedback_ticket (submitter_id, created_at DESC)
        """
    )


def downgrade() -> None:
    """删表与序列。"""

    op.execute("DROP TABLE IF EXISTS qc_feedback_ticket")
    op.execute("DROP SEQUENCE IF EXISTS qc_feedback_ticket_no_seq")
