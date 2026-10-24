"""聚合运行时结果身份与期望候选迁移合同。"""

from __future__ import annotations

import importlib.util
from pathlib import Path

MIGRATION_PATH = (
    Path(__file__).parents[1]
    / "migrations"
    / "versions"
    / "20260818_0067_aggregate_inspection_runtime.py"
)


class RecordingOperations:
    def __init__(self) -> None:
        self.statements: list[str] = []

    def execute(self, statement: str) -> None:
        self.statements.append(" ".join(statement.split()))


def _load():
    spec = importlib.util.spec_from_file_location("aggregate_runtime_migration", MIGRATION_PATH)
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


def test_runtime_migration_follows_app_publication_head() -> None:
    migration = _load()
    assert migration.revision == "20260818_0067"
    assert migration.down_revision == "20260818_0066"


def test_candidate_and_result_identity_never_borrow_page_capture() -> None:
    sql = _sql("upgrade")

    assert "CREATE TABLE qc_aggregate_inspection_candidate" in sql
    assert "UNIQUE (task_id, rule_id, scope_key, finalization_epoch)" in sql
    assert "ADD COLUMN aggregate_work_item_id uuid" in sql
    assert "page_capture_id IS NULL" in sql
    assert "uq_qc_rule_execution_aggregate_work" in sql
    assert "uq_qc_observation_packet_aggregate_work" in sql


def test_runtime_downgrade_refuses_to_drop_aggregate_facts() -> None:
    sql = _sql("downgrade")
    assert "cannot downgrade aggregate runtime while aggregate facts exist" in sql
