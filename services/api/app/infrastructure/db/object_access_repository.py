"""任务聚合对象的仓储级访问判定。

对象入口不得先无作用域读取完整业务数据再在 API 层猜测权限。本仓储只读取不可变的
owner/tenant 身份，并统一裁决 task、artifact、run、review 与 App build；调用方对
拒绝和不存在采用同一 404 策略，避免泄露跨主体对象是否存在。
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any
from uuid import UUID

from services.api.app.domain.iam.access_scope import AccessScope
from services.api.app.infrastructure.db.pool import get_postgres_pool


class ObjectAccessAction(StrEnum):
    """用户面对象操作类型。"""

    READ = "read"
    WRITE = "write"
    REVIEW = "review"


class PostgresObjectAccessRepository:
    """用权威 PostgreSQL owner/tenant 字段裁决对象级访问。"""

    def __init__(self, *, database_url: str | None = None, pool: Any | None = None) -> None:
        self._database_url = database_url
        self._pool = pool

    async def can_access_task(
        self,
        *,
        task_id: str,
        scope: AccessScope,
        action: ObjectAccessAction = ObjectAccessAction.READ,
    ) -> bool:
        if not _is_uuid(task_id):
            return False
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                SELECT submitter_id::text AS owner_user_id, tenant_id::text AS tenant_id,
                       department_name
                FROM qc_task
                WHERE id = $1::uuid AND deleted_at IS NULL
                """,
                task_id,
            )
        return _allows(scope, row, action)

    async def can_access_artifact(
        self,
        *,
        artifact_id: str,
        scope: AccessScope,
        action: ObjectAccessAction = ObjectAccessAction.READ,
    ) -> bool:
        if not _is_uuid(artifact_id):
            return False
        # 读：排除已删／待删；写（恢复）：仅排除终态 deleted
        if action is ObjectAccessAction.READ:
            lifecycle_clause = (
                "AND COALESCE(artifact.lifecycle_status, 'active') "
                "NOT IN ('deleted', 'pending_delete')"
            )
        else:
            lifecycle_clause = (
                "AND COALESCE(artifact.lifecycle_status, 'active') <> 'deleted'"
            )
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                f"""
                SELECT task.submitter_id::text AS owner_user_id,
                       task.tenant_id::text AS tenant_id,
                       task.department_name
                FROM qc_evidence_artifact AS artifact
                JOIN qc_task AS task ON task.id = artifact.task_id
                WHERE artifact.id = $1::uuid
                  {lifecycle_clause}
                """,
                artifact_id,
            )
        return _allows(scope, row, action)

    async def can_access_agent_run(
        self,
        *,
        run_id: str,
        scope: AccessScope,
        action: ObjectAccessAction = ObjectAccessAction.READ,
    ) -> bool:
        if not _is_uuid(run_id):
            return False
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                SELECT task.submitter_id::text AS owner_user_id,
                       task.tenant_id::text AS tenant_id,
                       task.department_name
                FROM qc_agent_run AS run
                JOIN qc_task AS task ON task.id = run.task_id
                WHERE run.id = $1::uuid
                """,
                run_id,
            )
        return _allows(scope, row, action)

    async def can_access_review(
        self,
        *,
        review_id: str,
        scope: AccessScope,
    ) -> bool:
        if not _is_uuid(review_id):
            return False
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                SELECT task.submitter_id::text AS owner_user_id,
                       task.tenant_id::text AS tenant_id,
                       task.department_name
                FROM qc_review_item AS review
                JOIN qc_task AS task ON task.id = review.task_id
                WHERE review.id = $1::uuid
                  AND task.deleted_at IS NULL
                """,
                review_id,
            )
        return _allows(scope, row, ObjectAccessAction.REVIEW)

    async def can_access_build(
        self,
        *,
        build_id: str,
        scope: AccessScope,
        action: ObjectAccessAction = ObjectAccessAction.READ,
    ) -> bool:
        normalized = build_id.removeprefix("app-build:")
        if not _is_uuid(normalized):
            return False
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                SELECT created_by::text AS owner_user_id, tenant_id::text AS tenant_id,
                       NULL::varchar AS department_name
                FROM qc_platform_build
                WHERE id = $1::uuid
                """,
                normalized,
            )
        return _allows(scope, row, action)

    async def _get_pool(self) -> Any:
        if self._pool is not None:
            return self._pool
        return await get_postgres_pool(self._database_url)


def _department_of(row: Any) -> str | None:
    try:
        value = row["department_name"]
    except (KeyError, IndexError, TypeError):
        return None
    return str(value) if value else None


def _allows(scope: AccessScope, row: Any | None, action: ObjectAccessAction) -> bool:
    if row is None:
        return False
    owner_user_id = str(row["owner_user_id"])
    tenant_id = str(row["tenant_id"])
    department_name = _department_of(row)
    if action is ObjectAccessAction.READ:
        return scope.can_read_resource(
            owner_id=owner_user_id, tenant_id=tenant_id, department_name=department_name
        )
    if action is ObjectAccessAction.WRITE:
        return scope.can_write_resource(owner_id=owner_user_id, tenant_id=tenant_id)
    return scope.can_review_resource(
        owner_id=owner_user_id, tenant_id=tenant_id, department_name=department_name
    )


def _is_uuid(value: str) -> bool:
    try:
        UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        return False
    return True
