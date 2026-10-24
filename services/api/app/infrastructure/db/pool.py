"""进程级 PostgreSQL 连接池：按数据库地址复用并保证首次初始化并发安全。"""

import asyncio
import os
from typing import Any

import asyncpg

DEFAULT_POSTGRES_POOL_MIN_SIZE = 1
DEFAULT_POSTGRES_POOL_MAX_SIZE = 10
DEFAULT_POSTGRES_OPERATION_TIMEOUT_SECONDS = 10.0


class BoundedPostgresPool:
    """为 asyncpg Pool 的获取与归还补上统一超时预算。"""

    def __init__(self, pool: Any, *, operation_timeout: float) -> None:
        self._pool = pool
        self._operation_timeout = operation_timeout

    def acquire(self, *, timeout: float | None = None) -> Any:
        """获取连接，并把同一预算传给后续连接归还清理。"""
        resolved_timeout = self._operation_timeout if timeout is None else timeout
        return self._pool.acquire(timeout=resolved_timeout)

    async def release(self, connection: Any, *, timeout: float | None = None) -> None:
        """显式归还连接时同样保持有界。"""
        resolved_timeout = self._operation_timeout if timeout is None else timeout
        await self._pool.release(connection, timeout=resolved_timeout)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._pool, name)


class PostgresPoolManager:
    """管理当前进程内按数据库地址隔离的 asyncpg 连接池。"""

    def __init__(
        self,
        *,
        min_size: int = DEFAULT_POSTGRES_POOL_MIN_SIZE,
        max_size: int = DEFAULT_POSTGRES_POOL_MAX_SIZE,
        operation_timeout: float = DEFAULT_POSTGRES_OPERATION_TIMEOUT_SECONDS,
    ) -> None:
        self._min_size = min_size
        self._max_size = max_size
        self._operation_timeout = operation_timeout
        self._pools: dict[str, Any] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    async def get_pool(self, database_url: str) -> Any:
        normalized_url = _normalize_asyncpg_url(database_url)
        pool = self._pools.get(normalized_url)
        if pool is not None:
            return pool

        lock = self._locks.setdefault(normalized_url, asyncio.Lock())
        async with lock:
            pool = self._pools.get(normalized_url)
            if pool is None:
                raw_pool = await asyncpg.create_pool(
                    normalized_url,
                    min_size=self._min_size,
                    max_size=self._max_size,
                    # 这里约束建连；连接获取与归还由 BoundedPostgresPool 单独约束。
                    timeout=self._operation_timeout,
                    command_timeout=60.0,
                    # 远端连接抖动后 prepared statement 清理可能脱离命令超时；
                    # 使用未命名语句避免缓存清理把业务协程永久挂起。
                    statement_cache_size=0,
                )
                pool = BoundedPostgresPool(
                    raw_pool,
                    operation_timeout=self._operation_timeout,
                )
                self._pools[normalized_url] = pool
        return pool


_default_pool_manager = PostgresPoolManager()


async def get_postgres_pool(database_url: str | None = None) -> Any:
    """获取当前进程共享的 PostgreSQL 连接池。"""
    resolved_url = database_url or os.getenv("ALIOTH_DATABASE_URL")
    if not resolved_url:
        raise RuntimeError("缺少数据库连接配置：请设置 ALIOTH_DATABASE_URL")
    return await _default_pool_manager.get_pool(resolved_url)


def _normalize_asyncpg_url(url: str) -> str:
    if url.startswith("postgresql+asyncpg://"):
        return url.replace("postgresql+asyncpg://", "postgresql://", 1)
    return url
