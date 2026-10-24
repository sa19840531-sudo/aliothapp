"""PostgreSQL 产品仓储实现。"""

from __future__ import annotations

import json
from typing import Any

from services.api.app.infrastructure.db.pool import get_postgres_pool

# 产品行都带所属公司名：tenant_id 为空是运营中心维护的共享产品，全公司可见。
_SELECT_FIELDS = """
    product.id::text AS id,
    product.project_code,
    product.product_name,
    product.h5_url,
    product.department_name,
    product.owner_name,
    product.platforms,
    product.status,
    product.tenant_id::text AS tenant_id,
    tenant.display_name AS tenant_name,
    product.created_at,
    product.updated_at
"""
_TENANT_JOIN = "LEFT JOIN qc_tenant AS tenant ON tenant.id = product.tenant_id"
# 可见范围：$visible 为空看全部；否则看本公司的 + 共享的。
_VISIBILITY_SQL = "($V::uuid IS NULL OR product.tenant_id = $V::uuid OR product.tenant_id IS NULL)"
# 部门账号只看本部门的产品；没挂部门的产品（含共享产品）只有公司级以上账号能看到。
_DEPARTMENT_SQL = "($D::varchar IS NULL OR product.department_name = $D)"


def _platforms(value: object) -> list[str]:
    if not value:
        return []
    return [str(item) for item in value]


