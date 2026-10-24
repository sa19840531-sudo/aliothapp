"""V2 PostgreSQL 控制面的事务、租约、幂等与归属 SQL 契约测试。"""

from __future__ import annotations

import inspect
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from services.api.app.application.evidence_requests import (
    EvidenceRequestClaimDisposition,
    EvidenceRequestLeaseFence,
    EvidenceRequestLeaseReleaseDisposition,
    EvidenceRequestStatus,
    ReleaseEvidenceRequestLeaseCommand,
    SettleEvidenceRequestLeaseCommand,
    TransitionEvidenceRequestCommand,
)
from services.api.app.application.exploration_work_queue import (
    AppFrontierSettlement,
    ClaimExplorerWorkCommand,
    CommitExplorerWorkSuccessorCommand,
    ExplorerWorkStatus,
    ExplorerWorkType,
    RenewExplorerWorkLeaseCommand,
    TransitionExplorerWorkCommand,
    UpdateExplorerWorkPayloadCommand,
)
from services.api.app.application.h5_exploration_recovery import (
    H5BrowserRecoveryAttemptFacts,
    H5BrowserRecoveryFacts,
    H5FailureStage,
    build_h5_recovery_session_id,
)
from services.api.app.application.journey_execution import CommitJourneyStepCommand
from services.api.app.application.outbox import (
    SUPPORTED_WAKEUP_MESSAGE_TYPES,
    TASK_FINALIZATION_PRIORITY_WAKEUP_MESSAGE_TYPES,
)
from services.api.app.application.supplemental_evidence_policy import (
    SupplementalEvidenceDisabledError,
    SupplementalEvidencePolicy,
)
from services.api.app.domain.evidence import (
    ArtifactReference,
    EvidenceQualityStatus,
    build_evidence_manifest_reference,
)
from services.api.app.domain.exploration import (
    AppFrontierStatus,
    CandidateAction,
    ExplorationBoundaries,
    ExplorationBudget,
    ExplorationTerminal,
    ExplorationTerminalStatus,
    FrontierSnapshot,
    SideEffectPolicy,
    SurfaceObservation,
    build_exploration_work,
    build_probe_work,
)
from services.api.app.domain.exploration.h5_frontier import H5TaskFrontierSnapshot
from services.api.app.domain.inspection import (
    EvidenceReplayPolicy,
    EvidenceRequiredEquivalence,
    EvidenceSessionDependency,
    EvidenceSideEffectPolicy,
    EvidenceSourceGrade,
    build_evidence_request,
)
from services.api.app.domain.interaction import (
    InteractionOperation,
    InteractionRiskLevel,
    SurfaceKind,
)
from services.api.app.domain.journey import (
    JourneyExecutionSpec,
    JourneyExecutionStatus,
    JourneyStepAttempt,
    JourneyStepAttemptStatus,
    build_journey_execution_id,
    build_journey_execution_plan,
    build_journey_step_attempt_id,
    parse_journey_definition,
)
from services.api.app.domain.protocol import ProtocolContractError
from services.api.app.infrastructure.db.evidence_manifest_repository import (
    PostgresEvidenceManifestRepository,
)
from services.api.app.infrastructure.db.evidence_request_repository import (
    PostgresEvidenceRequestRepository,
)
from services.api.app.infrastructure.db.explorer_work_repository import (
    PostgresExplorationWorkPublisher,
    PostgresExplorerWorkRepository,
    PostgresProbeWorkPublisher,
    _validate_work_transition,
)
from services.api.app.infrastructure.db.journey_definition_repository import (
    PostgresJourneyDefinitionRepository,
)
from services.api.app.infrastructure.db.journey_execution_repository import (
    PostgresJourneyExecutionRepository,
)
from services.api.app.infrastructure.db.outbox_repository import (
    PostgresOutboxRepository,
)

TASK_ID = "00000000-0000-0000-0000-000000000001"
CAPTURE_ID = "00000000-0000-0000-0000-000000000002"
RESTORED_CAPTURE_ID = "00000000-0000-0000-0000-000000000012"
WORK_ITEM_ID = "00000000-0000-0000-0000-000000000003"
ATTEMPT_ID = "00000000-0000-0000-0000-000000000004"


def test_exploration_publish_validates_common_capture_identity() -> None:
    source = inspect.getsource(PostgresExplorerWorkRepository.publish)

    assert "INNER JOIN qc_capture AS capture" in source
    assert "INNER JOIN qc_page_capture AS capture" not in source


RULE_ID = "00000000-0000-0000-0000-000000000005"
ARTIFACT_ID = "00000000-0000-0000-0000-000000000006"
PACKET_ID = "00000000-0000-0000-0000-000000000007"
LEASE_TOKEN = "00000000-0000-0000-0000-000000000008"
AGENT_RUN_ID = "00000000-0000-0000-0000-000000000009"
NOW = datetime.now(UTC)


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
        return "UPDATE 0"


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


def _budget() -> ExplorationBudget:
    return ExplorationBudget(
        max_actions=5,
        remaining_actions=5,
        max_depth=3,
        remaining_duration_ms=30_000,
    )


def _exploration_work():
    observation = SurfaceObservation(
        capture_id=CAPTURE_ID,
        surface_kind=SurfaceKind.WEB,
        location="https://example.com/",
        state_version="state-1",
        observed_at=NOW,
        page_type="home",
    )
    frontier = FrontierSnapshot(
        snapshot_id="frontier-1",
        observation=observation,
        candidates=(
            CandidateAction(
                candidate_id="candidate-1",
                semantic_target="登录按钮",
                action_type=InteractionOperation.CLICK,
                risk_level=InteractionRiskLevel.LOW,
                exploration_value=80,
                navigation_depth=1,
            ),
        ),
    )
    return build_exploration_work(
        task_id=TASK_ID,
        session_id="session-1",
        branch_id="branch-1",
        objective="进入登录页",
        surface_kind=SurfaceKind.WEB,
        source_capture_id=CAPTURE_ID,
        frontier_snapshot_id=frontier.snapshot_id,
        frontier=frontier,
        boundaries=ExplorationBoundaries(
            allowed_hosts=("example.com",),
            allowed_operations=(InteractionOperation.CLICK,),
            max_navigation_depth=3,
            side_effect_policy=SideEffectPolicy.READ_ONLY,
        ),
        budget=_budget(),
        # 整包 backend-test 可能跑超过 30～40 分钟；deadline 必须相对调用时而非模块导入时。
        deadline=datetime.now(UTC) + timedelta(hours=1),
    )


def _successor_work():
    parent = _exploration_work()
    observation = SurfaceObservation(
        capture_id=RESTORED_CAPTURE_ID,
        surface_kind=parent.surface_kind,
        location="https://example.com/home",
        state_version="state-2",
        observed_at=datetime.now(UTC) + timedelta(milliseconds=10),
        page_type="home",
        metadata={
            "parent_work_id": parent.work_id,
            "parent_branch_id": parent.branch_id,
            "parent_capture_id": parent.source_capture_id,
        },
    )
    frontier = FrontierSnapshot(
        snapshot_id="frontier-2",
        observation=observation,
        candidates=(),
        visited_state_ids=(parent.frontier.observation.state_version,),
    )
    return build_exploration_work(
        task_id=parent.task_id,
        session_id=parent.session_id,
        branch_id="branch-2",
        objective=parent.objective,
        surface_kind=parent.surface_kind,
        source_capture_id=RESTORED_CAPTURE_ID,
        frontier_snapshot_id=frontier.snapshot_id,
        frontier=frontier,
        boundaries=parent.boundaries,
        budget=ExplorationBudget(
            max_actions=parent.budget.max_actions,
            remaining_actions=parent.budget.remaining_actions - 1,
            max_depth=parent.budget.max_depth,
            remaining_duration_ms=parent.budget.remaining_duration_ms - 10,
        ),
        deadline=parent.deadline,
    )


def _probe_work():
    now = datetime.now(UTC)
    request = build_evidence_request(
        work_item_id=WORK_ITEM_ID,
        attempt_id=ATTEMPT_ID,
        task_id=TASK_ID,
        rule_id=RULE_ID,
        rule_code="alioth_1",
        target_capture_id=CAPTURE_ID,
        reason_code="TRUSTED_EVIDENCE_INVALID",
        evidence_goal="补采动作后截图",
        required_artifact_types=("screenshot",),
        session_dependency=EvidenceSessionDependency.ACTIVE_SESSION,
        replay_policy=EvidenceReplayPolicy.SAME_SURFACE_ONLY,
        required_equivalence=EvidenceRequiredEquivalence.SAME_SURFACE_CONTINUATION,
        side_effect_policy=EvidenceSideEffectPolicy.READ_ONLY,
        target_invariants={"route": "/"},
        minimum_source_grade=EvidenceSourceGrade.A1,
        expires_at=now + timedelta(hours=1),
    )
    return build_probe_work(
        session_id="session-1",
        branch_id="probe-1",
        surface_kind=SurfaceKind.WEB,
        source_capture_id=RESTORED_CAPTURE_ID,
        restoration_plan_hash="restoration-plan-hash",
        restoration_attempt_id="restoration-attempt-1",
        equivalence_assessment_id="state-equivalence-1",
        source_grade=EvidenceSourceGrade.A1,
        evidence_request=request,
        budget=_budget(),
        # 不可用模块级冻结 NOW+30m：整包用例跑超时后 publish 会被 deadline 门禁拒绝。
        deadline=now + timedelta(hours=1),
    )


