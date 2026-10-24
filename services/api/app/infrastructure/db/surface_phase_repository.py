"""Surface 阶段运行与阶段步骤的 PostgreSQL 实现。"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from services.api.app.application.surface_sessions import SessionCommandPermit
from services.api.app.domain.exploration import (
    SURFACE_PHASE_RUN_PROTOCOL_VERSION,
    SURFACE_PHASE_STEP_PROTOCOL_VERSION,
    SurfacePhaseRunSnapshot,
    SurfacePhaseRunStatus,
    SurfacePhaseStepSnapshot,
    SurfacePhaseStepStatus,
    build_surface_phase_payload_hash,
    build_surface_phase_run_id,
    build_surface_phase_step_id,
)
from services.api.app.domain.interaction import SurfaceKind
from services.api.app.domain.protocol import require_mapping, require_text
from services.api.app.infrastructure.db.pool import get_postgres_pool


class SurfacePhasePersistenceError(RuntimeError):
    """阶段运行、步骤、Session Lease 或 Capture 归属不满足持久化约束。"""


class PostgresSurfacePhaseRepository:
    """记录 W15/W16 Surface 阶段运行和步骤审计。"""

    def __init__(self, *, database_url: str | None = None, pool: Any | None = None) -> None:
        self._database_url = database_url
        self._pool = pool

    async def start_run(
        self,
        *,
        task_id: str,
        session_id: str,
        surface_kind: SurfaceKind,
        phase_key: str,
        phase_protocol_version: str,
        idempotency_key: str,
        source_capture_id: str,
        context_payload: Mapping[str, Any],
        permit: SessionCommandPermit,
    ) -> SurfacePhaseRunSnapshot:
        """在有效 Session Lease 和当前 Capture 下创建或复用阶段运行。"""

        _validate_permit(
            permit=permit,
            task_id=task_id,
            session_id=session_id,
            surface_kind=surface_kind,
        )
        phase_run_id = build_surface_phase_run_id(
            task_id=task_id,
            session_id=session_id,
            phase_key=phase_key,
            idempotency_key=idempotency_key,
        )
        context = require_mapping(context_payload, "context_payload")
        context_hash = build_surface_phase_payload_hash(context)
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                INSERT INTO qc_surface_phase_run AS existing (
                    phase_run_id, task_id, session_id, surface_kind,
                    phase_key, phase_protocol_version, idempotency_key,
                    source_capture_id, status, command_sequence,
                    finalization_epoch, context_payload, context_payload_hash
                )
                SELECT
                    $1, session.task_id, session.session_id, session.surface_kind,
                    $5, $6, $7, capture.id, 'running',
                    session.command_sequence, session.finalization_epoch,
                    $9::jsonb, $10
                FROM qc_surface_session AS session
                INNER JOIN qc_capture AS capture
                    ON capture.id = $8::uuid
                   AND capture.task_id = session.task_id
                   AND capture.surface_session_id = session.session_id
                   AND capture.surface_kind = session.surface_kind
                   AND capture.capture_status = 'captured'
                WHERE session.task_id = $2::uuid
                  AND session.session_id = $3
                  AND session.surface_kind = $4
                  AND session.current_capture_id = capture.id
                  AND session.lease_token = $11::uuid
                  AND session.lease_owner = $12
                  AND session.lease_expires_at > now()
                  AND session.command_sequence = $13
                  AND session.finalization_epoch = $14
                  AND session.status IN ('active', 'draining')
                ON CONFLICT (task_id, session_id, phase_key, idempotency_key)
                DO UPDATE
                SET phase_run_id = existing.phase_run_id
                WHERE existing.phase_run_id = EXCLUDED.phase_run_id
                  AND existing.context_payload_hash = EXCLUDED.context_payload_hash
                  AND existing.source_capture_id = EXCLUDED.source_capture_id
                RETURNING existing.*
                """,
                phase_run_id,
                task_id,
                session_id,
                surface_kind.value,
                phase_key,
                phase_protocol_version,
                idempotency_key,
                source_capture_id,
                _json_dumps(context),
                context_hash,
                permit.lease_token,
                permit.lease_owner,
                permit.command_sequence,
                permit.finalization_epoch,
            )
        if row is None:
            raise SurfacePhasePersistenceError(
                "阶段运行归属无效、Session Lease 失效、当前 Capture 不一致或幂等内容漂移"
            )
        return _parse_run(row)

    async def record_step(
        self,
        *,
        phase_run_id: str,
        step_key: str,
        status: SurfacePhaseStepStatus,
        decision_status: str,
        reason_code: str,
        source_capture_id: str,
        decision_payload: Mapping[str, Any],
        evidence_artifact_ids: Sequence[str] = (),
        next_action_payload: Mapping[str, Any] | None = None,
        action_execution_id: str | None = None,
        result_capture_id: str | None = None,
    ) -> SurfacePhaseStepSnapshot:
        """追加阶段步骤；相同 step_key 只能幂等复用相同决策。"""

        step_id = build_surface_phase_step_id(
            phase_run_id=phase_run_id,
            step_key=step_key,
        )
        decision = require_mapping(decision_payload, "decision_payload")
        next_action = (
            require_mapping(next_action_payload, "next_action_payload")
            if next_action_payload is not None
            else None
        )
        artifact_ids = _unique_text_tuple(evidence_artifact_ids, "evidence_artifact_ids")
        decision_hash = build_surface_phase_payload_hash(decision)
        pool = await self._get_pool()
        async with pool.acquire() as connection, connection.transaction():
            row = await connection.fetchrow(
                """
                WITH phase AS (
                    SELECT *
                    FROM qc_surface_phase_run
                    WHERE phase_run_id = $1
                      AND status = 'running'
                    FOR UPDATE
                ),
                inserted AS (
                    INSERT INTO qc_surface_phase_step AS existing (
                        phase_step_id, phase_run_id, task_id, session_id,
                        step_key, step_index, status, decision_status,
                        reason_code, source_capture_id, result_capture_id,
                        action_execution_id, decision_payload,
                        decision_payload_hash, next_action_payload,
                        evidence_artifact_ids
                    )
                    SELECT
                        $2, phase.phase_run_id, phase.task_id, phase.session_id,
                        $3, phase.step_count + 1, $4, $5, $6,
                        source.id, result.id, action.action_execution_id,
                        $10::jsonb, $11, $12::jsonb, $13::jsonb
                    FROM phase
                    INNER JOIN qc_capture AS source
                        ON source.id = $7::uuid
                       AND source.task_id = phase.task_id
                       AND source.surface_session_id = phase.session_id
                       AND source.capture_status = 'captured'
                    LEFT JOIN qc_capture AS result
                        ON result.id = $8::uuid
                       AND result.task_id = phase.task_id
                       AND result.surface_session_id = phase.session_id
                       AND result.capture_status = 'captured'
                    LEFT JOIN qc_action_execution AS action
                        ON action.action_execution_id = $9::text
                       AND action.task_id = phase.task_id
                       AND action.session_id = phase.session_id
                    WHERE ($8::uuid IS NULL OR result.id IS NOT NULL)
                      AND ($9::text IS NULL OR action.action_execution_id IS NOT NULL)
                    ON CONFLICT (phase_run_id, step_key)
                    DO UPDATE
                    SET phase_step_id = existing.phase_step_id
                    WHERE existing.phase_step_id = EXCLUDED.phase_step_id
                      AND existing.decision_payload_hash
                            = EXCLUDED.decision_payload_hash
                      AND existing.source_capture_id = EXCLUDED.source_capture_id
                    RETURNING existing.*
                ),
                advanced AS (
                    UPDATE qc_surface_phase_run AS run
                    SET
                        step_count = GREATEST(run.step_count, inserted.step_index),
                        evidence_artifact_ids = COALESCE((
                            SELECT jsonb_agg(DISTINCT value)
                            FROM jsonb_array_elements_text(
                                run.evidence_artifact_ids
                                || inserted.evidence_artifact_ids
                            ) AS value
                        ), '[]'::jsonb),
                        row_version = run.row_version + CASE
                            WHEN run.step_count < inserted.step_index THEN 1
                            ELSE 0
                        END,
                        updated_at = now()
                    FROM inserted
                    WHERE run.phase_run_id = inserted.phase_run_id
                    RETURNING run.phase_run_id
                )
                SELECT inserted.*
                FROM inserted
                INNER JOIN advanced
                    ON advanced.phase_run_id = inserted.phase_run_id
                """,
                phase_run_id,
                step_id,
                step_key,
                status.value,
                decision_status,
                reason_code,
                source_capture_id,
                result_capture_id,
                action_execution_id,
                _json_dumps(decision),
                decision_hash,
                _json_dumps(next_action) if next_action is not None else None,
                _json_dumps(list(artifact_ids)),
            )
        if row is None:
            raise SurfacePhasePersistenceError(
                "阶段步骤归属无效、Capture/Action 不一致、阶段已终止或幂等内容漂移"
            )
        return _parse_step(row)

    async def finish_run(
        self,
        *,
        phase_run_id: str,
        status: SurfacePhaseRunStatus,
        terminal_reason_code: str,
        expected_row_version: int,
    ) -> SurfacePhaseRunSnapshot:
        """将阶段运行封存为终态。"""

        if status is SurfacePhaseRunStatus.RUNNING:
            raise ValueError("finish_run 不能写入 running 状态")
        reason = require_text(terminal_reason_code, "terminal_reason_code")
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                UPDATE qc_surface_phase_run AS phase
                SET
                    status = $2,
                    terminal_reason_code = $3,
                    finished_at = now(),
                    row_version = phase.row_version + 1,
                    updated_at = now()
                WHERE phase.phase_run_id = $1
                  AND phase.status = 'running'
                  AND phase.row_version = $4
                RETURNING phase.*
                """,
                phase_run_id,
                status.value,
                reason,
                expected_row_version,
            )
        if row is None:
            raise SurfacePhasePersistenceError("阶段终态写回时状态或版本已变化")
        return _parse_run(row)

    async def get_run(self, *, phase_run_id: str) -> SurfacePhaseRunSnapshot | None:
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                "SELECT * FROM qc_surface_phase_run WHERE phase_run_id = $1",
                phase_run_id,
            )
        return _parse_run(row) if row is not None else None

    async def list_steps(
        self,
        *,
        phase_run_id: str,
    ) -> tuple[SurfacePhaseStepSnapshot, ...]:
        """按持久化顺序读取阶段步骤，供幂等恢复判断使用。"""
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            rows = await connection.fetch(
                """
                SELECT *
                FROM qc_surface_phase_step
                WHERE phase_run_id = $1
                ORDER BY step_index ASC
                """,
                phase_run_id,
            )
        return tuple(_parse_step(row) for row in rows)

    async def _get_pool(self) -> Any:
        if self._pool is not None:
            return self._pool
        return await get_postgres_pool(self._database_url)


def _validate_permit(
    *,
    permit: SessionCommandPermit,
    task_id: str,
    session_id: str,
    surface_kind: SurfaceKind,
) -> None:
    if not isinstance(permit, SessionCommandPermit):
        raise TypeError("permit 必须为 SessionCommandPermit")
    if (
        permit.task_id != task_id
        or permit.session_id != session_id
        or permit.surface_kind is not surface_kind
    ):
        raise ValueError("Session 命令许可与阶段运行不一致")


def _parse_run(row: Mapping[str, Any]) -> SurfacePhaseRunSnapshot:
    return SurfacePhaseRunSnapshot(
        phase_run_id=str(row["phase_run_id"]),
        task_id=str(row["task_id"]),
        session_id=str(row["session_id"]),
        surface_kind=SurfaceKind(str(row["surface_kind"])),
        phase_key=str(row["phase_key"]),
        phase_protocol_version=str(row["phase_protocol_version"]),
        idempotency_key=str(row["idempotency_key"]),
        source_capture_id=str(row["source_capture_id"]),
        status=SurfacePhaseRunStatus(str(row["status"])),
        command_sequence=int(row["command_sequence"]),
        finalization_epoch=int(row["finalization_epoch"]),
        row_version=int(row["row_version"]),
        context_payload=_as_mapping(row["context_payload"]),
        evidence_artifact_ids=_as_text_tuple(row["evidence_artifact_ids"]),
        step_count=int(row["step_count"]),
        terminal_reason_code=row["terminal_reason_code"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        protocol_version=SURFACE_PHASE_RUN_PROTOCOL_VERSION,
    )


def _parse_step(row: Mapping[str, Any]) -> SurfacePhaseStepSnapshot:
    return SurfacePhaseStepSnapshot(
        phase_step_id=str(row["phase_step_id"]),
        phase_run_id=str(row["phase_run_id"]),
        task_id=str(row["task_id"]),
        session_id=str(row["session_id"]),
        step_key=str(row["step_key"]),
        step_index=int(row["step_index"]),
        status=SurfacePhaseStepStatus(str(row["status"])),
        decision_status=str(row["decision_status"]),
        reason_code=str(row["reason_code"]),
        source_capture_id=str(row["source_capture_id"]),
        result_capture_id=(
            str(row["result_capture_id"]) if row["result_capture_id"] is not None else None
        ),
        action_execution_id=(
            str(row["action_execution_id"])
            if row["action_execution_id"] is not None
            else None
        ),
        decision_payload=_as_mapping(row["decision_payload"]),
        evidence_artifact_ids=_as_text_tuple(row["evidence_artifact_ids"]),
        next_action_payload=(
            _as_mapping(row["next_action_payload"])
            if row["next_action_payload"] is not None
            else None
        ),
        created_at=row["created_at"],
        protocol_version=SURFACE_PHASE_STEP_PROTOCOL_VERSION,
    )


def _as_mapping(value: Any) -> Mapping[str, Any]:
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, Mapping):
        raise SurfacePhasePersistenceError("payload 必须为对象")
    return value


def _as_text_tuple(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise SurfacePhasePersistenceError("evidence_artifact_ids 必须为数组")
    return tuple(str(item) for item in value)


def _unique_text_tuple(values: Sequence[str], field_name: str) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)):
        raise ValueError(f"{field_name} 必须为数组")
    result = tuple(require_text(value, field_name) for value in values)
    if len(set(result)) != len(result):
        raise ValueError(f"{field_name} 不能包含重复值")
    return result


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
