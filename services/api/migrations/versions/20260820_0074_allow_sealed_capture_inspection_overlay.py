"""允许封存后写入 Capture 质检投影 overlay。

Revision ID: 20260820_0074
Revises: 20260820_0073
Create Date: 2026-08-20

App Publication 在 Evidence Index seal 之后仍需把 inspection_status
写回 qc_capture.metadata；原守卫把任意 metadata/updated_at 变更都拒掉，
导致 Publication Entry 无法收口。本迁移只放行质检投影字段，证据身份仍不可变。
"""

from __future__ import annotations

from alembic import op

revision = "20260820_0074"
down_revision = "20260820_0073"
branch_labels = None
depends_on = None

_SEALED_CAPTURE_GUARD_SQL = """
CREATE OR REPLACE FUNCTION qc_guard_sealed_app_evidence_capture()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    old_agent_run_id uuid;
    new_agent_run_id uuid;
    old_index_sealed boolean := false;
    new_index_sealed boolean := false;
    old_core jsonb;
    new_core jsonb;
BEGIN
    IF TG_OP IN ('UPDATE', 'DELETE') THEN
        old_agent_run_id := OLD.agent_run_id;
    END IF;
    IF TG_OP IN ('INSERT', 'UPDATE') THEN
        new_agent_run_id := NEW.agent_run_id;
    END IF;

    IF old_agent_run_id IS NOT NULL
       AND new_agent_run_id IS NOT NULL
       AND old_agent_run_id IS DISTINCT FROM new_agent_run_id THEN
        PERFORM pg_advisory_xact_lock(
            hashtext(
                'app-evidence-index:'
                || LEAST(old_agent_run_id::text, new_agent_run_id::text)
            )
        );
        PERFORM pg_advisory_xact_lock(
            hashtext(
                'app-evidence-index:'
                || GREATEST(old_agent_run_id::text, new_agent_run_id::text)
            )
        );
    ELSIF COALESCE(old_agent_run_id, new_agent_run_id) IS NOT NULL THEN
        PERFORM pg_advisory_xact_lock(
            hashtext(
                'app-evidence-index:'
                || COALESCE(old_agent_run_id, new_agent_run_id)::text
            )
        );
    END IF;

    IF old_agent_run_id IS NOT NULL THEN
        SELECT EXISTS (
            SELECT 1
            FROM qc_app_evidence_index AS evidence_index
            WHERE evidence_index.status = 'sealed'
              AND evidence_index.task_id = OLD.task_id
              AND evidence_index.agent_run_id = old_agent_run_id
              AND evidence_index.surface_session_id = OLD.surface_session_id
        ) INTO old_index_sealed;
    END IF;
    IF old_index_sealed THEN
        IF TG_OP = 'UPDATE'
           AND NEW.id = OLD.id
           AND NEW.task_id = OLD.task_id
           AND NEW.agent_run_id IS NOT DISTINCT FROM OLD.agent_run_id
           AND NEW.surface_kind = OLD.surface_kind
           AND NEW.surface_session_id IS NOT DISTINCT FROM OLD.surface_session_id
           AND NEW.parent_capture_id IS NOT DISTINCT FROM OLD.parent_capture_id
           AND NEW.action_execution_id IS NOT DISTINCT FROM OLD.action_execution_id
           AND NEW.capture_role = OLD.capture_role
           AND NEW.session_command_sequence
                IS NOT DISTINCT FROM OLD.session_command_sequence
           AND NEW.capture_status = OLD.capture_status
           AND NEW.captured_at IS NOT DISTINCT FROM OLD.captured_at
           AND NEW.created_at = OLD.created_at THEN
            IF NEW.metadata IS NOT DISTINCT FROM OLD.metadata
               AND NEW.updated_at IS NOT DISTINCT FROM OLD.updated_at THEN
                RETURN OLD;
            END IF;
            old_core := COALESCE(OLD.metadata, '{}'::jsonb)
                - 'inspection'
                - 'inspection_status'
                - 'page_type'
                - 'page_state';
            new_core := COALESCE(NEW.metadata, '{}'::jsonb)
                - 'inspection'
                - 'inspection_status'
                - 'page_type'
                - 'page_state';
            IF old_core = new_core THEN
                RETURN NEW;
            END IF;
        END IF;
        RAISE EXCEPTION 'App Evidence Index 已封存，禁止追加、修改或删除 Capture';
    END IF;

    IF new_agent_run_id IS NOT NULL THEN
        SELECT EXISTS (
            SELECT 1
            FROM qc_app_evidence_index AS evidence_index
            WHERE evidence_index.status = 'sealed'
              AND evidence_index.task_id = NEW.task_id
              AND evidence_index.agent_run_id = new_agent_run_id
              AND evidence_index.surface_session_id = NEW.surface_session_id
        ) INTO new_index_sealed;
    END IF;
    IF new_index_sealed THEN
        RAISE EXCEPTION 'App Evidence Index 已封存，禁止追加、修改或删除 Capture';
    END IF;
    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;
    RETURN NEW;
END;
$$
"""