def _manifest():
    return build_evidence_manifest_reference(
        task_id=TASK_ID,
        session_id="session-1",
        capture_id=CAPTURE_ID,
        manifest_version=1,
        artifacts=(
            ArtifactReference(
                artifact_id=ARTIFACT_ID,
                artifact_type="screenshot",
                role="primary",
                storage_path="evidence/screenshot.png",
                checksum="sha256:abc",
                size_bytes=128,
                content_type="image/png",
            ),
        ),
        quality_status=EvidenceQualityStatus.PASS,
        observation_packet_ids=(PACKET_ID,),
        created_at=NOW,
    )


def _journey_definition():
    return parse_journey_definition(
        {
            "schema_version": "journey-dsl.v1",
            "definition_id": "login",
            "version": 1,
            "name": "登录旅程",
            "surface_kinds": ["web"],
            "steps": [
                {
                    "id": "open-login",
                    "type": "action",
                    "operation": "click",
                    "semantic_target": "登录按钮",
                }
            ],
        }
    )


async def test_journey_publish_serializes_version_and_plan_locks_hash() -> None:
    definition = _journey_definition()
    connection = FakeConnection(fetchval_results=[True, True])
    repository = PostgresJourneyDefinitionRepository(pool=FakePool(connection))

    assert await repository.add(definition=definition) is True
    plan = build_journey_execution_plan(definition, task_id=TASK_ID)
    assert await repository.add_execution_plan(plan=plan) is True

    assert connection.transaction_entered == 2
    advisory_query = connection.calls[0][1]
    definition_query = connection.calls[1][1]
    plan_query = connection.calls[2][1]
    assert "pg_advisory_xact_lock" in advisory_query
    assert "version >= $2" in definition_query
    assert "ON CONFLICT DO NOTHING" in definition_query
    assert "definition.content_hash = $5" in plan_query
    assert "definition.schema_version = $6" in plan_query
    assert "WHERE task.id = $2::uuid" in plan_query


async def test_exploration_publish_writes_work_and_outbox_in_one_transaction() -> None:
    connection = FakeConnection(
        fetchrow_results=[{"status": "pending", "created": True}],
        fetchval_results=["message-1"],
    )
    repository = PostgresExplorerWorkRepository(pool=FakePool(connection))

    published = await repository.publish(_exploration_work())

    assert published.created is True
    assert connection.transaction_entered == 1
    work_query = next(call[1] for call in connection.calls if call[0] == "fetchrow")
    outbox_query = next(call[1] for call in connection.calls if call[0] == "fetchval")
    assert "capture.task_id = task.id" in work_query
    assert "capture.capture_status = 'captured'" in work_query
    assert "existing.payload_hash = EXCLUDED.payload_hash" in work_query
    assert "INSERT INTO qc_outbox_event" in outbox_query
    assert "aggregate_version" in outbox_query
    assert "ON CONFLICT DO NOTHING" in outbox_query
    outbox_args = next(
        call[2] for call in connection.calls if "INSERT INTO qc_outbox_event" in call[1]
    )
    assert outbox_args[2:5] == ("explorer_work", published.work_id, 0)


async def test_exploration_publish_rejects_oversized_payload_before_database_write() -> None:
    """写入端必须拒绝读取端无法解析的 Work，避免形成延迟故障。"""
    work = _exploration_work()
    oversized_candidate = replace(
        work.frontier.candidates[0],
        metadata={"candidate_context": "y" * 140_000},
    )
    oversized = replace(
        work,
        frontier=replace(
            work.frontier,
            observation=replace(
                work.frontier.observation,
                metadata={"observation_context": "x" * 140_000},
            ),
            candidates=(oversized_candidate,),
        ),
    )
    connection = FakeConnection()
    repository = PostgresExplorerWorkRepository(pool=FakePool(connection))

    with pytest.raises(ProtocolContractError, match="ExplorationWork 超出 256000 字节上限"):
        await repository.publish(oversized)

    assert connection.calls == []


async def test_exploration_publish_can_persist_work_without_outbox_wakeup() -> None:
    connection = FakeConnection(
        fetchrow_results=[{"status": "pending", "created": True}],
    )
    repository = PostgresExplorerWorkRepository(pool=FakePool(connection))

    published = await repository.publish(_exploration_work(), emit_wakeup=False)

    assert published.created is True
    assert any("INSERT INTO qc_v2_work_item" in query for _, query, _ in connection.calls)
    assert not any("INSERT INTO qc_outbox_event" in query for _, query, _ in connection.calls)


async def test_idempotent_exploration_replay_without_wakeup_still_skips_outbox() -> None:
    connection = FakeConnection(
        fetchrow_results=[{"status": "pending", "created": False}],
    )
    repository = PostgresExplorerWorkRepository(pool=FakePool(connection))

    published = await repository.publish(_exploration_work(), emit_wakeup=False)

    assert published.created is False
    assert any("INSERT INTO qc_v2_work_item" in query for _, query, _ in connection.calls)
    assert not any("INSERT INTO qc_outbox_event" in query for _, query, _ in connection.calls)


async def test_idempotent_exploration_replay_keeps_default_outbox_upsert() -> None:
    connection = FakeConnection(
        fetchrow_results=[{"status": "pending", "created": False}],
        fetchval_results=["message-1"],
    )
    repository = PostgresExplorerWorkRepository(pool=FakePool(connection))

    published = await repository.publish(_exploration_work())

    assert published.created is False
    assert any("INSERT INTO qc_v2_work_item" in query for _, query, _ in connection.calls)
    assert any("INSERT INTO qc_outbox_event" in query for _, query, _ in connection.calls)


async def test_probe_publish_persists_request_before_work_and_routes_atomically() -> None:
    connection = FakeConnection(
        fetchrow_results=[
            {"status": "pending"},
            {"status": "pending", "created": True},
        ],
        fetchval_results=["request-id", "message-id"],
    )
    repository = PostgresExplorerWorkRepository(
        pool=FakePool(connection),
        supplemental_evidence_policy=SupplementalEvidencePolicy(
            enabled=True,
            runtime_ready=True,
        ),
    )

    await repository.publish(_probe_work())

    queries = [call[1] for call in connection.calls]
    request_index = next(
        index for index, query in enumerate(queries) if "INSERT INTO qc_evidence_request" in query
    )
    work_index = next(
        index for index, query in enumerate(queries) if "INSERT INTO qc_v2_work_item" in query
    )
    route_index = next(
        index
        for index, query in enumerate(queries)
        if "UPDATE qc_evidence_request AS request" in query
    )
    outbox_index = next(
        index for index, query in enumerate(queries) if "INSERT INTO qc_outbox_event" in query
    )
    assert request_index < work_index < route_index < outbox_index
    assert any("pg_advisory_xact_lock" in query for query in queries[:request_index])
    lock_keys = [call[2][0] for call in connection.calls if "pg_advisory_xact_lock" in call[1]]
    assert lock_keys[0] == f"task-finalization:{TASK_ID}"
    assert lock_keys[-1] == f"evidence-exit:{TASK_ID}:{CAPTURE_ID}"
    assert "attempt.work_item_id = work_item.id" in queries[request_index]
    assert "attempt.status IN ('running', 'judged')" in queries[request_index]
    assert "member.rule_id = $8::uuid" in queries[request_index]
    assert "work_item.page_capture_id = $10::uuid" in queries[request_index]
    assert "work_item.control_mode = 'enforced'" in queries[request_index]
    assert "FROM qc_evidence_exit_gate_evaluation AS exit_gate" in queries[request_index]
    assert "exit_gate.reservation_status = 'reserved'" in queries[request_index]
    assert "restoration.status = 'equivalent'" in queries[work_index]
    assert "restoration.restored_capture_id = capture.id" in queries[work_index]
    assert "restoration.equivalence_assessment_id = $13" in queries[work_index]
    assert "restoration.plan_hash = $15" in queries[work_index]
    assert "restoration_attempt_id, equivalence_assessment_id" in queries[work_index]
    assert "WHEN status = 'pending' THEN 'routed'" in queries[route_index]
    assert "$2::uuid IS NULL AND request.lease_token IS NULL" in queries[route_index]
    assert "task.status = 'review'" in queries[work_index]
    assert "lower(task.target_platform) = 'h5'" in queries[work_index]
    assert "request.session_dependency = 'active_session'" in queries[work_index]


