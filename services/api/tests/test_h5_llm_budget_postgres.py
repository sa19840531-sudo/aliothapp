"""隔离 PostgreSQL 中验证竞争准入、重启不刷新预算及围栏反例。"""

import asyncio
import importlib
import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio

from services.api.app.application.h5_llm_budget import H5LLMBudgetExhausted
from services.api.app.infrastructure.db.h5_llm_budget_repository import (
    PostgresH5LLMBudgetRepository,
)
from services.api.app.infrastructure.db.test_database_guard import (
    require_isolated_test_database_url,
)


@pytest_asyncio.fixture
async def database():
    url = os.getenv("ALIOTH_TEST_DATABASE_URL")
    if not url:
        pytest.skip("需要显式隔离测试库，不使用当前任务库")
    require_isolated_test_database_url(url, os.getenv("ALIOTH_DATABASE_URL"))
    schema = f"h5_budget_test_{uuid4().hex}"
    admin = await asyncpg.connect(url)
    await admin.execute(f'CREATE SCHEMA "{schema}"')
    pool = await asyncpg.create_pool(
        url, server_settings={"search_path": schema}, min_size=1, max_size=8
    )
    try:
        async with pool.acquire() as connection:
            await connection.execute("""
                CREATE TABLE qc_task(id uuid PRIMARY KEY, target_platform text, status text,
                    finalization_epoch int);
                CREATE TABLE qc_agent_run(id uuid PRIMARY KEY, task_id uuid, status text);
                CREATE TABLE qc_llm_usage_event(task_id uuid, ai_calls int);
            """)
            migration = importlib.import_module(
                "services.api.migrations.versions.20261010_0132_h5_llm_call_budget"
            )
            await connection.execute(migration.CREATE_BUDGET_TABLE)
        yield pool
    finally:
        await pool.close()
        await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await admin.close()


async def seed(pool, used=0):
    task, main, judge = (uuid4() for _ in range(3))
    async with pool.acquire() as connection:
        await connection.execute("INSERT INTO qc_task VALUES($1,'h5','running',4)", task)
        await connection.execute(
            "INSERT INTO qc_agent_run VALUES($1,$3,'running'),($2,$3,'running')", main, judge, task
        )
        await connection.execute("INSERT INTO qc_llm_usage_event VALUES($1,$2)", task, used)
    return str(task), str(main), str(judge)


async def test_atomic_physical_call_contention_across_twenty_consumers(database):
    task, main, judge = await seed(database, used=198)
    repository = PostgresH5LLMBudgetRepository(pool=database)
    await repository.initialize(
        task_id=task,
        agent_run_id=main,
        max_calls=200,
        deadline=datetime.now(UTC) + timedelta(minutes=180),
    )
    results = await asyncio.gather(
        *(
            repository.reserve(
                task_id=task,
                consumer_agent_run_id=main if index % 2 else judge,
            )
            for index in range(20)
        ),
        return_exceptions=True,
    )
    assert results.count(None) == 2
    assert sum(isinstance(result, H5LLMBudgetExhausted) for result in results) == 18
    async with database.acquire() as connection:
        assert await connection.fetchval("SELECT admitted_calls FROM qc_h5_llm_call_budget") == 200
        # 预留不是费用流水，不得写假 Token 或费用来占位。
        assert await connection.fetchval("SELECT sum(ai_calls) FROM qc_llm_usage_event") == 198


async def test_reinitialization_never_resets_counter_or_extends_frozen_deadline(database):
    task, main, judge = await seed(database, used=2)
    deadline = datetime.now(UTC) + timedelta(minutes=180)
    repository = PostgresH5LLMBudgetRepository(pool=database)
    await repository.initialize(task_id=task, agent_run_id=main, max_calls=200, deadline=deadline)
    await repository.reserve(task_id=task, consumer_agent_run_id=judge)
    await repository.initialize(
        task_id=task, agent_run_id=main, max_calls=300, deadline=deadline + timedelta(hours=3)
    )
    async with database.acquire() as connection:
        row = await connection.fetchrow("SELECT * FROM qc_h5_llm_call_budget")
    assert row["max_calls"] == 200 and row["admitted_calls"] == 3 and row["deadline"] == deadline


async def test_another_root_run_cannot_replace_same_epoch_budget(database):
    task, main, judge = await seed(database)
    repository = PostgresH5LLMBudgetRepository(pool=database)
    deadline = datetime.now(UTC) + timedelta(minutes=180)
    await repository.initialize(task_id=task, agent_run_id=main, max_calls=200, deadline=deadline)
    with pytest.raises(RuntimeError, match="主 Run"):
        await repository.initialize(
            task_id=task, agent_run_id=judge, max_calls=300, deadline=deadline + timedelta(hours=3)
        )
    async with database.acquire() as connection:
        row = await connection.fetchrow("SELECT * FROM qc_h5_llm_call_budget")
    assert (
        str(row["agent_run_id"]) == main and row["max_calls"] == 200 and row["deadline"] == deadline
    )


@pytest.mark.parametrize(
    "condition", ["remaining", "exhausted", "deadline", "foreign_run", "epoch"],
)
async def test_read_exhaustion_keeps_original_counter_and_scope(database, condition):
    task, main, judge = await seed(database, used=200 if condition == "exhausted" else 3)
    repository = PostgresH5LLMBudgetRepository(pool=database)
    await repository.initialize(
        task_id=task, agent_run_id=main, max_calls=200,
        deadline=datetime.now(UTC) + timedelta(minutes=180),
    )
    async with database.acquire() as connection:
        if condition == "deadline":
            await connection.execute(
                "UPDATE qc_h5_llm_call_budget SET deadline=now()-interval '1 second'"
            )
        if condition == "epoch":
            await connection.execute("UPDATE qc_task SET finalization_epoch=5")
        before = dict(await connection.fetchrow("SELECT * FROM qc_h5_llm_call_budget"))
    if condition in {"foreign_run", "epoch"}:
        with pytest.raises(RuntimeError, match="围栏"):
            await repository.is_exhausted(
                task_id=task, agent_run_id=judge if condition == "foreign_run" else main,
                finalization_epoch=4,
            )
    else:
        assert await repository.is_exhausted(
            task_id=task, agent_run_id=main, finalization_epoch=4,
        ) is (condition in {"exhausted", "deadline"})
    async with database.acquire() as connection:
        assert dict(await connection.fetchrow("SELECT * FROM qc_h5_llm_call_budget")) == before


@pytest.mark.parametrize(
    "fault", ["platform", "epoch", "task", "consumer", "deadline", "uninitialized"]
)
async def test_h5_budget_scope_never_admits_invalid_or_expired_consumer(database, fault):
    task, main, judge = await seed(database)
    repository = PostgresH5LLMBudgetRepository(pool=database)
    if fault != "uninitialized":
        await repository.initialize(
            task_id=task,
            agent_run_id=main,
            max_calls=200,
            deadline=datetime.now(UTC) + timedelta(minutes=180),
        )
    async with database.acquire() as connection:
        if fault == "platform":
            await connection.execute("UPDATE qc_task SET target_platform='web'")
        elif fault == "epoch":
            await connection.execute("UPDATE qc_task SET finalization_epoch=5")
        elif fault == "deadline":
            await connection.execute(
                "UPDATE qc_h5_llm_call_budget SET deadline=now()-interval '1 second'"
            )
    if fault == "task":
        task = str(uuid4())
    if fault == "consumer":
        judge = str(uuid4())
    with pytest.raises(H5LLMBudgetExhausted):
        await repository.reserve(task_id=task, consumer_agent_run_id=judge)
