from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

MIGRATION_PATH = (
    Path(__file__).parents[1]
    / "migrations"
    / "versions"
    / "20260901_0081_web_exploration_budget_ledger.py"
)


class RecordingOperations:
    def __init__(self) -> None:
        self.statements: list[str] = []

    def execute(self, statement: str) -> None:
        self.statements.append(" ".join(statement.split()))


def _load_migration(path: Path = MIGRATION_PATH) -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "web_exploration_budget_ledger_migration",
        path,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_unbounded_migration_is_next_revision_and_executes_upgrade_and_downgrade() -> None:
    migration = _load_migration(MIGRATION_PATH.with_name("20260914_0117_web_unbounded_menu_budget.py"))
    operations = RecordingOperations()
    migration.op = operations
    assert migration.revision == "20260914_0117"
    assert migration.down_revision == "20260914_0116"
    migration.upgrade()
    migration.downgrade()
    assert operations.statements


def test_upgrade_creates_web_only_hard_budget_tables() -> None:
    migration = _load_migration()
    operations = RecordingOperations()
    migration.op = operations

    migration.upgrade()

    sql = " ".join(operations.statements)
    assert migration.revision == "20260901_0081"
    assert migration.down_revision == "20260826_0080"
    assert "CREATE TABLE qc_web_exploration_budget" in sql
    assert "CHECK (menu_limit = 10)" in sql
    assert "CREATE TABLE qc_web_menu_ledger_entry" in sql
    assert "execution_slot BETWEEN 1 AND 10" in sql
    assert "menu_action_count IN (0, 1)" in sql
    assert "detail_action_count IN (0, 1)" in sql
    assert "status IN (" in sql
    assert "status = 'home_baseline'" in sql
    assert "status = 'menu_page_captured'" in sql
    assert "status = 'detail_captured'" in sql
    assert "menu_action_execution_id IS NOT NULL" in sql
    assert "menu_probe_capture_id IS NOT NULL" in sql
    assert "menu_ready_capture_id IS NOT NULL" in sql
    assert "menu_page_type = 'home'" in sql
    assert "menu_action_count = 0" in sql
    assert "detail_action_count = 0" in sql
    assert "WHERE execution_slot IS NOT NULL" in sql
    assert "surface_kind = 'web'" in sql
    assert "session_owner = 'web-v2-workflow'" in sql


def test_upgrade_keeps_evidence_payloads_and_raw_urls_out_of_ledger() -> None:
    migration = _load_migration()
    operations = RecordingOperations()
    migration.op = operations

    migration.upgrade()

    sql = " ".join(operations.statements).casefold()
    assert "screenshot" not in sql
    assert "dom_html" not in sql
    assert "canonical_url text" not in sql
    assert "destination_url text" not in sql
    assert "target_origin text" not in sql
    assert "menu_source_capture_id uuid" in sql
    assert "detail_source_capture_id uuid" in sql
    assert "menu_action_execution_id varchar(96)" in sql
    assert "detail_action_execution_id varchar(96)" in sql


def test_downgrade_drops_entries_before_budget() -> None:
    migration = _load_migration()
    operations = RecordingOperations()
    migration.op = operations

    migration.downgrade()

    assert operations.statements == [
        "DROP TABLE IF EXISTS qc_web_menu_ledger_entry",
        "DROP TABLE IF EXISTS qc_web_exploration_budget",
    ]
