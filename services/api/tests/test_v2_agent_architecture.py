"""HTML 基线下两个 Agent 与 MAF 确定性 Orchestrator 的架构门禁。"""

import ast
import inspect
from datetime import UTC, datetime
from pathlib import Path

import pytest
from agent_framework import Executor, Workflow

from services.api.app.agents.explorer import ExplorerAgent
from services.api.app.agents.roles import V2AgentRole
from services.api.app.agents.rule_judge import RuleJudgeAgent
from services.api.app.agents.runtime.legacy_dispatch import LegacyV1WorkDispatcher
from services.api.app.agents.runtime.work_dispatcher import prepare_work_dispatch
from services.api.app.application import orchestration as orchestration_module
from services.api.app.application.evidence_requests import EvidenceRequestLeaseFence
from services.api.app.application.orchestration import (
    BEGIN_EXPLORATION_DRAINING_WORKFLOW_NAME,
    BEGIN_SESSION_DRAINING_WORKFLOW_NAME,
    EVALUATE_COMPLETION_GATE_WORKFLOW_NAME,
    ROUTE_EVIDENCE_REQUEST_WORKFLOW_NAME,
    START_EXPLORATION_WORKFLOW_NAME,
    AdvanceTaskFinalizationCommand,
    BeginExplorationDrainingCommand,
    BeginSessionDrainingCommand,
    BindSealedReportCommand,
    EvaluateCompletionGateCommand,
    OrchestrationWorkflowError,
    QcOrchestrator,
    RouteEvidenceRequestCommand,
    StartExplorationCommand,
    build_begin_exploration_draining_workflow,
    build_begin_session_draining_workflow,
    build_evaluate_completion_gate_workflow,
    build_route_evidence_request_workflow,
    build_start_exploration_workflow,
)
from services.api.app.domain.exploration import (
    CandidateAction,
    ExplorationBoundaries,
    ExplorationBudget,
    ExplorationSnapshotValidationError,
    FrontierSnapshot,
    SideEffectPolicy,
    SurfaceObservation,
)
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
from services.api.app.domain.task import (
    CompletionGateDecision,
    CompletionGateFacts,
    TaskExecutionStatus,
    TaskFinalizationSnapshot,
    TaskFinalizationState,
    TaskQualityOutcome,
    build_completion_gate_snapshot,
)


class RecordingPublisher:
    def __init__(self) -> None:
        self.items = []

    async def publish(self, work) -> None:
        self.items.append(work)


class FailingPublisher:
    async def publish(self, _work) -> None:
        raise RuntimeError("持久化发布失败")


class RecordingFinalizationCoordinator:
    def __init__(self) -> None:
        self.commands = []
        self.snapshot = TaskFinalizationSnapshot(
            task_id="task-1",
            state=TaskFinalizationState.OPEN,
            finalization_epoch=0,
            row_version=0,
            agent_run_id="run-1",
        )

    async def get(self, *, task_id: str):
        assert task_id == self.snapshot.task_id
        return self.snapshot

    async def begin_exploration_draining(self, command):
        self.commands.append(command)
        self.snapshot = TaskFinalizationSnapshot(
            task_id=command.task_id,
            state=TaskFinalizationState.EXPLORATION_DRAINING,
            finalization_epoch=0,
            row_version=1,
            agent_run_id=command.agent_run_id,
        )
        return self.snapshot

    async def begin_session_draining(self, command):
        self.commands.append(command)
        self.snapshot = TaskFinalizationSnapshot(
            task_id=command.task_id,
            state=TaskFinalizationState.SESSION_DRAINING,
            finalization_epoch=command.expected_finalization_epoch,
            row_version=2,
            agent_run_id=command.agent_run_id,
        )
        return self.snapshot

    async def evaluate_and_seal(self, command):
        self.commands.append(command)
        return self._decision(command)

    async def finalize_task_with_report(self, command):
        self.commands.append(command)
        return self._decision(command)

    async def bind_report(self, command):
        self.commands.append(command)
        return object()

    def _decision(self, command):
        epoch = command.expected_finalization_epoch + 1
        facts = CompletionGateFacts(
            exploration_terminal=True,
            pending_action_count=0,
            pending_capture_commit_count=0,
            nonterminal_enforced_work_item_count=0,
            active_session_request_count=0,
            candidate_rule_count=1,
            terminal_candidate_rule_count=1,
            background_execution_count=0,
            review_policy_satisfied=True,
        )
        snapshot = build_completion_gate_snapshot(
            task_id=command.task_id,
            finalization_epoch=epoch,
            evaluation_sequence=1,
            facts=facts,
            evaluated_at=datetime(2026, 8, 3, 18, 30, tzinfo=UTC),
        )
        decision = CompletionGateDecision(
            snapshot=snapshot,
            finalization=TaskFinalizationSnapshot(
                task_id=command.task_id,
                state=TaskFinalizationState.SEALED,
                finalization_epoch=epoch,
                row_version=4,
                gate_snapshot_id=snapshot.gate_snapshot_id,
                execution_status=command.execution_status,
                quality_outcome=command.quality_outcome,
            ),
        )
        self.snapshot = decision.finalization
        return decision


