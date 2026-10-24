"""Web 动作前绑定的实际 SQL 必须能由 PostgreSQL 编译。"""

import ast
import inspect
import os
import textwrap

import asyncpg
import pytest

from services.api.app.infrastructure.browser.ui_snapshot_reader import (
    PostgresTrustedUiSnapshotReader,
)
from services.api.app.infrastructure.db.test_database_guard import (
    require_isolated_test_database_url,
)


def _binding_sql():
    source = inspect.getsource(PostgresTrustedUiSnapshotReader.read_web_before_action_binding)
    tree = ast.parse(textwrap.dedent(source))
    queries = [
        node.value for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
        and "SELECT" in node.value and "FROM qc_capture AS before_capture" in node.value
    ]
    assert len(queries) == 1
    return queries[0]


def test_web_binding_sql_does_not_embed_python_comment():
    assert not any(line.lstrip().startswith("#") for line in _binding_sql().splitlines())


@pytest.mark.skipif(
    not os.getenv("ALIOTH_TEST_DATABASE_URL") or not os.getenv("ALIOTH_DATABASE_URL"),
    reason="需要显式隔离的 test/primary 双 DSN 与已迁移测试库",
)
async def test_web_binding_actual_sql_prepares_on_isolated_postgres():
    url = require_isolated_test_database_url(
        os.environ["ALIOTH_TEST_DATABASE_URL"], os.environ["ALIOTH_DATABASE_URL"],
    ).replace("postgresql+asyncpg://", "postgresql://", 1)
    connection = await asyncpg.connect(url)
    try:
        async with connection.transaction(readonly=True):
            statement = await connection.prepare(_binding_sql())
            assert len(statement.get_parameters()) == 2
            assert "capture_id" in {item.name for item in statement.get_attributes()}
    finally:
        await connection.close()
