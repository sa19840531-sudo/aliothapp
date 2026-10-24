"""需保留封存证据的用例不得污染共享隔离库的后续全局统计。"""

import os

import asyncpg

from services.api.tests.test_exploration_scheduler_facts_postgres_integration import (
    _database,
)
from services.api.tests.test_exploration_scheduler_facts_postgres_integration import (
    _upgrade_isolated_postgres as _upgrade_isolated_postgres,
)
from services.api.tests.test_exploration_scheduler_facts_postgres_integration import (
    pytestmark as pytestmark,
)


async def test_retained_case_uses_private_database_and_preserves_shared_counts():
    shared = await asyncpg.connect(os.environ["ALIOTH_TEST_DATABASE_URL"])
    try:
        before = await shared.fetchval("SELECT count(*) FROM qc_task")
        name = await shared.fetchval("SELECT current_database()")
        async with _database(
            surface_kind="web", target_platform="h5", canonical_h5=True, retain_sealed=True,
        ) as (connection, _, ids):
            assert await connection.fetchval("SELECT current_database()") != name
            assert await connection.fetchval(
                "SELECT count(*) FROM qc_task WHERE id=$1::uuid", ids["task"],
            )
            assert await shared.fetchval("SELECT count(*) FROM qc_task") == before
        assert await shared.fetchval("SELECT count(*) FROM qc_task") == before
    finally:
        await shared.close()
