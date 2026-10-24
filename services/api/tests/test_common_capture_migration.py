from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

MIGRATION_PATH = (
    Path(__file__).parents[1]
    / "migrations"
    / "versions"
    / "20260804_0034_common_capture_identity.py"
)


class RecordingOperations:
    def __init__(self) -> None:
        self.statements: list[str] = []

    def execute(self, statement: str) -> None:
        self.statements.append(" ".join(statement.split()))


def _load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location("common_capture_migration", MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_upgrade_backfills_web_capture_identity_and_adds_app_subtype() -> None:
    migration = _load_migration()
    operations = RecordingOperations()
    migration.op = operations

    migration.upgrade()

    sql = " ".join(operations.statements)
    assert migration.down_revision == "20260804_0033"
    assert "CREATE TABLE qc_capture" in sql
    assert "surface_kind IN ('web', 'android', 'ios')" in sql
    assert "capture_status IN ('pending', 'captured', 'failed', 'skipped')" in sql
    assert "INSERT INTO qc_capture" in sql
    assert "SELECT page.id" in sql
    assert "ALTER TABLE qc_page_capture" in sql
    assert "REFERENCES qc_capture(id)" in sql
    assert "DEFERRABLE INITIALLY DEFERRED" in sql
    assert "CREATE TABLE qc_app_capture" in sql
    app_table = sql.split("CREATE TABLE qc_app_capture", 1)[1].split(")", 1)[0]
    assert "dom" not in app_table
    assert "visible_text" not in app_table
    assert "ui_tree" not in app_table
    assert "CREATE TRIGGER trg_qc_page_capture_parent_insert" in sql
    assert "CREATE TRIGGER trg_qc_page_capture_parent_update" in sql


def test_downgrade_refuses_when_app_capture_facts_exist() -> None:
    migration = _load_migration()
    operations = RecordingOperations()
    migration.op = operations

    migration.downgrade()

    sql = " ".join(operations.statements)
    assert "IF EXISTS (SELECT 1 FROM qc_app_capture)" in sql
    assert "RAISE EXCEPTION" in sql
    assert "DROP TABLE IF EXISTS qc_app_capture" in sql
    assert "DROP TABLE IF EXISTS qc_capture" in sql
