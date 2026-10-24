"""对齐 Web Page Capture 与公共 Capture 的证据角色。

Revision ID: 20260812_0048
Revises: 20260812_0047
Create Date: 2026-08-12
"""

from __future__ import annotations

from alembic import op

revision = "20260812_0048"
down_revision = "20260812_0047"
branch_labels = None
depends_on = None

_ROLE_VALUES = (
    "'primary', 'before_action', 'after_action', 'bootstrap', "
    "'probe_before', 'probe_result_replay', 'shadow_candidate', "
    "'refreshed', 'observation', 'supplemental'"
)


def upgrade() -> None:
    op.execute(
        f"""
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1
                FROM qc_page_capture
                WHERE capture_role NOT IN ({_ROLE_VALUES})
            ) THEN
                RAISE EXCEPTION
                    'qc_page_capture contains roles outside the frozen supply vocabulary';
            END IF;
        END
        $$
        """
    )
    op.execute("ALTER TABLE qc_capture DROP CONSTRAINT ck_qc_capture_role")
    op.execute(
        f"""
        ALTER TABLE qc_capture
        ADD CONSTRAINT ck_qc_capture_role
        CHECK (capture_role IN ({_ROLE_VALUES}))
        """
    )
    # 只在页面采集确实带角色时对齐：V1 链路没有 Surface Session，
    # page_capture_repository 对 capture_role 写的是 NULL
    # （CASE WHEN session.session_id IS NOT NULL THEN ... ELSE NULL END），
    # 直接抄过来会撞 qc_capture.capture_role 的 NOT NULL 约束。
    # 上方词汇守卫也拦不住这种行——NULL NOT IN (...) 求值为 NULL 而非 TRUE。
    # 角色缺失的存量行保留 0034 回填的 'observation' 占位值。
    op.execute(
        """
        UPDATE qc_capture AS common_capture
        SET capture_role = page_capture.capture_role,
            updated_at = now()
        FROM qc_page_capture AS page_capture
        WHERE common_capture.id = page_capture.id
          AND page_capture.capture_role IS NOT NULL
          AND common_capture.capture_role IS DISTINCT FROM page_capture.capture_role
        """
    )
    _install_aligned_triggers()


def downgrade() -> None:
    _install_legacy_triggers()
    op.execute(
        """
        UPDATE qc_capture AS common_capture
        SET capture_role = 'observation',
            updated_at = now()
        FROM qc_page_capture AS page_capture
        WHERE common_capture.id = page_capture.id
          AND common_capture.capture_role <> 'observation'
        """
    )
    op.execute("ALTER TABLE qc_capture DROP CONSTRAINT ck_qc_capture_role")
    op.execute(
        """
        ALTER TABLE qc_capture
        ADD CONSTRAINT ck_qc_capture_role CHECK (
            capture_role IN (
                'bootstrap', 'before_action', 'after_action', 'observation', 'supplemental'
            )
        )
        """
    )


def _install_aligned_triggers() -> None:
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
                id, task_id, agent_run_id, surface_kind, surface_session_id,
                parent_capture_id, action_execution_id, capture_role,
                session_command_sequence, capture_status, captured_at,
                metadata, created_at, updated_at
            )
            VALUES (
                NEW.id, NEW.task_id, NEW.agent_run_id, 'web', NEW.surface_session_id,
                NEW.parent_page_capture_id, NEW.action_execution_id,
                -- qc_page_capture.capture_role 可为空：V1 链路没有 Surface Session 时
                -- page_capture_repository 写 NULL；qc_capture.capture_role 是 NOT NULL，
                -- 直接透传会让 V1 页面采集整体写不进去，故沿用 0034 的占位语义兜底。
                COALESCE(NEW.capture_role, 'observation'),
                NEW.session_command_sequence, NEW.capture_status, NEW.captured_at,
                NEW.metadata, NEW.created_at, NEW.updated_at
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
            SET task_id = NEW.task_id,
                agent_run_id = NEW.agent_run_id,
                surface_session_id = NEW.surface_session_id,
                parent_capture_id = NEW.parent_page_capture_id,
                action_execution_id = NEW.action_execution_id,
                -- 同 INSERT 触发器：角色缺失时保留占位值，不写 NULL
                capture_role = COALESCE(NEW.capture_role, qc_capture.capture_role),
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


def _install_legacy_triggers() -> None:
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
                id, task_id, agent_run_id, surface_kind, surface_session_id,
                parent_capture_id, action_execution_id, capture_role,
                session_command_sequence, capture_status, captured_at,
                metadata, created_at, updated_at
            )
            VALUES (
                NEW.id, NEW.task_id, NEW.agent_run_id, 'web', NEW.surface_session_id,
                NEW.parent_page_capture_id, NEW.action_execution_id, 'observation',
                NEW.session_command_sequence, NEW.capture_status, NEW.captured_at,
                NEW.metadata, NEW.created_at, NEW.updated_at
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
            SET task_id = NEW.task_id,
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
