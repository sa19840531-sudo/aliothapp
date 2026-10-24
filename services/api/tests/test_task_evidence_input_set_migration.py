"""任务级 EvidenceInputSet 持久化迁移合同测试。"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

MIGRATION_PATH = (
    Path(__file__).parents[1]
    / "migrations"
    / "versions"
    / "20260814_0060_task_evidence_input_set.py"
)


class RecordingOperations:
    def __init__(self) -> None:
        self.statements: list[str] = []

    def execute(self, statement: str) -> None:
        self.statements.append(" ".join(statement.split()))


def _load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "task_evidence_input_set_migration",
        MIGRATION_PATH,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _upgrade_sql() -> str:
    migration = _load_migration()
    operations = RecordingOperations()
    migration.op = operations
    migration.upgrade()
    return " ".join(operations.statements)


def test_migration_follows_current_linear_head() -> None:
    migration = _load_migration()

    assert migration.revision == "20260814_0060"
    assert migration.down_revision == "20260814_0059"


def test_main_identity_is_task_scoped_without_page_anchor() -> None:
    sql = _upgrade_sql()

    assert "CREATE TABLE qc_evidence_input_set" in sql
    assert "source_agent_run_id uuid NOT NULL" in sql
    assert "finalization_epoch integer NOT NULL" in sql
    assert "scope IN ('page', 'journey', 'task', 'environment_matrix')" in sql
    assert "input_set_id = 'evidence-input-set:' || input_set_hash" in sql
    assert "protocol_version = 'evidence-input-set.v1'" in sql
    main_sql = sql.split("CREATE TABLE qc_evidence_input_set_member", maxsplit=1)[0]
    assert "page_capture_id" not in main_sql


def test_member_identity_has_composite_task_capture_and_manifest_lineage() -> None:
    sql = _upgrade_sql()

    assert "FOREIGN KEY (task_id, source_agent_run_id)" in sql
    assert "REFERENCES qc_agent_run(task_id, id)" in sql
    assert "FOREIGN KEY (task_id, capture_id)" in sql
    assert "REFERENCES qc_capture(task_id, id)" in sql
    assert "manifest_version, manifest_hash" in sql
    assert "REFERENCES qc_evidence_manifest" in sql
    assert sql.count("DEFERRABLE INITIALLY DEFERRED") == 3
    assert "jsonb_array_length(artifact_ids) > 0" in sql


def test_aggregate_identity_is_once_per_rule_scope_and_epoch() -> None:
    sql = _upgrade_sql()

    assert (
        "UNIQUE ( task_id, rule_id, scope, scope_key, finalization_epoch )"
        in sql
    )


def test_input_set_and_members_are_immutable() -> None:
    sql = _upgrade_sql()

    assert "BEFORE UPDATE ON qc_evidence_input_set" in sql
    assert "BEFORE UPDATE ON qc_evidence_input_set_member" in sql
    assert "to_jsonb(NEW) IS DISTINCT FROM to_jsonb(OLD)" in sql


def test_downgrade_refuses_to_drop_sealed_rows() -> None:
    migration = _load_migration()
    operations = RecordingOperations()
    migration.op = operations

    migration.downgrade()

    sql = " ".join(operations.statements)
    assert "IF EXISTS (SELECT 1 FROM qc_evidence_input_set LIMIT 1)" in sql
    assert "EXISTS (SELECT 1 FROM qc_evidence_input_set_member LIMIT 1)" in sql
    assert "cannot downgrade evidence input set schema while sealed rows exist" in sql
    assert sql.index("DROP TABLE IF EXISTS qc_evidence_input_set_member") < sql.index(
        "DROP TABLE IF EXISTS qc_evidence_input_set "
    )
