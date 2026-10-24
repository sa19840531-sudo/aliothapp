"""PostgreSQL 流程基建仓储：控制／挂起／预约承诺／事件序／观察事件。"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from services.api.app.domain.exploration.flow_foundation import (
    ArtifactCommitment,
    CaptureOwnershipBinding,
    CaptureReservation,
    CaptureReservationStatus,
)
from services.api.app.domain.exploration.flow_foundation_control import (
    BudgetReservation,
    BudgetReservationStatus,
    FlowClaimRecord,
    ParentHoldStatus,
    ParentWorkHold,
)
from services.api.app.domain.exploration.flow_foundation_evidence import (
    ObservationClockDomain,
)
from services.api.app.infrastructure.db.pool import get_postgres_pool

logger = logging.getLogger(__name__)


class FlowFoundationPersistenceError(RuntimeError):
    """流程基建落库失败。"""

    def __init__(
        self,
        message: str,
        *,
        error_code: str = "FLOW_FOUNDATION_PERSISTENCE_FAILED",
        retryable: bool = True,
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.retryable = retryable


class PostgresFlowFoundationRepository:
    """流程基建异步 CRUD；供 API 与 AppFlowFoundationRuntime 调用。"""

    def __init__(self, *, database_url: str | None = None, pool: Any | None = None) -> None:
        self._database_url = database_url
        self._pool = pool

    async def upsert_claim(
        self,
        *,
        claim: FlowClaimRecord,
        task_id: str,
        flow_code: str,
        definition_hash: str,
        control_status: str = "claimed",
        execution_result: str = "pending",
        evidence_state: str = "pending",
        resume_result: str = "not_required",
        payload: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        body = dict(payload or {})
        body.setdefault("parent_work_id", claim.parent_work_id)
        body.setdefault("parent_version", claim.parent_version)
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                INSERT INTO qc_flow_control (
                    phase_run_id, task_id, trigger_key, flow_code, definition_hash,
                    control_status, execution_result, evidence_state, resume_result,
                    budget_consumption_id, parent_hold_id, payload
                )
                VALUES (
                    $1, $2::uuid, $3, $4, $5,
                    $6, $7, $8, $9,
                    $10, $11, $12::jsonb
                )
                ON CONFLICT (phase_run_id) DO UPDATE SET
                    trigger_key = EXCLUDED.trigger_key,
                    flow_code = EXCLUDED.flow_code,
                    definition_hash = EXCLUDED.definition_hash,
                    control_status = EXCLUDED.control_status,
                    execution_result = EXCLUDED.execution_result,
                    evidence_state = EXCLUDED.evidence_state,
                    resume_result = EXCLUDED.resume_result,
                    budget_consumption_id = EXCLUDED.budget_consumption_id,
                    parent_hold_id = EXCLUDED.parent_hold_id,
                    payload = EXCLUDED.payload,
                    updated_at = now()
                RETURNING *
                """,
                claim.phase_run_id,
                task_id,
                claim.trigger_key,
                flow_code,
                definition_hash,
                control_status,
                execution_result,
                evidence_state,
                resume_result,
                claim.consumption_id,
                claim.hold_id,
                json.dumps(body, ensure_ascii=False),
            )
        return _row_to_dict(row)

    async def get_claim(self, *, phase_run_id: str) -> dict[str, Any] | None:
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                "SELECT * FROM qc_flow_control WHERE phase_run_id = $1",
                phase_run_id,
            )
        return _row_to_dict(row) if row is not None else None

    async def get_claim_by_trigger_key(self, *, trigger_key: str) -> dict[str, Any] | None:
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                "SELECT * FROM qc_flow_control WHERE trigger_key = $1",
                trigger_key,
            )
        return _row_to_dict(row) if row is not None else None

    async def upsert_hold(self, hold: ParentWorkHold) -> dict[str, Any]:
        pool = await self._get_pool()
        async with pool.acquire() as connection, connection.transaction():
            row = await connection.fetchrow(
                """
                INSERT INTO qc_flow_parent_work_hold (
                    hold_id, task_id, phase_run_id, parent_work_kind, parent_work_id,
                    parent_version, owner_execution_segment_id, status,
                    frontier_ref, resume_snapshot_ref
                )
                VALUES (
                    $1, $2::uuid, $3, $4, $5,
                    $6, $7, $8,
                    $9, $10
                )
                ON CONFLICT (hold_id) DO UPDATE SET
                    status = EXCLUDED.status,
                    frontier_ref = EXCLUDED.frontier_ref,
                    resume_snapshot_ref = EXCLUDED.resume_snapshot_ref
                RETURNING *
                """,
                hold.hold_id,
                hold.task_id,
                hold.phase_run_id,
                hold.parent_work_kind,
                hold.parent_work_id,
                hold.parent_version,
                hold.owner_execution_segment_id,
                hold.status.value,
                hold.frontier_ref or None,
                hold.resume_snapshot_ref or None,
            )
        return _row_to_dict(row)

    async def get_active_hold(self, *, parent_work_id: str) -> dict[str, Any] | None:
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                SELECT * FROM qc_flow_parent_work_hold
                WHERE parent_work_id = $1 AND status = 'active'
                LIMIT 1
                """,
                parent_work_id,
            )
        return _row_to_dict(row) if row is not None else None

    async def upsert_budget(self, reservation: BudgetReservation) -> dict[str, Any]:
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                INSERT INTO qc_flow_budget_reservation (
                    consumption_id, task_id, phase_run_id, units, status, updated_at
                )
                VALUES ($1, $2::uuid, $3, $4, $5, now())
                ON CONFLICT (consumption_id) DO UPDATE SET
                    units = EXCLUDED.units,
                    status = EXCLUDED.status,
                    updated_at = now()
                RETURNING *
                """,
                reservation.consumption_id,
                reservation.task_id,
                reservation.phase_run_id,
                reservation.units,
                reservation.status.value,
            )
        return _row_to_dict(row)

    async def get_budget(self, *, consumption_id: str) -> dict[str, Any] | None:
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                "SELECT * FROM qc_flow_budget_reservation WHERE consumption_id = $1",
                consumption_id,
            )
        return _row_to_dict(row) if row is not None else None

    async def next_resource_event_seq(
        self, *, resource_id: str, producer_epoch: str
    ) -> int:
        pool = await self._get_pool()
        async with pool.acquire() as connection, connection.transaction():
            row = await connection.fetchrow(
                """
                INSERT INTO qc_flow_resource_event_seq (resource_id, producer_epoch, next_seq)
                VALUES ($1, $2, 1)
                ON CONFLICT (resource_id, producer_epoch) DO UPDATE SET
                    next_seq = qc_flow_resource_event_seq.next_seq + 1
                RETURNING next_seq
                """,
                resource_id,
                producer_epoch,
            )
        return int(row["next_seq"])

    async def upsert_reservation(self, reservation: CaptureReservation) -> dict[str, Any]:
        ownership = {
            "task_id": reservation.ownership.task_id,
            "capture_id": reservation.ownership.capture_id,
            "owner_phase_run_id": reservation.ownership.owner_phase_run_id,
            "owner_execution_segment_id": reservation.ownership.owner_execution_segment_id,
            "action_execution_id": reservation.ownership.action_execution_id,
            "session_id": reservation.ownership.session_id,
        }
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                INSERT INTO qc_capture_reservation (
                    capture_request_id, capture_id, task_id, status, ownership, artifact_slots
                )
                VALUES ($1, $2, $3::uuid, $4, $5::jsonb, $6::text[])
                ON CONFLICT (capture_request_id) DO UPDATE SET
                    status = EXCLUDED.status,
                    ownership = EXCLUDED.ownership,
                    artifact_slots = EXCLUDED.artifact_slots
                RETURNING *
                """,
                reservation.capture_request_id,
                reservation.capture_id,
                reservation.task_id,
                reservation.status.value,
                json.dumps(ownership, ensure_ascii=False),
                list(reservation.artifact_slots),
            )
        return _row_to_dict(row)

    async def get_reservation(self, *, capture_request_id: str) -> CaptureReservation | None:
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                "SELECT * FROM qc_capture_reservation WHERE capture_request_id = $1",
                capture_request_id,
            )
        if row is None:
            return None
        return _parse_reservation(row)

    async def upsert_commitment(self, commitment: ArtifactCommitment) -> dict[str, Any]:
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                INSERT INTO qc_artifact_commitment (
                    capture_request_id, artifact_slot, capture_id,
                    content_digest, content_length, produced_under_fencing_token
                )
                VALUES ($1, $2, $3, $4, $5, $6)
                ON CONFLICT (capture_request_id, artifact_slot) DO UPDATE SET
                    content_digest = EXCLUDED.content_digest,
                    content_length = EXCLUDED.content_length,
                    produced_under_fencing_token = EXCLUDED.produced_under_fencing_token
                RETURNING *
                """,
                commitment.capture_request_id,
                commitment.artifact_slot,
                commitment.capture_id,
                commitment.content_digest,
                commitment.content_length,
                commitment.produced_under_fencing_token,
            )
        return _row_to_dict(row)

    async def get_commitment(
        self,
        *,
        capture_request_id: str,
        artifact_slot: str,
    ) -> ArtifactCommitment | None:
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                SELECT * FROM qc_artifact_commitment
                WHERE capture_request_id = $1 AND artifact_slot = $2
                """,
                capture_request_id,
                artifact_slot,
            )
        if row is None:
            return None
        return ArtifactCommitment(
            capture_request_id=str(row["capture_request_id"]),
            capture_id=str(row["capture_id"]),
            artifact_slot=str(row["artifact_slot"]),
            content_digest=str(row["content_digest"]),
            content_length=int(row["content_length"]),
            produced_under_fencing_token=str(row["produced_under_fencing_token"]),
        )

    async def insert_observation_event(
        self,
        *,
        task_id: str,
        phase_run_id: str,
        resource_id: str,
        producer_epoch: str,
        resource_event_seq: int,
        sample_index: int,
        capture_id: str,
        clock_domain: ObservationClockDomain | str,
        content_digest: str,
    ) -> dict[str, Any]:
        domain = (
            clock_domain.value
            if isinstance(clock_domain, ObservationClockDomain)
            else str(clock_domain)
        )
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                INSERT INTO qc_flow_observation_event (
                    task_id, phase_run_id, resource_id, producer_epoch,
                    resource_event_seq, sample_index, capture_id, clock_domain,
                    content_digest
                )
                VALUES (
                    $1::uuid, $2, $3, $4,
                    $5, $6, $7, $8,
                    $9
                )
                ON CONFLICT (resource_id, producer_epoch, resource_event_seq) DO NOTHING
                RETURNING *
                """,
                task_id,
                phase_run_id,
                resource_id,
                producer_epoch,
                resource_event_seq,
                sample_index,
                capture_id,
                domain,
                content_digest,
            )
        if row is None:
            # 幂等冲突：回读已有行
            async with pool.acquire() as connection:
                row = await connection.fetchrow(
                    """
                    SELECT * FROM qc_flow_observation_event
                    WHERE resource_id = $1
                      AND producer_epoch = $2
                      AND resource_event_seq = $3
                    """,
                    resource_id,
                    producer_epoch,
                    resource_event_seq,
                )
        return _row_to_dict(row)

    async def list_flows_for_task(self, *, task_id: str) -> list[dict[str, Any]]:
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            rows = await connection.fetch(
                """
                SELECT * FROM qc_flow_control
                WHERE task_id = $1::uuid
                ORDER BY created_at ASC, phase_run_id ASC
                """,
                task_id,
            )
        return [_row_to_dict(row) for row in rows]

    async def list_observations_for_phase(
        self, *, task_id: str, phase_run_id: str
    ) -> list[dict[str, Any]]:
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            rows = await connection.fetch(
                """
                SELECT * FROM qc_flow_observation_event
                WHERE task_id = $1::uuid AND phase_run_id = $2
                ORDER BY resource_event_seq ASC, sample_index ASC
                """,
                task_id,
                phase_run_id,
            )
        return [_row_to_dict(row) for row in rows]

    async def list_flow_links_for_capture(
        self, *, task_id: str, capture_id: str
    ) -> list[dict[str, Any]]:
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            rows = await connection.fetch(
                """
                SELECT
                    event.task_id,
                    event.phase_run_id,
                    event.capture_id,
                    event.resource_id,
                    event.producer_epoch,
                    event.resource_event_seq,
                    event.sample_index,
                    event.content_digest,
                    control.trigger_key,
                    control.flow_code,
                    control.definition_hash,
                    control.control_status,
                    reservation.capture_request_id,
                    commitment.artifact_slot,
                    commitment.produced_under_fencing_token
                FROM qc_flow_observation_event AS event
                LEFT JOIN qc_flow_control AS control
                    ON control.phase_run_id = event.phase_run_id
                   AND control.task_id = event.task_id
                LEFT JOIN qc_capture_reservation AS reservation
                    ON reservation.capture_id = event.capture_id
                   AND reservation.task_id = event.task_id
                LEFT JOIN qc_artifact_commitment AS commitment
                    ON commitment.capture_id = event.capture_id
                   AND commitment.capture_request_id = reservation.capture_request_id
                WHERE event.task_id = $1::uuid
                  AND event.capture_id = $2
                ORDER BY event.resource_event_seq ASC
                """,
                task_id,
                capture_id,
            )
        return [_row_to_dict(row) for row in rows]

    async def _get_pool(self) -> Any:
        if self._pool is not None:
            return self._pool
        return await get_postgres_pool(self._database_url)


