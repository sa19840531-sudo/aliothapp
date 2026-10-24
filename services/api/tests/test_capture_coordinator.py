"""Capture Coordinator 调用顺序、关联和失败门禁测试。"""

from dataclasses import replace
from datetime import UTC, datetime

import pytest

from services.api.app.application.app_capture_persistence import AppCaptureRecoveryError
from services.api.app.application.capture_coordinator import (
    AppActionRecoveryBlockedError,
    CaptureCoordinationCommand,
    CaptureCoordinationError,
    CaptureCoordinationStage,
    CaptureCoordinator,
    CapturePhase,
)
from services.api.app.application.evidence_exit_gate import (
    EvidenceExitGateBlockedError,
    build_evidence_exit_blocked_outcome,
    is_trusted_initial_surface_action,
    requires_evidence_exit_gate,
    trusted_h5_browser_recovery_identity,
)
from services.api.app.application.h5_exploration_recovery import (
    H5FailureStage,
    build_h5_recovery_session_id,
)
from services.api.app.application.supplemental_evidence_policy import (
    SupplementalEvidenceDisabledError,
)
from services.api.app.domain.exploration import (
    EvidenceExitGateSnapshot,
    EvidenceExitReservationStatus,
    build_evidence_exit_gate_decision,
)
from services.api.app.domain.interaction import (
    ActionExecutionSnapshot,
    ActionExecutionStatus,
    ActionIntent,
    CaptureDirective,
    CaptureKind,
    CaptureProfile,
    CaptureRef,
    InteractionExecutionOutcome,
    InteractionOperation,
    InteractionResult,
    InteractionResultStatus,
    InteractionRiskLevel,
    InteractionVerificationOutcome,
    SurfaceKind,
    TimedObservationIntent,
    build_action_execution_id,
    build_action_intent_hash,
)
from services.api.app.infrastructure.db.evidence_exit_gate_repository import (
    EvidenceExitGatePersistenceError,
)


def _allowed_gate_decision(*, intent_id: str = "intent-1"):
    snapshot = EvidenceExitGateSnapshot(
        task_id="task-1",
        session_id="session-1",
        source_capture_id="capture-parent",
        requirement_set_id="requirements-1",
        baseline_pass_manifest_count=1,
        nonterminal_active_session_requirement_count=0,
        nonterminal_inline_evidence_request_count=0,
        evidence_commit_in_progress_count=0,
        evaluated_at=datetime(2026, 8, 3, 12, 0, tzinfo=UTC),
    )
    return build_evidence_exit_gate_decision(
        action_intent_id=intent_id,
        snapshot=snapshot,
    )


class RecordingEvidenceExitGate:
    def __init__(
        self,
        *,
        blocked: bool = False,
        finalize_error: BaseException | None = None,
    ) -> None:
        self.blocked = blocked
        self.finalize_error = finalize_error
        self.reservations = []
        self.recovery_reservations = []
        self.stale_finalizations = []
        self.finalizations = []

    async def reserve(self, *, intent, permit):
        self.reservations.append((intent, permit))
        decision = _allowed_gate_decision(intent_id=intent.intent_id)
        if self.blocked:
            snapshot = replace(
                decision.snapshot,
                baseline_pass_manifest_count=0,
                latest_manifest_id="manifest-1",
                latest_manifest_version=1,
                latest_manifest_hash="a" * 64,
                latest_manifest_quality_status="fail",
                latest_manifest_quality_reason="缺少证据",
            )
            blocked_decision = build_evidence_exit_gate_decision(
                action_intent_id=intent.intent_id,
                snapshot=snapshot,
            )
            raise EvidenceExitGateBlockedError(
                blocked_decision,
                build_evidence_exit_blocked_outcome(decision=blocked_decision),
            )
        return decision

    async def finalize_stale_action_reservations(
        self,
        *,
        intent,
        permit,
        action_execution_id,
        status,
    ):
        self.stale_finalizations.append(
            {
                "intent": intent,
                "permit": permit,
                "action_execution_id": action_execution_id,
                "status": status,
            }
        )
        return 1

    async def reserve_action_recovery(
        self,
        *,
        intent,
        permit,
        action_execution_id,
        before_capture_id,
    ):
        self.recovery_reservations.append(
            {
                "intent": intent,
                "permit": permit,
                "action_execution_id": action_execution_id,
                "before_capture_id": before_capture_id,
            }
        )
        decision = _allowed_gate_decision(intent_id=intent.intent_id)
        if self.blocked:
            snapshot = replace(
                decision.snapshot,
                baseline_pass_manifest_count=0,
            )
            raise EvidenceExitGateBlockedError(
                build_evidence_exit_gate_decision(
                    action_intent_id=intent.intent_id,
                    snapshot=snapshot,
                )
            )
        return decision

    async def finalize(self, *, decision, status):
        self.finalizations.append((decision, status))
        if self.finalize_error is not None:
            raise self.finalize_error

    async def expire_stale(self, *, limit=100):
        del limit
        return 0


class RecordingCaptureWriter:
    def __init__(
        self,
        events,
        *,
        fail_phase=None,
        wrong_after_parent=False,
        wrong_task=False,
        transient_fail_times: int = 0,
        transient_fail_message: str = "capture unavailable",
    ) -> None:
        self.events = events
        self.fail_phase = fail_phase
        self.wrong_after_parent = wrong_after_parent
        self.wrong_task = wrong_task
        self.transient_fail_times = transient_fail_times
        self._initial_transient_fail_times = transient_fail_times
        self.transient_fail_message = transient_fail_message
        self.requests = []

    async def capture(self, request):
        self.events.append(f"capture:{request.phase.value}")
        self.requests.append(request)
        phase_matches = self.fail_phase is None or request.phase == self.fail_phase
        if self.transient_fail_times > 0 and phase_matches:
            self.transient_fail_times -= 1
            raise RuntimeError(self.transient_fail_message)
        # 仅当未配置瞬时失败时，fail_phase 才表示永久失败。
        if request.phase == self.fail_phase and self._initial_transient_fail_times == 0:
            raise RuntimeError("capture unavailable")
        is_after = request.phase == CapturePhase.AFTER_ACTION
        return CaptureRef(
            task_id="other-task" if self.wrong_task else request.task_id,
            session_id=request.session_id,
            capture_id="capture-after" if is_after else "capture-before",
            surface_kind=request.surface_kind,
            capture_kind=(
                CaptureKind.PAGE if request.surface_kind is SurfaceKind.WEB else CaptureKind.APP
            ),
            role=request.directive.role,
            artifact_ids=("artifact-after" if is_after else "artifact-before",),
            parent_capture_id=(
                "wrong-parent"
                if is_after and self.wrong_after_parent
                else request.parent_capture_id
            ),
            action_execution_id=request.action_execution_id,
        )


class RecordingInteractionExecutor:
    def __init__(
        self,
        events,
        *,
        status=InteractionResultStatus.EVIDENCE_PENDING,
    ) -> None:
        self.events = events
        self.status = status
        self.requests = []

    async def execute(self, request):
        self.events.append("execute")
        self.requests.append(request)
        return InteractionResult(
            task_id=request.intent.task_id,
            session_id=request.intent.session_id,
            intent_id=request.intent.intent_id,
            action_execution_id=request.action_execution_id or "action-1",
            source_capture_id=request.source_capture_id,
            status=self.status,
            error_code=("driver_failed" if self.status == InteractionResultStatus.FAILED else None),
        )


class FailingInteractionExecutor:
    async def execute(self, request):
        del request
        raise TimeoutError("device interaction channel timed out")


def _command(*, surface_kind: SurfaceKind = SurfaceKind.WEB) -> CaptureCoordinationCommand:
    return CaptureCoordinationCommand(
        agent_run_id="run-1",
        intent=ActionIntent(
            task_id="task-1",
            session_id="session-1",
            intent_id="intent-1",
            idempotency_key="task-1:branch-1:intent-1",
            surface_kind=surface_kind,
            source_capture_id="capture-parent",
            action_type=InteractionOperation.CLICK,
            semantic_target="登录按钮",
            purpose="进入登录页",
            capture_profile=CaptureProfile(
                profile_id="critical-action",
                before=CaptureDirective(
                    role="before",
                    artifact_types=("screenshot", "ui_snapshot"),
                ),
                after=CaptureDirective(
                    role="primary",
                    artifact_types=("screenshot", "ui_snapshot", "action_trace"),
                ),
            ),
            deadline=datetime(2026, 8, 3, 18, 0, tzinfo=UTC),
        ),
    )


def _web_primary_content_command() -> CaptureCoordinationCommand:
    command = _command()
    return replace(
        command,
        intent=replace(
            command.intent,
            action_type=InteractionOperation.SCROLL,
            semantic_target="滚动到非广告主体内容",
            parameters={
                "target_mode": "web_primary_content_candidate",
                "web_primary_content_candidate_id": "web-primary-content:target",
                "web_primary_content_discovery_capture_id": "capture-discovery",
                "web_primary_content_source_capture_id": "capture-discovery",
                "expected_page_type": "content_detail",
                "logical_page_id": "web-page:target",
            },
        ),
    )


