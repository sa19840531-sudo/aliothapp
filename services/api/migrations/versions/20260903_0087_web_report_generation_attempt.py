"""增加 Web 正式报告生成 attempt 审计账本。

Revision ID: 20260903_0087
Revises: 20260903_0086
Create Date: 2026-09-03
"""

from __future__ import annotations

from alembic import op

revision = "20260903_0087"
down_revision = "20260903_0086"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """建立 Web 正式报告每次真实生成调用的持久审计事实。"""

    op.execute(
        """
        CREATE TABLE qc_web_report_generation_attempt (
            id uuid PRIMARY KEY,
            task_id uuid NOT NULL REFERENCES qc_task(id) ON DELETE RESTRICT,
            agent_run_id uuid NOT NULL
                REFERENCES qc_agent_run(id) ON DELETE RESTRICT,
            finalization_epoch integer NOT NULL,
            attempt_number integer NOT NULL,
            status varchar(16) NOT NULL DEFAULT 'running',
            failure_code varchar(128),
            started_at timestamptz NOT NULL DEFAULT clock_timestamp(),
            finished_at timestamptz,
            CONSTRAINT uq_qc_web_report_generation_attempt_sequence
                UNIQUE (task_id, agent_run_id, finalization_epoch, attempt_number),
            CONSTRAINT ck_qc_web_report_generation_attempt_numbers CHECK (
                finalization_epoch >= 0 AND attempt_number > 0
            ),
            CONSTRAINT ck_qc_web_report_generation_attempt_status CHECK (
                status IN ('running', 'succeeded', 'failed', 'cancelled')
            ),
            CONSTRAINT ck_qc_web_report_generation_attempt_terminal CHECK (
                (
                    status = 'running'
                    AND finished_at IS NULL
                    AND failure_code IS NULL
                )
                OR
                (
                    status = 'succeeded'
                    AND finished_at IS NOT NULL
                    AND failure_code IS NULL
                )
                OR
                (
                    status IN ('failed', 'cancelled')
                    AND finished_at IS NOT NULL
                    AND failure_code IS NOT NULL
                )
            ),
            CONSTRAINT ck_qc_web_report_generation_attempt_time_order CHECK (
                finished_at IS NULL OR finished_at >= started_at
            )
        )
        """
    )
    op.execute(
        """
        CREATE INDEX idx_qc_web_report_generation_attempt_stage
        ON qc_web_report_generation_attempt (
            task_id,
            agent_run_id,
            finalization_epoch,
            started_at,
            finished_at
        )
        """
    )
    op.execute(
        """
        CREATE FUNCTION qc_validate_web_report_generation_attempt()
        RETURNS trigger AS $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1
                FROM qc_task AS task
                INNER JOIN qc_agent_run AS run
                  ON run.id = NEW.agent_run_id
                 AND run.task_id = task.id
                 AND run.agent_type = 'main'
                WHERE task.id = NEW.task_id
                  AND task.target_platform = 'web'
                  AND LOWER(
                      COALESCE(task.task_config->>'workflow_version', '')
                  ) = 'v2'
                  AND task.task_config->>'execution_mode' = 'full_inspection'
                  AND task.finalization_epoch = NEW.finalization_epoch
            ) THEN
                RAISE EXCEPTION
                    'web report generation attempt identity is invalid';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_qc_web_report_generation_attempt_validate
        BEFORE INSERT ON qc_web_report_generation_attempt
        FOR EACH ROW EXECUTE FUNCTION qc_validate_web_report_generation_attempt()
        """
    )
    op.execute(
        """
        CREATE FUNCTION qc_guard_web_report_generation_attempt_mutation()
        RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'web report generation attempt is immutable';
            END IF;
            IF OLD.status <> 'running'
                OR NEW.id IS DISTINCT FROM OLD.id
                OR NEW.task_id IS DISTINCT FROM OLD.task_id
                OR NEW.agent_run_id IS DISTINCT FROM OLD.agent_run_id
                OR NEW.finalization_epoch IS DISTINCT FROM OLD.finalization_epoch
                OR NEW.attempt_number IS DISTINCT FROM OLD.attempt_number
                OR NEW.started_at IS DISTINCT FROM OLD.started_at
                OR NEW.status NOT IN ('succeeded', 'failed', 'cancelled')
            THEN
                RAISE EXCEPTION 'web report generation attempt mutation is invalid';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_qc_web_report_generation_attempt_guard
        BEFORE UPDATE OR DELETE ON qc_web_report_generation_attempt
        FOR EACH ROW EXECUTE FUNCTION qc_guard_web_report_generation_attempt_mutation()
        """
    )
    op.execute(
        """
        COMMENT ON TABLE qc_web_report_generation_attempt IS
        'Web V2 正式报告每次真实 generate 调用的不可覆盖审计账本'
        """
    )
    op.execute(
        """
        COMMENT ON COLUMN qc_web_report_generation_attempt.attempt_number IS
        '同 task、main run、finalization epoch 内的串行调用序号'
        """
    )
    op.execute(
        """
        COMMENT ON COLUMN qc_web_report_generation_attempt.failure_code IS
        '失败或取消时的异常类型；不保存可能含敏感信息的异常正文'
        """
    )


def downgrade() -> None:
    """有审计事实时拒绝破坏性降级。"""

    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM qc_web_report_generation_attempt LIMIT 1
            ) THEN
                RAISE EXCEPTION
                    'cannot downgrade web report generation audit while rows exist';
            END IF;
        END
        $$
        """
    )
    op.execute(
        "DROP TRIGGER IF EXISTS trg_qc_web_report_generation_attempt_guard "
        "ON qc_web_report_generation_attempt"
    )
    op.execute("DROP FUNCTION IF EXISTS qc_guard_web_report_generation_attempt_mutation()")
    op.execute(
        "DROP TRIGGER IF EXISTS trg_qc_web_report_generation_attempt_validate "
        "ON qc_web_report_generation_attempt"
    )
    op.execute("DROP FUNCTION IF EXISTS qc_validate_web_report_generation_attempt()")
    op.execute("DROP INDEX IF EXISTS idx_qc_web_report_generation_attempt_stage")
    op.execute("DROP TABLE qc_web_report_generation_attempt")