async def test_probe_publish_can_persist_request_and_work_without_outbox_wakeup() -> None:
    connection = FakeConnection(
        fetchrow_results=[
            {"status": "pending"},
            {"status": "pending", "created": True},
        ],
        fetchval_results=["request-id"],
    )
    repository = PostgresExplorerWorkRepository(
        pool=FakePool(connection),
        supplemental_evidence_policy=SupplementalEvidencePolicy(
            enabled=True,
            runtime_ready=True,
        ),
    )

    published = await repository.publish(_probe_work(), emit_wakeup=False)

    queries = [call[1] for call in connection.calls]
    assert published.created is True
    assert any("INSERT INTO qc_evidence_request" in query for query in queries)
    assert any("INSERT INTO qc_v2_work_item" in query for query in queries)
    assert any("WHEN status = 'pending' THEN 'routed'" in query for query in queries)
    assert not any("INSERT INTO qc_outbox_event" in query for query in queries)


@pytest.mark.parametrize(
    ("publisher_type", "work_factory"),
    (
        (PostgresExplorationWorkPublisher, _exploration_work),
        (PostgresProbeWorkPublisher, _probe_work),
    ),
)
async def test_postgres_work_publishers_forward_explicit_wakeup_policy(
    publisher_type,
    work_factory,
) -> None:
    calls: list[tuple[object, bool]] = []

    class RecordingRepository:
        async def publish(self, work, *, emit_wakeup: bool = True):
            calls.append((work, emit_wakeup))

    publisher = publisher_type(
        repository=RecordingRepository(),  # type: ignore[arg-type]
        emit_wakeup=False,
        **(
            {
                "supplemental_evidence_policy": SupplementalEvidencePolicy(
                    enabled=True,
                    runtime_ready=True,
                )
            }
            if publisher_type is PostgresProbeWorkPublisher
            else {}
        ),
    )
    work = work_factory()

    await publisher.publish(work)

    assert calls == [(work, False)]


async def test_probe_publish_is_blocked_before_database_when_policy_is_disabled() -> None:
    connection = FakeConnection()
    repository = PostgresExplorerWorkRepository(pool=FakePool(connection))

    with pytest.raises(SupplementalEvidenceDisabledError) as error:
        await repository.publish(_probe_work())

    assert error.value.operation.value == "publish_probe"
    assert connection.calls == []
    assert connection.transaction_entered == 0


async def test_probe_publisher_is_blocked_before_repository_when_policy_is_disabled() -> None:
    class RecordingRepository:
        def __init__(self) -> None:
            self.calls = []

        async def publish(self, work):
            self.calls.append(work)

    repository = RecordingRepository()
    publisher = PostgresProbeWorkPublisher(repository=repository)  # type: ignore[arg-type]

    with pytest.raises(SupplementalEvidenceDisabledError):
        await publisher.publish(_probe_work())

    assert repository.calls == []


async def test_evidence_request_only_accepts_original_or_proven_supplement_manifest() -> None:
    connection = FakeConnection(fetchval_results=[2])
    repository = PostgresEvidenceRequestRepository(pool=FakePool(connection))

    row_version = await repository.transition(
        command=TransitionEvidenceRequestCommand(
            request_id=_probe_work().evidence_request.request_id,
            expected_status=EvidenceRequestStatus.RUNNING,
            target_status=EvidenceRequestStatus.SATISFIED,
            row_version=1,
            resolution_manifest_id="manifest-1",
        )
    )

    assert row_version == 2
    query = connection.calls[0][1]
    assert "manifest.capture_id = request.target_capture_id" in query
    assert "FROM qc_evidence_supplement AS supplement" in query
    assert "supplement.request_id = request.request_id" in query
    assert "lease_owner = NULL" in query
    assert "lease_token = NULL" in query
    assert "lease_expires_at = NULL" in query


async def test_evidence_request_can_be_admitted_without_probe_work() -> None:
    connection = FakeConnection(fetchrow_results=[{"status": "pending"}])
    repository = PostgresEvidenceRequestRepository(pool=FakePool(connection))
    request = _probe_work().evidence_request

    await repository.upsert(request=request)

    assert connection.transaction_entered == 1
    queries = [call[1] for call in connection.calls]
    assert any("INSERT INTO qc_evidence_request" in query for query in queries)
    assert all("INSERT INTO qc_v2_work_item" not in query for query in queries)
    insert_query = next(
        query for query in queries if "INSERT INTO qc_evidence_request" in query
    )
    assert "existing.payload_hash = EXCLUDED.payload_hash" not in insert_query
    assert "existing.work_item_id = EXCLUDED.work_item_id" in insert_query
    assert "existing.target_capture_id = EXCLUDED.target_capture_id" in insert_query


async def test_claimed_probe_publish_routes_request_with_same_lease_fence() -> None:
    request = _probe_work().evidence_request
    connection = FakeConnection(
        fetchrow_results=[
            {"status": "pending"},
            {"status": "pending", "created": True},
        ],
        fetchval_results=[request.request_id],
    )
    repository = PostgresExplorerWorkRepository(
        pool=FakePool(connection),
        supplemental_evidence_policy=SupplementalEvidencePolicy(
            enabled=True,
            runtime_ready=True,
        ),
    )
    fence = EvidenceRequestLeaseFence(
        request_id=request.request_id,
        task_id=request.task_id,
        finalization_epoch=3,
        lease_owner="worker-1:app-recapture",
        lease_token=LEASE_TOKEN,
        row_version=2,
    )

    published = await repository.publish(
        _probe_work(),
        emit_wakeup=False,
        request_lease=fence,
    )

    assert published.created is True
    route_call = next(
        call
        for call in connection.calls
        if call[0] == "fetchval" and "UPDATE qc_evidence_request" in call[1]
    )
    query, args = route_call[1], route_call[2]
    assert "UPDATE qc_evidence_request AS request" in query
    assert "request.lease_token = $2::uuid" in query
    assert "request.lease_owner = $3" in query
    assert "request.finalization_epoch = $4" in query
    assert "request.row_version = $5" in query
    assert "request.lease_expires_at > now()" in query
    assert "request.task_id = $6::uuid" in query
    assert "lease_owner = NULL" in query
    assert "lease_token = NULL" in query
    assert args == (
        request.request_id,
        LEASE_TOKEN,
        fence.lease_owner,
        fence.finalization_epoch,
        fence.row_version,
        request.task_id,
    )
    assert connection.transaction_entered == 1


async def test_evidence_request_claim_is_atomic_and_run_scoped() -> None:
    request = _probe_work().evidence_request
    connection = FakeConnection(
        fetchrow_results=[
            {
                "request_payload": request.to_dict(),
                "lease_token": LEASE_TOKEN,
                "finalization_epoch": 3,
                "row_version": 2,
                "attempt_count": 1,
                "max_attempts": 3,
            }
        ]
    )
    repository = PostgresEvidenceRequestRepository(pool=FakePool(connection))

    result = await repository.claim_next(
        task_id=TASK_ID,
        agent_run_id=AGENT_RUN_ID,
        finalization_epoch=3,
        lease_owner="worker-1:app-recapture",
        lease_seconds=180,
    )

    assert result.disposition is EvidenceRequestClaimDisposition.CLAIMED
    assert result.lease is not None
    assert result.lease.request == request
    assert result.lease.lease_token == LEASE_TOKEN
    assert result.lease.row_version == 2
    query = next(
        call[1]
        for call in connection.calls
        if call[0] == "fetchrow" and "WITH candidate AS" in call[1]
    )
    assert "FOR UPDATE OF request SKIP LOCKED" in query
    assert "capture.agent_run_id = $2::uuid" in query
    assert "task.finalization_epoch = request.finalization_epoch" in query
    assert "request.status = 'pending'" in query
    assert "request.lease_owner IS NULL OR request.lease_expires_at <= now()" in query
    claim_call = next(
        call
        for call in connection.calls
        if call[0] == "fetchrow" and "WITH candidate AS" in call[1]
    )
    assert claim_call[2][5] == ["active_session"]
    assert connection.transaction_entered == 1


async def test_evidence_request_claim_returns_empty_without_candidate() -> None:
    repository = PostgresEvidenceRequestRepository(
        pool=FakePool(FakeConnection(fetchrow_results=[None]))
    )

    result = await repository.claim_next(
        task_id=TASK_ID,
        agent_run_id=AGENT_RUN_ID,
        finalization_epoch=3,
        lease_owner="worker-1:app-recapture",
        lease_seconds=180,
    )

    assert result.disposition is EvidenceRequestClaimDisposition.EMPTY
    assert result.lease is None


async def test_evidence_request_retry_release_is_fenced_and_scheduled() -> None:
    connection = FakeConnection(
        fetchrow_results=[{"status": "pending", "row_version": 3}]
    )
    repository = PostgresEvidenceRequestRepository(pool=FakePool(connection))
    next_attempt_at = NOW + timedelta(minutes=1)

    result = await repository.release_lease(
        command=ReleaseEvidenceRequestLeaseCommand(
            request_id=_probe_work().evidence_request.request_id,
            task_id=TASK_ID,
            finalization_epoch=3,
            lease_owner="worker-1:app-recapture",
            lease_token=LEASE_TOKEN,
            row_version=2,
            next_attempt_at=next_attempt_at,
            reason_code="RECAPTURE_RUNTIME_UNAVAILABLE",
        )
    )

    assert result.disposition is EvidenceRequestLeaseReleaseDisposition.RETRY_SCHEDULED
    assert result.row_version == 3
    query = next(call[1] for call in connection.calls if call[0] == "fetchrow")
    assert "request.task_id = $2::uuid" in query
    assert "request.finalization_epoch = $3" in query
    assert "request.lease_owner = $4" in query
    assert "request.lease_token = $5::uuid" in query
    assert "request.row_version = $6" in query
    assert "request.lease_expires_at > now()" in query
    assert "request.attempt_count >= request.max_attempts" in query
    assert "status = 'unfulfillable'" in query
    assert "WHEN eligible.target_status = 'pending' THEN $7" in query
    assert "AND $7 > now()" in query
    assert "lease_owner = NULL" in query
    assert connection.transaction_entered == 1