_LEGACY_SEALED_CAPTURE_GUARD_SQL = """
CREATE OR REPLACE FUNCTION qc_guard_sealed_app_evidence_capture()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    old_agent_run_id uuid;
    new_agent_run_id uuid;
    old_index_sealed boolean := false;
    new_index_sealed boolean := false;
BEGIN
    IF TG_OP IN ('UPDATE', 'DELETE') THEN
        old_agent_run_id := OLD.agent_run_id;
    END IF;
    IF TG_OP IN ('INSERT', 'UPDATE') THEN
        new_agent_run_id := NEW.agent_run_id;
    END IF;

    IF old_agent_run_id IS NOT NULL
       AND new_agent_run_id IS NOT NULL
       AND old_agent_run_id IS DISTINCT FROM new_agent_run_id THEN
        PERFORM pg_advisory_xact_lock(
            hashtext(
                'app-evidence-index:'
                || LEAST(old_agent_run_id::text, new_agent_run_id::text)
            )
        );
        PERFORM pg_advisory_xact_lock(
            hashtext(
                'app-evidence-index:'
                || GREATEST(old_agent_run_id::text, new_agent_run_id::text)
            )
        );
    ELSIF COALESCE(old_agent_run_id, new_agent_run_id) IS NOT NULL THEN
        PERFORM pg_advisory_xact_lock(
            hashtext(
                'app-evidence-index:'
                || COALESCE(old_agent_run_id, new_agent_run_id)::text
            )
        );
    END IF;

    IF old_agent_run_id IS NOT NULL THEN
        SELECT EXISTS (
            SELECT 1
            FROM qc_app_evidence_index AS evidence_index
            WHERE evidence_index.status = 'sealed'
              AND evidence_index.task_id = OLD.task_id
              AND evidence_index.agent_run_id = old_agent_run_id
              AND evidence_index.surface_session_id = OLD.surface_session_id
        ) INTO old_index_sealed;
    END IF;
    IF old_index_sealed THEN
        IF TG_OP = 'UPDATE'
           AND NEW.id = OLD.id
           AND NEW.task_id = OLD.task_id
           AND NEW.agent_run_id IS NOT DISTINCT FROM OLD.agent_run_id
           AND NEW.surface_kind = OLD.surface_kind
           AND NEW.surface_session_id IS NOT DISTINCT FROM OLD.surface_session_id
           AND NEW.parent_capture_id IS NOT DISTINCT FROM OLD.parent_capture_id
           AND NEW.action_execution_id IS NOT DISTINCT FROM OLD.action_execution_id
           AND NEW.capture_role = OLD.capture_role
           AND NEW.session_command_sequence
                IS NOT DISTINCT FROM OLD.session_command_sequence
           AND NEW.capture_status = OLD.capture_status
           AND NEW.captured_at IS NOT DISTINCT FROM OLD.captured_at
           AND NEW.metadata = OLD.metadata
           AND NEW.created_at = OLD.created_at
           AND NEW.updated_at = OLD.updated_at THEN
            RETURN OLD;
        END IF;
        RAISE EXCEPTION 'App Evidence Index 已封存，禁止追加、修改或删除 Capture';
    END IF;

    IF new_agent_run_id IS NOT NULL THEN
        SELECT EXISTS (
            SELECT 1
            FROM qc_app_evidence_index AS evidence_index
            WHERE evidence_index.status = 'sealed'
              AND evidence_index.task_id = NEW.task_id
              AND evidence_index.agent_run_id = new_agent_run_id
              AND evidence_index.surface_session_id = NEW.surface_session_id
        ) INTO new_index_sealed;
    END IF;
    IF new_index_sealed THEN
        RAISE EXCEPTION 'App Evidence Index 已封存，禁止追加、修改或删除 Capture';
    END IF;
    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;
    RETURN NEW;
END;
$$
"""


def upgrade() -> None:
    """放行封存 Capture 的质检投影 metadata 写回。"""

    op.execute(_SEALED_CAPTURE_GUARD_SQL)


def downgrade() -> None:
    """恢复严格禁止封存 Capture 任意 metadata 变更。"""

    op.execute(_LEGACY_SEALED_CAPTURE_GUARD_SQL)
