from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

MIGRATION_PATH = (
    Path(__file__).parents[1]
    / "migrations"
    / "versions"
    / "20260812_0049_app_visual_analysis_phase_facts.py"
)


class RecordingOperations:
    def __init__(self) -> None:
        self.statements: list[str] = []

    def execute(self, statement: str) -> None:
        self.statements.append(" ".join(statement.split()))


def _load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "app_visual_phase_facts_migration", MIGRATION_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_upgrade_appends_phase_facts_to_current_head() -> None:
    migration = _load_migration()
    operations = RecordingOperations()
    migration.op = operations

    migration.upgrade()

    sql = " ".join(operations.statements)
    assert migration.revision == "20260812_0049"
    assert migration.down_revision == "20260812_0048"
    assert "ADD COLUMN page_state varchar(64)" in sql
    assert "ADD COLUMN overlay_signature text" in sql
    assert "ADD COLUMN surface_facts jsonb" in sql
    assert "SET page_state = 'unknown'" in sql
    assert "ALTER COLUMN page_state SET NOT NULL" in sql
    assert "ALTER COLUMN surface_facts SET NOT NULL" in sql
    assert "jsonb_typeof(surface_facts) = 'object'" in sql
    assert "contract_version <> 'app-visual-analysis.v1'" in sql


def test_downgrade_refuses_to_drop_nondefault_phase_facts() -> None:
    migration = _load_migration()
    operations = RecordingOperations()
    migration.op = operations

    migration.downgrade()

    sql = " ".join(operations.statements)
    assert "page_state <> 'unknown'" in sql
    assert "overlay_signature IS NOT NULL" in sql
    assert "surface_facts <> '{}'::jsonb" in sql
    assert "RAISE EXCEPTION" in sql
    assert "DROP COLUMN surface_facts" in sql
    assert "DROP COLUMN overlay_signature" in sql
    assert "DROP COLUMN page_state" in sql
