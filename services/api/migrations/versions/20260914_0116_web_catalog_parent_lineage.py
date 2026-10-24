"""允许运行时菜单安全引用真实 Catalog 父项。

Revision ID: 20260914_0116
Revises: 20260914_0115
"""

from alembic import op

revision = "20260914_0116"
down_revision = "20260914_0115"
branch_labels = None
depends_on = None


_RUNTIME_ONLY_VALIDATOR = """
CREATE OR REPLACE FUNCTION qc_validate_web_menu_lineage() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
  parent_depth integer;
  parent_path jsonb;
  parent_logical_identity varchar(128);
BEGIN
  IF TG_OP='UPDATE' AND (
    NEW.parent_menu_entry_id IS DISTINCT FROM OLD.parent_menu_entry_id OR
    NEW.menu_depth IS DISTINCT FROM OLD.menu_depth OR
    NEW.exploration_path IS DISTINCT FROM OLD.exploration_path OR
    NEW.logical_target_identity IS DISTINCT FROM OLD.logical_target_identity
  ) THEN
    RAISE EXCEPTION 'WEB_MENU_LINEAGE_IMMUTABLE' USING ERRCODE='23514';
  END IF;
  IF NEW.source_kind <> 'runtime_discovered' THEN
    RETURN NEW;
  END IF;
  IF NEW.parent_menu_entry_id = NEW.menu_entry_id THEN
    RAISE EXCEPTION 'WEB_MENU_LINEAGE_SELF_PARENT' USING ERRCODE='23514';
  END IF;
  IF NEW.parent_menu_entry_id IS NULL THEN
    IF NEW.menu_depth > 1 AND NOT (
      NEW.diagnostic->>'error_code'='WEB_MENU_LINEAGE_UNAVAILABLE'
    ) THEN
      RAISE EXCEPTION 'WEB_MENU_LINEAGE_PARENT_MISSING' USING ERRCODE='23514';
    END IF;
    IF NEW.menu_depth = 1 AND
       NEW.discovery_manifest->'candidate'->>'parent_logical_target_identity'
           IS NOT NULL THEN
      RAISE EXCEPTION 'WEB_MENU_LINEAGE_ROOT_PARENT_INVALID' USING ERRCODE='23514';
    END IF;
    RETURN NEW;
  END IF;
  SELECT parent.menu_depth, parent.exploration_path,
         parent.logical_target_identity
  INTO parent_depth, parent_path, parent_logical_identity
  FROM qc_web_menu_ledger_entry AS parent
  WHERE parent.agent_run_id=NEW.agent_run_id
    AND parent.site_id=NEW.site_id
    AND parent.menu_entry_id=NEW.parent_menu_entry_id
    AND parent.source_kind='runtime_discovered';
  IF NOT FOUND THEN
    RAISE EXCEPTION 'WEB_MENU_LINEAGE_PARENT_SCOPE_INVALID' USING ERRCODE='23514';
  END IF;
  IF NEW.menu_depth <> parent_depth + 1
     OR jsonb_array_length(NEW.exploration_path)
        <= jsonb_array_length(parent_path)
     OR EXISTS (
       SELECT 1
       FROM generate_series(0, jsonb_array_length(parent_path) - 1)
            AS path_index
       WHERE NEW.exploration_path->>path_index
           IS DISTINCT FROM parent_path->>path_index
     )
     OR NEW.discovery_manifest->'candidate'->>'parent_logical_target_identity'
        IS DISTINCT FROM parent_logical_identity THEN
    RAISE EXCEPTION 'WEB_MENU_LINEAGE_PARENT_MISMATCH' USING ERRCODE='23514';
  END IF;
  RETURN NEW;
END $$
"""


