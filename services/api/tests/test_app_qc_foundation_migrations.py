from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

MIGRATION_PATH = (
    Path(__file__).parents[1]
    / "migrations"
    / "versions"
    / "20260804_0033_app_task_target_and_build.py"
)


class RecordingOperations:
    def __init__(self) -> None:
        self.statements: list[str] = []

    def execute(self, statement: str) -> None:
        self.statements.append(" ".join(statement.split()))


def _load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location("app_task_target_migration", MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_upgrade_adds_platform_build_and_enforces_task_target_shape() -> None:
    migration = _load_migration()
    operations = RecordingOperations()
    migration.op = operations

    migration.upgrade()

    sql = " ".join(operations.statements)
    assert migration.down_revision == "20260803_0032"
    assert "ADD COLUMN target_platform varchar(16) NOT NULL DEFAULT 'h5'" in sql
    assert "ALTER COLUMN target_url DROP NOT NULL" in sql
    assert "CREATE TABLE qc_platform_build" in sql
    assert "status IN ('uploading', 'verified', 'rejected', 'deleted')" in sql
    assert "target_platform IN ('h5', 'app', 'web')" in sql
    assert "target_url IS NULL" in sql
    assert "task_config #>> '{app,platform_build_ref}'" in sql
    assert "CREATE INDEX idx_qc_platform_build_status" in sql


def test_downgrade_refuses_to_restore_url_only_schema_while_app_tasks_exist() -> None:
    migration = _load_migration()
    operations = RecordingOperations()
    migration.op = operations

    migration.downgrade()

    sql = " ".join(operations.statements)
    assert "IF EXISTS (SELECT 1 FROM qc_task WHERE target_platform = 'app')" in sql
    assert "RAISE EXCEPTION" in sql
    assert "ALTER COLUMN target_url SET NOT NULL" in sql
    assert "DROP TABLE IF EXISTS qc_platform_build" in sql
    assert "DROP COLUMN IF EXISTS target_platform" in sql