def _web_auxiliary_command() -> CaptureCoordinationCommand:
    command = _command()
    return replace(
        command,
        intent=replace(
            command.intent,
            action_type=InteractionOperation.NAVIGATE,
            semantic_target="站内搜索",
            parameters={
                "target_mode": "web_search_discovery_candidate",
                "destination_url": "https://variable-layout.test/find",
                "web_auxiliary_source_capture_id": "capture-discovery",
                "web_auxiliary_candidate_source_capture_id": "capture-discovery",
            },
        ),
    )


def _web_detail_command() -> CaptureCoordinationCommand:
    command = _command()
    return replace(
        command,
        intent=replace(
            command.intent,
            action_type=InteractionOperation.NAVIGATE,
            semantic_target="打开当前列表可信详情",
            parameters={
                "target_mode": "web_detail_candidate_url",
                "destination_url": "https://variable-layout.test/article/1",
                "web_detail_source_capture_id": "capture-discovery",
            },
        ),
    )


async def test_coordinator_enforces_before_action_after_order_and_links() -> None:
    events = []
    writer = RecordingCaptureWriter(events)
    executor = RecordingInteractionExecutor(events)
    exit_gate = RecordingEvidenceExitGate()
    coordinator = CaptureCoordinator(
        capture_writer=writer,
        interaction_executor=executor,
        evidence_exit_gate=exit_gate,
    )

    result = await coordinator.coordinate(_command())

    assert events == ["capture:before_action", "execute", "capture:after_action"]
    assert writer.requests[0].parent_capture_id == "capture-parent"
    assert writer.requests[0].surface_kind is SurfaceKind.WEB
    assert executor.requests[0].source_capture_id == "capture-before"
    after_request = writer.requests[1]
    assert after_request.parent_capture_id == "capture-before"
    assert after_request.action_execution_id == "action-1"
    assert result.after_capture.capture_id == "capture-after"
    assert result.interaction.status == InteractionResultStatus.SUCCEEDED
    assert result.interaction.result_capture_id == "capture-after"
    assert result.exit_gate_decision == exit_gate.finalizations[0][0]
    assert exit_gate.finalizations[0][1] is EvidenceExitReservationStatus.CONSUMED


@pytest.mark.asyncio
async def test_coordinator_propagates_gate_persistence_failure_after_coordination_failure() -> None:
    events = []
    gate = RecordingEvidenceExitGate(
        finalize_error=EvidenceExitGatePersistenceError("离场预留无法持久化")
    )
    coordinator = CaptureCoordinator(
        capture_writer=RecordingCaptureWriter(events, fail_phase=CapturePhase.BEFORE_ACTION),
        interaction_executor=RecordingInteractionExecutor(events),
        evidence_exit_gate=gate,
    )

    with pytest.raises(EvidenceExitGatePersistenceError) as error:
        await coordinator.coordinate(_command())

    assert isinstance(error.value.__cause__, CaptureCoordinationError)
    assert gate.finalizations[0][1] is EvidenceExitReservationStatus.CANCELLED


@pytest.mark.asyncio
async def test_coordinator_injects_before_capture_only_for_web_primary_content_intent() -> None:
    events = []
    executor = RecordingInteractionExecutor(events)
    coordinator = CaptureCoordinator(
        capture_writer=RecordingCaptureWriter(events),
        interaction_executor=executor,
        evidence_exit_gate=RecordingEvidenceExitGate(),
    )

    await coordinator.coordinate(_web_primary_content_command())

    executed_intent = executor.requests[0].intent
    assert executed_intent.parameters["web_primary_content_before_action_capture_id"] == (
        "capture-before"
    )
    assert executed_intent.parameters["web_primary_content_discovery_capture_id"] == (
        "capture-discovery"
    )


@pytest.mark.asyncio
async def test_coordinator_injects_fresh_before_capture_for_web_auxiliary_intent() -> None:
    events = []
    executor = RecordingInteractionExecutor(events)
    coordinator = CaptureCoordinator(
        capture_writer=RecordingCaptureWriter(events),
        interaction_executor=executor,
        evidence_exit_gate=RecordingEvidenceExitGate(),
    )

    await coordinator.coordinate(_web_auxiliary_command())

    executed_intent = executor.requests[0].intent
    assert executed_intent.parameters["web_auxiliary_before_action_capture_id"] == (
        "capture-before"
    )
    assert executed_intent.parameters["web_auxiliary_source_capture_id"] == ("capture-discovery")


@pytest.mark.asyncio
async def test_coordinator_injects_fresh_before_capture_for_web_detail_intent() -> None:
    events = []
    executor = RecordingInteractionExecutor(events)
    coordinator = CaptureCoordinator(
        capture_writer=RecordingCaptureWriter(events),
        interaction_executor=executor,
        evidence_exit_gate=RecordingEvidenceExitGate(),
    )

    await coordinator.coordinate(_web_detail_command())

    executed_intent = executor.requests[0].intent
    assert executed_intent.parameters["web_detail_before_action_capture_id"] == "capture-before"
    assert executed_intent.parameters["web_detail_source_capture_id"] == "capture-discovery"


@pytest.mark.asyncio
async def test_coordinator_keeps_ordinary_web_h5_and_app_intents_unchanged() -> None:
    for command in (
        _command(),
        replace(
            _command(),
            intent=replace(_command().intent, parameters={"h5_navigation": True}),
        ),
        _command(surface_kind=SurfaceKind.ANDROID),
    ):
        events = []
        executor = RecordingInteractionExecutor(events)
        coordinator = CaptureCoordinator(
            capture_writer=RecordingCaptureWriter(events),
            interaction_executor=executor,
            evidence_exit_gate=RecordingEvidenceExitGate(),
        )

        await coordinator.coordinate(command)

        assert executor.requests[0].intent is command.intent


@pytest.mark.parametrize("stage", ["web_page_probe", "web_primary_content_evidence"])
async def test_web_capture_stage_is_forwarded_to_before_and_after_capture(stage) -> None:
    events = []
    writer = RecordingCaptureWriter(events)
    command = _command()
    command = replace(
        command,
        intent=replace(
            command.intent,
            parameters={"web_capture_stage": stage},
        ),
    )
    coordinator = CaptureCoordinator(
        capture_writer=writer,
        interaction_executor=RecordingInteractionExecutor(events),
        evidence_exit_gate=RecordingEvidenceExitGate(),
    )

    await coordinator.coordinate(command)

    assert [request.web_capture_stage for request in writer.requests] == [
        stage,
        stage,
    ]


async def test_web_capture_stage_is_not_forwarded_to_app_capture() -> None:
    events = []
    writer = RecordingCaptureWriter(events)
    command = _command(surface_kind=SurfaceKind.ANDROID)
    command = replace(
        command,
        intent=replace(
            command.intent,
            parameters={"web_capture_stage": "web_page_probe"},
        ),
    )
    coordinator = CaptureCoordinator(
        capture_writer=writer,
        interaction_executor=RecordingInteractionExecutor(events),
        evidence_exit_gate=RecordingEvidenceExitGate(),
    )

    await coordinator.coordinate(command)

    assert [request.web_capture_stage for request in writer.requests] == [None, None]


@pytest.mark.parametrize("session_id,error_code", [
    ("web-v2:run-1", "LOGICAL_TRANSITION_UNVERIFIED"),
    ("h5:run-1", "POPUP_DISMISSAL_NOT_OBSERVED"),
])
async def test_logical_transition_failure_still_seals_after_action_capture(
    session_id, error_code,
) -> None:
    """动作已发生但后验失败时仍封存后态，不将留证改成执行成功。"""

    class LogicalTransitionFailureExecutor:
        async def execute(self, request):
            return InteractionResult(
                task_id=request.intent.task_id,
                session_id=request.intent.session_id,
                intent_id=request.intent.intent_id,
                action_execution_id=request.action_execution_id or "action-logical-1",
                source_capture_id=request.source_capture_id,
                status=InteractionResultStatus.BLOCKED,
                error_code=error_code,
                error_message="动作后证据未证明进入新的逻辑页面",
                retryable=True,
                state_summary={"post_action_evidence_available": True},
            )

    events: list[str] = []
    writer = RecordingCaptureWriter(events)
    coordinator = CaptureCoordinator(
        capture_writer=writer,
        interaction_executor=LogicalTransitionFailureExecutor(),
        evidence_exit_gate=RecordingEvidenceExitGate(),
    )

    command = _command()
    command = replace(command, intent=replace(command.intent, session_id=session_id))
    result = await coordinator.coordinate(command)

    assert events == ["capture:before_action", "capture:after_action"]
    assert result.interaction.status is InteractionResultStatus.BLOCKED
    assert result.interaction.error_code == error_code
    assert result.interaction.result_capture_id == "capture-after"