async def test_evidence_request_retry_release_reports_attempt_exhaustion() -> None:
    connection = FakeConnection(
        fetchrow_results=[{"status": "unfulfillable", "row_version": 4}]
    )
    repository = PostgresEvidenceRequestRepository(pool=FakePool(connection))

    result = await repository.release_lease(
        command=ReleaseEvidenceRequestLeaseCommand(
            request_id=_probe_work().evidence_request.request_id,
            task_id=TASK_ID,
            finalization_epoch=3,
            lease_owner="worker-1:app-recapture",
            lease_token=LEASE_TOKEN,
            row_version=3,
            next_attempt_at=NOW + timedelta(minutes=1),
            reason_code="RECAPTURE_RUNTIME_UNAVAILABLE",
        )
    )

    assert result.disposition is EvidenceRequestLeaseReleaseDisposition.RETRY_EXHAUSTED
    assert result.row_version == 4


async def test_evidence_request_terminal_settlement_requires_active_lease_fence() -> None:
    connection = FakeConnection(fetchval_results=[4])
    repository = PostgresEvidenceRequestRepository(pool=FakePool(connection))

    row_version = await repository.settle_lease(
        command=SettleEvidenceRequestLeaseCommand(
            request_id=_probe_work().evidence_request.request_id,
            task_id=TASK_ID,
            finalization_epoch=3,
            lease_owner="worker-1:app-recapture",
            lease_token=LEASE_TOKEN,
            row_version=3,
            target_status=EvidenceRequestStatus.UNFULFILLABLE,
            resolution_reason="TRUSTED_RECOVERY_ROUTE_UNAVAILABLE",
        )
    )

    assert row_version == 4
    query = next(call[1] for call in connection.calls if call[0] == "fetchval")
    assert "request.task_id = $2::uuid" in query
    assert "request.finalization_epoch = $3" in query
    assert "request.lease_owner = $4" in query
    assert "request.lease_token = $5::uuid" in query
    assert "request.row_version = $6" in query
    assert "request.lease_expires_at > now()" in query
    assert "$7::varchar <> 'expired' OR request.expires_at <= now()" in query
    assert "task.finalization_epoch = request.finalization_epoch" in query
    assert "lease_owner = NULL" in query
    assert "completed_at = now()" in query
    assert connection.transaction_entered == 1


async def test_unfenced_evidence_request_transition_excludes_active_lease() -> None:
    connection = FakeConnection(fetchval_results=[2])
    repository = PostgresEvidenceRequestRepository(pool=FakePool(connection))

    await repository.transition(
        command=TransitionEvidenceRequestCommand(
            request_id=_probe_work().evidence_request.request_id,
            expected_status=EvidenceRequestStatus.PENDING,
            target_status=EvidenceRequestStatus.UNFULFILLABLE,
            row_version=1,
            resolution_reason="legacy_route_failed",
        )
    )

    query = next(call[1] for call in connection.calls if call[0] == "fetchval")
    assert "request.lease_token IS NULL OR request.lease_expires_at <= now()" in query


async def test_explorer_claim_uses_skip_locked_and_reclaims_expired_lease() -> None:
    work = _exploration_work()
    connection = FakeConnection(
        fetchrow_results=[
            {
                "work_type": "exploration",
                "work_payload": work.to_dict(),
                "status": "leased",
                "lease_token": LEASE_TOKEN,
                "attempt_count": 1,
                "row_version": 1,
            }
        ]
    )
    repository = PostgresExplorerWorkRepository(pool=FakePool(connection))

    lease = await repository.claim(
        command=ClaimExplorerWorkCommand(
            work_type=ExplorerWorkType.EXPLORATION,
            lease_owner="explorer-worker-1",
            task_id=TASK_ID,
        ),
        lease_token=LEASE_TOKEN,
    )

    assert lease is not None
    assert lease.work == work
    query = connection.calls[0][1]
    assert "FOR UPDATE OF work_item SKIP LOCKED" in query
    assert "work_item.lease_expires_at <= now()" in query
    assert "task.status = 'running'" in query
    assert "work_item.deadline > now()" in query


async def test_explorer_claim_waits_for_retry_deadline_and_reclaims_both_expired_lease_states() -> (
    None
):
    """retry_wait 未到 next_attempt_at 不得被当前 Worker 提前重领。"""

    work = _exploration_work()
    connection = FakeConnection(
        fetchrow_results=[
            {
                "work_type": "exploration",
                "work_payload": work.to_dict(),
                "status": "leased",
                "lease_token": LEASE_TOKEN,
                "attempt_count": 2,
                "row_version": 3,
            }
        ]
    )
    repository = PostgresExplorerWorkRepository(pool=FakePool(connection))

    lease = await repository.claim(
        command=ClaimExplorerWorkCommand(
            work_type=ExplorerWorkType.EXPLORATION,
            lease_owner="explorer-worker-2",
            task_id=TASK_ID,
        ),
        lease_token=LEASE_TOKEN,
    )

    assert lease is not None
    query = connection.calls[0][1]
    assert "work_item.attempt_count < work_item.max_attempts" in query
    assert "work_item.status IN ('pending', 'retry_wait')" in query
    assert "work_item.next_attempt_at <= now()" in query
    assert "work_item.status IN ('leased', 'running')" in query
    assert "work_item.lease_expires_at <= now()" in query
    assert "FOR UPDATE OF work_item SKIP LOCKED" in query


async def test_expire_unclaimable_marks_attempt_exhaustion_and_clears_leases() -> None:
    connection = FakeConnection(fetchval_results=[2])
    repository = PostgresExplorerWorkRepository(pool=FakePool(connection))

    expired = await repository.expire_unclaimable(limit=50)

    assert expired == 2
    query = connection.calls[0][1]
    assert "status NOT IN" in query
    assert "deadline <= now()" in query
    assert "attempt_count >= max_attempts" in query
    assert "FOR UPDATE SKIP LOCKED" in query
    assert "EXPLORER_WORK_ATTEMPTS_EXHAUSTED" in query
    assert "lease_owner = NULL" in query
    assert "lease_token = NULL" in query
    assert "lease_expires_at = NULL" in query
    assert "finished_at = now()" in query


async def test_explorer_payload_update_preserves_h5_frontier_under_running_lease() -> None:
    """若恢复快照更新未受租约保护，旧 Worker 可覆盖另一个 Worker 的尝试计数。"""
    work = _exploration_work()
    connection = FakeConnection(fetchval_results=[5])
    repository = PostgresExplorerWorkRepository(pool=FakePool(connection))

    row_version = await repository.update_payload(
        command=UpdateExplorerWorkPayloadCommand(
            work=work,
            lease_token=LEASE_TOKEN,
            row_version=4,
        )
    )

    assert row_version == 5
    query = connection.calls[0][1]
    args = connection.calls[0][2]
    assert "SET work_payload = $2::jsonb" in query
    assert "payload_hash = $3::varchar" in query
    assert "status = 'running'" in query
    assert "row_version = $4::bigint" in query
    assert "lease_token = $5::uuid" in query
    assert '"frontier"' in args[1]


async def test_explorer_payload_update_persists_rebaseline_capture_and_restore_audit() -> None:
    """成功恢复后，父 Work 的新 Capture 与恢复次数必须在 successor 前落库。"""
    work = _exploration_work()
    snapshot = H5TaskFrontierSnapshot.create(max_pages=None).merge(
        source_capture_id=work.source_capture_id,
        source_state_version=work.frontier.observation.state_version,
        source_location=work.frontier.observation.location,
        navigation_depth=0,
        candidates=work.frontier.candidates,
    )
    snapshot = snapshot.select_entry(snapshot.pending_entries[0].entry_id)
    snapshot = snapshot.record_restoration_attempt(snapshot.entries[0].entry_id)
    observation = replace(
        work.frontier.observation,
        capture_id=RESTORED_CAPTURE_ID,
        metadata={
            **work.frontier.observation.metadata,
            "h5_task_frontier": snapshot.to_dict(),
        },
    )
    restored_work = replace(
        work,
        source_capture_id=RESTORED_CAPTURE_ID,
        frontier=replace(work.frontier, observation=observation),
    )
    connection = FakeConnection(fetchval_results=[5])
    repository = PostgresExplorerWorkRepository(pool=FakePool(connection))

    row_version = await repository.update_payload(
        command=UpdateExplorerWorkPayloadCommand(
            work=restored_work,
            lease_token=LEASE_TOKEN,
            row_version=4,
        )
    )

    assert row_version == 5
    query = connection.calls[0][1]
    args = connection.calls[0][2]
    serialized_payload = args[1]
    assert f'"source_capture_id":"{RESTORED_CAPTURE_ID}"' in serialized_payload
    assert '"restoration_attempt_count":1' in serialized_payload
    assert "source_capture_id = capture.id" in query
    assert "FROM qc_capture AS capture" in query
    assert "capture.id = $6::uuid" in query
    assert "capture.task_id = work_item.task_id" in query
    assert "capture.surface_session_id = work_item.session_id" in query
    assert "capture.capture_status = 'captured'" in query
    assert args[5] == RESTORED_CAPTURE_ID


