"""Surface 阶段运行与步骤持久化 SQL 合同测试。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from services.api.app.application.surface_sessions import SessionCommandPermit
from services.api.app.domain.exploration import (
    SurfacePhaseRunStatus,
    SurfacePhaseStepStatus,
)
from services.api.app.domain.interaction import SurfaceKind
from services.api.app.infrastructure.db.surface_phase_repository import (
    PostgresSurfacePhaseRepository,
)

TASK_ID = "11111111-1111-1111-1111-111111111111"
SESSION_ID = "session-1"
CAPTURE_ID = "22222222-2222-2222-2222-222222222222"
LEASE_TOKEN = "33333333-3333-3333-3333-333333333333"
NOW = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)


class FakeTransaction:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        return None


class FakeAcquire:
    def __init__(self, connection) -> None:
        self.connection = connection

    async def __aenter__(self):
        return self.connection

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        return None


class FakePool:
    def __init__(self, connection) -> None:
        self.connection = connection

    def acquire(self) -> FakeAcquire:
        return FakeAcquire(self.connection)


class PhaseConnection:
    def __init__(self) -> None:
        self.queries: list[str] = []
        self.run_row_version = 0

    def transaction(self) -> FakeTransaction:
        return FakeTransaction()

    async def fetchrow(self, query: str, *args: Any):
        normalized = " ".join(query.split())
        self.queries.append(normalized)
        if "INSERT INTO qc_surface_phase_run" in normalized:
            return _run_row(
                phase_run_id=args[0],
                phase_key=args[4],
                phase_protocol_version=args[5],
                idempotency_key=args[6],
                source_capture_id=args[7],
                context_payload={"phase": "startup_ad_countdown"},
                command_sequence=args[12],
                finalization_epoch=args[13],
                row_version=self.run_row_version,
            )
        if "WITH phase AS" in normalized:
            self.run_row_version += 1
            return _step_row(
                phase_run_id=args[0],
                phase_step_id=args[1],
                step_key=args[2],
                status=args[3],
                decision_status=args[4],
                reason_code=args[5],
                source_capture_id=args[6],
                decision_payload={"status": args[4], "reason_code": args[5]},
                evidence_artifact_ids=["artifact-1"],
            )
        if "UPDATE qc_surface_phase_run AS phase" in normalized:
            self.run_row_version += 1
            return _run_row(
                phase_run_id=args[0],
                phase_key="startup_ad_countdown",
                phase_protocol_version="startup-ad-countdown-phase.v1",
                idempotency_key="task:startup-ad:1",
                source_capture_id=CAPTURE_ID,
                context_payload={"phase": "startup_ad_countdown"},
                command_sequence=7,
                finalization_epoch=2,
                status=args[1],
                terminal_reason_code=args[2],
                row_version=self.run_row_version,
                finished_at=NOW,
            )
        raise AssertionError(f"未处理的 fetchrow SQL：{normalized}")


def _permit() -> SessionCommandPermit:
    return SessionCommandPermit(
        task_id=TASK_ID,
        session_id=SESSION_ID,
        surface_kind=SurfaceKind.WEB,
        lease_owner="worker-1",
        lease_token=LEASE_TOKEN,
        command_sequence=7,
        finalization_epoch=2,
        lease_expires_at=NOW + timedelta(minutes=2),
    )


def _run_row(
    *,
    phase_run_id: str,
    phase_key: str,
    phase_protocol_version: str,
    idempotency_key: str,
    source_capture_id: str,
    context_payload: dict[str, Any],
    command_sequence: int,
    finalization_epoch: int,
    status: str = "running",
    terminal_reason_code: str | None = None,
    row_version: int = 0,
    finished_at: datetime | None = None,
) -> dict[str, Any]:
    return {
        "phase_run_id": phase_run_id,
        "task_id": TASK_ID,
        "session_id": SESSION_ID,
        "surface_kind": "web",
        "phase_key": phase_key,
        "phase_protocol_version": phase_protocol_version,
        "idempotency_key": idempotency_key,
        "source_capture_id": source_capture_id,
        "status": status,
        "command_sequence": command_sequence,
        "finalization_epoch": finalization_epoch,
        "context_payload": context_payload,
        "evidence_artifact_ids": [],
        "step_count": 0,
        "terminal_reason_code": terminal_reason_code,
        "row_version": row_version,
        "started_at": NOW,
        "finished_at": finished_at,
    }


def _step_row(
    *,
    phase_run_id: str,
    phase_step_id: str,
    step_key: str,
    status: str,
    decision_status: str,
    reason_code: str,
    source_capture_id: str,
    decision_payload: dict[str, Any],
    evidence_artifact_ids: list[str],
) -> dict[str, Any]:
    return {
        "phase_step_id": phase_step_id,
        "phase_run_id": phase_run_id,
        "task_id": TASK_ID,
        "session_id": SESSION_ID,
        "step_key": step_key,
        "step_index": 1,
        "status": status,
        "decision_status": decision_status,
        "reason_code": reason_code,
        "source_capture_id": source_capture_id,
        "result_capture_id": None,
        "action_execution_id": None,
        "decision_payload": decision_payload,
        "evidence_artifact_ids": evidence_artifact_ids,
        "next_action_payload": {"action_type": "timed_capture"},
        "created_at": NOW,
    }


async def test_surface_phase_run_starts_under_session_lease_and_current_capture() -> None:
    connection = PhaseConnection()
    repository = PostgresSurfacePhaseRepository(pool=FakePool(connection))

    run = await repository.start_run(
        task_id=TASK_ID,
        session_id=SESSION_ID,
        surface_kind=SurfaceKind.WEB,
        phase_key="startup_ad_countdown",
        phase_protocol_version="startup-ad-countdown-phase.v1",
        idempotency_key="task:startup-ad:1",
        source_capture_id=CAPTURE_ID,
        context_payload={"phase": "startup_ad_countdown"},
        permit=_permit(),
    )

    assert run.status is SurfacePhaseRunStatus.RUNNING
    query = connection.queries[0]
    assert "session.current_capture_id = capture.id" in query
    assert "session.lease_token = $11::uuid" in query
    assert "session.finalization_epoch = $14" in query
    assert "capture.capture_status = 'captured'" in query


async def test_surface_phase_step_locks_run_and_is_idempotent_by_step_key() -> None:
    connection = PhaseConnection()
    repository = PostgresSurfacePhaseRepository(pool=FakePool(connection))
    run = await repository.start_run(
        task_id=TASK_ID,
        session_id=SESSION_ID,
        surface_kind=SurfaceKind.WEB,
        phase_key="startup_ad_countdown",
        phase_protocol_version="startup-ad-countdown-phase.v1",
        idempotency_key="task:startup-ad:1",
        source_capture_id=CAPTURE_ID,
        context_payload={"phase": "startup_ad_countdown"},
        permit=_permit(),
    )

    step = await repository.record_step(
        phase_run_id=run.phase_run_id,
        step_key="observe-countdown-1",
        status=SurfacePhaseStepStatus.ACTION_PLANNED,
        decision_status="waiting_countdown",
        reason_code="countdown_not_finished",
        source_capture_id=CAPTURE_ID,
        decision_payload={
            "status": "waiting_countdown",
            "reason_code": "countdown_not_finished",
        },
        evidence_artifact_ids=("artifact-1",),
        next_action_payload={"action_type": "timed_capture"},
    )

    assert step.status is SurfacePhaseStepStatus.ACTION_PLANNED
    query = connection.queries[1]
    assert "FOR UPDATE" in query
    assert "ON CONFLICT (phase_run_id, step_key)" in query
    assert "GREATEST(run.step_count, inserted.step_index)" in query
    assert "WHEN run.step_count < inserted.step_index THEN 1" in query
    assert "source.surface_session_id = phase.session_id" in query


async def test_surface_phase_run_finishes_with_expected_row_version() -> None:
    connection = PhaseConnection()
    repository = PostgresSurfacePhaseRepository(pool=FakePool(connection))

    finished = await repository.finish_run(
        phase_run_id="surface_phase_run:1",
        status=SurfacePhaseRunStatus.COMPLETED,
        terminal_reason_code="startup_ad_closed_and_stable",
        expected_row_version=0,
    )

    assert finished.status is SurfacePhaseRunStatus.COMPLETED
    assert finished.terminal_reason_code == "startup_ad_closed_and_stable"
    query = connection.queries[0]
    assert "phase.status = 'running'" in query
    assert "phase.row_version = $4" in query


async def test_surface_phase_repository_lists_steps_in_persisted_order() -> None:
    class StepListConnection(PhaseConnection):
        async def fetch(self, query: str, *args: Any):
            normalized = " ".join(query.split())
            self.queries.append(normalized)
            assert args == ("surface_phase_run:1",)
            return [
                _step_row(
                    phase_run_id="surface_phase_run:1",
                    phase_step_id="step-1",
                    step_key="credential_submit_planned",
                    status="action_planned",
                    decision_status="checking",
                    reason_code="credential_submit_planned",
                    source_capture_id=CAPTURE_ID,
                    decision_payload={"status": "checking"},
                    evidence_artifact_ids=[],
                )
            ]

    connection = StepListConnection()
    repository = PostgresSurfacePhaseRepository(pool=FakePool(connection))

    steps = await repository.list_steps(phase_run_id="surface_phase_run:1")

    assert [step.step_key for step in steps] == ["credential_submit_planned"]
    assert "ORDER BY step_index ASC" in connection.queries[0]
