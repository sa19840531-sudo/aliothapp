"""增加 App Workflow Bootstrap 检查点并统一运行时 Capture 外键。

Revision ID: 20260804_0035
Revises: 20260804_0034
Create Date: 2026-08-04
"""

from __future__ import annotations

from alembic import op

revision = "20260804_0035"
down_revision = "20260804_0034"
branch_labels = None
depends_on = None

_COMMON_CAPTURE_FOREIGN_KEYS = (
    ("qc_evidence_artifact", "page_capture_id", "SET NULL"),
    ("qc_observation_packet", "page_capture_id", "SET NULL"),
    ("qc_rule_execution", "page_capture_id", "SET NULL"),
    ("qc_inspection_work_item", "page_capture_id", "CASCADE"),
    ("qc_evidence_request", "target_capture_id", "RESTRICT"),
    ("qc_v2_work_item", "source_capture_id", "RESTRICT"),
    ("qc_evidence_manifest", "capture_id", "CASCADE"),
    ("qc_journey_execution", "current_capture_id", "RESTRICT"),
    ("qc_journey_step_attempt", "source_capture_id", "RESTRICT"),
    ("qc_journey_step_attempt", "result_capture_id", "RESTRICT"),
    ("qc_restoration_attempt", "original_capture_id", "RESTRICT"),
    ("qc_restoration_attempt", "restored_capture_id", "RESTRICT"),
    ("qc_surface_session", "current_capture_id", "RESTRICT"),
    ("qc_surface_ref", "current_capture_id", "RESTRICT"),
    ("qc_action_execution", "requested_source_capture_id", "RESTRICT"),
    ("qc_action_execution", "source_capture_id", "RESTRICT"),
    ("qc_action_execution", "result_capture_id", "RESTRICT"),
    ("qc_evidence_exit_requirement_set", "capture_id", "RESTRICT"),
    ("qc_evidence_exit_gate_evaluation", "source_capture_id", "RESTRICT"),
)


def upgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1
                FROM qc_page_capture AS page
                LEFT JOIN qc_capture AS capture ON capture.id = page.id
                WHERE capture.id IS NULL
            ) THEN
                RAISE EXCEPTION 'qc_page_capture 尚未完整回填 qc_capture';
            END IF;
        END;
        $$
        """
    )
    for table_name, column_name, on_delete in _COMMON_CAPTURE_FOREIGN_KEYS:
        _replace_capture_foreign_key(
            table_name=table_name,
            column_name=column_name,
            target_table="qc_capture",
            on_delete=on_delete,
            suffix="common",
        )
    op.execute(
        """
        CREATE TABLE qc_app_bootstrap (
            session_id varchar(128) PRIMARY KEY,
            task_id uuid NOT NULL,
            platform_build_ref varchar(255) NOT NULL,
            stage varchar(32) NOT NULL DEFAULT 'validating',
            install_request_id varchar(255),
            install_attempt integer NOT NULL DEFAULT 0,
            installed_package varchar(512),
            open_request_id varchar(255),
            open_attempt integer NOT NULL DEFAULT 0,
            bootstrap_capture_id uuid
                REFERENCES qc_capture(id) ON DELETE RESTRICT
                DEFERRABLE INITIALLY DEFERRED,
            workflow_lease_owner varchar(255),
            workflow_lease_token uuid,
            workflow_lease_expires_at timestamptz,
            error_code varchar(128),
            error_message text,
            row_version bigint NOT NULL DEFAULT 0,
            started_at timestamptz NOT NULL DEFAULT now(),
            ready_at timestamptz,
            failed_at timestamptz,
            updated_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT fk_qc_app_bootstrap_session FOREIGN KEY (task_id, session_id)
                REFERENCES qc_surface_session (task_id, session_id)
                ON DELETE CASCADE,
            CONSTRAINT ck_qc_app_bootstrap_stage CHECK (
                stage IN (
                    'validating', 'acquiring_session', 'resolving_build',
                    'installing', 'installed', 'opening', 'opened',
                    'bootstrap_capturing', 'app_ready', 'failed', 'canceled'
                )
            ),
            CONSTRAINT ck_qc_app_bootstrap_attempts CHECK (
                install_attempt >= 0
                AND open_attempt >= 0
                AND row_version >= 0
            ),
            CONSTRAINT ck_qc_app_bootstrap_lease CHECK (
                (
                    workflow_lease_owner IS NULL
                    AND workflow_lease_token IS NULL
                    AND workflow_lease_expires_at IS NULL
                )
                OR (
                    stage NOT IN ('app_ready', 'failed', 'canceled')
                    AND workflow_lease_owner IS NOT NULL
                    AND workflow_lease_token IS NOT NULL
                    AND workflow_lease_expires_at IS NOT NULL
                )
            ),
            CONSTRAINT ck_qc_app_bootstrap_ready CHECK (
                (
                    stage = 'app_ready'
                    AND install_request_id IS NOT NULL
                    AND installed_package IS NOT NULL
                    AND open_request_id IS NOT NULL
                    AND bootstrap_capture_id IS NOT NULL
                    AND ready_at IS NOT NULL
                    AND failed_at IS NULL
                    AND error_code IS NULL
                    AND error_message IS NULL
                )
                OR (
                    stage IN ('bootstrap_capturing', 'failed', 'canceled')
                    AND ready_at IS NULL
                )
                OR (
                    stage NOT IN (
                        'bootstrap_capturing', 'app_ready', 'failed', 'canceled'
                    )
                    AND bootstrap_capture_id IS NULL
                    AND ready_at IS NULL
                )
            ),
            CONSTRAINT ck_qc_app_bootstrap_failed CHECK (
                (
                    stage = 'failed'
                    AND error_code IS NOT NULL
                    AND failed_at IS NOT NULL
                    AND ready_at IS NULL
                )
                OR (
                    stage <> 'failed'
                    AND error_code IS NULL
                    AND error_message IS NULL
                    AND failed_at IS NULL
                )
            ),
            CONSTRAINT ck_qc_app_bootstrap_install_result CHECK (
                stage NOT IN (
                    'installed', 'opening', 'opened',
                    'bootstrap_capturing', 'app_ready'
                )
                OR (
                    install_request_id IS NOT NULL
                    AND installed_package IS NOT NULL
                    AND install_attempt > 0
                )
            ),
            CONSTRAINT ck_qc_app_bootstrap_open_result CHECK (
                stage NOT IN ('opened', 'bootstrap_capturing', 'app_ready')
                OR (open_request_id IS NOT NULL AND open_attempt > 0)
            )
        )
        """
    )
    op.execute(
        """
        CREATE INDEX idx_qc_app_bootstrap_stage
        ON qc_app_bootstrap (stage, workflow_lease_expires_at, updated_at)
        WHERE stage NOT IN ('app_ready', 'failed', 'canceled')
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM qc_app_bootstrap) THEN
                RAISE EXCEPTION '存在 App Bootstrap 事实，不能回滚运行时检查点';
            END IF;
            IF EXISTS (SELECT 1 FROM qc_app_capture) THEN
                RAISE EXCEPTION '存在 App Capture 事实，不能恢复 Web-only Capture 外键';
            END IF;
            IF EXISTS (
                SELECT 1
                FROM qc_capture AS capture
                LEFT JOIN qc_page_capture AS page ON page.id = capture.id
                WHERE page.id IS NULL OR capture.surface_kind <> 'web'
            ) THEN
                RAISE EXCEPTION '存在非 Web 公共 Capture，不能恢复 Web-only Capture 外键';
            END IF;
        END;
        $$
        """
    )
    op.execute("DROP INDEX IF EXISTS idx_qc_app_bootstrap_stage")
    op.execute("DROP TABLE IF EXISTS qc_app_bootstrap")
    for table_name, column_name, on_delete in reversed(_COMMON_CAPTURE_FOREIGN_KEYS):
        _replace_capture_foreign_key(
            table_name=table_name,
            column_name=column_name,
            target_table="qc_page_capture",
            on_delete=on_delete,
            suffix="page",
        )


def _replace_capture_foreign_key(
    *,
    table_name: str,
    column_name: str,
    target_table: str,
    on_delete: str,
    suffix: str,
) -> None:
    constraint_name = f"fk_{table_name.removeprefix('qc_')}_{column_name}_{suffix}"
    # asyncpg 不允许单次 prepared statement 含多条 SQL，拆成两步。
    op.execute(
        f"""
        DO $$
        DECLARE
            existing_constraint text;
        BEGIN
            FOR existing_constraint IN
                SELECT constraint_row.conname
                FROM pg_constraint AS constraint_row
                JOIN pg_class AS table_row
                  ON table_row.oid = constraint_row.conrelid
                JOIN unnest(constraint_row.conkey) AS key_column(attnum)
                  ON true
                JOIN pg_attribute AS attribute_row
                  ON attribute_row.attrelid = table_row.oid
                 AND attribute_row.attnum = key_column.attnum
                WHERE constraint_row.contype = 'f'
                  AND table_row.relname = '{table_name}'
                  AND attribute_row.attname = '{column_name}'
            LOOP
                EXECUTE format(
                    'ALTER TABLE %I DROP CONSTRAINT %I',
                    '{table_name}',
                    existing_constraint
                );
            END LOOP;
        END;
        $$
        """
    )
    op.execute(
        f"""
        ALTER TABLE {table_name}
        ADD CONSTRAINT {constraint_name}
            FOREIGN KEY ({column_name}) REFERENCES {target_table}(id)
            ON DELETE {on_delete}
            DEFERRABLE INITIALLY DEFERRED
        """
    )
