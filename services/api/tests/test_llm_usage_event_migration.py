"""LLM 调用成本流水迁移的 asyncpg 兼容性回归测试。"""

from __future__ import annotations

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

MIGRATION = (
    Path(__file__).parents[1]
    / "migrations"
    / "versions"
    / "20260826_0078_llm_usage_event_audit.py"
)


class AsyncpgCompatibleOperations:
    """模拟 asyncpg prepared statement 的单命令执行边界。"""

    def __init__(self) -> None:
        self.statements: list[str] = []

    def execute(self, statement: str) -> None:
        commands = [
            command.strip()
            for command in statement.split(";")
            if command.strip()
        ]
        assert len(commands) == 1, "asyncpg prepared statement 每次只允许一条 SQL 命令"
        self.statements.append(commands[0])


def _load_migration():
    spec = spec_from_file_location("llm_usage_event_audit", MIGRATION)
    assert spec is not None and spec.loader is not None
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_llm_usage_event_migration_is_current_head_extension() -> None:
    migration = _load_migration()
    source = MIGRATION.read_text(encoding="utf-8")

    assert migration.revision == "20260826_0078"
    assert migration.down_revision == "20260824_0077"
    assert "CREATE TABLE qc_llm_usage_event" in source
    assert "CREATE INDEX idx_qc_llm_usage_event_model" in source


def test_llm_usage_event_comments_are_single_prepared_statements() -> None:
    migration = _load_migration()

    assert migration._COMMENT_STATEMENTS
    for statement in migration._COMMENT_STATEMENTS:
        assert statement.startswith("COMMENT ON ")
        assert statement.count("COMMENT ON ") == 1
        assert ";" not in statement


def test_upgrade_emits_one_sql_command_per_asyncpg_execute_call() -> None:
    migration = _load_migration()
    operations = AsyncpgCompatibleOperations()
    migration.op = operations

    migration.upgrade()

    comment_statements = [
        statement
        for statement in operations.statements
        if statement.lstrip().upper().startswith("COMMENT ON ")
    ]
    assert len(comment_statements) == len(migration._COMMENT_STATEMENTS)