@pytest.mark.parametrize(
    ("source_capture_id", "parameters"),
    [
        (
            "h5-entry:run-1",
            {
                "destination_url": "https://m.example.test/home",
                "initial_surface_action": True,
            },
        ),
        ("agent-entry:session-1", {"destination_url": "https://m.example.test/home"}),
        ("legacy-agent-entry", {"destination_url": "https://m.example.test/home"}),
    ],
)
async def test_initial_surface_action_persists_before_capture_without_fake_parent(
    source_capture_id: str,
    parameters: dict[str, object],
) -> None:
    """首跳 before Capture 必须是根证据，不能引用并不存在的入口 Capture。"""
    events = []
    writer = RecordingCaptureWriter(events)
    coordinator = CaptureCoordinator(
        capture_writer=writer,
        interaction_executor=RecordingInteractionExecutor(events),
        evidence_exit_gate=RecordingEvidenceExitGate(),
    )
    command = _command()
    initial_intent = replace(
        command.intent,
        source_capture_id=source_capture_id,
        action_type=InteractionOperation.NAVIGATE,
        semantic_target="H5 任务锁定入口",
        parameters=parameters,
    )

    result = await coordinator.coordinate(replace(command, intent=initial_intent))

    assert writer.requests[0].parent_capture_id is None
    assert result.before_capture.parent_capture_id is None
    assert result.after_capture.parent_capture_id == result.before_capture.capture_id
    # 首跳的 before 采集发生在导航之前，Surface 上还没有任何页面；
    # 交接门禁又要求 before/after 成对留证，只能先开空白 Surface 再观察。
    assert writer.requests[0].bootstrap_blank_surface is True
    # after 采集时页面已由 Navigate 建立，不得再借入口通道
    assert writer.requests[1].bootstrap_blank_surface is False


async def test_non_initial_action_never_bootstraps_blank_surface() -> None:
    """普通动作必须有真实来源页面，不能靠空白 Surface 兜底。"""
    events = []
    writer = RecordingCaptureWriter(events)
    coordinator = CaptureCoordinator(
        capture_writer=writer,
        interaction_executor=RecordingInteractionExecutor(events),
        evidence_exit_gate=RecordingEvidenceExitGate(),
    )

    await coordinator.coordinate(_command())

    assert all(request.bootstrap_blank_surface is False for request in writer.requests)


def test_only_trusted_initial_navigation_bypasses_exit_gate() -> None:
    command = _command()
    initial = replace(
        command.intent,
        source_capture_id="h5-entry:run-1",
        action_type=InteractionOperation.NAVIGATE,
        semantic_target="H5 任务锁定入口",
        parameters={
            "destination_url": "https://m.example.test/home",
            "initial_surface_action": True,
        },
    )
    forged = replace(initial, source_capture_id="capture-parent")

    assert requires_evidence_exit_gate(initial) is False
    assert requires_evidence_exit_gate(forged) is True


def _recovery_intent(
    *,
    stage: H5FailureStage = H5FailureStage.ACTION,
    attempt: int = 1,
) -> ActionIntent:
    work_id = "work-browser-loss-1"
    run_id = "run-1"
    intent_id = f"h5-browser-recovery:{work_id}:{stage.value}:{attempt}"
    destination = "https://m.example.test/account"
    return replace(
        _command().intent,
        session_id=build_h5_recovery_session_id(
            agent_run_id=run_id,
            work_id=work_id,
            stage=stage,
            recovery_attempt_count=attempt,
        ),
        intent_id=intent_id,
        idempotency_key=intent_id,
        source_capture_id="capture-from-lost-session",
        action_type=InteractionOperation.NAVIGATE,
        semantic_target=destination,
        purpose="恢复丢失的 H5 浏览器会话并重建可信基线",
        risk_level=InteractionRiskLevel.LOW,
        parameters={
            "destination_url": destination,
            "h5_browser_recovery": True,
            "recovery_source_session_id": f"h5:{run_id}",
            "failure_stage": stage.value,
        },
    )


def test_only_fully_bound_h5_browser_recovery_navigation_bypasses_exit_gate() -> None:
    trusted = _recovery_intent()
    assert requires_evidence_exit_gate(trusted) is False

    mutations = (
        replace(trusted, idempotency_key="forged"),
        replace(trusted, purpose="普通导航"),
        replace(trusted, risk_level=InteractionRiskLevel.HIGH),
        replace(trusted, semantic_target="https://m.example.test/other"),
        replace(trusted, parameters={**trusted.parameters, "extra": True}),
        replace(
            trusted,
            parameters={**trusted.parameters, "failure_stage": "after_capture"},
        ),
        _recovery_intent(stage=H5FailureStage.BEFORE_CAPTURE, attempt=2),
        _recovery_intent(stage=H5FailureStage.ACTION, attempt=3),
    )
    assert all(requires_evidence_exit_gate(intent) is True for intent in mutations)


def test_only_fully_bound_h5_targeted_recapture_action_bypasses_exit_gate() -> None:
    intent_id = "probe-intent:" + "a" * 64
    trusted = replace(
        _command().intent,
        session_id="h5:run-1",
        intent_id=intent_id,
        idempotency_key=intent_id,
        surface_kind=SurfaceKind.WEB,
        action_type=InteractionOperation.CLICK,
        capture_profile=CaptureProfile(
            profile_id="probe-click-v1",
            before=CaptureDirective(
                role="probe_before",
                artifact_types=("screenshot", "ui_snapshot"),
            ),
            after=CaptureDirective(
                role="probe_result_replay",
                artifact_types=("screenshot", "ui_snapshot", "action_trace"),
            ),
        ),
        parameters={
            "evidence_request_id": "evidence-request:1",
            "exploration_work_id": "probe-work:1",
            "restoration_plan_hash": "restoration-plan:1",
            "restoration_attempt_id": "restoration-attempt:1",
            "equivalence_assessment_id": "equivalence-assessment:1",
            "supplemental_evidence_source": "targeted_recapture",
            "viewport_width": 390,
            "viewport_height": 844,
        },
    )

    assert requires_evidence_exit_gate(trusted) is False
    mutations = (
        replace(trusted, session_id="web-v2:run-1"),
        replace(trusted, intent_id="probe-intent:forged", idempotency_key="probe-intent:forged"),
        replace(trusted, capture_profile=replace(
            trusted.capture_profile,
            after=replace(trusted.capture_profile.after, role="after_action"),
        )),
        replace(trusted, parameters={**trusted.parameters, "evidence_request_id": ""}),
        replace(trusted, parameters={**trusted.parameters, "viewport_width": 1920}),
    )
    assert all(requires_evidence_exit_gate(intent) is True for intent in mutations)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "destination",
    (
        "http://[",
        "https://m.example.test:invalid/account",
        "https://user@m.example.test/account",
    ),
)
async def test_invalid_recovery_destination_is_rejected_before_gate_and_executor(
    destination: str,
) -> None:
    intent = _recovery_intent()
    intent = replace(
        intent,
        semantic_target=destination,
        parameters={**intent.parameters, "destination_url": destination},
    )
    assert trusted_h5_browser_recovery_identity(intent) is None
    assert requires_evidence_exit_gate(intent) is True
    events: list[str] = []
    executor = RecordingInteractionExecutor(events)
    coordinator = CaptureCoordinator(
        capture_writer=RecordingCaptureWriter(events),
        interaction_executor=executor,
        evidence_exit_gate=RecordingEvidenceExitGate(blocked=True),
    )

    with pytest.raises(CaptureCoordinationError) as caught:
        await coordinator.coordinate(replace(_command(), intent=intent))

    assert caught.value.error_code == "COMMAND_DESTINATION_INVALID"
    assert caught.value.stage is CaptureCoordinationStage.BEFORE_CAPTURE
    assert executor.requests == []


async def test_timed_observation_creates_one_capture_without_action_execution() -> None:
    events = []
    writer = RecordingCaptureWriter(events)
    coordinator = CaptureCoordinator(
        capture_writer=writer,
        interaction_executor=RecordingInteractionExecutor(events),
        evidence_exit_gate=RecordingEvidenceExitGate(),
    )
    timed_intent = TimedObservationIntent(
        task_id="task-1",
        session_id="session-1",
        phase_run_id="phase-1",
        decision_id="decision-1",
        observation_id="observation-1",
        source_capture_id="capture-parent",
        surface_kind=SurfaceKind.ANDROID,
        delay_ms=0,
        directive=CaptureDirective(
            role="observation",
            artifact_types=("screenshot", "ui_tree"),
            inherit_parent_semantics=True,
        ),
        deadline=datetime(2026, 8, 20, 18, 0, tzinfo=UTC),
    )

    result = await coordinator.capture_observation(
        agent_run_id="run-1",
        intent=timed_intent,
    )

    assert events == ["capture:observation"]
    assert len(writer.requests) == 1
    request = writer.requests[0]
    assert request.phase is CapturePhase.OBSERVATION
    assert request.parent_capture_id == "capture-parent"
    assert request.action_execution_id is None
    assert request.observation_context == {
        "observation_id": "observation-1",
        "surface_phase_run_id": "phase-1",
        "surface_phase_decision_id": "decision-1",
        "semantic_role": "surface_reobserve",
    }
    assert result.action_execution_id is None