_CATALOG_PARENT_VALIDATOR = """
CREATE OR REPLACE FUNCTION qc_validate_web_menu_lineage() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
  parent_source_kind varchar(32);
  parent_depth integer;
  parent_path jsonb;
  parent_logical_identity varchar(128);
  parent_menu_action_count integer;
  parent_menu_action_execution_id varchar(128);
  parent_menu_ready_capture_id uuid;
BEGIN
  IF TG_OP='UPDATE' AND (
    NEW.parent_menu_entry_id IS DISTINCT FROM OLD.parent_menu_entry_id OR
    NEW.menu_depth IS DISTINCT FROM OLD.menu_depth OR
    NEW.exploration_path IS DISTINCT FROM OLD.exploration_path OR
    NEW.logical_target_identity IS DISTINCT FROM OLD.logical_target_identity
  ) THEN
    RAISE EXCEPTION 'WEB_MENU_LINEAGE_IMMUTABLE' USING ERRCODE='23514';
  END IF;
  IF NEW.source_kind <> 'runtime_discovered' THEN
    RETURN NEW;
  END IF;
  IF NEW.parent_menu_entry_id = NEW.menu_entry_id THEN
    RAISE EXCEPTION 'WEB_MENU_LINEAGE_SELF_PARENT' USING ERRCODE='23514';
  END IF;
  IF NEW.parent_menu_entry_id IS NULL THEN
    IF NEW.menu_depth > 1 AND NOT (
      NEW.diagnostic->>'error_code'='WEB_MENU_LINEAGE_UNAVAILABLE'
    ) THEN
      RAISE EXCEPTION 'WEB_MENU_LINEAGE_PARENT_MISSING' USING ERRCODE='23514';
    END IF;
    IF NEW.menu_depth = 1 AND
       NEW.discovery_manifest->'candidate'->>'parent_logical_target_identity'
           IS NOT NULL THEN
      RAISE EXCEPTION 'WEB_MENU_LINEAGE_ROOT_PARENT_INVALID' USING ERRCODE='23514';
    END IF;
    RETURN NEW;
  END IF;
  SELECT parent.source_kind,
         CASE WHEN parent.source_kind='catalog' THEN 1 ELSE parent.menu_depth END,
         CASE WHEN parent.source_kind='catalog' THEN parent.menu_path
              ELSE parent.exploration_path END,
         parent.logical_target_identity,
         parent.menu_action_count,
         parent.menu_action_execution_id,
         parent.menu_ready_capture_id
  INTO parent_source_kind, parent_depth, parent_path, parent_logical_identity,
       parent_menu_action_count, parent_menu_action_execution_id,
       parent_menu_ready_capture_id
  FROM qc_web_menu_ledger_entry AS parent
  WHERE parent.agent_run_id=NEW.agent_run_id
    AND parent.site_id=NEW.site_id
    AND parent.menu_entry_id=NEW.parent_menu_entry_id
    AND parent.source_kind IN ('catalog', 'runtime_discovered');
  IF NOT FOUND OR (
    parent_source_kind='catalog' AND NOT (
      parent_menu_action_count=1
      AND parent_menu_action_execution_id IS NOT NULL
      AND parent_menu_ready_capture_id IS NOT NULL
    )
  ) THEN
    RAISE EXCEPTION 'WEB_MENU_LINEAGE_PARENT_SCOPE_INVALID' USING ERRCODE='23514';
  END IF;
  IF NEW.menu_depth <> parent_depth + 1
     OR (parent_source_kind='catalog' AND NEW.menu_depth <> 2)
     OR jsonb_array_length(NEW.exploration_path)
        <= jsonb_array_length(parent_path)
     OR EXISTS (
       SELECT 1
       FROM generate_series(0, jsonb_array_length(parent_path) - 1)
            AS path_index
       WHERE NEW.exploration_path->>path_index
           IS DISTINCT FROM parent_path->>path_index
     )
     OR (
       parent_source_kind='runtime_discovered'
       AND NEW.discovery_manifest->'candidate'
             ->>'parent_logical_target_identity'
           IS DISTINCT FROM parent_logical_identity
     )
     OR (
       parent_source_kind='catalog'
       AND NOT COALESCE(
         NEW.discovery_manifest->'candidate'
           ->>'parent_logical_target_identity' ~ '^[0-9a-f]{64}$',
         false
       )
     ) THEN
    RAISE EXCEPTION 'WEB_MENU_LINEAGE_PARENT_MISMATCH' USING ERRCODE='23514';
  END IF;
  RETURN NEW;
END $$
"""


def _replace_validator(statement: str) -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS trg_qc_web_menu_lineage "
        "ON qc_web_menu_ledger_entry"
    )
    op.execute(statement)
    op.execute(
        """
        CREATE TRIGGER trg_qc_web_menu_lineage
        BEFORE INSERT OR UPDATE ON qc_web_menu_ledger_entry
        FOR EACH ROW EXECUTE FUNCTION qc_validate_web_menu_lineage()
        """
    )


def upgrade() -> None:
    """Catalog 行保持无 lineage 列，仅允许已完成访问的同域父项承接新子项。"""

    _replace_validator(_CATALOG_PARENT_VALIDATOR)


def downgrade() -> None:
    """存在 Catalog 父子事实时拒绝退回 runtime-only 父链合同。"""

    op.execute(
        """
        DO $$ BEGIN
          IF EXISTS (
            SELECT 1
            FROM qc_web_menu_ledger_entry AS child
            JOIN qc_web_menu_ledger_entry AS parent
              ON parent.agent_run_id=child.agent_run_id
             AND parent.site_id=child.site_id
             AND parent.menu_entry_id=child.parent_menu_entry_id
             AND parent.source_kind='catalog'
            WHERE child.source_kind='runtime_discovered'
          ) THEN
            RAISE EXCEPTION 'WEB_MENU_CATALOG_PARENT_DOWNGRADE_BLOCKED';
          END IF;
        END $$
        """
    )
    _replace_validator(_RUNTIME_ONLY_VALIDATOR)
