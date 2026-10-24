"""PostgreSQL 反馈工单仓储。

可见范围由调用方（API 层）按 AccessScope 决定后以过滤条件传入：跨公司、
本公司或仅本人；仓储本身不推断权限。
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from services.api.app.infrastructure.db.pool import get_postgres_pool

_TICKET_COLUMNS = """
    ticket.id::text AS id,
    ticket.ticket_no,
    ticket.tenant_id::text AS tenant_id,
    tenant.display_name AS tenant_name,
    ticket.submitter_id::text AS submitter_id,
    ticket.submitter_name,
    ticket.kind,
    ticket.priority,
    ticket.title,
    ticket.description,
    ticket.task_id::text AS task_id,
    ticket.rule_code,
    ticket.contact,
    ticket.status,
    ticket.reply,
    ticket.handled_by,
    ticket.handled_at,
    ticket.created_at,
    ticket.updated_at
"""

_FROM_TICKET = """
    FROM qc_feedback_ticket AS ticket
    LEFT JOIN qc_tenant AS tenant ON tenant.id = ticket.tenant_id
"""


def _is_uuid(value: str | None) -> bool:
    try:
        UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        return False
    return True


class PostgresFeedbackRepository:
    def __init__(self, *, database_url: str | None = None, pool: Any | None = None) -> None:
        self._database_url = database_url
        self._pool = pool

    async def create_ticket(self, payload: dict[str, Any]) -> dict[str, Any]:
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                f"""
                WITH inserted AS (
                    INSERT INTO qc_feedback_ticket (
                        ticket_no, tenant_id, submitter_id, submitter_name,
                        kind, priority, title, description, task_id, rule_code, contact
                    )
                    VALUES (
                        'FB' || to_char(now() AT TIME ZONE 'UTC', 'YYYYMMDD')
                            || lpad(nextval('qc_feedback_ticket_no_seq')::text, 4, '0'),
                        $1::uuid, $2::uuid, $3,
                        $4, $5, $6, $7, $8::uuid, $9, $10
                    )
                    RETURNING *
                )
                SELECT {_TICKET_COLUMNS}
                FROM inserted AS ticket
                LEFT JOIN qc_tenant AS tenant ON tenant.id = ticket.tenant_id
                """,
                payload["tenant_id"],
                payload["submitter_id"],
                payload["submitter_name"],
                payload["kind"],
                payload["priority"],
                payload["title"],
                payload["description"],
                payload.get("task_id") if _is_uuid(payload.get("task_id")) else None,
                payload.get("rule_code"),
                payload.get("contact"),
            )
        return dict(row)

    async def list_tickets(
        self,
        *,
        tenant_id: str | None,
        submitter_id: str | None,
        status: str | None = None,
        kind: str | None = None,
        keyword: str | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        conditions: list[str] = []
        params: list[Any] = []
        if tenant_id is not None:
            params.append(tenant_id)
            conditions.append(f"ticket.tenant_id = ${len(params)}::uuid")
        if submitter_id is not None:
            params.append(submitter_id)
            conditions.append(f"ticket.submitter_id = ${len(params)}::uuid")
        if status:
            params.append(status)
            conditions.append(f"ticket.status = ${len(params)}")
        if kind:
            params.append(kind)
            conditions.append(f"ticket.kind = ${len(params)}")
        if keyword:
            params.append(f"%{keyword.strip()}%")
            index = len(params)
            conditions.append(
                f"(ticket.title ILIKE ${index} OR ticket.description ILIKE ${index}"
                f" OR ticket.ticket_no ILIKE ${index} OR ticket.submitter_name ILIKE ${index}"
                f" OR ticket.task_id::text ILIKE ${index} OR ticket.rule_code ILIKE ${index})"
            )
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        offset = max(page - 1, 0) * page_size
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            total = await connection.fetchval(
                f"SELECT COUNT(*) {_FROM_TICKET} {where}",
                *params,
            )
            rows = await connection.fetch(
                f"""
                SELECT {_TICKET_COLUMNS}
                {_FROM_TICKET}
                {where}
                ORDER BY ticket.created_at DESC
                LIMIT ${len(params) + 1} OFFSET ${len(params) + 2}
                """,
                *params,
                page_size,
                offset,
            )
        return [dict(row) for row in rows], int(total or 0)

    async def find_by_id(self, ticket_id: str) -> dict[str, Any] | None:
        if not _is_uuid(ticket_id):
            return None
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                f"""
                SELECT {_TICKET_COLUMNS}
                {_FROM_TICKET}
                WHERE ticket.id = $1::uuid
                """,
                ticket_id,
            )
        return dict(row) if row is not None else None

    async def update_ticket(
        self,
        ticket_id: str,
        *,
        status: str,
        reply: str | None,
        handled_by: str,
    ) -> dict[str, Any] | None:
        if not _is_uuid(ticket_id):
            return None
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                f"""
                WITH updated AS (
                    UPDATE qc_feedback_ticket
                    SET status = $2,
                        reply = $3,
                        handled_by = $4,
                        handled_at = now(),
                        updated_at = now()
                    WHERE id = $1::uuid
                    RETURNING *
                )
                SELECT {_TICKET_COLUMNS}
                FROM updated AS ticket
                LEFT JOIN qc_tenant AS tenant ON tenant.id = ticket.tenant_id
                """,
                ticket_id,
                status,
                reply,
                handled_by,
            )
        return dict(row) if row is not None else None

    async def _get_pool(self) -> Any:
        if self._pool is not None:
            return self._pool
        return await get_postgres_pool(self._database_url)
