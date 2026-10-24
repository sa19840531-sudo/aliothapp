"""在隔离 PG 临时表验证大运行上下文的计量提取及真实账本字段。"""

import json
import os
from contextlib import asynccontextmanager
from uuid import uuid4

import asyncpg
import pytest

from services.api.app.infrastructure.db.cost_report_repository import PostgresCostReportRepository
from services.api.app.infrastructure.db.llm_usage_event_repository import (
    PostgresLLMUsageEventRepository,
)
from services.api.app.infrastructure.db.test_database_guard import (
    require_isolated_test_database_url,
)


class _Pool:
    def __init__(self, connection):
        self.connection = connection

    @asynccontextmanager
    async def acquire(self):
        yield self.connection


@pytest.fixture
async def cost_case():
    url = os.getenv("ALIOTH_TEST_DATABASE_URL")
    if not url:
        pytest.skip("需要显式设置完整迁移后的隔离 ALIOTH_TEST_DATABASE_URL")
    connection = await asyncpg.connect(
        require_isolated_test_database_url(url, os.getenv("ALIOTH_DATABASE_URL") or None)
    )
    try:
        for name in ("qc_task", "qc_agent_run", "qc_llm_usage_event"):
            await connection.execute(
                f"CREATE TEMP TABLE {name} (LIKE public.{name} INCLUDING DEFAULTS)"
            )
        task, run = str(uuid4()), str(uuid4())
        await connection.execute(
            """INSERT INTO qc_task(id,target_url,ruleset_id,submitter_id,tenant_id,task_config)
               VALUES($1::uuid,'https://example.test/',$1::uuid,$1::uuid,$1::uuid,
                      '{"task_name":"计量回归"}')""",
            task,
        )
        await connection.execute(
            """INSERT INTO qc_agent_run(id,task_id,agent_type,agent_name,status)
               VALUES($1::uuid,$2::uuid,'main','计量回归','done')""",
            run,
            task,
        )
        yield connection, _Pool(connection), task, run
    finally:
        await connection.close()


async def test_cost_query_preserves_usage_types_and_nested_breakdown_with_large_trace(cost_case):
    connection, pool, task, run = cost_case
    usage = {
        "input_tokens": 101,
        "output_tokens": 27,
        "cache_hit_tokens": "42",
        "billable_input_tokens": 99,
        "input_cost": 1.25,
        "total_cost": "错误类型",
        "cost_currency": "CNY",
        "pricing_source": "回归计价",
        "pricing_configured": True,
        "applied_pricing_tiers": ["测试阶梯"],
        "pricing_breakdown": [{"input_cost": 1.25}],
        "provider": "测试供应商",
        "model": "测试模型",
    }
    await connection.execute(
        "UPDATE qc_agent_run SET output_context=$2::jsonb WHERE id=$1::uuid",
        run,
        json.dumps({"usage": usage, "unrelated_trace": [str(uuid4()) for _ in range(3000)]}),
    )
    await connection.execute(
        """INSERT INTO qc_agent_run(id,task_id,agent_type,agent_name,status,created_at)
           VALUES($1::uuid,$2::uuid,'main','过期运行','done',now()-interval '40 days')""",
        str(uuid4()),
        task,
    )
    rows = await PostgresCostReportRepository(pool=pool).list_recent_runs(days=30)
    assert len(rows) == 1
    row = rows[0]
    assert row["run_id"] == run and row["task_id"] == task
    assert row["input_tokens"] == 101 and row["output_tokens"] == 27
    assert row["cache_hit_tokens"] == 0
    assert row["billable_input_tokens"] == 99
    assert row["input_cost"] == 1.25 and row["total_cost"] is None
    assert row["pricing_configured"] is True
    assert json.loads(row["applied_pricing_tiers"]) == usage["applied_pricing_tiers"]
    assert json.loads(row["pricing_breakdown"]) == usage["pricing_breakdown"]
    assert row["provider"] == usage["provider"] and row["model"] == usage["model"]
    assert row["task_usage_event_count"] is None


async def test_cost_query_keeps_task_ledger_and_missing_usage_distinct(cost_case):
    _connection, pool, task, run = cost_case
    ledger = PostgresLLMUsageEventRepository(pool=pool)
    for usage_available, tokens, calls in ((True, 123, 1), (False, 0, 0)):
        await ledger.record(
            {
                "scope_type": "task_run",
                "task_id": task,
                "agent_run_id": run,
                "source": "查询回归",
                "provider": "测试供应商",
                "model": "测试模型",
                "usage_available": usage_available,
                "input_tokens": tokens,
                "total_tokens": tokens,
                "ai_calls": calls,
            }
        )
    row = (await PostgresCostReportRepository(pool=pool).list_recent_runs(days=30))[0]
    assert row["task_usage_event_count"] == 2
    assert row["task_usage_total_tokens"] == 123
    assert row["task_usage_ai_calls"] == 1
    assert row["task_usage_missing_count"] == row["run_usage_missing_count"] == 1
    assert json.loads(row["task_usage_providers"]) == ["测试供应商"]
