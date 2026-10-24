"""PostgreSQL 公司（租户）仓储实现。"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from services.api.app.infrastructure.db.organization_deletion import OrganizationDeletion
from services.api.app.infrastructure.db.pool import get_postgres_pool

DEFAULT_TENANT_ID = "00000000-0000-0000-0000-000000000001"
TENANT_KEY_UNIQUE_CONSTRAINT = "uq_qc_tenant_key"
TENANT_NAME_UNIQUE_INDEX = "uq_qc_tenant_display_name_normalized"

# 分公司列表页要展示总管理员账号、部门数、产品数：总部（体验中心）没有「公司管理员」，
# 退回第一个平台管理员；共享产品（tenant_id 为空）算在体验中心名下。
_TENANT_COLUMNS = f"""
    tenant.id::text AS id,
    tenant.tenant_key,
    tenant.display_name,
    tenant.status,
    tenant.contact_name,
    tenant.remark,
    tenant.created_at,
    tenant.updated_at,
    (
        SELECT COUNT(*)
        FROM qc_user AS member
        WHERE member.tenant_id = tenant.id
          AND member.username <> '__history_principal__'
    ) AS user_count,
    (
        SELECT member.username
        FROM qc_user AS member
        WHERE member.tenant_id = tenant.id
          AND member.role IN ('公司管理员', '平台管理员')
          AND member.username <> '__history_principal__'
        ORDER BY CASE WHEN member.role = '公司管理员' THEN 0 ELSE 1 END, member.created_at ASC
        LIMIT 1
    ) AS admin_username,
    (
        SELECT COUNT(*)
        FROM qc_department AS dept
        WHERE dept.tenant_id = tenant.id
    )::int AS department_count,
    (
        SELECT COUNT(*)
        FROM qc_product AS product
        WHERE product.tenant_id = tenant.id
           OR (product.tenant_id IS NULL AND tenant.id = '{DEFAULT_TENANT_ID}'::uuid)
    )::int AS product_count
"""


class PostgresTenantRepository:
    def __init__(self, *, database_url: str | None = None, pool: Any | None = None) -> None:
        self._database_url = database_url
        self._pool = pool

    async def list_tenants(self) -> list[dict[str, Any]]:
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            rows = await connection.fetch(
                f"""
                SELECT {_TENANT_COLUMNS}
                FROM qc_tenant AS tenant
                ORDER BY (tenant.id = $1::uuid) DESC, tenant.created_at ASC
                """,
                DEFAULT_TENANT_ID,
            )
        return [dict(row) for row in rows]

    async def find_by_id(self, tenant_id: str) -> dict[str, Any] | None:
        try:
            UUID(str(tenant_id))
        except (ValueError, TypeError, AttributeError):
            return None
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                f"""
                SELECT {_TENANT_COLUMNS}
                FROM qc_tenant AS tenant
                WHERE tenant.id = $1::uuid
                """,
                tenant_id,
            )
        return dict(row) if row is not None else None

    async def find_by_display_name(self, display_name: str) -> dict[str, Any] | None:
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                f"""
                SELECT {_TENANT_COLUMNS}
                FROM qc_tenant AS tenant
                WHERE LOWER(BTRIM(tenant.display_name)) = LOWER(BTRIM($1))
                """,
                display_name,
            )
        return dict(row) if row is not None else None

    async def create_tenant(self, payload: dict[str, Any]) -> dict[str, Any]:
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                f"""
                WITH inserted AS (
                    INSERT INTO qc_tenant (id, tenant_key, display_name, status, contact_name, remark)
                    VALUES (gen_random_uuid(), $1, $2, 'enabled', $3, $4)
                    RETURNING *
                )
                SELECT {_TENANT_COLUMNS}
                FROM inserted AS tenant
                """,
                payload["tenant_key"],
                payload["display_name"],
                payload.get("contact_name"),
                payload.get("remark"),
            )
        return dict(row)

    async def update_tenant(self, tenant_id: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                f"""
                WITH updated AS (
                    UPDATE qc_tenant
                    SET display_name = $2,
                        status = $3,
                        contact_name = $4,
                        remark = $5,
                        updated_at = now()
                    WHERE id = $1::uuid
                    RETURNING *
                )
                SELECT {_TENANT_COLUMNS}
                FROM updated AS tenant
                """,
                tenant_id,
                payload["display_name"],
                payload["status"],
                payload.get("contact_name"),
                payload.get("remark"),
            )
        return dict(row) if row is not None else None

    async def delete_tenant(self, tenant_id: str) -> OrganizationDeletion:
        """无任何业务、历史或审计关联才能删除；外键之外的关联也要检查。"""
        if tenant_id == DEFAULT_TENANT_ID:
            return OrganizationDeletion(associations={"总部保护": 1})
        pool = await self._get_pool()
        async with pool.acquire() as connection, connection.transaction():
            await connection.execute("SET LOCAL lock_timeout = '2s'")
            # 先锁住非外键关联表，再锁公司行；关联检查与删除在同一事务完成。
            await connection.execute(
                "LOCK TABLE qc_task_submission, qc_evidence_download_audit IN SHARE ROW EXCLUSIVE MODE"
            )
            tenant = await connection.fetchrow(
                "SELECT id FROM qc_tenant WHERE id = $1::uuid FOR UPDATE", tenant_id
            )
            if tenant is None:
                return OrganizationDeletion()
            counts = await connection.fetchrow(
                """
                SELECT
                    (SELECT count(*) FROM qc_user WHERE tenant_id = $1::uuid)::int AS "账号",
                    (SELECT count(*) FROM qc_department WHERE tenant_id = $1::uuid)::int AS "部门",
                    (SELECT count(*) FROM qc_product WHERE tenant_id = $1::uuid)::int AS "产品",
                    (SELECT count(*) FROM qc_task WHERE tenant_id = $1::uuid)::int AS "任务",
                    (SELECT count(*) FROM qc_platform_build WHERE tenant_id = $1::uuid)::int AS "安装包",
                    (SELECT count(*) FROM qc_product_import_record WHERE tenant_id = $1::uuid)::int AS "产品导入记录",
                    (SELECT count(*) FROM qc_task_draft WHERE tenant_id = $1::uuid)::int AS "任务草稿",
                    (SELECT count(*) FROM qc_feedback_ticket WHERE tenant_id = $1::uuid)::int AS "反馈记录",
                    (SELECT count(*) FROM qc_task_submission WHERE tenant_id = $1::uuid)::int AS "任务提交记录",
                    (SELECT count(*) FROM qc_evidence_download_audit WHERE tenant_id = $1::uuid::text)::int AS "证据下载审计"
                """,
                tenant_id,
            )
            associations = {key: int(value) for key, value in dict(counts).items() if value}
            if associations:
                return OrganizationDeletion(associations=associations)
            await connection.execute("DELETE FROM qc_tenant WHERE id = $1::uuid", tenant_id)
        return OrganizationDeletion(deleted=True)

    async def _get_pool(self) -> Any:
        if self._pool is not None:
            return self._pool
        return await get_postgres_pool(self._database_url)
