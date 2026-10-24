"""PostgreSQL 连接池并发初始化、跨仓储复用与有界清理测试。"""

import asyncio
from typing import Any

import asyncpg
import pytest

from services.api.app.infrastructure.db import pool as pool_module
from services.api.app.infrastructure.db.rule_repository import PostgresRuleRepository
from services.api.app.infrastructure.db.task_insight_repository import (
    PostgresTaskInsightRepository,
)
from services.api.app.infrastructure.db.task_repository import PostgresTaskRepository
from services.api.app.infrastructure.db.user_repository import PostgresUserRepository
from services.api.app.infrastructure.db.workstation_dashboard_repository import (
    PostgresWorkstationDashboardRepository,
)
from services.api.app.infrastructure.db.workstation_query_repository import (
    PostgresWorkstationQueryRepository,
)
from services.api.app.infrastructure.db.workstation_review_repository import (
    PostgresWorkstationReviewRepository,
)
from services.api.app.infrastructure.queue.state_store import PostgresAgentRunStateStore


async def test_concurrent_first_access_creates_one_bounded_pool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    create_calls: list[tuple[str, dict[str, Any]]] = []

    async def _fake_create_pool(database_url: str, **kwargs: Any) -> object:
        create_calls.append((database_url, kwargs))
        await asyncio.sleep(0.01)
        return object()

    monkeypatch.setattr(asyncpg, "create_pool", _fake_create_pool)
    repository = PostgresTaskRepository(
        database_url="postgresql+asyncpg://user:password@localhost/alioth_pool_concurrency"
    )

    pools = await asyncio.gather(*(repository._get_pool() for _ in range(20)))

    assert create_calls == [
        (
            "postgresql://user:password@localhost/alioth_pool_concurrency",
            {
                "min_size": 1,
                "max_size": 10,
                "timeout": 10.0,
                "command_timeout": 60.0,
                "statement_cache_size": 0,
            },
        )
    ]
    assert all(pool is pools[0] for pool in pools)


async def test_postgres_repositories_share_pool_for_same_database_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    create_calls: list[tuple[str, dict[str, Any]]] = []

    async def _fake_create_pool(database_url: str, **kwargs: Any) -> object:
        create_calls.append((database_url, kwargs))
        await asyncio.sleep(0.01)
        return object()

    monkeypatch.setattr(asyncpg, "create_pool", _fake_create_pool)
    database_url = "postgresql+asyncpg://user:password@localhost/alioth_pool_shared"
    pool_owners = [
        PostgresTaskRepository(database_url=database_url),
        PostgresRuleRepository(database_url=database_url),
        PostgresUserRepository(database_url=database_url),
        PostgresAgentRunStateStore(database_url=database_url),
        PostgresTaskInsightRepository(database_url=database_url),
        PostgresWorkstationDashboardRepository(database_url=database_url),
        PostgresWorkstationQueryRepository(database_url=database_url),
        PostgresWorkstationReviewRepository(database_url=database_url),
    ]

    pools = await asyncio.gather(*(owner._get_pool() for owner in pool_owners))

    assert len(create_calls) == 1
    assert all(pool is pools[0] for pool in pools)


class _FakePool:
    def __init__(self) -> None:
        self.acquire_timeouts: list[float | None] = []
        self.release_timeouts: list[float | None] = []

    def acquire(self, *, timeout: float | None = None) -> object:
        self.acquire_timeouts.append(timeout)
        return object()

    async def release(
        self,
        connection: Any,
        *,
        timeout: float | None = None,
    ) -> None:
        del connection
        self.release_timeouts.append(timeout)


@pytest.mark.asyncio
async def test_pool_manager_disables_statement_cache_and_bounds_pool_operations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw_pool = _FakePool()
    create_calls: list[tuple[str, dict[str, Any]]] = []

    async def fake_create_pool(database_url: str, **kwargs: Any) -> _FakePool:
        create_calls.append((database_url, kwargs))
        return raw_pool

    monkeypatch.setattr(pool_module.asyncpg, "create_pool", fake_create_pool)
    manager = pool_module.PostgresPoolManager(operation_timeout=7.5)

    bounded_pool = await manager.get_pool("postgresql+asyncpg://db.example/alioth")
    bounded_pool.acquire()
    await bounded_pool.release(object())

    assert create_calls == [
        (
            "postgresql://db.example/alioth",
            {
                "min_size": 1,
                "max_size": 10,
                "timeout": 7.5,
                "command_timeout": 60.0,
                "statement_cache_size": 0,
            },
        )
    ]
    assert raw_pool.acquire_timeouts == [7.5]
    assert raw_pool.release_timeouts == [7.5]


@pytest.mark.asyncio
async def test_bounded_pool_preserves_explicit_operation_timeout() -> None:
    raw_pool = _FakePool()
    bounded_pool = pool_module.BoundedPostgresPool(
        raw_pool,
        operation_timeout=10.0,
    )

    bounded_pool.acquire(timeout=2.0)
    await bounded_pool.release(object(), timeout=3.0)

    assert raw_pool.acquire_timeouts == [2.0]
    assert raw_pool.release_timeouts == [3.0]