def _budget() -> ExplorationBudget:
    return ExplorationBudget(
        max_actions=10,
        remaining_actions=8,
        max_depth=4,
        remaining_duration_ms=30_000,
    )


def _advance_finalization_command(
    *,
    agent_run_id: str = "run-1",
) -> AdvanceTaskFinalizationCommand:
    return AdvanceTaskFinalizationCommand(
        task_id="task-1",
        agent_run_id=agent_run_id,
        report_id="report-1",
        report_version=2,
        execution_status=TaskExecutionStatus.COMPLETED,
        quality_outcome=TaskQualityOutcome.PASS,
        final_report={
            "summary": {"overall_status": "pass"},
            "generated_at": datetime(2026, 8, 18, 16, 0, tzinfo=UTC),
        },
    )


def _start_command(**overrides) -> StartExplorationCommand:
    observation = SurfaceObservation(
        capture_id="capture-1",
        surface_kind=SurfaceKind.WEB,
        location="https://example.com/",
        state_version="state-1",
        observed_at=datetime(2026, 8, 3, 18, 0, tzinfo=UTC),
        page_type="home",
        key_elements=("登录按钮",),
    )
    values = {
        "task_id": "task-1",
        "session_id": "session-1",
        "branch_id": "branch-login",
        "objective": "找到登录入口并进入登录页",
        "surface_kind": SurfaceKind.WEB,
        "source_capture_id": "capture-1",
        "frontier_snapshot_id": "frontier-1",
        "frontier": FrontierSnapshot(
            snapshot_id="frontier-1",
            observation=observation,
            candidates=(
                CandidateAction(
                    candidate_id="candidate-login",
                    semantic_target="登录按钮",
                    action_type=InteractionOperation.CLICK,
                    risk_level=InteractionRiskLevel.LOW,
                    exploration_value=90,
                    navigation_depth=1,
                ),
            ),
        ),
        "boundaries": ExplorationBoundaries(
            allowed_hosts=("example.com",),
            allowed_operations=(InteractionOperation.CLICK,),
            max_navigation_depth=4,
            side_effect_policy=SideEffectPolicy.READ_ONLY,
        ),
        "budget": _budget(),
        "deadline": datetime(2026, 8, 3, 19, 0, tzinfo=UTC),
    }
    values.update(overrides)
    return StartExplorationCommand(**values)


def test_v2_has_exactly_two_agent_roles_and_async_ports() -> None:
    assert list(V2AgentRole) == [
        V2AgentRole.EXPLORER,
        V2AgentRole.RULE_JUDGE,
    ]
    assert inspect.iscoroutinefunction(ExplorerAgent.decide_next)
    assert inspect.iscoroutinefunction(RuleJudgeAgent.judge)


def test_orchestrator_uses_maf_workflow_but_is_not_agent_code() -> None:
    package_dir = Path(orchestration_module.__file__).parent
    source = "\n".join(path.read_text(encoding="utf-8") for path in package_dir.glob("*.py"))
    normalized = source.casefold()

    assert "agent_framework" in normalized
    assert "services.api.app.agents" not in normalized
    assert "rule_judge_tools" not in normalized

    workflow_source = (package_dir / "maf_workflow.py").read_text(encoding="utf-8")
    imported_framework_names = {
        alias.name
        for node in ast.walk(ast.parse(workflow_source))
        if isinstance(node, ast.ImportFrom) and node.module == "agent_framework"
        for alias in node.names
    }
    assert {"Executor", "Workflow", "WorkflowBuilder", "WorkflowContext", "handler"} <= (
        imported_framework_names
    )
    assert imported_framework_names.isdisjoint({"Agent", "AgentExecutor", "WorkflowAgent"})
    assert ".as_agent(" not in normalized
    assert "chat_client" not in normalized

    agents_orchestrator = package_dir.parents[1] / "agents" / "orchestrator"
    assert list(agents_orchestrator.glob("*.py")) == []


