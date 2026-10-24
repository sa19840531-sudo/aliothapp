"""Journey Definition、Version 与执行快照的 PostgreSQL 仓储。"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from services.api.app.domain.journey import (
    JourneyDefinition,
    JourneyExecutionPlan,
    parse_journey_definition,
    parse_journey_execution_plan,
)
from services.api.app.infrastructure.db.pool import get_postgres_pool


class PostgresJourneyDefinitionRepository:
    """以不可覆盖的版本行保存 Journey DSL 与任务执行快照。"""

    def __init__(self, *, database_url: str | None = None, pool: Any | None = None) -> None:
        self._database_url = database_url
        self._pool = pool

    async def get(
        self,
        *,
        definition_id: str,
        version: int,
    ) -> JourneyDefinition | None:
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            payload = await connection.fetchval(
                """
                SELECT definition_payload
                FROM qc_journey_definition
                WHERE definition_id = $1
                  AND version = $2
                """,
                definition_id,
                version,
            )
        return _parse_definition(payload)

    async def get_latest(self, *, definition_id: str) -> JourneyDefinition | None:
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            payload = await connection.fetchval(
                """
                SELECT definition_payload
                FROM qc_journey_definition
                WHERE definition_id = $1
                ORDER BY version DESC
                LIMIT 1
                """,
                definition_id,
            )
        return _parse_definition(payload)

    async def add(self, *, definition: JourneyDefinition) -> bool:
        """串行化同一 Definition 的发布，避免并发写入倒序版本。"""
        pool = await self._get_pool()
        payload = definition.to_dict()
        async with pool.acquire() as connection, connection.transaction():
            await connection.execute(
                "SELECT pg_advisory_xact_lock(hashtext($1))",
                f"journey-definition:{definition.definition_id}",
            )
            created = await connection.fetchval(
                """
                INSERT INTO qc_journey_definition (
                    definition_id, version, schema_version, name, description,
                    surface_kinds, content_hash, definition_payload
                )
                SELECT
                    $1::text, $2::int, $3::text, $4::text, $5::text,
                    $6::jsonb, $7::text, $8::jsonb
                WHERE NOT EXISTS (
                    SELECT 1
                    FROM qc_journey_definition
                    WHERE definition_id = $1::text
                      AND version >= $2::int
                )
                ON CONFLICT DO NOTHING
                RETURNING true
                """,
                definition.definition_id,
                definition.version,
                definition.schema_version,
                definition.name,
                definition.description,
                _json_dumps(list(definition.surface_kinds)),
                definition.content_hash,
                _json_dumps(payload),
            )
        return bool(created)

    async def get_execution_plan(
        self,
        *,
        execution_plan_id: str,
    ) -> JourneyExecutionPlan | None:
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            payload = await connection.fetchval(
                """
                SELECT plan_payload
                FROM qc_journey_execution_plan
                WHERE execution_plan_id = $1
                """,
                execution_plan_id,
            )
        return _parse_plan(payload)

    async def get_execution_plan_for_task(
        self,
        *,
        task_id: str,
    ) -> JourneyExecutionPlan | None:
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            payload = await connection.fetchval(
                """
                SELECT plan_payload
                FROM qc_journey_execution_plan
                WHERE task_id = $1::uuid
                  AND agent_run_id IS NULL
                """,
                task_id,
            )
        return _parse_plan(payload)

    async def get_execution_plan_for_scope(
        self,
        *,
        task_id: str,
        agent_run_id: str,
        definition_id: str,
        scenario_key: str,
    ) -> JourneyExecutionPlan | None:
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            payload = await connection.fetchval(
                """SELECT plan_payload FROM qc_journey_execution_plan
                   WHERE task_id=$1::uuid AND agent_run_id=$2::uuid
                     AND definition_id=$3 AND scenario_key=$4""",
                task_id,
                agent_run_id,
                definition_id,
                scenario_key,
            )
        return _parse_plan(payload)

    async def list_execution_plans_for_run(
        self,
        *,
        task_id: str,
        agent_run_id: str,
    ) -> tuple[JourneyExecutionPlan, ...]:
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            rows = await connection.fetch(
                """SELECT plan_payload FROM qc_journey_execution_plan
                   WHERE task_id=$1::uuid AND agent_run_id=$2::uuid
                   ORDER BY definition_id, scenario_key, execution_plan_id""",
                task_id,
                agent_run_id,
            )
        return tuple(
            parse_journey_execution_plan(_as_mapping(row["plan_payload"], "plan_payload"))
            for row in rows
        )

    async def add_execution_plan(self, *, plan: JourneyExecutionPlan) -> bool:
        """只在任务与 Definition 快照归属一致时锁定执行计划。"""
        pool = await self._get_pool()
        async with pool.acquire() as connection, connection.transaction():
            created = await connection.fetchval(
                """
                INSERT INTO qc_journey_execution_plan (
                    execution_plan_id, task_id, definition_id,
                    definition_version, definition_content_hash,
                    schema_version, plan_payload, agent_run_id, scenario_key
                )
                SELECT
                    $1,
                    task.id,
                    definition.definition_id,
                    definition.version,
                    definition.content_hash,
                    $7,
                    $8::jsonb,
                    $9::uuid,
                    $10::text
                FROM qc_task AS task
                INNER JOIN qc_journey_definition AS definition
                    ON definition.definition_id = $3
                   AND definition.version = $4
                   AND definition.content_hash = $5
                   AND definition.schema_version = $6
                WHERE task.id = $2::uuid
                  AND ($9::uuid IS NULL OR EXISTS (
                    SELECT 1 FROM qc_agent_run AS run
                    WHERE run.id=$9::uuid AND run.task_id=task.id AND run.status='running'
                  ))
                  AND ($9::uuid IS NULL OR
                    definition.definition_payload->'checkpoints'=$8::jsonb->'checkpoints')
                ON CONFLICT DO NOTHING
                RETURNING true
                """,
                plan.execution_plan_id,
                plan.task_id,
                plan.journey_version.definition_id,
                plan.journey_version.version,
                plan.journey_version.content_hash,
                plan.journey_version.schema_version,
                plan.schema_version,
                _json_dumps(plan.to_dict()),
                plan.agent_run_id,
                plan.scenario_key,
            )
        return bool(created)

    async def _get_pool(self) -> Any:
        if self._pool is not None:
            return self._pool
        return await get_postgres_pool(self._database_url)


def _parse_definition(payload: Any) -> JourneyDefinition | None:
    if payload is None:
        return None
    return parse_journey_definition(_as_mapping(payload, "definition_payload"))


def _parse_plan(payload: Any) -> JourneyExecutionPlan | None:
    if payload is None:
        return None
    return parse_journey_execution_plan(_as_mapping(payload, "plan_payload"))


def _as_mapping(value: Any, field_name: str) -> Mapping[str, Any]:
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, Mapping):
        raise ValueError(f"{field_name} 必须为对象")
    return value


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
