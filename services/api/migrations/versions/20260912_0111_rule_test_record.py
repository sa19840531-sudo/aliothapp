"""规则测试记录：每次人工证据测试的结论和当时证据留档，可查看、重测、批量删除。

Revision ID: 20260912_0111
Revises: 20260911_0109
Create Date: 2026-09-11

- 结论存整份测试结果；页面文字、源码、界面快照、补充判定输入存库，截图存证据存储
  （路径 rule-tests/<记录>/screenshot-N.png，只在这张表里登记）。
- 不自动清理，由体验中心管理员按勾选或按时间段批量删除。与正式任务、报告的表互不相干。
"""

from __future__ import annotations

from alembic import op

revision = "20260912_0111"
down_revision = "20260912_0110"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE qc_rule_test_record (
            id uuid PRIMARY KEY,
            rule_id uuid REFERENCES qc_rule(id) ON DELETE SET NULL,
            rule_code varchar(128) NOT NULL,
            rule_name varchar(255) NOT NULL,
            tester_user_id uuid REFERENCES qc_user(id) ON DELETE SET NULL,
            tester_name varchar(128) NOT NULL,
            platform varchar(16) NOT NULL,
            check_mode varchar(16) NOT NULL,
            page_type varchar(64) NOT NULL DEFAULT '',
            page_state varchar(64) NOT NULL DEFAULT '',
            page_url text NOT NULL DEFAULT '',
            page_title text NOT NULL DEFAULT '',
            visible_text text NOT NULL DEFAULT '',
            dom_html text NOT NULL DEFAULT '',
            ui_snapshot jsonb,
            judge_input jsonb NOT NULL DEFAULT '{}'::jsonb,
            screenshots jsonb NOT NULL DEFAULT '[]'::jsonb,
            outcome varchar(32) NOT NULL,
            result_summary text NOT NULL DEFAULT '',
            result_payload jsonb NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT ck_qc_rule_test_record_platform CHECK (
                platform IN ('h5', 'web', 'android', 'ios')
            ),
            CONSTRAINT ck_qc_rule_test_record_check_mode CHECK (
                check_mode IN ('relaxed', 'strict')
            ),
            CONSTRAINT ck_qc_rule_test_record_json CHECK (
                jsonb_typeof(judge_input) = 'object'
                AND jsonb_typeof(screenshots) = 'array'
                AND jsonb_typeof(result_payload) = 'object'
                AND (ui_snapshot IS NULL OR jsonb_typeof(ui_snapshot) = 'object')
            )
        )
        """
    )
    op.execute(
        "CREATE INDEX idx_qc_rule_test_record_created ON qc_rule_test_record (created_at DESC)"
    )
    op.execute(
        """
        CREATE INDEX idx_qc_rule_test_record_rule
        ON qc_rule_test_record (rule_id, created_at DESC)
        """
    )
    op.execute(
        """
        COMMENT ON TABLE qc_rule_test_record IS
        '规则配置里人工证据测试的留档；截图在证据存储 rule-tests/<id>/ 下，删记录时一并删除'
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS qc_rule_test_record")