async def test_next_claim_at_reads_postgres_retry_or_lease_schedule_without_local_state() -> None:
    next_attempt_at = NOW + timedelta(minutes=2)
    connection = FakeConnection(fetchval_results=[next_attempt_at])
    repository = PostgresExplorerWorkRepository(pool=FakePool(connection))

    observed = await repository.next_claim_at(
        task_id=TASK_ID,
        work_type=ExplorerWorkType.EXPLORATION,
    )

    assert observed == next_attempt_at
    query = connection.calls[0][1]
    assert "WHEN work_item.status = 'retry_wait'" in query
    assert "THEN work_item.next_attempt_at" in query
    assert "work_item.status IN ('leased', 'running')" in query
    assert "THEN work_item.lease_expires_at" in query
    assert "work_item.attempt_count < work_item.max_attempts" in query
    assert "work_item.next_attempt_at < work_item.deadline" in query
    assert "work_item.lease_expires_at < work_item.deadline" in query
    assert "work_item.deadline > now()" in query
    assert "task.status = 'running'" in query
    assert "task.finalization_state = 'open'" in query
    assert "work_item.finalization_epoch = task.finalization_epoch" in query


async def test_cancel_nonterminal_clears_explorer_lease_without_touching_probe_work() -> None:
    connection = FakeConnection(fetchval_results=[3])
    repository = PostgresExplorerWorkRepository(pool=FakePool(connection))

    canceled = await repository.cancel_nonterminal(
        task_id=TASK_ID,
        reason="App 探索收到统一 Run 取消信号",
    )

    assert canceled == 3
    query = connection.calls[0][1]
    assert "status = 'cancelled'" in query
    assert "work_type = ANY($3::varchar[])" in query
    assert "status IN ('pending', 'leased', 'running', 'retry_wait')" in query
    assert "last_error_code = COALESCE(last_error_code, 'WORKFLOW_CANCELED')" in query
    assert "last_error_message = COALESCE(last_error_message, $2::text)" in query
    assert "lease_owner = NULL" in query
    assert "lease_token = NULL" in query
    assert "lease_expires_at = NULL" in query


async def test_cancel_nonterminal_preserves_error_and_records_cancel_reason() -> None:
    """取消只能补齐缺失错误字段，并将取消原因写入现有终态 JSON。"""

    connection = FakeConnection(fetchval_results=[1])
    repository = PostgresExplorerWorkRepository(pool=FakePool(connection))

    canceled = await repository.cancel_nonterminal(
        task_id=TASK_ID,
        reason="H5 探索收到统一 Run 取消信号",
    )

    assert canceled == 1
    query = connection.calls[0][1]
    assert "terminal_payload = COALESCE(terminal_payload, '{}'::jsonb)" in query
    assert "jsonb_build_object('cancel_reason', $2::text)" in query
    assert "last_error_code = COALESCE(last_error_code, 'WORKFLOW_CANCELED')" in query
    assert "last_error_message = COALESCE(last_error_message, $2::text)" in query
    assert "last_error_code = 'WORKFLOW_CANCELED'" not in query
    assert "last_error_message = $2" not in query


async def test_h5_cancel_nonterminal_covers_probe_and_limits_to_web_work() -> None:
    """若遗漏 H5 条件，App Probe 或 H5 Probe 会在取消竞态中被错误保留或误取消。"""

    connection = FakeConnection(fetchval_results=[2])
    repository = PostgresExplorerWorkRepository(pool=FakePool(connection))

    canceled = await repository.cancel_nonterminal(
        task_id=TASK_ID,
        reason="H5 探索收到统一 Run 取消信号",
        work_types=(ExplorerWorkType.EXPLORATION, ExplorerWorkType.PROBE),
        h5_only=True,
    )

    assert canceled == 2
    query = connection.calls[0][1]
    assert "work_type = ANY($3::varchar[])" in query
    assert "work_payload->>'surface_kind' = 'web'" in query
    assert "work_item.work_payload" not in query


async def test_has_nonterminal_work_is_the_restart_safe_orphan_check() -> None:
    connection = FakeConnection(fetchval_results=[False])
    repository = PostgresExplorerWorkRepository(pool=FakePool(connection))

    assert await repository.has_nonterminal_work(task_id=TASK_ID) is False

    query = connection.calls[0][1]
    assert "SELECT EXISTS" in query
    assert "status NOT IN" in query
    assert "'retry_wait'" not in query.split("status NOT IN", 1)[1].split(")", 1)[0]
    assert "'cancelled'" in query


async def test_probe_claim_is_blocked_before_database_when_policy_is_disabled() -> None:
    connection = FakeConnection()
    repository = PostgresExplorerWorkRepository(pool=FakePool(connection))

    with pytest.raises(SupplementalEvidenceDisabledError) as error:
        await repository.claim(
            command=ClaimExplorerWorkCommand(
                work_type=ExplorerWorkType.PROBE,
                lease_owner="explorer-worker-1",
                task_id=TASK_ID,
            ),
            lease_token=LEASE_TOKEN,
        )

    assert error.value.operation.value == "claim_probe"
    assert connection.calls == []
    assert connection.transaction_entered == 0


async def test_start_transition_keeps_live_lease_for_terminal_writeback() -> None:
    connection = FakeConnection(fetchval_results=[2])
    repository = PostgresExplorerWorkRepository(pool=FakePool(connection))

    row_version = await repository.transition(
        command=TransitionExplorerWorkCommand(
            work_id=_exploration_work().work_id,
            expected_status=ExplorerWorkStatus.LEASED,
            target_status=ExplorerWorkStatus.RUNNING,
            lease_token=LEASE_TOKEN,
            row_version=1,
        )
    )

    assert row_version == 2
    query = connection.calls[0][1]
    assert "WHEN $2::varchar = 'running' THEN lease_token" in query
    assert "lease_expires_at > now()" in query
    assert "AND row_version = $4" in query


async def test_package_boundary_transition_atomically_cancels_remaining_app_frontier() -> None:
    work = _exploration_work()
    connection = FakeConnection(fetchval_results=[2, "app-frontier:item-1"])
    repository = PostgresExplorerWorkRepository(pool=FakePool(connection))

    row_version = await repository.transition(
        command=TransitionExplorerWorkCommand(
            work_id=work.work_id,
            expected_status=ExplorerWorkStatus.RUNNING,
            target_status=ExplorerWorkStatus.NO_PROGRESS,
            lease_token=LEASE_TOKEN,
            row_version=1,
            terminal=ExplorationTerminal(
                work_id=work.work_id,
                task_id=work.task_id,
                status=ExplorationTerminalStatus.NO_PROGRESS,
                reason="当前前台应用已越过目标 App 包边界，停止生成后继探索工作。",
                outcome="evidence_insufficient",
                reason_code="app_package_boundary_violation",
            ),
            error_code="APP_PACKAGE_BOUNDARY_VIOLATION",
            error_message="设备当前前台包已超出目标 App 包边界",
            app_frontier_settlement=AppFrontierSettlement(
                task_id=work.task_id,
                agent_run_id=AGENT_RUN_ID,
                session_id=work.session_id,
                frontier_item_id="app-frontier:item-1",
                candidate_fingerprint="app-frontier-fingerprint:" + "a" * 64,
                target_status=AppFrontierStatus.FAILED,
                lease_token=LEASE_TOKEN,
                row_version=2,
                result_capture_id=CAPTURE_ID,
                error_code="APP_PACKAGE_BOUNDARY_VIOLATION",
            ),
        )
    )

    assert row_version == 2
    queries = [call[1] for call in connection.calls]
    lock_index = next(
        index
        for index, query in enumerate(queries)
        if "pg_advisory_xact_lock" in query
    )
    work_index = next(
        index
        for index, query in enumerate(queries)
        if "UPDATE qc_v2_work_item SET status = $2::varchar" in query
    )
    settled_index = next(
        index
        for index, query in enumerate(queries)
        if "UPDATE qc_app_exploration_frontier SET status = $7::varchar" in query
    )
    cancel_work_index = next(
        index
        for index, query in enumerate(queries)
        if "work_id <> $2::varchar" in query
    )
    cancel_frontier_index = next(
        index
        for index, query in enumerate(queries)
        if "UPDATE qc_app_exploration_frontier SET status = 'cancelled'" in query
    )
    assert lock_index < work_index < settled_index < cancel_work_index < cancel_frontier_index
    assert "APP_PACKAGE_BOUNDARY_VIOLATION" in queries[cancel_work_index]
    assert "APP_PACKAGE_BOUNDARY_VIOLATION" in queries[cancel_frontier_index]