async def test_supplemental_intent_is_blocked_before_capture_and_driver() -> None:
    events = []
    exit_gate = RecordingEvidenceExitGate()
    coordinator = CaptureCoordinator(
        capture_writer=RecordingCaptureWriter(events),
        interaction_executor=RecordingInteractionExecutor(events),
        evidence_exit_gate=exit_gate,
    )
    command = _command()
    supplemental_intent = replace(
        command.intent,
        parameters={"evidence_request_id": "request-1"},
    )

    with pytest.raises(SupplementalEvidenceDisabledError) as error:
        await coordinator.coordinate(replace(command, intent=supplemental_intent))

    assert error.value.operation.value == "capture_supplement"
    assert events == []
    assert exit_gate.reservations == []


async def test_coordinator_accepts_android_capture_refs() -> None:
    events = []
    writer = RecordingCaptureWriter(events)
    coordinator = CaptureCoordinator(
        capture_writer=writer,
        interaction_executor=RecordingInteractionExecutor(events),
        evidence_exit_gate=RecordingEvidenceExitGate(),
    )

    result = await coordinator.coordinate(_command(surface_kind=SurfaceKind.ANDROID))

    assert result.before_capture.capture_kind is CaptureKind.APP
    assert result.before_capture.surface_kind is SurfaceKind.ANDROID
    assert result.after_capture.parent_capture_id == result.before_capture.capture_id


async def test_before_capture_failure_stops_action() -> None:
    events = []
    coordinator = CaptureCoordinator(
        capture_writer=RecordingCaptureWriter(
            events,
            fail_phase=CapturePhase.BEFORE_ACTION,
        ),
        interaction_executor=RecordingInteractionExecutor(events),
        evidence_exit_gate=(exit_gate := RecordingEvidenceExitGate()),
    )

    with pytest.raises(CaptureCoordinationError) as error:
        await coordinator.coordinate(_command())

    assert error.value.stage == CaptureCoordinationStage.BEFORE_CAPTURE
    assert error.value.error_code == "before_capture_failed"
    assert events == ["capture:before_action"]
    assert error.value.before_capture is None
    assert exit_gate.finalizations[0][1] is EvidenceExitReservationStatus.CANCELLED


async def test_exit_gate_block_stops_capture_and_action() -> None:
    events = []
    exit_gate = RecordingEvidenceExitGate(blocked=True)
    capture_writer = RecordingCaptureWriter(events)
    interaction_executor = RecordingInteractionExecutor(events)
    coordinator = CaptureCoordinator(
        capture_writer=capture_writer,
        interaction_executor=interaction_executor,
        evidence_exit_gate=exit_gate,
    )

    with pytest.raises(EvidenceExitGateBlockedError) as error:
        await coordinator.coordinate(_command())

    assert error.value.decision.allow_leave is False
    assert events == []
    assert capture_writer.requests == []
    assert interaction_executor.requests == []
    assert len(exit_gate.reservations) == 1
    assert exit_gate.finalizations == []


async def test_capture_reference_must_belong_to_exact_task_and_session() -> None:
    events = []
    coordinator = CaptureCoordinator(
        capture_writer=RecordingCaptureWriter(events, wrong_task=True),
        interaction_executor=RecordingInteractionExecutor(events),
        evidence_exit_gate=RecordingEvidenceExitGate(),
    )

    with pytest.raises(CaptureCoordinationError) as error:
        await coordinator.coordinate(_command())

    assert error.value.stage == CaptureCoordinationStage.BEFORE_CAPTURE
    assert events == ["capture:before_action"]


async def test_failed_action_never_reports_capture_success() -> None:
    events = []
    coordinator = CaptureCoordinator(
        capture_writer=RecordingCaptureWriter(events),
        interaction_executor=RecordingInteractionExecutor(
            events,
            status=InteractionResultStatus.FAILED,
        ),
        evidence_exit_gate=(exit_gate := RecordingEvidenceExitGate()),
    )

    with pytest.raises(CaptureCoordinationError) as error:
        await coordinator.coordinate(_command())

    assert error.value.stage == CaptureCoordinationStage.ACTION
    assert error.value.error_code == "driver_failed"
    assert error.value.interaction.error_code == "driver_failed"
    assert events == ["capture:before_action", "execute"]
    assert exit_gate.finalizations[0][1] is EvidenceExitReservationStatus.CONSUMED


async def test_app_failed_action_keeps_execution_truth_with_after_capture() -> None:
    """App 动作失败后允许留存 after 截图，但不能改写执行事实。"""
    events = []
    coordinator = CaptureCoordinator(
        capture_writer=RecordingCaptureWriter(events),
        interaction_executor=RecordingInteractionExecutor(
            events,
            status=InteractionResultStatus.FAILED,
        ),
        evidence_exit_gate=(exit_gate := RecordingEvidenceExitGate()),
    )

    result = await coordinator.coordinate(_command(surface_kind=SurfaceKind.ANDROID))

    assert events == ["capture:before_action", "execute", "capture:after_action"]
    assert result.after_capture.capture_id == "capture-after"
    assert result.interaction.status is InteractionResultStatus.FAILED
    assert result.interaction.result_capture_id == "capture-after"
    assert result.interaction.execution_outcome is InteractionExecutionOutcome.FAILED
    assert result.interaction.verification_outcome is InteractionVerificationOutcome.UNVERIFIED
    assert "action_soft_continued" not in result.interaction.state_summary
    assert exit_gate.finalizations[0][1] is EvidenceExitReservationStatus.CONSUMED


async def test_app_coordinator_writes_action_log_after_success() -> None:
    events = []
    attachments = []

    class Writer:
        async def attach_action_log(self, **kwargs):
            attachments.append(kwargs)

    coordinator = CaptureCoordinator(
        capture_writer=RecordingCaptureWriter(events),
        interaction_executor=RecordingInteractionExecutor(events),
        evidence_exit_gate=RecordingEvidenceExitGate(),
        action_evidence_writer=Writer(),
    )

    await coordinator.coordinate(_command(surface_kind=SurfaceKind.ANDROID))

    assert len(attachments) == 1
    assert attachments[0]["after_capture_id"] == "capture-after"
    assert attachments[0]["before_capture_id"] == "capture-before"
    assert attachments[0]["entry"]["action"] == "click"
    assert attachments[0]["entry"]["purpose"] == "进入登录页"


async def test_app_feedback_click_adds_a_distinct_refresh_capture() -> None:
    events = []
    attachments = []

    class RefreshWriter(RecordingCaptureWriter):
        async def capture(self, request):
            ref = await super().capture(request)
            if request.phase is CapturePhase.OBSERVATION:
                return replace(ref, capture_id="capture-refresh")
            return ref

    class LogWriter:
        async def attach_action_log(self, **kwargs):
            attachments.append(kwargs)

    base = _command(surface_kind=SurfaceKind.ANDROID)
    command = replace(base, intent=replace(base.intent, semantic_target="关注"))
    coordinator = CaptureCoordinator(
        capture_writer=RefreshWriter(events),
        interaction_executor=RecordingInteractionExecutor(events),
        evidence_exit_gate=RecordingEvidenceExitGate(),
        action_evidence_writer=LogWriter(),
    )

    await coordinator.coordinate(command)

    assert events == [
        "capture:before_action",
        "execute",
        "capture:after_action",
        "capture:observation",
    ]
    feedback = attachments[0]["entry"]["interaction_feedback"]
    assert feedback["complete"] is True
    assert feedback["refreshed_capture_id"] == "capture-refresh"
    assert attachments[0]["entry"]["evidence_capture_roles"]["refreshed"] == "capture-refresh"


async def test_missing_refresh_capture_keeps_the_feedback_click_and_records_the_gap() -> None:
    events = []
    attachments = []

    class LogWriter:
        async def attach_action_log(self, **kwargs):
            attachments.append(kwargs)

    base = _command(surface_kind=SurfaceKind.ANDROID)
    command = replace(base, intent=replace(base.intent, semantic_target="点赞"))
    coordinator = CaptureCoordinator(
        capture_writer=RecordingCaptureWriter(events, fail_phase=CapturePhase.OBSERVATION),
        interaction_executor=RecordingInteractionExecutor(events),
        evidence_exit_gate=RecordingEvidenceExitGate(),
        action_evidence_writer=LogWriter(),
    )

    result = await coordinator.coordinate(command)

    assert result.after_capture is not None
    assert events[-1] == "capture:observation"
    assert attachments[0]["entry"]["gap_codes"] == ["interaction_refresh_capture_missing"]
    assert "refreshed" not in attachments[0]["entry"]["evidence_capture_roles"]


async def test_after_capture_failure_returns_evidence_failed_status() -> None:
    events = []
    coordinator = CaptureCoordinator(
        capture_writer=RecordingCaptureWriter(
            events,
            fail_phase=CapturePhase.AFTER_ACTION,
        ),
        interaction_executor=RecordingInteractionExecutor(events),
        evidence_exit_gate=RecordingEvidenceExitGate(),
    )

    with pytest.raises(CaptureCoordinationError) as error:
        await coordinator.coordinate(_command())

    assert error.value.stage == CaptureCoordinationStage.AFTER_CAPTURE
    assert error.value.before_capture.capture_id == "capture-before"
    assert error.value.interaction.action_execution_id == "action-1"
    assert error.value.interaction.status == InteractionResultStatus.EVIDENCE_FAILED
    assert error.value.to_dict()["interaction"]["status"] == "evidence_failed"
    assert events == ["capture:before_action", "execute", "capture:after_action"]


