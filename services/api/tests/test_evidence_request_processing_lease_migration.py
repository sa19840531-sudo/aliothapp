"""Evidence Request 补采消费者租约迁移合同。"""

from __future__ import annotations

import importlib.util
from pathlib import Path

MIGRATION_PATH = (
    Path(__file__).parents[1]
    / "migrations"
    / "versions"
    / "20260908_0099_evidence_request_processing_lease.py"
)


class RecordingOperations:
    def __init__(self) -> None:
        self.statements: list[str] = []

    def execute(self, statement: str) -> None:
        self.statements.append(" ".join(statement.split()))


def _load():
    spec = importlib.util.spec_from_file_location(
        "evidence_request_processing_lease_migration",
        MIGRATION_PATH,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _sql(method: str) -> str:
    migration = _load()
    operations = RecordingOperations()
    migration.op = operations
    getattr(migration, method)()
    return " ".join(operations.statements)


def test_migration_extends_current_head() -> None:
    migration = _load()

    assert migration.revision == "20260908_0099"
    assert migration.down_revision == "20260908_0098"


def test_upgrade_adds_processing_status_lease_and_claim_index() -> None:
    sql = _sql("upgrade")

    assert "ADD COLUMN lease_owner varchar(255)" in sql
    assert "ADD COLUMN lease_token uuid" in sql
    assert "ADD COLUMN lease_expires_at timestamptz" in sql
    assert "ADD COLUMN attempt_count integer NOT NULL DEFAULT 0" in sql
    assert "ADD COLUMN max_attempts integer NOT NULL DEFAULT 3" in sql
    assert "lease_owner IS NOT NULL" in sql
    assert "status = 'pending'" in sql
    assert "idx_qc_evidence_request_processing_claim" in sql


def test_downgrade_clears_leases_before_dropping_columns() -> None:
    sql = _sql("downgrade")

    assert "SET lease_owner = NULL" in sql
    assert "WHERE lease_owner IS NOT NULL" in sql
    assert "DROP COLUMN lease_token" in sql