def _parse_reservation(row: Mapping[str, Any]) -> CaptureReservation:
    ownership_raw = row["ownership"]
    if isinstance(ownership_raw, str):
        ownership_raw = json.loads(ownership_raw)
    if not isinstance(ownership_raw, Mapping):
        raise FlowFoundationPersistenceError(
            "capture reservation ownership 无效",
            error_code="CAPTURE_RESERVATION_CORRUPT",
            retryable=False,
        )
    slots = row.get("artifact_slots") or ()
    if isinstance(slots, str):
        slots = (slots,)
    return CaptureReservation(
        capture_request_id=str(row["capture_request_id"]),
        capture_id=str(row["capture_id"]),
        task_id=str(row["task_id"]),
        status=CaptureReservationStatus(str(row["status"])),
        ownership=CaptureOwnershipBinding(
            task_id=str(ownership_raw.get("task_id") or row["task_id"]),
            capture_id=str(ownership_raw.get("capture_id") or row["capture_id"]),
            owner_phase_run_id=_optional_str(ownership_raw.get("owner_phase_run_id")),
            owner_execution_segment_id=_optional_str(
                ownership_raw.get("owner_execution_segment_id")
            ),
            action_execution_id=_optional_str(ownership_raw.get("action_execution_id")),
            session_id=_optional_str(ownership_raw.get("session_id")),
        ),
        artifact_slots=tuple(str(item) for item in slots),
    )


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _row_to_dict(row: Mapping[str, Any] | None) -> dict[str, Any]:
    if row is None:
        return {}
    result: dict[str, Any] = {}
    for key, value in dict(row).items():
        if isinstance(value, datetime):
            result[key] = value.isoformat()
        elif hasattr(value, "hex"):  # uuid
            result[key] = str(value)
        elif isinstance(value, memoryview):
            result[key] = bytes(value).decode("utf-8", errors="replace")
        else:
            result[key] = value
    return result


