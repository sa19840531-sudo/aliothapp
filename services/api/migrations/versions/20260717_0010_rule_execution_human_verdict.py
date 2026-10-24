"""规则执行结果增加人工确认终审字段。

新版「报告中心」把人工确认下沉到报告页：机器结果为"未识别到 / 识别异常"的规则
由人工给出终审结论（通过 / 未通过 / 忽略，忽略=该规则无需质检）。终审结论独立于
既有 review 复核流（qc_review_item / review_status 语义不变），写在执行行自身上，
报告导出以"待确认清零"为门禁。

Revision ID: 20260717_0010
Revises: 20260717_0009
Create Date: 2026-07-17
"""

from __future__ import annotations

from alembic import op

revision = "20260717_0010"
down_revision = "20260717_0009"
branch_labels = None
depends_on = None

_UPGRADE_STATEMENTS = [
    """
    ALTER TABLE qc_rule_execution
        ADD COLUMN human_verdict varchar(16),
        ADD COLUMN human_verdict_note text,
        ADD COLUMN human_verdict_by varchar(128),
        ADD COLUMN human_verdict_at timestamptz
    """,
    """
    ALTER TABLE qc_rule_execution
        ADD CONSTRAINT ck_qc_rule_execution_human_verdict CHECK (
            human_verdict IS NULL OR human_verdict IN ('pass', 'fail', 'ignored')
        )
    """,
    """
    COMMENT ON COLUMN qc_rule_execution.human_verdict IS
    '人工确认终审结论：pass 通过 / fail 未通过 / ignored 忽略（无需质检）；NULL=未确认'
    """,
    """
    COMMENT ON COLUMN qc_rule_execution.human_verdict_note IS '人工确认备注'
    """,
    """
    COMMENT ON COLUMN qc_rule_execution.human_verdict_by IS '确认人标识（预留，当前未接登录态）'
    """,
    """
    COMMENT ON COLUMN qc_rule_execution.human_verdict_at IS '人工确认时间'
    """,
]

_DOWNGRADE_STATEMENTS = [
    """
    ALTER TABLE qc_rule_execution
        DROP CONSTRAINT IF EXISTS ck_qc_rule_execution_human_verdict
    """,
    "ALTER TABLE qc_rule_execution DROP COLUMN IF EXISTS human_verdict_at",
    "ALTER TABLE qc_rule_execution DROP COLUMN IF EXISTS human_verdict_by",
    "ALTER TABLE qc_rule_execution DROP COLUMN IF EXISTS human_verdict_note",
    "ALTER TABLE qc_rule_execution DROP COLUMN IF EXISTS human_verdict",
]


def upgrade() -> None:
    for statement in _UPGRADE_STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    for statement in _DOWNGRADE_STATEMENTS:
        op.execute(statement)
