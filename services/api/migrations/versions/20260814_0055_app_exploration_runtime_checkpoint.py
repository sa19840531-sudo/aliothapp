"""增加 App 探索运行时恢复检查点及其 PostgreSQL 封存守卫。

检查点把 App Run 的 Session、构建、Bootstrap Capture、安装包、运行策略、
探索目标和时间窗口作为一条不可猜测的恢复事实保存；H5 不得写入此表。

Revision ID: 20260814_0055
Revises: 20260814_0054
Create Date: 2026-08-14
"""

from __future__ import annotations

from alembic import op

revision = "20260814_0055"
down_revision = "20260814_0054"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE qc_app_exploration_runtime_checkpoint (
            agent_run_id uuid PRIMARY KEY
                REFERENCES qc_agent_run(id) ON DELETE CASCADE,
            task_id uuid NOT NULL
                REFERENCES qc_task(id) ON DELETE CASCADE,
            surface_session_id varchar(128) NOT NULL,
            platform_build_ref varchar(255) NOT NULL,
            bootstrap_capture_id uuid NOT NULL
                REFERENCES qc_capture(id) ON DELETE RESTRICT,
            installed_package varchar(255) NOT NULL,
            protocol_version varchar(128) NOT NULL,
            runtime_policy jsonb NOT NULL,
            runtime_policy_sha256 varchar(128) NOT NULL,
            objective_hash varchar(128) NOT NULL,
            started_at timestamptz NOT NULL,
            deadline timestamptz NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT fk_qc_app_runtime_checkpoint_session
                FOREIGN KEY (task_id, surface_session_id)
                REFERENCES qc_surface_session (task_id, session_id)
                ON DELETE RESTRICT,
            CONSTRAINT ck_qc_app_runtime_checkpoint_text CHECK (
                BTRIM(surface_session_id) <> ''
                AND BTRIM(platform_build_ref) <> ''
                AND BTRIM(installed_package) <> ''
                AND BTRIM(protocol_version) <> ''
            ),
            CONSTRAINT ck_qc_app_runtime_checkpoint_policy CHECK (
                jsonb_typeof(runtime_policy) = 'object'
            ),
            CONSTRAINT ck_qc_app_runtime_checkpoint_policy_hash CHECK (
                runtime_policy_sha256 ~ '^sha256:[0-9a-f]{64}$'
            ),
            CONSTRAINT ck_qc_app_runtime_checkpoint_objective_hash CHECK (
                objective_hash ~ '^[0-9a-f]{64}$'
            ),
            CONSTRAINT ck_qc_app_runtime_checkpoint_window CHECK (
                deadline > started_at
            )
        )
        """
    )
    op.execute(
        """
        CREATE INDEX idx_qc_app_runtime_checkpoint_task_session
        ON qc_app_exploration_runtime_checkpoint (
            task_id, surface_session_id, updated_at DESC
        )
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION qc_guard_app_exploration_runtime_checkpoint()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            -- 业务代码不得单独删除封存检查点；父 Task/Run 的外键级联会形成嵌套触发器。
            IF TG_OP = 'DELETE' THEN
                IF pg_trigger_depth() <= 1 THEN
                    RAISE EXCEPTION
                        'App 探索运行时检查点已封存，禁止直接删除';
                END IF;
                RETURN OLD;
            END IF;

            -- 检查点是封存事实；只有完全相同的幂等 UPDATE 可以重放。
            IF TG_OP = 'UPDATE' AND (
                NEW.agent_run_id IS DISTINCT FROM OLD.agent_run_id
                OR NEW.task_id IS DISTINCT FROM OLD.task_id
                OR NEW.surface_session_id IS DISTINCT FROM OLD.surface_session_id
                OR NEW.platform_build_ref IS DISTINCT FROM OLD.platform_build_ref
                OR NEW.bootstrap_capture_id IS DISTINCT FROM OLD.bootstrap_capture_id
                OR NEW.installed_package IS DISTINCT FROM OLD.installed_package
                OR NEW.protocol_version IS DISTINCT FROM OLD.protocol_version
                OR NEW.runtime_policy IS DISTINCT FROM OLD.runtime_policy
                OR NEW.runtime_policy_sha256 IS DISTINCT FROM OLD.runtime_policy_sha256
                OR NEW.objective_hash IS DISTINCT FROM OLD.objective_hash
                OR NEW.started_at IS DISTINCT FROM OLD.started_at
                OR NEW.deadline IS DISTINCT FROM OLD.deadline
                OR NEW.created_at IS DISTINCT FROM OLD.created_at
                OR NEW.updated_at IS DISTINCT FROM OLD.updated_at
            ) THEN
                RAISE EXCEPTION
                    'App 探索运行时检查点已封存，身份、策略、目标和时间窗口禁止修改';
            END IF;

            -- 由 Run、Task、Session、Bootstrap Capture 和 APP_READY 组成完整 App 链。
            IF NOT EXISTS (
                SELECT 1
                FROM qc_agent_run AS run
                INNER JOIN qc_task AS task
                    ON task.id = run.task_id
                INNER JOIN qc_surface_session AS session
                    ON session.task_id = task.id
                   AND session.session_id = NEW.surface_session_id
                INNER JOIN qc_capture AS capture
                    ON capture.id = NEW.bootstrap_capture_id
                   AND capture.task_id = task.id
                   AND capture.agent_run_id = run.id
                   AND capture.surface_session_id = session.session_id
                INNER JOIN qc_app_bootstrap AS bootstrap
                    ON bootstrap.task_id = task.id
                   AND bootstrap.session_id = session.session_id
                   AND bootstrap.stage = 'app_ready'
                WHERE run.id = NEW.agent_run_id
                  AND run.task_id = NEW.task_id
                  AND task.id = NEW.task_id
                  AND task.target_platform = 'app'
                  AND session.surface_kind IN ('android', 'ios')
                  AND session.status = 'active'
                  AND session.platform_build_ref = NEW.platform_build_ref
                  AND session.platform_build_ref IS NOT NULL
                  AND capture.surface_kind = session.surface_kind
                  AND capture.capture_status = 'captured'
                  AND capture.capture_role = 'bootstrap'
                  AND bootstrap.platform_build_ref = NEW.platform_build_ref
                  AND bootstrap.bootstrap_capture_id = NEW.bootstrap_capture_id
                  AND bootstrap.installed_package = NEW.installed_package
            ) THEN
                RAISE EXCEPTION
                    'App 探索运行时检查点的 Run、Task、Session、构建、Bootstrap Capture 或安装包归属无效';
            END IF;

            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_qc_app_exploration_runtime_checkpoint_guard
        BEFORE INSERT OR UPDATE OR DELETE ON qc_app_exploration_runtime_checkpoint
        FOR EACH ROW EXECUTE FUNCTION qc_guard_app_exploration_runtime_checkpoint()
        """
    )
    op.execute(
        """
        COMMENT ON TABLE qc_app_exploration_runtime_checkpoint IS
        'App 探索运行时恢复检查点；Run、Session、Bootstrap 身份与策略封存后不可变'
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1
                FROM qc_app_exploration_runtime_checkpoint
            ) THEN
                RAISE EXCEPTION
                    '存在 App 探索运行时检查点事实，禁止降级 0054';
            END IF;
        END;
        $$
        """
    )
    op.execute(
        """
        DROP TRIGGER IF EXISTS trg_qc_app_exploration_runtime_checkpoint_guard
        ON qc_app_exploration_runtime_checkpoint
        """
    )
    op.execute(
        """
        DROP FUNCTION IF EXISTS qc_guard_app_exploration_runtime_checkpoint()
        """
    )
    op.execute(
        """
        DROP INDEX IF EXISTS idx_qc_app_runtime_checkpoint_task_session
        """
    )
    op.execute(
        "DROP TABLE IF EXISTS qc_app_exploration_runtime_checkpoint"
    )
