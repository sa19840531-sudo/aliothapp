"""H5 自适应交互模式迁移合同测试。"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

MIGRATION_PATH = (
    Path(__file__).parents[1]
    / "migrations"
    / "versions"
    / "20260917_0123_h5_interaction_pattern.py"
)
PAGE_TAB_MIGRATION_PATH = (
    Path(__file__).parents[1]
    / "migrations"
    / "versions"
    / "20260924_0127_h5_page_tab_interaction_pattern.py"
)


class RecordingOperations:
    def __init__(self) -> None:
        self.statements: list[str] = []

    def execute(self, statement: str) -> None:
        self.statements.append(" ".join(statement.split()))


def _load_migration(path: Path = MIGRATION_PATH) -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "h5_interaction_pattern_migration",
        path,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_migration_extends_current_head_and_keeps_statements_separate() -> None:
    migration = _load_migration()
    operations = RecordingOperations()
    migration.op = operations

    migration.upgrade()

    assert migration.revision == "20260917_0123"
    assert migration.down_revision == "20260917_0122"
    assert len(operations.statements) == 5
    assert all(statement.count(";") <= 1 for statement in operations.statements)


def test_observation_is_task_capture_bound_and_idempotent() -> None:
    migration = _load_migration()
    operations = RecordingOperations()
    migration.op = operations

    migration.upgrade()
    sql = " ".join(operations.statements)

    assert "FOREIGN KEY (task_id, page_capture_id)" in sql
    assert "REFERENCES qc_page_capture(task_id, id) ON DELETE CASCADE" in sql
    assert "UNIQUE (pattern_signature, task_id, page_capture_id)" in sql
    assert "status IN ('candidate', 'shadow', 'trusted', 'quarantined', 'deprecated')" in sql


def test_page_tab_pattern_migration_extends_current_head_and_preserves_bottom_navigation() -> None:
    migration = _load_migration(PAGE_TAB_MIGRATION_PATH)
    operations = RecordingOperations()
    migration.op = operations

    migration.upgrade()

    assert migration.revision == "20260924_0127"
    assert migration.down_revision == "20260924_0126"
    assert len(operations.statements) == 2
    constraint_sql = " ".join(operations.statements)
    assert "'bottom_navigation', 'page_tab_navigation'" in constraint_sql
