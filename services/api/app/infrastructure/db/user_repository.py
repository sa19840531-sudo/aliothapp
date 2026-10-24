"""PostgreSQL 用户仓储实现。"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from services.api.app.infrastructure.db.pool import get_postgres_pool

_DEFAULT_TENANT_ID = "00000000-0000-0000-0000-000000000001"

# 账号行始终带上所属公司的名字和状态：登录态要拒绝已停用公司，页面要展示公司名。
_USER_COLUMNS = """
    account.id::text AS id,
    account.username,
    account.full_name,
    account.password_hash,
    account.role,
    account.status,
    account.tenant_id::text AS tenant_id,
    COALESCE(tenant.display_name, '') AS tenant_name,
    COALESCE(tenant.status, '') AS tenant_status,
    account.department_name,
    account.last_login_at,
    account.created_at,
    account.updated_at
"""
_TENANT_JOIN = "LEFT JOIN qc_tenant AS tenant ON tenant.id = account.tenant_id"


class PostgresUserRepository:
    def __init__(self, *, database_url: str | None = None, pool: Any | None = None) -> None:
        self._database_url = database_url
        self._pool = pool

    async def find_by_username(self, username: str) -> dict[str, Any] | None:
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                f"""
                SELECT {_USER_COLUMNS}
                FROM qc_user AS account
                {_TENANT_JOIN}
                WHERE LOWER(account.username) = LOWER($1)
                """,
                username,
            )
        return dict(row) if row is not None else None

    async def find_by_id(self, user_id: str) -> dict[str, Any] | None:
        """按用户 ID 加载当前用户事实，供下载票据兑现时重查权限使用。"""
        try:
            UUID(str(user_id))
        except (ValueError, TypeError, AttributeError):
            return None
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                f"""
                SELECT {_USER_COLUMNS}
                FROM qc_user AS account
                {_TENANT_JOIN}
                WHERE account.id = $1::uuid
                """,
                user_id,
            )
        return dict(row) if row is not None else None

    async def list_users(
        self,
        *,
        keyword: str | None,
        status: str | None,
        page: int,
        page_size: int,
        tenant_id: str | None = None,
        role: str | None = None,
        department_name: str | None = None,
    ) -> tuple[list[dict[str, Any]], int]:
        pool = await self._get_pool()
        offset = max(page - 1, 0) * page_size
        async with pool.acquire() as connection:
            rows = await connection.fetch(
                f"""
                WITH filtered AS (
                    SELECT {_USER_COLUMNS}
                    FROM qc_user AS account
                    {_TENANT_JOIN}
                    WHERE (
                        $1::varchar IS NULL
                        OR account.username ILIKE ('%%' || $1 || '%%')
                        OR account.full_name ILIKE ('%%' || $1 || '%%')
                    )
                      AND ($2::varchar IS NULL OR account.status = $2)
                      AND ($5::uuid IS NULL OR account.tenant_id = $5::uuid)
                      AND ($6::varchar IS NULL OR account.role = $6)
                      AND ($7::varchar IS NULL OR account.department_name = $7)
                )
                SELECT *, COUNT(*) OVER() AS total
                FROM filtered
                ORDER BY created_at DESC
                LIMIT $3 OFFSET $4
                """,
                keyword,
                status,
                page_size,
                offset,
                tenant_id,
                role,
                department_name,
            )
        total = int(rows[0]["total"]) if rows else 0
        return [dict(row) for row in rows], total

    async def create_user(self, payload: dict[str, Any]) -> dict[str, Any]:
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                f"""
                WITH inserted AS (
                    INSERT INTO qc_user (
                        username,
                        full_name,
                        password_hash,
                        role,
                        status,
                        tenant_id,
                        department_name
                    )
                    VALUES ($1, $2, $3, $4, $5, $6::uuid, $7)
                    RETURNING *
                )
                SELECT {_USER_COLUMNS}
                FROM inserted AS account
                {_TENANT_JOIN}
                """,
                payload["username"],
                payload["full_name"],
                payload["password_hash"],
                payload["role"],
                payload["status"],
                payload.get("tenant_id") or _DEFAULT_TENANT_ID,
                payload.get("department_name"),
            )
        return dict(row)

    async def update_user(self, user_id: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                f"""
                WITH updated AS (
                    UPDATE qc_user
                    SET full_name = $2,
                        role = $3,
                        status = $4,
                        password_hash = COALESCE($5, password_hash),
                        tenant_id = COALESCE($6::uuid, tenant_id),
                        department_name = $7,
                        updated_at = now()
                    WHERE id = $1::uuid
                    RETURNING *
                )
                SELECT {_USER_COLUMNS}
                FROM updated AS account
                {_TENANT_JOIN}
                """,
                user_id,
                payload["full_name"],
                payload["role"],
                payload["status"],
                payload.get("password_hash"),
                payload.get("tenant_id"),
                payload.get("department_name"),
            )
        return dict(row) if row is not None else None

    async def delete_user(self, user_id: str) -> bool:
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                "DELETE FROM qc_user WHERE id = $1::uuid RETURNING id",
                user_id,
            )
        return row is not None

    async def touch_last_login(self, user_id: str) -> None:
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            await connection.execute(
                """
                UPDATE qc_user
                SET last_login_at = now(),
                    updated_at = now()
                WHERE id = $1::uuid
                """,
                user_id,
            )

    async def _get_pool(self) -> Any:
        if self._pool is not None:
            return self._pool
        return await get_postgres_pool(self._database_url)
