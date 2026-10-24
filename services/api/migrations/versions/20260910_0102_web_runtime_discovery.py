"""为 Web 统一菜单台账增加目录外发现来源，不另建执行预算。

Revision ID: 20260910_0102
Revises: 20260909_0101
"""

from alembic import op

revision = "20260910_0102"
down_revision = "20260909_0101"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE qc_web_menu_ledger_entry
          ADD COLUMN source_kind varchar(32) NOT NULL DEFAULT 'catalog',
          ADD COLUMN discovery_source_capture_id uuid REFERENCES qc_capture(id) ON DELETE RESTRICT,
          ADD COLUMN discovery_manifest jsonb NOT NULL DEFAULT '{}'::jsonb,
          ADD CONSTRAINT ck_qc_web_menu_source CHECK ((
            (source_kind='catalog' AND discovery_source_capture_id IS NULL
             AND discovery_manifest='{}'::jsonb
             AND decision IN ('home_baseline','allowlisted_observe_only','blocked_cross_origin'))
            OR
            (source_kind='runtime_discovered' AND discovery_source_capture_id IS NOT NULL
             AND decision IN ('runtime_record_only','runtime_observe_only')
             AND jsonb_typeof(discovery_manifest)='object'
             AND discovery_manifest->>'version'='web-discovery-manifest.v1'
             AND jsonb_typeof(discovery_manifest->'entry_id')='string'
             AND discovery_manifest->>'entry_id'=menu_entry_id
             AND jsonb_typeof(discovery_manifest->'candidate')='object'
             AND jsonb_typeof(discovery_manifest->'candidate'->'entry_id')='string'
             AND length(trim(discovery_manifest->'candidate'->>'entry_id')) > 0
             AND (execution_slot IS NULL OR decision='runtime_observe_only'))
          ) IS TRUE)
    """)

    op.execute("""
        CREATE FUNCTION qc_validate_web_discovery_source() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF TG_OP='UPDATE' AND (
            NEW.source_kind IS DISTINCT FROM OLD.source_kind OR
            NEW.discovery_source_capture_id IS DISTINCT FROM OLD.discovery_source_capture_id OR
            (NEW.discovery_manifest - 'authorization') IS DISTINCT FROM
              (OLD.discovery_manifest - 'authorization')
          ) THEN
            RAISE EXCEPTION 'WEB_DISCOVERY_SOURCE_IMMUTABLE' USING ERRCODE='23514';
          END IF;
          IF NEW.source_kind='runtime_discovered' AND NOT EXISTS (
            SELECT 1 FROM qc_web_exploration_budget budget
            JOIN qc_agent_run run ON run.id=budget.agent_run_id AND run.task_id=budget.task_id
            JOIN qc_task task ON task.id=budget.task_id AND task.target_platform='web'
            JOIN qc_surface_session session ON session.task_id=task.id
              AND session.session_id=budget.session_id AND session.session_owner='web-v2-workflow'
            JOIN qc_capture source ON source.id=NEW.discovery_source_capture_id
              AND source.task_id=task.id AND source.agent_run_id=run.id
              AND source.surface_session_id=session.session_id AND source.surface_kind='web'
              AND source.capture_status='captured'
            WHERE budget.agent_run_id=NEW.agent_run_id AND budget.site_id=NEW.site_id
          ) THEN
            RAISE EXCEPTION 'WEB_DISCOVERY_SOURCE_SCOPE_INVALID' USING ERRCODE='23514';
          END IF;
          RETURN NEW;
        END $$;
    """)
    op.execute("""
        CREATE TRIGGER trg_qc_web_discovery_source BEFORE INSERT OR UPDATE
        ON qc_web_menu_ledger_entry FOR EACH ROW EXECUTE FUNCTION qc_validate_web_discovery_source()
    """)


def downgrade() -> None:
    """发现证据尚存时拒绝下迁移，不能丢弃审计事实以迁就旧代码。"""
    op.execute("""
        DO $$ BEGIN
          IF EXISTS (SELECT 1 FROM qc_web_menu_ledger_entry WHERE source_kind='runtime_discovered') THEN
            RAISE EXCEPTION 'WEB_DISCOVERY_DOWNGRADE_REQUIRES_NO_DISCOVERY_RECORDS';
          END IF;
        END $$
    """)
    op.execute("DROP TRIGGER IF EXISTS trg_qc_web_discovery_source ON qc_web_menu_ledger_entry")
    op.execute("DROP FUNCTION IF EXISTS qc_validate_web_discovery_source()")
    op.execute("""
        ALTER TABLE qc_web_menu_ledger_entry
          DROP CONSTRAINT ck_qc_web_menu_source,
          DROP COLUMN discovery_manifest,
          DROP COLUMN discovery_source_capture_id,
          DROP COLUMN source_kind
    """)
