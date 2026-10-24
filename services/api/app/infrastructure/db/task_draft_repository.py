"""PostgreSQL 创建任务草稿仓储：草稿只属于提交它的账号。"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from services.api.app.infrastructure.db.pool import get_postgres_pool

_DRAFT_COLUMNS = """
    id::text AS id,
    tenant_id::text AS tenant_id,
    owner_id::text AS owner_id,
    name,
    target_platform,
    payload,
    created_at,
    updated_at
"""


def _is_uuid(value: str | None) -> bool:
    try:
        UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        return False
    return True


def _normalize(row: Any) -> dict[str, Any]:
    data = dict(row)
    payload = data.get("payload")
    if isinstance(payload, str):
        try:
            data["payload"] = json.loads(payload)
        except json.JSONDecodeError:
            data["payload"] = {}
    elif payload is None:
        data["payload"] = {}
    return data


class PostgresTaskDraftRepository:
    def __init__(self, *, database_url: str | None = None, pool: Any | None = None) -> None:
        self._database_url = database_url
        self._pool = pool

    async def list_drafts(self, *, owner_id: str, limit: int = 50) -> list[dict[str, Any]]:
        if not _is_uuid(owner_id):
            return []
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            rows = await connection.fetch(
                f"""
                SELECT {_DRAFT_COLUMNS}
                FROM qc_task_draft
                WHERE owner_id = $1::uuid
                ORDER BY updated_at DESC
                LIMIT $2
                """,
                owner_id,
                limit,
            )
        return [_normalize(row) for row in rows]

    async def create_draft(self, payload: dict[str, Any]) -> dict[str, Any]:
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                f"""
                INSERT INTO qc_task_draft (tenant_id, owner_id, name, target_platform, payload)
                VALUES ($1::uuid, $2::uuid, $3, $4, $5::jsonb)
                RETURNING {_DRAFT_COLUMNS}
                """,
                payload["tenant_id"],
                payload["owner_id"],
                payload["name"],
                payload["target_platform"],
                json.dumps(payload.get("payload") or {}, ensure_ascii=False),
            )
        return _normalize(row)

    async def update_draft(
        self, draft_id: str, *, owner_id: str, payload: dict[str, Any]
    ) -> dict[str, Any] | None:
        if not _is_uuid(draft_id) or not _is_uuid(owner_id):
            return None
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                f"""
                UPDATE qc_task_draft
                SET name = $3,
                    target_platform = $4,
                    payload = $5::jsonb,
                    updated_at = now()
                WHERE id = $1::uuid AND owner_id = $2::uuid
                RETURNING {_DRAFT_COLUMNS}
                """,
                draft_id,
                owner_id,
                payload["name"],
                payload["target_platform"],
                json.dumps(payload.get("payload") or {}, ensure_ascii=False),
            )
        return _normalize(row) if row is not None else None

    async def delete_draft(self, draft_id: str, *, owner_id: str) -> bool:
        if not _is_uuid(draft_id) or not _is_uuid(owner_id):
            return False
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                "DELETE FROM qc_task_draft WHERE id = $1::uuid AND owner_id = $2::uuid RETURNING id",
                draft_id,
                owner_id,
            )
        return row is not None

    async def _get_pool(self) -> Any:
        if self._pool is not None:
            return self._pool
        return await get_postgres_pool(self._database_url)
