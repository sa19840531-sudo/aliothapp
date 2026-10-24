"""增加 Web/App 共用 Capture 身份和 App Capture 子表。

Revision ID: 20260804_0034
Revises: 20260804_0033
Create Date: 2026-08-04
"""

from __future__ import annotations

from alembic import op

revision = "20260804_0034"
down_revision = "20260804_0033"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE qc_capture (
            id uuid PRIMARY KEY,
            task_id uuid NOT NULL REFERENCES qc_task(id) ON DELETE CASCADE,
            agent_run_id uuid REFERENCES qc_agent_run(id) ON DELETE SET NULL,
            surface_kind varchar(16) NOT NULL,
            surface_session_id varchar(128),
            parent_capture_id uuid,
            action_execution_id varchar(96),
            capture_role varchar(32) NOT NULL,
            session_command_sequence bigint,
            capture_status varchar(32) NOT NULL DEFAULT 'pending',
            captured_at timestamptz,
            metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT fk_qc_capture_parent FOREIGN KEY (parent_capture_id)
                REFERENCES qc_capture(id) ON DELETE SET NULL
                DEFERRABLE INITIALLY DEFERRED,
            CONSTRAINT ck_qc_capture_surface CHECK (
                surface_kind IN ('web', 'android', 'ios')
            ),
            CONSTRAINT ck_qc_capture_role CHECK (
                capture_role IN (
                    'bootstrap', 'before_action', 'after_action',
                    'observation', 'supplemental'
                )
            ),
            CONSTRAINT ck_qc_capture_status CHECK (
                capture_status IN ('pending', 'captured', 'failed', 'skipped')
            ),
            CONSTRAINT ck_qc_capture_command_sequence CHECK (
                session_command_sequence IS NULL OR session_command_sequence >= 0
            )
        )
        """
    )
    op.execute(
        """
        INSERT INTO qc_capture (
            id,
            task_id,
            agent_run_id,
            surface_kind,
            surface_session_id,
            parent_capture_id,
            action_execution_id,
            capture_role,
            session_command_sequence,
            capture_status,
            captured_at,
            metadata,
            created_at,
            updated_at
        )
        SELECT
            page.id,
            page.task_id,
            page.agent_run_id,
            'web',
            page.surface_session_id,
            page.parent_page_capture_id,
            page.action_execution_id,
            'observation',
            page.session_command_sequence,
            page.capture_status,
            page.captured_at,
            page.metadata,
            page.created_at,
            page.updated_at
        FROM qc_page_capture AS page
        ON CONFLICT (id) DO NOTHING
        """
    )
    # qc_capture 自带自引用延迟外键 fk_qc_capture_parent（见上方建表），
    # 上面的回填 INSERT 会为每一行排入待决约束检查；只要还有待决触发事件，
    # PostgreSQL 就拒绝在该表上建索引
    # （cannot CREATE INDEX because it has pending trigger events）。
    # 空库无行可回填故不触发，有存量数据必然失败，因此先强制结算延迟约束。
    op.execute("SET CONSTRAINTS ALL IMMEDIATE")
    op.execute(
        """
        CREATE INDEX idx_qc_capture_task
        ON qc_capture (task_id, surface_kind, created_at)
        """
    )
    op.execute(
        """
        CREATE INDEX idx_qc_capture_session
        ON qc_capture (surface_session_id, session_command_sequence)
        WHERE surface_session_id IS NOT NULL
        """
    )
    op.execute(
        """
        ALTER TABLE qc_page_capture
        ADD CONSTRAINT fk_qc_page_capture_common_identity
            FOREIGN KEY (id) REFERENCES qc_capture(id) ON DELETE CASCADE
            DEFERRABLE INITIALLY DEFERRED
        """
    )
    op.execute(
        """
        CREATE TABLE qc_app_capture (
            capture_id uuid PRIMARY KEY
                REFERENCES qc_capture(id) ON DELETE CASCADE,
            screenshot_artifact_id uuid NOT NULL
                REFERENCES qc_evidence_artifact(id) ON DELETE RESTRICT,
            package_name varchar(512),
            activity varchar(512),
            width integer NOT NULL,
            height integer NOT NULL,
            orientation varchar(16) NOT NULL DEFAULT 'unknown',
            image_signature varchar(128) NOT NULL,
            device_snapshot jsonb NOT NULL DEFAULT '{}'::jsonb,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT ck_qc_app_capture_dimensions CHECK (
                width > 0 AND height > 0
            ),
            CONSTRAINT ck_qc_app_capture_orientation CHECK (
                orientation IN ('portrait', 'landscape', 'unknown')
            ),
            CONSTRAINT ck_qc_app_capture_signature CHECK (
                BTRIM(image_signature) <> ''
            )
        )
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION qc_create_page_capture_parent()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF NEW.id IS NULL THEN
                NEW.id := gen_random_uuid();
            END IF;
            INSERT INTO qc_capture (
                id,
                task_id,
                agent_run_id,
                surface_kind,
                surface_session_id,
                parent_capture_id,
                action_execution_id,
                capture_role,
                session_command_sequence,
                capture_status,
                captured_at,
                metadata,
                created_at,
                updated_at
            )
            VALUES (
                NEW.id,
                NEW.task_id,
                NEW.agent_run_id,
                'web',
                NEW.surface_session_id,
                NEW.parent_page_capture_id,
                NEW.action_execution_id,
                'observation',
                NEW.session_command_sequence,
                NEW.capture_status,
                NEW.captured_at,
                NEW.metadata,
                NEW.created_at,
                NEW.updated_at
            )
            ON CONFLICT (id) DO NOTHING;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION qc_update_page_capture_parent()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            UPDATE qc_capture
            SET
                task_id = NEW.task_id,
                agent_run_id = NEW.agent_run_id,
                surface_session_id = NEW.surface_session_id,
                parent_capture_id = NEW.parent_page_capture_id,
                action_execution_id = NEW.action_execution_id,
                session_command_sequence = NEW.session_command_sequence,
                capture_status = NEW.capture_status,
                captured_at = NEW.captured_at,
                metadata = NEW.metadata,
                updated_at = NEW.updated_at
            WHERE id = NEW.id;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_qc_page_capture_parent_insert
        BEFORE INSERT ON qc_page_capture
        FOR EACH ROW EXECUTE FUNCTION qc_create_page_capture_parent()
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_qc_page_capture_parent_update
        AFTER UPDATE ON qc_page_capture
        FOR EACH ROW EXECUTE FUNCTION qc_update_page_capture_parent()
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM qc_app_capture) THEN
                RAISE EXCEPTION '存在 App Capture 事实，不能删除公共 Capture 身份';
            END IF;
        END;
        $$
        """
    )
    op.execute("DROP INDEX IF EXISTS idx_qc_capture_session")
    op.execute("DROP INDEX IF EXISTS idx_qc_capture_task")
    op.execute(
        "DROP TRIGGER IF EXISTS trg_qc_page_capture_parent_update ON qc_page_capture"
    )
    op.execute(
        "DROP TRIGGER IF EXISTS trg_qc_page_capture_parent_insert ON qc_page_capture"
    )
    op.execute("DROP FUNCTION IF EXISTS qc_update_page_capture_parent()")
    op.execute("DROP FUNCTION IF EXISTS qc_create_page_capture_parent()")
    op.execute("DROP TABLE IF EXISTS qc_app_capture")
    op.execute(
        """
        ALTER TABLE qc_page_capture
        DROP CONSTRAINT IF EXISTS fk_qc_page_capture_common_identity
        """
    )
    op.execute("DROP TABLE IF EXISTS qc_capture")
