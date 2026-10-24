from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from services.api.app.application.app_evidence_recovery_runtime import (
    AppEvidenceRecoveryRuntime,
    AppEvidenceRecoveryRuntimeError,
    AppRecoveryCandidateBinding,
    AppRecoveryVisualFacts,
)
from services.api.app.application.capture_coordination import (
    CaptureCoordinationResult,
)
from services.api.app.application.evidence_recovery_replay import (
    RecoveryGraphStep,
    RecoveryObservation,
)
from services.api.app.domain.exploration import (
    AppVisualAnalysis,
    AppVisualCandidate,
    NormalizedBounds,
)
from services.api.app.domain.inspection import (
    EvidenceReplayPolicy,
    EvidenceRequiredEquivalence,
    EvidenceSessionDependency,
    EvidenceSideEffectPolicy,
    EvidenceSourceGrade,
    RestorationStrategy,
    build_evidence_request,
    build_restoration_plan,
)
from services.api.app.domain.interaction import (
    CaptureKind,
    CaptureRef,
    InteractionExecutionOutcome,
    InteractionOperation,
    InteractionResult,
    InteractionResultStatus,
    InteractionRiskLevel,
    InteractionVerificationOutcome,
    SurfaceKind,
    SurfaceSessionStatus,
)

NOW = datetime(2026, 9, 8, 16, 0, tzinfo=UTC)


def _plan():
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
    return build_restoration_plan(
        request=request,
        source_session_id="session-1",
        target_session_id="session-1",
        surface_kind=SurfaceKind.ANDROID,
        strategy=RestorationStrategy.SAME_APP_SESSION,
        replay_execution_plan_id="page-graph-route:route-1",
        normalization_step_ids=("edge-1",),
        deadline=NOW + timedelta(minutes=5),
    )


def _step(
    *,
    action_kind: str = "click",
    semantic_target: str | None = "内容详情",
) -> RecoveryGraphStep:
    return RecoveryGraphStep(
        transition_id="edge-1",
        source_state_id="historical-home",
        target_state_id="historical-detail",
        action_kind=action_kind,
        semantic_target=semantic_target,
        source_identity="home",
        source_state_fingerprint="fp-home",
        target_identity="detail",
        target_state_fingerprint="fp-detail",
    )


def _capture(
    capture_id: str,
    *,
    role: str,
    parent_capture_id: str | None,
    action_execution_id: str | None = None,
) -> CaptureRef:
    return CaptureRef(
        task_id="task-1",
        session_id="session-1",
        capture_id=capture_id,
        surface_kind=SurfaceKind.ANDROID,
        capture_kind=CaptureKind.APP,
        role=role,
        artifact_ids=(f"artifact-{capture_id}",),
        parent_capture_id=parent_capture_id,
        action_execution_id=action_execution_id,
    )


def _observation(capture_id: str, identity: str, fingerprint: str) -> RecoveryObservation:
    return RecoveryObservation(
        capture_id=capture_id,
        page_identity=identity,
        state_fingerprint=fingerprint,
        observed_at=NOW,
    )


class Session:
    task_id = "task-1"
    session_id = "session-1"
    surface_kind = SurfaceKind.ANDROID
    status = SurfaceSessionStatus.ACTIVE
    finalization_epoch = 3
    current_capture_id = "session-current"


class Sessions:
    async def get(self, *, session_id: str):
        assert session_id == "session-1"
        return Session()


class Facts:
    def __init__(self) -> None:
        self.reads: list[str] = []
        self.binds = []

    async def read(self, *, capture, agent_run_id, finalization_epoch):
        assert agent_run_id == "run-1"
        assert finalization_epoch == 3
        self.reads.append(capture.capture_id)
        if capture.capture_id == "fresh-source":
            return _observation("fresh-source", "home", "fp-home")
        return _observation("fresh-after", "detail", "fp-detail")

    async def bind(self, *, source, step):
        self.binds.append((source, step))
        return AppRecoveryCandidateBinding(
            candidate_id="candidate-fresh-source",
            purpose=(
                "从当前首页打开内容详情"
                if step.action_kind == InteractionOperation.CLICK.value
                else "在当前页面安全下滑并重新采集"
            ),
            action_type=InteractionOperation(step.action_kind),
            semantic_target=step.semantic_target,
        )


