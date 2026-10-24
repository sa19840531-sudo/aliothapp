"""Fake App 从初始截图到动作后截图的单轮闭环测试。"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace

from services.api.app.agents.runtime.models import ExecutionOutcome, TerminationReason
from services.api.app.application.app_exploration_target import (
    build_app_exploration_target_snapshot,
)
from services.api.app.application.app_exploration_turn import AppExplorationTurnRunner
from services.api.app.application.app_interaction_runtime import (
    AppCandidateResolver,
    AppCaptureWriter,
    AppInteractionExecutor,
)
from services.api.app.application.app_runtime_composition import (
    ProductionAppV2RuntimeExecutor,
)
from services.api.app.application.app_screenshot_exploration import (
    AppScreenshotExplorationWorkflow,
    ContinueAppScreenshotExplorationCommand,
)
from services.api.app.application.capture_coordinator import (
    CaptureCoordinator,
    InProcessSessionCommandGate,
)
from services.api.app.application.capture_profiles import CaptureProfilePolicy
from services.api.app.application.exploration_work_queue import (
    CommittedExplorerWorkSuccessor,
    ExplorerWorkLease,
    ExplorerWorkStatus,
)
from services.api.app.application.explorer_turn import ExplorerTurnExecutor
from services.api.app.application.interaction_command_validation import (
    InteractionCommandValidator,
)
from services.api.app.application.page_capture_persistence import PersistedEvidenceArtifact
from services.api.app.application.surface_sessions import AppExplorationPendingFacts
from services.api.app.core.config import Settings
from services.api.app.domain.exploration import (
    AppExplorationTerminalEvaluator,
    AppVisualAnalysis,
    AppVisualCandidate,
    NormalizedBounds,
    build_exploration_work,
)
from services.api.app.domain.interaction import (
    ActionExecutionSnapshot,
    ActionExecutionStatus,
    ActionIntent,
    AppInstallRequest,
    AppOpenRequest,
    AppReadyHandoff,
    CaptureKind,
    CaptureRef,
    InteractionOperation,
    InteractionResultStatus,
    InteractionRiskLevel,
    SurfaceKind,
    SurfaceSessionSnapshot,
    SurfaceSessionSpec,
    SurfaceSessionStatus,
    build_action_execution_id,
    build_action_intent_hash,
)
from services.api.app.infrastructure.app_runtime.factory import AppRuntimeFactory
from services.api.app.infrastructure.app_runtime.fake import FakeAppScenario
from services.api.app.infrastructure.db.app_visual_observation_repository import (
    SavedAppVisualObservation,
)
from services.api.app.infrastructure.queue.redis_queue import AgentRunRecord

NOW = datetime(2026, 8, 4, 12, 0, tzinfo=UTC)


class StaticSurfaceSessions:
    def __init__(self, reference) -> None:
        self.reference = reference

    async def get(self, *, session_id: str):
        assert session_id == self.reference.session_id
        return SurfaceSessionSnapshot(
            session_id=self.reference.session_id,
            task_id=self.reference.task_id,
            surface_kind=self.reference.surface_kind,
            session_owner="app-worker-1",
            status=SurfaceSessionStatus.ACTIVE,
            command_sequence=0,
            finalization_epoch=0,
            row_version=1,
            account_environment_ref=None,
            platform_build_ref="app-build:build-1",
            device_ref="fake-device-1",
            current_surface_ref=self.reference.primary_surface_ref,
            current_capture_id="capture-bootstrap",
            started_at=NOW,
            updated_at=NOW,
        )

    async def exploration_pending_facts(
        self,
        *,
        task_id: str,
        session_id: str,
    ) -> AppExplorationPendingFacts:
        assert task_id == self.reference.task_id
        assert session_id == self.reference.session_id
        return AppExplorationPendingFacts(
            task_id=task_id,
            session_id=session_id,
            pending_action_count=0,
            pending_evidence_count=0,
        )


class MemoryAppCaptures:
    def __init__(self) -> None:
        self.saved = {}
        self.calls = []
        self._next_id = 0

    def add(self, capture) -> None:
        self.saved[capture.capture.capture_id] = capture

    async def get_capture(self, *, capture_id: str):
        return self.saved.get(capture_id)

    async def persist_action_capture(self, **kwargs):
        self.calls.append(kwargs)
        self._next_id += 1
        capture_id = f"capture-action-{self._next_id}"
        screenshot_raw = next(
            item for item in kwargs["artifacts"].artifacts if item.artifact_type == "screenshot"
        )
        screenshot_bytes = screenshot_raw.content
        signature = f"sha256:{hashlib.sha256(screenshot_bytes).hexdigest()}"
        screenshot = PersistedEvidenceArtifact(
            artifact_id=f"artifact-screenshot-{self._next_id}",
            artifact_type="screenshot",
            storage_path=f"task/{capture_id}/screenshot.png",
            content_type="image/png",
            checksum=signature,
            size_bytes=len(screenshot_bytes),
            metadata={"encoding": "binary"},
        )
        artifact_ids = tuple(
            screenshot.artifact_id
            if item.artifact_type == "screenshot"
            else f"artifact-{item.artifact_type}-{self._next_id}"
            for item in kwargs["artifacts"].artifacts
        )
        from services.api.app.application.app_capture_persistence import (
            PersistedAppCapture,
        )

        persisted = PersistedAppCapture(
            capture=CaptureRef(
                task_id=kwargs["task_id"],
                session_id=kwargs["session_id"],
                capture_id=capture_id,
                surface_kind=kwargs["surface_kind"],
                capture_kind=CaptureKind.APP,
                role=kwargs["role"],
                artifact_ids=artifact_ids,
                parent_capture_id=kwargs["parent_capture_id"],
                action_execution_id=kwargs["action_execution_id"],
            ),
            agent_run_id=kwargs["agent_run_id"],
            screenshot=screenshot,
            width=int(kwargs["artifacts"].metadata["width"]),
            height=int(kwargs["artifacts"].metadata["height"]),
            orientation=str(kwargs["artifacts"].metadata["orientation"]),
            image_signature=signature,
            package_name=kwargs["artifacts"].metadata["package_name"],
            activity=kwargs["artifacts"].metadata["activity"],
        )
        self.add(persisted)
        return persisted


class ContextAwareFakeDriver:
    """在测试中显式模拟正式 Driver 的上下文执行端口。"""

    def __init__(self, delegate) -> None:
        self._delegate = delegate
        self.contexts: list[dict[str, object]] = []

    def __getattr__(self, name: str):
        return getattr(self._delegate, name)

    async def execute_with_context(
        self,
        *,
        session,
        intent,
        action_execution_id,
        execution_context,
    ):
        assert isinstance(execution_context, Mapping)
        context = dict(execution_context)
        assert set(context) == {
            "task_id",
            "session_id",
            "surface_kind",
            "action_execution_id",
            "source_capture_id",
            "finalization_epoch",
            "command_sequence",
            "lease_owner",
            "lease_token",
            "resolved_candidate_id",
        }
        assert context["task_id"] == session.task_id == intent.task_id
        assert context["session_id"] == session.session_id == intent.session_id
        assert context["surface_kind"] == session.surface_kind.value
        assert context["action_execution_id"] == action_execution_id
        assert context["source_capture_id"] == intent.source_capture_id
        sequence = context["command_sequence"]
        epoch = context["finalization_epoch"]
        assert isinstance(sequence, int) and not isinstance(sequence, bool) and sequence > 0
        assert isinstance(epoch, int) and not isinstance(epoch, bool) and epoch >= 0
        assert isinstance(context["lease_owner"], str) and context["lease_owner"]
        assert isinstance(context["lease_token"], str) and context["lease_token"]
        self.contexts.append(context)
        return await self._delegate.execute(
            session=session,
            intent=intent,
            action_execution_id=action_execution_id,
        )


class MemoryVisualObservations:
    def __init__(self, analysis: AppVisualAnalysis) -> None:
        self.saved = SavedAppVisualObservation(
            observation_id="visual-1",
            analysis=analysis,
            observed_at=NOW,
            created=False,
        )
        self._save_count = 0
        self.by_capture = {analysis.capture_id: self.saved}

    async def get_by_capture_id(self, *, capture_id: str):
        return self.by_capture.get(capture_id)

    async def save(self, analysis: AppVisualAnalysis):
        self._save_count += 1
        self.saved = SavedAppVisualObservation(
            observation_id="visual-1",
            analysis=analysis,
            observed_at=NOW.replace(microsecond=self._save_count * 1_000),
            created=True,
        )
        self.by_capture[analysis.capture_id] = self.saved
        return self.saved


class StaticUnderstanding:
    def __init__(self, analysis) -> None:
        self.analysis = analysis

    async def resolve_capture_image_signature(self, capture):
        del capture
        return self.analysis.image_signature

    async def understand(self, capture):
        assert capture.capture_id == self.analysis.capture_id
        return self.analysis


class CaptureBackedUnderstanding:
    def __init__(
        self,
        captures: MemoryAppCaptures,
        bootstrap_analysis: AppVisualAnalysis,
    ) -> None:
        self._captures = captures
        self._bootstrap_analysis = bootstrap_analysis

    async def resolve_capture_image_signature(self, capture):
        if capture.capture_id == self._bootstrap_analysis.capture_id:
            return self._bootstrap_analysis.image_signature
        return self._captures.saved[capture.capture_id].image_signature

    async def understand(self, capture):
        if capture.capture_id == self._bootstrap_analysis.capture_id:
            return self._bootstrap_analysis
        persisted = self._captures.saved[capture.capture_id]
        return AppVisualAnalysis.build(
            capture_id=capture.capture_id,
            screenshot_artifact_id=persisted.screenshot.artifact_id,
            image_signature=persisted.image_signature,
            width=persisted.width,
            height=persisted.height,
            orientation=persisted.orientation,
            package_name=persisted.package_name,
            activity=persisted.activity,
            visual_page_type="terminal",
            visual_summary="目标页",
            key_elements=(),
            candidates=(),
            provider_id="fake-vision",
            model_id="deterministic-v1",
            page_state="stable",
        )


class MemoryOrchestrator:
    async def start_exploration(self, command):
        return await self.prepare_exploration(command)

    async def prepare_exploration(self, command):
        return build_exploration_work(
            task_id=command.task_id,
            session_id=command.session_id,
            branch_id=command.branch_id,
            objective=command.objective,
            surface_kind=command.surface_kind,
            source_capture_id=command.source_capture_id,
            frontier_snapshot_id=command.frontier_snapshot_id,
            frontier=command.frontier,
            boundaries=command.boundaries,
            budget=command.budget,
            deadline=command.deadline,
            app_goal=command.app_goal,
        )


class QueuePublishingOrchestrator(MemoryOrchestrator):
    def __init__(self, queue) -> None:
        self._queue = queue

    async def start_exploration(self, command):
        work = await super().start_exploration(command)
        await self._queue.publish(work)
        return work


class SelectingExplorer:
    async def decide_next(self, work):
        if not work.frontier.candidates:
            evaluation = AppExplorationTerminalEvaluator(now=lambda: NOW).evaluate(
                work,
                eligible_candidate_count=0,
            )
            assert evaluation.terminal is not None
            return evaluation.terminal
        candidate = work.frontier.candidates[0]
        raw_progress = work.frontier.observation.metadata.get("app_curriculum_progress")
        phase_id = (
            raw_progress.get("current_phase_id") if isinstance(raw_progress, Mapping) else None
        )
        parameters = {
            "candidate_id": candidate.candidate_id,
            "app_curriculum_phase_id": phase_id,
        }
        curriculum_observation = candidate.metadata.get("curriculum_observation")
        if candidate.action_type is InteractionOperation.OBSERVE and isinstance(
            curriculum_observation, Mapping
        ):
            parameters["app_curriculum_observation"] = dict(curriculum_observation)
        coverage_seek_contract = candidate.metadata.get("coverage_page_seek_contract")
        if (
            candidate.action_type is InteractionOperation.CLICK
            and candidate.metadata.get("coverage_page_seek") is True
            and isinstance(coverage_seek_contract, Mapping)
        ):
            parameters["app_coverage_page_seek"] = dict(coverage_seek_contract)
        return ActionIntent(
            task_id=work.task_id,
            session_id=work.session_id,
            intent_id="intent-fake-cycle",
            idempotency_key="intent-fake-cycle",
            surface_kind=work.surface_kind,
            source_capture_id=work.source_capture_id,
            action_type=candidate.action_type,
            semantic_target=candidate.semantic_target,
            parameters=parameters,
            purpose=str(candidate.metadata.get("purpose") or "执行 Fake 截图候选"),
            risk_level=candidate.risk_level,
            capture_profile=CaptureProfilePolicy.for_surface(
                work.surface_kind,
                candidate.action_type,
            ),
            deadline=work.deadline,
        )


class MemoryActionExecutions:
    def __init__(self) -> None:
        self.state = None

    async def plan(self, *, intent, permit):
        self.state = ActionExecutionSnapshot(
            action_execution_id=build_action_execution_id(intent),
            task_id=intent.task_id,
            session_id=intent.session_id,
            intent_id=intent.intent_id,
            intent_hash=build_action_intent_hash(intent),
            command_sequence=permit.command_sequence,
            finalization_epoch=permit.finalization_epoch,
            requested_source_capture_id=intent.source_capture_id,
            status=ActionExecutionStatus.PLANNED,
            row_version=0,
            created_at=NOW,
        )
        return self.state

    async def mark_executing(
        self, *, action_execution_id, permit, source_capture_id, expected_row_version
    ):
        del action_execution_id, permit, expected_row_version
        self.state = replace(
            self.state,
            status=ActionExecutionStatus.EXECUTING,
            source_capture_id=source_capture_id,
            started_at=NOW,
            row_version=1,
        )
        return self.state

    async def record_interaction(
        self, *, interaction, permit, expected_status, expected_row_version
    ):
        del permit
        assert expected_status is ActionExecutionStatus.EXECUTING
        assert expected_row_version == self.state.row_version
        status_map = {
            InteractionResultStatus.EVIDENCE_PENDING: ActionExecutionStatus.EVIDENCE_PENDING,
            InteractionResultStatus.FAILED: ActionExecutionStatus.FAILED,
            InteractionResultStatus.BLOCKED: ActionExecutionStatus.BLOCKED,
            InteractionResultStatus.TIMED_OUT: ActionExecutionStatus.TIMED_OUT,
        }
        self.state = replace(
            self.state,
            status=status_map[interaction.status],
            result_payload=interaction.to_dict(),
            error_code=interaction.error_code,
            error_message=interaction.error_message,
            action_finished_at=NOW,
            row_version=self.state.row_version + 1,
        )
        return self.state

    async def complete_evidence(self, *, interaction, permit, expected_row_version):
        del permit, expected_row_version
        self.state = replace(
            self.state,
            status=ActionExecutionStatus.COMPLETED,
            result_capture_id=interaction.result_capture_id,
            result_payload=interaction.to_dict(),
            evidence_finished_at=NOW,
            row_version=3,
        )
        return self.state

    async def attach_terminal_evidence(self, *, interaction, permit, expected_row_version):
        del permit
        assert interaction.status in {
            InteractionResultStatus.FAILED,
            InteractionResultStatus.BLOCKED,
            InteractionResultStatus.TIMED_OUT,
        }
        assert interaction.result_capture_id is not None
        assert expected_row_version == self.state.row_version
        assert (
            self.state.status
            is {
                InteractionResultStatus.FAILED: ActionExecutionStatus.FAILED,
                InteractionResultStatus.BLOCKED: ActionExecutionStatus.BLOCKED,
                InteractionResultStatus.TIMED_OUT: ActionExecutionStatus.TIMED_OUT,
            }[interaction.status]
        )
        self.state = replace(
            self.state,
            result_capture_id=interaction.result_capture_id,
            result_payload=interaction.to_dict(),
            evidence_finished_at=NOW,
            row_version=self.state.row_version + 1,
        )
        return self.state


class NoopEvidenceExitGate:
    async def reserve(self, *, intent, permit):
        del intent, permit
        return None

    async def finalize(self, *, decision, status):
        del decision, status

    async def expire_stale(self, *, limit=100):
        del limit
        return 0


class MemoryQueue:
    def __init__(self, work=None) -> None:
        self.works = {work.work_id: work} if work is not None else {}
        self.statuses = {work.work_id: ExplorerWorkStatus.PENDING} if work is not None else {}
        self.row_versions = {work.work_id: 0} if work is not None else {}
        self.transitions = []
        self.claimed_work_ids = []
        self.renewals = []
        self.successor_commits = []

    async def publish(self, work):
        self.works.setdefault(work.work_id, work)
        self.statuses.setdefault(work.work_id, ExplorerWorkStatus.PENDING)
        self.row_versions.setdefault(work.work_id, 0)

    async def claim(self, *, command, lease_token):
        for work_id, work in self.works.items():
            if (
                work.task_id == command.task_id
                and self.statuses[work_id] is ExplorerWorkStatus.PENDING
            ):
                self.statuses[work_id] = ExplorerWorkStatus.LEASED
                self.row_versions[work_id] += 1
                self.claimed_work_ids.append(work_id)
                return ExplorerWorkLease(
                    work=work,
                    status=ExplorerWorkStatus.LEASED,
                    lease_token=lease_token,
                    attempt_count=1,
                    row_version=self.row_versions[work_id],
                )
        return None

    async def transition(self, *, command):
        self.transitions.append(command)
        assert self.statuses[command.work_id] is command.expected_status
        assert self.row_versions[command.work_id] == command.row_version
        self.statuses[command.work_id] = command.target_status
        self.row_versions[command.work_id] += 1
        return self.row_versions[command.work_id]

    async def renew(self, *, command):
        self.renewals.append(command)

    async def commit_successor(self, *, command):
        self.successor_commits.append(command)
        assert self.statuses[command.parent_work_id] is ExplorerWorkStatus.RUNNING
        assert self.row_versions[command.parent_work_id] == command.row_version
        self.statuses[command.parent_work_id] = ExplorerWorkStatus.SUCCEEDED
        self.row_versions[command.parent_work_id] += 1
        await self.publish(command.successor)
        return CommittedExplorerWorkSuccessor(
            parent_row_version=self.row_versions[command.parent_work_id],
            successor_work_id=command.successor.work_id,
            successor_created=True,
        )


def _bootstrap_capture(raw) -> object:
    from services.api.app.application.app_capture_persistence import PersistedAppCapture

    screenshot_bytes = next(
        item.content for item in raw.artifacts if item.artifact_type == "screenshot"
    )
    signature = f"sha256:{hashlib.sha256(screenshot_bytes).hexdigest()}"
    screenshot = PersistedEvidenceArtifact(
        artifact_id="artifact-bootstrap",
        artifact_type="screenshot",
        storage_path="task/capture-bootstrap/screenshot.png",
        content_type="image/png",
        checksum=signature,
        size_bytes=len(screenshot_bytes),
        metadata={"encoding": "binary"},
    )
    return PersistedAppCapture(
        capture=CaptureRef(
            task_id=raw.task_id,
            session_id=raw.session_id,
            capture_id="capture-bootstrap",
            surface_kind=SurfaceKind.ANDROID,
            capture_kind=CaptureKind.APP,
            role="bootstrap",
            artifact_ids=(screenshot.artifact_id,),
        ),
        agent_run_id="run-1",
        screenshot=screenshot,
        width=int(raw.metadata["width"]),
        height=int(raw.metadata["height"]),
        orientation=str(raw.metadata["orientation"]),
        image_signature=signature,
        package_name=raw.metadata["package_name"],
        activity=raw.metadata["activity"],
        bootstrap_row_version=1,
    )


async def test_fake_dynamic_frontier_reports_shared_target_gap() -> None:
    bootstrap_frame = b"\x89PNG\r\n\x1a\nbootstrap"
    after_frame = b"\x89PNG\r\n\x1a\nafter"
    bundle = AppRuntimeFactory(environment="test").build(
        "fake",
        fake_scenario=FakeAppScenario(
            screenshot_sequence=(bootstrap_frame, bootstrap_frame, after_frame)
        ),
    )
    driver = ContextAwareFakeDriver(bundle.driver)
    spec = SurfaceSessionSpec(
        task_id="task-1",
        session_id="session-1",
        surface_kind=SurfaceKind.ANDROID,
        platform_build_ref="app-build:build-1",
    )
    session = await driver.start_session(spec)
    installed = await driver.ensure_installed(
        AppInstallRequest(
            task_id=spec.task_id,
            session_id=spec.session_id,
            platform_build_ref=spec.platform_build_ref,
            sha256="a" * 64,
            package_name="com.example.target",
        )
    )
    assert installed.succeeded is True
    opened = await driver.open_app(
        AppOpenRequest(
            task_id=spec.task_id,
            session_id=spec.session_id,
            package_name="com.example.target",
        )
    )
    assert opened.succeeded is True
    raw = await driver.capture(
        session=session,
        profile=CaptureProfilePolicy.for_surface(SurfaceKind.ANDROID),
    )
    bootstrap = _bootstrap_capture(raw)
    captures = MemoryAppCaptures()
    captures.add(bootstrap)
    candidate = AppVisualCandidate.build(
        source_capture_id=bootstrap.capture.capture_id,
        source_image_signature=bootstrap.image_signature,
        source_width=bootstrap.width,
        source_height=bootstrap.height,
        source_orientation=bootstrap.orientation,
        semantic_name="主要操作按钮",
        action_type=InteractionOperation.CLICK,
        normalized_bounds=NormalizedBounds(0.1, 0.1, 0.9, 0.9),
        confidence=0.99,
        risk_level=InteractionRiskLevel.LOW,
        purpose="进入下一页",
    )
    analysis = AppVisualAnalysis.build(
        capture_id=bootstrap.capture.capture_id,
        screenshot_artifact_id=bootstrap.screenshot.artifact_id,
        image_signature=bootstrap.image_signature,
        width=bootstrap.width,
        height=bootstrap.height,
        orientation=bootstrap.orientation,
        package_name=bootstrap.package_name,
        activity=bootstrap.activity,
        visual_page_type="home",
        visual_summary="入口页",
        key_elements=("主要操作按钮",),
        candidates=(candidate,),
        provider_id="fake-vision",
        model_id="deterministic-v1",
        page_state="stable",
    )
    observations = MemoryVisualObservations(analysis)
    handoff = AppReadyHandoff(
        task_id=spec.task_id,
        agent_run_id="run-1",
        session_id=spec.session_id,
        installed_package="com.example.target",
        capture=bootstrap.capture,
        capability_manifest=driver.describe_capabilities(),
        bootstrap_row_version=1,
    )
    sessions = StaticSurfaceSessions(session)
    queue = MemoryQueue()
    workflow = AppScreenshotExplorationWorkflow(
        understanding=CaptureBackedUnderstanding(captures, analysis),
        observations=observations,
        orchestrator=QueuePublishingOrchestrator(queue),
        pending_facts=sessions,
    )
    coordinator = CaptureCoordinator(
        capture_writer=AppCaptureWriter(
            driver=driver,
            surface_sessions=sessions,
            persistence=captures,
        ),
        interaction_executor=AppInteractionExecutor(
            driver=driver,
            command_validator=InteractionCommandValidator(),
            surface_sessions=sessions,
            candidate_resolver=AppCandidateResolver(
                visual_observations=observations,
                captures=captures,
                prepare_before_visual=workflow.prepare_before_action_visual,
            ),
        ),
        evidence_exit_gate=NoopEvidenceExitGate(),
        session_command_gate=InProcessSessionCommandGate(),
        action_execution_repository=MemoryActionExecutions(),
    )
    runner = AppExplorationTurnRunner(
        queue=queue,
        turn_executor=ExplorerTurnExecutor(
            explorer=SelectingExplorer(),
            capture_coordinator=coordinator,
        ),
        continuation=workflow,
        lease_owner="app-explorer-1",
    )

    class Runtime:
        def __init__(self) -> None:
            self.outcomes = []
            self.close_commands = []

        async def bootstrap(self, command):
            assert command.task_id == spec.task_id
            return handoff

        async def publish_exploration(self, command):
            return await workflow.run(command)

        async def run_one_turn(self, *, task_id, agent_run_id, work_type=None):
            kwargs = {"task_id": task_id, "agent_run_id": agent_run_id}
            if work_type is not None:
                kwargs["work_type"] = work_type
            outcome = await runner.run_one(**kwargs)
            self.outcomes.append(outcome)
            return outcome

        async def close_exploration(self, command):
            self.close_commands.append(command)
            return SimpleNamespace(close_error=None)

        async def mark_exploration_lost(self, **kwargs):
            raise AssertionError(f"业务终态不得标记 LOST：{kwargs}")

    runtime = Runtime()
    run_started_at = datetime.now(UTC)
    target_snapshot = build_app_exploration_target_snapshot(
        journey_steps=("主要操作按钮",),
        required_page_types=("home",),
        required_milestones=("journey:未到达",),
        minimum_business_pages=1,
    )
    result = await ProductionAppV2RuntimeExecutor(
        worker_instance_id="app-worker-1",
        now=lambda: run_started_at,
    ).run(
        runtime,
        record=AgentRunRecord(
            run_id="run-1",
            task_id=spec.task_id,
            message="探索 Fake App",
            submitted_at=run_started_at,
            task_config={
                "workflow_version": "v2",
                "scan_mode": "full",
                "max_depth": 4,
                "journey_steps": ["主要操作按钮"],
                "app_exploration_target": target_snapshot.to_dict(),
                "_task_target": {
                    "platform": "app",
                    "target_url": None,
                    "app": {
                        "platform_build_ref": "app-build:build-1",
                        "package_name": "com.example.target",
                        "device": {"backend": "fake", "profile_id": "pixel-8"},
                    },
                },
            },
            page_sampling_policy={},
            loop_guard={
                "max_depth": 4,
                "max_duration_seconds": 3_600,
                "max_steps": None,
            },
        ),
        settings=Settings(),
    )
    assert len(runtime.outcomes) == 2, (runtime.outcomes, result)
    action_outcome, terminal_outcome = runtime.outcomes
    interaction = action_outcome.turn.coordination.interaction
    assert interaction.status is InteractionResultStatus.SUCCEEDED, (
        interaction.error_code, interaction.error_message,
    )
    initial_work = queue.works[queue.claimed_work_ids[0]]
    await workflow.prepare_after_action(
        ContinueAppScreenshotExplorationCommand(
            prior_work=initial_work,
            turn=action_outcome.turn,
        )
    )

    assert result.termination_reason is TerminationReason.INCOMPLETE
    assert result.execution_outcome is ExecutionOutcome.NEEDS_REVIEW
    assert result.working_memory.step_count == 2
    assert len(runtime.close_commands) == 1
    close_command = runtime.close_commands[0]
    assert close_command.session_id == "app:task-1"
    assert close_command.outcome.value == "evidence_insufficient"
    assert close_command.terminal_reason_code == "page_types_incomplete"
    assert {"payment", "video_detail"} <= set(
        close_command.coverage_summary["app_flow_contract"]["missing_page_types"]
    )
    assert close_command.capture_final_observation is True
    assert close_command.coverage_summary["consumed_turn_count"] == 2
    assert close_command.coverage_summary["terminal_status"] == "no_progress"
    assert close_command.coverage_summary["curriculum_complete"] is True
    assert close_command.coverage_summary["missing_page_types"] == ()
    assert close_command.coverage_summary["missing_milestones"] == ("journey:未到达",)
    assert action_outcome.final_status is ExplorerWorkStatus.SUCCEEDED
    assert action_outcome.result_capture_id == "capture-action-2"
    assert action_outcome.successor_work_id is not None
    assert terminal_outcome.final_status is ExplorerWorkStatus.NO_PROGRESS
    assert terminal_outcome.turn is not None
    assert terminal_outcome.turn.is_terminal is True
    assert len(queue.works) == 2
    assert queue.claimed_work_ids == [
        initial_work.work_id,
        action_outcome.successor_work_id,
    ]
    successor = queue.works[action_outcome.successor_work_id]
    assert successor.source_capture_id == "capture-action-2"
    assert successor.budget.remaining_actions == initial_work.budget.remaining_actions - 1
    assert successor.frontier.visited_state_ids == (
        initial_work.frontier.observation.state_version,
    )
    successor_payload = json.dumps(successor.to_dict(), ensure_ascii=False)
    assert "screenshot_png" not in successor_payload
    assert "\u0089PNG" not in successor_payload
    assert '"dom"' not in successor_payload
    assert [call["role"] for call in captures.calls] == [
        "before_action",
        "after_action",
    ]
    persisted_screenshots = [
        next(
            item.content
            for item in call["artifacts"].artifacts
            if item.artifact_type == "screenshot"
        )
        for call in captures.calls
    ]
    assert persisted_screenshots == [bootstrap_frame, after_frame]
    after_trace = next(
        item.content
        for item in captures.calls[1]["artifacts"].artifacts
        if item.artifact_type == "action_trace"
    )
    assert after_trace["entries"][0]["action"] == "click"
    assert len(driver.contexts) == 1
    context = driver.contexts[0]
    assert context["task_id"] == spec.task_id
    assert context["session_id"] == spec.session_id
    assert context["command_sequence"] == 1
    before_analysis = observations.by_capture["capture-action-1"].analysis
    assert context["resolved_candidate_id"] == before_analysis.candidates[0].candidate_id
    assert context["finalization_epoch"] == 0
    assert bundle.bootstrap_port is bundle.driver
