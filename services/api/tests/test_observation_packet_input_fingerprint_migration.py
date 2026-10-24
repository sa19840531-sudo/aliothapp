"""Observation Packet 证据绑定身份迁移合同。"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

MIGRATION_PATH = (
    Path(__file__).parents[1]
    / "migrations"
    / "versions"
    / "20260831_0081_observation_packet_input_fingerprint.py"
)


class RecordingOperations:
    def __init__(self) -> None:
        self.statements: list[str] = []

    def execute(self, statement: str) -> None:
        self.statements.append(" ".join(statement.split()))


def _load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "observation_packet_input_fingerprint_migration",
        MIGRATION_PATH,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_upgrade_replaces_packet_identity_with_evidence_binding_identity() -> None:
    migration = _load_migration()
    operations = RecordingOperations()
    migration.op = operations

    migration.upgrade()

    sql = " ".join(operations.statements)
    assert migration.revision == "20260831_0081"
    assert migration.down_revision == "20260826_0080"
    assert "ADD COLUMN input_fingerprint varchar(64)" in sql
    assert "DROP INDEX IF EXISTS uq_qc_observation_packet_run_page_rule" in sql
    assert "CREATE UNIQUE INDEX uq_qc_observation_packet_run_page_rule_input" in sql
    assert "rule_id, input_fingerprint" in sql
    assert "input_fingerprint IS NOT NULL" in sql
    assert "COMMENT ON COLUMN qc_observation_packet.input_fingerprint" in sql


def test_downgrade_deduplicates_before_restoring_legacy_identity() -> None:
    migration = _load_migration()
    operations = RecordingOperations()
    migration.op = operations

    migration.downgrade()

    sql = " ".join(operations.statements)
    assert "DELETE FROM qc_observation_packet AS packet" in sql
    assert "CREATE UNIQUE INDEX uq_qc_observation_packet_run_page_rule" in sql
    assert "DROP COLUMN input_fingerprint" in sql
