"""App Exit Gate 忽略 inline 补证迁移合同。"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

MIGRATION_PATH = (
    Path(__file__).parents[1]
    / "migrations"
    / "versions"
    / "20261022_0144_app_exit_gate_ignore_inline.py"
)


class RecordingOperations:
    def __init__(self) -> None:
        self.statements: list[str] = []

    def execute(self, statement: str) -> None:
        self.statements.append(" ".join(statement.split()))


def _load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "app_exit_gate_ignore_inline_migration",
        MIGRATION_PATH,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_upgrade_allows_app_leave_with_pending_inline() -> None:
    migration = _load_migration()
    operations = RecordingOperations()
    migration.op = operations

    migration.upgrade()

    sql = " ".join(operations.statements)
    assert migration.revision == "20261022_0144"
    assert migration.down_revision == "20261022_0143"
    assert "UPDATE qc_evidence_exit_gate_evaluation" in sql
    assert "reservation_status = 'cancelled'" in sql
    assert "business_outcome = NULL" in sql
    assert "outcome_payload = NULL" in sql
    assert "nonterminal_inline_evidence_request_count = 0 OR" in sql
    assert (
        "nonterminal_inline_evidence_request_count > 0 AND NOT" in sql
        or "nonterminal_inline_evidence_request_count > 0 AND NOT (" in sql
    )
    assert "inline_evidence_request_pending" in sql
    assert "ADD CONSTRAINT ck_qc_evidence_exit_gate_formula" in sql


def test_downgrade_restores_strict_inline_for_app() -> None:
    migration = _load_migration()
    operations = RecordingOperations()
    migration.op = operations

    migration.downgrade()

    sql = " ".join(operations.statements)
    assert "AND nonterminal_inline_evidence_request_count = 0 AND" in sql
    assert "nonterminal_inline_evidence_request_count > 0 AND NOT" not in sql
