"""PostgreSQL Agent能力注册表仓储。"""

import json
from typing import Any

from services.api.app.application.capability_registry import CapabilityRegistryEntry
from services.api.app.infrastructure.db.pool import get_postgres_pool


class PostgresCapabilityRegistryRepository:
    """原子同步运行时Manifest对应的数据库能力投影。"""

    def __init__(self, *, database_url: str | None = None, pool: Any | None = None) -> None:
        self._database_url = database_url
        self._pool = pool

    async def sync(self, capabilities: tuple[CapabilityRegistryEntry, ...]) -> int:
        pool = await self._get_pool()
        async with pool.acquire() as connection, connection.transaction():
            await connection.execute(
                "SELECT pg_advisory_xact_lock(hashtext('qc_agent_capability_registry_sync'))"
            )
            for capability in capabilities:
                await connection.execute(
                    _UPSERT_CAPABILITY_SQL,
                    capability.capability_name,
                    capability.capability_kind,
                    capability.display_name,
                    capability.description,
                    capability.version,
                    _json_dumps(capability.input_schema),
                    _json_dumps(capability.output_schema),
                    capability.permission_level.value,
                    capability.timeout_ms,
                    _json_dumps(capability.retry_policy),
                    capability.owner_team,
                    capability.implementation_ref,
                )

            manifest_entries = _json_dumps(
                [
                    {
                        "capability_name": capability.capability_name,
                        "version": capability.version,
                    }
                    for capability in capabilities
                ]
            )
            status = await connection.execute(_DISABLE_STALE_CAPABILITIES_SQL, manifest_entries)
        return _affected_rows(status)

    async def _get_pool(self) -> Any:
        if self._pool is not None:
            return self._pool
        return await get_postgres_pool(self._database_url)


_UPSERT_CAPABILITY_SQL = """
    INSERT INTO qc_agent_capability_registry (
        capability_name,
        capability_kind,
        display_name,
        description,
        version,
        status,
        input_schema,
        output_schema,
        permission_level,
        timeout_ms,
        retry_policy,
        owner_team,
        implementation_ref
    )
    VALUES (
        $1, $2, $3, $4, $5, 'enabled', $6::jsonb, $7::jsonb,
        $8, $9, $10::jsonb, $11, $12
    )
    ON CONFLICT (capability_name, version)
    DO UPDATE SET
        capability_kind = EXCLUDED.capability_kind,
        display_name = EXCLUDED.display_name,
        description = EXCLUDED.description,
        status = EXCLUDED.status,
        input_schema = EXCLUDED.input_schema,
        output_schema = EXCLUDED.output_schema,
        permission_level = EXCLUDED.permission_level,
        timeout_ms = EXCLUDED.timeout_ms,
        retry_policy = EXCLUDED.retry_policy,
        owner_team = EXCLUDED.owner_team,
        implementation_ref = EXCLUDED.implementation_ref,
        updated_at = now()
"""


_DISABLE_STALE_CAPABILITIES_SQL = """
    WITH manifest_entries AS (
        SELECT capability_name, version
        FROM jsonb_to_recordset($1::jsonb) AS entry(
            capability_name varchar(128),
            version varchar(64)
        )
    )
    UPDATE qc_agent_capability_registry AS registry
    SET status = 'disabled', updated_at = now()
    WHERE registry.status IN ('draft', 'enabled')
      AND NOT EXISTS (
          SELECT 1
          FROM manifest_entries AS entry
          WHERE entry.capability_name = registry.capability_name
            AND entry.version = registry.version
      )
"""


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _affected_rows(status: str) -> int:
    try:
        return int(status.rsplit(" ", 1)[-1])
    except ValueError as exc:
        raise RuntimeError(f"无法解析数据库同步结果：{status}") from exc
