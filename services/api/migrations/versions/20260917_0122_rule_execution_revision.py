"""规则执行结论被改写前先留底：补采后重判、重试改判都不再抹掉旧结论。

Revision ID: 20260917_0122
Revises: 20260916_0121
Create Date: 2026-09-17

09-17 审查复核（P1-10 遗留）：规则执行按「任务 + 规则 + 页面采集」只存一行，补采后重判整行覆盖，
旧结论只剩在判定尝试的原始载荷里。设计要求旧结论不可变、新结果以 revision 表达。
这里在结论字段（执行状态、结论摘要、问题描述、证据附件）变化时，把改写前的整行结论追加进
qc_rule_execution_revision；复核、人工结论、输入集合回写、操作记录关联这类不改结论的更新不留底。
观察包编号不作判断依据：观察包被删时外键会把它置空，那不是改判。历史行只追加，不能改写；
删任务时随规则执行级联删除。
"""

from __future__ import annotations

from alembic import op

revision = "20260917_0122"
down_revision = "20260916_0121"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE qc_rule_execution_revision (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            rule_execution_id uuid NOT NULL
                REFERENCES qc_rule_execution(id) ON DELETE CASCADE,
            revision_no integer NOT NULL,
            task_id uuid NOT NULL,
            rule_id uuid NOT NULL,
            page_capture_id uuid,
            agent_run_id uuid,
            observation_packet_id uuid,
            evidence_input_set_id varchar(96),
            execution_status varchar(32) NOT NULL,
            judge_strategy varchar(32) NOT NULL,
            confidence numeric(5, 4),
            severity varchar(32),
            result_summary text,
            problem_desc text,
            evidence_artifact_ids jsonb NOT NULL,
            judge_input jsonb NOT NULL,
            judge_output jsonb NOT NULL,
            review_status varchar(32),
            review_reason text,
            human_verdict varchar(16),
            human_verdict_note text,
            human_verdict_by varchar(128),
            human_verdict_at timestamptz,
            error_code varchar(128),
            error_message text,
            started_at timestamptz,
            finished_at timestamptz,
            superseded_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT uq_qc_rule_execution_revision_no
                UNIQUE (rule_execution_id, revision_no)
        )
        """
    )
    op.execute(
        """
        COMMENT ON TABLE qc_rule_execution_revision IS
        '规则执行结论被改写前的整行留底（补采后重判、重试改判），按 revision_no 从旧到新，只追加'
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION qc_keep_rule_execution_revision()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            INSERT INTO qc_rule_execution_revision (
                rule_execution_id, revision_no, task_id, rule_id, page_capture_id,
                agent_run_id, observation_packet_id, evidence_input_set_id,
                execution_status, judge_strategy, confidence, severity,
                result_summary, problem_desc, evidence_artifact_ids,
                judge_input, judge_output, review_status, review_reason,
                human_verdict, human_verdict_note, human_verdict_by, human_verdict_at,
                error_code, error_message, started_at, finished_at
            )
            SELECT
                OLD.id, COALESCE(max(revision.revision_no), 0) + 1, OLD.task_id,
                OLD.rule_id, OLD.page_capture_id,
                OLD.agent_run_id, OLD.observation_packet_id, OLD.evidence_input_set_id,
                OLD.execution_status, OLD.judge_strategy, OLD.confidence, OLD.severity,
                OLD.result_summary, OLD.problem_desc, OLD.evidence_artifact_ids,
                OLD.judge_input, OLD.judge_output, OLD.review_status, OLD.review_reason,
                OLD.human_verdict, OLD.human_verdict_note, OLD.human_verdict_by,
                OLD.human_verdict_at,
                OLD.error_code, OLD.error_message, OLD.started_at, OLD.finished_at
            FROM qc_rule_execution_revision AS revision
            WHERE revision.rule_execution_id = OLD.id;
            RETURN NULL;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_qc_rule_execution_keep_revision
        AFTER UPDATE ON qc_rule_execution
        FOR EACH ROW
        WHEN (
            OLD.execution_status IS DISTINCT FROM NEW.execution_status
            OR OLD.result_summary IS DISTINCT FROM NEW.result_summary
            OR OLD.problem_desc IS DISTINCT FROM NEW.problem_desc
            OR OLD.evidence_artifact_ids IS DISTINCT FROM NEW.evidence_artifact_ids
        )
        EXECUTE FUNCTION qc_keep_rule_execution_revision()
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION qc_guard_rule_execution_revision_immutable()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            RAISE EXCEPTION '规则结论历史不可改写';
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_qc_rule_execution_revision_immutable
        BEFORE UPDATE ON qc_rule_execution_revision
        FOR EACH ROW
        EXECUTE FUNCTION qc_guard_rule_execution_revision_immutable()
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS trg_qc_rule_execution_keep_revision ON qc_rule_execution"
    )
    op.execute("DROP TABLE IF EXISTS qc_rule_execution_revision")
    op.execute("DROP FUNCTION IF EXISTS qc_keep_rule_execution_revision()")
    op.execute("DROP FUNCTION IF EXISTS qc_guard_rule_execution_revision_immutable()")
