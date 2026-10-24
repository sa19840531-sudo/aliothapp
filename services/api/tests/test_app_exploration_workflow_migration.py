"""App 探索 Evidence Index 迁移 SQL 合同测试。"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

MIGRATION_PATH = (
    Path(__file__).parents[1]
    / "migrations"
    / "versions"
    / "20260811_0046_app_exploration_evidence_index.py"
)
RESULT_MIGRATION_PATH = (
    Path(__file__).parents[1]
    / "migrations"
    / "versions"
    / "20260812_0047_app_exploration_result.py"
)


class RecordingOperations:
    def __init__(self) -> None:
        self.statements: list[str] = []

    def execute(self, statement: str) -> None:
        self.statements.append(" ".join(statement.split()))


def _load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location("app_evidence_index_migration", MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_result_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "app_exploration_result_migration",
        RESULT_MIGRATION_PATH,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_upgrade_adds_unique_index_entries_and_sealed_guards() -> None:
    migration = _load_migration()
    operations = RecordingOperations()
    migration.op = operations

    migration.upgrade()

    sql = " ".join(operations.statements)
    assert migration.revision == "20260811_0046"
    assert migration.down_revision == "20260811_0045"
    assert "CREATE TABLE qc_app_evidence_index" in sql
    assert "CREATE TABLE qc_app_evidence_index_entry" in sql
    assert "UNIQUE (agent_run_id)" in sql
    assert "status IN ('building', 'sealed')" in sql
    assert "sequence_no > 0" in sql
    assert "UNIQUE (index_id, sequence_no)" in sql
    assert "quality_status <> 'pass'" in sql
    assert "Evidence Index 已封存" in sql
    assert "content_sha256 IS NOT NULL" in sql
    assert "sealed_at IS NOT NULL" in sql


def test_upgrade_creates_unique_app_exploration_result_with_result_hash() -> None:
    migration = _load_result_migration()
    operations = RecordingOperations()
    migration.op = operations

    migration.upgrade()

    sql = " ".join(operations.statements)
    assert migration.revision == "20260812_0047"
    assert migration.down_revision == "20260811_0046"
    assert "CREATE TABLE qc_app_exploration_result" in sql
    # agent_run_id 为单结果主键也满足每个 Agent Run 唯一；Evidence Index 使用
    # 显式 UNIQUE 约束（两种 SQL 书写均锁定同一数据库合同）。
    assert "agent_run_id uuid PRIMARY KEY" in sql or "UNIQUE (agent_run_id)" in sql
    assert (
        "UNIQUE (evidence_index_id)" in sql
        or "evidence_index_id varchar(96) NOT NULL UNIQUE" in sql
    )
    assert "REFERENCES qc_agent_run(id)" in sql
    assert "REFERENCES qc_task(id)" in sql
    assert "REFERENCES qc_app_evidence_index(index_id)" in sql
    assert "REFERENCES qc_surface_session (task_id, session_id)" in sql
    assert "result_sha256" in sql
    assert "outcome IN ('exploration_completed', 'evidence_insufficient')" in sql
    assert "result_sha256 varchar(128) NOT NULL" in sql
    assert "result_sha256 ~ '^sha256:[0-9a-f]{64}$'" in sql


def test_upgrade_guards_sealed_app_exploration_result_from_mutation() -> None:
    migration = _load_result_migration()
    operations = RecordingOperations()
    migration.op = operations

    migration.upgrade()

    sql = " ".join(operations.statements)
    assert "CREATE OR REPLACE FUNCTION qc_guard_app_exploration_result" in sql
    assert "CREATE TRIGGER" in sql
    assert "ON qc_app_exploration_result" in sql
    assert "RAISE EXCEPTION" in sql
    assert "TG_OP <> 'INSERT'" in sql
    assert "禁止修改或删除" in sql or "已封存" in sql
