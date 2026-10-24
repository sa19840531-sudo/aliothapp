"""Journey 可恢复执行游标与步骤审计的 PostgreSQL 仓储。"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from services.api.app.application.journey_execution import (
    CommitJourneyStepCommand,
    JourneyExecutionPersistenceError,
)
from services.api.app.domain.journey import (
    JourneyExecutionSpec,
    JourneyExecutionState,
    JourneyExecutionStatus,
)
from services.api.app.domain.protocol import stable_protocol_hash
from services.api.app.infrastructure.db.pool import get_postgres_pool


class PostgresJourneyExecutionRepository:
    """以乐观锁原子提交步骤尝试和执行游标。"""

    def __init__(self, *, database_url: str | None = None, pool: Any | None = None) -> None:
        self._database_url = database_url
        self._pool = pool

    async def start(self, *, spec: JourneyExecutionSpec) -> JourneyExecutionState:
        """幂等创建执行；同一身份不允许替换输入快照。"""
        payload = spec.to_dict()
        pool = await self._get_pool()
        async with pool.acquire() as connection, connection.transaction():
            await connection.execute(
                "SELECT pg_advisory_xact_lock(hashtext($1))",
                f"task-finalization:{spec.task_id}",
            )
            row = await connection.fetchrow(
                """
                INSERT INTO qc_journey_execution AS existing (
                    execution_id, execution_plan_id, task_id, session_id,
                    agent_run_id, surface_kind, execution_epoch,
                    protocol_version, spec_hash, spec_payload,
                    current_capture_id, deadline, finalization_epoch
                )
                SELECT
                    $1,
                    plan.execution_plan_id,
                    task.id,
                    $4,
                    run.id,
                    $6,
                    $7,
                    $8,
                    $9,
                    $10::jsonb,
                    capture.id,
                    $12,
                    task.finalization_epoch
                FROM qc_task AS task
                INNER JOIN qc_journey_execution_plan AS plan
                    ON plan.execution_plan_id = $2
                   AND plan.task_id = task.id
                INNER JOIN qc_agent_run AS run
                    ON run.id = $5::uuid
                   AND run.task_id = task.id
                   AND run.agent_type IN ('explorer', 'execution')
                   AND run.status = 'running'
                INNER JOIN qc_journey_definition AS definition
                    ON definition.definition_id = plan.definition_id
                   AND definition.version = plan.definition_version
                   AND definition.content_hash = plan.definition_content_hash
                   AND definition.surface_kinds ? $6
                INNER JOIN qc_capture AS capture
                    ON capture.id = $11::uuid
                   AND capture.task_id = task.id
                   AND capture.capture_status = 'captured'
                WHERE task.id = $3::uuid
                  AND task.status = 'running'
                  AND task.finalization_state IN (
                      'open', 'exploration_draining', 'session_draining'
                  )
                  AND $12 > now()
                ON CONFLICT (execution_id) DO UPDATE
                SET execution_id = existing.execution_id
                WHERE existing.spec_hash = EXCLUDED.spec_hash
                  AND existing.execution_plan_id = EXCLUDED.execution_plan_id
                  AND existing.task_id = EXCLUDED.task_id
                RETURNING
                    existing.spec_payload,
                    existing.status,
                    existing.next_step_index,
                    existing.current_step_attempt_count,
                    existing.current_capture_id::text AS current_capture_id,
                    existing.gap_count,
                    existing.row_version,
                    existing.error_code,
                    existing.error_message
                """,
                spec.execution_id,
                spec.execution_plan_id,
                spec.task_id,
                spec.session_id,
                spec.agent_run_id,
                spec.surface_kind.value,
                spec.execution_epoch,
                spec.protocol_version,
                stable_protocol_hash(payload),
                _json_dumps(payload),
                spec.source_capture_id,
                spec.deadline,
            )
        if row is None:
            raise JourneyExecutionPersistenceError("Journey 执行归属无效、已过期或输入身份发生漂移")
        return _parse_state(row)

    async def commit_step(
        self,
        *,
        command: CommitJourneyStepCommand,
    ) -> JourneyExecutionState:
        """同一事务写入不可变尝试，并用版本和游标推进执行。"""
        payload = command.attempt.to_dict()
        pool = await self._get_pool()
        async with pool.acquire() as connection, connection.transaction():
            inserted_id = await connection.fetchval(
                """
                INSERT INTO qc_journey_step_attempt AS existing (
                    attempt_id, execution_id, task_id, step_id, step_index,
                    attempt_no, status, protocol_version, payload_hash,
                    attempt_payload, source_capture_id, result_capture_id,
                    action_execution_id, error_code, error_message, retryable,
                    started_at, finished_at
                )
                SELECT
                    $1,
                    execution.execution_id,
                    execution.task_id,
                    $3,
                    $4,
                    $5,
                    $6,
                    $7,
                    $8,
                    $9::jsonb,
                    source_capture.id,
                    result_capture.id,
                    $12,
                    $13,
                    $14,
                    $15,
                    $16,
                    $17
                FROM qc_journey_execution AS execution
                INNER JOIN qc_capture AS source_capture
                    ON source_capture.id = $10::uuid
                   AND source_capture.task_id = execution.task_id
                   AND source_capture.capture_status = 'captured'
                LEFT JOIN qc_capture AS result_capture
                    ON result_capture.id = $11::uuid
                   AND result_capture.task_id = execution.task_id
                   AND result_capture.capture_status = 'captured'
                WHERE execution.execution_id = $2
                  AND execution.status = 'running'
                  AND execution.row_version = $18
                  AND execution.next_step_index = $19
                  AND execution.current_step_attempt_count = $20
                  AND (
                    $11::uuid IS NULL
                    OR result_capture.id IS NOT NULL
                  )
                ON CONFLICT (attempt_id) DO UPDATE
                SET attempt_id = existing.attempt_id
                WHERE existing.payload_hash = EXCLUDED.payload_hash
                  AND existing.execution_id = EXCLUDED.execution_id
                RETURNING existing.attempt_id
                """,
                command.attempt.attempt_id,
                command.execution_id,
                command.attempt.step_id,
                command.attempt.step_index,
                command.attempt.attempt_no,
                command.attempt.status.value,
                command.attempt.protocol_version,
                stable_protocol_hash(payload),
                _json_dumps(payload),
                command.attempt.source_capture_id,
                command.attempt.result_capture_id,
                command.attempt.action_execution_id,
                command.attempt.error_code,
                command.attempt.error_message,
                command.attempt.retryable,
                command.attempt.started_at,
                command.attempt.finished_at,
                command.expected_row_version,
                command.expected_step_index,
                command.expected_attempt_count,
            )
            if inserted_id is None:
                raise JourneyExecutionPersistenceError(
                    "Journey 步骤尝试归属无效、游标已变化或幂等载荷漂移"
                )
            row = await connection.fetchrow(
                """
                UPDATE qc_journey_execution AS execution
                SET
                    status = $2,
                    next_step_index = $3,
                    current_step_attempt_count = $4,
                    current_capture_id = current_capture.id,
                    gap_count = $6,
                    row_version = execution.row_version + 1,
                    error_code = $7,
                    error_message = $8,
                    finished_at = CASE
                        WHEN $2 = 'running' THEN NULL
                        ELSE now()
                    END,
                    updated_at = now()
                FROM qc_capture AS current_capture
                WHERE execution.execution_id = $1
                  AND execution.status = 'running'
                  AND execution.row_version = $9
                  AND execution.next_step_index = $10
                  AND execution.current_step_attempt_count = $11
                  AND current_capture.id = $5::uuid
                  AND current_capture.task_id = execution.task_id
                  AND current_capture.capture_status = 'captured'
                RETURNING
                    execution.spec_payload,
                    execution.status,
                    execution.next_step_index,
                    execution.current_step_attempt_count,
                    execution.current_capture_id::text AS current_capture_id,
                    execution.gap_count,
                    execution.row_version,
                    execution.error_code,
                    execution.error_message
                """,
                command.execution_id,
                command.target_status.value,
                command.next_step_index,
                command.next_step_attempt_count,
                command.current_capture_id,
                command.gap_count,
                command.error_code,
                command.error_message,
                command.expected_row_version,
                command.expected_step_index,
                command.expected_attempt_count,
            )
            if row is None:
                raise JourneyExecutionPersistenceError(
                    "Journey 执行状态、版本或当前 Capture 已变化"
                )
        return _parse_state(row)

    async def _get_pool(self) -> Any:
        if self._pool is not None:
            return self._pool
        return await get_postgres_pool(self._database_url)


def _parse_state(row: Mapping[str, Any]) -> JourneyExecutionState:
    payload = _as_mapping(row["spec_payload"], "spec_payload")
    return JourneyExecutionState(
        spec=JourneyExecutionSpec.from_dict(dict(payload)),
        status=JourneyExecutionStatus(str(row["status"])),
        next_step_index=int(row["next_step_index"]),
        current_step_attempt_count=int(row["current_step_attempt_count"]),
        current_capture_id=str(row["current_capture_id"]),
        gap_count=int(row["gap_count"]),
        row_version=int(row["row_version"]),
        error_code=row["error_code"],
        error_message=row["error_message"],
    )


def _as_mapping(value: Any, field_name: str) -> Mapping[str, Any]:
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, Mapping):
        raise JourneyExecutionPersistenceError(f"{field_name} 必须为对象")
    return value


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
