"""增加 App 专用 Agent Run 执行租约事实表与数据库守卫。

Revision ID: 20260814_0054
Revises: 20260814_0053
Create Date: 2026-08-13
"""

from __future__ import annotations

from alembic import op

revision = "20260814_0054"
down_revision = "20260814_0053"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE qc_app_run_execution_lease (
            agent_run_id uuid PRIMARY KEY
                REFERENCES qc_agent_run(id) ON DELETE CASCADE,
            task_id uuid NOT NULL
                REFERENCES qc_task(id) ON DELETE CASCADE,
            lease_owner varchar(255) NOT NULL,
            lease_token uuid NOT NULL,
            lease_expires_at timestamptz NOT NULL,
            row_version bigint NOT NULL DEFAULT 0,
            attempt_count integer NOT NULL DEFAULT 0,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT ck_qc_app_run_execution_lease_owner CHECK (
                BTRIM(lease_owner) <> ''
            ),
            CONSTRAINT ck_qc_app_run_execution_lease_counters CHECK (
                row_version >= 0
                AND attempt_count >= 0
            )
        )
        """
    )
    op.execute(
        """
        CREATE INDEX idx_qc_app_run_execution_lease_expiry
        ON qc_app_run_execution_lease (lease_expires_at)
        """
    )
    op.execute(
        """
        CREATE INDEX idx_qc_app_run_execution_lease_task
        ON qc_app_run_execution_lease (task_id, updated_at DESC)
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION qc_guard_app_run_execution_lease()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        DECLARE
            run_status varchar(32);
        BEGIN
            IF TG_OP = 'UPDATE' AND (
                NEW.agent_run_id IS DISTINCT FROM OLD.agent_run_id
                OR NEW.task_id IS DISTINCT FROM OLD.task_id
                OR NEW.created_at IS DISTINCT FROM OLD.created_at
            ) THEN
                RAISE EXCEPTION 'App Run 执行租约的 Run、任务和创建时间身份不可修改';
            END IF;

            SELECT run.status
            INTO run_status
            FROM qc_agent_run AS run
            INNER JOIN qc_task AS task
                ON task.id = run.task_id
            WHERE run.id = NEW.agent_run_id
              AND run.task_id = NEW.task_id
              AND task.target_platform = 'app'
            FOR UPDATE OF run;

            IF run_status IS NULL THEN
                RAISE EXCEPTION
                    'App Run 执行租约必须绑定同一任务的 App Agent Run';
            END IF;
            IF NEW.lease_expires_at <= now() THEN
                RAISE EXCEPTION 'App Run 执行租约到期时间必须晚于当前时间';
            END IF;

            IF TG_OP = 'INSERT' THEN
                IF run_status IN ('done', 'failed', 'aborted', 'canceled') THEN
                    RAISE EXCEPTION
                        '终态 Agent Run 不允许新建 App 执行租约';
                END IF;
            ELSE
                IF NEW.row_version <> OLD.row_version + 1 THEN
                    RAISE EXCEPTION
                        'App Run 执行租约 UPDATE 必须递增 row_version';
                END IF;
                IF (
                    NEW.lease_owner IS DISTINCT FROM OLD.lease_owner
                    OR NEW.lease_token IS DISTINCT FROM OLD.lease_token
                ) THEN
                    IF OLD.lease_expires_at > now() THEN
                        RAISE EXCEPTION
                            '活跃 App Run 执行租约禁止被其他持有者接管';
                    END IF;
                    IF NEW.attempt_count <> OLD.attempt_count + 1 THEN
                        RAISE EXCEPTION
                            'App Run 执行租约接管必须递增 attempt_count';
                    END IF;
                ELSE
                    IF OLD.lease_expires_at <= now() THEN
                        RAISE EXCEPTION
                            '已过期 App Run 执行租约不得以原 token 恢复续租';
                    END IF;
                    IF NEW.attempt_count <> OLD.attempt_count THEN
                        RAISE EXCEPTION
                            '同一 App Run 执行租约续租不得改变 attempt_count';
                    END IF;
                END IF;
                IF run_status IN ('done', 'failed', 'aborted', 'canceled') AND (
                    NEW.lease_owner IS DISTINCT FROM OLD.lease_owner
                    OR NEW.lease_token IS DISTINCT FROM OLD.lease_token
                    OR NEW.attempt_count IS DISTINCT FROM OLD.attempt_count
                ) THEN
                    RAISE EXCEPTION
                        '终态 Agent Run 只允许原持有者续租以完成终态交接';
                END IF;
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_qc_app_run_execution_lease_guard
        BEFORE INSERT OR UPDATE ON qc_app_run_execution_lease
        FOR EACH ROW EXECUTE FUNCTION qc_guard_app_run_execution_lease()
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_qc_app_run_execution_lease_updated_at
        BEFORE UPDATE ON qc_app_run_execution_lease
        FOR EACH ROW EXECUTE FUNCTION set_updated_at()
        """
    )
    op.execute(
        """
        COMMENT ON TABLE qc_app_run_execution_lease IS
        'App Agent Run 的 PostgreSQL 执行租约事实；终态不得新领，原持有者可续租完成交接'
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1
                FROM qc_app_run_execution_lease
                WHERE lease_expires_at > now()
            ) THEN
                RAISE EXCEPTION
                    '存在活跃 App Run 执行租约，禁止降级 0053';
            END IF;
            IF EXISTS (SELECT 1 FROM qc_app_run_execution_lease) THEN
                RAISE EXCEPTION
                    '存在已过期 App Run 执行租约，禁止静默清理后降级 0053';
            END IF;
        END;
        $$
        """
    )
    op.execute(
        """
        DROP TRIGGER IF EXISTS trg_qc_app_run_execution_lease_updated_at
        ON qc_app_run_execution_lease
        """
    )
    op.execute(
        """
        DROP TRIGGER IF EXISTS trg_qc_app_run_execution_lease_guard
        ON qc_app_run_execution_lease
        """
    )
    op.execute("DROP FUNCTION IF EXISTS qc_guard_app_run_execution_lease()")
    op.execute("DROP INDEX IF EXISTS idx_qc_app_run_execution_lease_task")
    op.execute("DROP INDEX IF EXISTS idx_qc_app_run_execution_lease_expiry")
    op.execute("DROP TABLE IF EXISTS qc_app_run_execution_lease")