def hold_from_row(row: Mapping[str, Any]) -> ParentWorkHold:
    return ParentWorkHold(
        hold_id=str(row["hold_id"]),
        task_id=str(row["task_id"]),
        phase_run_id=str(row["phase_run_id"]),
        parent_work_kind=str(row["parent_work_kind"]),
        parent_work_id=str(row["parent_work_id"]),
        parent_version=int(row["parent_version"]),
        owner_execution_segment_id=str(row["owner_execution_segment_id"]),
        status=ParentHoldStatus(str(row["status"])),
        frontier_ref=str(row.get("frontier_ref") or ""),
        resume_snapshot_ref=str(row.get("resume_snapshot_ref") or ""),
    )


def budget_from_row(row: Mapping[str, Any]) -> BudgetReservation:
    return BudgetReservation(
        consumption_id=str(row["consumption_id"]),
        task_id=str(row["task_id"]),
        phase_run_id=str(row["phase_run_id"]),
        units=int(row["units"]),
        status=BudgetReservationStatus(str(row["status"])),
    )


def claim_from_control_row(row: Mapping[str, Any]) -> FlowClaimRecord:
    payload = row.get("payload") or {}
    if isinstance(payload, str):
        payload = json.loads(payload)
    if not isinstance(payload, Mapping):
        payload = {}
    return FlowClaimRecord(
        trigger_key=str(row["trigger_key"]),
        phase_run_id=str(row["phase_run_id"]),
        consumption_id=str(row.get("budget_consumption_id") or ""),
        hold_id=str(row.get("parent_hold_id") or ""),
        parent_work_id=str(payload.get("parent_work_id") or ""),
        parent_version=int(payload.get("parent_version") or 0),
    )


__all__ = [
    "FlowFoundationPersistenceError",
    "PostgresFlowFoundationRepository",
    "budget_from_row",
    "claim_from_control_row",
    "hold_from_row",
]