class Coordinator:
    def __init__(self) -> None:
        self.observation_intents = []
        self.commands = []

    async def capture_observation(self, *, agent_run_id, intent, **kwargs):
        assert agent_run_id == "run-1"
        assert kwargs == {"semantic_role": "surface_reobserve"}
        self.observation_intents.append(intent)
        return _capture(
            "fresh-source",
            role="observation",
            parent_capture_id="session-current",
        )

    async def coordinate(self, command):
        self.commands.append(command)
        intent = command.intent
        before = _capture(
            "before-action",
            role="before_action",
            parent_capture_id=intent.source_capture_id,
            action_execution_id="action-1",
        )
        after = _capture(
            "fresh-after",
            role="after_action",
            parent_capture_id=before.capture_id,
            action_execution_id="action-1",
        )
        interaction = InteractionResult(
            task_id="task-1",
            session_id="session-1",
            intent_id=intent.intent_id,
            action_execution_id="action-1",
            source_capture_id=before.capture_id,
            status=InteractionResultStatus.SUCCEEDED,
            execution_outcome=InteractionExecutionOutcome.SUCCEEDED,
            verification_outcome=InteractionVerificationOutcome.EFFECT_CONFIRMED,
            result_capture_id=after.capture_id,
        )
        return CaptureCoordinationResult(
            before_capture=before,
            interaction=interaction,
            after_capture=after,
        )


@pytest.mark.asyncio
async def test_runtime_reobserves_then_executes_through_capture_coordinator() -> None:
    coordinator = Coordinator()
    facts = Facts()
    runtime = AppEvidenceRecoveryRuntime(
        surface_sessions=Sessions(),
        capture_coordinator=coordinator,
        facts=facts,
        now=lambda: NOW,
    )
    plan = _plan()

    source = await runtime.observe(
        task_id="task-1",
        session_id="session-1",
        plan=plan,
        agent_run_id="run-1",
        finalization_epoch=3,
    )
    receipt = await runtime.execute(
        task_id="task-1",
        session_id="session-1",
        plan=plan,
        agent_run_id="run-1",
        finalization_epoch=3,
        step=_step(),
        source=source,
    )

    assert receipt.action_execution_id == "action-1"
    assert receipt.after.capture_id == "fresh-after"
    assert facts.reads == ["fresh-source", "fresh-after"]
    assert len(coordinator.commands) == 1
    intent = coordinator.commands[0].intent
    assert intent.source_capture_id == "fresh-source"
    assert intent.parameters["candidate_id"] == "candidate-fresh-source"
    assert intent.parameters["evidence_request_id"] == plan.request_id
    assert intent.parameters["supplemental_evidence_source"] == "targeted_recapture"
    assert intent.risk_level is InteractionRiskLevel.LOW


@pytest.mark.parametrize("action_kind", ("input", "warm_restore"))
@pytest.mark.asyncio
async def test_runtime_rejects_unsupported_replay_action_before_binding(
    action_kind: str,
) -> None:
    coordinator = Coordinator()
    facts = Facts()
    runtime = AppEvidenceRecoveryRuntime(
        surface_sessions=Sessions(),
        capture_coordinator=coordinator,
        facts=facts,
        now=lambda: NOW,
    )

    with pytest.raises(AppEvidenceRecoveryRuntimeError) as error:
        await runtime.execute(
            task_id="task-1",
            session_id="session-1",
            plan=_plan(),
            agent_run_id="run-1",
            finalization_epoch=3,
            step=_step(action_kind=action_kind),
            source=_observation("fresh-source", "home", "fp-home"),
        )

    assert error.value.reason_code == "RECOVERY_ACTION_NOT_SUPPORTED"
    assert facts.binds == []
    assert coordinator.commands == []