async def test_after_capture_must_reference_exact_parent_and_action() -> None:
    events = []
    coordinator = CaptureCoordinator(
        capture_writer=RecordingCaptureWriter(events, wrong_after_parent=True),
        interaction_executor=RecordingInteractionExecutor(events),
        evidence_exit_gate=RecordingEvidenceExitGate(),
    )

    with pytest.raises(CaptureCoordinationError) as error:
        await coordinator.coordinate(_command())

    assert error.value.stage == CaptureCoordinationStage.AFTER_CAPTURE
    assert error.value.error_code == "after_capture_failed"
    assert error.value.interaction.status == InteractionResultStatus.EVIDENCE_FAILED
    assert error.value.after_capture.capture_id == "capture-after"


class RecordingActionExecutionRepository:
    def __init__(self, events) -> None:
        self.events = events
        self.state = None
        self.get_calls = []
        self.resume_calls = []
        self.recover_calls = []

    async def get(self, *, action_execution_id):
        self.get_calls.append(action_execution_id)
        if self.state is None or self.state.action_execution_id != action_execution_id:
            return None
        return self.state

    async def plan(self, *, intent, permit):
        self.events.append("action:planned")
        self.state = ActionExecutionSnapshot(
            action_execution_id=build_action_execution_id(intent),
            task_id=intent.task_id,
            session_id=intent.session_id,
            intent_id=intent.intent_id,
            intent_hash=build_action_intent_hash(intent),
            command_sequence=permit.command_sequence,
            finalization_epoch=permit.finalization_epoch,
            requested_source_capture_id=(
                None if is_trusted_initial_surface_action(intent) else intent.source_capture_id
            ),
            status=ActionExecutionStatus.PLANNED,
            row_version=0,
            created_at=datetime.now(UTC),
        )
        return self.state

    async def mark_executing(
        self,
        *,
        action_execution_id,
        permit,
        source_capture_id,
        expected_row_version,
    ):
        del action_execution_id, permit
        assert expected_row_version == self.state.row_version
        self.events.append("action:executing")
        self.state = replace(
            self.state,
            status=ActionExecutionStatus.EXECUTING,
            source_capture_id=source_capture_id,
            started_at=datetime.now(UTC),
            row_version=self.state.row_version + 1,
        )
        return self.state

    async def resume_planned_with_capture(
        self,
        *,
        action_execution_id,
        permit,
        source_capture_id,
        expected_row_version,
    ):
        assert self.state is not None
        assert action_execution_id == self.state.action_execution_id
        assert self.state.status is ActionExecutionStatus.PLANNED
        assert expected_row_version == self.state.row_version
        self.events.append("action:resume_planned")
        self.resume_calls.append(
            {
                "action_execution_id": action_execution_id,
                "source_capture_id": source_capture_id,
                "permit": permit,
            }
        )
        self.state = replace(
            self.state,
            status=ActionExecutionStatus.EXECUTING,
            command_sequence=permit.command_sequence,
            source_capture_id=source_capture_id,
            started_at=datetime.now(UTC),
            row_version=self.state.row_version + 1,
        )
        return self.state

    async def recover(
        self,
        *,
        action_execution_id,
        permit,
        expected_row_version,
    ):
        assert self.state is not None
        assert action_execution_id == self.state.action_execution_id
        assert expected_row_version == self.state.row_version
        self.events.append("action:recover")
        self.recover_calls.append(
            {
                "action_execution_id": action_execution_id,
                "permit": permit,
            }
        )
        if (
            self.state.status is ActionExecutionStatus.FAILED
            and self.state.source_capture_id is None
            and self.state.started_at is None
            and self.state.error_code == "before_capture_failed"
        ):
            self.state = replace(
                self.state,
                status=ActionExecutionStatus.PLANNED,
                command_sequence=permit.command_sequence,
                result_payload={"before_capture_retry_count": 1},
                error_code=None,
                error_message=None,
                action_finished_at=None,
                row_version=self.state.row_version + 1,
            )
        return self.state

    async def record_interaction(
        self,
        *,
        interaction,
        permit,
        expected_status,
        expected_row_version,
    ):
        del permit
        assert expected_status is ActionExecutionStatus.EXECUTING
        assert expected_row_version == self.state.row_version
        status_map = {
            InteractionResultStatus.EVIDENCE_PENDING: (ActionExecutionStatus.EVIDENCE_PENDING),
            InteractionResultStatus.FAILED: ActionExecutionStatus.FAILED,
            InteractionResultStatus.BLOCKED: ActionExecutionStatus.BLOCKED,
            InteractionResultStatus.TIMED_OUT: ActionExecutionStatus.TIMED_OUT,
        }
        target_status = status_map[interaction.status]
        self.events.append(f"action:{target_status.value}")
        self.state = replace(
            self.state,
            status=target_status,
            result_payload=interaction.to_dict(),
            error_code=interaction.error_code,
            error_message=interaction.error_message,
            action_finished_at=datetime.now(UTC),
            row_version=self.state.row_version + 1,
        )
        return self.state

    async def complete_evidence(self, *, interaction, permit, expected_row_version):
        del permit
        assert expected_row_version == self.state.row_version
        self.events.append("action:completed")
        self.state = replace(
            self.state,
            status=ActionExecutionStatus.COMPLETED,
            result_capture_id=interaction.result_capture_id,
            result_payload=interaction.to_dict(),
            evidence_finished_at=datetime.now(UTC),
            row_version=self.state.row_version + 1,
        )
        return self.state

    async def attach_terminal_evidence(
        self,
        *,
        interaction,
        permit,
        expected_row_version,
    ):
        del permit
        assert expected_row_version == self.state.row_version
        self.events.append("action:terminal_evidence")
        self.state = replace(
            self.state,
            result_capture_id=interaction.result_capture_id,
            result_payload=interaction.to_dict(),
            evidence_finished_at=datetime.now(UTC),
            row_version=self.state.row_version + 1,
        )
        return self.state


class PlannedIdentityInteractionExecutor:
    def __init__(
        self,
        events,
        *,
        status: InteractionResultStatus = InteractionResultStatus.EVIDENCE_PENDING,
    ) -> None:
        self.events = events
        self.status = status

    async def execute(self, request):
        self.events.append("execute")
        return InteractionResult(
            task_id=request.intent.task_id,
            session_id=request.intent.session_id,
            intent_id=request.intent.intent_id,
            action_execution_id=request.action_execution_id,
            source_capture_id=request.source_capture_id,
            status=self.status,
            error_code=("driver_failed" if self.status is InteractionResultStatus.FAILED else None),
        )


class RecordingRecoveryCaptureReader:
    def __init__(
        self,
        *,
        action_capture=None,
        captures=None,
        action_error: Exception | None = None,
        capture_error: Exception | None = None,
    ) -> None:
        self.action_capture = action_capture
        self.captures = dict(captures or {})
        self.action_error = action_error
        self.capture_error = capture_error
        self.action_calls = []
        self.capture_calls = []

    async def get_verified_action_capture(self, *, action_execution_id, role):
        self.action_calls.append((action_execution_id, role))
        if self.action_error is not None:
            raise self.action_error
        return self.action_capture

    async def get_verified_capture(self, *, capture_id):
        self.capture_calls.append(capture_id)
        if self.capture_error is not None:
            raise self.capture_error
        return self.captures.get(capture_id)


class RecordingActionEvidenceWriter:
    def __init__(self) -> None:
        self.attachments = []

    async def attach_action_log(self, **kwargs):
        self.attachments.append(kwargs)


def _app_recovery_command() -> CaptureCoordinationCommand:
    command = _command(surface_kind=SurfaceKind.ANDROID)
    profile = replace(
        command.intent.capture_profile,
        before=replace(command.intent.capture_profile.before, role="before_action"),
        after=replace(command.intent.capture_profile.after, role="after_action"),
    )
    return replace(command, intent=replace(command.intent, capture_profile=profile))


def _h5_recovery_command() -> CaptureCoordinationCommand:
    command = _command(surface_kind=SurfaceKind.WEB)
    return replace(
        command,
        intent=replace(
            command.intent,
            session_id="h5:run-1",
            intent_id="h5-platform-fallback:intent-1",
            idempotency_key="task-1:h5:run-1:intent-1",
            capture_profile=replace(
                command.intent.capture_profile,
                before=replace(command.intent.capture_profile.before, role="before_action"),
                after=replace(command.intent.capture_profile.after, role="after_action"),
            ),
        ),
    )


def _h5_bootstrap_command() -> CaptureCoordinationCommand:
    command = _h5_recovery_command()
    return replace(
        command,
        intent=replace(
            command.intent,
            intent_id="h5-bootstrap:run-1",
            idempotency_key="h5-bootstrap:run-1",
            source_capture_id="h5-entry:run-1",
            action_type=InteractionOperation.NAVIGATE,
            parameters={
                "initial_surface_action": True,
                "destination_url": "https://example.com",
            },
        ),
    )


