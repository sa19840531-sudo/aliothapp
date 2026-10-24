"""H5 定向补采的执行域、规划和 PostgreSQL 隔离测试。"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from services.api.app.application.evidence_recovery_replay import (
    EvidenceRecoveryReplayError,
)
from services.api.app.application.evidence_requests import (
    EvidenceRequestClaimDisposition,
    EvidenceRequestClaimResult,
    EvidenceRequestLeaseReleaseDisposition,
    EvidenceRequestLeaseReleaseResult,
    EvidenceRequestProcessingLease,
    EvidenceRequestStatus,
)
from services.api.app.application.h5_targeted_recapture import (
    H5FinalizingRecoverySnapshotReader,
    H5StateEquivalenceGate,
    H5TargetedRecaptureConsumer,
    H5TargetedRecaptureContext,
    H5TargetedRecaptureDisposition,
    H5TargetedRecaptureDrainService,
    H5TargetedRecapturePlanService,
    H5TargetedRecaptureRuntime,
)
from services.api.app.application.page_capture_persistence import PageCaptureRecoveryError
from services.api.app.application.supplemental_evidence_policy import (
    build_h5_supplemental_evidence_policy,
    build_lab_supplemental_evidence_policy,
)
from services.api.app.domain.exploration import ExplorationBudget
from services.api.app.domain.inspection import (
    EvidenceReplayPolicy,
    EvidenceRequiredEquivalence,
    EvidenceSessionDependency,
    EvidenceSideEffectPolicy,
    EvidenceSourceGrade,
    RestorationStrategy,
    RestorationValidationError,
    StateInvariantSnapshot,
    build_evidence_request,
    build_restoration_plan,
)
from services.api.app.domain.interaction import CaptureKind, CaptureRef, SurfaceKind
from services.api.app.infrastructure.db.h5_targeted_recapture_repository import (
    H5ClaimedEvidenceRequestReference,
    PostgresH5StateInvariantReader,
    PostgresH5TargetedRecaptureRepository,
)
from services.api.app.infrastructure.db.restoration_attempt_repository import (
    RestorationAttemptPersistenceError,
)
from services.api.tests.test_v2_control_plane_repositories import FakeConnection, FakePool

NOW = datetime(2026, 9, 12, 20, tzinfo=UTC)


def _request():
    return build_evidence_request(
        work_item_id="work-h5",
        attempt_id="attempt-h5",
        task_id="task-h5",
        rule_id="rule-h5",
        rule_code="h5_popup",
        target_capture_id="capture-target",
        reason_code="BASE_PAGE_OCCLUDED",
        evidence_goal="关闭弹窗后补采底层页面",
        required_artifact_types=("screenshot", "dom", "visible_text"),
        session_dependency=EvidenceSessionDependency.ACTIVE_SESSION,
        replay_policy=EvidenceReplayPolicy.SAME_SURFACE_ONLY,
        required_equivalence=EvidenceRequiredEquivalence.SAME_SURFACE_CONTINUATION,
        side_effect_policy=EvidenceSideEffectPolicy.READ_ONLY,
        target_invariants={
            "environment": {"surface_kind": "web"},
            "business": {"page_type": "home"},
            "structure": {"layer_target": "base_page"},
        },
        minimum_source_grade=EvidenceSourceGrade.A1,
        expires_at=NOW + timedelta(minutes=10),
    )


def _snapshot_request():
    return build_evidence_request(
        work_item_id="work-h5-snapshot",
        attempt_id="attempt-h5-snapshot",
        task_id="task-h5",
        rule_id="rule-h5-snapshot",
        rule_code="h5_snapshot",
        target_capture_id="capture-target",
        reason_code="EVIDENCE_INSUFFICIENT",
        evidence_goal="回到目标业务页补采截图",
        required_artifact_types=("screenshot", "dom"),
        session_dependency=EvidenceSessionDependency.SNAPSHOT,
        replay_policy=EvidenceReplayPolicy.NEW_SESSION_ALLOWED,
        required_equivalence=EvidenceRequiredEquivalence.LOGICAL_EQUIVALENT,
        side_effect_policy=EvidenceSideEffectPolicy.READ_ONLY,
        target_invariants={
            "environment": {"surface_kind": "web"},
            "business": {"page_type": "home"},
        },
        minimum_source_grade=EvidenceSourceGrade.C,
        expires_at=NOW + timedelta(minutes=10),
    )


async def test_claimed_request_reference_does_not_repeat_initial_admission() -> None:
    connection = SimpleNamespace()

    await H5ClaimedEvidenceRequestReference().upsert_in_transaction(
        connection,
        request=_request(),
    )


async def test_h5_plan_pins_current_capture_without_navigation() -> None:
    contexts = SimpleNamespace(
        read=AsyncMock(
            return_value=H5TargetedRecaptureContext(
                task_id="task-h5",
                agent_run_id="run-h5",
                finalization_epoch=4,
                session_id="h5:run-h5",
                current_capture_id="capture-target",
            )
        )
    )

    resolution = await H5TargetedRecapturePlanService(
        contexts=contexts,
        clock=lambda: NOW,
    ).resolve(
        request=_request(),
        agent_run_id="run-h5",
        finalization_epoch=4,
    )

    assert resolution.executable
    assert resolution.plan is not None
    assert resolution.plan.strategy is RestorationStrategy.ORIGINAL_SURFACE_PIN
    assert resolution.plan.source_session_id == "h5:run-h5"
    assert resolution.plan.normalization_step_ids == ()


async def test_h5_plan_rejects_page_drift_instead_of_navigating() -> None:
    page_graph = SimpleNamespace(get_recovery_route=AsyncMock())
    contexts = SimpleNamespace(
        read=AsyncMock(
            return_value=H5TargetedRecaptureContext(
                task_id="task-h5",
                agent_run_id="run-h5",
                finalization_epoch=4,
                session_id="h5:run-h5",
                current_capture_id="capture-other",
            )
        )
    )

    resolution = await H5TargetedRecapturePlanService(
        contexts=contexts,
        page_graph=page_graph,
        clock=lambda: NOW,
    ).resolve(
        request=_request(),
        agent_run_id="run-h5",
        finalization_epoch=4,
    )

    assert not resolution.executable
    assert resolution.reason_code == "H5_ACTIVE_SESSION_TARGET_DRIFTED"
    page_graph.get_recovery_route.assert_not_awaited()


async def test_h5_plan_reports_context_scope_mismatch() -> None:
    contexts = SimpleNamespace(
        read=AsyncMock(
            return_value=H5TargetedRecaptureContext(
                task_id="task-h5",
                agent_run_id="other-run",
                finalization_epoch=4,
                session_id="h5:other-run",
                current_capture_id="capture-target",
            )
        )
    )

    with pytest.raises(EvidenceRecoveryReplayError) as raised:
        await H5TargetedRecapturePlanService(
            contexts=contexts,
            clock=lambda: NOW,
        ).resolve(
            request=_request(),
            agent_run_id="run-h5",
            finalization_epoch=4,
        )

    assert raised.value.reason_code == "H5_RECAPTURE_CONTEXT_SCOPE_MISMATCH"
    assert raised.value.retryable is False


@pytest.mark.parametrize("policy,equivalence,identity,url,allowed", [
    (EvidenceReplayPolicy.SAME_CONTEXT_ALLOWED, EvidenceRequiredEquivalence.LOGICAL_EQUIVALENT,
     "logical:home", "https://m.example.test/home", True),
    (EvidenceReplayPolicy.SAME_SURFACE_ONLY, EvidenceRequiredEquivalence.LOGICAL_EQUIVALENT,
     "logical:home", "https://m.example.test/home", False),
    (EvidenceReplayPolicy.SAME_CONTEXT_ALLOWED, EvidenceRequiredEquivalence.ORIGINAL_ATOMIC,
     "logical:home", "https://m.example.test/home", False),
    (EvidenceReplayPolicy.SAME_CONTEXT_ALLOWED, EvidenceRequiredEquivalence.LOGICAL_EQUIVALENT,
     None, "https://m.example.test/home", False),
    (EvidenceReplayPolicy.SAME_CONTEXT_ALLOWED, EvidenceRequiredEquivalence.LOGICAL_EQUIVALENT,
     "logical:other", "https://m.example.test/home", False),
    (EvidenceReplayPolicy.SAME_CONTEXT_ALLOWED, EvidenceRequiredEquivalence.LOGICAL_EQUIVALENT,
     "logical:home", "https://m.example.test/other", False),
])
async def test_h5_active_recapture_respects_explicit_logical_equivalence(
    policy, equivalence, identity, url, allowed,
) -> None:
    original = _request()
    request = build_evidence_request(
        work_item_id=original.work_item_id, attempt_id=original.attempt_id,
        task_id=original.task_id, rule_id=original.rule_id, rule_code=original.rule_code,
        target_capture_id=original.target_capture_id, reason_code=original.reason_code,
        evidence_goal=original.evidence_goal,
        required_artifact_types=original.required_artifact_types,
        session_dependency=original.session_dependency, replay_policy=policy,
        required_equivalence=equivalence, side_effect_policy=original.side_effect_policy,
        target_invariants=original.target_invariants,
        minimum_source_grade=original.minimum_source_grade, expires_at=original.expires_at,
    )
    context = H5TargetedRecaptureContext(
        task_id="task-h5", agent_run_id="run-h5", finalization_epoch=4,
        session_id="h5:run-h5", current_capture_id="capture-new-frame",
        target_identity="logical:home", current_identity=identity,
        target_url="https://m.example.test/home", current_url=url,
    )
    resolution = await H5TargetedRecapturePlanService(
        contexts=SimpleNamespace(read=AsyncMock(return_value=context)), clock=lambda: NOW,
    ).resolve(request=request, agent_run_id="run-h5", finalization_epoch=4)
    assert resolution.executable is allowed
    if allowed:
        assert resolution.plan.strategy is RestorationStrategy.ORIGINAL_SURFACE_PIN
    else:
        assert resolution.reason_code == "H5_ACTIVE_SESSION_TARGET_DRIFTED"


async def test_h5_snapshot_plan_pins_confirmed_equivalent_current_surface() -> None:
    contexts = SimpleNamespace(
        read=AsyncMock(
            return_value=H5TargetedRecaptureContext(
                task_id="task-h5",
                agent_run_id="run-h5",
                finalization_epoch=4,
                session_id="h5:run-h5",
                current_capture_id="capture-current",
                target_state_id="capture-target",
                target_identity="logical:home",
                current_identity="logical:home",
                target_url="https://m.example.test/home",
                current_url="https://m.example.test/home",
            )
        )
    )

    resolution = await H5TargetedRecapturePlanService(
        contexts=contexts,
        clock=lambda: NOW,
    ).resolve(
        request=_snapshot_request(),
        agent_run_id="run-h5",
        finalization_epoch=4,
    )

    assert resolution.executable
    assert resolution.plan is not None
    assert resolution.plan.strategy is RestorationStrategy.ORIGINAL_SURFACE_PIN
    assert resolution.plan.normalization_step_ids == ()


@pytest.mark.parametrize("active_equivalent", [False, True])
async def test_h5_snapshot_plan_uses_trusted_page_graph_route(active_equivalent) -> None:
    original = _snapshot_request()
    request = build_evidence_request(
        work_item_id=original.work_item_id, attempt_id=original.attempt_id,
        task_id=original.task_id, rule_id=original.rule_id, rule_code=original.rule_code,
        target_capture_id=original.target_capture_id, reason_code=original.reason_code,
        evidence_goal=original.evidence_goal,
        required_artifact_types=original.required_artifact_types,
        session_dependency=(EvidenceSessionDependency.ACTIVE_SESSION if active_equivalent
                            else original.session_dependency),
        replay_policy=(EvidenceReplayPolicy.SAME_CONTEXT_ALLOWED if active_equivalent
                       else original.replay_policy),
        required_equivalence=original.required_equivalence,
        side_effect_policy=original.side_effect_policy,
        target_invariants=original.target_invariants,
        minimum_source_grade=original.minimum_source_grade, expires_at=original.expires_at,
    )
    contexts = SimpleNamespace(
        read=AsyncMock(
            return_value=H5TargetedRecaptureContext(
                task_id="task-h5",
                agent_run_id="run-h5",
                finalization_epoch=4,
                session_id="h5:run-h5",
                current_capture_id="capture-current",
                target_state_id="capture-target",
                target_identity="logical:home",
                current_identity="logical:detail",
                target_url="https://m.example.test/home",
                current_url="https://m.example.test/detail/1",
            )
        )
    )
    page_graph = SimpleNamespace(
        get_recovery_route=AsyncMock(
            return_value={
                "protocol_version": "page_graph_recovery.v1",
                "status": "available",
                "task_id": "task-h5",
                "agent_run_id": "run-h5",
                "finalization_epoch": 4,
                "target_platform": "h5",
                "requires_stepwise_reverification": True,
                "routes": [
                    {
                        "source_state_id": "capture-current",
                        "target_state_id": "capture-target",
                        "target_page_key": "logical:home",
                        "steps": [
                            {
                                "transition_id": "transition-back",
                                "source_state_id": "capture-current",
                                "target_state_id": "capture-target",
                                "status": "completed",
                                "proof": "trusted",
                                "risk_level": "low",
                                "replay_eligible": True,
                                "action_kind": "back",
                            }
                        ],
                    }
                ],
            }
        )
    )

    resolution = await H5TargetedRecapturePlanService(
        contexts=contexts,
        page_graph=page_graph,
        clock=lambda: NOW,
    ).resolve(
        request=request,
        agent_run_id="run-h5",
        finalization_epoch=4,
    )

    assert resolution.executable
    assert resolution.plan is not None
    assert resolution.plan.strategy is RestorationStrategy.SAME_CONTEXT_NEW_SURFACE
    assert resolution.plan.normalization_step_ids == ("transition-back",)
    assert resolution.plan.source_session_id == resolution.plan.target_session_id


async def test_h5_plan_settles_contract_that_cannot_use_recovery() -> None:
    request = build_evidence_request(
        work_item_id="work-h5-atomic",
        attempt_id="attempt-h5-atomic",
        task_id="task-h5",
        rule_id="rule-h5-atomic",
        rule_code="h5_atomic",
        target_capture_id="capture-target",
        reason_code="ATOMIC_EVIDENCE_MISSING",
        evidence_goal="要求原动作原子证据",
        required_artifact_types=("screenshot",),
        session_dependency=EvidenceSessionDependency.ACTIVE_SESSION,
        replay_policy=EvidenceReplayPolicy.FORBIDDEN,
        required_equivalence=EvidenceRequiredEquivalence.ORIGINAL_ATOMIC,
        side_effect_policy=EvidenceSideEffectPolicy.READ_ONLY,
        target_invariants={"environment": {"surface_kind": "web"}},
        minimum_source_grade=EvidenceSourceGrade.A0,
        expires_at=NOW + timedelta(minutes=10),
    )
    contexts = SimpleNamespace(
        read=AsyncMock(
            return_value=H5TargetedRecaptureContext(
                task_id="task-h5",
                agent_run_id="run-h5",
                finalization_epoch=4,
                session_id="h5:run-h5",
                current_capture_id="capture-target",
            )
        )
    )

    resolution = await H5TargetedRecapturePlanService(
        contexts=contexts,
        clock=lambda: NOW,
    ).resolve(
        request=request,
        agent_run_id="run-h5",
        finalization_epoch=4,
    )

    assert not resolution.executable
    assert resolution.reason_code == "H5_RECAPTURE_REQUEST_CONSTRAINT_UNSUPPORTED"


async def test_h5_runtime_finalizes_target_manifest_before_trusted_capture_read() -> None:
    events: list[str] = []
    capture = CaptureRef(
        task_id="task-h5",
        session_id="h5:run-h5",
        capture_id="capture-target",
        surface_kind=SurfaceKind.WEB,
        capture_kind=CaptureKind.PAGE,
        role="after_action",
    )

    class ManifestFinalizer:
        async def finalize_capture(self, **kwargs) -> None:
            assert kwargs == {
                "task_id": "task-h5",
                "capture_id": "capture-target",
            }
            events.append("finalize")

    class Captures:
        async def get_verified_capture(self, **kwargs):
            assert kwargs == {"capture_id": "capture-target"}
            events.append("read")
            return SimpleNamespace(capture=capture)

    class Snapshots:
        async def read(self, **kwargs):
            assert kwargs == {"capture": capture}
            events.append("snapshot")
            return object()

    expected = object()

    class Restoration:
        async def restore_and_publish(self, **kwargs):
            events.append("restore")
            return expected

    runtime = H5TargetedRecaptureRuntime(
        captures=Captures(),
        snapshots=Snapshots(),
        planning=SimpleNamespace(),
        restoration_attempts=SimpleNamespace(),
        restoration=Restoration(),
        agent_run_id="run-h5",
        finalization_epoch=4,
        manifest_finalizer=ManifestFinalizer(),
    )

    result = await runtime.restore_and_publish(
        request=_request(),
        plan=SimpleNamespace(source_session_id="h5:run-h5"),
        budget=ExplorationBudget(
            max_actions=1,
            remaining_actions=1,
            max_depth=1,
            remaining_duration_ms=1_000,
        ),
        attempt_no=1,
        request_lease=object(),
    )

    assert result is expected
    assert events == ["finalize", "read", "snapshot", "restore"]


async def test_h5_runtime_reports_target_capture_scope_mismatch() -> None:
    runtime = H5TargetedRecaptureRuntime(
        captures=SimpleNamespace(
            get_verified_capture=AsyncMock(
                return_value=SimpleNamespace(
                    capture=CaptureRef(
                        task_id="other-task",
                        session_id="h5:run-h5",
                        capture_id="capture-target",
                        surface_kind=SurfaceKind.WEB,
                        capture_kind=CaptureKind.PAGE,
                        role="after_action",
                    )
                )
            )
        ),
        snapshots=SimpleNamespace(),
        planning=SimpleNamespace(),
        restoration_attempts=SimpleNamespace(),
        restoration=SimpleNamespace(),
        agent_run_id="run-h5",
        finalization_epoch=4,
    )

    with pytest.raises(EvidenceRecoveryReplayError) as raised:
        await runtime.restore_and_publish(
            request=_request(),
            plan=SimpleNamespace(source_session_id="h5:run-h5"),
            budget=ExplorationBudget(
                max_actions=1,
                remaining_actions=1,
                max_depth=1,
                remaining_duration_ms=1_000,
            ),
            attempt_no=1,
            request_lease=object(),
        )

    assert raised.value.reason_code == "H5_RECAPTURE_TARGET_CAPTURE_SCOPE_MISMATCH"
    assert raised.value.retryable is False


@pytest.mark.parametrize("cleanup_fails", [False, True])
@pytest.mark.parametrize("attempt_state", ["running", "terminal", "missing", "other_plan"])
async def test_h5_runtime_cancellation_settles_its_running_restoration(
    cleanup_fails, attempt_state,
):
    import asyncio

    from services.api.app.application.restoration import (
        RestorationCoordinator,
        new_running_restoration_attempt,
    )
    from services.api.app.application.targeted_recapture_restoration import (
        TargetedRecaptureRestorationService,
    )
    from services.api.app.domain.inspection import RestorationAttemptStatus

    request = _request()
    plan = build_restoration_plan(
        request=request, source_session_id="h5:run-h5", target_session_id="h5:run-h5",
        surface_kind=SurfaceKind.WEB, strategy=RestorationStrategy.ORIGINAL_SURFACE_PIN,
        deadline=NOW + timedelta(minutes=5),
    )
    running = new_running_restoration_attempt(plan=plan, attempt_no=1, started_at=NOW)
    persisted = running
    if attempt_state == "terminal":
        persisted = replace(running, status=RestorationAttemptStatus.FAILED,
                            finished_at=NOW, error_code="既有终态")
    elif attempt_state == "missing":
        persisted = None
    elif attempt_state == "other_plan":
        other_plan = build_restoration_plan(
            request=request, source_session_id="h5:run-h5", target_session_id="h5:run-h5",
            surface_kind=SurfaceKind.WEB, strategy=RestorationStrategy.ORIGINAL_SURFACE_PIN,
            deadline=NOW + timedelta(minutes=4),
        )
        persisted = new_running_restoration_attempt(plan=other_plan, attempt_no=1, started_at=NOW)
    repository = SimpleNamespace(
        start=AsyncMock(return_value=running), get=AsyncMock(return_value=persisted),
        complete=AsyncMock(side_effect=RuntimeError("版本已变化") if cleanup_fails else None),
    )
    publisher = SimpleNamespace(publish=AsyncMock())
    executor = SimpleNamespace(restore=AsyncMock(side_effect=asyncio.CancelledError()))
    capture = CaptureRef(task_id=request.task_id, session_id=plan.source_session_id,
                         capture_id=request.target_capture_id, surface_kind=SurfaceKind.WEB,
                         capture_kind=CaptureKind.PAGE, role="after_action")
    snapshot = StateInvariantSnapshot(
        task_id=request.task_id, capture_id=capture.capture_id, session_id=capture.session_id,
        surface_kind=SurfaceKind.WEB, environment={}, business={}, semantic={}, structure={},
        captured_at=NOW,
    )
    runtime = H5TargetedRecaptureRuntime(
        captures=SimpleNamespace(get_verified_capture=AsyncMock(
            return_value=SimpleNamespace(capture=capture))),
        snapshots=SimpleNamespace(read=AsyncMock(return_value=snapshot)),
        planning=SimpleNamespace(), restoration_attempts=repository,
        restoration=TargetedRecaptureRestorationService(
            coordinator=RestorationCoordinator(repository=repository, executor=executor,
                                              equivalence_gate=H5StateEquivalenceGate()),
            probe_publisher=publisher,
        ), agent_run_id="run-h5", finalization_epoch=4,
    )
    with pytest.raises(asyncio.CancelledError):
        await runtime.restore_and_publish(
            request=request, plan=plan, attempt_no=1, request_lease=object(),
            budget=ExplorationBudget(max_actions=1, remaining_actions=1, max_depth=1,
                                     remaining_duration_ms=1000),
        )
    if attempt_state == "running":
        repository.complete.assert_awaited_once()
        terminal = repository.complete.await_args.kwargs["attempt"]
        assert terminal.status is RestorationAttemptStatus.FAILED
        assert terminal.error_code == "H5_RESTORATION_INTERRUPTED"
        assert terminal.plan == plan
        assert repository.complete.await_args.kwargs["expected_row_version"] == running.row_version
    else:
        repository.complete.assert_not_awaited()
    publisher.publish.assert_not_awaited()


async def test_h5_recovery_snapshot_finalizes_restored_capture_before_read() -> None:
    events: list[str] = []
    expected = StateInvariantSnapshot(
        task_id="task-h5",
        capture_id="capture-restored",
        session_id="h5:run-h5",
        surface_kind=SurfaceKind.WEB,
        environment={"surface_kind": "web"},
        business={"page_type": "home"},
        semantic={"page_state": "stable"},
        structure={},
        captured_at=NOW,
    )

    class ManifestFinalizer:
        async def finalize_capture(self, **kwargs) -> None:
            assert kwargs == {
                "task_id": "task-h5",
                "capture_id": "capture-restored",
            }
            events.append("finalize")

    class Delegate:
        async def read(self, **kwargs):
            assert kwargs == {
                "task_id": "task-h5",
                "session_id": "h5:run-h5",
                "surface_kind": SurfaceKind.WEB,
                "capture_id": "capture-restored",
            }
            events.append("read")
            return expected

    result = await H5FinalizingRecoverySnapshotReader(
        delegate=Delegate(),
        manifest_finalizer=ManifestFinalizer(),
    ).read(
        task_id="task-h5",
        session_id="h5:run-h5",
        surface_kind=SurfaceKind.WEB,
        capture_id="capture-restored",
    )

    assert result is expected
    assert events == ["finalize", "read"]


async def test_h5_consumer_preserves_nonretryable_capture_error_code() -> None:
    request = _request()
    lease = EvidenceRequestProcessingLease(
        request=request,
        lease_owner="worker-h5:recapture",
        lease_token="lease-h5",
        finalization_epoch=4,
        row_version=2,
        attempt_count=1,
        max_attempts=3,
    )
    requests = SimpleNamespace(
        claim_next=AsyncMock(
            return_value=EvidenceRequestClaimResult(
                disposition=EvidenceRequestClaimDisposition.CLAIMED,
                lease=lease,
            )
        ),
        settle_lease=AsyncMock(return_value=3),
    )

    class Runtime:
        async def resume_equivalent_publication(self, **kwargs):
            del kwargs
            return None

        async def resolve_plan(self, **kwargs):
            return SimpleNamespace(plan=build_restoration_plan(
                request=request, source_session_id="h5:run-h5", target_session_id="h5:run-h5",
                surface_kind=SurfaceKind.WEB, strategy=RestorationStrategy.ORIGINAL_SURFACE_PIN,
                deadline=kwargs["deadline"],
            ))

        async def restore_and_publish(self, **kwargs):
            del kwargs
            raise PageCaptureRecoveryError(
                "Manifest 校验失败",
                error_code="H5_ACTION_RECOVERY_CAPTURE_INVALID",
            )

    consumer = H5TargetedRecaptureConsumer(
        requests=requests,
        runtime_factory=SimpleNamespace(build=lambda **_kwargs: Runtime()),
        policy=build_lab_supplemental_evidence_policy(),
        lease_owner="worker-h5:recapture",
        clock=lambda: NOW,
    )

    outcome = await consumer.consume_next(
        task_id="task-h5",
        agent_run_id="run-h5",
        finalization_epoch=4,
        budget=ExplorationBudget(
            max_actions=1,
            remaining_actions=1,
            max_depth=1,
            remaining_duration_ms=1_000,
        ),
        deadline=NOW + timedelta(minutes=1),
    )

    assert outcome.disposition is H5TargetedRecaptureDisposition.UNFULFILLABLE
    expected_reason = "H5_ACTION_RECOVERY_CAPTURE_INVALID"
    assert outcome.reason_code == expected_reason
    assert requests.claim_next.await_args.kwargs["session_dependencies"] == (
        EvidenceSessionDependency.ACTIVE_SESSION,
    )
    command = requests.settle_lease.await_args.kwargs["command"]
    assert command.resolution_reason == expected_reason


async def test_h5_consumer_preserves_restoration_persistence_reason_code() -> None:
    request = _request()
    lease = EvidenceRequestProcessingLease(
        request=request,
        lease_owner="worker-h5:recapture",
        lease_token="lease-h5",
        finalization_epoch=4,
        row_version=2,
        attempt_count=1,
        max_attempts=3,
    )
    requests = SimpleNamespace(
        claim_next=AsyncMock(
            return_value=EvidenceRequestClaimResult(
                disposition=EvidenceRequestClaimDisposition.CLAIMED,
                lease=lease,
            )
        ),
        release_lease=AsyncMock(
            return_value=EvidenceRequestLeaseReleaseResult(
                disposition=EvidenceRequestLeaseReleaseDisposition.RETRY_SCHEDULED,
                row_version=3,
            )
        ),
    )

    class Runtime:
        async def resume_equivalent_publication(self, **kwargs):
            del kwargs
            return None

        async def resolve_plan(self, **kwargs):
            return SimpleNamespace(plan=build_restoration_plan(
                request=request, source_session_id="h5:run-h5", target_session_id="h5:run-h5",
                surface_kind=SurfaceKind.WEB, strategy=RestorationStrategy.ORIGINAL_SURFACE_PIN,
                deadline=kwargs["deadline"],
            ))

        async def restore_and_publish(self, **kwargs):
            del kwargs
            raise RestorationAttemptPersistenceError(
                "任务仍在排空",
                reason_code="TASK_NOT_RUNNABLE",
            )

    consumer = H5TargetedRecaptureConsumer(
        requests=requests,
        runtime_factory=SimpleNamespace(build=lambda **_kwargs: Runtime()),
        policy=build_lab_supplemental_evidence_policy(),
        lease_owner="worker-h5:recapture",
        clock=lambda: NOW,
    )

    outcome = await consumer.consume_next(
        task_id="task-h5",
        agent_run_id="run-h5",
        finalization_epoch=4,
        budget=ExplorationBudget(
            max_actions=1,
            remaining_actions=1,
            max_depth=1,
            remaining_duration_ms=1_000,
        ),
        deadline=NOW + timedelta(minutes=1),
    )

    assert outcome.disposition is H5TargetedRecaptureDisposition.RETRY_SCHEDULED
    assert outcome.reason_code == "TASK_NOT_RUNNABLE"
    command = requests.release_lease.await_args.kwargs["command"]
    assert command.reason_code == "TASK_NOT_RUNNABLE"


async def test_h5_consumer_does_not_mislabel_task_deadline_as_request_expiry() -> None:
    request = _request()
    lease = EvidenceRequestProcessingLease(
        request=request,
        lease_owner="worker-h5:recapture",
        lease_token="lease-h5",
        finalization_epoch=4,
        row_version=2,
        attempt_count=1,
        max_attempts=3,
    )
    requests = SimpleNamespace(
        claim_next=AsyncMock(
            return_value=EvidenceRequestClaimResult(
                disposition=EvidenceRequestClaimDisposition.CLAIMED,
                lease=lease,
            )
        ),
        settle_lease=AsyncMock(return_value=3),
    )
    consumer = H5TargetedRecaptureConsumer(
        requests=requests,
        runtime_factory=SimpleNamespace(
            build=lambda **kwargs: (_ for _ in ()).throw(
                AssertionError(f"截止后不得构造 Runtime：{kwargs}")
            )
        ),
        policy=build_lab_supplemental_evidence_policy(),
        lease_owner="worker-h5:recapture",
        clock=lambda: NOW,
    )

    outcome = await consumer.consume_next(
        task_id="task-h5",
        agent_run_id="run-h5",
        finalization_epoch=4,
        budget=ExplorationBudget(
            max_actions=1,
            remaining_actions=1,
            max_depth=1,
            remaining_duration_ms=1_000,
        ),
        deadline=NOW,
    )

    assert outcome.disposition is H5TargetedRecaptureDisposition.UNFULFILLABLE
    command = requests.settle_lease.await_args.kwargs["command"]
    assert command.target_status is EvidenceRequestStatus.UNFULFILLABLE
    assert command.resolution_reason == "H5_RECAPTURE_DEADLINE_EXHAUSTED"


@pytest.mark.parametrize("initialization_fails", [False, True])
async def test_h5_consumer_releases_lease_when_runtime_or_projection_is_not_ready(
    initialization_fails: bool,
) -> None:
    requests = SimpleNamespace(
        claim_next=AsyncMock(return_value=EvidenceRequestClaimResult(
            disposition=EvidenceRequestClaimDisposition.CLAIMED,
            lease=EvidenceRequestProcessingLease(
                request=_request(), lease_owner="worker-h5:recapture",
                lease_token="lease-h5", finalization_epoch=4, row_version=2,
                attempt_count=1, max_attempts=3,
            ),
        )),
        release_lease=AsyncMock(return_value=EvidenceRequestLeaseReleaseResult(
            disposition=EvidenceRequestLeaseReleaseDisposition.RETRY_SCHEDULED,
            row_version=3,
        )),
        settle_lease=AsyncMock(),
    )
    runtime = SimpleNamespace(
        resume_equivalent_publication=AsyncMock(return_value=None),
        resolve_plan=AsyncMock(return_value=SimpleNamespace(
            plan=None, reason_code="H5_TARGET_GRAPH_STATE_UNAVAILABLE", retryable=True,
        )),
        restore_and_publish=AsyncMock(),
    )

    def build(**_kwargs):
        if initialization_fails:
            raise ConnectionError("补采组件暂不可用")
        return runtime

    consumer = H5TargetedRecaptureConsumer(
        requests=requests, runtime_factory=SimpleNamespace(build=build),
        policy=build_lab_supplemental_evidence_policy(),
        lease_owner="worker-h5:recapture", clock=lambda: NOW,
    )
    outcome = await consumer.consume_next(
        task_id="task-h5", agent_run_id="run-h5", finalization_epoch=4,
        budget=ExplorationBudget(max_actions=1, remaining_actions=1, max_depth=1,
                                 remaining_duration_ms=1_000),
        deadline=NOW + timedelta(minutes=1),
    )
    assert outcome.disposition is H5TargetedRecaptureDisposition.RETRY_SCHEDULED
    requests.release_lease.assert_awaited_once()
    requests.settle_lease.assert_not_awaited()
    runtime.restore_and_publish.assert_not_awaited()
    command = requests.release_lease.await_args.kwargs["command"]
    assert command.lease_token == "lease-h5"
    assert command.row_version == 2
    assert command.next_attempt_at > NOW


@pytest.mark.parametrize("release_failed", [False, True])
async def test_h5_consumer_releases_owned_lease_on_workflow_cancellation(release_failed):
    import asyncio

    requests = SimpleNamespace(
        claim_next=AsyncMock(return_value=EvidenceRequestClaimResult(
            disposition=EvidenceRequestClaimDisposition.CLAIMED,
            lease=EvidenceRequestProcessingLease(
                request=_request(), lease_owner="worker-h5:recapture",
                lease_token="lease-h5", finalization_epoch=4, row_version=2,
                attempt_count=1, max_attempts=3,
            ),
        )),
        release_lease=AsyncMock(return_value=EvidenceRequestLeaseReleaseResult(
            disposition=EvidenceRequestLeaseReleaseDisposition.RETRY_SCHEDULED, row_version=3,
        )),
        settle_lease=AsyncMock(),
    )
    runtime = SimpleNamespace(resume_equivalent_publication=AsyncMock(
        side_effect=asyncio.CancelledError,
    ))
    if release_failed:
        requests.release_lease.side_effect = RuntimeError("lease fence changed")
    consumer = H5TargetedRecaptureConsumer(
        requests=requests, runtime_factory=SimpleNamespace(build=lambda **kwargs: runtime),
        policy=build_lab_supplemental_evidence_policy(),
        lease_owner="worker-h5:recapture", clock=lambda: NOW,
    )
    with pytest.raises(asyncio.CancelledError):
        await consumer.consume_next(
            task_id="task-h5", agent_run_id="run-h5", finalization_epoch=4,
            budget=ExplorationBudget(max_actions=1, remaining_actions=1, max_depth=1,
                                     remaining_duration_ms=1_000),
            deadline=NOW + timedelta(minutes=1),
        )
    requests.release_lease.assert_awaited_once()
    assert requests.release_lease.await_args.kwargs["command"].lease_token == "lease-h5"


@pytest.mark.parametrize("checkpoint", [True, False])
async def test_mixed_snapshot_deferral_does_not_skip_final_drain(checkpoint):
    healthy = SimpleNamespace(request_id="healthy-snapshot")
    unavailable = SimpleNamespace(request_id="unavailable-snapshot")
    consumer = SimpleNamespace(
        can_defer_snapshot_requests=AsyncMock(side_effect=[True, False]),
        consume_next=AsyncMock(return_value=SimpleNamespace(
            disposition=H5TargetedRecaptureDisposition.EMPTY,
        )),
    )
    reader = SimpleNamespace(
        read_h5_epoch=AsyncMock(return_value=4),
        list_deferred_snapshot_requests=AsyncMock(return_value=(healthy, unavailable)),
    )
    drain = H5TargetedRecaptureDrainService(
        consumer=consumer, scope_reader=reader,
        budget=ExplorationBudget(max_actions=1, remaining_actions=1, max_depth=1,
                                 remaining_duration_ms=1000),
    )
    await drain.drain_one(
        task_id="task-h5", agent_run_id="run-h5", deadline=NOW + timedelta(minutes=1),
        include_snapshot=True, defer_recoverable_snapshots=checkpoint,
    )
    arguments = consumer.consume_next.await_args.kwargs
    if checkpoint:
        assert arguments["deferred_snapshot_request_ids"] == (healthy.request_id,)
        reader.list_deferred_snapshot_requests.assert_awaited_once_with(
            task_id="task-h5", agent_run_id="run-h5", include_ready=True,
        )
    else:
        assert "deferred_snapshot_request_ids" not in arguments
        reader.list_deferred_snapshot_requests.assert_not_awaited()


async def test_h5_plan_marks_missing_live_context_as_retryable() -> None:
    resolution = await H5TargetedRecapturePlanService(
        contexts=SimpleNamespace(read=AsyncMock(return_value=None)), clock=lambda: NOW,
    ).resolve(request=_request(), agent_run_id="run-h5", finalization_epoch=4)
    assert resolution.reason_code == "H5_RECAPTURE_CONTEXT_UNAVAILABLE"
    assert resolution.retryable is True


@pytest.mark.parametrize("disposition,progressed,should_wake", [
    (H5TargetedRecaptureDisposition.UNFULFILLABLE, True, True),
    (H5TargetedRecaptureDisposition.EXPIRED, True, True),
    (H5TargetedRecaptureDisposition.PROBE_PUBLISHED, True, False),
    (H5TargetedRecaptureDisposition.RETRY_SCHEDULED, False, False),
    (H5TargetedRecaptureDisposition.EMPTY, False, False),
])
async def test_h5_terminal_recapture_wakes_waiting_inspection_work(
    disposition, progressed, should_wake,
) -> None:
    consumer = SimpleNamespace(
        consume_next=AsyncMock(
            return_value=SimpleNamespace(
                disposition=disposition
            )
        )
    )
    wakeup = SimpleNamespace(reopen_waiting_evidence=AsyncMock(return_value=0))
    drain = H5TargetedRecaptureDrainService(
        consumer=consumer,
        scope_reader=SimpleNamespace(read_h5_epoch=AsyncMock(return_value=4)),
        budget=ExplorationBudget(
            max_actions=1,
            remaining_actions=1,
            max_depth=1,
            remaining_duration_ms=1_000,
        ),
        inspection_wakeup=wakeup,
    )

    result = await drain.drain_one(
        task_id="task-h5",
        agent_run_id="run-h5",
        deadline=NOW + timedelta(minutes=1),
        include_snapshot=True,
    )

    assert result is progressed
    assert consumer.consume_next.await_args.kwargs["include_snapshot"] is True
    if should_wake:
        wakeup.reopen_waiting_evidence.assert_awaited_once_with(task_id="task-h5")
    else:
        wakeup.reopen_waiting_evidence.assert_not_awaited()


@pytest.mark.parametrize("strategy,steps,dependency,allowed", [
    (RestorationStrategy.SAME_CONTEXT_NEW_SURFACE, ("trusted-edge",), "snapshot", True),
    (RestorationStrategy.ORIGINAL_SURFACE_PIN, (), "snapshot", False),
    (RestorationStrategy.SAME_CONTEXT_NEW_SURFACE, (), "snapshot", False),
    (RestorationStrategy.SAME_CONTEXT_NEW_SURFACE, ("trusted-edge",), "active_session", False),
    (None, (), "snapshot", False),
])
async def test_only_routed_old_snapshot_can_defer_before_navigation(
    strategy, steps, dependency, allowed,
):
    request = _snapshot_request() if dependency == "snapshot" else _request()
    plan = None if strategy is None else SimpleNamespace(
        strategy=strategy, normalization_step_ids=steps,
    )
    runtime = SimpleNamespace(resolve_plan=AsyncMock(return_value=SimpleNamespace(plan=plan)))
    consumer = H5TargetedRecaptureConsumer(
        requests=object(), runtime_factory=SimpleNamespace(build=lambda **kwargs: runtime),
        policy=build_h5_supplemental_evidence_policy(enabled=True, h5_v2_mode="on"),
        lease_owner="h5-recapture", clock=lambda: NOW,
    )
    assert await consumer.can_defer_snapshot_requests(
        requests=(request,), agent_run_id="run-h5", finalization_epoch=4,
    ) is allowed


@pytest.mark.parametrize("ready", [True, False])
async def test_current_snapshot_deferral_requires_successful_readonly_replay_preflight(ready):
    request = _snapshot_request()
    preflight = AsyncMock(return_value=ready)
    runtime = SimpleNamespace(
        resolve_plan=AsyncMock(return_value=SimpleNamespace(plan=SimpleNamespace(
            strategy=RestorationStrategy.ORIGINAL_SURFACE_PIN, normalization_step_ids=(),
        ))), planning=SimpleNamespace(can_defer_snapshot=preflight),
    )
    consumer = H5TargetedRecaptureConsumer(
        requests=object(), runtime_factory=SimpleNamespace(build=lambda **kwargs: runtime),
        policy=build_h5_supplemental_evidence_policy(enabled=True, h5_v2_mode="on"),
        lease_owner="h5-recapture", clock=lambda: NOW,
    )
    assert await consumer.can_defer_snapshot_requests(
        requests=(request,), agent_run_id="run-h5", finalization_epoch=4,
    ) is ready
    preflight.assert_awaited_once_with(
        request=request, agent_run_id="run-h5", finalization_epoch=4,
    )


@pytest.mark.parametrize("reason,ready,dependency,allowed", [
    ("TRUSTED_RECOVERY_ROUTE_UNAVAILABLE", True, "snapshot", True),
    ("TRUSTED_RECOVERY_ROUTE_UNAVAILABLE", False, "snapshot", False),
    ("TRUSTED_RECOVERY_ROUTE_UNAVAILABLE", True, "active_session", False),
    ("PAGE_GRAPH_ROUTE_CONTRACT_INVALID", True, "snapshot", False),
    ("EVIDENCE_REQUEST_EXPIRED", True, "snapshot", False),
    ("H5_RECAPTURE_CONTEXT_UNAVAILABLE", True, "snapshot", False),
])
async def test_missing_current_route_defers_only_snapshot_with_verified_entry_replay(
    reason, ready, dependency, allowed,
):
    request = _snapshot_request() if dependency == "snapshot" else _request()
    preflight = AsyncMock(return_value=ready)
    runtime = SimpleNamespace(
        resolve_plan=AsyncMock(return_value=SimpleNamespace(plan=None, reason_code=reason)),
        planning=SimpleNamespace(can_defer_snapshot=preflight),
    )
    consumer = H5TargetedRecaptureConsumer(
        requests=object(), runtime_factory=SimpleNamespace(build=lambda **kwargs: runtime),
        policy=build_h5_supplemental_evidence_policy(enabled=True, h5_v2_mode="on"),
        lease_owner="h5-recapture", clock=lambda: NOW,
    )
    assert await consumer.can_defer_snapshot_requests(
        requests=(request,), agent_run_id="run-h5", finalization_epoch=4,
    ) is allowed
    if dependency == "snapshot" and reason == "TRUSTED_RECOVERY_ROUTE_UNAVAILABLE":
        preflight.assert_awaited_once_with(
            request=request, agent_run_id="run-h5", finalization_epoch=4,
        )
    else:
        preflight.assert_not_awaited()
    if dependency == "snapshot":
        runtime.resolve_plan.assert_awaited_once()
    else:
        runtime.resolve_plan.assert_not_awaited()


@pytest.mark.parametrize("expected,actual,allowed", [(1, 1, True), (2, 1, False), (51, 51, False)])
async def test_defer_requires_complete_current_request_inventory(expected, actual, allowed):
    consumer = SimpleNamespace(can_defer_snapshot_requests=AsyncMock(return_value=True))
    drain = H5TargetedRecaptureDrainService(
        consumer=consumer, scope_reader=SimpleNamespace(
            read_h5_epoch=AsyncMock(return_value=4),
            list_snapshot_checkpoint_requests=AsyncMock(return_value=tuple(SimpleNamespace(
                request=_snapshot_request(), finalization_epoch=4, blocks_current_surface=False,
            ) for _ in range(actual))),
        ), budget=ExplorationBudget(
            max_actions=1, remaining_actions=1, max_depth=1, remaining_duration_ms=1_000,
        ),
    )
    assert await drain.can_defer_snapshot_requests(
        task_id="task-h5", agent_run_id="run-h5", expected_request_count=expected,
    ) is allowed


async def test_h5_context_query_excludes_app_and_desktop_web_tasks() -> None:
    connection = FakeConnection(
        fetchrow_results=[
            {
                "task_id": "task-h5",
                "agent_run_id": "run-h5",
                "finalization_epoch": 4,
                "session_id": "h5:run-h5",
                "current_capture_id": "capture-target",
                "target_state_id": "capture-target",
                "target_identity": "logical:home",
                "current_identity": "logical:home",
                "target_url": "https://m.example.test/home",
                "current_url": "https://m.example.test/home",
            }
        ]
    )

    context = await PostgresH5TargetedRecaptureRepository(
        pool=FakePool(connection)
    ).read(request=_request())

    assert context is not None
    query = connection.calls[0][1]
    assert "lower(task.target_platform) = 'h5'" in query
    assert "capture.surface_kind = 'web'" in query
    assert "session.session_owner = 'h5-workflow'" in query
    assert "session.session_owner = 'h5-browser-recovery:' || capture.agent_run_id::text" in query
    assert "':recovery:[0-9a-f]{20}$'" in query
    assert "session.lease_owner IS NOT NULL" not in query
    assert "session.lease_token IS NOT NULL" not in query
    assert "session.lease_expires_at > now()" not in query
    assert "'active_session', 'snapshot'" in query
    assert connection.calls[0][2] == (_request().request_id, "task-h5")


@pytest.mark.parametrize("identity_status", [None, "confirmed", "ambiguous"])
async def test_h5_snapshot_reader_requires_h5_web_page_graph_state(identity_status) -> None:
    connection = FakeConnection(
        fetchrow_results=[
            {
                "task_id": "task-h5",
                "agent_run_id": "run-h5",
                "session_id": "h5:run-h5",
                "surface_kind": "web",
                "metadata": {"region_label": "首页主区域"},
                "captured_at": NOW,
                "url": "https://m.example.test/home",
                "page_type": "home",
                "page_state": "stable",
                "logical_page_key": "home",
                "state_fingerprint": "fingerprint-home",
                "is_overlay": False,
                "is_external": False,
                "state_payload": {"identity_status": identity_status},
            }
        ]
    )
    capture = SimpleNamespace(
        capture_id="capture-target",
        task_id="task-h5",
        session_id="h5:run-h5",
        surface_kind=SurfaceKind.WEB,
    )

    snapshot = await PostgresH5StateInvariantReader(
        pool=FakePool(connection)
    ).read(capture=capture)

    query = connection.calls[0][1]
    assert "lower(task.target_platform) = 'h5'" in query
    assert "capture.surface_kind = 'web'" in query
    assert "qc_page_graph_state" in query
    assert snapshot.business["page_type"] == "home"
    assert snapshot.semantic["region"] == "首页主区域"
    assert snapshot.structure["state_fingerprint"] == "fingerprint-home"
    assert snapshot.structure["identity_status"] == identity_status


async def test_h5_equivalence_adapts_any_layer_and_page_state_set() -> None:
    request = build_evidence_request(
        work_item_id="work-h5-set",
        attempt_id="attempt-h5-set",
        task_id="task-h5",
        rule_id="rule-h5-set",
        rule_code="h5_state_set",
        target_capture_id="capture-original",
        reason_code="TRUSTED_EVIDENCE_INVALID",
        evidence_goal="补采当前可接受状态",
        required_artifact_types=("screenshot",),
        session_dependency=EvidenceSessionDependency.ACTIVE_SESSION,
        replay_policy=EvidenceReplayPolicy.SAME_SURFACE_ONLY,
        required_equivalence=EvidenceRequiredEquivalence.SAME_SURFACE_CONTINUATION,
        side_effect_policy=EvidenceSideEffectPolicy.READ_ONLY,
        target_invariants={
            "environment": {"surface_kind": "web"},
            "semantic": {"page_state": ["stable", "ready"]},
            "structure": {"layer_target": "any"},
        },
        minimum_source_grade=EvidenceSourceGrade.A1,
        expires_at=NOW + timedelta(minutes=10),
    )
    plan = H5TargetedRecapturePlanService(
        contexts=SimpleNamespace(
            read=AsyncMock(
                return_value=H5TargetedRecaptureContext(
                    task_id="task-h5",
                    agent_run_id="run-h5",
                    finalization_epoch=4,
                    session_id="h5:run-h5",
                    current_capture_id="capture-original",
                )
            )
        ),
        clock=lambda: NOW,
    )

    resolution = await plan.resolve(
        request=request,
        agent_run_id="run-h5",
        finalization_epoch=4,
    )
    assert resolution.plan is not None
    original = StateInvariantSnapshot(
        task_id="task-h5",
        capture_id="capture-original",
        session_id="h5:run-h5",
        surface_kind=SurfaceKind.WEB,
        environment={"surface_kind": "web"},
        business={},
        semantic={"page_state": "stable"},
        structure={"layer_target": "base_page", "document_identity": "document-states",
                   "logical_state_fingerprint": "a" * 64},
        captured_at=NOW,
    )
    restored = StateInvariantSnapshot(
        task_id="task-h5",
        capture_id="capture-restored",
        session_id="h5:run-h5",
        surface_kind=SurfaceKind.WEB,
        environment={"surface_kind": "web"},
        business={},
        semantic={"page_state": "ready"},
        structure={"layer_target": "overlay", "document_identity": "document-states",
                   "logical_state_fingerprint": "a" * 64},
        captured_at=NOW + timedelta(seconds=1),
    )
    assessment = H5StateEquivalenceGate().assess(
        request=request,
        plan=resolution.plan,
        restoration_attempt_id="restoration-h5",
        original=original,
        restored=restored,
    )
    assert assessment.passed is True


async def test_h5_equivalence_treats_probe_region_and_target_layer_as_capture_goal() -> None:
    request = build_evidence_request(
        work_item_id="work-h5-region",
        attempt_id="attempt-h5-region",
        task_id="task-h5",
        rule_id="rule-h5-region",
        rule_code="h5_region",
        target_capture_id="capture-original",
        reason_code="EVIDENCE_INSUFFICIENT",
        evidence_goal="补采页面文案",
        required_artifact_types=("screenshot", "ui_snapshot"),
        session_dependency=EvidenceSessionDependency.ACTIVE_SESSION,
        replay_policy=EvidenceReplayPolicy.SAME_SURFACE_ONLY,
        required_equivalence=EvidenceRequiredEquivalence.LOGICAL_EQUIVALENT,
        side_effect_policy=EvidenceSideEffectPolicy.READ_ONLY,
        target_invariants={
            "environment": {"surface_kind": "web"},
            "business": {"page_type": "video_list"},
            "semantic": {
                "page_state": "popup",
                "region": "页面可见文案",
                "capture_roles": ["source"],
            },
            "structure": {"layer_target": "base_page"},
        },
        minimum_source_grade=EvidenceSourceGrade.B,
        expires_at=NOW + timedelta(minutes=10),
    )
    resolution = await H5TargetedRecapturePlanService(
        contexts=SimpleNamespace(
            read=AsyncMock(
                return_value=H5TargetedRecaptureContext(
                    task_id="task-h5",
                    agent_run_id="run-h5",
                    finalization_epoch=4,
                    session_id="h5:run-h5",
                    current_capture_id="capture-original",
                )
            )
        ),
        clock=lambda: NOW,
    ).resolve(
        request=request,
        agent_run_id="run-h5",
        finalization_epoch=4,
    )
    assert resolution.plan is not None
    original = StateInvariantSnapshot(
        task_id="task-h5",
        capture_id="capture-original",
        session_id="h5:run-h5",
        surface_kind=SurfaceKind.WEB,
        environment={"surface_kind": "web"},
        business={"page_type": "video_list"},
        semantic={"page_state": "popup"},
        structure={"layer_target": "overlay", "document_identity": "document-region"},
        captured_at=NOW,
    )
    restored = replace(
        original,
        capture_id="capture-restored",
        captured_at=NOW + timedelta(seconds=1),
    )

    assessment = H5StateEquivalenceGate().assess(
        request=request,
        plan=resolution.plan,
        restoration_attempt_id="restoration-h5-region",
        original=original,
        restored=restored,
    )

    assert assessment.passed is True
    assert all(item.matched for item in assessment.comparisons)


async def test_h5_equivalence_rejects_actual_layer_drift() -> None:
    request = _request()
    resolution = await H5TargetedRecapturePlanService(
        contexts=SimpleNamespace(
            read=AsyncMock(
                return_value=H5TargetedRecaptureContext(
                    task_id="task-h5",
                    agent_run_id="run-h5",
                    finalization_epoch=4,
                    session_id="h5:run-h5",
                    current_capture_id="capture-target",
                )
            )
        ),
        clock=lambda: NOW,
    ).resolve(
        request=request,
        agent_run_id="run-h5",
        finalization_epoch=4,
    )
    assert resolution.plan is not None
    original = StateInvariantSnapshot(
        task_id="task-h5",
        capture_id="capture-target",
        session_id="h5:run-h5",
        surface_kind=SurfaceKind.WEB,
        environment={"surface_kind": "web"},
        business={"page_type": "home"},
        semantic={"page_state": "stable"},
        structure={"layer_target": "overlay"},
        captured_at=NOW,
    )
    restored = replace(
        original,
        capture_id="capture-restored",
        structure={"layer_target": "base_page"},
        captured_at=NOW + timedelta(seconds=1),
    )

    assessment = H5StateEquivalenceGate().assess(
        request=request,
        plan=resolution.plan,
        restoration_attempt_id="restoration-h5-layer-drift",
        original=original,
        restored=restored,
    )

    assert assessment.passed is False
    assert any(
        item.key == "layer_target" and not item.matched
        for item in assessment.comparisons
    )


@pytest.mark.parametrize(
    "variation",
    ["cleared", "missing_request_state", "legacy_empty_context", "other_identity",
     "unconfirmed", "external", "still_popup",
     "locked", "required_popup", "overlay_goal", "missing_identity", "other_url",
     "other_page_type", "web_session", "required_failure", "before_close", "remaining_layer",
     "other_document", "missing_document"],
)
@pytest.mark.parametrize("page_type", ["home", "video_list", "payment", "settings"])
async def test_h5_base_page_recovery_accepts_only_confirmed_same_page_clearance(
    variation, page_type,
):
    semantic = {"page_state": ["popup"] if variation == "required_popup" else "popup"}
    if variation in {"missing_request_state", "legacy_empty_context"}:
        # 真实缺口曾只留下区域和 base_page，原 Capture 上的弹窗状态仍可独立核实。
        semantic = {"region": "页面可见内容"}
    elif variation == "required_failure":
        semantic = {"page_state": ["ad_load_failed"]}
    elif variation == "before_close":
        semantic["capture_roles"] = ["before"]
    request = build_evidence_request(
        work_item_id="work-h5-clearance", attempt_id="attempt-h5-clearance",
        task_id="task-h5", rule_id="rule-h5-clearance", rule_code="h5_clearance",
        target_capture_id="capture-original", reason_code="BASE_PAGE_OCCLUDED",
        evidence_goal="关闭遮挡后采集原业务页", required_artifact_types=("screenshot",),
        session_dependency=EvidenceSessionDependency.ACTIVE_SESSION,
        replay_policy=EvidenceReplayPolicy.SAME_SURFACE_ONLY,
        required_equivalence=EvidenceRequiredEquivalence.LOGICAL_EQUIVALENT,
        side_effect_policy=EvidenceSideEffectPolicy.READ_ONLY,
        target_invariants={
            "environment": {} if variation == "legacy_empty_context" else {"surface_kind": "web"},
            "business": {} if variation == "legacy_empty_context" else {
                "page_type": page_type, "url_pattern": "https://site.test/",
            },
            "semantic": semantic,
            "structure": {
                "layer_target": "overlay" if variation == "overlay_goal" else "base_page",
            },
        },
        minimum_source_grade=EvidenceSourceGrade.B, expires_at=NOW + timedelta(minutes=10),
    )
    session_id = "web:run-h5" if variation == "web_session" else "h5:run-h5"
    resolution = await H5TargetedRecapturePlanService(
        contexts=SimpleNamespace(read=AsyncMock(return_value=H5TargetedRecaptureContext(
            task_id="task-h5", agent_run_id="run-h5", finalization_epoch=4,
            session_id="h5:run-h5", current_capture_id="capture-original",
        ))), clock=lambda: NOW,
    ).resolve(request=request, agent_run_id="run-h5", finalization_epoch=4)
    assert resolution.plan is not None
    plan = build_restoration_plan(
        request=request, source_session_id=session_id,
        target_session_id=session_id, surface_kind=SurfaceKind.WEB,
        strategy=resolution.plan.strategy, deadline=resolution.plan.deadline,
    )
    original = StateInvariantSnapshot(
        task_id="task-h5", capture_id="capture-original", session_id=session_id,
        surface_kind=SurfaceKind.WEB, environment={"surface_kind": "web"},
        business={"page_type": page_type, "url_pattern": "https://site.test/",
                  "logical_page_key": "logical:home"},
        semantic={"page_state": "popup"},
        structure={"layer_target": "overlay", "identity_status": "confirmed", "is_external": False,
                   "document_identity": "document-clearance"},
        captured_at=NOW,
    )
    business = dict(original.business)
    structure = {"layer_target": "base_page", "identity_status": "confirmed", "is_external": False,
                 "document_identity": "document-clearance"}
    state = "stable"
    if variation == "other_identity":
        business["logical_page_key"] = "logical:other"
    elif variation == "missing_identity":
        business.pop("logical_page_key")
    elif variation == "other_url":
        business["url_pattern"] = "https://other.test/"
    elif variation == "other_page_type":
        business["page_type"] = "profile"
    elif variation == "unconfirmed":
        structure["identity_status"] = "ambiguous"
    elif variation == "external":
        structure["is_external"] = True
    elif variation == "still_popup":
        state = "popup"
    elif variation == "locked":
        state = "entitlement_locked"
    elif variation == "remaining_layer":
        structure["layer_target"] = "overlay"
    elif variation == "other_document":
        structure["document_identity"] = "document-reloaded"
    elif variation == "missing_document":
        structure.pop("document_identity")
    restored = replace(original, capture_id="capture-restored", business=business,
                       semantic={"page_state": state}, structure=structure,
                       captured_at=NOW + timedelta(seconds=1))
    request_payload = request.to_dict()
    assessment = H5StateEquivalenceGate().assess(
        request=request, plan=plan, restoration_attempt_id="restoration-h5-clearance",
        original=original, restored=restored,
    )
    assert assessment.passed is (
        variation in {"cleared", "missing_request_state", "legacy_empty_context"}
    )
    assert original.semantic["page_state"] == "popup"
    assert restored.semantic["page_state"] == state
    assert original.structure["layer_target"] == "overlay"
    assert dict(restored.structure) == structure
    assert request.to_dict() == request_payload


@pytest.mark.parametrize("scope", ["h5", "web", "app", "all_empty", "layer_drift"])
async def test_h5_empty_invariant_dimensions_do_not_discard_declared_constraints(scope):
    from services.api.app.application.state_equivalence import StateEquivalenceGate

    request = build_evidence_request(
        work_item_id="work-empty", attempt_id="attempt-empty", task_id="task-h5",
        rule_id="rule-empty", rule_code="h5_empty", target_capture_id="capture-target",
        reason_code="EVIDENCE_INSUFFICIENT", evidence_goal="重新观察当前层级",
        required_artifact_types=("screenshot",),
        session_dependency=EvidenceSessionDependency.ACTIVE_SESSION,
        replay_policy=EvidenceReplayPolicy.SAME_CONTEXT_ALLOWED,
        required_equivalence=EvidenceRequiredEquivalence.LOGICAL_EQUIVALENT,
        side_effect_policy=EvidenceSideEffectPolicy.READ_ONLY,
        target_invariants={
            "environment": {}, "business": {}, "semantic": {},
            "structure": {} if scope == "all_empty" else {"layer_target": "base_page"},
        }, minimum_source_grade=EvidenceSourceGrade.B,
        expires_at=NOW + timedelta(minutes=10),
    )
    resolution = await H5TargetedRecapturePlanService(
        contexts=SimpleNamespace(read=AsyncMock(return_value=H5TargetedRecaptureContext(
            task_id="task-h5", agent_run_id="run-h5", finalization_epoch=4,
            session_id="h5:run-h5", current_capture_id="capture-target",
        ))), clock=lambda: NOW,
    ).resolve(request=request, agent_run_id="run-h5", finalization_epoch=4)
    assert resolution.plan is not None
    plan = resolution.plan
    if scope in {"web", "app"}:
        plan = build_restoration_plan(
            request=request, source_session_id=f"{scope}:run", target_session_id=f"{scope}:run",
            surface_kind=SurfaceKind.WEB, strategy=plan.strategy, deadline=plan.deadline,
        )
    original = StateInvariantSnapshot(
        task_id="task-h5", capture_id="capture-target", session_id=plan.source_session_id,
        surface_kind=SurfaceKind.WEB, environment={"surface_kind": "web"}, business={},
        semantic={}, structure={"layer_target": "base_page"}, captured_at=NOW,
    )
    restored = replace(
        original, capture_id="capture-restored", session_id=plan.target_session_id,
        structure={"layer_target": "overlay" if scope == "layer_drift" else "base_page"},
        captured_at=NOW + timedelta(seconds=1),
    )
    arguments = dict(request=request, plan=plan, restoration_attempt_id="restoration-empty",
                     original=original, restored=restored)
    if scope in {"web", "app", "all_empty"}:
        with pytest.raises(RestorationValidationError):
            StateEquivalenceGate().assess(**arguments)
    else:
        assessment = StateEquivalenceGate().assess(**arguments)
        assert assessment.passed is (scope == "h5")
        assert any(item.key == "layer_target" for item in assessment.comparisons)
