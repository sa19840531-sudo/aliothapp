"""App 定向补采正式组合测试。"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from services.api.app.application.app_targeted_recapture_composition import (
    AppTargetedRecaptureRuntime,
    AppTargetedRecaptureRuntimeFactory,
)
from services.api.app.application.evidence_recovery_restoration import (
    EvidenceRecoveryRestorationExecutor,
)
from services.api.app.application.evidence_requests import EvidenceRequestLeaseFence
from services.api.app.application.restoration import new_running_restoration_attempt
from services.api.app.application.state_equivalence import StateEquivalenceGate
from services.api.app.application.targeted_recapture_restoration import (
    TargetedRecaptureRestorationOutcome,
)
from services.api.app.domain.exploration import ExplorationBudget
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
    build_restoration_plan,
)
from services.api.app.domain.interaction import CaptureKind, CaptureRef, SurfaceKind

NOW = datetime(2026, 9, 8, 17, tzinfo=UTC)


def _request_and_plan():
    request = build_evidence_request(
        work_item_id="inspection-work-1",
        attempt_id="inspection-attempt-1",
        task_id="task-1",
        rule_id="rule-1",
        rule_code="rule_1",
        target_capture_id="capture-original",
        reason_code="TRUSTED_EVIDENCE_INVALID",
        evidence_goal="补采详情页截图",
        required_artifact_types=("screenshot",),
        session_dependency=EvidenceSessionDependency.ACTIVE_SESSION,
        replay_policy=EvidenceReplayPolicy.SAME_CONTEXT_ALLOWED,
        required_equivalence=EvidenceRequiredEquivalence.LOGICAL_EQUIVALENT,
        side_effect_policy=EvidenceSideEffectPolicy.READ_ONLY,
        target_invariants={"business": {"page_type": "content_detail"}},
        minimum_source_grade=EvidenceSourceGrade.B,
        expires_at=NOW + timedelta(minutes=10),
    )
    plan = build_restoration_plan(
        request=request,
        source_session_id="session-1",
        target_session_id="session-1",
        surface_kind=SurfaceKind.ANDROID,
        strategy=RestorationStrategy.SAME_APP_SESSION,
        replay_execution_plan_id="page-graph-route:route-1",
        normalization_step_ids=("transition-1",),
        deadline=NOW + timedelta(minutes=5),
    )
    return request, plan


def _capture(*, task_id: str = "task-1") -> CaptureRef:
    return CaptureRef(
        task_id=task_id,
        session_id="session-1",
        capture_id="capture-original",
        surface_kind=SurfaceKind.ANDROID,
        capture_kind=CaptureKind.APP,
        role="observation",
        artifact_ids=("screenshot-1", "ui-tree-1"),
    )


def _budget() -> ExplorationBudget:
    return ExplorationBudget(
        max_actions=2,
        remaining_actions=2,
        max_depth=1,
        remaining_duration_ms=60_000,
    )


async def test_runtime_reads_persisted_original_capture_before_restoration() -> None:
    request, plan = _request_and_plan()
    capture = _capture()
    original_snapshot = SimpleNamespace(capture_id=capture.capture_id)
    expected = TargetedRecaptureRestorationOutcome(
        restoration_attempt=SimpleNamespace(),
        probe_work=None,
    )
    captures = SimpleNamespace(
        get_verified_capture=AsyncMock(
            return_value=SimpleNamespace(capture=capture),
        )
    )
    snapshots = SimpleNamespace(read=AsyncMock(return_value=original_snapshot))
    restoration = SimpleNamespace(restore_and_publish=AsyncMock(return_value=expected))
    runtime = AppTargetedRecaptureRuntime(
        captures=captures,
        original_snapshots=snapshots,
        planning=SimpleNamespace(),
        restoration_attempts=SimpleNamespace(),
        restoration=restoration,
        agent_run_id="run-1",
        finalization_epoch=3,
    )

    outcome = await runtime.restore_and_publish(
        request=request,
        plan=plan,
        budget=_budget(),
        attempt_no=1,
    )

    assert outcome is expected
    captures.get_verified_capture.assert_awaited_once_with(
        capture_id=request.target_capture_id
    )
    snapshots.read.assert_awaited_once_with(capture=capture)
    restoration.restore_and_publish.assert_awaited_once_with(
        request=request,
        plan=plan,
        original_snapshot=original_snapshot,
        budget=_budget(),
        attempt_no=1,
    )


async def test_runtime_forwards_claim_lease_to_restoration() -> None:
    request, plan = _request_and_plan()
    capture = _capture()
    fence = EvidenceRequestLeaseFence(
        request_id=request.request_id,
        task_id=request.task_id,
        finalization_epoch=3,
        lease_owner="worker-1:app-recapture",
        lease_token="00000000-0000-0000-0000-000000000008",
        row_version=2,
    )
    restoration = SimpleNamespace(
        restore_and_publish=AsyncMock(
            return_value=TargetedRecaptureRestorationOutcome(
                restoration_attempt=SimpleNamespace(),
                probe_work=None,
            )
        )
    )
    runtime = AppTargetedRecaptureRuntime(
        captures=SimpleNamespace(
            get_verified_capture=AsyncMock(return_value=SimpleNamespace(capture=capture))
        ),
        original_snapshots=SimpleNamespace(read=AsyncMock(return_value=SimpleNamespace())),
        planning=SimpleNamespace(),
        restoration_attempts=SimpleNamespace(),
        restoration=restoration,
        agent_run_id="run-1",
        finalization_epoch=3,
    )

    await runtime.restore_and_publish(
        request=request,
        plan=plan,
        budget=_budget(),
        attempt_no=1,
        request_lease=fence,
    )

    assert restoration.restore_and_publish.await_args.kwargs["request_lease"] is fence


async def test_runtime_rejects_stale_lease_epoch_before_surface_access() -> None:
    request, plan = _request_and_plan()
    captures = SimpleNamespace(get_verified_capture=AsyncMock())
    restoration = SimpleNamespace(restore_and_publish=AsyncMock())
    runtime = AppTargetedRecaptureRuntime(
        captures=captures,
        original_snapshots=SimpleNamespace(read=AsyncMock()),
        planning=SimpleNamespace(),
        restoration_attempts=SimpleNamespace(),
        restoration=restoration,
        agent_run_id="run-1",
        finalization_epoch=3,
    )
    stale_fence = EvidenceRequestLeaseFence(
        request_id=request.request_id,
        task_id=request.task_id,
        finalization_epoch=2,
        lease_owner="worker-1:app-recapture",
        lease_token="00000000-0000-0000-0000-000000000008",
        row_version=2,
    )

    with pytest.raises(ValueError, match="租约.*执行域"):
        await runtime.restore_and_publish(
            request=request,
            plan=plan,
            budget=_budget(),
            attempt_no=1,
            request_lease=stale_fence,
        )

    captures.get_verified_capture.assert_not_awaited()
    restoration.restore_and_publish.assert_not_awaited()


async def test_runtime_rejects_original_capture_from_another_task() -> None:
    request, plan = _request_and_plan()
    snapshots = SimpleNamespace(read=AsyncMock())
    restoration = SimpleNamespace(restore_and_publish=AsyncMock())
    runtime = AppTargetedRecaptureRuntime(
        captures=SimpleNamespace(
            get_verified_capture=AsyncMock(
                return_value=SimpleNamespace(capture=_capture(task_id="task-other")),
            )
        ),
        original_snapshots=snapshots,
        planning=SimpleNamespace(),
        restoration_attempts=SimpleNamespace(),
        restoration=restoration,
        agent_run_id="run-1",
        finalization_epoch=3,
    )

    with pytest.raises(ValueError, match="原 Capture.*身份"):
        await runtime.restore_and_publish(
            request=request,
            plan=plan,
            budget=_budget(),
            attempt_no=1,
        )

    snapshots.read.assert_not_awaited()
    restoration.restore_and_publish.assert_not_awaited()


async def test_runtime_resolves_plan_with_bound_run_epoch() -> None:
    request, _ = _request_and_plan()
    expected = SimpleNamespace(executable=True)
    planning = SimpleNamespace(resolve=AsyncMock(return_value=expected))
    runtime = AppTargetedRecaptureRuntime(
        captures=SimpleNamespace(),
        original_snapshots=SimpleNamespace(),
        planning=planning,
        restoration_attempts=SimpleNamespace(),
        restoration=SimpleNamespace(),
        agent_run_id="run-1",
        finalization_epoch=3,
    )

    result = await runtime.resolve_plan(request=request)

    assert result is expected
    planning.resolve.assert_awaited_once_with(
        request=request,
        agent_run_id="run-1",
        finalization_epoch=3,
    )


async def test_runtime_resumes_equivalent_publication_without_surface_access() -> None:
    request, plan = _request_and_plan()
    restored_snapshot = StateInvariantSnapshot(
        task_id=request.task_id,
        capture_id="capture-restored",
        session_id=plan.target_session_id,
        surface_kind=SurfaceKind.ANDROID,
        environment={},
        business={"page_type": "content_detail"},
        semantic={},
        structure={},
        captured_at=NOW + timedelta(seconds=1),
    )
    running = new_running_restoration_attempt(plan=plan, attempt_no=1, started_at=NOW)
    assessment = StateEquivalenceGate(clock=lambda: NOW + timedelta(seconds=2)).assess(
        request=request,
        plan=plan,
        restoration_attempt_id=running.attempt_id,
        original=StateInvariantSnapshot(
            task_id=request.task_id,
            capture_id=request.target_capture_id,
            session_id=plan.source_session_id,
            surface_kind=SurfaceKind.ANDROID,
            environment={},
            business={"page_type": "content_detail"},
            semantic={},
            structure={},
            captured_at=NOW,
        ),
        restored=restored_snapshot,
    )
    equivalent = replace(
        running,
        status=RestorationAttemptStatus.EQUIVALENT,
        restored_capture_id=restored_snapshot.capture_id,
        assessment=assessment,
        finished_at=NOW + timedelta(seconds=2),
    )
    fence = EvidenceRequestLeaseFence(
        request_id=request.request_id,
        task_id=request.task_id,
        finalization_epoch=3,
        lease_owner="worker-1:app-recapture",
        lease_token="00000000-0000-0000-0000-000000000008",
        row_version=2,
    )
    attempts = SimpleNamespace(
        find_equivalent_for_lease=AsyncMock(return_value=equivalent)
    )
    expected = TargetedRecaptureRestorationOutcome(
        restoration_attempt=equivalent,
        probe_work=SimpleNamespace(),
    )
    restoration = SimpleNamespace(
        publish_equivalent=AsyncMock(return_value=expected),
        restore_and_publish=AsyncMock(),
    )
    captures = SimpleNamespace(get_verified_capture=AsyncMock())
    snapshots = SimpleNamespace(read=AsyncMock())
    runtime = AppTargetedRecaptureRuntime(
        captures=captures,
        original_snapshots=snapshots,
        planning=SimpleNamespace(),
        restoration_attempts=attempts,
        restoration=restoration,
        agent_run_id="run-1",
        finalization_epoch=3,
    )

    outcome = await runtime.resume_equivalent_publication(
        request=request,
        budget=_budget(),
        request_lease=fence,
    )

    assert outcome is expected
    attempts.find_equivalent_for_lease.assert_awaited_once_with(fence=fence)
    restoration.publish_equivalent.assert_awaited_once_with(
        request=request,
        restoration_attempt=equivalent,
        budget=_budget(),
        request_lease=fence,
    )
    captures.get_verified_capture.assert_not_awaited()
    snapshots.read.assert_not_awaited()
    restoration.restore_and_publish.assert_not_awaited()


def test_factory_binds_run_epoch_and_existing_runtime_ports() -> None:
    dependencies = {
        "page_graph": SimpleNamespace(),
        "planning_contexts": SimpleNamespace(),
        "surface_sessions": SimpleNamespace(),
        "capture_coordinator": SimpleNamespace(),
        "captures": SimpleNamespace(),
        "understanding": SimpleNamespace(),
        "observations": SimpleNamespace(),
        "original_snapshots": SimpleNamespace(),
        "restored_snapshots": SimpleNamespace(),
        "restoration_attempts": SimpleNamespace(),
        "probe_orchestrator": SimpleNamespace(),
    }
    factory = AppTargetedRecaptureRuntimeFactory(**dependencies)

    runtime = factory.build(agent_run_id="run-1", finalization_epoch=3)

    assert runtime.agent_run_id == "run-1"
    assert runtime.finalization_epoch == 3
    assert runtime.planning._contexts is dependencies["planning_contexts"]
    coordinator = runtime.restoration._coordinator
    executor = coordinator._executor
    assert isinstance(executor, EvidenceRecoveryRestorationExecutor)
    assert executor._agent_run_id == "run-1"
    assert executor._finalization_epoch == 3
    assert executor._replay._repository is dependencies["page_graph"]
    assert executor._replay._runtime._surface_sessions is dependencies["surface_sessions"]
    assert (
        executor._replay._runtime._capture_coordinator
        is dependencies["capture_coordinator"]
    )
    assert coordinator._repository is dependencies["restoration_attempts"]
    assert (
        runtime.restoration._probe_publisher._orchestrator
        is dependencies["probe_orchestrator"]
    )
