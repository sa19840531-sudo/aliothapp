"""H5 Probe Work 执行与补采挂接测试。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from services.api.app.agents.explorer.maf_agent import MafExplorerAgent
from services.api.app.application.evidence_request_consumption import (
    ProbeSupplementAttachment,
)
from services.api.app.application.evidence_requests import EvidenceRequestStatus
from services.api.app.application.exploration_work_queue import (
    ExplorerWorkLease,
    ExplorerWorkStatus,
    ExplorerWorkType,
)
from services.api.app.application.h5_probe_work import (
    H5ProbeCompletionService,
    H5ProbeDecisionAdapter,
    H5ProbeDrainService,
    H5ProbeWorkError,
    H5ProbeWorkOutcome,
    H5ProbeWorkRunner,
    H5SharedProbeEvidence,
)
from services.api.app.application.supplemental_evidence_policy import (
    SupplementalEvidenceDisabledError,
    build_lab_supplemental_evidence_policy,
)
from services.api.app.domain.exploration import (
    ExplorationBudget,
    ExplorationTerminal,
    ExplorationTerminalStatus,
    build_probe_work,
)
from services.api.app.domain.inspection import (
    EvidenceInteractionRequirement,
    EvidenceReplayPolicy,
    EvidenceRequiredEquivalence,
    EvidenceSessionDependency,
    EvidenceSideEffectPolicy,
    EvidenceSourceGrade,
    build_evidence_request,
)
from services.api.app.domain.inspection.evidence_gap import (
    build_evidence_gap,
    materialize_evidence_request,
)
from services.api.app.domain.interaction import (
    ActionIntent,
    CaptureDirective,
    CaptureKind,
    CaptureProfile,
    CaptureRef,
    InteractionOperation,
    InteractionResult,
    InteractionResultStatus,
    SurfaceKind,
)

NOW = datetime.now(UTC)


def _work(request=None):
    request = request or build_evidence_request(
        work_item_id="inspection-work-1",
        attempt_id="inspection-attempt-1",
        task_id="task-1",
        rule_id="rule-1",
        rule_code="alioth_1",
        target_capture_id="capture-original",
        reason_code="TRUSTED_EVIDENCE_INVALID",
        evidence_goal="补采展开详情后的截图",
        required_artifact_types=("screenshot", "dom", "visible_text"),
        required_interaction=EvidenceInteractionRequirement(
            operation=InteractionOperation.CLICK,
            semantic_target="展开商品详情",
            purpose="验证详情面板展开后的内容",
        ),
        session_dependency=EvidenceSessionDependency.ACTIVE_SESSION,
        replay_policy=EvidenceReplayPolicy.SAME_SURFACE_ONLY,
        required_equivalence=EvidenceRequiredEquivalence.SAME_SURFACE_CONTINUATION,
        side_effect_policy=EvidenceSideEffectPolicy.READ_ONLY,
        target_invariants={"route": "/products"},
        minimum_source_grade=EvidenceSourceGrade.A1,
        expires_at=NOW + timedelta(minutes=10),
    )
    return build_probe_work(
        session_id="h5:session-1",
        branch_id="probe-1",
        surface_kind=SurfaceKind.WEB,
        source_capture_id="capture-restored",
        restoration_plan_hash="restoration-plan-hash",
        restoration_attempt_id="restoration-attempt-1",
        equivalence_assessment_id="state-equivalence-1",
        source_grade=EvidenceSourceGrade.A1,
        evidence_request=request,
        budget=ExplorationBudget(
            max_actions=1,
            remaining_actions=1,
            max_depth=0,
            remaining_duration_ms=30_000,
        ),
        deadline=NOW + timedelta(minutes=5),
    )


def _intent(work) -> ActionIntent:
    return ActionIntent(
        task_id=work.task_id,
        session_id=work.session_id,
        intent_id="probe-intent-1",
        idempotency_key="probe-intent-1",
        surface_kind=SurfaceKind.WEB,
        source_capture_id=work.source_capture_id,
        action_type=InteractionOperation.CLICK,
        semantic_target="展开商品详情",
        purpose="验证详情面板展开后的内容",
        capture_profile=CaptureProfile(
            profile_id="probe-click-v1",
            before=CaptureDirective(
                role="probe_before",
                artifact_types=("screenshot", "dom", "visible_text"),
            ),
            after=CaptureDirective(
                role="probe_result_replay",
                artifact_types=("screenshot", "dom", "visible_text", "action_trace"),
            ),
        ),
        deadline=work.deadline,
        parameters={
            "evidence_request_id": work.evidence_request.request_id,
            "restoration_attempt_id": work.restoration_attempt_id,
            "equivalence_assessment_id": work.equivalence_assessment_id,
        },
    )


def _turn(work):
    before = CaptureRef(
        task_id=work.task_id,
        session_id=work.session_id,
        capture_id="capture-before",
        surface_kind=SurfaceKind.WEB,
        capture_kind=CaptureKind.PAGE,
        role="probe_before",
    )
    after = CaptureRef(
        task_id=work.task_id,
        session_id=work.session_id,
        capture_id="capture-result",
        surface_kind=SurfaceKind.WEB,
        capture_kind=CaptureKind.PAGE,
        role="probe_result_replay",
        parent_capture_id=before.capture_id,
        action_execution_id="action-1",
    )
    interaction = InteractionResult(
        task_id=work.task_id,
        session_id=work.session_id,
        intent_id="probe-intent-1",
        action_execution_id="action-1",
        source_capture_id=before.capture_id,
        status=InteractionResultStatus.SUCCEEDED,
        result_capture_id=after.capture_id,
    )
    return SimpleNamespace(
        decision=_intent(work),
        coordination=SimpleNamespace(
            before_capture=before,
            interaction=interaction,
            after_capture=after,
        ),
    )


class MemoryQueue:
    def __init__(self, work) -> None:
        self.work = work
        self.claims = []
        self.transitions = []
        self.renewals = []

    async def claim(self, *, command, lease_token):
        self.claims.append(command)
        if self.work is None:
            return None
        work, self.work = self.work, None
        return ExplorerWorkLease(
            work=work,
            status=ExplorerWorkStatus.LEASED,
            lease_token=lease_token,
            attempt_count=1,
            row_version=1,
        )

    async def transition(self, *, command):
        self.transitions.append(command)
        return command.row_version + 1

    async def renew(self, *, command):
        self.renewals.append(command)


@pytest.mark.parametrize("shared", [True, False])
async def test_compatible_evidence_uses_own_probe_without_forging_an_action(shared):
    from services.api.tests.test_h5_recapture_coalescing import request
    work = _work(request=request())
    queue = MemoryQueue(work)
    executor = SimpleNamespace(execute=AsyncMock(return_value=_turn(work)))
    consumption = SimpleNamespace(ensure_running=AsyncMock(
        return_value=SimpleNamespace(status=EvidenceRequestStatus.RUNNING)))
    completion = SimpleNamespace(complete=AsyncMock())
    reader = SimpleNamespace(find=AsyncMock(return_value=(
        H5SharedProbeEvidence(
            origin_work_id="real-observe-leader", result_capture_id="capture-result",
        )
        if shared else None)))
    runner = H5ProbeWorkRunner(
        queue=queue, turn_executor=executor, request_consumption=consumption,
        completion=completion, lease_owner="worker", shared_evidence=reader,
        policy=build_lab_supplemental_evidence_policy(), now=lambda: NOW,
    )
    outcome = await runner.run_one(task_id=work.task_id, agent_run_id="run-one")
    assert outcome.final_status is ExplorerWorkStatus.SUCCEEDED
    assert outcome.work_id == work.work_id
    assert outcome.evidence_request_id == work.evidence_request.request_id
    assert outcome.restoration_attempt_id == work.restoration_attempt_id
    assert executor.execute.await_count == (0 if shared else 1)
    assert completion.complete.await_args.kwargs["outcome"] == outcome
    terminal = queue.transitions[-1].terminal
    assert (terminal.reason if terminal else None) == (
        "h5_shared_evidence:real-observe-leader" if shared else None)


async def test_failed_shared_observation_requeues_unexecuted_probe_not_unfulfillable():
    from services.api.tests.test_h5_recapture_coalescing import request
    work = _work(request=request())
    queue = MemoryQueue(work)
    executor = SimpleNamespace(execute=AsyncMock())
    completion = SimpleNamespace(complete=AsyncMock())
    reader = SimpleNamespace(find=AsyncMock(return_value=None),
                             release_stale_unexecuted=AsyncMock(return_value=True))
    runner = H5ProbeWorkRunner(
        queue=queue, turn_executor=executor,
        request_consumption=SimpleNamespace(ensure_running=AsyncMock(
            return_value=SimpleNamespace(status=EvidenceRequestStatus.RUNNING))),
        completion=completion, lease_owner="worker", shared_evidence=reader,
        policy=build_lab_supplemental_evidence_policy(), now=lambda: NOW,
    )
    outcome = await runner.run_one(task_id=work.task_id, agent_run_id="run-one")
    assert outcome.final_status is ExplorerWorkStatus.SUPERSEDED
    assert outcome.error_code == "H5_SHARED_PROBE_SOURCE_REPLACED"
    executor.execute.assert_not_awaited()
    completion.complete.assert_not_awaited()
    assert reader.release_stale_unexecuted.await_args.kwargs["row_version"] == 2


async def test_h5_probe_decision_adds_work_lineage_and_mobile_viewport() -> None:
    work = _work()
    delegate = SimpleNamespace(decide_next=AsyncMock(return_value=_intent(work)))

    decision = await H5ProbeDecisionAdapter(delegate=delegate).decide_next(work)

    assert isinstance(decision, ActionIntent)
    assert decision.parameters["evidence_request_id"] == work.evidence_request.request_id
    assert decision.parameters["exploration_work_id"] == work.work_id
    assert decision.parameters["restoration_attempt_id"] == work.restoration_attempt_id
    assert decision.parameters["equivalence_assessment_id"] == work.equivalence_assessment_id
    assert decision.parameters["supplemental_evidence_source"] == "targeted_recapture"
    assert decision.parameters["viewport_width"] == 390
    assert decision.parameters["viewport_height"] == 844
    assert decision.capture_profile.before.role == "probe_before"
    assert decision.capture_profile.after.role == "probe_result_replay"


async def test_h5_probe_decision_uses_deterministic_probe_contract_without_model() -> None:
    work = _work()
    delegate = MafExplorerAgent(agent=SimpleNamespace(), now=lambda: NOW)

    decision = await H5ProbeDecisionAdapter(delegate=delegate).decide_next(work)

    assert isinstance(decision, ActionIntent)
    assert decision.action_type is InteractionOperation.CLICK
    assert decision.semantic_target == "展开商品详情"
    assert decision.parameters["evidence_request_id"] == work.evidence_request.request_id
    assert decision.parameters["exploration_work_id"] == work.work_id


async def test_h5_probe_from_occluded_gap_closes_overlay_instead_of_observing() -> None:
    """正式链路：底层页面被遮挡的缺口必须点掉弹窗，不能只是再看一眼。"""

    gap = build_evidence_gap(
        task_id="task-1",
        rule_id="rule-1",
        rule_code="alioth_12",
        rule_name="提示文案叹号结尾检查",
        category="text",
        check_point="提示文字不应使用叹号结尾",
        input_contract={"recapture_policy": {"kind": "equivalent_state"}},
        page_capture_id="capture-original",
        page={
            "page_capture_id": "capture-original",
            "url": "https://example.com/detail",
            "page_type": "content_detail",
            "page_state": "stable",
            "surface_kind": "h5",
        },
        missing_artifact_types=["screenshot"],
        presentation_code="BASE_PAGE_OCCLUDED",
        presentation_reason="最上层是充值弹窗，底层页面不可见",
        reason_code="EVIDENCE_INSUFFICIENT",
        request_expires_at=NOW + timedelta(minutes=10),
    )
    request = materialize_evidence_request(
        gap=gap.to_dict(),
        work_item_id="inspection-work-1",
        attempt_id="inspection-attempt-1",
        task_id="task-1",
        rule_id="rule-1",
        rule_code="alioth_12",
        page_capture_id="capture-original",
    )
    work = _work(request)
    delegate = MafExplorerAgent(agent=SimpleNamespace(), now=lambda: NOW)

    decision = await H5ProbeDecisionAdapter(delegate=delegate).decide_next(work)

    assert isinstance(decision, ActionIntent)
    assert decision.action_type is InteractionOperation.CLICK
    assert decision.action_type is not InteractionOperation.OBSERVE
    assert decision.semantic_target == "关闭当前弹窗"
    assert decision.parameters["target_mode"] == "h5_probe_popup_dismissal"
    assert "popup_dismissal_capability" not in decision.parameters
    assert "obstruction_signature" not in decision.parameters


async def test_h5_probe_runner_claims_only_web_probe_and_attaches_success() -> None:
    work = _work()
    queue = MemoryQueue(work)
    executor = SimpleNamespace(execute=AsyncMock(return_value=_turn(work)))
    completion = SimpleNamespace(complete=AsyncMock())
    requests = SimpleNamespace(
        ensure_running=AsyncMock(
            return_value=SimpleNamespace(status=EvidenceRequestStatus.RUNNING)
        )
    )
    runner = H5ProbeWorkRunner(
        queue=queue,
        turn_executor=executor,
        request_consumption=requests,
        completion=completion,
        lease_owner="worker-1:h5-probe",
        policy=build_lab_supplemental_evidence_policy(),
        now=lambda: NOW,
    )

    outcome = await runner.run_one(task_id="task-1", agent_run_id="run-1")

    assert outcome.claimed is True
    assert outcome.final_status is ExplorerWorkStatus.SUCCEEDED
    assert outcome.result_capture_id == "capture-result"
    claim = queue.claims[0]
    assert claim.work_type is ExplorerWorkType.PROBE
    assert claim.task_id == "task-1"
    assert claim.surface_kinds == (SurfaceKind.WEB,)
    requests.ensure_running.assert_awaited_once_with(
        task_id="task-1",
        request_id=work.evidence_request.request_id,
    )
    completion.complete.assert_awaited_once_with(task_id="task-1", outcome=outcome)
    assert [item.target_status for item in queue.transitions] == [
        ExplorerWorkStatus.RUNNING,
        ExplorerWorkStatus.SUCCEEDED,
    ]


async def test_h5_probe_runner_does_not_claim_when_policy_is_disabled() -> None:
    queue = MemoryQueue(_work())
    runner = H5ProbeWorkRunner(
        queue=queue,
        turn_executor=SimpleNamespace(),
        request_consumption=SimpleNamespace(),
        completion=SimpleNamespace(),
        lease_owner="worker-1:h5-probe",
    )

    with pytest.raises(SupplementalEvidenceDisabledError):
        await runner.run_one(task_id="task-1", agent_run_id="run-1")

    assert queue.claims == []


async def test_h5_probe_runner_does_not_repeat_action_for_satisfied_request() -> None:
    work = _work()
    queue = MemoryQueue(work)
    executor = SimpleNamespace(execute=AsyncMock())
    completion = SimpleNamespace(complete=AsyncMock())
    runner = H5ProbeWorkRunner(
        queue=queue,
        turn_executor=executor,
        request_consumption=SimpleNamespace(
            ensure_running=AsyncMock(
                return_value=SimpleNamespace(status=EvidenceRequestStatus.SATISFIED)
            )
        ),
        completion=completion,
        lease_owner="worker-1:h5-probe",
        policy=build_lab_supplemental_evidence_policy(),
        now=lambda: NOW,
    )

    outcome = await runner.run_one(task_id="task-1", agent_run_id="run-1")

    assert outcome.final_status is ExplorerWorkStatus.CANCELLED
    assert outcome.error_code == "H5_PROBE_REQUEST_ALREADY_SATISFIED"
    executor.execute.assert_not_awaited()
    completion.complete.assert_not_awaited()


async def test_h5_probe_runner_persists_terminal_without_fake_completion() -> None:
    work = _work()
    queue = MemoryQueue(work)
    terminal = ExplorationTerminal(
        work_id=work.work_id,
        task_id=work.task_id,
        status=ExplorationTerminalStatus.BLOCKED,
        reason="补采目标当前不可执行",
    )
    completion = SimpleNamespace(complete=AsyncMock())
    runner = H5ProbeWorkRunner(
        queue=queue,
        turn_executor=SimpleNamespace(
            execute=AsyncMock(return_value=SimpleNamespace(decision=terminal))
        ),
        request_consumption=SimpleNamespace(
            ensure_running=AsyncMock(
                return_value=SimpleNamespace(status=EvidenceRequestStatus.RUNNING)
            )
        ),
        completion=completion,
        lease_owner="worker-1:h5-probe",
        policy=build_lab_supplemental_evidence_policy(),
        now=lambda: NOW,
    )

    outcome = await runner.run_one(task_id="task-1", agent_run_id="run-1")

    assert outcome.final_status is ExplorerWorkStatus.BLOCKED
    assert outcome.result_capture_id is None
    completion.complete.assert_not_awaited()
    assert queue.transitions[-1].target_status is ExplorerWorkStatus.BLOCKED
    assert queue.transitions[-1].terminal is terminal


async def test_h5_probe_runner_does_not_retry_terminal_interaction_without_capture() -> None:
    work = _work()
    queue = MemoryQueue(work)
    error = RuntimeError("动作执行未完成")
    error.error_code = "SEMANTIC_TARGET_NOT_FOUND"
    error.retryable = False
    error.interaction = SimpleNamespace(
        retryable=True,
        status=InteractionResultStatus.FAILED,
        result_capture_id=None,
    )
    runner = H5ProbeWorkRunner(
        queue=queue,
        turn_executor=SimpleNamespace(execute=AsyncMock(side_effect=error)),
        request_consumption=SimpleNamespace(
            ensure_running=AsyncMock(
                return_value=SimpleNamespace(status=EvidenceRequestStatus.RUNNING)
            )
        ),
        completion=SimpleNamespace(complete=AsyncMock()),
        lease_owner="worker-1:h5-probe",
        policy=build_lab_supplemental_evidence_policy(),
        now=lambda: NOW,
    )

    outcome = await runner.run_one(task_id="task-1", agent_run_id="run-1")

    assert outcome.final_status is ExplorerWorkStatus.FAILED
    assert outcome.error_code == "SEMANTIC_TARGET_NOT_FOUND"
    assert queue.transitions[-1].target_status is ExplorerWorkStatus.FAILED
    assert queue.transitions[-1].retry_delay_seconds == 0


async def test_h5_probe_runner_retries_failure_before_interaction_terminal() -> None:
    work = _work()
    queue = MemoryQueue(work)
    error = RuntimeError("驱动连接瞬时失败")
    error.error_code = "SURFACE_DRIVER_FAILED"
    error.retryable = True
    runner = H5ProbeWorkRunner(
        queue=queue,
        turn_executor=SimpleNamespace(execute=AsyncMock(side_effect=error)),
        request_consumption=SimpleNamespace(
            ensure_running=AsyncMock(
                return_value=SimpleNamespace(status=EvidenceRequestStatus.RUNNING)
            )
        ),
        completion=SimpleNamespace(complete=AsyncMock()),
        lease_owner="worker-1:h5-probe",
        policy=build_lab_supplemental_evidence_policy(),
        now=lambda: NOW,
    )

    outcome = await runner.run_one(task_id="task-1", agent_run_id="run-1")

    assert outcome.final_status is ExplorerWorkStatus.RETRY_WAIT
    assert outcome.error_code == "SURFACE_DRIVER_FAILED"
    assert queue.transitions[-1].target_status is ExplorerWorkStatus.RETRY_WAIT
    assert queue.transitions[-1].retry_delay_seconds > 0


@pytest.mark.parametrize("status", [
    InteractionResultStatus.FAILED,
    InteractionResultStatus.BLOCKED,
    InteractionResultStatus.TIMED_OUT,
    InteractionResultStatus.EVIDENCE_FAILED,
])
@pytest.mark.parametrize("result_capture_id", [None, "capture-result"])
async def test_returned_terminal_interaction_never_replays_stale_probe(status, result_capture_id):
    """动作已有终态时保留首个原因，不重新领取旧来源制造作用域错误。"""
    work = _work()
    queue = MemoryQueue(work)
    turn = _turn(work)
    turn.coordination.interaction = InteractionResult(
        task_id=work.task_id, session_id=work.session_id, intent_id="probe-intent-1",
        action_execution_id="action-1", source_capture_id="capture-before",
        status=status, result_capture_id=result_capture_id,
        error_code="LOGICAL_TRANSITION_UNVERIFIED", error_message="未证明目标转场",
        retryable=True,
    )
    completion = SimpleNamespace(complete=AsyncMock())
    runner = H5ProbeWorkRunner(
        queue=queue,
        turn_executor=SimpleNamespace(execute=AsyncMock(return_value=turn)),
        request_consumption=SimpleNamespace(
            ensure_running=AsyncMock(return_value=SimpleNamespace(status=EvidenceRequestStatus.RUNNING)),
        ),
        completion=completion, lease_owner="worker-1:h5-probe",
        policy=build_lab_supplemental_evidence_policy(),
        now=lambda: NOW,
    )

    outcome = await runner.run_one(task_id=work.task_id, agent_run_id="run-1")

    assert outcome.final_status is ExplorerWorkStatus.FAILED
    assert outcome.error_code == "LOGICAL_TRANSITION_UNVERIFIED"
    assert queue.transitions[-1].retry_delay_seconds == 0
    completion.complete.assert_not_awaited()


async def test_h5_probe_runner_recovers_attachment_without_replaying_action() -> None:
    work = _work()
    queue = MemoryQueue(work)
    executor = SimpleNamespace(execute=AsyncMock(return_value=_turn(work)))
    completion = SimpleNamespace(
        complete=AsyncMock(
            side_effect=H5ProbeWorkError(
                "Manifest 尚未完成",
                error_code="H5_PROBE_SUPPLEMENT_ATTACH_FAILED",
                retryable=True,
            )
        )
    )
    runner = H5ProbeWorkRunner(
        queue=queue,
        turn_executor=executor,
        request_consumption=SimpleNamespace(
            ensure_running=AsyncMock(
                return_value=SimpleNamespace(status=EvidenceRequestStatus.RUNNING)
            )
        ),
        completion=completion,
        lease_owner="worker-1:h5-probe",
        policy=build_lab_supplemental_evidence_policy(),
        now=lambda: NOW,
    )

    outcome = await runner.run_one(task_id="task-1", agent_run_id="run-1")

    assert outcome.final_status is ExplorerWorkStatus.SUCCEEDED
    assert outcome.completion_pending is True
    assert outcome.error_code == "H5_PROBE_SUPPLEMENT_ATTACH_FAILED"
    executor.execute.assert_awaited_once()
    assert queue.transitions[-1].target_status is ExplorerWorkStatus.SUCCEEDED


async def test_h5_probe_completion_builds_full_public_proof() -> None:
    consumption = SimpleNamespace(
        attach_supplement=AsyncMock(
            return_value=ProbeSupplementAttachment(
                request_id="request-1",
                manifest_id="manifest-1",
                attached=True,
            )
        )
    )
    service = H5ProbeCompletionService(consumption=consumption)
    outcome = H5ProbeWorkOutcome(
        claimed=True,
        work_id="work-1",
        final_status=ExplorerWorkStatus.SUCCEEDED,
        result_capture_id="capture-result",
        evidence_request_id="request-1",
        restoration_attempt_id="restoration-1",
        equivalence_assessment_id="assessment-1",
        source_grade=EvidenceSourceGrade.A1,
    )

    result = await service.complete(task_id="task-1", outcome=outcome)

    proof = consumption.attach_supplement.await_args.kwargs["proof"]
    assert proof.request_id == "request-1"
    assert proof.result_capture_id == "capture-result"
    assert proof.work_id == "work-1"
    assert proof.restoration_attempt_id == "restoration-1"
    assert proof.equivalence_assessment_id == "assessment-1"
    assert proof.source_grade is EvidenceSourceGrade.A1
    assert result.attached is True


async def test_h5_probe_completion_prepares_manifests_before_attachment() -> None:
    calls = []

    async def prepare(**kwargs):
        calls.append("prepare")

    async def attach(**kwargs):
        calls.append("attach")
        return ProbeSupplementAttachment(
            request_id="request-1", manifest_id="manifest-1", attached=True
        )

    preparation = SimpleNamespace(prepare=AsyncMock(side_effect=prepare))
    consumption = SimpleNamespace(attach_supplement=AsyncMock(side_effect=attach))
    service = H5ProbeCompletionService(consumption=consumption, evidence_preparation=preparation)
    outcome = H5ProbeWorkOutcome(
        claimed=True,
        work_id="work-1",
        final_status=ExplorerWorkStatus.SUCCEEDED,
        result_capture_id="capture-result",
        evidence_request_id="request-1",
        restoration_attempt_id="restoration-1",
        equivalence_assessment_id="assessment-1",
        source_grade=EvidenceSourceGrade.A1,
    )
    await service.complete(task_id="task-1", outcome=outcome)
    assert calls == ["prepare", "attach"]
    preparation.prepare.side_effect = H5ProbeWorkError(
        "清单未通过", error_code="MANIFEST_UNAVAILABLE", retryable=True
    )
    with pytest.raises(H5ProbeWorkError):
        await service.complete(task_id="task-1", outcome=outcome)
    assert calls == ["prepare", "attach"]


async def test_h5_probe_drain_recovers_attachment_before_claiming_new_work() -> None:
    completion = SimpleNamespace(
        recover_next=AsyncMock(
            return_value=ProbeSupplementAttachment(
                request_id="request-1",
                manifest_id="manifest-1",
                attached=True,
            )
        )
    )
    settlement = SimpleNamespace(settle_ready=AsyncMock(return_value=0))
    runner = SimpleNamespace(run_one=AsyncMock())
    wakeup = SimpleNamespace(reopen_waiting_evidence=AsyncMock(return_value=1))
    service = H5ProbeDrainService(
        runner=runner,
        completion=completion,
        terminal_settlement=settlement,
        inspection_wakeup=wakeup,
        policy=build_lab_supplemental_evidence_policy(),
    )

    progressed = await service.drain_one(task_id="task-1", agent_run_id="run-1")

    assert progressed is True
    runner.run_one.assert_not_awaited()
    wakeup.reopen_waiting_evidence.assert_awaited_once_with(task_id="task-1")


async def test_h5_probe_drain_settles_terminal_request_before_claiming_work() -> None:
    completion = SimpleNamespace(recover_next=AsyncMock(return_value=None))
    settlement = SimpleNamespace(settle_ready=AsyncMock(return_value=1))
    runner = SimpleNamespace(run_one=AsyncMock())
    wakeup = SimpleNamespace(reopen_waiting_evidence=AsyncMock(return_value=1))
    service = H5ProbeDrainService(
        runner=runner,
        completion=completion,
        terminal_settlement=settlement,
        inspection_wakeup=wakeup,
        policy=build_lab_supplemental_evidence_policy(),
    )

    progressed = await service.drain_one(task_id="task-1", agent_run_id="run-1")

    assert progressed is True
    settlement.settle_ready.assert_awaited_once_with(
        task_id="task-1",
        agent_run_id="run-1",
    )
    runner.run_one.assert_not_awaited()
    wakeup.reopen_waiting_evidence.assert_awaited_once_with(task_id="task-1")


@pytest.mark.parametrize("retryable", [True, False])
async def test_h5_probe_recovery_failure_is_not_fake_progress(retryable):
    completion = SimpleNamespace(recover_next=AsyncMock(side_effect=H5ProbeWorkError(
        "识别尚未持久化", error_code="H5_PROBE_PAGE_DETECTION_UNAVAILABLE", retryable=retryable,
    )))
    runner = SimpleNamespace(run_one=AsyncMock())
    wakeup = SimpleNamespace(reopen_waiting_evidence=AsyncMock())
    service = H5ProbeDrainService(
        runner=runner, completion=completion,
        terminal_settlement=SimpleNamespace(settle_ready=AsyncMock(return_value=0)),
        inspection_wakeup=wakeup, policy=build_lab_supplemental_evidence_policy(),
    )
    if retryable:
        assert await service.drain_one(task_id="task-1", agent_run_id="run-1") is False
    else:
        with pytest.raises(H5ProbeWorkError):
            await service.drain_one(task_id="task-1", agent_run_id="run-1")
    runner.run_one.assert_not_awaited()
    wakeup.reopen_waiting_evidence.assert_not_awaited()


@pytest.mark.parametrize("settled", [0, 1])
@pytest.mark.parametrize("error_code", [
    "H5_PROBE_CAPTURE_PAIR_MANIFEST_UNAVAILABLE", "H5_PROBE_ATTACHMENT_DEADLINE_EXHAUSTED",
])
async def test_permanent_attachment_failure_settles_only_verified_request(settled, error_code):
    outcome = H5ProbeWorkOutcome(
        claimed=True, work_id="work-1", final_status=ExplorerWorkStatus.SUCCEEDED,
        result_capture_id="capture-result", evidence_request_id="request-1",
        restoration_attempt_id="restoration-1", equivalence_assessment_id="assessment-1",
        source_grade=EvidenceSourceGrade.A1,
    )
    consumption = SimpleNamespace(attach_supplement=AsyncMock())
    completion = H5ProbeCompletionService(
        consumption=consumption,
        recovery=SimpleNamespace(find_next=AsyncMock(return_value=outcome)),
        evidence_preparation=SimpleNamespace(prepare=AsyncMock(side_effect=H5ProbeWorkError(
            "附件质量失败", error_code=error_code,
            retryable=False,
        ))),
    )
    settlement = SimpleNamespace(
        settle_ready=AsyncMock(return_value=0),
        settle_completion_failure=AsyncMock(return_value=settled),
    )
    runner = SimpleNamespace(run_one=AsyncMock())
    wakeup = SimpleNamespace(reopen_waiting_evidence=AsyncMock(return_value=1))
    service = H5ProbeDrainService(
        runner=runner, completion=completion, terminal_settlement=settlement,
        inspection_wakeup=wakeup, policy=build_lab_supplemental_evidence_policy(),
    )
    if settled:
        assert await service.drain_one(task_id="task-1", agent_run_id="run-1") is True
        wakeup.reopen_waiting_evidence.assert_awaited_once_with(task_id="task-1")
    else:
        with pytest.raises(H5ProbeWorkError):
            await service.drain_one(task_id="task-1", agent_run_id="run-1")
        wakeup.reopen_waiting_evidence.assert_not_awaited()
    settlement.settle_completion_failure.assert_awaited_once_with(
        task_id="task-1", agent_run_id="run-1", outcome=outcome,
        error_code=error_code,
    )
    consumption.attach_supplement.assert_not_awaited()
    runner.run_one.assert_not_awaited()


async def test_h5_probe_drain_settles_request_after_terminal_work() -> None:
    completion = SimpleNamespace(recover_next=AsyncMock(return_value=None))
    settlement = SimpleNamespace(
        settle_ready=AsyncMock(side_effect=[0, 1]),
    )
    runner = SimpleNamespace(
        run_one=AsyncMock(
            return_value=H5ProbeWorkOutcome(
                claimed=True,
                work_id="work-1",
                final_status=ExplorerWorkStatus.BLOCKED,
                evidence_request_id="request-1",
            )
        )
    )
    wakeup = SimpleNamespace(reopen_waiting_evidence=AsyncMock(return_value=1))
    service = H5ProbeDrainService(
        runner=runner,
        completion=completion,
        terminal_settlement=settlement,
        inspection_wakeup=wakeup,
        policy=build_lab_supplemental_evidence_policy(),
    )

    progressed = await service.drain_one(task_id="task-1", agent_run_id="run-1")

    assert progressed is True
    assert settlement.settle_ready.await_count == 2
    wakeup.reopen_waiting_evidence.assert_awaited_once_with(task_id="task-1")


async def test_h5_probe_retry_claim_without_evidence_is_not_progress() -> None:
    completion = SimpleNamespace(recover_next=AsyncMock(return_value=None))
    settlement = SimpleNamespace(settle_ready=AsyncMock(return_value=0))
    runner = SimpleNamespace(run_one=AsyncMock(return_value=H5ProbeWorkOutcome(
        claimed=True, work_id="work-1", final_status=ExplorerWorkStatus.RETRY_WAIT,
        evidence_request_id="request-1", error_code="SURFACE_DRIVER_FAILED",
    )))
    wakeup = SimpleNamespace(reopen_waiting_evidence=AsyncMock(return_value=0))
    service = H5ProbeDrainService(
        runner=runner, completion=completion, terminal_settlement=settlement,
        inspection_wakeup=wakeup, policy=build_lab_supplemental_evidence_policy(),
    )
    assert await service.drain_one(task_id="task-1", agent_run_id="run-1") is False
    wakeup.reopen_waiting_evidence.assert_not_awaited()