@pytest.mark.asyncio
async def test_runtime_rebinds_scroll_on_current_capture_and_verifies_after_state() -> None:
    coordinator = Coordinator()
    facts = Facts()
    runtime = AppEvidenceRecoveryRuntime(
        surface_sessions=Sessions(),
        capture_coordinator=coordinator,
        facts=facts,
        now=lambda: NOW,
    )

    receipt = await runtime.execute(
        task_id="task-1",
        session_id="session-1",
        plan=_plan(),
        agent_run_id="run-1",
        finalization_epoch=3,
        step=_step(action_kind="scroll", semantic_target=None),
        source=_observation("fresh-source", "home", "fp-home"),
    )

    assert receipt.after.capture_id == "fresh-after"
    assert facts.binds[0][0].capture_id == "fresh-source"
    assert len(coordinator.commands) == 1
    intent = coordinator.commands[0].intent
    assert intent.action_type is InteractionOperation.SCROLL
    assert intent.source_capture_id == "fresh-source"
    assert intent.parameters["candidate_id"] == "candidate-fresh-source"
    assert facts.reads == ["fresh-after"]


@pytest.mark.asyncio
async def test_runtime_accepts_low_risk_scroll_into_binding_path() -> None:
    """有界低风险 SCROLL 不再被白名单在绑定前硬拒。"""

    class NoCandidateFacts(Facts):
        async def bind(self, *, source, step):
            self.binds.append((source, step))
            raise AppEvidenceRecoveryRuntimeError(
                reason_code="RECOVERY_CANDIDATE_NOT_FOUND",
                message="当前视觉事实中没有可重新绑定的唯一低风险候选",
            )

    coordinator = Coordinator()
    facts = NoCandidateFacts()
    runtime = AppEvidenceRecoveryRuntime(
        surface_sessions=Sessions(),
        capture_coordinator=coordinator,
        facts=facts,
        now=lambda: NOW,
    )

    with pytest.raises(AppEvidenceRecoveryRuntimeError) as error:
        await runtime.execute(
            task_id="task-1",
            session_id="session-1",
            plan=_plan(),
            agent_run_id="run-1",
            finalization_epoch=3,
            step=_step(action_kind="scroll"),
            source=_observation("fresh-source", "home", "fp-home"),
        )

    assert error.value.reason_code == "RECOVERY_CANDIDATE_NOT_FOUND"
    assert facts.binds
    assert coordinator.commands == []


@pytest.mark.asyncio
async def test_runtime_keeps_semantic_match_for_non_scroll_without_target_text() -> None:
    class MismatchedFacts(Facts):
        async def bind(self, *, source, step):
            self.binds.append((source, step))
            return AppRecoveryCandidateBinding(
                candidate_id="candidate-fresh-source",
                purpose="返回上一级",
                action_type=InteractionOperation.BACK,
                semantic_target="错误语义",
            )

    coordinator = Coordinator()
    facts = MismatchedFacts()
    runtime = AppEvidenceRecoveryRuntime(
        surface_sessions=Sessions(),
        capture_coordinator=coordinator,
        facts=facts,
        now=lambda: NOW,
    )

    with pytest.raises(AppEvidenceRecoveryRuntimeError) as error:
        await runtime.execute(
            task_id="task-1",
            session_id="session-1",
            plan=_plan(),
            agent_run_id="run-1",
            finalization_epoch=3,
            step=_step(action_kind="back", semantic_target=None),
            source=_observation("fresh-source", "home", "fp-home"),
        )

    assert error.value.reason_code == "RECOVERY_CANDIDATE_BINDING_MISMATCH"
    assert coordinator.commands == []


