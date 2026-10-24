from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

MIGRATION_PATH = (
    Path(__file__).parents[1]
    / "migrations"
    / "versions"
    / "20260804_0036_app_visual_observation.py"
)


class RecordingOperations:
    def __init__(self) -> None:
        self.statements: list[str] = []

    def execute(self, statement: str) -> None:
        self.statements.append(" ".join(statement.split()))


def _load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "app_visual_observation_migration", MIGRATION_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_upgrade_creates_capture_unique_visual_fact_without_task_status() -> None:
    migration = _load_migration()
    operations = RecordingOperations()
    migration.op = operations

    migration.upgrade()

    sql = " ".join(operations.statements)
    assert migration.down_revision == "20260804_0035"
    assert "CREATE TABLE qc_app_visual_observation" in sql
    assert "capture_id uuid NOT NULL UNIQUE" in sql
    assert "REFERENCES qc_app_capture(capture_id)" in sql
    assert "screenshot_artifact_id uuid NOT NULL" in sql
    assert "candidate_targets jsonb NOT NULL" in sql
    assert "result_hash varchar(64) NOT NULL" in sql
    assert "task_status" not in sql
    assert "work_status" not in sql
    assert "rule_status" not in sql


def test_downgrade_refuses_to_drop_existing_visual_facts() -> None:
    migration = _load_migration()
    operations = RecordingOperations()
    migration.op = operations

    migration.downgrade()

    sql = " ".join(operations.statements)
    assert "IF EXISTS (SELECT 1 FROM qc_app_visual_observation)" in sql
    assert "RAISE EXCEPTION" in sql
    assert "DROP TABLE IF EXISTS qc_app_visual_observation" in sql
