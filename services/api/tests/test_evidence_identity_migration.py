"""证据附件可信身份约束迁移测试。"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

MIGRATION_PATH = (
    Path(__file__).parents[1]
    / "migrations"
    / "versions"
    / "20260813_0049_evidence_artifact_identity_and_exit_outcome.py"
)


class RecordingOperations:
    def __init__(self) -> None:
        self.statements: list[str] = []

    def execute(self, statement: str) -> None:
        self.statements.append(" ".join(statement.split()))


def _load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location("evidence_identity_migration", MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_upgrade_preflights_then_validates_before_not_null() -> None:
    migration = _load_migration()
    operations = RecordingOperations()
    migration.op = operations

    migration.upgrade()

    sql = " ".join(operations.statements)
    assert migration.revision == "20260813_0049"
    assert migration.down_revision == "20260812_0048"
    assert "IF EXISTS ( SELECT 1 FROM qc_evidence_artifact" in operations.statements[0]
    assert "RAISE EXCEPTION" in operations.statements[0]
    assert "btrim(storage_path) = ''" in operations.statements[0]
    assert "checksum !~ '^sha256:[0-9a-f]{64}$'" in operations.statements[0]
    assert "size_bytes IS NULL OR size_bytes < 0" in operations.statements[0]
    assert "NOT VALID" in sql
    assert "VALIDATE CONSTRAINT ck_qc_evidence_artifact_checksum_format" in sql
    assert "ALTER COLUMN checksum SET NOT NULL" in sql
    assert sql.index("VALIDATE CONSTRAINT ck_qc_evidence_artifact_checksum_nn") < sql.index(
        "ALTER COLUMN checksum SET NOT NULL"
    )
    assert sql.index("VALIDATE CONSTRAINT ck_qc_evidence_artifact_size_nn") < sql.index(
        "ALTER COLUMN size_bytes SET NOT NULL"
    )


def test_upgrade_preserves_frozen_artifact_types_and_adds_ui_tree() -> None:
    migration = _load_migration()
    operations = RecordingOperations()
    migration.op = operations

    migration.upgrade()

    sql = " ".join(operations.statements)
    for artifact_type in (
        "screenshot",
        "dom",
        "visible_text",
        "ocr_text",
        "har",
        "console_log",
        "media_state",
        "ai_io",
        "report",
        "action_trace",
        "ui_snapshot",
        "temporal_trace",
        "page_coverage",
        "ui_tree",
    ):
        assert f"'{artifact_type}'" in sql


def test_upgrade_adds_versioned_gate_lineage_and_closed_outcome_constraints() -> None:
    migration = _load_migration()
    operations = RecordingOperations()
    migration.op = operations

    migration.upgrade()

    sql = " ".join(operations.statements)
    assert "ADD COLUMN manifest_id varchar(96)" in sql
    assert "ADD COLUMN manifest_version integer" in sql
    assert "ADD COLUMN manifest_hash varchar(128)" in sql
    assert "ADD COLUMN manifest_quality_status varchar(16)" in sql
    assert "ADD COLUMN business_outcome varchar(64)" in sql
    assert "ADD COLUMN outcome_payload jsonb" in sql
    assert "'evidence_insufficient'" in sql
    assert "'evidence_pending'" in sql
    assert "'control_plane_incomplete'" in sql
    assert "'evidence_integrity_failed'" in sql
    assert "business_outcome = 'evidence_insufficient'" in sql
    assert "manifest_quality_status = 'fail'" in sql
    assert "requirement_set_id IS NOT NULL" in sql


def test_downgrade_is_non_destructive_and_restores_nullable_identity() -> None:
    migration = _load_migration()
    operations = RecordingOperations()
    migration.op = operations

    migration.downgrade()

    sql = " ".join(operations.statements)
    assert "ALTER COLUMN checksum DROP NOT NULL" in sql
    assert "ALTER COLUMN size_bytes DROP NOT NULL" in sql
    assert "IF EXISTS" in operations.statements[0]
    assert "artifact_type = 'ui_tree'" in operations.statements[0]
    assert "RAISE EXCEPTION" in operations.statements[0]
    assert "DELETE FROM qc_evidence_artifact" not in sql
    assert "UPDATE qc_evidence_artifact" not in sql