def test_orchestrator_builds_typed_maf_executor_graphs() -> None:
    exploration_workflow = build_start_exploration_workflow(RecordingPublisher())
    probe_workflow = build_route_evidence_request_workflow(RecordingPublisher())

    assert isinstance(exploration_workflow, Workflow)
    assert isinstance(probe_workflow, Workflow)
    assert exploration_workflow.to_dict()["name"] == START_EXPLORATION_WORKFLOW_NAME
    assert probe_workflow.to_dict()["name"] == ROUTE_EVIDENCE_REQUEST_WORKFLOW_NAME
    assert exploration_workflow.input_types == [StartExplorationCommand]
    assert probe_workflow.input_types == [RouteEvidenceRequestCommand]
    assert [executor.id for executor in exploration_workflow.get_executors_list()] == [
        "build_exploration_work",
        "publish_exploration_work",
    ]
    assert [executor.id for executor in probe_workflow.get_executors_list()] == [
        "build_probe_work",
        "publish_probe_work",
    ]
    assert all(
        isinstance(executor, Executor)
        for workflow in (exploration_workflow, probe_workflow)
        for executor in workflow.get_executors_list()
    )


def test_orchestrator_builds_typed_maf_finalization_workflows() -> None:
    coordinator = RecordingFinalizationCoordinator()
    exploration_draining = build_begin_exploration_draining_workflow(coordinator)
    session_draining = build_begin_session_draining_workflow(coordinator)
    completion_gate = build_evaluate_completion_gate_workflow(coordinator)

    expected = (
        (
            exploration_draining,
            BEGIN_EXPLORATION_DRAINING_WORKFLOW_NAME,
            BeginExplorationDrainingCommand,
            "begin_exploration_draining",
        ),
        (
            session_draining,
            BEGIN_SESSION_DRAINING_WORKFLOW_NAME,
            BeginSessionDrainingCommand,
            "begin_session_draining",
        ),
        (
            completion_gate,
            EVALUATE_COMPLETION_GATE_WORKFLOW_NAME,
            EvaluateCompletionGateCommand,
            "evaluate_completion_gate",
        ),
    )
    for workflow, name, input_type, executor_id in expected:
        assert isinstance(workflow, Workflow)
        assert workflow.to_dict()["name"] == name
        assert workflow.input_types == [input_type]
        assert [executor.id for executor in workflow.get_executors_list()] == [executor_id]


async def test_orchestrator_only_builds_and_publishes_stable_exploration_work() -> None:
    exploration_publisher = RecordingPublisher()
    probe_publisher = RecordingPublisher()
    orchestrator = QcOrchestrator(
        exploration_publisher=exploration_publisher,
        probe_publisher=probe_publisher,
    )
    command = _start_command()

    first = await orchestrator.start_exploration(command)
    second = await orchestrator.start_exploration(command)

    assert first.work_id == second.work_id
    assert first.work_id.startswith("exploration-work:")
    assert first.protocol_version == "exploration-work.v1"
    assert first.to_dict()["frontier_snapshot_id"] == "frontier-1"
    assert exploration_publisher.items == [first, second]
    assert probe_publisher.items == []


async def test_orchestrator_can_prepare_successor_without_publishing_it() -> None:
    exploration_publisher = RecordingPublisher()
    orchestrator = QcOrchestrator(
        exploration_publisher=exploration_publisher,
        probe_publisher=RecordingPublisher(),
    )

    prepared = await orchestrator.prepare_exploration(_start_command())

    assert prepared.work_id.startswith("exploration-work:")
    assert exploration_publisher.items == []