@pytest.mark.asyncio
async def test_runtime_rejects_failed_interaction_without_reading_after_state() -> None:
    class FailedCoordinator(Coordinator):
        async def coordinate(self, command):
            result = await super().coordinate(command)
            return CaptureCoordinationResult(
                before_capture=result.before_capture,
                interaction=InteractionResult(
                    task_id="task-1",
                    session_id="session-1",
                    intent_id=command.intent.intent_id,
                    action_execution_id="action-1",
                    source_capture_id=result.before_capture.capture_id,
                    status=InteractionResultStatus.FAILED,
                    error_code="APP_ACTION_FAILED",
                ),
                after_capture=result.after_capture,
            )

    coordinator = FailedCoordinator()
    facts = Facts()
    runtime = AppEvidenceRecoveryRuntime(
        surface_sessions=Sessions(),
        capture_coordinator=coordinator,
        facts=facts,
        now=lambda: NOW,
    )

    with pytest.raises(AppEvidenceRecoveryRuntimeError) as error:
        await runtime.execute(
            task_id="task-1",
            session_id="session-1",
            plan=_plan(),
            agent_run_id="run-1",
            finalization_epoch=3,
            step=_step(),
            source=_observation("fresh-source", "home", "fp-home"),
        )

    assert error.value.reason_code == "RECOVERY_ACTION_FAILED"
    assert facts.reads == []


def _analysis(
    capture_id: str,
    *,
    candidates: tuple[AppVisualCandidate, ...],
) -> AppVisualAnalysis:
    return AppVisualAnalysis.build(
        capture_id=capture_id,
        screenshot_artifact_id=f"artifact-{capture_id}",
        image_signature="sha256:" + "a" * 64,
        width=1080,
        height=1920,
        orientation="portrait",
        visual_page_type="home",
        visual_summary="首页",
        key_elements=("内容详情",),
        candidates=candidates,
        provider_id="test",
        model_id="test",
        package_name="com.example.app",
        activity=".MainActivity",
        page_state="stable",
    )


def _candidate(
    capture_id: str,
    *,
    bounds: NormalizedBounds | None = None,
    risk_level: InteractionRiskLevel = InteractionRiskLevel.LOW,
    action_type: InteractionOperation = InteractionOperation.CLICK,
    semantic_name: str = "内容详情",
) -> AppVisualCandidate:
    normalized_bounds = bounds
    if action_type is not InteractionOperation.SCROLL and normalized_bounds is None:
        normalized_bounds = NormalizedBounds(left=0.1, top=0.2, right=0.4, bottom=0.3)
    return AppVisualCandidate.build(
        source_capture_id=capture_id,
        source_image_signature="sha256:" + "a" * 64,
        source_width=1080,
        source_height=1920,
        source_orientation="portrait",
        semantic_name=semantic_name,
        action_type=action_type,
        normalized_bounds=normalized_bounds,
        confidence=0.95,
        risk_level=risk_level,
        purpose="打开内容详情",
        expected_state_hint="detail",
    )


class Saved:
    def __init__(self, analysis: AppVisualAnalysis) -> None:
        self.observation_id = "observation-1"
        self.analysis = analysis
        self.observed_at = NOW
        self.created = False


class Observations:
    def __init__(self, analysis: AppVisualAnalysis) -> None:
        self.saved = Saved(analysis)

    async def get_by_capture_id(self, *, capture_id: str):
        assert capture_id == self.saved.analysis.capture_id
        return self.saved

    async def save(self, analysis):
        self.saved = Saved(analysis)
        return self.saved


