"""App Evidence Exit Gate 公式迁移合同测试。"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

MIGRATION_PATH = (
    Path(__file__).parents[1]
    / "migrations"
    / "versions"
    / "20260820_0072_app_evidence_exit_gate_formula.py"
)


class RecordingOperations:
    def __init__(self) -> None:
        self.statements: list[str] = []

    def execute(self, statement: str) -> None:
        self.statements.append(" ".join(statement.split()))


def _load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "app_evidence_exit_gate_formula_migration",
        MIGRATION_PATH,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_upgrade_aligns_app_formula_with_snapshot_domain_rules() -> None:
    migration = _load_migration()
    operations = RecordingOperations()
    migration.op = operations

    migration.upgrade()

    sql = " ".join(operations.statements)
    assert migration.revision == "20260820_0072"
    assert migration.down_revision == "20260818_0071"
    assert "DROP CONSTRAINT IF EXISTS ck_qc_evidence_exit_gate_formula" in sql
    assert (
        "snapshot_payload->>'protocol_version' = 'evidence-exit-gate-snapshot.v3'"
        in sql
    )
    assert "COALESCE(snapshot_payload->>'surface_kind', '') IN ('android', 'ios')" in sql
    assert "COALESCE(snapshot_payload->>'baseline_manifest_total_count', '0') = '0'" in sql
    assert "requirement_set_id IS NOT NULL OR" in sql
    assert "requirement_set_id IS NULL AND NOT" in sql
    assert "baseline_pass_manifest_count = 0 AND NOT" in sql


def test_downgrade_restores_strict_web_formula() -> None:
    migration = _load_migration()
    operations = RecordingOperations()
    migration.op = operations

    migration.downgrade()

    sql = " ".join(operations.statements)
    assert "baseline_pass_manifest_count > 0 AND requirement_set_id IS NOT NULL" in sql
    assert "snapshot_payload->>'protocol_version'" not in sql
    assert "snapshot_payload->>'surface_kind'" not in sql