async def test_evidence_request_is_deterministically_routed_to_probe_work() -> None:
    exploration_publisher = RecordingPublisher()
    probe_publisher = RecordingPublisher()
    orchestrator = QcOrchestrator(
        exploration_publisher=exploration_publisher,
        probe_publisher=probe_publisher,
    )
    request = build_evidence_request(
        work_item_id="inspection-work-1",
        attempt_id="attempt-1",
        task_id="task-1",
        rule_id="rule-1",
        rule_code="alioth_1",
        target_capture_id="capture-1",
        reason_code="TRUSTED_EVIDENCE_INVALID",
        evidence_goal="缺少动作后的截图",
        required_artifact_types=("capture_artifacts",),
        session_dependency=EvidenceSessionDependency.ACTIVE_SESSION,
        replay_policy=EvidenceReplayPolicy.SAME_SURFACE_ONLY,
        required_equivalence=EvidenceRequiredEquivalence.SAME_SURFACE_CONTINUATION,
        side_effect_policy=EvidenceSideEffectPolicy.READ_ONLY,
        target_invariants={"target_capture_id": "capture-1"},
        minimum_source_grade=EvidenceSourceGrade.A1,
        expires_at=datetime(2026, 8, 3, 19, 0, tzinfo=UTC),
    )

    probe = await orchestrator.route_evidence_request(
        RouteEvidenceRequestCommand(
            session_id="session-1",
            branch_id="branch-login",
            surface_kind=SurfaceKind.WEB,
            source_capture_id="capture-restored-1",
            restoration_plan_hash="restoration-plan-1",
            restoration_attempt_id="restoration-attempt-1",
            equivalence_assessment_id="state-equivalence-1",
            source_grade=EvidenceSourceGrade.A1,
            evidence_request=request,
            budget=_budget(),
            deadline=datetime(2026, 8, 3, 19, 0, tzinfo=UTC),
        )
    )

    assert probe.protocol_version == "probe-work.v2"
    assert probe.task_id == "task-1"
    assert probe.source_capture_id == "capture-restored-1"
    assert probe.restoration_attempt_id == "restoration-attempt-1"
    assert probe.evidence_request is request
    assert probe.work_id.startswith("probe-work:")
    assert probe_publisher.items == [probe]
    assert exploration_publisher.items == []


async def test_claimed_evidence_request_keeps_lease_fence_through_workflow() -> None:
    class ClaimedProbePublisher:
        def __init__(self) -> None:
            self.calls = []

        async def publish(self, work, *, request_lease=None) -> None:  # noqa: ANN001
            self.calls.append((work, request_lease))

    publisher = ClaimedProbePublisher()
    orchestrator = QcOrchestrator(probe_publisher=publisher)
    request = build_evidence_request(
        work_item_id="inspection-work-1",
        attempt_id="attempt-1",
        task_id="task-1",
        rule_id="rule-1",
        rule_code="alioth_1",
        target_capture_id="capture-1",
        reason_code="TRUSTED_EVIDENCE_INVALID",
        evidence_goal="缺少动作后的截图",
        required_artifact_types=("capture_artifacts",),
        session_dependency=EvidenceSessionDependency.ACTIVE_SESSION,
        replay_policy=EvidenceReplayPolicy.SAME_SURFACE_ONLY,
        required_equivalence=EvidenceRequiredEquivalence.SAME_SURFACE_CONTINUATION,
        side_effect_policy=EvidenceSideEffectPolicy.READ_ONLY,
        target_invariants={"target_capture_id": "capture-1"},
        minimum_source_grade=EvidenceSourceGrade.A1,
        expires_at=datetime(2026, 8, 3, 19, 0, tzinfo=UTC),
    )
    fence = EvidenceRequestLeaseFence(
        request_id=request.request_id,
        task_id=request.task_id,
        finalization_epoch=3,
        lease_owner="worker-1:app-recapture",
        lease_token="00000000-0000-0000-0000-000000000008",
        row_version=2,
    )

    probe = await orchestrator.route_evidence_request(
        RouteEvidenceRequestCommand(
            session_id="session-1",
            branch_id="branch-login",
            surface_kind=SurfaceKind.ANDROID,
            source_capture_id="capture-restored-1",
            restoration_plan_hash="restoration-plan-1",
            restoration_attempt_id="restoration-attempt-1",
            equivalence_assessment_id="state-equivalence-1",
            source_grade=EvidenceSourceGrade.A1,
            evidence_request=request,
            budget=_budget(),
            deadline=datetime(2026, 8, 3, 19, 0, tzinfo=UTC),
            request_lease=fence,
        )
    )

    assert publisher.calls == [(probe, fence)]


