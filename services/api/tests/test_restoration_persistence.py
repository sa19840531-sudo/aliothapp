"""V2 恢复尝试与重放补采挂接的 PostgreSQL 契约测试。"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from services.api.app.application.evidence_requests import EvidenceRequestLeaseFence
from services.api.app.application.evidence_supplements import (
    AttachEvidenceSupplementCommand,
    EvidenceSupplementAttacher,
)
from services.api.app.application.restoration import new_running_restoration_attempt
from services.api.app.application.state_equivalence import StateEquivalenceGate
from services.api.app.application.supplemental_evidence_policy import (
    SupplementalEvidenceDisabledError,
    SupplementalEvidencePolicy,
)
from services.api.app.domain.inspection import (
    EvidenceReplayPolicy,
    EvidenceRequiredEquivalence,
    EvidenceSessionDependency,
    EvidenceSideEffectPolicy,
    EvidenceSourceGrade,
    RestorationAttemptStatus,
    RestorationStrategy,
    StateInvariantSnapshot,
    build_evidence_request,
    build_evidence_supplement,
    build_restoration_plan,
)
from services.api.app.domain.interaction import SurfaceKind
from services.api.app.infrastructure.db.evidence_supplement_repository import (
    PostgresEvidenceSupplementRepository,
)
from services.api.app.infrastructure.db.restoration_attempt_repository import (
    PostgresRestorationAttemptRepository,
)

TASK_ID = "00000000-0000-0000-0000-000000000001"
ORIGINAL_CAPTURE_ID = "00000000-0000-0000-0000-000000000002"
RESTORED_CAPTURE_ID = "00000000-0000-0000-0000-000000000003"
NOW = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)


class FakeTransaction:
    def __init__(self, connection: FakeConnection) -> None:
        self._connection = connection

    async def __aenter__(self) -> None:
        self._connection.transaction_entered += 1
        return None

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        return None


class FakeConnection:
    def __init__(
        self,
        *,
        fetchrow_results: list[Any] | None = None,
        fetchval_results: list[Any] | None = None,
    ) -> None:
        self.fetchrow_results = list(fetchrow_results or [])
        self.fetchval_results = list(fetchval_results or [])
        self.calls: list[tuple[str, str, tuple[Any, ...]]] = []
        self.transaction_entered = 0

    def transaction(self) -> FakeTransaction:
        return FakeTransaction(self)

    async def fetchrow(self, query: str, *args: Any) -> Any:
        self.calls.append(("fetchrow", " ".join(query.split()), args))
        return self.fetchrow_results.pop(0) if self.fetchrow_results else None

    async def fetchval(self, query: str, *args: Any) -> Any:
        self.calls.append(("fetchval", " ".join(query.split()), args))
        return self.fetchval_results.pop(0) if self.fetchval_results else None

    async def execute(self, query: str, *args: Any) -> str:
        self.calls.append(("execute", " ".join(query.split()), args))
        return "SELECT 1"


class FakeAcquire:
    def __init__(self, connection: FakeConnection) -> None:
        self._connection = connection

    async def __aenter__(self) -> FakeConnection:
        return self._connection

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        return None


class FakePool:
    def __init__(self, connection: FakeConnection) -> None:
        self.connection = connection

    def acquire(self) -> FakeAcquire:
        return FakeAcquire(self.connection)


def _request():
    return build_evidence_request(
        work_item_id="00000000-0000-0000-0000-000000000010",
        attempt_id="00000000-0000-0000-0000-000000000011",
        task_id=TASK_ID,
        rule_id="00000000-0000-0000-0000-000000000012",
        rule_code="alioth_1",
        target_capture_id=ORIGINAL_CAPTURE_ID,
        reason_code="TRUSTED_EVIDENCE_INVALID",
        evidence_goal="补采截图和动作轨迹",
        required_artifact_types=("screenshot", "action_trace"),
        session_dependency=EvidenceSessionDependency.ACTIVE_SESSION,
        replay_policy=EvidenceReplayPolicy.SAME_SURFACE_ONLY,
        required_equivalence=EvidenceRequiredEquivalence.SAME_SURFACE_CONTINUATION,
        side_effect_policy=EvidenceSideEffectPolicy.READ_ONLY,
        target_invariants={"route": "/product/42"},
        minimum_source_grade=EvidenceSourceGrade.A1,
        expires_at=NOW + timedelta(hours=1),
    )


def _plan():
    return build_restoration_plan(
        request=_request(),
        source_session_id="session-1",
        target_session_id="session-1",
        surface_kind=SurfaceKind.WEB,
        strategy=RestorationStrategy.ORIGINAL_SURFACE_PIN,
        deadline=NOW + timedelta(minutes=30),
    )


def _snapshot(*, capture_id: str, captured_at: datetime):
    return StateInvariantSnapshot(
        task_id=TASK_ID,
        capture_id=capture_id,
        session_id="session-1",
        surface_kind=SurfaceKind.WEB,
        environment={},
        business={"route": "/product/42"},
        semantic={},
        structure={},
        captured_at=captured_at,
    )


def _row(attempt, *, row_version: int) -> dict[str, Any]:
    return {
        "attempt_id": attempt.attempt_id,
        "plan_payload": attempt.plan.to_dict(),
        "attempt_no": attempt.attempt_no,
        "status": attempt.status.value,
        "restored_capture_id": attempt.restored_capture_id,
        "assessment_payload": (
            attempt.assessment.to_dict() if attempt.assessment is not None else None
        ),
        "action_execution_ids": list(attempt.action_execution_ids),
        "error_code": attempt.error_code,
        "error_message": attempt.error_message,
        "started_at": attempt.started_at,
        "finished_at": attempt.finished_at,
        "row_version": row_version,
        "attempt_protocol_version": attempt.protocol_version,
    }


async def test_restoration_repository_locks_identity_and_commits_terminal() -> None:
    plan = _plan()
    running = new_running_restoration_attempt(
        plan=plan,
        attempt_no=1,
        started_at=NOW,
    )
    assessment = StateEquivalenceGate(clock=lambda: NOW + timedelta(minutes=2)).assess(
        request=_request(),
        plan=plan,
        restoration_attempt_id=running.attempt_id,
        original=_snapshot(capture_id=ORIGINAL_CAPTURE_ID, captured_at=NOW),
        restored=_snapshot(
            capture_id=RESTORED_CAPTURE_ID,
            captured_at=NOW + timedelta(minutes=1),
        ),
    )
    terminal = replace(
        running,
        status=RestorationAttemptStatus.EQUIVALENT,
        restored_capture_id=RESTORED_CAPTURE_ID,
        assessment=assessment,
        action_execution_ids=("action-restore-1",),
        finished_at=NOW + timedelta(minutes=2),
    )
    connection = FakeConnection(
        fetchrow_results=[_row(running, row_version=0), _row(terminal, row_version=1)]
    )
    repository = PostgresRestorationAttemptRepository(pool=FakePool(connection))

    started = await repository.start(plan=plan, attempt_no=1)
    completed = await repository.complete(
        attempt=terminal,
        expected_row_version=started.row_version,
    )

    assert completed.row_version == 1
    assert completed.status is RestorationAttemptStatus.EQUIVALENT
    assert connection.transaction_entered == 2
    fetchrow_queries = [call[1] for call in connection.calls if call[0] == "fetchrow"]
    start_query = fetchrow_queries[0]
    complete_query = fetchrow_queries[1]
    assert "pg_advisory_xact_lock" in connection.calls[0][1]
    assert "request.status IN ('pending', 'routed', 'running')" in start_query
    assert "request.expires_at > now()" in start_query
    assert "replay_plan.task_id = task.id" in start_query
    assert "restoration.row_version = $11" in complete_query
    assert "restored_capture.task_id = restoration.task_id" in complete_query
    assert "$6::jsonb->>'source_grade' = restoration.source_grade" in complete_query


async def test_restoration_repository_reads_equivalent_attempt_for_current_lease() -> None:
    plan = _plan()
    running = new_running_restoration_attempt(
        plan=plan,
        attempt_no=1,
        started_at=NOW,
    )
    assessment = StateEquivalenceGate(clock=lambda: NOW + timedelta(minutes=2)).assess(
        request=_request(),
        plan=plan,
        restoration_attempt_id=running.attempt_id,
        original=_snapshot(capture_id=ORIGINAL_CAPTURE_ID, captured_at=NOW),
        restored=_snapshot(
            capture_id=RESTORED_CAPTURE_ID,
            captured_at=NOW + timedelta(minutes=1),
        ),
    )
    equivalent = replace(
        running,
        status=RestorationAttemptStatus.EQUIVALENT,
        restored_capture_id=RESTORED_CAPTURE_ID,
        assessment=assessment,
        action_execution_ids=("action-restore-1",),
        finished_at=NOW + timedelta(minutes=2),
    )
    connection = FakeConnection(fetchrow_results=[_row(equivalent, row_version=1)])
    repository = PostgresRestorationAttemptRepository(pool=FakePool(connection))
    fence = EvidenceRequestLeaseFence(
        request_id=_request().request_id,
        task_id=TASK_ID,
        finalization_epoch=3,
        lease_owner="worker-1:app-recapture",
        lease_token="00000000-0000-0000-0000-000000000008",
        row_version=4,
    )

    found = await repository.find_equivalent_for_lease(fence=fence)

    assert found == replace(equivalent, row_version=1)
    query = connection.calls[0][1]
    assert "restoration.status = 'equivalent'" in query
    assert "request.lease_owner = $4" in query
    assert "request.lease_token = $5::uuid" in query
    assert "request.row_version = $6" in query
    assert "request.lease_expires_at > now()" in query
    assert "restoration.finalization_epoch = $3" in query
    assert "restored_capture.capture_status = 'captured'" in query


async def test_restoration_repository_returns_none_without_reusable_attempt() -> None:
    connection = FakeConnection(fetchrow_results=[None])
    repository = PostgresRestorationAttemptRepository(pool=FakePool(connection))
    fence = EvidenceRequestLeaseFence(
        request_id=_request().request_id,
        task_id=TASK_ID,
        finalization_epoch=3,
        lease_owner="worker-1:app-recapture",
        lease_token="00000000-0000-0000-0000-000000000008",
        row_version=4,
    )

    assert await repository.find_equivalent_for_lease(fence=fence) is None


async def test_supplement_repository_requires_full_proof_chain_and_closes_request() -> None:
    plan = _plan()
    running = new_running_restoration_attempt(
        plan=plan,
        attempt_no=1,
        started_at=NOW,
    )
    assessment = StateEquivalenceGate(clock=lambda: NOW + timedelta(minutes=2)).assess(
        request=_request(),
        plan=plan,
        restoration_attempt_id=running.attempt_id,
        original=_snapshot(capture_id=ORIGINAL_CAPTURE_ID, captured_at=NOW),
        restored=_snapshot(
            capture_id=RESTORED_CAPTURE_ID,
            captured_at=NOW + timedelta(minutes=1),
        ),
    )
    probe_work_id = "probe-work:proof-1"
    manifest_id = "manifest:result-1"
    attacher_clock = NOW + timedelta(minutes=3)
    expected_created = {
        "supplement_id": "",
        "task_id": TASK_ID,
        "request_id": _request().request_id,
        "manifest_id": manifest_id,
        "probe_work_id": probe_work_id,
        "restoration_attempt_id": running.attempt_id,
        "equivalence_assessment_id": assessment.assessment_id,
        "source_grade": "A1",
        "origin": "replay_probe",
        "protocol_version": "evidence-supplement.v1",
        "created_at": attacher_clock,
        "created": True,
    }
    connection = FakeConnection(
        fetchrow_results=[expected_created],
        fetchval_results=[1, "message-1"],
    )
    enabled_policy = SupplementalEvidencePolicy(enabled=True, runtime_ready=True)
    repository = PostgresEvidenceSupplementRepository(
        pool=FakePool(connection),
        supplemental_evidence_policy=enabled_policy,
    )
    attacher = EvidenceSupplementAttacher(
        repository=repository,
        clock=lambda: attacher_clock,
        supplemental_evidence_policy=enabled_policy,
    )
    command = AttachEvidenceSupplementCommand(
        task_id=TASK_ID,
        request_id=_request().request_id,
        manifest_id=manifest_id,
        probe_work_id=probe_work_id,
        restoration_attempt_id=running.attempt_id,
        equivalence_assessment_id=assessment.assessment_id,
        source_grade=EvidenceSourceGrade.A1,
    )
    expected = build_evidence_supplement(
        task_id=command.task_id,
        request_id=command.request_id,
        manifest_id=command.manifest_id,
        probe_work_id=command.probe_work_id,
        restoration_attempt_id=command.restoration_attempt_id,
        equivalence_assessment_id=command.equivalence_assessment_id,
        source_grade=command.source_grade,
        created_at=attacher_clock,
    )
    expected_created["supplement_id"] = expected.supplement_id

    persisted = await attacher.attach(command)

    assert persisted.created is True
    assert persisted.supplement == expected
    assert connection.transaction_entered == 1
    queries = [call[1] for call in connection.calls]
    attach_query = queries[0]
    resolution_query = queries[1]
    outbox_query = queries[2]
    assert "WITH RECURSIVE locked_request" in attach_query
    assert "FOR UPDATE" in attach_query
    assert "probe.status = 'succeeded'" in attach_query
    assert "restoration.status = 'equivalent'" in attach_query
    assert "probe.work_payload->>'surface_kind' = 'web'" in attach_query
    assert "probe.created_at >= restoration.updated_at" in attach_query
    assert "probe.created_at >= restoration.finished_at" in attach_query
    assert "probe_action.status = 'completed'" in attach_query
    assert "'{parameters,exploration_work_id}' = probe.work_id" in attach_query
    assert "probe.work_payload->>'surface_kind' IN ('android', 'ios')" in attach_query
    assert "capture_role' = 'after_action'" in attach_query
    assert "capture_lineage" in attach_query
    assert "required_artifact_types" in attach_query
    assert "artifact_type' = 'action_trace'" in attach_query
    assert "status = 'satisfied'" in resolution_query
    assert "INSERT INTO qc_outbox_event" in outbox_query
    outbox_args = connection.calls[2][2]
    assert outbox_args[2:5] == ("evidence_request", expected.request_id, 1)


async def test_supplement_attacher_is_blocked_before_repository_when_disabled() -> None:
    class RecordingRepository:
        def __init__(self) -> None:
            self.calls = []

        async def attach(self, *, supplement):
            self.calls.append(supplement)
            raise AssertionError("补采关闭时不应调用仓储")

    repository = RecordingRepository()
    attacher = EvidenceSupplementAttacher(repository=repository)
    command = AttachEvidenceSupplementCommand(
        task_id=TASK_ID,
        request_id="request-1",
        manifest_id="manifest-1",
        probe_work_id="probe-work-1",
        restoration_attempt_id="restoration-1",
        equivalence_assessment_id="equivalence-1",
        source_grade=EvidenceSourceGrade.A1,
    )

    with pytest.raises(SupplementalEvidenceDisabledError) as error:
        await attacher.attach(command)

    assert error.value.operation.value == "attach_supplement"
    assert repository.calls == []


async def test_supplement_repository_is_blocked_before_database_when_disabled() -> None:
    connection = FakeConnection()
    repository = PostgresEvidenceSupplementRepository(pool=FakePool(connection))
    supplement = build_evidence_supplement(
        task_id=TASK_ID,
        request_id="request-1",
        manifest_id="manifest-1",
        probe_work_id="probe-work-1",
        restoration_attempt_id="restoration-1",
        equivalence_assessment_id="equivalence-1",
        source_grade=EvidenceSourceGrade.A1,
        created_at=NOW,
    )

    with pytest.raises(SupplementalEvidenceDisabledError):
        await repository.attach(supplement=supplement)

    assert connection.calls == []
    assert connection.transaction_entered == 0
