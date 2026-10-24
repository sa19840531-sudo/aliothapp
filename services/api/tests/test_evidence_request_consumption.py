"""补采请求跨平台消费入口的测试。

这条入口是 App/H5/Web 共用的：状态流转、挂接前置条件、终态结算都在这里定死，
平台侧只提供 :class:`ProbeCompletionProof`。App 侧的委托由
``test_app_probe_completion`` 守着行为不变。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from services.api.app.application.evidence_request_consumption import (
    EvidenceRequestConsumptionError,
    EvidenceRequestConsumptionService,
    ProbeCompletionProof,
)
from services.api.app.application.evidence_requests import (
    EvidenceRequestClaimDisposition,
    EvidenceRequestClaimResult,
    EvidenceRequestLeaseReleaseDisposition,
    EvidenceRequestLeaseReleaseResult,
    EvidenceRequestStatus,
    ReleaseEvidenceRequestLeaseCommand,
    SettleEvidenceRequestLeaseCommand,
    StoredEvidenceRequest,
)
from services.api.app.domain.inspection import (
    EvidenceReplayPolicy,
    EvidenceRequiredEquivalence,
    EvidenceSessionDependency,
    EvidenceSideEffectPolicy,
    EvidenceSourceGrade,
    InvariantComparison,
    RestorationAttempt,
    RestorationAttemptStatus,
    RestorationStrategy,
    StateEquivalenceAssessment,
    StateInvariantDimension,
    build_evidence_request,
    build_restoration_attempt_id,
    build_restoration_plan,
    build_state_equivalence_assessment_id,
)
from services.api.app.domain.interaction import SurfaceKind

NOW = datetime(2026, 9, 10, 16, 0, tzinfo=UTC)


def _proof() -> ProbeCompletionProof:
    return ProbeCompletionProof(
        request_id="request-1",
        result_capture_id="capture-result",
        work_id="probe-work-1",
        restoration_attempt_id="restoration-attempt-1",
        equivalence_assessment_id="assessment-1",
        source_grade=EvidenceSourceGrade.B,
    )


def _stored(*, status: EvidenceRequestStatus, row_version: int = 2) -> StoredEvidenceRequest:
    return StoredEvidenceRequest(
        request=SimpleNamespace(request_id="request-1", task_id="task-1"),
        status=status,
        row_version=row_version,
    )


def _attempt(
    status: RestorationAttemptStatus,
    *,
    error_code: str | None = None,
) -> RestorationAttempt:
    request = build_evidence_request(
        work_item_id="work-1",
        attempt_id="attempt-1",
        task_id="task-1",
        rule_id="rule-1",
        rule_code="RULE-1",
        target_capture_id="historical-detail",
        reason_code="missing_detail_evidence",
        evidence_goal="补采详情页证据",
        required_artifact_types=("screenshot",),
        session_dependency=EvidenceSessionDependency.ACTIVE_SESSION,
        replay_policy=EvidenceReplayPolicy.SAME_CONTEXT_ALLOWED,
        required_equivalence=EvidenceRequiredEquivalence.LOGICAL_EQUIVALENT,
        side_effect_policy=EvidenceSideEffectPolicy.READ_ONLY,
        target_invariants={"semantic": {"page_identity": "detail"}},
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
        normalization_step_ids=("edge-1",),
        deadline=NOW + timedelta(minutes=5),
    )
    attempt_id = build_restoration_attempt_id(plan_id=plan.plan_id, attempt_no=1)
    restored_capture_id = None
    assessment = None
    if status is RestorationAttemptStatus.EQUIVALENT:
        restored_capture_id = "capture-result"
        assessment = _assessment(
            attempt_id=attempt_id,
            request=request,
            plan=plan,
            restored_capture_id=restored_capture_id,
        )
    return RestorationAttempt(
        attempt_id=attempt_id,
        plan=plan,
        attempt_no=1,
        status=status,
        started_at=NOW,
        finished_at=NOW + timedelta(seconds=30),
        restored_capture_id=restored_capture_id,
        assessment=assessment,
        error_code=error_code,
    )


def _assessment(
    *,
    attempt_id: str,
    request,
    plan,
    restored_capture_id: str,
) -> StateEquivalenceAssessment:
    """一条全部命中的不变量比对，等价结论为通过。"""

    comparisons = (
        InvariantComparison(
            dimension=StateInvariantDimension.SEMANTIC,
            key="page_identity",
            expected_fingerprint="fingerprint-detail",
            original_fingerprint="fingerprint-detail",
            restored_fingerprint="fingerprint-detail",
            matched=True,
        ),
    )
    identity = {
        "restoration_attempt_id": attempt_id,
        "request_id": request.request_id,
        "task_id": request.task_id,
        "original_capture_id": plan.original_capture_id,
        "restored_capture_id": restored_capture_id,
        "required_equivalence": request.required_equivalence,
        "source_grade": plan.source_grade,
        "minimum_source_grade": request.minimum_source_grade,
        "comparisons": comparisons,
        "evaluator_version": "test-evaluator.v1",
    }
    return StateEquivalenceAssessment(
        assessment_id=build_state_equivalence_assessment_id(**identity),
        passed=True,
        evaluated_at=NOW + timedelta(seconds=20),
        **identity,
    )


def _service(*, requests, manifests=None, attacher=None, manifest_finalizer=None):
    return EvidenceRequestConsumptionService(
        requests=requests,
        manifests=manifests or SimpleNamespace(get_latest_for_capture=AsyncMock()),
        attacher=attacher or SimpleNamespace(attach=AsyncMock()),
        manifest_finalizer=manifest_finalizer,
    )


async def test_ensure_running_advances_routed_request() -> None:
    routed = _stored(status=EvidenceRequestStatus.ROUTED)
    running = _stored(status=EvidenceRequestStatus.RUNNING, row_version=3)
    requests = SimpleNamespace(
        get=AsyncMock(side_effect=[routed, running]),
        transition=AsyncMock(return_value=3),
    )

    stored = await _service(requests=requests).ensure_running(
        task_id="task-1", request_id="request-1"
    )

    assert stored.status is EvidenceRequestStatus.RUNNING
    command = requests.transition.await_args.kwargs["command"]
    assert command.expected_status is EvidenceRequestStatus.ROUTED
    assert command.target_status is EvidenceRequestStatus.RUNNING
    assert command.row_version == 2


async def test_ensure_running_is_idempotent_for_already_running_request() -> None:
    requests = SimpleNamespace(
        get=AsyncMock(return_value=_stored(status=EvidenceRequestStatus.RUNNING)),
        transition=AsyncMock(),
    )

    stored = await _service(requests=requests).ensure_running(
        task_id="task-1", request_id="request-1"
    )

    assert stored.status is EvidenceRequestStatus.RUNNING
    requests.transition.assert_not_awaited()


async def test_ensure_running_rejects_request_from_another_task() -> None:
    requests = SimpleNamespace(
        get=AsyncMock(return_value=_stored(status=EvidenceRequestStatus.ROUTED)),
        transition=AsyncMock(),
    )

    with pytest.raises(EvidenceRequestConsumptionError, match="执行域不一致") as error:
        await _service(requests=requests).ensure_running(
            task_id="task-other", request_id="request-1"
        )

    assert error.value.retryable is False
    requests.transition.assert_not_awaited()


async def test_attach_supplement_marks_running_then_attaches_pass_manifest() -> None:
    routed = _stored(status=EvidenceRequestStatus.ROUTED)
    running = _stored(status=EvidenceRequestStatus.RUNNING, row_version=3)
    requests = SimpleNamespace(
        get=AsyncMock(side_effect=[routed, running]),
        transition=AsyncMock(return_value=3),
    )
    manifests = SimpleNamespace(
        get_latest_for_capture=AsyncMock(
            return_value=SimpleNamespace(manifest_id="manifest-pass-1")
        )
    )
    attacher = SimpleNamespace(
        attach=AsyncMock(return_value=SimpleNamespace(supplement=SimpleNamespace()))
    )

    result = await _service(
        requests=requests, manifests=manifests, attacher=attacher
    ).attach_supplement(task_id="task-1", proof=_proof())

    assert result.attached is True
    assert result.manifest_id == "manifest-pass-1"
    manifests.get_latest_for_capture.assert_awaited_once_with(
        task_id="task-1",
        capture_id="capture-result",
        require_quality_pass=True,
    )
    command = attacher.attach.await_args.args[0]
    assert command.task_id == "task-1"
    assert command.request_id == "request-1"
    assert command.probe_work_id == "probe-work-1"
    assert command.restoration_attempt_id == "restoration-attempt-1"
    assert command.equivalence_assessment_id == "assessment-1"
    assert command.source_grade is EvidenceSourceGrade.B


async def test_attach_supplement_requires_pass_manifest_before_touching_request() -> None:
    requests = SimpleNamespace(get=AsyncMock(), transition=AsyncMock())
    manifests = SimpleNamespace(get_latest_for_capture=AsyncMock(return_value=None))
    attacher = SimpleNamespace(attach=AsyncMock())

    with pytest.raises(EvidenceRequestConsumptionError, match="PASS Manifest") as error:
        await _service(requests=requests, manifests=manifests, attacher=attacher).attach_supplement(
            task_id="task-1", proof=_proof()
        )

    assert error.value.retryable is True
    requests.get.assert_not_awaited()
    requests.transition.assert_not_awaited()
    attacher.attach.assert_not_awaited()


async def test_attach_supplement_finalizes_pending_manifest_first() -> None:
    requests = SimpleNamespace(
        get=AsyncMock(return_value=_stored(status=EvidenceRequestStatus.RUNNING)),
        transition=AsyncMock(),
    )
    pending = SimpleNamespace(manifest_id="manifest-pending")
    manifests = SimpleNamespace(
        get_latest_for_capture=AsyncMock(
            side_effect=[None, pending, SimpleNamespace(manifest_id="manifest-pass-1")]
        )
    )
    attacher = SimpleNamespace(
        attach=AsyncMock(return_value=SimpleNamespace(supplement=SimpleNamespace()))
    )
    finalizer = SimpleNamespace(finalize=AsyncMock())

    result = await _service(
        requests=requests,
        manifests=manifests,
        attacher=attacher,
        manifest_finalizer=finalizer,
    ).attach_supplement(task_id="task-1", proof=_proof())

    assert result.manifest_id == "manifest-pass-1"
    finalizer.finalize.assert_awaited_once_with(pending)


async def test_attach_supplement_rejects_request_not_yet_routed() -> None:
    requests = SimpleNamespace(
        get=AsyncMock(return_value=_stored(status=EvidenceRequestStatus.PENDING)),
        transition=AsyncMock(),
    )
    manifests = SimpleNamespace(
        get_latest_for_capture=AsyncMock(
            return_value=SimpleNamespace(manifest_id="manifest-pass-1")
        )
    )
    attacher = SimpleNamespace(attach=AsyncMock())

    with pytest.raises(EvidenceRequestConsumptionError, match="不允许挂接") as error:
        await _service(requests=requests, manifests=manifests, attacher=attacher).attach_supplement(
            task_id="task-1", proof=_proof()
        )

    assert error.value.retryable is False
    attacher.attach.assert_not_awaited()


async def test_settle_terminal_marks_satisfied_when_restoration_is_equivalent() -> None:
    requests = SimpleNamespace(
        get=AsyncMock(return_value=_stored(status=EvidenceRequestStatus.RUNNING)),
        transition=AsyncMock(),
    )

    attempt = _attempt(RestorationAttemptStatus.EQUIVALENT)

    status = await _service(requests=requests).settle_terminal(
        task_id="task-1",
        request_id="request-1",
        attempt=attempt,
        pass_manifest_id="manifest-pass-1",
    )

    assert status is EvidenceRequestStatus.SATISFIED
    command = requests.transition.await_args.kwargs["command"]
    assert command.target_status is EvidenceRequestStatus.SATISFIED
    assert command.resolution_manifest_id == "manifest-pass-1"
    assert command.resolution_reason.startswith(f"restoration_equivalent:{attempt.attempt_id}:")


async def test_settle_terminal_requires_manifest_for_satisfied() -> None:
    requests = SimpleNamespace(
        get=AsyncMock(return_value=_stored(status=EvidenceRequestStatus.RUNNING)),
        transition=AsyncMock(),
    )

    with pytest.raises(EvidenceRequestConsumptionError, match="pass_manifest_id"):
        await _service(requests=requests).settle_terminal(
            task_id="task-1",
            request_id="request-1",
            attempt=_attempt(RestorationAttemptStatus.EQUIVALENT),
        )

    requests.transition.assert_not_awaited()


async def test_settle_terminal_marks_unfulfillable_with_error_code() -> None:
    requests = SimpleNamespace(
        get=AsyncMock(return_value=_stored(status=EvidenceRequestStatus.RUNNING)),
        transition=AsyncMock(),
    )

    status = await _service(requests=requests).settle_terminal(
        task_id="task-1",
        request_id="request-1",
        attempt=_attempt(
            RestorationAttemptStatus.UNFULFILLABLE,
            error_code="ROUTE_UNAVAILABLE",
        ),
    )

    assert status is EvidenceRequestStatus.UNFULFILLABLE
    command = requests.transition.await_args.kwargs["command"]
    assert command.resolution_reason == "restoration_unfulfillable:ROUTE_UNAVAILABLE"


async def test_settle_terminal_keeps_running_when_restoration_fails() -> None:
    requests = SimpleNamespace(
        get=AsyncMock(return_value=_stored(status=EvidenceRequestStatus.RUNNING)),
        transition=AsyncMock(),
    )

    status = await _service(requests=requests).settle_terminal(
        task_id="task-1",
        request_id="request-1",
        attempt=_attempt(RestorationAttemptStatus.FAILED, error_code="STATE_DRIFT"),
    )

    assert status is None
    requests.transition.assert_not_awaited()


def test_probe_completion_proof_rejects_incomplete_lineage() -> None:
    with pytest.raises(EvidenceRequestConsumptionError, match="restoration_attempt_id"):
        ProbeCompletionProof(
            request_id="request-1",
            result_capture_id="capture-result",
            work_id="probe-work-1",
            restoration_attempt_id="   ",
            equivalence_assessment_id="assessment-1",
            source_grade=EvidenceSourceGrade.B,
        )


def test_probe_completion_proof_rejects_missing_source_grade() -> None:
    with pytest.raises(EvidenceRequestConsumptionError, match="来源等级"):
        ProbeCompletionProof(
            request_id="request-1",
            result_capture_id="capture-result",
            work_id="probe-work-1",
            restoration_attempt_id="restoration-attempt-1",
            equivalence_assessment_id="assessment-1",
            source_grade="B",  # type: ignore[arg-type]
        )


async def test_claim_next_forwards_lease_context_and_reports_empty() -> None:
    """领不到是正常结果，不是错误；租约上下文原样交给仓储的围栏条件。"""

    requests = SimpleNamespace(
        claim_next=AsyncMock(
            return_value=EvidenceRequestClaimResult(
                disposition=EvidenceRequestClaimDisposition.EMPTY
            )
        )
    )

    result = await _service(requests=requests).claim_next(
        task_id="task-1",
        agent_run_id="run-1",
        finalization_epoch=3,
        lease_owner="h5-worker-1",
        lease_seconds=120,
    )

    assert result.disposition is EvidenceRequestClaimDisposition.EMPTY
    requests.claim_next.assert_awaited_once_with(
        task_id="task-1",
        agent_run_id="run-1",
        finalization_epoch=3,
        lease_owner="h5-worker-1",
        lease_seconds=120,
    )


async def test_claim_next_rejects_blank_lease_owner() -> None:
    requests = SimpleNamespace(claim_next=AsyncMock())

    with pytest.raises(EvidenceRequestConsumptionError, match="lease_owner"):
        await _service(requests=requests).claim_next(
            task_id="task-1",
            agent_run_id="run-1",
            finalization_epoch=3,
            lease_owner="  ",
            lease_seconds=120,
        )

    requests.claim_next.assert_not_awaited()


async def test_release_lease_reports_retry_disposition() -> None:
    command = ReleaseEvidenceRequestLeaseCommand(
        request_id="request-1",
        task_id="task-1",
        finalization_epoch=3,
        lease_owner="h5-worker-1",
        lease_token="7f1d2f4e-2f6a-4f5d-9f0a-1b2c3d4e5f60",
        row_version=4,
        next_attempt_at=NOW + timedelta(minutes=1),
        reason_code="restoration_route_unavailable",
    )
    requests = SimpleNamespace(
        release_lease=AsyncMock(
            return_value=EvidenceRequestLeaseReleaseResult(
                disposition=EvidenceRequestLeaseReleaseDisposition.RETRY_SCHEDULED,
                row_version=5,
            )
        )
    )

    result = await _service(requests=requests).release_lease(command=command)

    assert result.disposition is EvidenceRequestLeaseReleaseDisposition.RETRY_SCHEDULED
    requests.release_lease.assert_awaited_once_with(command=command)


async def test_settle_lease_delegates_to_repository() -> None:
    command = SettleEvidenceRequestLeaseCommand(
        request_id="request-1",
        task_id="task-1",
        finalization_epoch=3,
        lease_owner="h5-worker-1",
        lease_token="7f1d2f4e-2f6a-4f5d-9f0a-1b2c3d4e5f60",
        row_version=4,
        target_status=EvidenceRequestStatus.UNFULFILLABLE,
        resolution_reason="restoration_unfulfillable:ROUTE_UNAVAILABLE",
    )
    requests = SimpleNamespace(settle_lease=AsyncMock(return_value=5))

    assert await _service(requests=requests).settle_lease(command=command) == 5
    requests.settle_lease.assert_awaited_once_with(command=command)


async def test_settle_terminal_is_idempotent_after_attach_already_settled() -> None:
    """attach_supplement 底层已把请求原子结为 satisfied，成功路径误调不该再撞一次流转。"""

    requests = SimpleNamespace(
        get=AsyncMock(return_value=_stored(status=EvidenceRequestStatus.SATISFIED)),
        transition=AsyncMock(),
    )

    status = await _service(requests=requests).settle_terminal(
        task_id="task-1",
        request_id="request-1",
        attempt=_attempt(RestorationAttemptStatus.EQUIVALENT),
        pass_manifest_id="manifest-pass-1",
    )

    assert status is EvidenceRequestStatus.SATISFIED
    requests.transition.assert_not_awaited()


async def test_settle_terminal_does_not_reopen_unfulfillable_request() -> None:
    requests = SimpleNamespace(
        get=AsyncMock(return_value=_stored(status=EvidenceRequestStatus.UNFULFILLABLE)),
        transition=AsyncMock(),
    )

    status = await _service(requests=requests).settle_terminal(
        task_id="task-1",
        request_id="request-1",
        attempt=_attempt(
            RestorationAttemptStatus.UNFULFILLABLE,
            error_code="ROUTE_UNAVAILABLE",
        ),
    )

    assert status is EvidenceRequestStatus.UNFULFILLABLE
    requests.transition.assert_not_awaited()
