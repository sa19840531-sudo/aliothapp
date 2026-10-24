"""Evidence Exit Gate PostgreSQL 锁、租约、幂等与终结 SQL 契约测试。"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from services.api.app.application.evidence_exit_gate import EvidenceExitGateBlockedError
from services.api.app.application.surface_sessions import SessionCommandPermit
from services.api.app.domain.exploration import (
    EvidenceExitRequirementStatus,
    EvidenceExitReservationStatus,
    build_active_session_evidence_requirement,
    build_evidence_exit_requirement_set,
)
from services.api.app.domain.interaction import (
    ActionIntent,
    CaptureDirective,
    CaptureProfile,
    InteractionOperation,
    SurfaceKind,
    build_action_execution_id,
    build_action_intent_hash,
)
from services.api.app.infrastructure.db.evidence_exit_gate_repository import (
    EvidenceExitGatePersistenceError,
    PostgresEvidenceExitGate,
    PostgresEvidenceExitRequirementRepository,
)

TASK_ID = "11111111-1111-1111-1111-111111111111"
RUN_ID = "33333333-3333-3333-3333-333333333333"
CAPTURE_ID = "22222222-2222-2222-2222-222222222222"
NOW = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)


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


class GateConnection:
    def __init__(
        self,
        *,
        gate_row: dict[str, Any],
        reservation_live: bool = True,
        insert_conflict: bool = False,
        corrupt_outcome: bool = False,
    ) -> None:
        self.gate_row = gate_row
        self.reservation_live = reservation_live
        self.calls: list[tuple[str, str, tuple[Any, ...]]] = []
        self.finalize_status: str | None = None
        self.insert_conflict = insert_conflict
        self.corrupt_outcome = corrupt_outcome
        self.persisted_evaluation: dict[str, Any] | None = None

    def transaction(self) -> FakeTransaction:
        return FakeTransaction()

    async def execute(self, query: str, *args: Any) -> str:
        self.calls.append(("execute", " ".join(query.split()), args))
        return "OK"

    async def fetchrow(self, query: str, *args: Any):
        normalized = " ".join(query.split())
        self.calls.append(("fetchrow", normalized, args))
        if "WITH target AS" in normalized:
            return self.gate_row
        if "INSERT INTO qc_evidence_exit_gate_evaluation" in normalized:
            self.persisted_evaluation = {
                "decision_payload": args[15],
                "outcome_payload": args[21],
                "reservation_status": args[22],
                "expires_at": args[23],
                "reservation_live": self.reservation_live if args[12] else None,
                "manifest_id": args[16],
                "manifest_version": args[17],
                "manifest_hash": args[18],
                "manifest_quality_status": args[19],
                "business_outcome": args[20],
            }
            if self.corrupt_outcome and self.persisted_evaluation["outcome_payload"]:
                outcome = json.loads(self.persisted_evaluation["outcome_payload"])
                outcome["error_code"] = "CORRUPTED_RETRYABLE_CODE"
                outcome["retryable"] = True
                self.persisted_evaluation["outcome_payload"] = json.dumps(outcome)
            return None if self.insert_conflict else self.persisted_evaluation
        if "FROM qc_evidence_exit_gate_evaluation" in normalized:
            return self.persisted_evaluation
        raise AssertionError(f"未处理的 fetchrow SQL：{normalized}")

    async def fetchval(self, query: str, *args: Any):
        normalized = " ".join(query.split())
        self.calls.append(("fetchval", normalized, args))
        if "WITH finalized AS" in normalized:
            return 1
        if "UPDATE qc_evidence_exit_gate_evaluation" in normalized:
            self.finalize_status = args[1]
            return args[1]
        raise AssertionError(f"未处理的 fetchval SQL：{normalized}")


class RequirementConnection:
    def __init__(self, *, previous_payload=None) -> None:
        self.calls: list[tuple[str, str, tuple[Any, ...]]] = []
        self.previous_payload = previous_payload

    def transaction(self) -> FakeTransaction:
        return FakeTransaction()

    async def execute(self, query: str, *args: Any) -> str:
        self.calls.append(("execute", " ".join(query.split()), args))
        return "OK"

    async def fetchrow(self, query: str, *args: Any):
        normalized = " ".join(query.split())
        self.calls.append(("fetchrow", normalized, args))
        if "SELECT revision, requirement_payload" in normalized:
            if self.previous_payload is None:
                return None
            return {
                "revision": self.previous_payload["revision"],
                "requirement_payload": self.previous_payload,
            }
        return {"requirement_set_id": args[0], "created": True}


def _gate_row(**overrides) -> dict[str, Any]:
    row = {
        "task_id": TASK_ID,
        "session_id": "session-1",
        "source_capture_id": CAPTURE_ID,
        "requirement_set_id": "requirements-1",
        "manifest_id": "evidence-manifest:manifest-1",
        "manifest_version": 1,
        "manifest_hash": "a" * 64,
        "manifest_quality_status": "pass",
        "manifest_quality_reason": None,
        "baseline_pass_manifest_count": 1,
        "nonterminal_active_session_requirement_count": 0,
        "nonterminal_inline_evidence_request_count": 0,
        "evidence_commit_in_progress_count": 0,
        "evaluated_at": NOW,
        "lineage_integrity_failed": False,
    }
    row.update(overrides)
    return row


def _intent(
    *,
    deadline: datetime | None = None,
    surface_kind: SurfaceKind = SurfaceKind.WEB,
) -> ActionIntent:
    return ActionIntent(
        task_id=TASK_ID,
        session_id="session-1",
        intent_id="intent-1",
        idempotency_key="task:branch:intent-1",
        surface_kind=surface_kind,
        source_capture_id=CAPTURE_ID,
        action_type=InteractionOperation.CLICK,
        semantic_target="登录按钮",
        purpose="进入登录页",
        capture_profile=CaptureProfile(
            profile_id="baseline",
            before=CaptureDirective(role="before", artifact_types=("screenshot",)),
            after=CaptureDirective(role="after", artifact_types=("screenshot",)),
        ),
        deadline=deadline or NOW + timedelta(minutes=1),
    )


def _permit(*, surface_kind: SurfaceKind = SurfaceKind.WEB) -> SessionCommandPermit:
    return SessionCommandPermit(
        task_id=TASK_ID,
        session_id="session-1",
        surface_kind=surface_kind,
        lease_owner="worker-1",
        lease_token="33333333-3333-3333-3333-333333333333",
        command_sequence=3,
        finalization_epoch=1,
        lease_expires_at=NOW + timedelta(minutes=2),
    )


async def test_gate_reserves_under_capture_lock_and_exact_session_lease() -> None:
    connection = GateConnection(gate_row=_gate_row())
    repository = PostgresEvidenceExitGate(pool=FakePool(connection))

    decision = await repository.reserve(intent=_intent(), permit=_permit())

    assert decision is not None and decision.allow_leave is True
    queries = [call[1] for call in connection.calls]
    assert "pg_advisory_xact_lock" in queries[0]
    evaluation_query = next(query for query in queries if "WITH target AS" in query)
    assert "session.current_capture_id = capture.id" in evaluation_query
    assert "session.lease_token = $6::uuid" in evaluation_query
    assert "session.finalization_epoch = $8" in evaluation_query
    assert "request.status IN ('pending', 'routed', 'running')" in evaluation_query
    assert "manifest.manifest_id" in evaluation_query
    assert "manifest.manifest_version" in evaluation_query
    assert "manifest.manifest_hash" in evaluation_query
    assert "manifest.quality_reason" in evaluation_query
    assert "lineage_integrity_failed" in evaluation_query
    assert "manifest.manifest_payload" in evaluation_query
    assert "jsonb_array_length" in evaluation_query
    assert "artifact.artifact_type" in evaluation_query
    assert "NOT manifest_integrity.lineage_integrity_failed" in evaluation_query
    insert_query = next(
        query for query in queries if "INSERT INTO qc_evidence_exit_gate_evaluation" in query
    )
    assert "(expires_at > now()) AS reservation_live" in insert_query

    await repository.finalize(
        decision=decision,
        status=EvidenceExitReservationStatus.CONSUMED,
    )

    assert connection.finalize_status == "consumed"


async def test_gate_action_recovery_passes_before_capture_excludes_own_pending_and_audits_id(
) -> None:
    before_capture_id = "44444444-4444-4444-4444-444444444444"
    action_execution_id = "action-recovery-1"
    connection = GateConnection(gate_row=_gate_row())
    repository = PostgresEvidenceExitGate(pool=FakePool(connection))

    decision = await repository.reserve_action_recovery(
        intent=_intent(surface_kind=SurfaceKind.ANDROID),
        permit=_permit(surface_kind=SurfaceKind.ANDROID),
        action_execution_id=action_execution_id,
        before_capture_id=before_capture_id,
    )

    assert decision is not None and decision.allow_leave is True
    assert decision.action_intent_id.startswith(f"app-recovery:{action_execution_id}:")
    lock_calls = [call for call in connection.calls if call[0] == "execute"]
    assert len(lock_calls) == 2
    assert lock_calls[0][2] == (f"evidence-exit:{TASK_ID}:{CAPTURE_ID}",)
    assert lock_calls[1][2] == (f"evidence-exit:{TASK_ID}:{before_capture_id}",)
    evaluation_call = next(call for call in connection.calls if "WITH target AS" in call[1])
    evaluation_query = evaluation_call[1]
    assert evaluation_call[2][-2:] == (action_execution_id, before_capture_id)
    assert "session.current_capture_id = $10::uuid" in evaluation_query
    assert "recovery_before.action_execution_id = $9" in evaluation_query
    assert "action.action_execution_id <> $9" in evaluation_query
    insert_call = next(
        call
        for call in connection.calls
        if "INSERT INTO qc_evidence_exit_gate_evaluation" in call[1]
    )
    assert insert_call[2][4] == decision.action_intent_id
    decision_payload = json.loads(insert_call[2][15])
    assert decision_payload["action_intent_id"] == decision.action_intent_id


async def test_app_gate_allows_pass_manifest_without_requirement_snapshot() -> None:
    """App 的 PASS Manifest 不要求额外伪造 requirement snapshot。"""

    connection = GateConnection(
        gate_row=_gate_row(requirement_set_id=None),
    )
    repository = PostgresEvidenceExitGate(pool=FakePool(connection))

    decision = await repository.reserve(
        intent=_intent(surface_kind=SurfaceKind.ANDROID),
        permit=_permit(surface_kind=SurfaceKind.ANDROID),
    )

    assert decision is not None and decision.allow_leave is True
    insert_call = next(
        call
        for call in connection.calls
        if "INSERT INTO qc_evidence_exit_gate_evaluation" in call[1]
    )
    snapshot_payload = json.loads(insert_call[2][14])
    assert snapshot_payload["surface_kind"] == "android"
    assert snapshot_payload["requirement_set_id"] is None
    assert insert_call[2][5] is None


async def test_gate_finalizes_stale_app_action_reservation_under_current_session_lease(
) -> None:
    intent = _intent(surface_kind=SurfaceKind.ANDROID)
    permit = _permit(surface_kind=SurfaceKind.ANDROID)
    connection = GateConnection(gate_row=_gate_row())
    repository = PostgresEvidenceExitGate(pool=FakePool(connection))

    count = await repository.finalize_stale_action_reservations(
        intent=intent,
        permit=permit,
        action_execution_id=build_action_execution_id(intent),
        status=EvidenceExitReservationStatus.CONSUMED,
    )

    assert count == 1
    call = next(call for call in connection.calls if "WITH finalized AS" in call[1])
    assert call[2] == (
        build_action_execution_id(intent),
        intent.task_id,
        intent.session_id,
        intent.intent_id,
        build_action_intent_hash(intent),
        EvidenceExitReservationStatus.CONSUMED.value,
        SurfaceKind.ANDROID.value,
        permit.lease_owner,
        permit.lease_token,
        permit.command_sequence,
        permit.finalization_epoch,
    )
    assert "evaluation.action_intent_id = action.intent_id" in call[1]
    assert (
        "'app-recovery:' || action.action_execution_id || ':%'" in call[1]
    )
    assert "session.lease_token = $9::uuid" in call[1]


async def test_gate_persists_blocked_audit_before_raising() -> None:
    connection = GateConnection(
        gate_row=_gate_row(
            baseline_pass_manifest_count=0,
            manifest_quality_status="fail",
            manifest_quality_reason="缺少可裁决证据",
        )
    )
    repository = PostgresEvidenceExitGate(pool=FakePool(connection))

    with pytest.raises(EvidenceExitGateBlockedError) as error:
        await repository.reserve(intent=_intent(), permit=_permit())

    assert error.value.decision.allow_leave is False
    assert error.value.error_code == "EVIDENCE_INSUFFICIENT"
    assert error.value.retryable is False
    insert_call = next(
        call
        for call in connection.calls
        if "INSERT INTO qc_evidence_exit_gate_evaluation" in call[1]
    )
    assert "manifest_id, manifest_version, manifest_hash" in insert_call[1]
    assert insert_call[2][16:21] == (
        "evidence-manifest:manifest-1",
        1,
        "a" * 64,
        "fail",
        "evidence_insufficient",
    )
    outcome_payload = json.loads(insert_call[2][21])
    assert outcome_payload["evaluation_id"] == error.value.decision.evaluation_id
    assert outcome_payload["outcome"] == "evidence_insufficient"
    assert insert_call[2][22] == "blocked"
    assert all(
        not (
            ("INSERT INTO qc_evidence_request" in call[1])
            or ("UPDATE qc_evidence_request" in call[1])
        )
        for call in connection.calls
    )


@pytest.mark.parametrize(
    ("row_overrides", "error_code", "retryable"),
    [
        (
            {
                "baseline_pass_manifest_count": 0,
                "manifest_quality_status": "pending",
                "evidence_commit_in_progress_count": 1,
            },
            "EVIDENCE_COMMIT_PENDING",
            True,
        ),
        (
            {
                "baseline_pass_manifest_count": 0,
                "manifest_id": None,
                "manifest_version": None,
                "manifest_hash": None,
                "manifest_quality_status": None,
                "manifest_quality_reason": None,
            },
            "EVIDENCE_CONTROL_PLANE_INCOMPLETE",
            True,
        ),
    ],
)
async def test_gate_persists_retryable_blocked_outcomes(
    row_overrides,
    error_code,
    retryable,
) -> None:
    connection = GateConnection(gate_row=_gate_row(**row_overrides))
    repository = PostgresEvidenceExitGate(pool=FakePool(connection))

    with pytest.raises(EvidenceExitGateBlockedError) as error:
        await repository.reserve(intent=_intent(), permit=_permit())

    assert error.value.error_code == error_code
    assert error.value.retryable is retryable


async def test_blocked_gate_idempotent_conflict_reloads_linked_outcome() -> None:
    connection = GateConnection(
        gate_row=_gate_row(
            baseline_pass_manifest_count=0,
            manifest_quality_status="fail",
            manifest_quality_reason="缺少证据",
        ),
        insert_conflict=True,
    )
    repository = PostgresEvidenceExitGate(pool=FakePool(connection))

    with pytest.raises(EvidenceExitGateBlockedError) as error:
        await repository.reserve(intent=_intent(), permit=_permit())

    assert error.value.outcome.evaluation_id == error.value.decision.evaluation_id
    assert any(
        "SELECT decision_payload, outcome_payload" in call[1]
        for call in connection.calls
    )


async def test_gate_rejects_corrupted_idempotent_outcome_payload() -> None:
    connection = GateConnection(
        gate_row=_gate_row(
            baseline_pass_manifest_count=0,
            manifest_quality_status="fail",
            manifest_quality_reason="缺少证据",
        ),
        insert_conflict=True,
        corrupt_outcome=True,
    )
    repository = PostgresEvidenceExitGate(pool=FakePool(connection))

    with pytest.raises(EvidenceExitGatePersistenceError, match="业务结果.*不一致"):
        await repository.reserve(intent=_intent(), permit=_permit())


async def test_gate_classifies_manifest_lineage_conflict_as_integrity_failure() -> None:
    connection = GateConnection(
        gate_row=_gate_row(
            baseline_pass_manifest_count=0,
            manifest_quality_status="fail",
            manifest_quality_reason="缺少证据",
            lineage_integrity_failed=True,
        )
    )
    repository = PostgresEvidenceExitGate(pool=FakePool(connection))

    with pytest.raises(EvidenceExitGateBlockedError) as error:
        await repository.reserve(intent=_intent(), permit=_permit())

    assert error.value.outcome.outcome.value == "evidence_integrity_failed"
    assert error.value.error_code == "EVIDENCE_INTEGRITY_FAILED"
    assert error.value.retryable is False


async def test_gate_never_allows_pass_manifest_with_lineage_conflict() -> None:
    connection = GateConnection(
        gate_row=_gate_row(
            baseline_pass_manifest_count=1,
            manifest_quality_status="pass",
            lineage_integrity_failed=True,
        )
    )
    repository = PostgresEvidenceExitGate(pool=FakePool(connection))

    with pytest.raises(EvidenceExitGateBlockedError) as error:
        await repository.reserve(intent=_intent(), permit=_permit())

    assert error.value.outcome.outcome.value == "evidence_integrity_failed"
    assert error.value.error_code == "EVIDENCE_INTEGRITY_FAILED"


async def test_gate_rejects_expired_or_already_elapsed_reservation() -> None:
    expired_connection = GateConnection(gate_row=_gate_row(), reservation_live=False)
    repository = PostgresEvidenceExitGate(pool=FakePool(expired_connection))

    with pytest.raises(EvidenceExitGatePersistenceError, match="已过期"):
        await repository.reserve(intent=_intent(), permit=_permit())

    elapsed_connection = GateConnection(gate_row=_gate_row())
    repository = PostgresEvidenceExitGate(pool=FakePool(elapsed_connection))
    with pytest.raises(EvidenceExitGatePersistenceError, match="deadline 已到"):
        await repository.reserve(intent=_intent(deadline=NOW), permit=_permit())

    assert all(
        "INSERT INTO qc_evidence_exit_gate_evaluation" not in call[1]
        for call in elapsed_connection.calls
    )


async def test_requirement_snapshot_insert_is_locked_and_payload_counted() -> None:
    requirement_set = build_evidence_exit_requirement_set(
        revision=1,
        task_id=TASK_ID,
        session_id="session-1",
        capture_id=CAPTURE_ID,
        matcher_snapshot_hash="sha256:matcher",
        requirements=(),
        created_at=NOW,
    )
    connection = RequirementConnection()
    repository = PostgresEvidenceExitRequirementRepository(pool=FakePool(connection))

    persisted = await repository.add(requirement_set=requirement_set)

    assert persisted.created is True
    assert "pg_advisory_xact_lock" in connection.calls[0][1]
    insert_call = connection.calls[2]
    assert "capture.capture_status = 'captured'" in insert_call[1]
    assert "task.status = 'running'" in insert_call[1]
    assert insert_call[2][8:10] == (0, 0)
    payload = json.loads(insert_call[2][10])
    assert payload["requirements"] == []


async def test_requirement_same_semantic_revision_is_idempotent_across_created_at() -> None:
    previous = build_evidence_exit_requirement_set(
        revision=1,
        task_id=TASK_ID,
        session_id="session-1",
        capture_id=CAPTURE_ID,
        matcher_snapshot_hash="sha256:matcher",
        requirements=(),
        created_at=NOW,
    )
    replay = build_evidence_exit_requirement_set(
        revision=1,
        task_id=TASK_ID,
        session_id="session-1",
        capture_id=CAPTURE_ID,
        matcher_snapshot_hash="sha256:matcher",
        requirements=(),
        created_at=NOW + timedelta(milliseconds=1),
    )
    assert replay.requirement_set_id == previous.requirement_set_id

    connection = RequirementConnection(previous_payload=previous.to_dict())
    repository = PostgresEvidenceExitRequirementRepository(pool=FakePool(connection))

    persisted = await repository.add(requirement_set=replay)

    assert persisted.created is False
    assert persisted.requirement_set == previous
    assert all(
        "INSERT INTO qc_evidence_exit_requirement_set" not in call[1]
        for call in connection.calls
    )


async def test_requirement_snapshot_accepts_agent_run_session_for_unbound_web_capture() -> None:
    requirement_set = build_evidence_exit_requirement_set(
        revision=1,
        task_id=TASK_ID,
        session_id=f"agent-run:{RUN_ID}",
        capture_id=CAPTURE_ID,
        matcher_snapshot_hash="sha256:matcher",
        requirements=(),
        created_at=NOW,
    )
    connection = RequirementConnection()
    repository = PostgresEvidenceExitRequirementRepository(pool=FakePool(connection))

    persisted = await repository.add(requirement_set=requirement_set)

    assert persisted.created is True
    insert_call = connection.calls[2]
    assert "LEFT JOIN qc_surface_session AS session" in insert_call[1]
    assert "capture.surface_session_id IS NULL" in insert_call[1]
    assert "$4::varchar = ('agent-run:' || capture.agent_run_id::text)" in insert_call[1]
    assert insert_call[2][3] == f"agent-run:{RUN_ID}"


async def test_requirement_revision_cannot_remove_pending_member() -> None:
    pending = build_active_session_evidence_requirement(
        task_id=TASK_ID,
        capture_id=CAPTURE_ID,
        rule_id="rule-1",
        rule_code="LOGIN_VISIBLE",
        required_artifact_types=("screenshot",),
        required_capture_roles=(),
        status=EvidenceExitRequirementStatus.PENDING,
    )
    previous = build_evidence_exit_requirement_set(
        revision=1,
        task_id=TASK_ID,
        session_id="session-1",
        capture_id=CAPTURE_ID,
        matcher_snapshot_hash="sha256:matcher-1",
        requirements=(pending,),
        created_at=NOW,
    )
    current = build_evidence_exit_requirement_set(
        revision=2,
        task_id=TASK_ID,
        session_id="session-1",
        capture_id=CAPTURE_ID,
        matcher_snapshot_hash="sha256:matcher-2",
        requirements=(),
        created_at=NOW + timedelta(seconds=1),
    )
    connection = RequirementConnection(previous_payload=previous.to_dict())
    repository = PostgresEvidenceExitRequirementRepository(pool=FakePool(connection))

    with pytest.raises(EvidenceExitGatePersistenceError, match="不能.*删除"):
        await repository.add(requirement_set=current)

    assert all(
        "INSERT INTO qc_evidence_exit_requirement_set" not in call[1] for call in connection.calls
    )
