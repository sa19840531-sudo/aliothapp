"""AI 供应商登记、服务事件、平台设置与近 24 小时调用指标（PostgreSQL）。"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from services.api.app.infrastructure.db.pool import get_postgres_pool

_PROVIDER_COLUMNS = """
    provider.id::text AS id,
    provider.code,
    provider.name,
    provider.base_url,
    provider.model,
    provider.priority,
    provider.weight,
    provider.breaker_rule,
    provider.status,
    provider.is_primary,
    provider.api_key_configured,
    provider.managed_by_env,
    provider.quota_remaining_percent::float8 AS quota_remaining_percent,
    provider.last_check_status,
    provider.last_check_latency_ms,
    provider.last_check_message,
    provider.last_checked_at,
    provider.created_at,
    provider.updated_at
"""


class PostgresLlmProviderRepository:
    def __init__(self, *, database_url: str | None = None, pool: Any | None = None) -> None:
        self._database_url = database_url
        self._pool = pool

    async def list_providers(self) -> list[dict[str, Any]]:
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            rows = await connection.fetch(
                f"""
                SELECT {_PROVIDER_COLUMNS}
                FROM qc_llm_provider AS provider
                ORDER BY provider.is_primary DESC, provider.priority ASC, provider.created_at ASC
                """
            )
        return [dict(row) for row in rows]

    async def find_by_id(self, provider_id: str) -> dict[str, Any] | None:
        try:
            UUID(str(provider_id))
        except (ValueError, TypeError, AttributeError):
            return None
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                f"SELECT {_PROVIDER_COLUMNS} FROM qc_llm_provider AS provider WHERE provider.id = $1::uuid",
                provider_id,
            )
        return dict(row) if row is not None else None

    async def find_by_code(self, code: str) -> dict[str, Any] | None:
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                f"SELECT {_PROVIDER_COLUMNS} FROM qc_llm_provider AS provider WHERE provider.code = $1",
                code,
            )
        return dict(row) if row is not None else None

    async def create_provider(self, payload: dict[str, Any]) -> dict[str, Any]:
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                f"""
                WITH inserted AS (
                    INSERT INTO qc_llm_provider (
                        code, name, base_url, model, priority, weight, breaker_rule, status,
                        is_primary, api_key_configured, managed_by_env, quota_remaining_percent
                    )
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
                    RETURNING *
                )
                SELECT {_PROVIDER_COLUMNS}
                FROM inserted AS provider
                """,
                payload["code"],
                payload["name"],
                payload.get("base_url"),
                payload.get("model"),
                int(payload.get("priority") or 1),
                int(payload.get("weight") or 0),
                payload.get("breaker_rule"),
                payload.get("status") or "standby",
                bool(payload.get("is_primary")),
                bool(payload.get("api_key_configured")),
                bool(payload.get("managed_by_env")),
                payload.get("quota_remaining_percent"),
            )
        return dict(row)

    async def update_provider(self, provider_id: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                f"""
                WITH updated AS (
                    UPDATE qc_llm_provider
                    SET name = $2,
                        base_url = $3,
                        model = $4,
                        priority = $5,
                        weight = $6,
                        breaker_rule = $7,
                        status = $8,
                        quota_remaining_percent = $9,
                        updated_at = now()
                    WHERE id = $1::uuid
                    RETURNING *
                )
                SELECT {_PROVIDER_COLUMNS}
                FROM updated AS provider
                """,
                provider_id,
                payload["name"],
                payload.get("base_url"),
                payload.get("model"),
                int(payload.get("priority") or 1),
                int(payload.get("weight") or 0),
                payload.get("breaker_rule"),
                payload.get("status") or "standby",
                payload.get("quota_remaining_percent"),
            )
        return dict(row) if row is not None else None

    async def sync_env_provider(self, payload: dict[str, Any]) -> dict[str, Any]:
        """把环境变量里当前生效的供应商同步进表：只更新地址 / 模型 / 密钥状态，不动人工设置。"""
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                f"""
                WITH upserted AS (
                    INSERT INTO qc_llm_provider (
                        code, name, base_url, model, priority, weight, breaker_rule, status,
                        is_primary, api_key_configured, managed_by_env
                    )
                    VALUES ($1, $2, $3, $4, 1, 100, $5, 'active', true, $6, true)
                    ON CONFLICT (code) DO UPDATE
                    SET base_url = EXCLUDED.base_url,
                        model = EXCLUDED.model,
                        api_key_configured = EXCLUDED.api_key_configured,
                        managed_by_env = true,
                        updated_at = now()
                    RETURNING *
                )
                SELECT {_PROVIDER_COLUMNS}
                FROM upserted AS provider
                """,
                payload["code"],
                payload["name"],
                payload.get("base_url"),
                payload.get("model"),
                payload.get("breaker_rule"),
                bool(payload.get("api_key_configured")),
            )
        return dict(row)

    async def set_primary(self, provider_id: str) -> dict[str, Any] | None:
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            async with connection.transaction():
                await connection.execute(
                    """
                    UPDATE qc_llm_provider
                    SET is_primary = false,
                        status = CASE WHEN status = 'active' THEN 'standby' ELSE status END,
                        updated_at = now()
                    WHERE is_primary = true AND id <> $1::uuid
                    """,
                    provider_id,
                )
                row = await connection.fetchrow(
                    f"""
                    WITH updated AS (
                        UPDATE qc_llm_provider
                        SET is_primary = true, status = 'active', updated_at = now()
                        WHERE id = $1::uuid
                        RETURNING *
                    )
                    SELECT {_PROVIDER_COLUMNS}
                    FROM updated AS provider
                    """,
                    provider_id,
                )
        return dict(row) if row is not None else None

    async def record_check(
        self,
        provider_id: str,
        *,
        status: str,
        latency_ms: int | None,
        message: str | None,
    ) -> dict[str, Any] | None:
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                f"""
                WITH updated AS (
                    UPDATE qc_llm_provider
                    SET last_check_status = $2,
                        last_check_latency_ms = $3,
                        last_check_message = $4,
                        last_checked_at = now(),
                        updated_at = now()
                    WHERE id = $1::uuid
                    RETURNING *
                )
                SELECT {_PROVIDER_COLUMNS}
                FROM updated AS provider
                """,
                provider_id,
                status,
                latency_ms,
                message,
            )
        return dict(row) if row is not None else None

    async def record_event(self, payload: dict[str, Any]) -> dict[str, Any]:
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                INSERT INTO qc_llm_provider_event (
                    provider_id, provider_name, event_type, description, action, result, operator_name
                )
                VALUES ($1::uuid, $2, $3, $4, $5, $6, $7)
                RETURNING id::text AS id, provider_id::text AS provider_id, provider_name, event_type,
                    description, action, result, operator_name, occurred_at
                """,
                payload.get("provider_id"),
                payload["provider_name"],
                payload["event_type"],
                payload["description"],
                payload.get("action"),
                payload.get("result"),
                payload.get("operator_name"),
            )
        return dict(row)

    async def list_events(self, *, limit: int = 20) -> list[dict[str, Any]]:
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            rows = await connection.fetch(
                """
                SELECT id::text AS id, provider_id::text AS provider_id, provider_name, event_type,
                    description, action, result, operator_name, occurred_at
                FROM qc_llm_provider_event
                ORDER BY occurred_at DESC
                LIMIT $1
                """,
                limit,
            )
        return [dict(row) for row in rows]

    async def usage_metrics(self, *, hours: int = 24) -> dict[str, dict[str, Any]]:
        """按供应商代号汇总最近 N 小时的调用次数、失败次数（来自模型用量事件）。"""
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            rows = await connection.fetch(
                """
                SELECT
                    event.provider,
                    COUNT(*)::int AS call_count,
                    COUNT(*) FILTER (WHERE event.metadata->>'request_status' = 'failed')::int AS failed_count,
                    COALESCE(SUM(event.total_tokens), 0)::bigint AS total_tokens,
                    MAX(event.created_at) AS last_call_at
                FROM qc_llm_usage_event AS event
                WHERE event.created_at >= now() - ($1::integer * interval '1 hour')
                GROUP BY event.provider
                """,
                hours,
            )
        return {str(row["provider"]): dict(row) for row in rows}

    async def get_setting(self, key: str) -> dict[str, Any] | None:
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                "SELECT value FROM qc_platform_setting WHERE key = $1", key
            )
        if row is None:
            return None
        value = row["value"]
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except json.JSONDecodeError:
                value = {}
        return dict(value or {})

    async def set_setting(self, key: str, value: dict[str, Any], *, updated_by: str | None) -> None:
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            await connection.execute(
                """
                INSERT INTO qc_platform_setting (key, value, updated_by)
                VALUES ($1, $2::jsonb, $3)
                ON CONFLICT (key) DO UPDATE
                SET value = EXCLUDED.value, updated_by = EXCLUDED.updated_by, updated_at = now()
                """,
                key,
                json.dumps(value, ensure_ascii=False),
                updated_by,
            )

    async def _get_pool(self) -> Any:
        if self._pool is not None:
            return self._pool
        return await get_postgres_pool(self._database_url)
