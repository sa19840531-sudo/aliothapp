"""隔离 PostgreSQL 验证历史非终态扫描与当前合同排除。"""

import json

import pytest

from services.api.app.infrastructure.queue.state_store import PostgresAgentRunStateStore
from services.api.tests.test_web_exploration_budget_ledger_postgres_integration import (
    _database,
)
from services.api.tests.test_web_exploration_budget_ledger_postgres_integration import (
    _upgrade_isolated_postgres as _upgrade_isolated_postgres,
)
from services.api.tests.test_web_exploration_budget_ledger_postgres_integration import (
    pytestmark as pytestmark,
)


@pytest.mark.parametrize("status", ["pending", "running"])
async def test_postgres_recovers_only_nonterminal_legacy_web(status):
    async with _database() as (connection, pool, _other, ids):
        store = PostgresAgentRunStateStore(pool=pool)
        for platform, version, canceled, expected in [
            ("web", None, False, True),
            ("web", "web-runtime.v2", False, False),
            ("web", None, True, False),
            ("app", None, False, False),
        ]:
            await connection.execute(
                "UPDATE qc_agent_run SET status=$2, input_context=$3::jsonb, "
                "output_context=$4::jsonb WHERE id=$1::uuid",
                ids["run"], status,
                json.dumps({"task_config": {
                    "_task_target": {"platform": platform}, "web_runtime_version": version,
                }}),
                json.dumps({"cancel_requested": canceled}),
            )
            records = await store.list_legacy_web_recovery_records(limit=100)
            assert (ids["run"] in {record.run_id for record in records}) is expected
