"""App 目标覆盖与视觉质量 Completion Gate 迁移合同。"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

MIGRATION_PATH = (
    Path(__file__).parents[1]
    / "migrations"
    / "versions"
    / "20260818_0070_app_target_visual_quality_gate.py"
)


class RecordingOperations:
    def __init__(self) -> None:
        self.statements: list[str] = []

    def execute(self, statement: str) -> None:
        self.statements.append(" ".join(statement.split()))


def _load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "app_target_visual_quality_gate_migration",
        MIGRATION_PATH,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_upgrade_adds_app_coverage_to_gate_formula() -> None:
    migration = _load_migration()
    operations = RecordingOperations()
    migration.op = operations

    migration.upgrade()

    sql = " ".join(operations.statements)
    assert migration.revision == "20260818_0070"
    assert migration.down_revision == "20260818_0069"
    assert "ADD COLUMN app_coverage_satisfied boolean NOT NULL DEFAULT true" in sql
    assert "AND candidate_inventory_satisfied AND app_coverage_satisfied" in sql
    assert "AND h5_coverage_satisfied" in sql


def test_downgrade_restores_previous_formula_without_deleting_source_facts() -> None:
    migration = _load_migration()
    operations = RecordingOperations()
    migration.op = operations

    migration.downgrade()

    sql = " ".join(operations.statements)
    assert "DROP COLUMN app_coverage_satisfied" in sql
    assert "qc_app_exploration_result" not in sql
    assert "qc_app_visual_observation" not in sql
