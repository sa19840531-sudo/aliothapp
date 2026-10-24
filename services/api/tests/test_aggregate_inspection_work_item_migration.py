"""EvidenceInputSet 聚合质检工作项迁移合同测试。"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

MIGRATION_PATH = (
    Path(__file__).parents[1]
    / "migrations"
    / "versions"
    / "20260814_0061_aggregate_inspection_work_item.py"
)


class RecordingOperations:
    def __init__(self) -> None:
        self.statements: list[str] = []

    def execute(self, statement: str) -> None:
        self.statements.append(" ".join(statement.split()))


def _load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "aggregate_inspection_work_item_migration",
        MIGRATION_PATH,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _sql(method: str) -> str:
    migration = _load_migration()
    operations = RecordingOperations()
    migration.op = operations
    getattr(migration, method)()
    return " ".join(operations.statements)


def test_migration_follows_evidence_input_set_head() -> None:
    migration = _load_migration()

    assert migration.revision == "20260814_0061"
    assert migration.down_revision == "20260814_0060"


def test_work_item_binds_input_set_without_page_capture_anchor() -> None:
    sql = _sql("upgrade")

    assert "CREATE TABLE qc_aggregate_inspection_work_item" in sql
    assert "FOREIGN KEY (task_id, evidence_input_set_id)" in sql
    assert "REFERENCES qc_evidence_input_set(task_id, input_set_id)" in sql
    work_sql = sql.split("CREATE TABLE qc_aggregate_inspection_attempt", maxsplit=1)[0]
    assert "page_capture_id" not in work_sql
    assert "scope IN ('journey', 'task', 'environment_matrix')" in work_sql


def test_work_and_attempt_have_fencing_and_result_lifecycle_constraints() -> None:
    sql = _sql("upgrade")

    assert "CREATE TABLE qc_aggregate_inspection_attempt" in sql
    assert "consumer_agent_run_id IS NOT NULL" in sql
    assert "lease_token IS NOT NULL" in sql
    assert "attempt_count <= max_attempts" in sql
    assert "status IN ('succeeded', 'evidence_insufficient')" in sql
    assert "result_payload <> '{}'::jsonb" in sql
    assert "UNIQUE (work_item_id, attempt_no)" in sql


def test_downgrade_refuses_to_delete_audit_rows_and_drops_child_first() -> None:
    sql = _sql("downgrade")

    assert "qc_aggregate_inspection_attempt LIMIT 1" in sql
    assert "cannot downgrade aggregate inspection work while audit rows exist" in sql
    assert sql.index("DROP TABLE IF EXISTS qc_aggregate_inspection_attempt") < sql.index(
        "DROP TABLE IF EXISTS qc_aggregate_inspection_work_item"
    )
