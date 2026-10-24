"""规则适用范围计划迁移合同。"""

from __future__ import annotations

import importlib.util
from pathlib import Path

MIGRATION_PATH = (
    Path(__file__).parents[1] / "migrations" / "versions" / "20260908_0098_rule_scope_plan.py"
)


class RecordingOperations:
    def __init__(self) -> None:
        self.statements: list[str] = []

    def execute(self, statement: str) -> None:
        self.statements.append(" ".join(statement.split()))


def _load():
    spec = importlib.util.spec_from_file_location("rule_scope_plan_migration", MIGRATION_PATH)
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


def test_rule_scope_plan_migration_extends_current_head() -> None:
    migration = _load()
    assert migration.revision == "20260908_0098"
    assert migration.down_revision == "20260907_0097"


def test_rule_scope_plan_is_bound_to_candidate_identity() -> None:
    sql = _sql("upgrade")

    assert "ADD COLUMN rule_scope_plan_hash varchar(128)" in sql
    assert "ADD COLUMN rule_scope_plan jsonb" in sql
    assert "rule-scope-plan.v1" in sql
    assert "source_agent_run_id::text" in sql
    assert "finalization_epoch" in sql
    assert "rule_id::text" in sql
