"""Web 硬预算真实 PostgreSQL 测试的双 DSN 隔离防线。"""

import pytest

from services.api.tests.test_web_exploration_budget_ledger_postgres_integration import (
    WebBudgetPostgresIntegrationConfigurationError,
    _cleanup_web_budget_run,
    _require_web_budget_test_database_url,
)


def test_web_budget_postgres_guard_rejects_missing_primary_before_upgrade() -> None:
    upgrade_called = False

    def guarded_upgrade() -> None:
        nonlocal upgrade_called
        _require_web_budget_test_database_url(
            {"ALIOTH_TEST_DATABASE_URL": "postgresql://tester@db/test_db"}
        )
        upgrade_called = True

    with pytest.raises(
        WebBudgetPostgresIntegrationConfigurationError,
        match="ALIOTH_DATABASE_URL",
    ):
        guarded_upgrade()

    assert upgrade_called is False


def test_web_budget_postgres_guard_rejects_same_database_identity() -> None:
    with pytest.raises(ValueError, match="不能与 ALIOTH_DATABASE_URL 指向同一数据库"):
        _require_web_budget_test_database_url(
            {
                "ALIOTH_TEST_DATABASE_URL": "postgresql://test-role@db.example/test_db",
                "ALIOTH_DATABASE_URL": (
                    "postgresql://primary-role@DB.EXAMPLE:5432/test_db?sslmode=require"
                ),
            }
        )


@pytest.mark.asyncio
async def test_web_budget_postgres_cleanup_deletes_budget_before_task_and_ruleset() -> None:
    class Connection:
        def __init__(self) -> None:
            self.calls = []

        async def execute(self, sql, *args):
            self.calls.append((" ".join(sql.split()), args))
            return "DELETE 0"

    connection = Connection()
    await _cleanup_web_budget_run(
        connection,
        {
            "run": "00000000-0000-0000-0000-000000000001",
            "task": "00000000-0000-0000-0000-000000000002",
            "ruleset": "00000000-0000-0000-0000-000000000003",
            "user": "00000000-0000-0000-0000-000000000004",
            "tenant": "00000000-0000-0000-0000-000000000005",
        },
    )

    assert connection.calls == [
        (
            "DELETE FROM qc_web_exploration_budget WHERE agent_run_id = $1::uuid",
            ("00000000-0000-0000-0000-000000000001",),
        ),
        (
            "DELETE FROM qc_task WHERE id = $1::uuid",
            ("00000000-0000-0000-0000-000000000002",),
        ),
        (
            "DELETE FROM qc_ruleset WHERE id = $1::uuid",
            ("00000000-0000-0000-0000-000000000003",),
        ),
        (
            "DELETE FROM qc_user WHERE id = $1::uuid",
            ("00000000-0000-0000-0000-000000000004",),
        ),
        (
            "DELETE FROM qc_tenant WHERE id = $1::uuid",
            ("00000000-0000-0000-0000-000000000005",),
        ),
    ]
