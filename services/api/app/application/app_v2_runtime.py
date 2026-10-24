"""App V2 Bootstrap、截图探索与单轮执行的显式组合根。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from services.api.app.application.action_executions import (
    APP_WORKFLOW_CANCELED_ERROR_CODE,
    APP_WORKFLOW_CANCELED_ERROR_MESSAGE,
    ActionExecutionRepository,
)
from services.api.app.application.app_action_evidence import PostgresAppActionEvidenceStore
from services.api.app.application.app_bootstrap_state import AppBootstrapRepository
from services.api.app.application.app_capture_persistence import (
    AppCapturePersistenceService,
    PersistedAppCapture,
    VerifiedAppUiTree,
)
from services.api.app.application.app_capture_state_invariants import (
    AppCaptureStateInvariantReader,
)
from services.api.app.application.app_evidence_manifest import (
    AppEvidenceManifestFinalizer,
)
from services.api.app.application.app_exploration_closure import (
    AppExplorationClosureResult,
    AppExplorationClosureService,
    CloseAppExplorationCommand,
)
from services.api.app.application.app_exploration_evidence import (
    AppEvidenceIndexRepository,
    AppExplorationEvidenceAssembler,
)
from services.api.app.application.app_exploration_failures import (
    AppExplorationFailureRepository,
)
from services.api.app.application.app_exploration_reconciliation import (
    AppExplorationResultIntentRepository,
)
from services.api.app.application.app_exploration_results import (
    AppExplorationResultRepository,
)
from services.api.app.application.app_exploration_runtime_recovery import (
    AppExplorationRuntimeCheckpoint,
    AppExplorationRuntimeRecoveryRepository,
    AppExplorationWorkRecovery,
)
from services.api.app.application.app_exploration_turn import (
    AppExplorationTurnOutcome,
    AppExplorationTurnRunner,
)
from services.api.app.application.app_frontier_queue import AppFrontierQueue
from services.api.app.application.app_global_frontier import AppGlobalFrontierService
from services.api.app.application.app_interaction_runtime import (
    AppCandidateResolver,
    AppCaptureWriter,
    AppInteractionExecutor,
    DriverAppRuntimeIdentityReader,
)
from services.api.app.application.app_live_inspection_publish import (
    AppActiveSessionInspectionDrainService,
)
from services.api.app.application.app_probe_completion import AppProbeCompletionService
from services.api.app.application.app_restoration_executor import (
    AppRestorationInteractionExecutor,
    RestoredSnapshotReader,
)
from services.api.app.application.app_screenshot_exploration import (
    AppExplorationOrchestratorPort,
    AppScreenshotExplorationCommand,
    AppScreenshotExplorationResult,
    AppScreenshotExplorationWorkflow,
    AppVisualObservationRepositoryPort,
)
from services.api.app.application.app_screenshot_understanding import (
    AppScreenshotUnderstandingService,
    ScreenshotArtifactReader,
    ScreenshotUnderstandingProvider,
)
from services.api.app.application.app_supplemental_evidence_drain import (
    AppSupplementalEvidenceDrainService,
)
from services.api.app.application.app_targeted_recapture_composition import (
    AppTargetedRecaptureRuntimeFactory,
)
from services.api.app.application.app_targeted_recapture_consumer import (
    AppTargetedRecaptureConsumer,
)
from services.api.app.application.app_targeted_recapture_planning import (
    AppTargetedRecaptureContextReader,
)
from services.api.app.application.app_workflow import (
    AppQcWorkflow,
    AppReadyPublisher,
    AppWorkflowCancellation,
    RunAppWorkflowCommand,
    VerifiedPlatformBuildReader,
)
from services.api.app.application.capture_coordination import SessionCommandGate
from services.api.app.application.capture_coordinator import CaptureCoordinator
from services.api.app.application.device_lease_settlement import (
    preserves_cloud_phone_lease,
    settle_device_lease_after_session_loss,
)
from services.api.app.application.device_pool import (
    DeviceLeaseGuard,
    get_device_lease,
    get_runtime_device_pool,
)
from services.api.app.application.evidence_exit_gate import EvidenceExitGate
from services.api.app.application.evidence_manifests import EvidenceManifestRepository
from services.api.app.application.evidence_requests import EvidenceRequestRepository
from services.api.app.application.exploration_scheduler_facts import (
    ExplorationSchedulerFactsReader,
)
from services.api.app.application.exploration_work_queue import (
    ExplorerWorkQueue,
    ExplorerWorkStatus,
    ExplorerWorkType,
)
from services.api.app.application.explorer_turn import (
    ExplorerDecisionPort,
    ExplorerTurnExecutor,
)
from services.api.app.application.flow_foundation_runtime import (
    build_app_flow_foundation_runtime,
)
from services.api.app.application.inspection_work_items import InspectionWorkItemService
from services.api.app.application.interaction_command_validation import (
    InteractionCommandValidator,
    TrustedTaskCommandScopeReader,
)
from services.api.app.application.page_graph_contracts import PageGraphRepository
from services.api.app.application.restoration import RestorationAttemptRepository
from services.api.app.application.restored_probe_publication import (
    RestoredProbeOrchestrator,
)
from services.api.app.application.supplemental_evidence_policy import (
    SupplementalEvidencePolicy,
)
from services.api.app.application.surface_sessions import SurfaceSessionRepository
from services.api.app.core.config import WorkflowSettings
from services.api.app.domain.evidence import AppExplorationFailureFact, AppExplorationResult
from services.api.app.domain.inspection import StateInvariantSnapshot
from services.api.app.domain.interaction import (
    AppBootstrapPort,
    AppReadyHandoff,
    CaptureRef,
    SurfaceDriver,
    SurfaceKind,
)
from services.api.app.infrastructure.app_runtime.factory import (
    AppRuntimeBundle,
    AppRuntimeFactory,
)
from services.api.app.infrastructure.app_runtime.fake import FakeAppScenario
from services.api.app.infrastructure.app_runtime.official_api import OfficialAppApiClient
from services.api.app.infrastructure.db.app_evidence_index_repository import (
    PostgresAppEvidenceIndexRepository,
)
from services.api.app.infrastructure.db.app_exploration_result_repository import (
    PostgresAppExplorationResultRepository,
)
from services.api.app.infrastructure.db.app_supplemental_evidence_drain_repository import (
    PostgresAppSupplementalEvidenceDrainSettlementRepository,
    PostgresAppSupplementalEvidenceDrainStateReader,
)
from services.api.app.infrastructure.db.evidence_manifest_repository import (
    PostgresEvidenceManifestRepository,
)
from services.api.app.infrastructure.db.exploration_scheduler_facts_repository import (
    PostgresExplorationSchedulerFactsReader,
)
from services.api.app.infrastructure.db.flow_foundation_repository import (
    PostgresFlowFoundationRepository,
)


class AppBootstrapWorkflowPort(Protocol):
    async def run(self, command: RunAppWorkflowCommand) -> AppReadyHandoff: ...


class AppExplorationWorkflowPort(Protocol):
    async def run(
        self,
        command: AppScreenshotExplorationCommand,
    ) -> AppScreenshotExplorationResult: ...


class AppTurnRunnerPort(Protocol):
    async def run_one(
        self,
        *,
        task_id: str,
        agent_run_id: str,
        work_type: ExplorerWorkType | None = None,
    ) -> AppExplorationTurnOutcome: ...

    async def next_claim_at(
        self,
        *,
        task_id: str,
        work_type: ExplorerWorkType = ExplorerWorkType.EXPLORATION,
    ) -> datetime | None: ...

    async def expire_unclaimable(self, *, task_id: str) -> int: ...

    async def cancel_nonterminal(self, *, task_id: str) -> int: ...


class AppExplorationClosurePort(Protocol):
    async def close(
        self,
        command: CloseAppExplorationCommand,
    ) -> AppExplorationClosureResult: ...

    async def mark_lost(
        self,
        *,
        task_id: str,
        session_id: str,
        reason: str,
        expected_surface_kind: SurfaceKind | None = None,
        expected_build_id: str | None = None,
    ) -> object: ...


@dataclass(frozen=True, kw_only=True)
class AppV2RuntimeDependencies:
    """由 API/Worker 组合层提供的既有持久化与 Agent 端口。"""

    platform_builds: VerifiedPlatformBuildReader
    surface_sessions: SurfaceSessionRepository
    bootstraps: AppBootstrapRepository
    captures: AppCapturePersistenceService
    app_ready_publisher: AppReadyPublisher
    artifact_reader: ScreenshotArtifactReader
    understanding_provider: ScreenshotUnderstandingProvider
    visual_observations: AppVisualObservationRepositoryPort
    exploration_orchestrator: AppExplorationOrchestratorPort
    exploration_queue: ExplorerWorkQueue
    explorer: ExplorerDecisionPort
    evidence_exit_gate: EvidenceExitGate
    action_executions: ActionExecutionRepository
    session_command_gate: SessionCommandGate
    active_session_inspection: AppActiveSessionInspectionDrainService
    app_frontier_queue: AppFrontierQueue | None = None
    evidence_manifests: EvidenceManifestRepository | None = None
    evidence_indexes: AppEvidenceIndexRepository | None = None
    exploration_results: AppExplorationResultRepository | None = None
    result_intents: AppExplorationResultIntentRepository | None = None
    runtime_recovery: AppExplorationRuntimeRecoveryRepository | None = None
    exploration_failures: AppExplorationFailureRepository | None = None
    supplemental_evidence_policy: SupplementalEvidencePolicy = field(
        default_factory=SupplementalEvidencePolicy
    )
    cancellation: AppWorkflowCancellation | None = None
    allowed_packages: tuple[str, ...] = ()
    command_scope_reader: TrustedTaskCommandScopeReader | None = None
    scheduler_facts_reader: ExplorationSchedulerFactsReader | None = None
    page_graph: PageGraphRepository | None = None
    targeted_recapture_contexts: AppTargetedRecaptureContextReader | None = None
    restoration_attempts: RestorationAttemptRepository | None = None
    probe_orchestrator: RestoredProbeOrchestrator | None = None
    probe_completion: AppProbeCompletionService | None = None
    evidence_requests: EvidenceRequestRepository | None = None
    inspection_work_items: InspectionWorkItemService | None = None

    def __post_init__(self) -> None:
        if self.active_session_inspection is None:
            raise ValueError("App V2 Runtime 必须显式提供活会话规则排空器")


@dataclass(frozen=True, kw_only=True)
class AppV2Runtime:
    """显式暴露 Bootstrap、探索、单轮执行与封存关闭入口。"""

    runtime_bundle: AppRuntimeBundle
    bootstrap_workflow: AppBootstrapWorkflowPort
    exploration_workflow: AppExplorationWorkflowPort
    turn_runner: AppTurnRunnerPort
    closure_service: AppExplorationClosurePort | None = None
    runtime_recovery: AppExplorationRuntimeRecoveryRepository | None = None
    exploration_failures: AppExplorationFailureRepository | None = None
    cancellation: AppWorkflowCancellation | None = None
    interaction_executor: AppInteractionExecutor | None = None
    restoration_coordinator: CaptureCoordinator | None = None
    restoration_snapshot_reader: RestoredSnapshotReader | None = None
    surface_sessions: SurfaceSessionRepository | None = None
    # 轻量测试 Runtime 可以省略；正式组合根必须显式注入，取消时不得静默跳过动作账本。
    action_executions: ActionExecutionRepository | None = None
    targeted_recapture_factory: AppTargetedRecaptureRuntimeFactory | None = None
    probe_completion: AppProbeCompletionService | None = None
    active_session_inspection: AppActiveSessionInspectionDrainService | None = None
    device_lease_seconds: int = 300
    _device_lease_guards: dict[str, DeviceLeaseGuard] = field(
        default_factory=dict,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        if self.runtime_bundle.bootstrap_port is not self.runtime_bundle.driver:
            raise ValueError("App Bootstrap 与 Interaction 必须共享同一 Runtime 实例")
        # 完整组合根一旦提供恢复后的正式 Capture 读取器，就必须同时提供
        # 恢复 Coordinator；否则 hangback 不能安全地执行真实 App 动作。
        if isinstance(self.interaction_executor, AppInteractionExecutor) or (
            self.restoration_snapshot_reader is not None
        ):
            self.require_restoration_coordinator()

    def require_restoration_coordinator(self) -> CaptureCoordinator:
        """返回正式 App 恢复 Coordinator，缺失或不完整时立即拒绝。"""

        coordinator = self.restoration_coordinator
        if coordinator is None or not callable(getattr(coordinator, "coordinate", None)):
            raise ValueError("App V2 Runtime 正式恢复缺少 CaptureCoordinator")
        return coordinator

    def require_targeted_recapture_factory(self) -> AppTargetedRecaptureRuntimeFactory:
        """返回正式定向补采组合；轻量 Runtime 缺失时立即拒绝。"""

        factory = self.targeted_recapture_factory
        if factory is None:
            raise ValueError("App V2 Runtime 缺少定向补采组合")
        return factory

    @property
    def bootstrap_port(self) -> AppBootstrapPort:
        return self.runtime_bundle.bootstrap_port

    @property
    def bootstrap_driver(self) -> SurfaceDriver:
        return self.runtime_bundle.driver

    @property
    def interaction_driver(self) -> SurfaceDriver:
        return self.runtime_bundle.driver

    async def bootstrap(self, command: RunAppWorkflowCommand) -> AppReadyHandoff:
        handoff = await self.bootstrap_workflow.run(command)
        session_id = getattr(command, "session_id", None)
        if isinstance(session_id, str) and session_id:
            await self._start_device_lease_guard(session_id=session_id)
        if self.interaction_executor is not None:
            if handoff.installed_package is None:
                raise RuntimeError("APP_READY Handoff 缺少 installed_package")
            self.interaction_executor.bind_installed_package(handoff.installed_package)
        return handoff

    async def publish_exploration(
        self,
        command: AppScreenshotExplorationCommand,
    ) -> AppScreenshotExplorationResult:
        await self._assert_device_lease_healthy()
        return await self.exploration_workflow.run(command)

    async def run_one_turn(
        self,
        *,
        task_id: str,
        agent_run_id: str,
        work_type: ExplorerWorkType | None = None,
    ) -> AppExplorationTurnOutcome:
        kwargs: dict[str, object] = {
            "task_id": task_id,
            "agent_run_id": agent_run_id,
        }
        if work_type is not None:
            kwargs["work_type"] = work_type
        await self._assert_device_lease_healthy()
        outcome = await self.turn_runner.run_one(**kwargs)  # type: ignore[arg-type]
        if (
            outcome.claimed
            and outcome.work_type == ExplorerWorkType.PROBE.value
            and outcome.final_status is ExplorerWorkStatus.SUCCEEDED
        ):
            if self.probe_completion is None:
                raise RuntimeError("App V2 Runtime 缺少 Probe Completion 服务")
            await self.probe_completion.complete(task_id=task_id, outcome=outcome)
        return outcome

    async def inspect_current_app_surface(
        self,
        *,
        task_id: str,
        agent_run_id: str,
        deadline: datetime,
    ) -> object:
        """探索动作前消费真实活会话规则，保持设备租约与原任务时限。"""

        await self._assert_device_lease_healthy()
        if self.active_session_inspection is None:
            raise RuntimeError("App V2 Runtime 缺少活会话规则检查点")
        return await self.active_session_inspection.run_current_surface_checkpoint(
            task_id=task_id,
            agent_run_id=agent_run_id,
            deadline=deadline,
        )

    async def close_exploration(
        self,
        command: CloseAppExplorationCommand,
    ) -> AppExplorationClosureResult:
        if self.closure_service is None:
            raise RuntimeError("App V2 Runtime 未配置 Exploration Closure")
        session_id = getattr(command, "session_id", None)
        guard = (
            self._device_lease_guards.pop(session_id, None) if isinstance(session_id, str) else None
        )
        if guard is None:
            return await self.closure_service.close(command)
        try:
            return await guard.run(self.closure_service.close(command))
        finally:
            await guard.stop()

    async def get_exploration_runtime_checkpoint(
        self,
        *,
        agent_run_id: str,
    ) -> AppExplorationRuntimeCheckpoint | None:
        if self.runtime_recovery is None:
            return None
        return await self.runtime_recovery.get_checkpoint(agent_run_id=agent_run_id)

    async def create_exploration_runtime_checkpoint(
        self,
        *,
        checkpoint: AppExplorationRuntimeCheckpoint,
    ) -> AppExplorationRuntimeCheckpoint:
        if self.runtime_recovery is None:
            return checkpoint
        return await self.runtime_recovery.create_checkpoint(checkpoint=checkpoint)

    async def get_exploration_work_recovery(
        self,
        *,
        task_id: str,
        agent_run_id: str,
        surface_session_id: str,
    ) -> AppExplorationWorkRecovery | None:
        if self.runtime_recovery is None:
            return None
        return await self.runtime_recovery.get_work_recovery(
            task_id=task_id,
            agent_run_id=agent_run_id,
            surface_session_id=surface_session_id,
        )

    async def get_persisted_exploration_result(
        self,
        *,
        agent_run_id: str,
    ) -> AppExplorationResult | None:
        if self.runtime_recovery is None:
            return None
        return await self.runtime_recovery.get_result(agent_run_id=agent_run_id)

    async def record_exploration_failure(
        self,
        *,
        failure: AppExplorationFailureFact,
    ) -> object:
        """记录独立技术失败事实；未配置仓储的测试 Runtime 保持兼容。"""

        if self.exploration_failures is None:
            return None
        return await self.exploration_failures.record(failure=failure)

    async def is_cancel_requested(self, *, task_id: str, agent_run_id: str) -> bool:
        if self.cancellation is None:
            return False
        return await self.cancellation.is_cancel_requested(
            task_id=task_id,
            agent_run_id=agent_run_id,
        )

    async def next_exploration_claim_at(self, *, task_id: str) -> datetime | None:
        return await self.turn_runner.next_claim_at(task_id=task_id)

    async def expire_unclaimable_exploration(self, *, task_id: str) -> int:
        return await self.turn_runner.expire_unclaimable(task_id=task_id)

    async def cancel_exploration_work(self, *, task_id: str) -> int:
        return await self.turn_runner.cancel_nonterminal(task_id=task_id)

    async def cancel_workflow(self, *, task_id: str) -> int:
        """收束明确取消的整个 App 任务，并保留 Work/Action 两类失败。"""

        work_count = 0
        action_count = 0
        work_error: Exception | None = None
        action_error: Exception | None = None
        try:
            work_count = await self.cancel_exploration_work(task_id=task_id)
        except Exception as exc:  # noqa: BLE001 - 仍需尝试收束动作账本
            work_error = exc
        action_executions = self.action_executions
        if action_executions is None:
            action_error = RuntimeError(
                "App V2 Runtime 正式取消缺少 ActionExecutionRepository"
            )
        else:
            try:
                action_count = await action_executions.fail_nonterminal_for_task(
                    task_id=task_id,
                    error_code=APP_WORKFLOW_CANCELED_ERROR_CODE,
                    error_message=APP_WORKFLOW_CANCELED_ERROR_MESSAGE,
                )
            except Exception as exc:  # noqa: BLE001 - 失败必须回到取消收口层
                action_error = exc
        if work_error is not None:
            if action_error is not None:
                raise work_error from action_error
            raise work_error
        if action_error is not None:
            raise action_error
        return work_count + action_count

    async def mark_exploration_lost(
        self,
        *,
        task_id: str,
        session_id: str,
        reason: str,
        expected_surface_kind: SurfaceKind | None = None,
        expected_build_id: str | None = None,
    ) -> object:
        if preserves_cloud_phone_lease(reason):
            # 取消不得把 session 標丟失，否則租約續期停掉，流程接不下去。
            return None
        guard = self._device_lease_guards.pop(session_id, None)
        try:
            if self.closure_service is None:
                return None
            return await self.closure_service.mark_lost(
                task_id=task_id,
                session_id=session_id,
                reason=reason,
                expected_surface_kind=expected_surface_kind,
                expected_build_id=expected_build_id,
            )
        finally:
            if guard is not None:
                try:
                    await guard.stop()
                finally:
                    # 与 Closure 对齐：暂态 Capture／Official 故障只释放，不 quarantine。
                    await settle_device_lease_after_session_loss(
                        session_id=session_id,
                        reason=reason,
                    )

    async def _start_device_lease_guard(self, *, session_id: str) -> None:
        existing = self._device_lease_guards.get(session_id)
        if existing is not None:
            await existing.assert_healthy()
            return
        lease = await get_device_lease(session_id=session_id)
        if lease is None:
            return
        guard = DeviceLeaseGuard(
            pool=await get_runtime_device_pool(),
            lease=lease,
            lease_seconds=max(300, self.device_lease_seconds),
        )
        await guard.start()
        self._device_lease_guards[session_id] = guard

    async def _assert_device_lease_healthy(self) -> None:
        for guard in tuple(self._device_lease_guards.values()):
            await guard.assert_healthy()


class _AppCaptureScreenshotSource:
    """把公共 App Capture 服务适配为截图理解只读端口。"""

    def __init__(self, captures: AppCapturePersistenceService) -> None:
        self._captures = captures

    async def load(self, *, capture: CaptureRef) -> PersistedAppCapture | None:
        persisted = await self._captures.get_capture(capture_id=capture.capture_id)
        if persisted is None or persisted.capture != capture:
            return None
        return persisted

    async def load_ui_tree(self, *, capture: CaptureRef) -> VerifiedAppUiTree | None:
        """只返回已通过对象身份校验的当前 Capture UI Tree。"""

        return await self._captures.get_verified_ui_tree(capture=capture)


class _AppRestoredSnapshotReader:
    """从真实 After Capture、视觉理解和 UI Tree 生成恢复后不变量。"""

    def __init__(
        self,
        *,
        captures: AppCapturePersistenceService,
        understanding: AppScreenshotUnderstandingService,
        observations: AppVisualObservationRepositoryPort,
    ) -> None:
        self._reader = AppCaptureStateInvariantReader(
            captures=captures,
            understanding=understanding,
            observations=observations,
        )

    async def read(self, *, capture: CaptureRef) -> StateInvariantSnapshot:
        return await self._reader.read(
            capture=capture,
            required_role="after_action",
        )


class _AppProbeTurnRuntime:
    """把共享 App Turn Runner 适配为补采协调器的 Probe 入口。"""

    def __init__(self, turn_runner: AppTurnRunnerPort) -> None:
        self._turn_runner = turn_runner

    async def run_one_turn(
        self,
        *,
        task_id: str,
        agent_run_id: str,
        work_type: ExplorerWorkType,
    ) -> AppExplorationTurnOutcome:
        return await self._turn_runner.run_one(
            task_id=task_id,
            agent_run_id=agent_run_id,
            work_type=work_type,
        )


def build_app_v2_runtime(
    *,
    provider: str,
    environment: str,
    dependencies: AppV2RuntimeDependencies,
    lease_owner: str,
    fake_scenario: FakeAppScenario | None = None,
    workflow_settings: WorkflowSettings | None = None,
    official_client: OfficialAppApiClient | None = None,
    lease_seconds: int = 180,
    retry_delay_seconds: int = 5,
) -> AppV2Runtime:
    """装配可测试 App V2 Runtime；不会把 App 路由接入生产 Worker。"""
    bundle = AppRuntimeFactory(
        environment=environment,
        workflow_settings=workflow_settings,
    ).build(
        provider,
        fake_scenario=fake_scenario,
        official_client=official_client,
    )
    understanding = AppScreenshotUnderstandingService(
        sources=_AppCaptureScreenshotSource(dependencies.captures),
        artifacts=dependencies.artifact_reader,
        provider=dependencies.understanding_provider,
    )
    bootstrap_workflow = AppQcWorkflow(
        platform_builds=dependencies.platform_builds,
        surface_sessions=dependencies.surface_sessions,
        bootstraps=dependencies.bootstraps,
        bootstrap_port=bundle.bootstrap_port,
        driver=bundle.driver,
        captures=dependencies.captures,
        publisher=dependencies.app_ready_publisher,
        cancellation=dependencies.cancellation,
    )
    scheduler_facts_reader = (
        dependencies.scheduler_facts_reader or PostgresExplorationSchedulerFactsReader()
    )
    # 生产组合根挂 PostgreSQL 写穿；单测仍可 build_app_flow_foundation_runtime() 用纯内存。
    flow_foundation_runtime = build_app_flow_foundation_runtime(
        repo=PostgresFlowFoundationRepository(),
    )
    flow_foundation_p1 = flow_foundation_runtime.p1
    exploration_workflow = AppScreenshotExplorationWorkflow(
        understanding=understanding,
        observations=dependencies.visual_observations,
        orchestrator=dependencies.exploration_orchestrator,
        pending_facts=dependencies.surface_sessions,
        scheduler_facts_reader=scheduler_facts_reader,
        flow_foundation_runtime=flow_foundation_runtime,
    )
    command_validator = InteractionCommandValidator(
        scope_reader=dependencies.command_scope_reader,
    )
    interaction_executor = AppInteractionExecutor(
        driver=bundle.driver,
        surface_sessions=dependencies.surface_sessions,
        candidate_resolver=AppCandidateResolver(
            visual_observations=dependencies.visual_observations,
            captures=dependencies.captures,
            prepare_before_visual=exploration_workflow.prepare_before_action_visual,
        ),
        runtime_identity_reader=DriverAppRuntimeIdentityReader(bundle.driver),
        allowed_packages=dependencies.allowed_packages,
        supplemental_evidence_policy=dependencies.supplemental_evidence_policy,
        command_validator=command_validator,
    )
    app_capture_writer = AppCaptureWriter(
        driver=bundle.driver,
        surface_sessions=dependencies.surface_sessions,
        persistence=dependencies.captures,
        flow_foundation_runtime=flow_foundation_runtime,
    )
    action_evidence_writer = PostgresAppActionEvidenceStore()
    coordinator = CaptureCoordinator(
        capture_writer=app_capture_writer,
        interaction_executor=interaction_executor,
        evidence_exit_gate=dependencies.evidence_exit_gate,
        session_command_gate=dependencies.session_command_gate,
        action_execution_repository=dependencies.action_executions,
        action_recovery_captures=dependencies.captures,
        action_evidence_writer=action_evidence_writer,
        supplemental_evidence_policy=dependencies.supplemental_evidence_policy,
        command_validator=command_validator,
        flow_foundation_p1=flow_foundation_p1,
    )
    restoration_interaction_executor = AppRestorationInteractionExecutor(
        driver=bundle.driver,
        surface_sessions=dependencies.surface_sessions,
        package_name="",
        command_validator=command_validator,
    )
    restoration_coordinator = CaptureCoordinator(
        capture_writer=app_capture_writer,
        interaction_executor=restoration_interaction_executor,
        evidence_exit_gate=dependencies.evidence_exit_gate,
        session_command_gate=dependencies.session_command_gate,
        action_execution_repository=dependencies.action_executions,
        action_recovery_captures=dependencies.captures,
        action_evidence_writer=action_evidence_writer,
        supplemental_evidence_policy=dependencies.supplemental_evidence_policy,
        command_validator=command_validator,
        flow_foundation_p1=flow_foundation_p1,
    )
    restoration_snapshot_reader = _AppRestoredSnapshotReader(
        captures=dependencies.captures,
        understanding=understanding,
        observations=dependencies.visual_observations,
    )
    targeted_recapture_ports = (
        dependencies.page_graph,
        dependencies.targeted_recapture_contexts,
        dependencies.restoration_attempts,
        dependencies.probe_orchestrator,
    )
    if any(port is not None for port in targeted_recapture_ports) and not all(
        port is not None for port in targeted_recapture_ports
    ):
        raise ValueError("App 定向补采组合必须同时提供页面图、恢复仓储和 Probe Orchestrator")
    targeted_recapture_factory = None
    if all(port is not None for port in targeted_recapture_ports):
        targeted_recapture_factory = AppTargetedRecaptureRuntimeFactory(
            page_graph=dependencies.page_graph,
            planning_contexts=dependencies.targeted_recapture_contexts,
            surface_sessions=dependencies.surface_sessions,
            capture_coordinator=restoration_coordinator,
            captures=dependencies.captures,
            understanding=understanding,
            observations=dependencies.visual_observations,
            original_snapshots=AppCaptureStateInvariantReader(
                captures=dependencies.captures,
                understanding=understanding,
                observations=dependencies.visual_observations,
            ),
            restored_snapshots=restoration_snapshot_reader,
            restoration_attempts=dependencies.restoration_attempts,
            probe_orchestrator=dependencies.probe_orchestrator,
        )
    frontier_queue = dependencies.app_frontier_queue
    global_frontier = (
        AppGlobalFrontierService(repository=frontier_queue) if frontier_queue is not None else None
    )
    turn_runner = AppExplorationTurnRunner(
        queue=dependencies.exploration_queue,
        turn_executor=ExplorerTurnExecutor(
            explorer=dependencies.explorer,
            capture_coordinator=coordinator,
        ),
        continuation=exploration_workflow,
        lease_owner=lease_owner,
        lease_seconds=lease_seconds,
        retry_delay_seconds=retry_delay_seconds,
        cancellation=dependencies.cancellation,
        supplemental_evidence_policy=dependencies.supplemental_evidence_policy,
        global_frontier=global_frontier,
        frontier_queue=frontier_queue,
        scheduler_facts_reader=scheduler_facts_reader,
    )
    manifest_repository = dependencies.evidence_manifests or PostgresEvidenceManifestRepository()
    index_repository = dependencies.evidence_indexes or PostgresAppEvidenceIndexRepository()
    result_repository = dependencies.exploration_results or PostgresAppExplorationResultRepository()

    async def _load_page_graph_shadow(
        *,
        task_id: str,
        agent_run_id: str,
    ):
        repo = dependencies.page_graph
        if repo is None:
            return None
        getter = getattr(repo, "get_graph", None)
        if not callable(getter):
            return None
        graph = await getter(
            task_id=task_id,
            agent_run_id=agent_run_id,
            finalization_epoch=None,
        )
        if not isinstance(graph, dict) or graph.get("status") == "unavailable":
            return None
        return graph

    supplemental_drain = None
    supplemental_ports = (
        targeted_recapture_factory,
        dependencies.evidence_requests,
        dependencies.inspection_work_items,
        dependencies.probe_completion,
    )
    supplemental_runtime_enabled = (
        dependencies.supplemental_evidence_policy.enabled
        and dependencies.supplemental_evidence_policy.runtime_ready
    )
    if supplemental_runtime_enabled and not all(port is not None for port in supplemental_ports):
        raise ValueError("App 补采排空必须提供请求仓储、质检唤醒、Probe Completion 和定向补采")
    if supplemental_runtime_enabled:
        supplemental_drain = AppSupplementalEvidenceDrainService(
            inspection=dependencies.active_session_inspection,
            recapture=AppTargetedRecaptureConsumer(
                requests=dependencies.evidence_requests,
                runtime_factory=targeted_recapture_factory,
                policy=dependencies.supplemental_evidence_policy,
                lease_owner=f"{lease_owner}:app-recapture",
                lease_seconds=lease_seconds,
                retry_delay_seconds=retry_delay_seconds,
            ),
            probe_runtime=_AppProbeTurnRuntime(turn_runner),
            probe_completion=dependencies.probe_completion,
            inspection_wakeup=dependencies.inspection_work_items,
            state_reader=PostgresAppSupplementalEvidenceDrainStateReader(),
            exhaustion_settlement=PostgresAppSupplementalEvidenceDrainSettlementRepository(),
            policy=dependencies.supplemental_evidence_policy,
        )
    closure = AppExplorationClosureService(
        capture_coordinator=coordinator,
        captures=dependencies.captures,
        manifests=manifest_repository,
        manifest_finalizer=AppEvidenceManifestFinalizer(
            repository=manifest_repository,
            artifact_store=dependencies.artifact_reader,
        ),
        evidence_assembler=AppExplorationEvidenceAssembler(),
        evidence_indexes=index_repository,
        exploration_results=result_repository,
        result_intents=dependencies.result_intents,
        understanding=understanding,
        visual_observations=dependencies.visual_observations,
        sessions=dependencies.surface_sessions,
        driver=bundle.driver,
        active_session_inspection=dependencies.active_session_inspection,
        supplemental_evidence_drain=supplemental_drain,
        cancellation=dependencies.cancellation,
        page_graph_shadow_loader=_load_page_graph_shadow,
    )
    return AppV2Runtime(
        runtime_bundle=bundle,
        bootstrap_workflow=bootstrap_workflow,
        exploration_workflow=exploration_workflow,
        turn_runner=turn_runner,
        closure_service=closure,
        runtime_recovery=dependencies.runtime_recovery,
        exploration_failures=dependencies.exploration_failures,
        cancellation=dependencies.cancellation,
        interaction_executor=interaction_executor,
        restoration_coordinator=restoration_coordinator,
        restoration_snapshot_reader=restoration_snapshot_reader,
        surface_sessions=dependencies.surface_sessions,
        action_executions=dependencies.action_executions,
        targeted_recapture_factory=targeted_recapture_factory,
        probe_completion=dependencies.probe_completion,
        active_session_inspection=dependencies.active_session_inspection,
        device_lease_seconds=max(300, lease_seconds),
    )