class PageGraph:
    def __init__(self, *, status: str = "available") -> None:
        self.status = status

    async def get_graph(self, *, task_id, agent_run_id, finalization_epoch):
        return {
            "protocol_version": "page_graph.v1",
            "status": self.status,
            "task_id": task_id,
            "agent_run_id": agent_run_id,
            "finalization_epoch": finalization_epoch,
            "states": [
                {
                    "state_id": "fresh-source",
                    "task_id": task_id,
                    "agent_run_id": agent_run_id,
                    "surface_session_id": "session-1",
                    "surface_kind": "android",
                    "finalization_epoch": finalization_epoch,
                    "logical_page_key": "home",
                    "identity_status": "confirmed",
                    "state_fingerprint": "fp-home",
                    "counts_as_business_page": True,
                    "is_overlay": False,
                    "is_external": False,
                    "captured_at": NOW,
                }
            ],
            "transitions": [],
        }


class Understanding:
    async def understand(self, capture):
        raise AssertionError(f"不应重复理解已有视觉事实：{capture.capture_id}")


@pytest.mark.asyncio
async def test_visual_facts_reads_confirmed_state_and_binds_current_candidate() -> None:
    candidate = _candidate("fresh-source")
    observations = Observations(_analysis("fresh-source", candidates=(candidate,)))
    facts = AppRecoveryVisualFacts(
        understanding=Understanding(),
        observations=observations,
        page_graph=PageGraph(),
    )
    capture = _capture(
        "fresh-source",
        role="observation",
        parent_capture_id="session-current",
    )

    observation = await facts.read(
        capture=capture,
        agent_run_id="run-1",
        finalization_epoch=3,
    )
    binding = await facts.bind(source=observation, step=_step())

    assert observation.page_identity == "home"
    assert observation.state_fingerprint == "fp-home"
    assert binding.candidate_id == candidate.candidate_id
    assert binding.purpose == "打开内容详情"


@pytest.mark.asyncio
async def test_visual_facts_binds_unique_scroll_candidate_without_forcing_target_text() -> None:
    candidate = _candidate(
        "fresh-source",
        action_type=InteractionOperation.SCROLL,
        semantic_name="动态页面安全下滑",
    )
    facts = AppRecoveryVisualFacts(
        understanding=Understanding(),
        observations=Observations(_analysis("fresh-source", candidates=(candidate,))),
        page_graph=PageGraph(),
    )

    binding = await facts.bind(
        source=_observation("fresh-source", "home", "fp-home"),
        step=_step(action_kind="scroll", semantic_target=None),
    )

    assert binding.candidate_id == candidate.candidate_id
    assert binding.action_type is InteractionOperation.SCROLL
    assert binding.semantic_target == "动态页面安全下滑"


@pytest.mark.asyncio
async def test_visual_facts_rejects_ambiguous_current_candidates() -> None:
    candidates = (
        _candidate("fresh-source"),
        _candidate(
            "fresh-source",
            bounds=NormalizedBounds(left=0.5, top=0.2, right=0.8, bottom=0.3),
        ),
    )
    facts = AppRecoveryVisualFacts(
        understanding=Understanding(),
        observations=Observations(_analysis("fresh-source", candidates=candidates)),
        page_graph=PageGraph(),
    )

    with pytest.raises(AppEvidenceRecoveryRuntimeError) as error:
        await facts.bind(
            source=_observation("fresh-source", "home", "fp-home"),
            step=_step(),
        )

    assert error.value.reason_code == "RECOVERY_CANDIDATE_AMBIGUOUS"


@pytest.mark.asyncio
async def test_visual_facts_rejects_unavailable_page_graph_projection() -> None:
    candidate = _candidate("fresh-source")
    facts = AppRecoveryVisualFacts(
        understanding=Understanding(),
        observations=Observations(_analysis("fresh-source", candidates=(candidate,))),
        page_graph=PageGraph(status="legacy_unprojected"),
    )

    with pytest.raises(AppEvidenceRecoveryRuntimeError) as error:
        await facts.read(
            capture=_capture(
                "fresh-source",
                role="observation",
                parent_capture_id="session-current",
            ),
            agent_run_id="run-1",
            finalization_epoch=3,
        )

    assert error.value.reason_code == "RECOVERY_PAGE_STATE_UNAVAILABLE"