def _recovery_capture(
    intent: ActionIntent,
    *,
    capture_id: str,
    role: str,
    parent_capture_id: str | None,
    action_execution_id: str | None = None,
) -> CaptureRef:
    return CaptureRef(
        task_id=intent.task_id,
        session_id=intent.session_id,
        capture_id=capture_id,
        surface_kind=intent.surface_kind,
        capture_kind=(
            CaptureKind.PAGE if intent.surface_kind is SurfaceKind.WEB else CaptureKind.APP
        ),
        role=role,
        artifact_ids=(f"artifact-{capture_id}",),
        parent_capture_id=parent_capture_id,
        action_execution_id=action_execution_id,
    )


def _recovery_snapshot(
    intent: ActionIntent,
    *,
    status: ActionExecutionStatus,
    before_capture_id: str | None = "capture-before",
    after_capture_id: str | None = "capture-after",
    result_payload=None,
    command_sequence: int = 7,
    row_version: int = 3,
) -> ActionExecutionSnapshot:
    if status is ActionExecutionStatus.PLANNED:
        before_capture_id = None
        after_capture_id = None
    elif status in {
        ActionExecutionStatus.EXECUTING,
        ActionExecutionStatus.EVIDENCE_PENDING,
        ActionExecutionStatus.EVIDENCE_FAILED,
    }:
        after_capture_id = None
    action_execution_id = build_action_execution_id(intent)
    now = datetime(2026, 8, 13, 12, 0, tzinfo=UTC)
    if result_payload is None and before_capture_id and after_capture_id:
        result_status = {
            ActionExecutionStatus.COMPLETED: InteractionResultStatus.SUCCEEDED,
            ActionExecutionStatus.FAILED: InteractionResultStatus.FAILED,
            ActionExecutionStatus.BLOCKED: InteractionResultStatus.BLOCKED,
            ActionExecutionStatus.TIMED_OUT: InteractionResultStatus.TIMED_OUT,
            ActionExecutionStatus.EVIDENCE_PENDING: InteractionResultStatus.EVIDENCE_PENDING,
            ActionExecutionStatus.EVIDENCE_FAILED: InteractionResultStatus.EVIDENCE_FAILED,
        }.get(status)
        if result_status is not None:
            result_payload = _recovery_interaction(
                intent,
                status=result_status,
                before_capture_id=before_capture_id,
                after_capture_id=after_capture_id,
            ).to_dict()
    return ActionExecutionSnapshot(
        action_execution_id=action_execution_id,
        task_id=intent.task_id,
        session_id=intent.session_id,
        intent_id=intent.intent_id,
        intent_hash=build_action_intent_hash(intent),
        command_sequence=command_sequence,
        finalization_epoch=0,
        requested_source_capture_id=intent.source_capture_id,
        status=status,
        row_version=row_version,
        source_capture_id=before_capture_id,
        result_capture_id=after_capture_id,
        result_payload=result_payload or {},
        error_code=(
            None
            if status is ActionExecutionStatus.COMPLETED
            else "recovered_action_failed"
            if status
            in {
                ActionExecutionStatus.FAILED,
                ActionExecutionStatus.BLOCKED,
                ActionExecutionStatus.TIMED_OUT,
                ActionExecutionStatus.EVIDENCE_FAILED,
            }
            else None
        ),
        error_message=(
            None
            if status is ActionExecutionStatus.COMPLETED
            else "recovered action terminal fact"
            if status
            in {
                ActionExecutionStatus.FAILED,
                ActionExecutionStatus.BLOCKED,
                ActionExecutionStatus.TIMED_OUT,
                ActionExecutionStatus.EVIDENCE_FAILED,
            }
            else None
        ),
        created_at=now,
        started_at=(now if status is not ActionExecutionStatus.PLANNED else None),
        action_finished_at=(
            now
            if status
            in {
                ActionExecutionStatus.COMPLETED,
                ActionExecutionStatus.EVIDENCE_PENDING,
                ActionExecutionStatus.FAILED,
                ActionExecutionStatus.BLOCKED,
                ActionExecutionStatus.TIMED_OUT,
                ActionExecutionStatus.EVIDENCE_FAILED,
            }
            else None
        ),
        evidence_finished_at=(
            now
            if status
            in {
                ActionExecutionStatus.COMPLETED,
                ActionExecutionStatus.FAILED,
                ActionExecutionStatus.BLOCKED,
                ActionExecutionStatus.TIMED_OUT,
                ActionExecutionStatus.EVIDENCE_FAILED,
            }
            else None
        ),
    )


def _recovery_interaction(
    intent: ActionIntent,
    *,
    status: InteractionResultStatus,
    before_capture_id: str,
    after_capture_id: str,
) -> InteractionResult:
    return InteractionResult(
        task_id=intent.task_id,
        session_id=intent.session_id,
        intent_id=intent.intent_id,
        action_execution_id=build_action_execution_id(intent),
        source_capture_id=before_capture_id,
        status=status,
        result_capture_id=(
            None if status is InteractionResultStatus.EVIDENCE_PENDING else after_capture_id
        ),
        error_code=(
            None
            if status
            in {
                InteractionResultStatus.SUCCEEDED,
                InteractionResultStatus.EVIDENCE_PENDING,
            }
            else "recovered_action_failed"
        ),
        error_message=(
            None
            if status
            in {
                InteractionResultStatus.SUCCEEDED,
                InteractionResultStatus.EVIDENCE_PENDING,
            }
            else "recovered action terminal fact"
        ),
    )


async def test_coordinator_persists_action_state_around_capture_commits() -> None:
    events = []
    actions = RecordingActionExecutionRepository(events)
    writer = RecordingCaptureWriter(events)
    coordinator = CaptureCoordinator(
        capture_writer=writer,
        interaction_executor=PlannedIdentityInteractionExecutor(events),
        evidence_exit_gate=RecordingEvidenceExitGate(),
        action_execution_repository=actions,
    )

    result = await coordinator.coordinate(_command())

    assert events == [
        "action:planned",
        "capture:before_action",
        "action:executing",
        "execute",
        "action:evidence_pending",
        "capture:after_action",
        "action:completed",
    ]
    assert writer.requests[0].action_execution_id == result.interaction.action_execution_id
    assert writer.requests[0].session_command_sequence == 1
    assert writer.requests[1].session_command_sequence == 1
    assert actions.state.status is ActionExecutionStatus.COMPLETED


async def test_failed_app_action_ledger_stays_failed_after_terminal_evidence() -> None:
    events = []
    actions = RecordingActionExecutionRepository(events)
    coordinator = CaptureCoordinator(
        capture_writer=RecordingCaptureWriter(events),
        interaction_executor=PlannedIdentityInteractionExecutor(
            events,
            status=InteractionResultStatus.FAILED,
        ),
        evidence_exit_gate=RecordingEvidenceExitGate(),
        action_execution_repository=actions,
    )

    result = await coordinator.coordinate(_command(surface_kind=SurfaceKind.ANDROID))

    assert events == [
        "action:planned",
        "capture:before_action",
        "action:executing",
        "execute",
        "action:failed",
        "capture:after_action",
        "action:terminal_evidence",
    ]
    assert result.interaction.status is InteractionResultStatus.FAILED
    assert result.interaction.result_capture_id == "capture-after"
    assert actions.state.status is ActionExecutionStatus.FAILED
    assert actions.state.result_capture_id == "capture-after"


async def test_failed_app_action_stays_failed_when_after_capture_also_fails() -> None:
    events = []
    actions = RecordingActionExecutionRepository(events)
    coordinator = CaptureCoordinator(
        capture_writer=RecordingCaptureWriter(
            events,
            fail_phase=CapturePhase.AFTER_ACTION,
        ),
        interaction_executor=PlannedIdentityInteractionExecutor(
            events,
            status=InteractionResultStatus.FAILED,
        ),
        evidence_exit_gate=RecordingEvidenceExitGate(),
        action_execution_repository=actions,
    )

    with pytest.raises(CaptureCoordinationError) as error:
        await coordinator.coordinate(_command(surface_kind=SurfaceKind.ANDROID))

    assert error.value.stage is CaptureCoordinationStage.AFTER_CAPTURE
    assert error.value.error_code == "APP_ACTION_AFTER_CAPTURE_MISSING"
    assert error.value.interaction is not None
    assert error.value.interaction.status is InteractionResultStatus.FAILED
    assert error.value.interaction.execution_outcome is InteractionExecutionOutcome.FAILED
    assert error.value.interaction.verification_outcome is InteractionVerificationOutcome.UNVERIFIED
    assert actions.state.status is ActionExecutionStatus.FAILED
    assert actions.state.result_capture_id is None


async def test_app_after_capture_retries_once_on_transient_failure() -> None:
    events = []
    actions = RecordingActionExecutionRepository(events)
    coordinator = CaptureCoordinator(
        capture_writer=RecordingCaptureWriter(
            events,
            fail_phase=CapturePhase.AFTER_ACTION,
            transient_fail_times=1,
            transient_fail_message="adb screencap 502 aged channel",
        ),
        interaction_executor=PlannedIdentityInteractionExecutor(events),
        evidence_exit_gate=RecordingEvidenceExitGate(),
        action_execution_repository=actions,
    )

    result = await coordinator.coordinate(_command(surface_kind=SurfaceKind.ANDROID))

    after_events = [item for item in events if item == "capture:after_action"]
    assert len(after_events) == 2
    assert result.after_capture is not None
    assert result.after_capture.capture_id == "capture-after"


