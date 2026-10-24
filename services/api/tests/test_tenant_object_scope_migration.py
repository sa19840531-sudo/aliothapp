from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

MIGRATION_PATH = (
    Path(__file__).parents[1]
    / "migrations"
    / "versions"
    / "20260818_0065_tenant_object_scope.py"
)


class RecordingOperations:
    def __init__(self) -> None:
        self.statements: list[str] = []

    def execute(self, statement: str) -> None:
        self.statements.append(" ".join(statement.split()))


def _load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location("tenant_object_scope_migration", MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    return migration


def test_upgrade_has_single_head_and_backfills_history_subjects() -> None:
    migration = _load_migration()
    operations = RecordingOperations()
    migration.op = operations

    migration.upgrade()

    sql = " ".join(operations.statements)
    assert migration.revision == "20260818_0065"
    assert migration.down_revision == "20260817_0064"
    assert "CREATE TABLE qc_tenant" in sql
    assert migration.DEFAULT_TENANT_ID in sql
    assert migration.HISTORY_PRINCIPAL_ID in sql
    assert "'__history_principal__'" in sql
    assert "'disabled'" in sql
    assert "ALTER TABLE qc_user ADD COLUMN tenant_id uuid" in sql
    assert "ALTER TABLE qc_task ADD COLUMN tenant_id uuid" in sql
    assert "ALTER TABLE qc_platform_build ADD COLUMN tenant_id uuid" in sql
    assert "ALTER COLUMN submitter_id SET NOT NULL" in sql
    assert "ALTER COLUMN created_by SET NOT NULL" in sql
    assert "fk_qc_user_tenant" in sql
    assert "fk_qc_task_tenant" in sql
    assert "fk_qc_task_submitter" in sql
    assert "fk_qc_platform_build_tenant" in sql
    assert "fk_qc_platform_build_created_by" in sql
    assert "idx_qc_task_tenant_submitter_created" in sql
    assert "idx_qc_platform_build_tenant_created_by_created" in sql
    assert "DROP CONSTRAINT IF EXISTS qc_platform_build_sha256_key" in sql
    assert "uq_qc_platform_build_owner_sha256" in sql


def test_downgrade_removes_constraints_and_cleans_history_subject_values() -> None:
    migration = _load_migration()
    operations = RecordingOperations()
    migration.op = operations

    migration.downgrade()

    sql = " ".join(operations.statements)
    assert "DROP CONSTRAINT IF EXISTS fk_qc_task_submitter" in sql
    assert "DROP CONSTRAINT IF EXISTS fk_qc_platform_build_created_by" in sql
    assert "DROP INDEX IF EXISTS uq_qc_platform_build_owner_sha256" in sql
    assert "ADD CONSTRAINT qc_platform_build_sha256_key UNIQUE (sha256)" in sql
    assert "SET submitter_id = NULL" in sql
    assert "SET created_by = NULL" in sql
    assert "DELETE FROM qc_user" in sql
    assert "DROP TABLE IF EXISTS qc_tenant" in sql