async def test_dynamic_plateau_transition_does_not_supersede_remaining_frontier() -> None:
    work = _exploration_work()
    connection = FakeConnection(fetchval_results=[2])
    repository = PostgresExplorerWorkRepository(pool=FakePool(connection))

    row_version = await repository.transition(
        command=TransitionExplorerWorkCommand(
            work_id=work.work_id,
            expected_status=ExplorerWorkStatus.RUNNING,
            target_status=ExplorerWorkStatus.SUCCEEDED,
            lease_token=LEASE_TOKEN,
            row_version=1,
            terminal=ExplorationTerminal(
                work_id=work.work_id,
                task_id=work.task_id,
                status=ExplorationTerminalStatus.COMPLETED,
                reason="动态页面探索达到收益平台。",
                outcome="exploration_completed",
                reason_code="exploration_completed",
                coverage_summary={
                    "completion_mode": "dynamic_discovery",
                    "coverage_complete": True,
                    "dynamic_frontier_plateau_reached": True,
                },
            ),
        )
    )

    assert row_version == 2
    assert not any(
        method == "execute" and "APP_DYNAMIC_FRONTIER_PLATEAU" in query
        for method, query, _ in connection.calls
    )


async def test_running_exploration_or_probe_lease_can_be_renewed_without_changing_version() -> None:
    connection = FakeConnection(fetchval_results=[_exploration_work().work_id])
    repository = PostgresExplorerWorkRepository(pool=FakePool(connection))

    await repository.renew(
        command=RenewExplorerWorkLeaseCommand(
            work_id=_exploration_work().work_id,
            lease_owner="explorer-worker-1",
            lease_token=LEASE_TOKEN,
            lease_seconds=180,
        )
    )

    query = connection.calls[0][1]
    assert "status = 'running'" in query
    assert "lease_owner = $2" in query
    assert "lease_token = $3::uuid" in query
    assert "lease_expires_at > now()" in query
    assert "work_type = 'exploration'" not in query
    assert "row_version" not in query


async def test_successor_commit_completes_parent_and_writes_work_outbox_atomically() -> None:
    successor = _successor_work()
    connection = FakeConnection(
        fetchrow_results=[{"status": "pending", "created": True}],
        fetchval_results=[2, "message-1"],
    )
    repository = PostgresExplorerWorkRepository(pool=FakePool(connection))

    committed = await repository.commit_successor(
        command=CommitExplorerWorkSuccessorCommand(
            parent_work_id=_exploration_work().work_id,
            task_id=TASK_ID,
            session_id="session-1",
            lease_owner="explorer-worker-1",
            lease_token=LEASE_TOKEN,
            row_version=1,
            successor=successor,
        )
    )

    assert committed.parent_row_version == 2
    assert committed.successor_work_id == successor.work_id
    assert committed.successor_created is True
    assert connection.transaction_entered == 1
    queries = [call[1] for call in connection.calls]
    parent_index = next(
        index for index, query in enumerate(queries) if "UPDATE qc_v2_work_item AS parent" in query
    )
    successor_index = next(
        index
        for index, query in enumerate(queries)
        if "INSERT INTO qc_v2_work_item AS existing" in query
    )
    outbox_index = next(
        index for index, query in enumerate(queries) if "INSERT INTO qc_outbox_event" in query
    )
    assert parent_index < successor_index < outbox_index
    parent_query = queries[parent_index]
    assert "parent.status = 'running'" in parent_query
    assert "parent.lease_owner = $4" in parent_query
    assert "parent.lease_token = $5::uuid" in parent_query
    assert "parent.lease_expires_at > now()" in parent_query
    assert "parent.branch_id = $7" in parent_query
    assert "parent.source_capture_id = $8::uuid" in parent_query
    assert "SET status = $9::varchar" in parent_query
    assert connection.calls[parent_index][2][8] == ExplorerWorkStatus.SUCCEEDED.value


async def test_successor_commit_persists_parent_and_successor_without_outbox_wakeup() -> None:
    successor = _successor_work()
    connection = FakeConnection(
        fetchrow_results=[{"status": "pending", "created": True}],
        fetchval_results=[2],
    )
    repository = PostgresExplorerWorkRepository(pool=FakePool(connection))

    committed = await repository.commit_successor(
        command=CommitExplorerWorkSuccessorCommand(
            parent_work_id=_exploration_work().work_id,
            task_id=TASK_ID,
            session_id="session-1",
            lease_owner="explorer-worker-1",
            lease_token=LEASE_TOKEN,
            row_version=1,
            successor=successor,
        ),
        emit_wakeup=False,
    )

    queries = [call[1] for call in connection.calls]
    assert committed.parent_row_version == 2
    assert committed.successor_work_id == successor.work_id
    assert any("UPDATE qc_v2_work_item AS parent" in query for query in queries)
    assert any("INSERT INTO qc_v2_work_item AS existing" in query for query in queries)
    assert not any("INSERT INTO qc_outbox_event" in query for query in queries)


async def test_h5_recovery_commit_supersedes_parent_and_writes_successor_atomically() -> None:
    parent = replace(_exploration_work(), session_id="h5:run-1")
    target_session_id = build_h5_recovery_session_id(
        agent_run_id="run-1",
        work_id=parent.work_id,
        stage=H5FailureStage.BEFORE_CAPTURE,
        recovery_attempt_count=1,
    )
    successor = _successor_work()
    facts = H5BrowserRecoveryFacts.create(
        task_id=parent.task_id,
        agent_run_id="run-1",
        failed_work_id=parent.work_id,
        source_session_id=parent.session_id,
        target_session_id=target_session_id,
        failure_stage=H5FailureStage.BEFORE_CAPTURE,
        recovery_attempt_count=1,
        original_source_capture_id=parent.source_capture_id,
        rebaseline_before_capture_id="capture-recovery-before",
        rebaseline_after_capture_id=successor.source_capture_id,
        failed_action_execution_id=None,
        restoration_action_execution_id="action-recovery",
        inventory_status=None,
        source_state_version=parent.frontier.observation.state_version,
        target_state_version=successor.frontier.observation.state_version,
        equivalent=True,
        reason_code=None,
    )
    frontier = replace(
        successor.frontier,
        observation=replace(
            successor.frontier.observation,
            metadata={
                **dict(successor.frontier.observation.metadata),
                "parent_work_id": parent.work_id,
                "h5_browser_recovery": facts.to_dict(),
            },
        ),
    )
    successor = replace(
        successor,
        task_id=parent.task_id,
        session_id=target_session_id,
        frontier=frontier,
        frontier_snapshot_id=frontier.snapshot_id,
    )
    connection = FakeConnection(
        fetchrow_results=[{"status": "pending", "created": True}],
        fetchval_results=[2, "message-1"],
    )
    repository = PostgresExplorerWorkRepository(pool=FakePool(connection))

    await repository.commit_successor(
        command=CommitExplorerWorkSuccessorCommand(
            parent_work_id=parent.work_id,
            task_id=TASK_ID,
            session_id=parent.session_id,
            lease_owner="explorer-worker-1",
            lease_token=LEASE_TOKEN,
            row_version=1,
            successor=successor,
            parent_target_status=ExplorerWorkStatus.SUPERSEDED,
        )
    )

    parent_call = next(
        call for call in connection.calls if "UPDATE qc_v2_work_item AS parent" in call[1]
    )
    assert parent_call[2][8] == ExplorerWorkStatus.SUPERSEDED.value
    assert parent_call[2][2] == parent.session_id
    assert "SET status = $9::varchar" in parent_call[1]
    successor_call = next(
        call for call in connection.calls if "INSERT INTO qc_v2_work_item AS existing" in call[1]
    )
    assert successor_call[2][2] == successor.session_id


def test_successor_commit_rejects_nonterminal_parent_target_status() -> None:
    with pytest.raises(ValueError, match="parent_target_status"):
        CommitExplorerWorkSuccessorCommand(
            parent_work_id=_exploration_work().work_id,
            task_id=TASK_ID,
            session_id="session-1",
            lease_owner="explorer-worker-1",
            lease_token=LEASE_TOKEN,
            row_version=1,
            successor=_successor_work(),
            parent_target_status=ExplorerWorkStatus.RUNNING,
        )