async def test_web_after_capture_retries_once_without_reexecuting_action() -> None:
    """H5 动作后 Capture 瞬时超时应重采证据，不能重放已经成功的页面动作。"""
    events = []
    actions = RecordingActionExecutionRepository(events)
    coordinator = CaptureCoordinator(
        capture_writer=RecordingCaptureWriter(
            events,
            fail_phase=CapturePhase.AFTER_ACTION,
            transient_fail_times=1,
            transient_fail_message="page capture timed out",
        ),
        interaction_executor=PlannedIdentityInteractionExecutor(events),
        evidence_exit_gate=RecordingEvidenceExitGate(),
        action_execution_repository=actions,
    )

    result = await coordinator.coordinate(_command(surface_kind=SurfaceKind.WEB))

    assert events.count("execute") == 1
    assert events.count("capture:after_action") == 2
    assert result.after_capture.capture_id == "capture-after"


async def test_app_executor_exception_terminalizes_unknown_action_outcome() -> None:
    events = []
    actions = RecordingActionExecutionRepository(events)
    coordinator = CaptureCoordinator(
        capture_writer=RecordingCaptureWriter(events),
        interaction_executor=FailingInteractionExecutor(),
        evidence_exit_gate=RecordingEvidenceExitGate(),
        action_execution_repository=actions,
    )

    result = await coordinator.coordinate(_command(surface_kind=SurfaceKind.ANDROID))

    assert result.interaction.status is InteractionResultStatus.FAILED
    assert result.interaction.error_code == "APP_ACTION_OUTCOME_UNKNOWN_DURING_EXECUTION"
    assert result.interaction.state_summary["cause_type"] == "TimeoutError"
    assert result.after_capture is not None
    assert actions.state.status is ActionExecutionStatus.FAILED
    assert actions.state.error_code == "APP_ACTION_OUTCOME_UNKNOWN_DURING_EXECUTION"
    assert actions.state.result_capture_id == result.after_capture.capture_id


async def test_web_path_never_reads_app_recovery_or_calls_recover() -> None:
    events = []
    actions = RecordingActionExecutionRepository(events)
    reader = RecordingRecoveryCaptureReader(
        action_error=AssertionError("Web 不得读取 App 恢复 Capture")
    )
    exit_gate = RecordingEvidenceExitGate()
    coordinator = CaptureCoordinator(
        capture_writer=RecordingCaptureWriter(events),
        interaction_executor=RecordingInteractionExecutor(events),
        evidence_exit_gate=exit_gate,
        action_execution_repository=actions,
        action_recovery_captures=reader,
    )

    result = await coordinator.coordinate(_command())

    assert result.interaction.status is InteractionResultStatus.SUCCEEDED
    assert actions.get_calls == []
    assert actions.recover_calls == []
    assert reader.action_calls == []
    assert reader.capture_calls == []
    assert len(exit_gate.reservations) == 1
    assert exit_gate.recovery_reservations == []
    assert exit_gate.finalizations[0][1] is EvidenceExitReservationStatus.CONSUMED


async def test_h5_terminal_action_replays_without_second_browser_click() -> None:
    command = _h5_recovery_command()
    intent = command.intent
    action_execution_id = build_action_execution_id(intent)
    before = _recovery_capture(
        intent,
        capture_id="h5-replay-before",
        role="before_action",
        parent_capture_id=intent.source_capture_id,
        action_execution_id=action_execution_id,
    )
    after = _recovery_capture(
        intent,
        capture_id="h5-replay-after",
        role="after_action",
        parent_capture_id=before.capture_id,
        action_execution_id=action_execution_id,
    )
    actions = RecordingActionExecutionRepository(events := [])
    actions.state = _recovery_snapshot(
        intent,
        status=ActionExecutionStatus.COMPLETED,
        before_capture_id=before.capture_id,
        after_capture_id=after.capture_id,
    )
    reader = RecordingRecoveryCaptureReader(
        captures={
            before.capture_id: type("Persisted", (), {"capture": before})(),
            after.capture_id: type("Persisted", (), {"capture": after})(),
        }
    )
    writer = RecordingCaptureWriter(events)
    executor = RecordingInteractionExecutor(events)
    coordinator = CaptureCoordinator(
        capture_writer=writer,
        interaction_executor=executor,
        evidence_exit_gate=RecordingEvidenceExitGate(),
        action_execution_repository=actions,
        action_recovery_captures=reader,
    )

    result = await coordinator.coordinate(command)

    assert events == ["action:recover"]
    assert actions.get_calls == [action_execution_id]
    assert len(actions.recover_calls) == 1
    assert writer.requests == []
    assert executor.requests == []
    assert result.before_capture == before
    assert result.after_capture == after


async def test_h5_fresh_bootstrap_accepts_normalized_null_parent() -> None:
    command = _h5_bootstrap_command()
    events = []
    actions = RecordingActionExecutionRepository(events)
    coordinator = CaptureCoordinator(
        capture_writer=RecordingCaptureWriter(events),
        interaction_executor=RecordingInteractionExecutor(events),
        evidence_exit_gate=RecordingEvidenceExitGate(),
        action_execution_repository=actions,
        action_recovery_captures=RecordingRecoveryCaptureReader(),
    )

    result = await coordinator.coordinate(command)

    assert result.interaction.status is InteractionResultStatus.SUCCEEDED
    assert actions.state.requested_source_capture_id is None
    assert result.before_capture.parent_capture_id is None


async def test_planned_app_recovery_reuses_verified_before_and_executes_driver_once() -> None:
    command = _app_recovery_command()
    intent = command.intent
    action_execution_id = build_action_execution_id(intent)
    before = _recovery_capture(
        intent,
        capture_id="historical-before",
        role="before_action",
        parent_capture_id=intent.source_capture_id,
        action_execution_id=action_execution_id,
    )
    actions = RecordingActionExecutionRepository(events := [])
    actions.state = _recovery_snapshot(
        intent,
        status=ActionExecutionStatus.PLANNED,
        before_capture_id=None,
        after_capture_id=None,
    )
    reader = RecordingRecoveryCaptureReader(
        action_capture=type("Persisted", (), {"capture": before})()
    )
    exit_gate = RecordingEvidenceExitGate()
    executor = RecordingInteractionExecutor(events)
    coordinator = CaptureCoordinator(
        capture_writer=(writer := RecordingCaptureWriter(events)),
        interaction_executor=executor,
        evidence_exit_gate=exit_gate,
        action_execution_repository=actions,
        action_recovery_captures=reader,
    )

    result = await coordinator.coordinate(command)

    assert reader.action_calls == [(action_execution_id, "before_action")]
    assert exit_gate.recovery_reservations[0]["action_execution_id"] == action_execution_id
    assert exit_gate.recovery_reservations[0]["before_capture_id"] == "historical-before"
    assert exit_gate.stale_finalizations[0]["status"] is (EvidenceExitReservationStatus.CANCELLED)
    assert actions.resume_calls[0]["source_capture_id"] == "historical-before"
    assert actions.recover_calls == []
    assert len(executor.requests) == 1
    assert len(writer.requests) == 1
    assert writer.requests[0].phase is CapturePhase.AFTER_ACTION
    assert result.before_capture.capture_id == "historical-before"
    assert result.after_capture.capture_id == "capture-after"
    assert result.interaction.status is InteractionResultStatus.SUCCEEDED
    assert exit_gate.finalizations[0][1] is EvidenceExitReservationStatus.CONSUMED


async def test_failed_before_capture_retry_reenters_exit_gate_and_executes_driver_once() -> None:
    command = _app_recovery_command()
    actions = RecordingActionExecutionRepository(events := [])
    actions.state = replace(
        _recovery_snapshot(
            command.intent,
            status=ActionExecutionStatus.PLANNED,
            before_capture_id=None,
            after_capture_id=None,
        ),
        status=ActionExecutionStatus.FAILED,
        error_code="before_capture_failed",
        error_message="动作前 Capture 未完成",
        action_finished_at=datetime(2026, 8, 13, 12, 1, tzinfo=UTC),
    )
    exit_gate = RecordingEvidenceExitGate()
    executor = RecordingInteractionExecutor(events)
    coordinator = CaptureCoordinator(
        capture_writer=(writer := RecordingCaptureWriter(events)),
        interaction_executor=executor,
        evidence_exit_gate=exit_gate,
        action_execution_repository=actions,
        action_recovery_captures=RecordingRecoveryCaptureReader(),
    )

    result = await coordinator.coordinate(command)

    assert exit_gate.recovery_reservations[0]["before_capture_id"] is None
    assert exit_gate.stale_finalizations[0]["status"] is (EvidenceExitReservationStatus.CANCELLED)
    assert actions.recover_calls
    assert len(executor.requests) == 1
    assert [request.phase for request in writer.requests] == [
        CapturePhase.BEFORE_ACTION,
        CapturePhase.AFTER_ACTION,
    ]
    assert result.interaction.status is InteractionResultStatus.SUCCEEDED
    assert exit_gate.finalizations[0][1] is EvidenceExitReservationStatus.CONSUMED


