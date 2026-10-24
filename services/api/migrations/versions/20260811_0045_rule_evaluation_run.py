"""增加独立规则质检运行账本。

与正式规则结果（qc_rule_execution）物理隔离：独立规则质检基于既有证据重跑判定，
结果只写入本表，报告中心、Completion Gate 与审核链路不读取本表。

Revision ID: 20260811_0045
Revises: 20260810_0044
Create Date: 2026-08-11
"""

from __future__ import annotations

from alembic import op

revision = "20260811_0045"
down_revision = "20260810_0044"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE qc_rule_evaluation_run (
            run_id varchar(96) PRIMARY KEY,
            task_id uuid NOT NULL REFERENCES qc_task(id) ON DELETE CASCADE,
            status varchar(32) NOT NULL DEFAULT 'pending',
            page_count integer NOT NULL DEFAULT 0,
            rule_count integer NOT NULL DEFAULT 0,
            requested_rule_codes jsonb NOT NULL DEFAULT '[]'::jsonb,
            result_payload jsonb NOT NULL DEFAULT '[]'::jsonb,
            error_message text,
            requested_by varchar(128),
            created_at timestamptz NOT NULL DEFAULT now(),
            started_at timestamptz,
            finished_at timestamptz,
            updated_at timestamptz NOT NULL DEFAULT now(),
            row_version bigint NOT NULL DEFAULT 0,
            CONSTRAINT ck_qc_rule_evaluation_run_status CHECK (
                status IN (
                    'pending',
                    'running',
                    'succeeded',
                    'failed',
                    'evidence_insufficient',
                    'cancelled'
                )
            ),
            CONSTRAINT ck_qc_rule_evaluation_run_numbers CHECK (
                page_count >= 0 AND rule_count >= 0 AND row_version >= 0
            ),
            CONSTRAINT ck_qc_rule_evaluation_run_payload CHECK (
                jsonb_typeof(result_payload) = 'array'
                AND jsonb_typeof(requested_rule_codes) = 'array'
            ),
            CONSTRAINT ck_qc_rule_evaluation_run_terminal CHECK (
                (
                    status IN ('pending', 'running')
                    AND finished_at IS NULL
                )
                OR (
                    status IN (
                        'succeeded', 'failed', 'evidence_insufficient', 'cancelled'
                    )
                    AND finished_at IS NOT NULL
                )
            )
        )
        """
    )
    op.execute(
        """
        CREATE INDEX idx_qc_rule_evaluation_run_task
        ON qc_rule_evaluation_run (task_id, created_at DESC)
        """
    )
    op.execute(
        "COMMENT ON TABLE qc_rule_evaluation_run IS "
        "'独立规则质检运行账本：基于既有证据重跑判定，结果与正式规则结果物理隔离'"
    )
    op.execute(
        "COMMENT ON COLUMN qc_rule_evaluation_run.status IS "
        "'运行状态：pending/running 在途；succeeded/failed/evidence_insufficient/cancelled 终态'"
    )
    op.execute(
        "COMMENT ON COLUMN qc_rule_evaluation_run.result_payload IS "
        "'逐规则判定结果数组，字段沿用统一质检结果格式词汇（execution_status 等）'"
    )
    op.execute(
        "COMMENT ON COLUMN qc_rule_evaluation_run.error_message IS 'failed 时的失败原因摘要'"
    )
    op.execute(
        "COMMENT ON COLUMN qc_rule_evaluation_run.requested_rule_codes IS "
        "'本次指定测试的规则编码数组；空数组表示测全部命中规则'"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_qc_rule_evaluation_run_task")
    op.execute("DROP TABLE IF EXISTS qc_rule_evaluation_run")
