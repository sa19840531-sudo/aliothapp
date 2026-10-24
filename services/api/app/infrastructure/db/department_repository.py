"""PostgreSQL 部门仓储：分公司 → 部门 → 产品 的中间层。

产品与账号通过「部门名」挂到部门上（qc_product.department_name / qc_user.department_name），
所以部门行顺手带上启用产品数与账号数，停用部门前拿它做校验。
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from services.api.app.infrastructure.db.organization_deletion import OrganizationDeletion
from services.api.app.infrastructure.db.pool import get_postgres_pool

DEFAULT_TENANT_ID = "00000000-0000-0000-0000-000000000001"

# 共享产品（tenant_id 为空）算在体验中心名下，其它公司只数自己的产品。
_DEPARTMENT_COLUMNS = f"""
    dept.id::text AS id,
    dept.tenant_id::text AS tenant_id,
    COALESCE(tenant.display_name, '') AS tenant_name,
    dept.name,
    dept.owner_name,
    dept.status,
    dept.monthly_token_budget::float8 AS monthly_token_budget,
    dept.remark,
    dept.created_at,
    dept.updated_at,
    (
        SELECT COUNT(*)
        FROM qc_product AS product
        WHERE product.department_name = dept.name
          AND product.status = 'enabled'
          AND (
              product.tenant_id = dept.tenant_id
              OR (product.tenant_id IS NULL AND dept.tenant_id = '{DEFAULT_TENANT_ID}'::uuid)
          )
    )::int AS product_count,
    (
        SELECT COUNT(*)
        FROM qc_user AS member
        WHERE member.tenant_id = dept.tenant_id
          AND member.department_name = dept.name
          AND member.status = 'enabled'
    )::int AS user_count
"""
_TENANT_JOIN = "LEFT JOIN qc_tenant AS tenant ON tenant.id = dept.tenant_id"


class PostgresDepartmentRepository:
    def __init__(self, *, database_url: str | None = None, pool: Any | None = None) -> None:
        self._database_url = database_url
        self._pool = pool

    async def list_departments(
        self,
        *,
        tenant_id: str | None = None,
        keyword: str | None = None,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            rows = await connection.fetch(
                f"""
                SELECT {_DEPARTMENT_COLUMNS}
                FROM qc_department AS dept
                {_TENANT_JOIN}
                WHERE ($1::uuid IS NULL OR dept.tenant_id = $1::uuid)
                  AND ($2::varchar IS NULL OR dept.name ILIKE ('%%' || $2 || '%%'))
                  AND ($3::varchar IS NULL OR dept.status = $3)
                ORDER BY tenant.created_at ASC, dept.created_at ASC
                """,
                tenant_id,
                keyword,
                status,
            )
        return [dict(row) for row in rows]

    async def find_by_id(self, department_id: str) -> dict[str, Any] | None:
        try:
            UUID(str(department_id))
        except (ValueError, TypeError, AttributeError):
            return None
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                f"""
                SELECT {_DEPARTMENT_COLUMNS}
                FROM qc_department AS dept
                {_TENANT_JOIN}
                WHERE dept.id = $1::uuid
                """,
                department_id,
            )
        return dict(row) if row is not None else None

    async def find_by_name(self, *, tenant_id: str, name: str) -> dict[str, Any] | None:
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                f"""
                SELECT {_DEPARTMENT_COLUMNS}
                FROM qc_department AS dept
                {_TENANT_JOIN}
                WHERE dept.tenant_id = $1::uuid AND LOWER(dept.name) = LOWER($2)
                """,
                tenant_id,
                name,
            )
        return dict(row) if row is not None else None

    async def create_department(self, payload: dict[str, Any]) -> dict[str, Any]:
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                f"""
                WITH inserted AS (
                    INSERT INTO qc_department (
                        tenant_id, name, owner_name, status, monthly_token_budget, remark
                    )
                    VALUES ($1::uuid, $2, $3, $4, $5, $6)
                    RETURNING *
                )
                SELECT {_DEPARTMENT_COLUMNS}
                FROM inserted AS dept
                {_TENANT_JOIN}
                """,
                payload["tenant_id"],
                payload["name"],
                payload.get("owner_name"),
                payload.get("status") or "enabled",
                payload.get("monthly_token_budget"),
                payload.get("remark"),
            )
        return dict(row)

    async def update_department(
        self, department_id: str, payload: dict[str, Any]
    ) -> dict[str, Any] | None:
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                f"""
                WITH updated AS (
                    UPDATE qc_department
                    SET name = $2,
                        owner_name = $3,
                        status = $4,
                        monthly_token_budget = $5,
                        remark = $6,
                        updated_at = now()
                    WHERE id = $1::uuid
                    RETURNING *
                )
                SELECT {_DEPARTMENT_COLUMNS}
                FROM updated AS dept
                {_TENANT_JOIN}
                """,
                department_id,
                payload["name"],
                payload.get("owner_name"),
                payload.get("status") or "enabled",
                payload.get("monthly_token_budget"),
                payload.get("remark"),
            )
        return dict(row) if row is not None else None

    async def delete_department(
        self, department_id: str, *, tenant_id: str | None = None
    ) -> OrganizationDeletion:
        """检查全部状态的名称关联；共享产品仅挂在体验中心，历史任务也不能丢归属。"""
        pool = await self._get_pool()
        async with pool.acquire() as connection, connection.transaction():
            await connection.execute("SET LOCAL lock_timeout = '2s'")
            # 既有关系按名称而非外键关联，检查期间阻止并发新增、改名和归属变更。
            await connection.execute("LOCK TABLE qc_product, qc_user, qc_task IN SHARE ROW EXCLUSIVE MODE")
            department = await connection.fetchrow(
                """
                SELECT tenant_id::text AS tenant_id, name
                FROM qc_department
                WHERE id = $1::uuid AND ($2::uuid IS NULL OR tenant_id = $2::uuid)
                FOR UPDATE
                """,
                department_id, tenant_id,
            )
            if department is None:
                return OrganizationDeletion()
            counts = await connection.fetchrow(
                """
                SELECT
                    (SELECT count(*) FROM qc_product
                     WHERE department_name = $2
                       AND (tenant_id = $1::uuid OR (tenant_id IS NULL AND $1::uuid = $3::uuid)))::int AS "产品",
                    (SELECT count(*) FROM qc_user
                     WHERE tenant_id = $1::uuid AND department_name = $2)::int AS "账号",
                    (SELECT count(*) FROM qc_task
                     WHERE tenant_id = $1::uuid
                       AND (department_name = $2 OR task_config->'product'->>'department_name' = $2))::int AS "任务"
                """,
                department["tenant_id"], department["name"], DEFAULT_TENANT_ID,
            )
            associations = {key: int(value) for key, value in dict(counts).items() if value}
            if associations:
                return OrganizationDeletion(associations=associations)
            await connection.execute("DELETE FROM qc_department WHERE id = $1::uuid", department_id)
        return OrganizationDeletion(deleted=True)

    async def list_budgets(self) -> list[dict[str, Any]]:
        """Token 费用页用：每个部门的月度预算（元）。"""
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            rows = await connection.fetch(
                """
                SELECT tenant_id::text AS tenant_id, name, monthly_token_budget::float8 AS monthly_token_budget
                FROM qc_department
                WHERE monthly_token_budget IS NOT NULL
                """
            )
        return [dict(row) for row in rows]

    async def _get_pool(self) -> Any:
        if self._pool is not None:
            return self._pool
        return await get_postgres_pool(self._database_url)
