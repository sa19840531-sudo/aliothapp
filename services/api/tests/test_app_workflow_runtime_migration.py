"""App Workflow Runtime 检查点与公共 Capture FK 迁移测试。"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

MIGRATION_PATH = (
    Path(__file__).parents[1]
    / "migrations"
    / "versions"
    / "20260804_0035_app_workflow_runtime.py"
)


class RecordingOperations:
    def __init__(self) -> None:
        self.statements: list[str] = []

    def execute(self, statement: str) -> None:
        self.statements.append(" ".join(statement.split()))


def _load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "app_workflow_runtime_migration",
        MIGRATION_PATH,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_upgrade_creates_recoverable_bootstrap_checkpoint() -> None:
    migration = _load_migration()
    operations = RecordingOperations()
    migration.op = operations

    migration.upgrade()

    sql = " ".join(operations.statements)
    assert migration.down_revision == "20260804_0034"
    assert "CREATE TABLE qc_app_bootstrap" in sql
    assert "session_id varchar(128) PRIMARY KEY" in sql
    assert "REFERENCES qc_surface_session (task_id, session_id)" in sql
    assert "platform_build_ref varchar(255) NOT NULL" in sql
    for stage in (
        "validating",
        "acquiring_session",
        "resolving_build",
        "installing",
        "installed",
        "opening",
        "opened",
        "bootstrap_capturing",
        "app_ready",
        "failed",
        "canceled",
    ):
        assert f"'{stage}'" in sql
    assert "install_attempt >= 0" in sql
    assert "open_attempt >= 0" in sql
    assert "row_version >= 0" in sql
    assert "stage = 'app_ready'" in sql
    assert "bootstrap_capture_id IS NOT NULL" in sql
    assert "stage IN ('bootstrap_capturing', 'failed', 'canceled')" in sql
    assert "ready_at IS NOT NULL" in sql
    assert "stage = 'failed'" in sql
    assert "error_code IS NOT NULL" in sql
    assert "failed_at IS NOT NULL" in sql


def test_upgrade_retargets_runtime_and_evidence_fks_to_common_capture() -> None:
    migration = _load_migration()
    operations = RecordingOperations()
    migration.op = operations

    migration.upgrade()

    sql = " ".join(operations.statements)
    expected_columns = (
        ("qc_evidence_artifact", "page_capture_id"),
        ("qc_observation_packet", "page_capture_id"),
        ("qc_rule_execution", "page_capture_id"),
        ("qc_inspection_work_item", "page_capture_id"),
        ("qc_v2_work_item", "source_capture_id"),
        ("qc_evidence_manifest", "capture_id"),
        ("qc_surface_session", "current_capture_id"),
        ("qc_surface_ref", "current_capture_id"),
        ("qc_action_execution", "requested_source_capture_id"),
        ("qc_action_execution", "source_capture_id"),
        ("qc_action_execution", "result_capture_id"),
    )
    for table, column in expected_columns:
        assert f"ALTER TABLE {table}" in sql
        assert column in sql
    assert sql.count("REFERENCES qc_capture(id)") >= 10
    assert "DEFERRABLE INITIALLY DEFERRED" in sql


def test_downgrade_refuses_to_erase_app_runtime_facts() -> None:
    migration = _load_migration()
    operations = RecordingOperations()
    migration.op = operations

    migration.downgrade()

    sql = " ".join(operations.statements)
    assert "IF EXISTS (SELECT 1 FROM qc_app_bootstrap)" in sql
    assert "IF EXISTS (SELECT 1 FROM qc_app_capture)" in sql
    assert "RAISE EXCEPTION" in sql
    assert "REFERENCES qc_page_capture(id)" in sql
    assert "DROP TABLE IF EXISTS qc_app_bootstrap" in sql