@pytest.mark.parametrize(
    ("history_counts", "active_count"),
    [
        ((2,), None),
        ((1, 3), None),
        ((1, 2, 3), None),
        ((1,), 3),
    ],
)
def test_h5_recovery_attempt_history_requires_genesis_bounds_and_continuity(
    history_counts: tuple[int, ...],
    active_count: int | None,
) -> None:
    from services.api.app.application.h5_exploration_workflow import (
        _with_task_frontier,
    )
    from services.api.app.infrastructure.db.explorer_work_repository import (
        ExplorerWorkPersistenceError,
        _parse_work,
    )
    from services.api.tests.test_h5_exploration_turn import _work

    work = _work()
    snapshot = H5TaskFrontierSnapshot.create(max_pages=3)
    frontier = _with_task_frontier(frontier=work.frontier, snapshot=snapshot)
    work = replace(work, frontier=frontier, frontier_snapshot_id=frontier.snapshot_id)

    def attempt(count: int, *, status: str) -> dict[str, object]:
        return H5BrowserRecoveryAttemptFacts.create(
            task_id=work.task_id,
            agent_run_id="run-h5",
            source_session_id=work.session_id,
            failed_work_id=work.work_id,
            source_state_version=work.frontier.observation.state_version,
            frontier_snapshot_id=work.frontier_snapshot_id,
            failure_stage=H5FailureStage.ACTION,
            recovery_attempt_count=count,
            status=status,
            error_code="page_capture_session_crashed",
            action_execution_id="action-unknown",
            before_capture_id="capture-before",
            after_capture_id=None,
            interaction_status="failed",
            interaction_result_capture_id=None,
            started_at=work.frontier.observation.observed_at.isoformat(),
            deadline=work.deadline.isoformat(),
            remaining_duration_ms=work.budget.remaining_duration_ms,
            action_cost=2,
            remaining_actions_after_reservation=work.budget.remaining_actions,
            last_safe_location=work.frontier.observation.location,
            reason_code=("attempt_failed" if status == "completed" else None),
        ).to_dict()

    metadata = {
        **dict(work.frontier.observation.metadata),
        "agent_run_id": "run-h5",
        "h5_browser_recovery_attempt_history": [
            attempt(count, status="completed") for count in history_counts
        ],
    }
    if active_count is not None:
        metadata["h5_browser_recovery_attempt"] = attempt(
            active_count,
            status="started",
        )
    frontier = replace(
        work.frontier,
        observation=replace(work.frontier.observation, metadata=metadata),
    )
    work = replace(work, frontier=frontier, frontier_snapshot_id=frontier.snapshot_id)

    with pytest.raises(ExplorerWorkPersistenceError):
        _parse_work("exploration", work.to_dict())


@pytest.mark.asyncio
async def test_standalone_transition_cannot_supersede_without_atomic_successor() -> None:
    repository = PostgresExplorerWorkRepository(pool=FakePool(FakeConnection()))

    with pytest.raises(ValueError, match="状态迁移不受支持"):
        await repository.transition(
            command=TransitionExplorerWorkCommand(
                work_id=_exploration_work().work_id,
                expected_status=ExplorerWorkStatus.RUNNING,
                target_status=ExplorerWorkStatus.SUPERSEDED,
                lease_token=LEASE_TOKEN,
                row_version=1,
            )
        )


def test_default_successor_commit_rejects_session_change() -> None:
    with pytest.raises(ValueError, match="沿用父 Session"):
        CommitExplorerWorkSuccessorCommand(
            parent_work_id=_exploration_work().work_id,
            task_id=TASK_ID,
            session_id="session-1",
            lease_owner="explorer-worker-1",
            lease_token=LEASE_TOKEN,
            row_version=1,
            successor=replace(_successor_work(), session_id="session-recovery-1"),
        )


async def test_successor_commit_settles_leased_app_frontier_in_same_transaction() -> None:
    successor = _successor_work()
    connection = FakeConnection(
        fetchrow_results=[{"status": "pending", "created": True}],
        fetchval_results=[2, "app-frontier:item-1", "message-1"],
    )
    repository = PostgresExplorerWorkRepository(pool=FakePool(connection))

    await repository.commit_successor(
        command=CommitExplorerWorkSuccessorCommand(
            parent_work_id=_exploration_work().work_id,
            task_id=TASK_ID,
            session_id="session-1",
            lease_owner="explorer-worker-1",
            lease_token=LEASE_TOKEN,
            row_version=1,
            successor=successor,
            app_frontier_settlement=AppFrontierSettlement(
                task_id=TASK_ID,
                agent_run_id=AGENT_RUN_ID,
                session_id="session-1",
                frontier_item_id="app-frontier:item-1",
                candidate_fingerprint="app-frontier-fingerprint:" + "a" * 64,
                target_status=AppFrontierStatus.VISITED,
                lease_token=LEASE_TOKEN,
                row_version=2,
                result_capture_id=CAPTURE_ID,
            ),
        )
    )

    assert connection.transaction_entered == 1
    queries = [call[1] for call in connection.calls]
    parent_index = next(
        index for index, query in enumerate(queries) if "UPDATE qc_v2_work_item AS parent" in query
    )
    successor_index = next(
        index
        for index, query in enumerate(queries)
        if "INSERT INTO qc_v2_work_item AS existing" in query
    )
    frontier_index = next(
        index
        for index, query in enumerate(queries)
        if "UPDATE qc_app_exploration_frontier" in query
    )
    outbox_index = next(
        index for index, query in enumerate(queries) if "INSERT INTO qc_outbox_event" in query
    )
    assert parent_index < successor_index < frontier_index < outbox_index
    frontier_query = queries[frontier_index]
    assert "status = 'running'" in frontier_query
    assert "lease_token = $12::uuid" in frontier_query
    assert "lease_expires_at > now()" in frontier_query
    assert "result_work_id = COALESCE($8::varchar, result_work_id)" in frontier_query


async def test_app_cancellation_clears_work_and_frontier_in_same_transaction() -> None:
    connection = FakeConnection(fetchval_results=[2, 3])
    repository = PostgresExplorerWorkRepository(pool=FakePool(connection))

    canceled = await repository.cancel_app_exploration_nonterminal(
        task_id=TASK_ID,
        reason="App 探索收到统一 Run 取消信号",
    )

    assert canceled == 5
    assert connection.transaction_entered == 1
    queries = [call[1] for call in connection.calls]
    assert "pg_advisory_xact_lock" in queries[0]
    assert "UPDATE qc_v2_work_item" in queries[1]
    assert "work_type = 'exploration'" in queries[1]
    assert "UPDATE qc_app_exploration_frontier" in queries[2]
    assert all("status = 'cancelled'" in query for query in queries[1:])
    assert all("lease_token = NULL" in query for query in queries[1:])


async def test_expired_parent_lease_cannot_create_claimable_successor() -> None:
    connection = FakeConnection(fetchval_results=[None])
    repository = PostgresExplorerWorkRepository(pool=FakePool(connection))

    with pytest.raises(RuntimeError, match="租约"):
        await repository.commit_successor(
            command=CommitExplorerWorkSuccessorCommand(
                parent_work_id=_exploration_work().work_id,
                task_id=TASK_ID,
                session_id="session-1",
                lease_owner="explorer-worker-1",
                lease_token=LEASE_TOKEN,
                row_version=1,
                successor=_successor_work(),
            )
        )

    assert not any("INSERT INTO qc_v2_work_item" in query for _, query, _ in connection.calls)
    assert not any("INSERT INTO qc_outbox_event" in query for _, query, _ in connection.calls)


async def test_manifest_validates_sources_and_emits_two_outbox_events_atomically() -> None:
    manifest = _manifest()
    connection = FakeConnection(
        fetchrow_results=[{"manifest_id": manifest.manifest_id, "created": True}],
        fetchval_results=[True, True, "capture-message", "ready-message"],
    )
    repository = PostgresEvidenceManifestRepository(pool=FakePool(connection))

    persisted = await repository.add(manifest=manifest)

    assert persisted.created is True
    assert connection.transaction_entered == 1
    queries = [call[1] for call in connection.calls]
    assert any("checksum = $6" in query and "size_bytes = $7" in query for query in queries)
    assert any("quality_gate_status = 'pass'" in query for query in queries)
    assert sum("INSERT INTO qc_outbox_event" in query for query in queries) == 2
    outbox_calls = [call for call in connection.calls if "INSERT INTO qc_outbox_event" in call[1]]
    assert all(call[2][2] == "evidence_manifest" for call in outbox_calls)
    assert all(call[2][4] == manifest.manifest_version for call in outbox_calls)
    manifest_query = next(query for query in queries if "INSERT INTO qc_evidence_manifest" in query)
    assert "capture.task_id = $3::uuid" in manifest_query
    assert "task.finalization_state IN" in manifest_query
    assert "existing.payload_hash = EXCLUDED.payload_hash" in manifest_query
    lock_call = next(call for call in connection.calls if "pg_advisory_xact_lock" in call[1])
    assert lock_call[2] == (f"task-finalization:{TASK_ID}",)


async def test_outbox_claim_reaps_exhausted_and_uses_short_lease() -> None:
    work = _exploration_work()
    from services.api.app.domain.protocol import build_protocol_envelope

    envelope = build_protocol_envelope(
        message_type="alioth.v2.exploration-work.available",
        payload_protocol_version=work.protocol_version,
        idempotency_key=work.work_id,
        task_id=TASK_ID,
        aggregate_type="explorer_work",
        aggregate_id=work.work_id,
        aggregate_version=0,
        occurred_at=NOW,
        payload=work.to_dict(),
    )
    connection = FakeConnection(
        fetchrow_results=[
            {
                "aggregate_type": "explorer_work",
                "aggregate_id": work.work_id,
                "aggregate_version": 0,
                "envelope_payload": envelope.to_dict(),
                "lease_token": LEASE_TOKEN,
                "attempt_count": 1,
            }
        ]
    )
    repository = PostgresOutboxRepository(pool=FakePool(connection))

    lease = await repository.claim(
        lease_owner="outbox-relay-1",
        lease_token=LEASE_TOKEN,
    )

    assert lease is not None
    assert lease.envelope == envelope
    assert connection.transaction_entered == 1
    assert "status = 'dead_letter'" in connection.calls[0][1]
    claim_query = connection.calls[1][1]
    assert "FOR UPDATE SKIP LOCKED" in claim_query
    assert "lease_expires_at <= now()" in claim_query
    assert "attempt_count < max_attempts" in claim_query
    assert "message_type = ANY($4::varchar[])" in claim_query
    assert "(message_type = ANY($5::varchar[])) DESC" in claim_query
    assert connection.calls[1][2][3] == list(SUPPORTED_WAKEUP_MESSAGE_TYPES)
    assert connection.calls[1][2][4] == list(TASK_FINALIZATION_PRIORITY_WAKEUP_MESSAGE_TYPES)


