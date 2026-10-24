"""App Worker 的生产组合根与 V2 Runtime 执行器。"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from services.api.app.agents.explorer.bootstrap import build_default_explorer_agent
from services.api.app.agents.runtime.action_trace import InMemoryActionTraceSink
from services.api.app.agents.runtime.inspection_result_writer import (
    CurrentRuleDecisionWriter,
)
from services.api.app.agents.runtime.inspection_work_item_consumer import (
    InspectionWorkItemConsumer,
)
from services.api.app.agents.runtime.models import (
    ErrorCategory,
    ExecutionOutcome,
    RunResult,
    TerminationReason,
)
from services.api.app.agents.runtime.working_memory import WorkingMemory
from services.api.app.application.app_candidate_eligibility import (
    count_eligible_app_candidates,
)
from services.api.app.application.app_capture_persistence import AppCapturePersistenceService
from services.api.app.application.app_evidence_manifest import AppEvidenceManifestFinalizer
from services.api.app.application.app_exploration_closure import (
    CloseAppExplorationCommand,
)
from services.api.app.application.app_exploration_persistence_gate import (
    AppExplorationPersistenceGate,
    ExplorationCutoffDecision,
)
from services.api.app.application.app_exploration_policy import (
    AppExplorationPolicyError,
    AppExplorationRuntimePolicy,
    build_dynamic_app_exploration_runtime_policy,
)
from services.api.app.application.app_exploration_runtime_recovery import (
    AppExplorationRuntimeCheckpoint,
    AppExplorationWorkRecovery,
    build_app_exploration_runtime_checkpoint,
)
from services.api.app.application.app_exploration_turn import AppExplorationTurnOutcome
from services.api.app.application.app_inspection_publication import (
    APP_PUBLICATION_SNAPSHOT_MISMATCH,
    APP_PUBLICATION_SNAPSHOT_MISMATCH_CODE,
    AppPublicationSnapshotMismatch,
)
from services.api.app.application.app_inspection_publisher import (
    build_default_app_capture_inspection_publisher,
)
from services.api.app.application.app_live_inspection_publish import (
    AppActiveSessionInspectionDrainService,
    AppLiveInspectionPublishService,
)
from services.api.app.application.app_metrics import (
    APP_EXPLORATION_STALE_SESSION_TOTAL,
    APP_TECHNICAL_FAILURE_TOTAL,
    record_app_metric_safely,
)
from services.api.app.application.app_probe_completion import AppProbeCompletionService
from services.api.app.application.app_screenshot_exploration import (
    AppScreenshotExplorationCommand,
)
from services.api.app.application.app_screenshot_understanding import (
    ScreenshotUnderstandingProvider,
)
from services.api.app.application.app_v2_runtime import (
    AppV2Runtime,
    AppV2RuntimeDependencies,
    build_app_v2_runtime,
)
from services.api.app.application.app_workflow import (
    NoopAppReadyPublisher,
    RunAppWorkflowCommand,
)
from services.api.app.application.app_workflow_dispatch import (
    AppWorkflowHandler,
    RuntimeBackedAppWorkflowHandler,
    task_target_from_dict,
)
from services.api.app.application.dynamic_app_exploration import (
    build_dynamic_app_exploration_objective,
)
from services.api.app.application.evidence_request_admission import (
    EvidenceRequestAdmissionService,
)
from services.api.app.application.evidence_supplements import EvidenceSupplementAttacher
from services.api.app.application.exploration_work_queue import ExplorerWorkStatus
from services.api.app.application.explorer_turn import ExplorerTurnResult
from services.api.app.application.inspection_work_items import InspectionWorkItemService
from services.api.app.application.orchestration import QcOrchestrator
from services.api.app.application.rule_result_reuse import (
    build_current_rule_reuse_planner,
    rule_result_reuse_enabled,
)
from services.api.app.application.supplement_input_set_reseal import (
    SupplementInputSetResealService,
)
from services.api.app.application.supplemental_evidence_policy import (
    resolve_supplemental_evidence_policy,
)
from services.api.app.application.surface_phase_explorer import SurfacePhaseAwareExplorer
from services.api.app.core.config import Settings
from services.api.app.domain.evidence import (
    AppExplorationResultOutcome,
    build_app_exploration_failure_fact,
)
from services.api.app.domain.evidence.index import ExplorationEvidenceStatus
from services.api.app.domain.exploration import (
    AppExplorationOutcome,
    AppExplorationTerminalEvaluator,
    ExplorationBoundaries,
    ExplorationBudget,
    ExplorationTerminal,
    ExplorationTerminalStatus,
    SideEffectPolicy,
    build_app_exploration_goal,
    build_split_exploration_budget,
)
from services.api.app.domain.interaction import (
    AppBootstrapStage,
    AppReadyHandoff,
    AppWorkflowFailure,
    InteractionOperation,
    SurfaceKind,
)
from services.api.app.domain.task import TaskTarget, TaskTargetPlatform
from services.api.app.infrastructure.app_runtime.official_api import (
    HttpOfficialAppApiClient,
    OfficialAppApiClient,
)
from services.api.app.infrastructure.app_vision.factory import resolve_app_vision_provider
from services.api.app.infrastructure.db.action_execution_repository import (
    PostgresActionExecutionRepository,
)
from services.api.app.infrastructure.db.app_bootstrap_repository import (
    PostgresAppBootstrapRepository,
)
from services.api.app.infrastructure.db.app_capture_repository import (
    PostgresAppCaptureRepository,
)
from services.api.app.infrastructure.db.app_evidence_index_repository import (
    PostgresAppEvidenceIndexRepository,
)
from services.api.app.infrastructure.db.app_exploration_failure_repository import (
    PostgresAppExplorationFailureRepository,
)
from services.api.app.infrastructure.db.app_exploration_reconciliation_repository import (
    PostgresAppExplorationResultIntentRepository,
)
from services.api.app.infrastructure.db.app_exploration_result_repository import (
    PostgresAppExplorationResultRepository,
)
from services.api.app.infrastructure.db.app_exploration_runtime_recovery_repository import (
    PostgresAppExplorationRuntimeRecoveryRepository,
)
from services.api.app.infrastructure.db.app_frontier_repository import (
    PostgresAppFrontierRepository,
)
from services.api.app.infrastructure.db.app_live_inspection_repository import (
    PostgresAppLiveInspectionRepository,
)
from services.api.app.infrastructure.db.app_probe_completion_repository import (
    PostgresAppProbeCompletionRecoveryReader,
)
from services.api.app.infrastructure.db.app_targeted_recapture_context_repository import (
    PostgresAppTargetedRecaptureContextReader,
)
from services.api.app.infrastructure.db.app_visual_observation_repository import (
    PostgresAppVisualObservationRepository,
)
from services.api.app.infrastructure.db.command_scope_reader import (
    PostgresTaskCommandScopeReader,
)
from services.api.app.infrastructure.db.evidence_exit_gate_repository import (
    PostgresEvidenceExitGate,
)
from services.api.app.infrastructure.db.evidence_input_set_repository import (
    PostgresEvidenceInputSetRepository,
)
from services.api.app.infrastructure.db.evidence_manifest_repository import (
    PostgresEvidenceManifestRepository,
)
from services.api.app.infrastructure.db.evidence_request_repository import (
    PostgresEvidenceRequestRepository,
)
from services.api.app.infrastructure.db.evidence_supplement_repository import (
    PostgresEvidenceSupplementRepository,
)
from services.api.app.infrastructure.db.explorer_work_repository import (
    PostgresExplorationWorkPublisher,
    PostgresExplorerWorkRepository,
    PostgresProbeWorkPublisher,
)
from services.api.app.infrastructure.db.inspection_work_item_repository import (
    PostgresInspectionWorkItemRepository,
)
from services.api.app.infrastructure.db.page_graph_repository import (
    PostgresPageGraphRepository,
)
from services.api.app.infrastructure.db.platform_build_repository import (
    PostgresPlatformBuildRepository,
)
from services.api.app.infrastructure.db.restoration_attempt_repository import (
    PostgresRestorationAttemptRepository,
)
from services.api.app.infrastructure.db.session_command_gate import (
    PostgresSessionCommandGate,
)
from services.api.app.infrastructure.db.surface_phase_repository import (
    PostgresSurfacePhaseRepository,
)
from services.api.app.infrastructure.db.surface_session_repository import (
    PostgresSurfaceSessionRepository,
)
from services.api.app.infrastructure.device.app_exploration_abort_log import (
    log_app_exploration_abort,
)
from services.api.app.infrastructure.queue.redis_queue import AgentRunRecord, RedisAgentRunQueue
from services.api.app.infrastructure.queue.state_store import AgentRunStateStore
from services.api.app.infrastructure.storage.factory import build_evidence_artifact_store

logger = logging.getLogger(__name__)


@dataclass(kw_only=True)
class _AppActionBudgetGuard:
    """动作预算与 Work 轮数分离，预算归零后只允许一次终态确认。"""

    max_actions: int
    consumed_actions: int = 0
    terminal_confirmation_claimed: bool = False

    def __post_init__(self) -> None:
        for field_name in ("max_actions", "consumed_actions"):
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{field_name} 必须为非负整数")
        if self.consumed_actions > self.max_actions:
            raise ValueError("consumed_actions 不能超过 max_actions")

    @property
    def can_claim(self) -> bool:
        return self.consumed_actions < self.max_actions or not self.terminal_confirmation_claimed

    def record(self, outcome: AppExplorationTurnOutcome) -> None:
        if not outcome.claimed or outcome.final_status is ExplorerWorkStatus.RETRY_WAIT:
            return
        if _outcome_consumed_action(outcome):
            if self.consumed_actions >= self.max_actions:
                raise RuntimeError("App Explorer 超出已锁定的动作预算")
            self.consumed_actions += 1
            return
        if self.consumed_actions >= self.max_actions:
            self.terminal_confirmation_claimed = True


def _build_supplemental_evidence_budget(
    *,
    max_actions: int,
    consumed_actions: int,
    max_depth: int,
    deadline: datetime,
    now: datetime,
    persisted_budget: ExplorationBudget | None,
) -> ExplorationBudget:
    """把主探索的剩余硬边界交给关闭前补采，保留 business/obstruction 分账。"""

    remaining_actions = max(max_actions - consumed_actions, 0)
    remaining_duration_ms = max(int((deadline - now).total_seconds() * 1_000), 0)
    if persisted_budget is None:
        return ExplorationBudget(
            max_actions=max(max_actions, 1),
            remaining_actions=remaining_actions,
            max_depth=max_depth,
            remaining_duration_ms=remaining_duration_ms,
        )

    remaining_actions = min(remaining_actions, persisted_budget.remaining_actions)
    remaining_duration_ms = min(
        remaining_duration_ms,
        persisted_budget.remaining_duration_ms,
    )
    # 优先保留清障池，再填业务池，避免把 obstruction 抹进 business。
    rem_obstruction = min(
        persisted_budget.resolved_remaining_obstruction_actions,
        remaining_actions,
    )
    rem_business = min(
        persisted_budget.resolved_remaining_business_actions,
        remaining_actions - rem_obstruction,
    )
    if rem_business + rem_obstruction < remaining_actions:
        rem_business = remaining_actions - rem_obstruction
    # 冻结口径：max_* 沿用已持久化 Work 预算；仅收紧 remaining_*。
    frozen_max_actions = max(persisted_budget.max_actions, 1)
    max_business = persisted_budget.resolved_max_business_actions
    max_obstruction = persisted_budget.resolved_max_obstruction_actions
    if max_business + max_obstruction != frozen_max_actions:
        max_obstruction = min(max_obstruction, frozen_max_actions)
        max_business = frozen_max_actions - max_obstruction
    rem_obstruction = min(rem_obstruction, max_obstruction)
    rem_business = min(rem_business, max_business)
    if rem_business + rem_obstruction > remaining_actions:
        rem_obstruction = min(rem_obstruction, remaining_actions)
        rem_business = remaining_actions - rem_obstruction
    return ExplorationBudget(
        max_actions=frozen_max_actions,
        remaining_actions=remaining_actions,
        max_depth=min(max_depth, persisted_budget.max_depth),
        remaining_duration_ms=remaining_duration_ms,
        max_business_actions=max_business,
        remaining_business_actions=rem_business,
        max_obstruction_actions=max_obstruction,
        remaining_obstruction_actions=rem_obstruction,
        business_duration_floor_ms=persisted_budget.business_duration_floor_ms,
        business_duration_floor_applied=persisted_budget.business_duration_floor_applied,
    )


class QueueAppWorkflowCancellation:
    """把已有 Run 队列的取消状态适配为 App Workflow 合同。"""

    def __init__(
        self,
        queue: RedisAgentRunQueue,
        *,
        durable_source: AgentRunStateStore | None = None,
    ) -> None:
        self._queue = queue
        self._durable_source = durable_source

    async def is_cancel_requested(self, *, task_id: str, agent_run_id: str) -> bool:
        del task_id
        if await self._queue.is_cancel_requested(agent_run_id):
            return True
        if self._durable_source is None:
            return False
        return await self._durable_source.is_cancel_requested(agent_run_id)


class PostgresAppRuntimeProvider:
    """仅从已锁定的 App 任务快照装配一次 App V2 Runtime。"""

    def __init__(
        self,
        *,
        worker_instance_id: str,
        official_client: OfficialAppApiClient | None = None,
        cancellation: QueueAppWorkflowCancellation | None = None,
        understanding_provider: ScreenshotUnderstandingProvider | None = None,
        runtime_builder: Callable[..., AppV2Runtime] = build_app_v2_runtime,
        lab_supplemental_ready: bool = False,
    ) -> None:
        if not worker_instance_id or worker_instance_id != worker_instance_id.strip():
            raise ValueError("worker_instance_id 必须为非空且无首尾空白的字符串")
        self._worker_instance_id = worker_instance_id
        self._official_client = official_client
        self._cancellation = cancellation
        self._understanding_provider = understanding_provider
        self._runtime_builder = runtime_builder
        self._lab_supplemental_ready = bool(lab_supplemental_ready)

    async def build(self, *, record: AgentRunRecord, settings: Settings) -> AppV2Runtime:
        target = _app_target(record)
        dependencies = self._build_dependencies(settings, target=target)
        return self._runtime_builder(
            provider=settings.workflow.app_runtime_provider,
            environment=settings.environment,
            dependencies=dependencies,
            lease_owner=self._worker_instance_id,
            workflow_settings=settings.workflow,
            official_client=self._official_client,
        )

    def _build_dependencies(self, settings: Settings, *, target) -> AppV2RuntimeDependencies:
        understanding_provider = self._understanding_provider
        if understanding_provider is None:
            understanding_provider = resolve_app_vision_provider(settings)
        artifact_store = build_evidence_artifact_store(settings.evidence_storage)
        page_graph_repository = PostgresPageGraphRepository()
        surface_sessions = PostgresSurfaceSessionRepository()
        evidence_manifests = PostgresEvidenceManifestRepository()
        evidence_indexes = PostgresAppEvidenceIndexRepository()
        exploration_results = PostgresAppExplorationResultRepository()
        result_intents = PostgresAppExplorationResultIntentRepository()
        runtime_recovery = PostgresAppExplorationRuntimeRecoveryRepository()
        supplemental_evidence_policy = resolve_supplemental_evidence_policy(
            enabled=settings.supplemental_evidence.enabled,
            lab_runtime_ready=self._lab_supplemental_ready,
        )
        evidence_requests = PostgresEvidenceRequestRepository()
        inspection_work_item_repository = PostgresInspectionWorkItemRepository()
        evidence_input_sets = PostgresEvidenceInputSetRepository()
        supplement_input_set_resealer = SupplementInputSetResealService(
            input_sets=evidence_input_sets,
            manifests=evidence_manifests,
            work_items=inspection_work_item_repository,
            requests=evidence_requests,
        )
        explorer_queue = PostgresExplorerWorkRepository(
            supplemental_evidence_policy=supplemental_evidence_policy
        )
        inspection_work_service = InspectionWorkItemService(
            repository=inspection_work_item_repository
        )
        live_inspection_repository = PostgresAppLiveInspectionRepository()
        live_inspection_publisher = AppLiveInspectionPublishService(
            capture_reader=live_inspection_repository,
            publisher=build_default_app_capture_inspection_publisher(
                work_item_service=inspection_work_service,
            ),
        )
        session_command_gate = PostgresSessionCommandGate(
            repository=surface_sessions,
            lease_owner=f"{self._worker_instance_id}:app-command",
            session_owner="app-workflow",
            platform_build_ref=target.app.platform_build_ref,
            device_ref=f"{target.app.device_backend}:{target.app.device_profile_id}",
        )
        # 命令范围必须在每次动作前从任务与活跃 Session 快照读取；不把
        # Agent 的风险提示或任务输入包名当作授权事实。
        command_scope_reader = PostgresTaskCommandScopeReader()
        exploration_orchestrator = QcOrchestrator(
            exploration_publisher=PostgresExplorationWorkPublisher(repository=explorer_queue),
            probe_publisher=PostgresProbeWorkPublisher(
                repository=explorer_queue,
                supplemental_evidence_policy=supplemental_evidence_policy,
            ),
        )
        return AppV2RuntimeDependencies(
            platform_builds=PostgresPlatformBuildRepository(),
            surface_sessions=surface_sessions,
            bootstraps=PostgresAppBootstrapRepository(),
            captures=AppCapturePersistenceService(
                repository=PostgresAppCaptureRepository(
                    page_graph_repository=page_graph_repository,
                ),
                artifact_store=artifact_store,
                manifest_repository=evidence_manifests,
                max_screenshot_bytes=settings.workflow.official_app_max_screenshot_bytes,
            ),
            # APP_READY 已通过 Surface Session 的事务 Outbox 落库；执行器同步消费该 handoff。
            app_ready_publisher=NoopAppReadyPublisher(),
            artifact_reader=artifact_store,
            understanding_provider=understanding_provider,
            visual_observations=PostgresAppVisualObservationRepository(
                page_graph_repository=page_graph_repository,
            ),
            exploration_orchestrator=exploration_orchestrator,
            exploration_queue=explorer_queue,
            app_frontier_queue=PostgresAppFrontierRepository(),
            explorer=SurfacePhaseAwareExplorer(
                delegate=build_default_explorer_agent(),
                repository=PostgresSurfacePhaseRepository(),
                session_command_gate=session_command_gate,
            ),
            # 离场预留时长与 Web 门禁共用配置：APP 关弹窗一轮常超 2 分钟，默认 120s 会把预留判过期
            evidence_exit_gate=PostgresEvidenceExitGate(
                reservation_seconds=settings.web_evidence_exit_gate.reservation_seconds,
            ),
            action_executions=PostgresActionExecutionRepository(
                page_graph_repository=page_graph_repository,
            ),
            session_command_gate=session_command_gate,
            active_session_inspection=AppActiveSessionInspectionDrainService(
                publisher=live_inspection_publisher,
                consumer=InspectionWorkItemConsumer(
                    service=inspection_work_service,
                    lease_owner=f"{self._worker_instance_id}:app-active-inspection",
                    lease_seconds=settings.inspection_work_items.lease_seconds,
                    retry_backoff_seconds=settings.inspection_work_items.retry_base_seconds,
                    reuse_planner=build_current_rule_reuse_planner(
                        enabled=rule_result_reuse_enabled(settings)
                    ),
                    result_writer=CurrentRuleDecisionWriter(
                        request_admission=EvidenceRequestAdmissionService(
                            repository=evidence_requests,
                            policy=supplemental_evidence_policy,
                        ),
                        supplemental_evidence_policy=supplemental_evidence_policy,
                    ),
                    supplemental_evidence_policy=supplemental_evidence_policy,
                ),
                state_reader=live_inspection_repository,
                # 排空窗口用尽时按证据不足结算再关会话，否则设备租约结算执行不到。
                closure_settlement=inspection_work_service,
            ),
            evidence_manifests=evidence_manifests,
            evidence_indexes=evidence_indexes,
            exploration_results=exploration_results,
            result_intents=result_intents,
            exploration_failures=PostgresAppExplorationFailureRepository(),
            runtime_recovery=runtime_recovery,
            supplemental_evidence_policy=supplemental_evidence_policy,
            cancellation=self._cancellation,
            # 任务输入包名可空或陈旧；动作边界只在 APP_READY 后绑定安装结果。
            allowed_packages=(),
            command_scope_reader=command_scope_reader,
            page_graph=page_graph_repository,
            targeted_recapture_contexts=PostgresAppTargetedRecaptureContextReader(),
            restoration_attempts=PostgresRestorationAttemptRepository(),
            probe_orchestrator=exploration_orchestrator,
            probe_completion=AppProbeCompletionService(
                requests=evidence_requests,
                manifests=evidence_manifests,
                attacher=EvidenceSupplementAttacher(
                    repository=PostgresEvidenceSupplementRepository(
                        supplemental_evidence_policy=supplemental_evidence_policy,
                    ),
                    supplemental_evidence_policy=supplemental_evidence_policy,
                ),
                recovery=PostgresAppProbeCompletionRecoveryReader(),
                manifest_finalizer=AppEvidenceManifestFinalizer(
                    repository=evidence_manifests,
                    artifact_store=artifact_store,
                ),
                input_set_resealer=supplement_input_set_resealer,
            ),
            evidence_requests=evidence_requests,
            inspection_work_items=inspection_work_service,
        )


class ProductionAppV2RuntimeExecutor:
    """将 Bootstrap、截图理解和完整 Explorer 消费链映射为统一 RunResult。"""

    def __init__(
        self,
        *,
        worker_instance_id: str,
        persistence_gate: AppExplorationPersistenceGate | None = None,
        now: Callable[[], datetime] | None = None,
        lab_supplemental_ready: bool = False,
    ) -> None:
        if not worker_instance_id or worker_instance_id != worker_instance_id.strip():
            raise ValueError("worker_instance_id 必须为非空且无首尾空白的字符串")
        self._worker_instance_id = worker_instance_id
        self._persistence_gate = persistence_gate
        self._now = now or (lambda: datetime.now(UTC))
        self._lab_supplemental_ready = bool(lab_supplemental_ready)

    async def run(
        self,
        runtime: AppV2Runtime,
        *,
        record: AgentRunRecord,
        settings: Settings,
        progress_recorder: object | None = None,
    ) -> RunResult:
        consumed_turns = 0
        outcome = AppExplorationTurnOutcome(claimed=False)
        closure = None
        checkpoint: AppExplorationRuntimeCheckpoint | None = None
        action_budget: _AppActionBudgetGuard | None = None
        target = _app_target(record)
        assert target.app is not None
        command = RunAppWorkflowCommand(
            task_id=record.task_id,
            agent_run_id=record.run_id,
            session_id=f"app:{record.task_id}",
            platform_build_ref=target.app.platform_build_ref,
            lease_owner=self._worker_instance_id,
            lease_token=str(uuid4()),
            device_ref=f"{target.app.device_backend}:{target.app.device_profile_id}",
            task_remark=_task_remark_from_config(record.task_config),
        )
        try:
            persisted_result = await _get_persisted_app_exploration_result(
                runtime,
                agent_run_id=record.run_id,
            )
            if persisted_result is not None:
                return _run_result_from_persisted_app_exploration_result(
                    persisted_result,
                    task_id=record.task_id,
                    agent_run_id=record.run_id,
                    session_id=command.session_id,
                )
            checkpoint_before_bootstrap = await _get_app_exploration_runtime_checkpoint(
                runtime,
                agent_run_id=record.run_id,
            )
            work_recovery_before_bootstrap = None
            if checkpoint_before_bootstrap is not None:
                work_recovery_before_bootstrap = await _get_app_exploration_work_recovery(
                    runtime,
                    task_id=record.task_id,
                    agent_run_id=record.run_id,
                    surface_session_id=command.session_id,
                )
            if (
                checkpoint_before_bootstrap is not None
                and work_recovery_before_bootstrap is not None
                and not work_recovery_before_bootstrap.has_any_work
                and self._now() >= checkpoint_before_bootstrap.deadline
            ):
                return await _close_expired_recovered_runtime(
                    runtime,
                    record=record,
                    command=command,
                    target=target,
                    checkpoint=checkpoint_before_bootstrap,
                    consumed_turns=0,
                    consumed_actions=0,
                )
            supplemental_evidence_policy = resolve_supplemental_evidence_policy(
                enabled=settings.supplemental_evidence.enabled,
                lab_runtime_ready=self._lab_supplemental_ready,
            )
            supplemental_evidence_policy.validate_runtime_ready()
            try:
                handoff = await runtime.bootstrap(command)
            except AppWorkflowFailure as exc:
                recovery_error_code = getattr(exc, "error_code", None)
                if checkpoint_before_bootstrap is not None and recovery_error_code in {
                    "APP_SESSION_REATTACH_REQUIRED",
                    "APP_SESSION_REATTACH_FAILED",
                }:
                    return await _close_unavailable_recovered_session(
                        runtime,
                        record=record,
                        command=command,
                        target=target,
                        checkpoint=checkpoint_before_bootstrap,
                        error_code=recovery_error_code,
                    )
                raise
            installed_package = _installed_package_from_handoff(handoff)
            journey_steps = _journey_steps_from_record(record)
            runtime_policy = build_dynamic_app_exploration_runtime_policy(
                task_config=record.task_config or {},
                loop_guard=record.loop_guard or {},
                page_sampling_policy=record.page_sampling_policy or {},
                journey_steps=journey_steps,
            )
            app_goal = build_app_exploration_goal(
                package_name=installed_package,
                journey_steps=journey_steps,
                fallback_milestones=(),
                minimum_business_pages=runtime_policy.minimum_business_pages,
                required_page_types=(
                    runtime_policy.app_target_snapshot.required_page_types
                    if runtime_policy.app_target_snapshot is not None
                    else ()
                ),
                required_milestones=(
                    runtime_policy.app_target_snapshot.required_milestones
                    if runtime_policy.app_target_snapshot is not None
                    else ()
                ),
                optional_page_types=(
                    runtime_policy.app_target_snapshot.optional_page_types
                    if runtime_policy.app_target_snapshot is not None
                    else ()
                ),
                lifecycle_page_types=(
                    runtime_policy.app_target_snapshot.lifecycle_page_types
                    if runtime_policy.app_target_snapshot is not None
                    else ()
                ),
                applicability_mode=(
                    runtime_policy.app_target_snapshot.applicability_mode
                    if runtime_policy.app_target_snapshot is not None
                    else None
                ),
            )
            objective = build_dynamic_app_exploration_objective(
                app_goal,
                completion_mode=(
                    runtime_policy.app_target_snapshot.completion_mode
                    if runtime_policy.app_target_snapshot is not None
                    else "explicit_targets"
                ),
            )
            if runtime_policy.app_target_snapshot is not None:
                runtime_policy.app_target_snapshot.require_device_capabilities(
                    handoff.capability_manifest
                )
                runtime_policy.app_target_snapshot.require_installed_package(installed_package)
            max_actions = runtime_policy.max_actions
            duration_ms = runtime_policy.duration_ms
            deadline_seconds = runtime_policy.deadline_seconds
            proposed_started_at = self._now()
            checkpoint = await _restore_or_create_runtime_checkpoint(
                runtime,
                task_id=record.task_id,
                agent_run_id=record.run_id,
                handoff=handoff,
                platform_build_ref=target.app.platform_build_ref,
                installed_package=installed_package,
                runtime_policy=runtime_policy,
                objective=objective,
                proposed_started_at=proposed_started_at,
                proposed_deadline=proposed_started_at + timedelta(seconds=deadline_seconds),
                existing=checkpoint_before_bootstrap,
            )
            exploration_started_at = checkpoint.started_at
            exploration_deadline = checkpoint.deadline
            work_recovery = work_recovery_before_bootstrap
            if work_recovery is None:
                work_recovery = await _get_app_exploration_work_recovery(
                    runtime,
                    task_id=record.task_id,
                    agent_run_id=record.run_id,
                    surface_session_id=command.session_id,
                )
            if work_recovery is not None and work_recovery.has_any_work:
                _validate_app_exploration_work_recovery(
                    recovery=work_recovery,
                    checkpoint=checkpoint,
                    handoff=handoff,
                    objective=objective,
                    installed_package=installed_package,
                    runtime_policy=runtime_policy,
                )
                consumed_turns = work_recovery.consumed_turn_count
                # 先让 PostgreSQL 把 deadline/attempt 已耗尽的 Work 转为显式终态。
                expire = getattr(runtime, "expire_unclaimable_exploration", None)
                if callable(expire):
                    await expire(task_id=record.task_id)
                    refreshed = await _get_app_exploration_work_recovery(
                        runtime,
                        task_id=record.task_id,
                        agent_run_id=record.run_id,
                        surface_session_id=command.session_id,
                    )
                    if refreshed is not None:
                        _validate_app_exploration_work_recovery(
                            recovery=refreshed,
                            checkpoint=checkpoint,
                            handoff=handoff,
                            objective=objective,
                            installed_package=installed_package,
                            runtime_policy=runtime_policy,
                        )
                        work_recovery = refreshed
                        consumed_turns = refreshed.consumed_turn_count
            else:
                work_recovery = None
            if await _runtime_cancel_requested(
                runtime,
                task_id=record.task_id,
                agent_run_id=record.run_id,
            ):
                return await _close_canceled_exploration(
                    runtime,
                    record=record,
                    command=command,
                    handoff=handoff,
                    target=target,
                    started_at=exploration_started_at,
                    deadline=exploration_deadline,
                    consumed_turns=consumed_turns,
                    runtime_policy=runtime_policy,
                )
            pending_recovery = work_recovery is not None and work_recovery.has_nonterminal_work
            recovery_next_claim_at = work_recovery.next_claim_at if pending_recovery else None
            if (work_recovery is None or pending_recovery) and (
                self._now() >= exploration_deadline
                or (
                    recovery_next_claim_at is not None
                    and recovery_next_claim_at >= exploration_deadline
                )
            ):
                return await _close_expired_recovered_runtime(
                    runtime,
                    record=record,
                    command=command,
                    target=target,
                    checkpoint=checkpoint,
                    consumed_turns=consumed_turns,
                    consumed_actions=(
                        work_recovery.consumed_action_count if work_recovery is not None else 0
                    ),
                )
            if recovery_next_claim_at is not None:
                await _wait_for_database_retry(
                    runtime,
                    task_id=record.task_id,
                    agent_run_id=record.run_id,
                    deadline=exploration_deadline,
                    next_claim_at=recovery_next_claim_at,
                )
                if await _runtime_cancel_requested(
                    runtime,
                    task_id=record.task_id,
                    agent_run_id=record.run_id,
                ):
                    return await _close_canceled_exploration(
                        runtime,
                        record=record,
                        command=command,
                        handoff=handoff,
                        target=target,
                        started_at=exploration_started_at,
                        deadline=exploration_deadline,
                        consumed_turns=consumed_turns,
                        runtime_policy=runtime_policy,
                    )
            if work_recovery is None:
                await runtime.publish_exploration(
                    AppScreenshotExplorationCommand(
                        handoff=handoff,
                        objective=objective,
                        boundaries=ExplorationBoundaries(
                            allowed_hosts=(installed_package,),
                            allowed_operations=(
                                InteractionOperation.CLICK,
                                InteractionOperation.SCROLL,
                                InteractionOperation.BACK,
                                InteractionOperation.OBSERVE,
                                InteractionOperation.WARM_RESTORE,
                            ),
                            max_navigation_depth=runtime_policy.max_navigation_depth,
                            side_effect_policy=SideEffectPolicy.READ_ONLY,
                        ),
                        budget=build_split_exploration_budget(
                            max_actions=max_actions,
                            max_depth=runtime_policy.max_navigation_depth,
                            duration_ms=duration_ms,
                            required_action_count=runtime_policy.required_action_count,
                        ),
                        observed_at=exploration_started_at,
                        deadline=exploration_deadline,
                        app_goal=app_goal,
                        curriculum_progress=None,
                        runtime_policy_summary=runtime_policy.to_dict(),
                    )
                )
                _notify_exploration_published(
                    progress_recorder,
                    objective=objective,
                    max_actions=max_actions,
                    deadline_seconds=deadline_seconds,
                )
            action_budget = _AppActionBudgetGuard(
                max_actions=max_actions,
                consumed_actions=(
                    work_recovery.consumed_action_count if work_recovery is not None else 0
                ),
            )
            if work_recovery is not None and work_recovery.terminal is not None:
                outcome = _turn_outcome_from_persisted_work(work_recovery)
            while action_budget.can_claim:
                if outcome.turn is not None and outcome.turn.is_terminal:
                    break
                if await _runtime_cancel_requested(
                    runtime,
                    task_id=record.task_id,
                    agent_run_id=record.run_id,
                ):
                    return await _close_canceled_exploration(
                        runtime,
                        record=record,
                        command=command,
                        handoff=handoff,
                        target=target,
                        started_at=exploration_started_at,
                        deadline=exploration_deadline,
                        consumed_turns=consumed_turns,
                        runtime_policy=runtime_policy,
                    )
                turn_index = consumed_turns + 1
                inspection_checkpoint = getattr(runtime, "inspect_current_app_surface", None)
                if callable(inspection_checkpoint):
                    await inspection_checkpoint(
                        task_id=record.task_id,
                        agent_run_id=record.run_id,
                        deadline=exploration_deadline,
                    )
                _notify_turn_started(
                    progress_recorder,
                    turn_index=turn_index,
                    work_type="exploration",
                )
                outcome = await runtime.run_one_turn(
                    task_id=record.task_id,
                    agent_run_id=record.run_id,
                )
                _notify_turn_finished(
                    progress_recorder,
                    turn_index=turn_index,
                    work_type="exploration",
                    outcome=outcome,
                )
                if outcome.claimed and outcome.final_status is not ExplorerWorkStatus.RETRY_WAIT:
                    consumed_turns += 1
                    action_budget.record(outcome)
                if outcome.final_status is ExplorerWorkStatus.CANCELLED:
                    return await _close_canceled_exploration(
                        runtime,
                        record=record,
                        command=command,
                        handoff=handoff,
                        target=target,
                        started_at=exploration_started_at,
                        deadline=exploration_deadline,
                        consumed_turns=consumed_turns,
                        runtime_policy=runtime_policy,
                    )
                if await _runtime_cancel_requested(
                    runtime,
                    task_id=record.task_id,
                    agent_run_id=record.run_id,
                ):
                    return await _close_canceled_exploration(
                        runtime,
                        record=record,
                        command=command,
                        handoff=handoff,
                        target=target,
                        started_at=exploration_started_at,
                        deadline=exploration_deadline,
                        consumed_turns=consumed_turns,
                        runtime_policy=runtime_policy,
                    )
                # retry_wait 的可重领时间和尝试耗尽均以 PostgreSQL 为事实源。
                # 进程只等待数据库给出的时间，Worker 崩溃后其他实例仍可按过期租约恢复。
                if outcome.final_status is ExplorerWorkStatus.RETRY_WAIT:
                    can_retry = await _wait_for_database_retry(
                        runtime,
                        task_id=record.task_id,
                        agent_run_id=record.run_id,
                        deadline=exploration_deadline,
                    )
                    if not can_retry:
                        expire = getattr(runtime, "expire_unclaimable_exploration", None)
                        if callable(expire):
                            await expire(task_id=record.task_id)
                        break
                    continue
                # Worker 中断后动作结果未知：不关 session、不取消剩余 Work。
                # 下一轮重新观察前台；若已在浏览器／下载弹窗则走 Cancel／包边界 BACK。
                if _is_after_crash_continue(outcome):
                    continue
                if _is_publication_snapshot_mismatch(outcome.error_code):
                    continue
                if not _requires_follow_up_turn(outcome):
                    break
            terminal_decision = _terminal_decision(outcome)
            business_outcome = _business_outcome(terminal_decision)
            if business_outcome is not None:
                # 业务终态已经冻结后，剩余 Global Frontier 不再允许执行。必须先把
                # 它们收敛为可审计终态，否则 Surface close readiness 会把这些
                # 永远不会再领取的 pending sibling 当成在途动作，导致设备会话丢失。
                cancel_work = getattr(runtime, "cancel_exploration_work", None)
                if callable(cancel_work):
                    await cancel_work(task_id=record.task_id)
                if self._persistence_gate is not None:
                    cutoff = await self._persistence_gate.cutoff(
                        task_id=record.task_id,
                        session_id=command.session_id,
                        runtime=runtime,
                        agent_run_id=record.run_id,
                    )
                    return _attach_persistence_gate(
                        _turn_result(outcome, step_count=consumed_turns),
                        cutoff,
                    )
                # 关闭前重新读回本轮结算后的预算，不能复用启动时的恢复快照。
                closing_recovery = await _get_app_exploration_work_recovery(
                    runtime,
                    task_id=record.task_id,
                    agent_run_id=record.run_id,
                    surface_session_id=command.session_id,
                )
                closure = await runtime.close_exploration(
                    CloseAppExplorationCommand(
                        task_id=record.task_id,
                        agent_run_id=record.run_id,
                        session_id=command.session_id,
                        build_id=target.app.platform_build_ref,
                        surface_kind=handoff.capture.surface_kind,
                        outcome=business_outcome,
                        terminal_reason_code=(
                            terminal_decision.reason_code
                            if terminal_decision is not None
                            else "terminal_reason_missing"
                        ),
                        coverage_summary={
                            **(
                                dict(terminal_decision.coverage_summary)
                                if terminal_decision is not None
                                else {}
                            ),
                            "consumed_turn_count": consumed_turns,
                            "consumed_action_count": action_budget.consumed_actions,
                            "terminal_status": (
                                terminal_decision.status.value
                                if terminal_decision is not None
                                else None
                            ),
                            "app_runtime_policy": runtime_policy.to_dict(),
                        },
                        started_at=exploration_started_at,
                        deadline=exploration_deadline,
                        supplemental_evidence_budget=_build_supplemental_evidence_budget(
                            max_actions=max_actions,
                            consumed_actions=action_budget.consumed_actions,
                            max_depth=runtime_policy.max_navigation_depth,
                            deadline=exploration_deadline,
                            now=self._now(),
                            persisted_budget=(
                                closing_recovery.latest_work.budget
                                if closing_recovery is not None
                                and closing_recovery.latest_work is not None
                                else None
                            ),
                        ),
                    )
                )
                if closure.close_error is not None:
                    return _attach_exploration_closure(
                        RunResult(
                            termination_reason=TerminationReason.INCOMPLETE,
                            working_memory=WorkingMemory(step_count=consumed_turns),
                            trace_sink=InMemoryActionTraceSink(),
                            final_text="App 探索证据已封存，但设备会话关闭失败",
                            error_message=f"App Session 关闭失败：{closure.close_error}",
                            error_category=ErrorCategory.INFRASTRUCTURE,
                            execution_outcome=ExecutionOutcome.NEEDS_REVIEW,
                        ),
                        closure,
                    )
                if (
                    getattr(
                        getattr(closure, "exploration_result", None),
                        "outcome",
                        None,
                    )
                    is AppExplorationResultOutcome.EVIDENCE_INSUFFICIENT
                ):
                    return RunResult(
                        termination_reason=TerminationReason.INCOMPLETE,
                        working_memory=WorkingMemory(step_count=consumed_turns),
                        trace_sink=InMemoryActionTraceSink(),
                        final_text="App 探索已封存，但最终证据不足",
                        error_message=getattr(
                            closure.exploration_result,
                            "terminal_reason_code",
                            "evidence_incomplete",
                        ),
                        execution_outcome=ExecutionOutcome.NEEDS_REVIEW,
                    )
            elif _is_technical_outcome(outcome, terminal_decision=terminal_decision):
                if _is_publication_snapshot_mismatch(
                    technical_reason := (
                        terminal_decision.reason_code
                        if terminal_decision is not None
                        and terminal_decision.reason_code is not None
                        else outcome.error_code or "app_exploration_technical_failed"
                    ),
                    outcome.error_code,
                ):
                    raise AppPublicationSnapshotMismatch()
                if _is_action_recovery_block(outcome):
                    return await _close_action_recovery_block(
                        runtime,
                        record=record,
                        command=command,
                        handoff=handoff,
                        target=target,
                        started_at=exploration_started_at,
                        deadline=exploration_deadline,
                        consumed_turns=consumed_turns,
                        runtime_policy=runtime_policy,
                        error_code=outcome.error_code or "APP_ACTION_RECOVERY_BLOCKED",
                    )
                await _record_technical_failure(
                    runtime,
                    record=record,
                    session_id=command.session_id,
                    phase="exploration",
                    reason_code=technical_reason,
                    error_type="AppExplorationTechnicalOutcome",
                    retryable=False,
                    observed_at=self._now(),
                    progress_recorder=progress_recorder,
                    error_message=outcome.error_code,
                    consumed_turns=consumed_turns,
                    consumed_actions=(
                        action_budget.consumed_actions if action_budget is not None else None
                    ),
                )
                sealed_failure = await _seal_technical_failure_if_possible(
                    runtime,
                    record=record,
                    command=command,
                    target=target,
                    checkpoint=checkpoint,
                    reason_code=technical_reason,
                    consumed_turns=consumed_turns,
                    consumed_actions=(
                        action_budget.consumed_actions if action_budget is not None else 0
                    ),
                    coverage_summary=(
                        dict(terminal_decision.coverage_summary)
                        if terminal_decision is not None
                        else {}
                    ),
                )
                if sealed_failure is not None:
                    return sealed_failure
                await _mark_runtime_lost(
                    runtime,
                    task_id=record.task_id,
                    session_id=command.session_id,
                    expected_surface_kind=target.surface_kind,
                    expected_build_id=target.app.platform_build_ref,
                    reason=technical_reason,
                )
                return _turn_result(outcome, step_count=consumed_turns)
        except asyncio.CancelledError as exc:
            if not getattr(exc, "app_closure_cleanup_handled", False):
                await asyncio.shield(
                    _mark_runtime_lost(
                        runtime,
                        task_id=record.task_id,
                        session_id=command.session_id,
                        expected_surface_kind=target.surface_kind,
                        expected_build_id=target.app.platform_build_ref,
                        reason="runtime_cancelled",
                    )
                )
            raise
        except AppWorkflowFailure as exc:
            canceled = getattr(exc, "error_code", None) == "WORKFLOW_CANCELED"
            if not canceled:
                await _record_technical_failure(
                    runtime,
                    record=record,
                    session_id=command.session_id,
                    phase="bootstrap",
                    reason_code=getattr(exc, "error_code", None) or type(exc).__name__,
                    error_type=type(exc).__name__,
                    retryable=bool(getattr(exc, "retryable", False)),
                    observed_at=self._now(),
                    progress_recorder=progress_recorder,
                    error_message=str(exc),
                    consumed_turns=consumed_turns,
                )
            await _mark_runtime_lost(
                runtime,
                task_id=record.task_id,
                session_id=command.session_id,
                expected_surface_kind=target.surface_kind,
                expected_build_id=target.app.platform_build_ref,
                reason=getattr(exc, "error_code", None) or type(exc).__name__,
            )
            return _failed_result(
                message=str(exc),
                canceled=canceled,
                step_count=consumed_turns,
            )
        except AppExplorationPolicyError as exc:
            await _record_technical_failure(
                runtime,
                record=record,
                session_id=command.session_id,
                phase="policy",
                reason_code=exc.error_code,
                error_type=type(exc).__name__,
                retryable=False,
                observed_at=self._now(),
                progress_recorder=progress_recorder,
                error_message=str(exc),
                consumed_turns=consumed_turns,
            )
            await _mark_runtime_lost(
                runtime,
                task_id=record.task_id,
                session_id=command.session_id,
                expected_surface_kind=target.surface_kind,
                expected_build_id=target.app.platform_build_ref,
                reason=exc.error_code,
            )
            return _failed_result(
                message=str(exc),
                canceled=False,
                step_count=consumed_turns,
            )
        except AppPublicationSnapshotMismatch:
            raise
        except Exception as exc:  # noqa: BLE001 - 生产组合根必须把外部失败收敛为统一 RunResult
            reason_code = type(exc).__name__
            await _record_technical_failure(
                runtime,
                record=record,
                session_id=command.session_id,
                phase="runtime",
                reason_code=reason_code,
                error_type=type(exc).__name__,
                retryable=bool(getattr(exc, "retryable", False)),
                observed_at=self._now(),
                progress_recorder=progress_recorder,
                error_message=str(exc),
                consumed_turns=consumed_turns,
                consumed_actions=(
                    action_budget.consumed_actions if action_budget is not None else None
                ),
            )
            sealed_failure = await _seal_technical_failure_if_possible(
                runtime,
                record=record,
                command=command,
                target=target,
                checkpoint=checkpoint,
                reason_code=reason_code,
                consumed_turns=consumed_turns,
                consumed_actions=(
                    action_budget.consumed_actions if action_budget is not None else 0
                ),
            )
            if sealed_failure is not None:
                sealed_failure.error_message = str(exc)
                return sealed_failure
            await _mark_runtime_lost(
                runtime,
                task_id=record.task_id,
                session_id=command.session_id,
                expected_surface_kind=target.surface_kind,
                expected_build_id=target.app.platform_build_ref,
                reason=type(exc).__name__,
            )
            return _failed_result(
                message=str(exc),
                canceled=False,
                step_count=consumed_turns,
            )
        result = _turn_result(outcome, step_count=consumed_turns)
        if closure is not None:
            return _attach_exploration_closure(result, closure)
        return result


def _attach_exploration_closure(result: RunResult, closure: object) -> RunResult:
    """把封存截止结论挂到 RunResult，供上层持久化探索状态。"""
    exploration_result = getattr(closure, "exploration_result", None)
    outcome = getattr(exploration_result, "outcome", None)
    result.exploration_status = (
        "close_failed"
        if getattr(closure, "close_error", None) is not None
        else getattr(outcome, "value", None)
    )
    index = getattr(closure, "index", None)
    result.evidence_index_id = getattr(index, "index_id", None)
    gaps = getattr(exploration_result, "evidence_gaps", ())
    result.evidence_gap = tuple(
        str(getattr(item, "description", None) or getattr(item, "reason_code", item))
        for item in gaps
    )
    return result


def _attach_persistence_gate(
    result: RunResult,
    decision: ExplorationCutoffDecision,
) -> RunResult:
    """把显式注入的探索证据门禁结论映射到统一 RunResult。"""

    index = decision.index
    result.exploration_status = decision.status
    result.evidence_index_id = index.index_id if index is not None else None
    gaps = decision.gaps or (index.evidence_gap if index is not None else ())
    result.evidence_gap = tuple(item.detail for item in gaps)
    result.page_timeline = decision.page_timeline or (
        index.page_timeline if index is not None else ()
    )
    if decision.status == ExplorationEvidenceStatus.EVIDENCE_INSUFFICIENT.value:
        result.termination_reason = TerminationReason.INCOMPLETE
        result.execution_outcome = ExecutionOutcome.NEEDS_REVIEW
        result.final_text = "App 探索已封存，但最终证据不足"
        result.error_message = (
            gaps[0].code.value if gaps else "app_exploration_evidence_insufficient"
        )
    elif index is None:
        result.termination_reason = TerminationReason.INCOMPLETE
        result.execution_outcome = ExecutionOutcome.NEEDS_REVIEW
        result.final_text = "App 探索证据门禁尚未形成封存索引"
        result.error_message = "app_exploration_index_missing"
    return result


def build_app_workflow_handler(
    settings: Settings,
    worker_instance_id: str,
    official_client: OfficialAppApiClient | None = None,
    *,
    queue: RedisAgentRunQueue | None = None,
    state_store: AgentRunStateStore | None = None,
    understanding_provider: ScreenshotUnderstandingProvider | None = None,
    lab_supplemental_ready: bool | None = None,
    lab_executes_probe: bool | None = None,
) -> AppWorkflowHandler:
    """构建 Worker 生命周期内复用的唯一 App Workflow 入口。

    刀 B：``lab_executes_probe``（或兼容别名 ``lab_supplemental_ready``）为 None 时
    跟随 ``ALIOTH_APP_LAB_EXECUTES_PROBE``（且须 Lab M6/M7）。默认真 False，
    **不会**仅因 ``ALIOTH_APP_LAB_MILESTONE=M6|M7`` 打开关闭前排空／Probe。
    """
    if lab_executes_probe is None and lab_supplemental_ready is None:
        from services.api.app.application.app_lab_composition import (
            resolve_app_lab_composition,
        )

        lab_executes_probe = bool(
            resolve_app_lab_composition().get("lab_executes_probe")
        )
    elif lab_executes_probe is None:
        lab_executes_probe = bool(lab_supplemental_ready)
    else:
        lab_executes_probe = bool(lab_executes_probe)
    from services.api.app.application.app_bounded_exploration_milestones import (
        resolve_production_m6_supplemental_ready,
    )

    # P1-09：生产 M6 authority 也可声明 runtime_ready（≠ Lab 刀 B；≠自动 Probe）。
    production_m6_ready = resolve_production_m6_supplemental_ready(explicit=None)
    supplemental_runtime_ready = bool(lab_executes_probe or production_m6_ready)
    resolve_supplemental_evidence_policy(
        enabled=settings.supplemental_evidence.enabled,
        lab_runtime_ready=supplemental_runtime_ready,
    ).validate_runtime_ready()
    settings.workflow.validate_runtime_environment(settings.environment)
    if settings.workflow.app_runtime_provider == "official" and official_client is None:
        official_client = HttpOfficialAppApiClient(settings=settings.workflow)
    cancellation = (
        QueueAppWorkflowCancellation(queue, durable_source=state_store)
        if queue is not None
        else None
    )
    provider = PostgresAppRuntimeProvider(
        worker_instance_id=worker_instance_id,
        official_client=official_client,
        cancellation=cancellation,
        understanding_provider=understanding_provider,
        lab_supplemental_ready=supplemental_runtime_ready,
    )
    return RuntimeBackedAppWorkflowHandler(
        provider=provider,
        executor=ProductionAppV2RuntimeExecutor(
            worker_instance_id=worker_instance_id,
            lab_supplemental_ready=supplemental_runtime_ready,
        ),
    )


def _app_target(record: AgentRunRecord):
    payload = (record.task_config or {}).get("_task_target")
    target = task_target_from_dict(payload)
    if target.platform is not TaskTargetPlatform.APP or target.app is None:
        raise ValueError("App Runtime 只接受已锁定的 App 任务目标")
    return target


def _task_remark_from_config(task_config: object | None) -> str | None:
    """从任务配置读取备注，供同名降版扫描授权贯通到 Official Session。"""
    if not isinstance(task_config, dict):
        return None
    raw = task_config.get("remark")
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    return text or None


def _journey_steps_from_record(record: AgentRunRecord) -> list[str]:
    raw = (record.task_config or {}).get("journey_steps")
    if not isinstance(raw, list):
        return []
    return [str(item).strip() for item in raw if str(item).strip()]


async def _get_persisted_app_exploration_result(
    runtime: AppV2Runtime,
    *,
    agent_run_id: str,
):
    getter = getattr(runtime, "get_persisted_exploration_result", None)
    if not callable(getter):
        return None
    return await getter(agent_run_id=agent_run_id)


async def _get_app_exploration_runtime_checkpoint(
    runtime: AppV2Runtime,
    *,
    agent_run_id: str,
) -> AppExplorationRuntimeCheckpoint | None:
    getter = getattr(runtime, "get_exploration_runtime_checkpoint", None)
    if not callable(getter):
        return None
    return await getter(agent_run_id=agent_run_id)


def _run_result_from_persisted_app_exploration_result(
    result,
    *,
    task_id: str,
    agent_run_id: str,
    session_id: str,
) -> RunResult:
    """已封存 Result 是唯一终态；恢复不得再连接设备或调用 Explorer。"""

    if (
        result.task_id != task_id
        or result.run_id != agent_run_id
        or result.surface_session_id != session_id
    ):
        raise RuntimeError("持久化 App Exploration Result 与 Run 身份不一致")
    raw_steps = result.coverage_summary.get("consumed_turn_count", 0)
    step_count = (
        raw_steps
        if isinstance(raw_steps, int) and not isinstance(raw_steps, bool) and raw_steps >= 0
        else 0
    )
    if result.outcome is AppExplorationResultOutcome.EXPLORATION_COMPLETED:
        return RunResult(
            termination_reason=TerminationReason.COMPLETED,
            working_memory=WorkingMemory(step_count=step_count),
            trace_sink=InMemoryActionTraceSink(),
            final_text="App 探索已从 PostgreSQL 封存结果恢复完成",
            execution_outcome=ExecutionOutcome.SUCCESS,
        )
    return RunResult(
        termination_reason=TerminationReason.INCOMPLETE,
        working_memory=WorkingMemory(step_count=step_count),
        trace_sink=InMemoryActionTraceSink(),
        final_text="App 探索已从 PostgreSQL 封存结果恢复，证据仍需复核",
        error_message=result.terminal_reason_code,
        execution_outcome=ExecutionOutcome.NEEDS_REVIEW,
    )


async def _restore_or_create_runtime_checkpoint(
    runtime: AppV2Runtime,
    *,
    task_id: str,
    agent_run_id: str,
    handoff: AppReadyHandoff,
    platform_build_ref: str,
    installed_package: str,
    runtime_policy: AppExplorationRuntimePolicy,
    objective: str,
    proposed_started_at: datetime,
    proposed_deadline: datetime,
    existing: AppExplorationRuntimeCheckpoint | None = None,
) -> AppExplorationRuntimeCheckpoint:
    """首次发布前冻结时间线；重启只能复用，不能重置预算窗口。"""

    checkpoint = existing
    if checkpoint is None:
        checkpoint = await _get_app_exploration_runtime_checkpoint(
            runtime,
            agent_run_id=agent_run_id,
        )
    if checkpoint is None:
        checkpoint = build_app_exploration_runtime_checkpoint(
            task_id=task_id,
            agent_run_id=agent_run_id,
            surface_session_id=handoff.session_id,
            platform_build_ref=platform_build_ref,
            bootstrap_capture_id=handoff.capture.capture_id,
            installed_package=installed_package,
            runtime_policy=runtime_policy.to_dict(),
            objective=objective,
            started_at=proposed_started_at,
            deadline=proposed_deadline,
            created_at=proposed_started_at,
        )
        creator = getattr(runtime, "create_exploration_runtime_checkpoint", None)
        if callable(creator):
            checkpoint = await creator(checkpoint=checkpoint)
    checkpoint.validate_runtime_identity(
        task_id=task_id,
        agent_run_id=agent_run_id,
        surface_session_id=handoff.session_id,
        platform_build_ref=platform_build_ref,
        bootstrap_capture_id=handoff.capture.capture_id,
        installed_package=installed_package,
        runtime_policy=runtime_policy.to_dict(),
        objective=objective,
    )
    return checkpoint


async def _get_app_exploration_work_recovery(
    runtime: AppV2Runtime,
    *,
    task_id: str,
    agent_run_id: str,
    surface_session_id: str,
) -> AppExplorationWorkRecovery | None:
    getter = getattr(runtime, "get_exploration_work_recovery", None)
    if not callable(getter):
        return None
    return await getter(
        task_id=task_id,
        agent_run_id=agent_run_id,
        surface_session_id=surface_session_id,
    )


async def _latest_persisted_coverage_summary(
    runtime: AppV2Runtime,
    *,
    task_id: str,
    agent_run_id: str,
    surface_session_id: str,
    deadline: datetime,
) -> dict[str, object]:
    """技术收口时从最后持久化 Work 重建覆盖，避免异常分支把事实写成空。"""

    try:
        recovery = await _get_app_exploration_work_recovery(
            runtime,
            task_id=task_id,
            agent_run_id=agent_run_id,
            surface_session_id=surface_session_id,
        )
    except Exception:  # noqa: BLE001 - 覆盖回读失败不能阻断已有证据的尽力封存
        logger.exception(
            "App 技术收口回读最后 Work 失败 task_id=%s run_id=%s",
            task_id,
            agent_run_id,
        )
        return {"coverage_snapshot_unavailable": True}
    latest = recovery.latest_work if recovery is not None else None
    if latest is None:
        return {"coverage_snapshot_unavailable": True}
    if (
        latest.task_id != task_id
        or latest.session_id != surface_session_id
        or latest.deadline != deadline
    ):
        logger.error(
            "App 技术收口拒绝身份漂移的最后 Work task_id=%s run_id=%s",
            task_id,
            agent_run_id,
        )
        return {"coverage_snapshot_unavailable": True}
    evaluation = AppExplorationTerminalEvaluator(
        now=lambda: min(datetime.now(UTC), latest.deadline)
    ).evaluate(
        latest,
        eligible_candidate_count=count_eligible_app_candidates(latest),
    )
    return {
        **evaluation.coverage.to_dict(),
        "coverage_snapshot_source": "latest_persisted_work",
    }


def _validate_app_exploration_work_recovery(
    *,
    recovery: AppExplorationWorkRecovery,
    checkpoint: AppExplorationRuntimeCheckpoint,
    handoff: AppReadyHandoff,
    objective: str,
    installed_package: str,
    runtime_policy: AppExplorationRuntimePolicy,
) -> None:
    """恢复的 Work 必须与已冻结 Runtime 身份、预算和 deadline 完全相容。"""

    initial = recovery.initial_work
    latest = recovery.latest_work
    if initial is None or latest is None:
        raise RuntimeError("App Work 恢复事实首尾不完整")
    for work in (initial, latest):
        if (
            work.task_id != checkpoint.task_id
            or work.session_id != checkpoint.surface_session_id
            or work.objective != objective
            or work.objective_hash != checkpoint.objective_hash
            or work.deadline != checkpoint.deadline
            or work.surface_kind is not handoff.capture.surface_kind
            or work.budget.max_actions != runtime_policy.max_actions
            or work.budget.max_depth != runtime_policy.max_navigation_depth
            or tuple(work.boundaries.allowed_hosts) != (installed_package,)
            or work.boundaries.max_navigation_depth != runtime_policy.max_navigation_depth
        ):
            raise RuntimeError("持久化 App Work 与 Runtime Checkpoint 身份或预算不一致")
    origin_mismatches: list[str] = []
    if initial.source_capture_id != handoff.capture.capture_id:
        origin_mismatches.append("source_capture_id")
    if initial.source_capture_id != checkpoint.bootstrap_capture_id:
        origin_mismatches.append("bootstrap_capture_id")
    if initial.frontier.observation.observed_at != checkpoint.started_at:
        origin_mismatches.append("observed_at")
    if initial.budget.remaining_actions != runtime_policy.max_actions:
        origin_mismatches.append("remaining_actions")
    if initial.budget.remaining_duration_ms != runtime_policy.duration_ms:
        origin_mismatches.append("remaining_duration_ms")
    if latest.budget.remaining_actions > initial.budget.remaining_actions:
        origin_mismatches.append("budget_regression")
    if origin_mismatches:
        raise RuntimeError(
            "持久化 App Work 的起点或预算消费事实不一致:"
            + ",".join(origin_mismatches)
        )
    budget_consumed = runtime_policy.max_actions - latest.budget.remaining_actions
    terminal_has_explicit_count = (
        recovery.terminal is not None
        and "consumed_action_count" in recovery.terminal.coverage_summary
    )
    if terminal_has_explicit_count:
        terminal_count = recovery.terminal.coverage_summary["consumed_action_count"]
        if (
            isinstance(terminal_count, bool)
            or not isinstance(terminal_count, int)
            or terminal_count != recovery.consumed_action_count
            or not budget_consumed
            <= terminal_count
            <= min(runtime_policy.max_actions, budget_consumed + 1)
        ):
            raise RuntimeError(
                "持久化 App Work 的起点或预算消费事实不一致:terminal_consumed_action_count"
            )
    elif recovery.consumed_action_count != budget_consumed:
        raise RuntimeError(
            "持久化 App Work 的起点或预算消费事实不一致:consumed_action_count"
        )
    # 清障后可一次性补足 business_duration_floor；此时 remaining_duration_ms 允许回升，
    # 但不得超过 max(起点剩余, 地板值)。
    if latest.budget.remaining_duration_ms > initial.budget.remaining_duration_ms:
        floor_ms = int(latest.budget.business_duration_floor_ms or 0)
        floor_applied = int(latest.budget.business_duration_floor_applied or 0)
        allowed_ceiling = max(initial.budget.remaining_duration_ms, floor_ms)
        if not (
            floor_applied == 1
            and floor_ms > 0
            and latest.budget.remaining_duration_ms <= allowed_ceiling
        ):
            raise RuntimeError(
                "持久化 App Work 的起点或预算消费事实不一致:duration_floor"
            )
    if recovery.terminal is not None and recovery.terminal.work_id != latest.work_id:
        raise RuntimeError("持久化 App Terminal 不是当前 Work 时间线的末端")


def _turn_outcome_from_persisted_work(
    recovery: AppExplorationWorkRecovery,
) -> AppExplorationTurnOutcome:
    terminal = recovery.terminal
    latest = recovery.latest_work
    if terminal is None or latest is None or recovery.terminal_status is None:
        raise RuntimeError("持久化 Explorer 终态事实不完整")
    return AppExplorationTurnOutcome(
        claimed=True,
        work_id=latest.work_id,
        final_status=recovery.terminal_status,
        turn=ExplorerTurnResult(
            work_id=latest.work_id,
            task_id=latest.task_id,
            decision=terminal,
            coordination=None,
        ),
        error_code=recovery.terminal_error_code or terminal.reason_code,
    )


async def _close_unavailable_recovered_session(
    runtime: AppV2Runtime,
    *,
    record: AgentRunRecord,
    command: RunAppWorkflowCommand,
    target: TaskTarget,
    checkpoint: AppExplorationRuntimeCheckpoint,
    error_code: str,
) -> RunResult:
    """设备会话已不可恢复时，以 Checkpoint 和既有 Capture 封存证据不足。"""

    assert target.app is not None
    if (
        checkpoint.task_id != record.task_id
        or checkpoint.agent_run_id != record.run_id
        or checkpoint.surface_session_id != command.session_id
        or checkpoint.platform_build_ref != target.app.platform_build_ref
    ):
        raise RuntimeError("不可恢复 Session 与 Runtime Checkpoint 身份不一致")
    record_app_metric_safely(
        APP_EXPLORATION_STALE_SESSION_TOTAL,
        reason_code=error_code,
        surface_kind=target.surface_kind.value,
        outcome="evidence_insufficient",
    )
    cancel_work = getattr(runtime, "cancel_exploration_work", None)
    if callable(cancel_work):
        await cancel_work(task_id=record.task_id)
    closure = await runtime.close_exploration(
        CloseAppExplorationCommand(
            task_id=record.task_id,
            agent_run_id=record.run_id,
            session_id=command.session_id,
            build_id=target.app.platform_build_ref,
            surface_kind=target.surface_kind,
            outcome=AppExplorationResultOutcome.EVIDENCE_INSUFFICIENT,
            terminal_reason_code=error_code,
            coverage_summary={
                "session_recovery_unavailable": True,
                "runtime_checkpoint_protocol": checkpoint.protocol_version,
                "app_runtime_policy": dict(checkpoint.runtime_policy),
            },
            started_at=checkpoint.started_at,
            deadline=checkpoint.deadline,
            capture_final_observation=False,
            run_evidence_gap_reason_code=error_code,
        )
    )
    return RunResult(
        termination_reason=TerminationReason.INCOMPLETE,
        working_memory=WorkingMemory(step_count=0),
        trace_sink=InMemoryActionTraceSink(),
        final_text="App 设备会话不可恢复，已有探索证据已封存",
        error_message=(
            f"{error_code}；{closure.close_error}"
            if closure.close_error is not None
            else error_code
        ),
        error_category=ErrorCategory.INFRASTRUCTURE,
        execution_outcome=ExecutionOutcome.NEEDS_REVIEW,
    )


async def _close_expired_recovered_runtime(
    runtime: AppV2Runtime,
    *,
    record: AgentRunRecord,
    command: RunAppWorkflowCommand,
    target: TaskTarget,
    checkpoint: AppExplorationRuntimeCheckpoint,
    consumed_turns: int,
    consumed_actions: int,
) -> RunResult:
    """原时间窗口已耗尽时停止 Work，并按证据不足封存当前 App 事实。"""

    assert target.app is not None
    if (
        checkpoint.task_id != record.task_id
        or checkpoint.agent_run_id != record.run_id
        or checkpoint.surface_session_id != command.session_id
        or checkpoint.platform_build_ref != target.app.platform_build_ref
    ):
        raise RuntimeError("过期 Runtime Checkpoint 与恢复请求身份不一致")
    error_code = "APP_EXPLORATION_RUNTIME_DEADLINE_EXCEEDED"
    cancel_work = getattr(runtime, "cancel_exploration_work", None)
    if callable(cancel_work):
        await cancel_work(task_id=record.task_id)
    closure = await runtime.close_exploration(
        CloseAppExplorationCommand(
            task_id=record.task_id,
            agent_run_id=record.run_id,
            session_id=command.session_id,
            build_id=target.app.platform_build_ref,
            surface_kind=target.surface_kind,
            outcome=AppExplorationResultOutcome.EVIDENCE_INSUFFICIENT,
            terminal_reason_code=error_code,
            coverage_summary={
                "consumed_turn_count": consumed_turns,
                "consumed_action_count": consumed_actions,
                "runtime_deadline_exceeded": True,
                "runtime_checkpoint_protocol": checkpoint.protocol_version,
                "app_runtime_policy": dict(checkpoint.runtime_policy),
            },
            started_at=checkpoint.started_at,
            deadline=checkpoint.deadline,
            capture_final_observation=False,
            run_evidence_gap_reason_code=error_code,
        )
    )
    message = "App 探索原始时间窗口已耗尽，已有证据已按证据不足封存"
    if closure.close_error is not None:
        message = f"{message}；设备会话关闭失败：{closure.close_error}"
    return RunResult(
        termination_reason=TerminationReason.INCOMPLETE,
        working_memory=WorkingMemory(step_count=consumed_turns),
        trace_sink=InMemoryActionTraceSink(),
        final_text=message,
        error_message=error_code,
        execution_outcome=ExecutionOutcome.NEEDS_REVIEW,
    )


def _installed_package_from_handoff(handoff: AppReadyHandoff) -> str:
    package_name = handoff.installed_package
    if not isinstance(package_name, str) or not package_name.strip():
        raise AppWorkflowFailure(
            "APP_READY Handoff 缺少 installed_package",
            stage=AppBootstrapStage.APP_READY,
            error_code="APP_READY_PACKAGE_MISSING",
            retryable=False,
        )
    return package_name


def _failed_result(*, message: str, canceled: bool, step_count: int = 0) -> RunResult:
    return RunResult(
        termination_reason=(TerminationReason.CANCELED if canceled else TerminationReason.ERROR),
        working_memory=WorkingMemory(step_count=step_count),
        trace_sink=InMemoryActionTraceSink(),
        error_message=message,
        error_category=None if canceled else ErrorCategory.INFRASTRUCTURE,
        execution_outcome=ExecutionOutcome.NEEDS_REVIEW,
    )


async def _seal_technical_failure_if_possible(
    runtime: AppV2Runtime,
    *,
    record: AgentRunRecord,
    command: RunAppWorkflowCommand,
    target: TaskTarget,
    checkpoint: AppExplorationRuntimeCheckpoint | None,
    reason_code: str,
    consumed_turns: int,
    consumed_actions: int,
    coverage_summary: dict[str, object] | None = None,
) -> RunResult | None:
    """探索已冻结后技术失败时，尽力封存已有页面事实并保留技术失败结论。"""

    if checkpoint is None or target.app is None:
        return None
    if (
        checkpoint.task_id != record.task_id
        or checkpoint.agent_run_id != record.run_id
        or checkpoint.surface_session_id != command.session_id
        or checkpoint.platform_build_ref != target.app.platform_build_ref
    ):
        return None
    persisted_coverage = await _latest_persisted_coverage_summary(
        runtime,
        task_id=record.task_id,
        agent_run_id=record.run_id,
        surface_session_id=command.session_id,
        deadline=checkpoint.deadline,
    )
    cancel_work = getattr(runtime, "cancel_exploration_work", None)
    if callable(cancel_work):
        try:
            await cancel_work(task_id=record.task_id)
        except Exception:  # noqa: BLE001 - 仍需尝试封存已产生的 Capture
            logger.exception(
                "App 技术失败后取消残留探索 Work 失败 task_id=%s run_id=%s",
                record.task_id,
                record.run_id,
            )
    try:
        closure = await runtime.close_exploration(
            CloseAppExplorationCommand(
                task_id=record.task_id,
                agent_run_id=record.run_id,
                session_id=command.session_id,
                build_id=target.app.platform_build_ref,
                surface_kind=target.surface_kind,
                outcome=AppExplorationResultOutcome.EVIDENCE_INSUFFICIENT,
                terminal_reason_code=reason_code,
                coverage_summary={
                    **persisted_coverage,
                    **dict(coverage_summary or {}),
                    "consumed_turn_count": consumed_turns,
                    "consumed_action_count": consumed_actions,
                    "technical_failure": True,
                    "technical_failure_reason_code": reason_code,
                    "runtime_checkpoint_protocol": checkpoint.protocol_version,
                    "app_runtime_policy": dict(checkpoint.runtime_policy),
                },
                started_at=checkpoint.started_at,
                deadline=checkpoint.deadline,
                run_evidence_gap_reason_code=reason_code,
            )
        )
    except Exception:  # noqa: BLE001 - 调用方随后按原技术失败收敛 LOST
        logger.exception(
            "App 技术失败后的证据封存失败 task_id=%s run_id=%s reason=%s",
            record.task_id,
            record.run_id,
            reason_code,
        )
        return None
    message = "App 探索发生技术失败，失败前页面证据已按证据不足封存"
    if getattr(closure, "close_error", None) is not None:
        message = f"{message}；设备会话关闭失败：{closure.close_error}"
    return _attach_exploration_closure(
        RunResult(
            termination_reason=TerminationReason.INCOMPLETE,
            working_memory=WorkingMemory(step_count=consumed_turns),
            trace_sink=InMemoryActionTraceSink(),
            final_text=message,
            error_message=reason_code,
            error_category=ErrorCategory.INFRASTRUCTURE,
            execution_outcome=ExecutionOutcome.NEEDS_REVIEW,
        ),
        closure,
    )


async def _runtime_cancel_requested(
    runtime: AppV2Runtime,
    *,
    task_id: str,
    agent_run_id: str,
) -> bool:
    checker = getattr(runtime, "is_cancel_requested", None)
    if not callable(checker):
        return False
    return bool(await checker(task_id=task_id, agent_run_id=agent_run_id))


async def _close_canceled_exploration(
    runtime: AppV2Runtime,
    *,
    record: AgentRunRecord,
    command: RunAppWorkflowCommand,
    handoff: AppReadyHandoff,
    target: TaskTarget,
    started_at: datetime,
    deadline: datetime,
    consumed_turns: int,
    runtime_policy: AppExplorationRuntimePolicy,
) -> RunResult:
    """停止新动作，取消残留 Work，并尽力封存取消前已产生的证据。"""

    cancel_work = getattr(runtime, "cancel_workflow", None)
    if not callable(cancel_work):
        # 兼容未升级的测试替身；正式 AppV2Runtime 必须提供 cancel_workflow。
        cancel_work = getattr(runtime, "cancel_exploration_work", None)
    if callable(cancel_work):
        try:
            await cancel_work(task_id=record.task_id)
        except Exception as exc:  # noqa: BLE001 - 仍需继续安全关闭 Session
            logger.error(
                "App 任务取消收束 Work/Action 失败，继续关闭 Session "
                "task_id=%s run_id=%s error_type=%s",
                record.task_id,
                record.run_id,
                type(exc).__name__,
            )
    try:
        closure = await runtime.close_exploration(
            CloseAppExplorationCommand(
                task_id=record.task_id,
                agent_run_id=record.run_id,
                session_id=command.session_id,
                build_id=target.app.platform_build_ref,
                surface_kind=handoff.capture.surface_kind,
                outcome=AppExplorationResultOutcome.EVIDENCE_INSUFFICIENT,
                terminal_reason_code="workflow_canceled",
                coverage_summary={
                    "consumed_turn_count": consumed_turns,
                    "terminal_status": ExplorerWorkStatus.CANCELLED.value,
                    "cancel_requested": True,
                    "app_runtime_policy": runtime_policy.to_dict(),
                },
                started_at=started_at,
                deadline=deadline,
                capture_final_observation=False,
            )
        )
    except Exception as exc:  # noqa: BLE001 - 封存失败时仍需把 Session 收敛为 LOST
        await _mark_runtime_lost(
            runtime,
            task_id=record.task_id,
            session_id=command.session_id,
            expected_surface_kind=target.surface_kind,
            expected_build_id=target.app.platform_build_ref,
            reason=f"WORKFLOW_CANCELED_CLOSURE_FAILED:{type(exc).__name__}",
        )
        return _failed_result(
            message=f"App 探索已取消，但证据封存失败：{exc}",
            canceled=True,
            step_count=consumed_turns,
        )
    message = "App 探索已取消，取消前证据已按证据不足封存"
    if closure.close_error is not None:
        message = f"{message}；设备会话关闭失败：{closure.close_error}"
    return _attach_exploration_closure(
        _failed_result(
            message=message,
            canceled=True,
            step_count=consumed_turns,
        ),
        closure,
    )


async def _close_action_recovery_block(
    runtime: AppV2Runtime,
    *,
    record: AgentRunRecord,
    command: RunAppWorkflowCommand,
    handoff: AppReadyHandoff,
    target: TaskTarget,
    started_at: datetime,
    deadline: datetime,
    consumed_turns: int,
    runtime_policy: AppExplorationRuntimePolicy,
    error_code: str,
) -> RunResult:
    """动作结果未知时停止剩余 Work，封存已有事实且绝不重放 Driver。"""

    persisted_coverage = await _latest_persisted_coverage_summary(
        runtime,
        task_id=record.task_id,
        agent_run_id=record.run_id,
        surface_session_id=command.session_id,
        deadline=deadline,
    )
    cancel_work = getattr(runtime, "cancel_exploration_work", None)
    if callable(cancel_work):
        await cancel_work(task_id=record.task_id)
    closure = await runtime.close_exploration(
        CloseAppExplorationCommand(
            task_id=record.task_id,
            agent_run_id=record.run_id,
            session_id=command.session_id,
            build_id=target.app.platform_build_ref,
            surface_kind=handoff.capture.surface_kind,
            outcome=AppExplorationResultOutcome.EVIDENCE_INSUFFICIENT,
            terminal_reason_code=error_code,
            coverage_summary={
                **persisted_coverage,
                "consumed_turn_count": consumed_turns,
                "terminal_status": ExplorerWorkStatus.FAILED.value,
                "action_recovery_blocked": True,
                "action_recovery_error_code": error_code,
                "app_runtime_policy": runtime_policy.to_dict(),
            },
            started_at=started_at,
            deadline=deadline,
            run_evidence_gap_reason_code=error_code,
        )
    )
    message = "App 动作恢复结果不确定，已停止后续动作并按证据不足封存"
    if closure.close_error is not None:
        message = f"{message}；设备会话关闭失败：{closure.close_error}"
    return RunResult(
        termination_reason=TerminationReason.INCOMPLETE,
        working_memory=WorkingMemory(step_count=consumed_turns),
        trace_sink=InMemoryActionTraceSink(),
        final_text=message,
        error_message=error_code,
        execution_outcome=ExecutionOutcome.NEEDS_REVIEW,
    )


async def _wait_for_database_retry(
    runtime: AppV2Runtime,
    *,
    task_id: str,
    agent_run_id: str,
    deadline: datetime,
    next_claim_at: datetime | None = None,
    now: Callable[[], datetime] | None = None,
) -> bool:
    """以 PG next_attempt_at 决定何时重领；没有事实读取能力时安全退出。"""

    if next_claim_at is None:
        reader = getattr(runtime, "next_exploration_claim_at", None)
        if not callable(reader):
            return False
        next_claim_at = await reader(task_id=task_id)
    if next_claim_at is None or next_claim_at >= deadline:
        return False
    now_fn = now if now is not None else lambda: datetime.now(UTC)
    while True:
        if await _runtime_cancel_requested(
            runtime,
            task_id=task_id,
            agent_run_id=agent_run_id,
        ):
            return True
        remaining = (next_claim_at - now_fn()).total_seconds()
        if remaining <= 0:
            return True
        await asyncio.sleep(min(remaining, 1.0))


def _requires_follow_up_turn(outcome: AppExplorationTurnOutcome) -> bool:
    """成功非终态必须继续；App 单轮 FAILED 时若已有后继 work 也继续，避免课纲只跑开屏。"""
    if not isinstance(outcome, AppExplorationTurnOutcome) or not outcome.claimed:
        return False
    if outcome.final_status is ExplorerWorkStatus.SUCCEEDED:
        return (
            outcome.turn is not None
            and not outcome.turn.is_terminal
            and bool(outcome.successor_work_id)
        )
    # 软失败后仍可能已发布 successor；有 successor 就继续领，让课纲推进下一阶段
    if outcome.final_status is ExplorerWorkStatus.FAILED:
        if _is_after_crash_continue(outcome):
            return True
        return bool(outcome.successor_work_id)
    # RETRY_WAIT：短暂可恢复失败也继续领取（若队列已安排重试）
    if outcome.final_status is ExplorerWorkStatus.RETRY_WAIT:
        return True
    return False


def _outcome_consumed_action(outcome: AppExplorationTurnOutcome) -> bool:
    """只统计可能已进入 Driver 动作边界的轮次。"""

    turn = outcome.turn
    if turn is None:
        return bool(outcome.successor_work_id)
    # 动作已完成但在收尾时被改写成 ExplorationTerminal 时，协调回执仍是
    # 已进入 Driver 边界的唯一事实；必须先于纯终态判定计入动作预算。
    if getattr(turn, "coordination", None) is not None:
        return True
    if bool(getattr(turn, "is_terminal", False)):
        return False
    # Timed Observation 只有 observation_capture；不完整的非终态结果按动作计数以便 fail-closed。
    return getattr(turn, "observation_capture", None) is None


def _turn_result(outcome: AppExplorationTurnOutcome, *, step_count: int = 0) -> RunResult:
    """只有明确消费到 completed 终态时才把整个 App Run 记为成功。"""
    if not isinstance(outcome, AppExplorationTurnOutcome):
        return _failed_result(message="App Explorer 返回了无效单轮结果", canceled=False)
    turn = outcome.turn
    completed = (
        outcome.claimed
        and outcome.final_status is ExplorerWorkStatus.SUCCEEDED
        and turn is not None
        and turn.is_terminal
        and isinstance(turn.decision, ExplorationTerminal)
        and turn.decision.status is ExplorationTerminalStatus.COMPLETED
        and turn.decision.outcome == AppExplorationOutcome.EXPLORATION_COMPLETED.value
    )
    if completed:
        return RunResult(
            termination_reason=TerminationReason.COMPLETED,
            working_memory=WorkingMemory(step_count=step_count),
            trace_sink=InMemoryActionTraceSink(),
            final_text="App Explorer 已消费明确的 completed 终态",
            execution_outcome=ExecutionOutcome.SUCCESS,
        )

    terminal_decision = (
        turn.decision
        if turn is not None and isinstance(getattr(turn, "decision", None), ExplorationTerminal)
        else None
    )
    if (
        terminal_decision is not None
        and terminal_decision.outcome == AppExplorationOutcome.TECHNICAL_FAILED.value
    ):
        return _failed_result(
            message=terminal_decision.reason_code or terminal_decision.reason,
            canceled=False,
            step_count=step_count,
        )
    if (
        terminal_decision is not None
        and terminal_decision.outcome == AppExplorationOutcome.EVIDENCE_INSUFFICIENT.value
    ):
        return RunResult(
            termination_reason=TerminationReason.INCOMPLETE,
            working_memory=WorkingMemory(step_count=step_count),
            trace_sink=InMemoryActionTraceSink(),
            final_text="App 探索证据不足",
            error_message=terminal_decision.reason_code,
            execution_outcome=ExecutionOutcome.NEEDS_REVIEW,
        )

    failed_statuses = {
        ExplorerWorkStatus.FAILED,
        ExplorerWorkStatus.RETRY_WAIT,
    }
    if outcome.final_status in failed_statuses:
        return _failed_result(
            message=outcome.error_code or "App Explorer 单轮执行未成功",
            canceled=False,
            step_count=step_count,
        )
    if outcome.final_status is ExplorerWorkStatus.CANCELLED:
        return _failed_result(
            message="App Explorer 工作已取消",
            canceled=True,
            step_count=step_count,
        )
    return RunResult(
        termination_reason=TerminationReason.INCOMPLETE,
        working_memory=WorkingMemory(step_count=step_count),
        trace_sink=InMemoryActionTraceSink(),
        final_text=(
            "App Explorer 尚未形成 completed 终态"
            if outcome.claimed
            else "App Explorer 未领取到可证明完成的工作"
        ),
        error_message=outcome.error_code,
        execution_outcome=ExecutionOutcome.NEEDS_REVIEW,
    )


def _terminal_decision(
    outcome: AppExplorationTurnOutcome,
) -> ExplorationTerminal | None:
    turn = outcome.turn if isinstance(outcome, AppExplorationTurnOutcome) else None
    decision = getattr(turn, "decision", None) if turn is not None else None
    return decision if isinstance(decision, ExplorationTerminal) else None


def _business_outcome(
    terminal: ExplorationTerminal | None,
) -> AppExplorationResultOutcome | None:
    if terminal is None:
        return None
    mapping = {
        AppExplorationOutcome.EXPLORATION_COMPLETED.value: (
            AppExplorationResultOutcome.EXPLORATION_COMPLETED
        ),
        AppExplorationOutcome.EVIDENCE_INSUFFICIENT.value: (
            AppExplorationResultOutcome.EVIDENCE_INSUFFICIENT
        ),
    }
    return mapping.get(terminal.outcome)


def _is_publication_snapshot_mismatch(*values: object) -> bool:
    """来源、封存 Index 或任务快照不一致时，剩余流程还要继续。"""

    for value in values:
        if value is None:
            continue
        text = str(value)
        if (
            text == APP_PUBLICATION_SNAPSHOT_MISMATCH_CODE
            or APP_PUBLICATION_SNAPSHOT_MISMATCH in text
        ):
            return True
    return False


def _is_technical_outcome(
    outcome: AppExplorationTurnOutcome,
    *,
    terminal_decision: ExplorationTerminal | None,
) -> bool:
    if (
        terminal_decision is not None
        and terminal_decision.outcome == AppExplorationOutcome.TECHNICAL_FAILED.value
    ):
        return True
    if outcome.final_status is ExplorerWorkStatus.FAILED:
        return True
    # Frontier 等待租约／重试不是技术失败；父 Work 会以 RETRY_WAIT + 重置 attempt 续等。
    if (
        outcome.final_status is ExplorerWorkStatus.RETRY_WAIT
        and outcome.error_code == "APP_FRONTIER_NOT_CLAIMABLE"
    ):
        return False
    return outcome.final_status is ExplorerWorkStatus.RETRY_WAIT


def _is_after_crash_continue(outcome: AppExplorationTurnOutcome) -> bool:
    """Worker 崩溃后动作结果未知：继续观察当前前台，禁止整单封存。"""

    return outcome.error_code in {
        "APP_ACTION_OUTCOME_UNKNOWN_AFTER_CRASH",
        "APP_AFTER_CAPTURE_MISSING_AFTER_CRASH",
    }


def _is_action_recovery_block(outcome: AppExplorationTurnOutcome) -> bool:
    error_code = outcome.error_code
    if _is_after_crash_continue(outcome):
        return False
    return error_code in {
        "APP_ACTION_AFTER_CAPTURE_MISSING",
        "APP_ACTION_RECOVERY_BLOCKED",
        "APP_ACTION_RECOVERY_FACTS_MISSING",
        "APP_ACTION_RECOVERY_CAPTURE_READER_MISSING",
        "APP_ACTION_RECOVERY_CAPTURE_MISSING",
        "APP_ACTION_RECOVERY_CAPTURE_INVALID",
        "APP_ACTION_RECOVERY_CAPTURE_UNREADABLE",
        "APP_ACTION_RECOVERY_CAPTURE_CHECKSUM_MISMATCH",
        "APP_ACTION_RECOVERY_RESULT_INVALID",
        "APP_ACTION_RECOVERY_IDENTITY_MISMATCH",
    }


async def _mark_runtime_lost(
    runtime: AppV2Runtime,
    *,
    task_id: str,
    session_id: str,
    expected_surface_kind: SurfaceKind,
    expected_build_id: str,
    reason: str,
) -> None:
    try:
        await runtime.mark_exploration_lost(
            task_id=task_id,
            session_id=session_id,
            reason=reason,
            expected_surface_kind=expected_surface_kind,
            expected_build_id=expected_build_id,
        )
    except Exception:  # noqa: BLE001 - LOST 记录失败不得遮蔽原始技术失败
        logger.exception(
            "App Runtime 标记探索 Session LOST 失败",
            extra={
                "task_id": task_id,
                "session_id": session_id,
                "surface_kind": expected_surface_kind.value,
                "build_id": expected_build_id,
                "reason": reason,
            },
        )


async def _record_technical_failure(
    runtime: AppV2Runtime,
    *,
    record: AgentRunRecord,
    session_id: str,
    phase: str,
    reason_code: str,
    error_type: str,
    retryable: bool,
    observed_at: datetime,
    progress_recorder: object | None = None,
    error_message: str | None = None,
    consumed_turns: int | None = None,
    consumed_actions: int | None = None,
) -> None:
    """尽力写入独立失败事实；审计出口异常不得遮蔽原始技术失败。

    同步输出 ``app_exploration_abort`` 进程日志，并旁路写入 ``qc_task_log``，
    与点击坐标 ``app_tap_audit`` 一样可按关键字检索「因错误未跑完探索」。
    """
    log_app_exploration_abort(
        phase or "technical_failure",
        task_id=record.task_id,
        agent_run_id=record.run_id,
        session_id=session_id,
        reason_code=reason_code,
        error_type=error_type,
        error_message=error_message,
        retryable=retryable,
        consumed_turns=consumed_turns,
        consumed_actions=consumed_actions,
        observed_at=observed_at.isoformat(),
    )
    _notify_exploration_aborted(
        progress_recorder,
        phase=phase,
        reason_code=reason_code,
        error_type=error_type,
        error_message=error_message,
        retryable=retryable,
        consumed_turns=consumed_turns,
        consumed_actions=consumed_actions,
    )

    recorder = getattr(runtime, "record_exploration_failure", None)
    if not callable(recorder):
        return
    failure = build_app_exploration_failure_fact(
        task_id=record.task_id,
        agent_run_id=record.run_id,
        surface_session_id=session_id,
        phase=phase,
        reason_code=reason_code,
        error_type=error_type,
        retryable=retryable,
        observed_at=observed_at,
        metadata={"source": "production_app_v2_runtime_executor"},
    )
    try:
        await recorder(failure=failure)
    except Exception:  # noqa: BLE001 - 原始技术终态优先，待恢复机补写审计
        logger.exception(
            "App Runtime 技术失败事实写入失败",
            extra={
                "task_id": record.task_id,
                "agent_run_id": record.run_id,
                "phase": phase,
                "reason_code": reason_code,
            },
        )
        return
    record_app_metric_safely(
        APP_TECHNICAL_FAILURE_TOTAL,
        phase=phase,
        reason_code=reason_code,
        recovered="false",
    )


def _notify_exploration_published(
    progress_recorder: object | None,
    *,
    objective: str,
    max_actions: int,
    deadline_seconds: int,
) -> None:
    notify = getattr(progress_recorder, "on_app_exploration_published", None)
    if not callable(notify):
        return
    notify(
        objective=objective,
        max_actions=max_actions,
        deadline_seconds=deadline_seconds,
    )


def _notify_exploration_aborted(
    progress_recorder: object | None,
    *,
    phase: str,
    reason_code: str,
    error_type: str | None = None,
    error_message: str | None = None,
    retryable: bool | None = None,
    consumed_turns: int | None = None,
    consumed_actions: int | None = None,
) -> None:
    notify = getattr(progress_recorder, "on_app_exploration_aborted", None)
    if not callable(notify):
        return
    notify(
        phase=phase,
        reason_code=reason_code,
        error_type=error_type,
        error_message=error_message,
        retryable=retryable,
        consumed_turns=consumed_turns,
        consumed_actions=consumed_actions,
    )


def _notify_turn_started(
    progress_recorder: object | None,
    *,
    turn_index: int,
    work_type: str,
) -> None:
    notify = getattr(progress_recorder, "on_app_exploration_turn_started", None)
    if not callable(notify):
        return
    notify(turn_index=turn_index, work_type=work_type)


def _notify_turn_finished(
    progress_recorder: object | None,
    *,
    turn_index: int,
    work_type: str,
    outcome: AppExplorationTurnOutcome,
) -> None:
    notify = getattr(progress_recorder, "on_app_exploration_turn_finished", None)
    if not callable(notify):
        return
    notify(
        turn_index=turn_index,
        work_type=work_type,
        **_turn_progress_fields(outcome),
    )


def _turn_progress_fields(outcome: AppExplorationTurnOutcome) -> dict[str, object | None]:
    """从单轮结果抽出可写入执行日志的扁平字段。"""
    status = (
        outcome.final_status.value if isinstance(outcome.final_status, ExplorerWorkStatus) else None
    )
    action_type: str | None = None
    purpose: str | None = None
    semantic_target: str | None = None
    terminal_status: str | None = None
    terminal_reason: str | None = None
    turn = outcome.turn
    decision = getattr(turn, "decision", None) if turn is not None else None
    if isinstance(decision, ExplorationTerminal):
        terminal_status = decision.status.value
        terminal_reason = decision.reason
    elif decision is not None:
        action = getattr(decision, "action_type", None)
        action_type = getattr(action, "value", None) or (
            str(action) if action is not None else None
        )
        purpose_value = getattr(decision, "purpose", None)
        purpose = str(purpose_value) if purpose_value else None
        target_value = getattr(decision, "semantic_target", None)
        semantic_target = str(target_value) if target_value else None
    return {
        "claimed": bool(outcome.claimed),
        "work_id": outcome.work_id,
        "status": status,
        "action_type": action_type,
        "purpose": purpose,
        "semantic_target": semantic_target,
        "terminal_status": terminal_status,
        "terminal_reason": terminal_reason,
        "result_capture_id": outcome.result_capture_id,
        "successor_work_id": outcome.successor_work_id,
        "error_code": outcome.error_code,
    }