async def test_malformed_historical_before_blocks_before_resume_or_driver() -> None:
    command = _app_recovery_command()
    intent = command.intent
    actions = RecordingActionExecutionRepository(events := [])
    actions.state = _recovery_snapshot(
        intent,
        status=ActionExecutionStatus.PLANNED,
        before_capture_id=None,
        after_capture_id=None,
    )
    malformed = _recovery_capture(
        intent,
        capture_id="historical-before",
        role="before_action",
        parent_capture_id="wrong-parent",
        action_execution_id=actions.state.action_execution_id,
    )
    reader = RecordingRecoveryCaptureReader(
        action_capture=type("Persisted", (), {"capture": malformed})()
    )
    exit_gate = RecordingEvidenceExitGate()
    writer = RecordingCaptureWriter(events)
    executor = RecordingInteractionExecutor(events)
    coordinator = CaptureCoordinator(
        capture_writer=writer,
        interaction_executor=executor,
        evidence_exit_gate=exit_gate,
        action_execution_repository=actions,
        action_recovery_captures=reader,
    )

    with pytest.raises(AppActionRecoveryBlockedError) as caught:
        await coordinator.coordinate(command)

    assert caught.value.error_code == "APP_ACTION_RECOVERY_CAPTURE_INVALID"
    assert actions.resume_calls == []
    assert actions.recover_calls == []
    assert writer.requests == []
    assert executor.requests == []
    assert exit_gate.recovery_reservations == []


@pytest.mark.parametrize(
    "status",
    [
        ActionExecutionStatus.COMPLETED,
        ActionExecutionStatus.FAILED,
        ActionExecutionStatus.BLOCKED,
        ActionExecutionStatus.TIMED_OUT,
    ],
)
async def test_app_terminal_replay_never_calls_driver_and_attaches_action_log(status) -> None:
    command = _app_recovery_command()
    intent = command.intent
    action_execution_id = build_action_execution_id(intent)
    before = _recovery_capture(
        intent,
        capture_id="replay-before",
        role="before_action",
        parent_capture_id=intent.source_capture_id,
        action_execution_id=action_execution_id,
    )
    after = _recovery_capture(
        intent,
        capture_id="replay-after",
        role="after_action",
        parent_capture_id=before.capture_id,
        action_execution_id=action_execution_id,
    )
    actions = RecordingActionExecutionRepository(events := [])
    actions.state = _recovery_snapshot(
        intent,
        status=status,
        before_capture_id=before.capture_id,
        after_capture_id=after.capture_id,
    )
    reader = RecordingRecoveryCaptureReader(
        captures={
            before.capture_id: type("Persisted", (), {"capture": before})(),
            after.capture_id: type("Persisted", (), {"capture": after})(),
        }
    )
    evidence_writer = RecordingActionEvidenceWriter()
    writer = RecordingCaptureWriter(events)
    executor = RecordingInteractionExecutor(events)
    coordinator = CaptureCoordinator(
        capture_writer=writer,
        interaction_executor=executor,
        evidence_exit_gate=RecordingEvidenceExitGate(),
        action_execution_repository=actions,
        action_recovery_captures=reader,
        action_evidence_writer=evidence_writer,
    )

    result = await coordinator.coordinate(command)

    assert events == ["action:recover"]
    assert executor.requests == []
    assert writer.requests == []
    assert reader.capture_calls == [before.capture_id, after.capture_id]
    assert result.before_capture.capture_id == before.capture_id
    assert result.after_capture.capture_id == after.capture_id
    assert (
        result.interaction.status.value
        == {
            ActionExecutionStatus.COMPLETED: "succeeded",
            ActionExecutionStatus.FAILED: "failed",
            ActionExecutionStatus.BLOCKED: "blocked",
            ActionExecutionStatus.TIMED_OUT: "timed_out",
        }[status]
    )
    assert coordinator._evidence_exit_gate.stale_finalizations[0]["status"] is (
        EvidenceExitReservationStatus.CONSUMED
    )
    assert len(evidence_writer.attachments) == 1
    attachment = evidence_writer.attachments[0]
    assert attachment["before_capture_id"] == before.capture_id
    assert attachment["after_capture_id"] == after.capture_id
    assert attachment["entry"]["action_execution_id"] == action_execution_id


@pytest.mark.parametrize(
    "status",
    [
        ActionExecutionStatus.EXECUTING,
        ActionExecutionStatus.EVIDENCE_PENDING,
        ActionExecutionStatus.EVIDENCE_FAILED,
    ],
)
async def test_app_nonterminal_recovery_facts_block_without_driver(status) -> None:
    command = _app_recovery_command()
    intent = command.intent
    actions = RecordingActionExecutionRepository(events := [])
    actions.state = _recovery_snapshot(intent, status=status)
    reader = RecordingRecoveryCaptureReader()
    writer = RecordingCaptureWriter(events)
    executor = RecordingInteractionExecutor(events)
    coordinator = CaptureCoordinator(
        capture_writer=writer,
        interaction_executor=executor,
        evidence_exit_gate=RecordingEvidenceExitGate(),
        action_execution_repository=actions,
        action_recovery_captures=reader,
    )

    with pytest.raises(AppActionRecoveryBlockedError) as caught:
        await coordinator.coordinate(command)

    assert caught.value.error_code == "APP_ACTION_RECOVERY_BLOCKED"
    assert actions.recover_calls
    assert writer.requests == []
    assert executor.requests == []
    assert reader.capture_calls == []


@pytest.mark.parametrize(
    ("status", "error_code"),
    [
        (
            ActionExecutionStatus.FAILED,
            "APP_ACTION_OUTCOME_UNKNOWN_AFTER_CRASH",
        ),
        (
            ActionExecutionStatus.EVIDENCE_FAILED,
            "APP_AFTER_CAPTURE_MISSING_AFTER_CRASH",
        ),
    ],
)
async def test_app_recovery_preserves_structured_crash_reason_without_driver(
    status,
    error_code,
) -> None:
    command = _app_recovery_command()
    actions = RecordingActionExecutionRepository(events := [])
    actions.state = replace(
        _recovery_snapshot(
            command.intent,
            status=status,
            after_capture_id=None,
        ),
        error_code=error_code,
        error_message="恢复事实不完整",
    )
    writer = RecordingCaptureWriter(events)
    executor = RecordingInteractionExecutor(events)
    coordinator = CaptureCoordinator(
        capture_writer=writer,
        interaction_executor=executor,
        evidence_exit_gate=RecordingEvidenceExitGate(),
        action_execution_repository=actions,
        action_recovery_captures=RecordingRecoveryCaptureReader(),
    )

    with pytest.raises(AppActionRecoveryBlockedError) as caught:
        await coordinator.coordinate(command)

    assert caught.value.error_code == error_code
    assert writer.requests == []
    assert executor.requests == []


async def test_verified_before_reader_failure_maps_to_structured_recovery_error() -> None:
    command = _app_recovery_command()
    intent = command.intent
    actions = RecordingActionExecutionRepository(events := [])
    actions.state = _recovery_snapshot(
        intent,
        status=ActionExecutionStatus.PLANNED,
        before_capture_id=None,
        after_capture_id=None,
    )
    reader = RecordingRecoveryCaptureReader(
        action_error=AppCaptureRecoveryError(
            "对象校验失败",
            error_code="APP_ACTION_RECOVERY_CAPTURE_CHECKSUM_MISMATCH",
        )
    )
    coordinator = CaptureCoordinator(
        capture_writer=RecordingCaptureWriter(events),
        interaction_executor=RecordingInteractionExecutor(events),
        evidence_exit_gate=RecordingEvidenceExitGate(),
        action_execution_repository=actions,
        action_recovery_captures=reader,
    )

    with pytest.raises(AppActionRecoveryBlockedError) as caught:
        await coordinator.coordinate(command)

    assert caught.value.error_code == "APP_ACTION_RECOVERY_CAPTURE_CHECKSUM_MISMATCH"
    assert actions.resume_calls == []
    assert actions.recover_calls == []


async def test_verified_replay_reader_failure_maps_to_structured_recovery_error() -> None:
    command = _app_recovery_command()
    intent = command.intent
    actions = RecordingActionExecutionRepository(events := [])
    actions.state = _recovery_snapshot(
        intent,
        status=ActionExecutionStatus.COMPLETED,
    )
    reader = RecordingRecoveryCaptureReader(
        capture_error=AppCaptureRecoveryError(
            "对象不可读取",
            error_code="APP_ACTION_RECOVERY_CAPTURE_UNREADABLE",
        )
    )
    executor = RecordingInteractionExecutor(events)
    coordinator = CaptureCoordinator(
        capture_writer=RecordingCaptureWriter(events),
        interaction_executor=executor,
        evidence_exit_gate=RecordingEvidenceExitGate(),
        action_execution_repository=actions,
        action_recovery_captures=reader,
    )

    with pytest.raises(AppActionRecoveryBlockedError) as caught:
        await coordinator.coordinate(command)

    assert caught.value.error_code == "APP_ACTION_RECOVERY_CAPTURE_UNREADABLE"
    assert executor.requests == []
    assert reader.capture_calls == ["capture-before"]