class PostgresProductRepository:
    def __init__(self, *, database_url: str | None = None, pool: Any | None = None) -> None:
        self._database_url = database_url
        self._pool = pool

    async def list_products(
        self,
        *,
        keyword: str | None,
        status: str | None,
        page: int,
        page_size: int,
        tenant_id: str | None = None,
        visible_tenant_id: str | None = None,
        department_name: str | None = None,
        platform: str | None = None,
        visible_department: str | None = None,
    ) -> tuple[list[dict[str, Any]], int]:
        """tenant_id 精确筛某家公司（'shared' 只看共享产品）；visible_tenant_id / visible_department 收口可见范围。"""
        pool = await self._get_pool()
        offset = max(page - 1, 0) * page_size
        shared_only = tenant_id == "shared"
        exact_tenant = None if shared_only else tenant_id
        async with pool.acquire() as connection:
            rows = await connection.fetch(
                f"""
                WITH filtered AS (
                    SELECT {_SELECT_FIELDS}
                    FROM qc_product AS product
                    {_TENANT_JOIN}
                    WHERE ($1::varchar IS NULL
                           OR product.project_code ILIKE ('%%' || $1 || '%%')
                           OR product.product_name ILIKE ('%%' || $1 || '%%'))
                      AND ($2::varchar IS NULL OR product.status = $2)
                      AND ($5::uuid IS NULL OR product.tenant_id = $5::uuid)
                      AND (NOT $6::boolean OR product.tenant_id IS NULL)
                      AND {_VISIBILITY_SQL.replace('$V', '$7')}
                      AND ($8::varchar IS NULL OR product.department_name = $8)
                      AND ($9::varchar IS NULL OR $9 = ANY(product.platforms))
                      AND {_DEPARTMENT_SQL.replace('$D', '$10')}
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
                exact_tenant,
                shared_only,
                visible_tenant_id,
                department_name,
                platform,
                visible_department,
            )
        total = int(rows[0]["total"]) if rows else 0
        return [_with_platform_list(_strip_total(row)) for row in rows], total

    async def list_enabled_products(
        self, *, visible_tenant_id: str | None = None, visible_department: str | None = None
    ) -> list[dict[str, Any]]:
        """工作台用：可见范围内的启用产品（含只登记了 App 的产品），带部门/负责人/平台/公司。"""
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            rows = await connection.fetch(
                f"""
                SELECT {_SELECT_FIELDS}
                FROM qc_product AS product
                {_TENANT_JOIN}
                WHERE product.status = 'enabled'
                  AND {_VISIBILITY_SQL.replace('$V', '$1')}
                  AND {_DEPARTMENT_SQL.replace('$D', '$2')}
                ORDER BY product.project_code
                """,
                visible_tenant_id,
                visible_department,
            )
        return [_with_platform_list(dict(row)) for row in rows]

    async def find_by_id(self, product_id: str) -> dict[str, Any] | None:
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                f"""
                SELECT {_SELECT_FIELDS}
                FROM qc_product AS product
                {_TENANT_JOIN}
                WHERE product.id = $1::uuid
                """,
                product_id,
            )
        return _with_platform_list(dict(row)) if row is not None else None

    async def find_by_code(self, project_code: str) -> dict[str, Any] | None:
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                f"""
                SELECT {_SELECT_FIELDS}
                FROM qc_product AS product
                {_TENANT_JOIN}
                WHERE product.project_code = $1
                """,
                project_code,
            )
        return _with_platform_list(dict(row)) if row is not None else None

    async def upsert_product(self, payload: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        """按项目编号新增或更新（CSV 批量导入用）；返回 (行, 是否新建)。空值不覆盖旧值。"""
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                f"""
                WITH upserted AS (
                    INSERT INTO qc_product (
                        project_code, product_name, h5_url, department_name, owner_name, platforms,
                        status, tenant_id
                    )
                    VALUES ($1, $2, $3, $4, $5, $6::text[], $7, $8::uuid)
                    ON CONFLICT (project_code) DO UPDATE
                    SET product_name = EXCLUDED.product_name,
                        h5_url = COALESCE(EXCLUDED.h5_url, qc_product.h5_url),
                        department_name = COALESCE(EXCLUDED.department_name, qc_product.department_name),
                        owner_name = COALESCE(EXCLUDED.owner_name, qc_product.owner_name),
                        platforms = CASE
                            WHEN cardinality(EXCLUDED.platforms) > 0 THEN EXCLUDED.platforms
                            ELSE qc_product.platforms
                        END,
                        status = EXCLUDED.status,
                        tenant_id = COALESCE(EXCLUDED.tenant_id, qc_product.tenant_id),
                        updated_at = now()
                    RETURNING *, (xmax = 0) AS inserted
                )
                SELECT {_SELECT_FIELDS}, product.inserted
                FROM upserted AS product
                {_TENANT_JOIN}
                """,
                payload["project_code"],
                payload["product_name"],
                payload.get("h5_url"),
                payload.get("department_name"),
                payload.get("owner_name"),
                list(payload.get("platforms") or []),
                payload.get("status") or "enabled",
                payload.get("tenant_id"),
            )
        data = dict(row)
        inserted = bool(data.pop("inserted", False))
        return _with_platform_list(data), inserted

    async def record_import(self, payload: dict[str, Any]) -> dict[str, Any]:
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                INSERT INTO qc_product_import_record (
                    tenant_id, operator_id, operator_name, filename,
                    total_rows, created_count, updated_count, failed_count, errors, batch_no
                )
                VALUES (
                    $1::uuid, $2::uuid, $3, $4, $5, $6, $7, $8, $9::jsonb,
                    'IMP-' || to_char(now(), 'YYYYMMDD') || '-' || lpad((
                        (SELECT COUNT(*) FROM qc_product_import_record WHERE created_at::date = now()::date) + 1
                    )::text, 3, '0')
                )
                RETURNING id::text AS id, tenant_id::text AS tenant_id, operator_name, filename,
                    total_rows, created_count, updated_count, failed_count, errors, batch_no, created_at
                """,
                payload["tenant_id"],
                payload["operator_id"],
                payload["operator_name"],
                payload["filename"],
                int(payload.get("total_rows") or 0),
                int(payload.get("created_count") or 0),
                int(payload.get("updated_count") or 0),
                int(payload.get("failed_count") or 0),
                json.dumps(payload.get("errors") or [], ensure_ascii=False),
            )
        return _with_error_list(dict(row))

    async def list_import_records(
        self, *, tenant_id: str | None, limit: int = 50
    ) -> list[dict[str, Any]]:
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            rows = await connection.fetch(
                """
                SELECT record.id::text AS id, record.tenant_id::text AS tenant_id,
                    tenant.display_name AS tenant_name,
                    record.operator_name, record.filename, record.total_rows,
                    record.created_count, record.updated_count, record.failed_count,
                    record.errors, record.batch_no, record.created_at
                FROM qc_product_import_record AS record
                LEFT JOIN qc_tenant AS tenant ON tenant.id = record.tenant_id
                WHERE ($1::uuid IS NULL OR record.tenant_id = $1::uuid)
                ORDER BY record.created_at DESC
                LIMIT $2
                """,
                tenant_id,
                limit,
            )
        return [_with_error_list(dict(row)) for row in rows]

    async def find_import_record(self, record_id: str) -> dict[str, Any] | None:
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                SELECT record.id::text AS id, record.tenant_id::text AS tenant_id,
                    tenant.display_name AS tenant_name,
                    record.operator_name, record.filename, record.total_rows,
                    record.created_count, record.updated_count, record.failed_count,
                    record.errors, record.batch_no, record.created_at
                FROM qc_product_import_record AS record
                LEFT JOIN qc_tenant AS tenant ON tenant.id = record.tenant_id
                WHERE record.id = $1::uuid
                """,
                record_id,
            )
        return _with_error_list(dict(row)) if row is not None else None

    async def create_product(self, payload: dict[str, Any]) -> dict[str, Any]:
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                f"""
                WITH inserted AS (
                    INSERT INTO qc_product (
                        project_code, product_name, h5_url, department_name, owner_name, platforms,
                        status, tenant_id
                    )
                    VALUES ($1, $2, $3, $4, $5, $6::text[], $7, $8::uuid)
                    RETURNING *
                )
                SELECT {_SELECT_FIELDS}
                FROM inserted AS product
                {_TENANT_JOIN}
                """,
                payload["project_code"],
                payload["product_name"],
                payload.get("h5_url"),
                payload.get("department_name"),
                payload.get("owner_name"),
                list(payload.get("platforms") or []),
                payload["status"],
                payload.get("tenant_id"),
            )
        return _with_platform_list(dict(row))

    async def update_product(
        self, product_id: str, payload: dict[str, Any]
    ) -> dict[str, Any] | None:
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                f"""
                WITH updated AS (
                    UPDATE qc_product
                    SET project_code = $2,
                        product_name = $3,
                        h5_url = $4,
                        department_name = $5,
                        owner_name = $6,
                        platforms = $7::text[],
                        status = $8,
                        tenant_id = $9::uuid,
                        updated_at = now()
                    WHERE id = $1::uuid
                    RETURNING *
                )
                SELECT {_SELECT_FIELDS}
                FROM updated AS product
                {_TENANT_JOIN}
                """,
                product_id,
                payload["project_code"],
                payload["product_name"],
                payload.get("h5_url"),
                payload.get("department_name"),
                payload.get("owner_name"),
                list(payload.get("platforms") or []),
                payload["status"],
                payload.get("tenant_id"),
            )
        return _with_platform_list(dict(row)) if row is not None else None

    async def delete_product(self, product_id: str) -> bool:
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                "DELETE FROM qc_product WHERE id = $1::uuid RETURNING id", product_id
            )
        return row is not None

    async def _get_pool(self) -> Any:
        if self._pool is not None:
            return self._pool
        return await get_postgres_pool(self._database_url)


def _strip_total(row: Any) -> dict[str, Any]:
    data = dict(row)
    data.pop("total", None)
    return data


def _with_platform_list(row: dict[str, Any]) -> dict[str, Any]:
    row["platforms"] = _platforms(row.get("platforms"))
    return row


def _with_error_list(row: dict[str, Any]) -> dict[str, Any]:
    errors = row.get("errors")
    if isinstance(errors, str):
        try:
            errors = json.loads(errors)
        except json.JSONDecodeError:
            errors = []
    row["errors"] = list(errors or [])
    return row