def test_exploration_snapshot_rejects_runtime_handles() -> None:
    with pytest.raises(ExplorationSnapshotValidationError, match="运行时句柄"):
        SurfaceObservation(
            capture_id="capture-1",
            surface_kind=SurfaceKind.WEB,
            location="https://example.com/",
            state_version="state-1",
            observed_at=datetime(2026, 8, 3, 18, 0, tzinfo=UTC),
            metadata={"driver": "forbidden"},
        )


async def test_orchestrator_does_not_report_success_when_publisher_fails() -> None:
    orchestrator = QcOrchestrator(
        exploration_publisher=FailingPublisher(),
        probe_publisher=RecordingPublisher(),
    )

    with pytest.raises(RuntimeError, match="持久化发布失败"):
        await orchestrator.start_exploration(_start_command())


async def test_orchestrator_runs_all_finalization_phases_through_maf() -> None:
    coordinator = RecordingFinalizationCoordinator()
    orchestrator = QcOrchestrator(
        exploration_publisher=RecordingPublisher(),
        probe_publisher=RecordingPublisher(),
        finalization_coordinator=coordinator,
    )

    exploration = await orchestrator.begin_exploration_draining(
        BeginExplorationDrainingCommand(
            task_id="task-1",
            agent_run_id="run-1",
            expected_row_version=0,
        )
    )
    sessions = await orchestrator.begin_session_draining(
        BeginSessionDrainingCommand(
            task_id="task-1",
            agent_run_id="run-1",
            expected_finalization_epoch=0,
            expected_row_version=1,
        )
    )
    decision = await orchestrator.evaluate_completion_gate(
        EvaluateCompletionGateCommand(
            task_id="task-1",
            agent_run_id="run-1",
            expected_finalization_epoch=0,
            expected_row_version=2,
            execution_status=TaskExecutionStatus.COMPLETED,
            quality_outcome=TaskQualityOutcome.PASS,
        )
    )

    assert exploration.state is TaskFinalizationState.EXPLORATION_DRAINING
    assert sessions.state is TaskFinalizationState.SESSION_DRAINING
    assert decision.snapshot.gate_passed is True
    assert decision.finalization.state is TaskFinalizationState.SEALED
    assert len(coordinator.commands) == 3


async def test_orchestrator_owns_complete_task_finalization_sequence() -> None:
    coordinator = RecordingFinalizationCoordinator()
    orchestrator = QcOrchestrator(finalization_coordinator=coordinator)

    progress = await orchestrator.advance_task_finalization(
        _advance_finalization_command()
    )

    assert progress.gate_passed is True
    assert progress.finalization_epoch == 1
    assert progress.block_reasons == ()
    assert [type(command).__name__ for command in coordinator.commands] == [
        "BeginExplorationDrainingCommand",
        "BeginSessionDrainingCommand",
        "FinalizeTaskWithReportCommand",
    ]
    finalization = coordinator.commands[-1]
    assert finalization.final_report["generated_at"] == "2026-08-18T16:00:00+00:00"


async def test_orchestrator_prepares_aggregate_work_before_completion_gate() -> None:
    coordinator = RecordingFinalizationCoordinator()

    class RecordingPreparer:
        def __init__(self) -> None:
            self.calls = []

        async def prepare(self, **kwargs):
            self.calls.append(kwargs)

    preparer = RecordingPreparer()
    orchestrator = QcOrchestrator(
        finalization_coordinator=coordinator,
        completion_gate_preparer=preparer,
    )

    await orchestrator.advance_task_finalization(_advance_finalization_command())

    assert preparer.calls == [
        {
            "task_id": "task-1",
            "agent_run_id": "run-1",
            "finalization_epoch": 0,
        }
    ]
    assert type(coordinator.commands[-1]).__name__ == "FinalizeTaskWithReportCommand"