async def test_outbox_records_stream_receipt_and_sweeper_requeues_database_fact() -> None:
    connection = FakeConnection(fetchval_results=[TASK_ID, 2, True])
    repository = PostgresOutboxRepository(pool=FakePool(connection))

    published = await repository.mark_published(
        message_id="message-1",
        lease_token=LEASE_TOKEN,
        stream_name="alioth:v2:wakeup:inspection",
        stream_entry_id="1-0",
    )
    requeued = await repository.requeue_stale_deliveries(
        message_types=("alioth.v2.evidence-bundle.ready",),
        min_age_seconds=60,
        limit=100,
    )
    unpublished = await repository.has_unpublished(task_id=TASK_ID)

    assert published is True
    assert requeued == 2
    assert unpublished is True
    assert connection.transaction_entered == 2
    mark_query = connection.calls[0][1]
    assert "last_stream_name = $3" in mark_query
    assert "last_stream_entry_id = $4" in mark_query
    assert "RETURNING task_id::text" in mark_query
    wake_query = connection.calls[1][1]
    assert "finalization_retry_at = now()" in wake_query
    assert "finalization_state = 'session_draining'" in wake_query
    assert "completion_gate_snapshot_id IS NOT NULL" in wake_query
    assert "finalization_recovery_state = 'active'" in wake_query
    assert connection.calls[1][2] == (TASK_ID,)
    sweep_query = connection.calls[2][1]
    assert "FOR UPDATE OF event SKIP LOCKED" in sweep_query
    assert "task.status IN ('queued', 'running')" in sweep_query
    assert "event.aggregate_type <> 'inspection_work_item'" in sweep_query
    assert "event.aggregate_type <> 'explorer_work'" in sweep_query
    assert "work_item.status NOT IN" in sweep_query
    assert "event.status = 'published'" in sweep_query
    assert "event.status = 'dead_letter'" in sweep_query
    assert "replay_count = event.replay_count + 1" in sweep_query
    assert "'dead_letter'" in connection.calls[3][1]
    has_unpublished_query = connection.calls[3][1]
    assert "message_type = ANY($2::varchar[])" in has_unpublished_query
    assert connection.calls[3][2][1] == list(SUPPORTED_WAKEUP_MESSAGE_TYPES)
    assert "alioth.v2.h5-inspection-publication.available" in connection.calls[3][2][1]
    assert "alioth.v2.app-inspection-publication.available" in connection.calls[3][2][1]


async def test_outbox_does_not_wake_finalization_for_stale_publish_receipt() -> None:
    connection = FakeConnection(fetchval_results=[None])
    repository = PostgresOutboxRepository(pool=FakePool(connection))

    published = await repository.mark_published(
        message_id="message-1",
        lease_token=LEASE_TOKEN,
        stream_name="alioth:v2:wakeup:inspection",
        stream_entry_id="1-0",
    )

    assert published is False
    assert connection.transaction_entered == 1
    assert len(connection.calls) == 1


async def test_journey_runtime_persists_attempt_and_cursor_atomically() -> None:
    plan = build_journey_execution_plan(_journey_definition(), task_id=TASK_ID)
    execution_id = build_journey_execution_id(
        task_id=TASK_ID,
        execution_plan_id=plan.execution_plan_id,
        session_id="session-1",
        execution_epoch=0,
    )
    spec = JourneyExecutionSpec(
        execution_id=execution_id,
        execution_plan_id=plan.execution_plan_id,
        task_id=TASK_ID,
        session_id="session-1",
        agent_run_id=AGENT_RUN_ID,
        surface_kind=SurfaceKind.WEB,
        execution_epoch=0,
        source_capture_id=CAPTURE_ID,
        deadline=NOW + timedelta(hours=1),
    )
    running_row = {
        "spec_payload": spec.to_dict(),
        "status": "running",
        "next_step_index": 0,
        "current_step_attempt_count": 0,
        "current_capture_id": CAPTURE_ID,
        "gap_count": 0,
        "row_version": 0,
        "error_code": None,
        "error_message": None,
    }
    succeeded_row = {
        **running_row,
        "status": "succeeded",
        "next_step_index": 1,
        "row_version": 1,
    }
    attempt_id = build_journey_step_attempt_id(
        execution_id=execution_id,
        step_id="open-login",
        attempt_no=1,
    )
    connection = FakeConnection(
        fetchrow_results=[running_row, succeeded_row],
        fetchval_results=[attempt_id],
    )
    repository = PostgresJourneyExecutionRepository(pool=FakePool(connection))

    state = await repository.start(spec=spec)
    attempt = JourneyStepAttempt(
        attempt_id=attempt_id,
        execution_id=execution_id,
        step_id="open-login",
        step_index=0,
        attempt_no=1,
        status=JourneyStepAttemptStatus.SUCCEEDED,
        source_capture_id=CAPTURE_ID,
        result_capture_id=CAPTURE_ID,
        started_at=NOW,
        finished_at=NOW,
    )
    completed = await repository.commit_step(
        command=CommitJourneyStepCommand(
            execution_id=execution_id,
            expected_row_version=state.row_version,
            expected_step_index=0,
            expected_attempt_count=0,
            attempt=attempt,
            target_status=JourneyExecutionStatus.SUCCEEDED,
            next_step_index=1,
            next_step_attempt_count=0,
            current_capture_id=CAPTURE_ID,
            gap_count=0,
        )
    )

    assert completed.status is JourneyExecutionStatus.SUCCEEDED
    assert connection.transaction_entered == 2
    queries = [call[1] for call in connection.calls]
    start_query = next(query for query in queries if "INSERT INTO qc_journey_execution" in query)
    attempt_query = next(
        query for query in queries if "INSERT INTO qc_journey_step_attempt" in query
    )
    cursor_query = next(query for query in queries if "UPDATE qc_journey_execution" in query)
    assert "run.agent_type IN ('explorer', 'execution')" in start_query
    assert "definition.surface_kinds ? $6" in start_query
    assert "existing.spec_hash = EXCLUDED.spec_hash" in start_query
    assert "execution.row_version = $18" in attempt_query
    assert "source_capture.task_id = execution.task_id" in attempt_query
    assert "execution.row_version = $9" in cursor_query
    assert "current_capture.task_id = execution.task_id" in cursor_query


async def test_retry_wait_resets_delivery_attempts_only_when_sibling_reselected() -> None:
    """改选 sibling 后若不重置投递预算，Frontier 剩几条就会被 max_attempts 掐死。"""
    work = _exploration_work()
    connection = FakeConnection(fetchval_results=[2])
    repository = PostgresExplorerWorkRepository(pool=FakePool(connection))

    await repository.transition(
        command=TransitionExplorerWorkCommand(
            work_id=work.work_id,
            expected_status=ExplorerWorkStatus.RUNNING,
            target_status=ExplorerWorkStatus.RETRY_WAIT,
            lease_token=LEASE_TOKEN,
            row_version=1,
            reset_attempt_count=True,
        )
    )

    query, args = connection.calls[0][1], connection.calls[0][2]
    assert "WHEN $11::boolean AND $2::varchar = 'retry_wait' THEN 0" in query
    assert args[-1] is True

    connection_default = FakeConnection(fetchval_results=[2])
    repository_default = PostgresExplorerWorkRepository(pool=FakePool(connection_default))
    await repository_default.transition(
        command=TransitionExplorerWorkCommand(
            work_id=work.work_id,
            expected_status=ExplorerWorkStatus.RUNNING,
            target_status=ExplorerWorkStatus.RETRY_WAIT,
            lease_token=LEASE_TOKEN,
            row_version=1,
        )
    )
    assert connection_default.calls[0][2][-1] is False


def test_attempt_reset_is_rejected_outside_retry_wait() -> None:
    """重置投递预算只对重投有意义；用在终态迁移上会掩盖毒丸工作。"""
    work = _exploration_work()

    with pytest.raises(ValueError, match="重投"):
        _validate_work_transition(
            TransitionExplorerWorkCommand(
                work_id=work.work_id,
                expected_status=ExplorerWorkStatus.RUNNING,
                target_status=ExplorerWorkStatus.SUCCEEDED,
                lease_token=LEASE_TOKEN,
                row_version=1,
                reset_attempt_count=True,
            )
        )