async def test_orchestrator_resumes_finalization_from_persisted_phase() -> None:
    coordinator = RecordingFinalizationCoordinator()
    coordinator.snapshot = TaskFinalizationSnapshot(
        task_id="task-1",
        state=TaskFinalizationState.EXPLORATION_DRAINING,
        finalization_epoch=0,
        row_version=3,
        agent_run_id="run-1",
    )

    progress = await QcOrchestrator(
        finalization_coordinator=coordinator
    ).advance_task_finalization(_advance_finalization_command())

    assert progress.gate_passed is True
    assert [type(command).__name__ for command in coordinator.commands] == [
        "BeginSessionDrainingCommand",
        "FinalizeTaskWithReportCommand",
    ]


async def test_orchestrator_returns_sealed_finalization_idempotently() -> None:
    coordinator = RecordingFinalizationCoordinator()
    coordinator.snapshot = TaskFinalizationSnapshot(
        task_id="task-1",
        state=TaskFinalizationState.SEALED,
        finalization_epoch=4,
        row_version=9,
        agent_run_id="run-1",
        gate_snapshot_id="gate-sealed",
        execution_status=TaskExecutionStatus.COMPLETED,
        quality_outcome=TaskQualityOutcome.PASS,
    )

    progress = await QcOrchestrator(
        finalization_coordinator=coordinator
    ).advance_task_finalization(_advance_finalization_command())

    assert progress.gate_snapshot_id == "gate-sealed"
    assert progress.finalization_epoch == 4
    assert coordinator.commands == []


async def test_orchestrator_owns_sealed_report_binding_recovery() -> None:
    coordinator = RecordingFinalizationCoordinator()
    coordinator.snapshot = TaskFinalizationSnapshot(
        task_id="task-1",
        state=TaskFinalizationState.SEALED,
        finalization_epoch=4,
        row_version=9,
        agent_run_id="run-1",
        gate_snapshot_id="gate-sealed",
        execution_status=TaskExecutionStatus.COMPLETED,
        quality_outcome=TaskQualityOutcome.PASS,
    )
    report = {
        "report_stage": "final",
        "is_final": True,
        "generated_at": datetime(2026, 8, 18, 16, 30, tzinfo=UTC),
        "evidence": [
            {
                "captured_at": datetime(2026, 8, 18, 16, 31, tzinfo=UTC),
            }
        ],
    }

    progress = await QcOrchestrator(
        finalization_coordinator=coordinator
    ).bind_sealed_report(
        BindSealedReportCommand(
            task_id="task-1",
            agent_run_id="run-1",
            report_id="report-1",
            report_version=2,
            final_report=report,
        )
    )

    assert progress.gate_snapshot_id == "gate-sealed"
    assert len(coordinator.commands) == 1
    binding = coordinator.commands[0]
    assert binding.task_id == "task-1"
    assert binding.gate_snapshot_id == "gate-sealed"
    assert binding.final_report["generated_at"] == "2026-08-18T16:30:00+00:00"
    assert binding.final_report["evidence"][0]["captured_at"] == (
        "2026-08-18T16:31:00+00:00"
    )


def test_orchestrator_rejects_preview_report_binding_command() -> None:
    with pytest.raises(ValueError, match="正式报告"):
        BindSealedReportCommand(
            task_id="task-1",
            agent_run_id="run-1",
            report_id="report-1",
            report_version=2,
            final_report={"report_stage": "preview", "is_final": False},
        )


async def test_orchestrator_rejects_finalization_for_another_run() -> None:
    coordinator = RecordingFinalizationCoordinator()

    with pytest.raises(OrchestrationWorkflowError, match="Run"):
        await QcOrchestrator(
            finalization_coordinator=coordinator
        ).advance_task_finalization(_advance_finalization_command(agent_run_id="run-other"))

    assert coordinator.commands == []


async def test_orchestrator_rejects_finalization_without_coordinator_port() -> None:
    orchestrator = QcOrchestrator(
        exploration_publisher=RecordingPublisher(),
        probe_publisher=RecordingPublisher(),
    )

    with pytest.raises(OrchestrationWorkflowError, match="Task Finalization"):
        await orchestrator.begin_exploration_draining(
            BeginExplorationDrainingCommand(
                task_id="task-1",
                agent_run_id="run-1",
                expected_row_version=0,
            )
        )


def test_v1_dispatcher_is_explicit_legacy_compatibility_only() -> None:
    parameters = inspect.signature(prepare_work_dispatch).parameters

    assert list(parameters) == ["working_memory"]
    assert "legacy_dispatch" in inspect.getsourcefile(LegacyV1WorkDispatcher)
