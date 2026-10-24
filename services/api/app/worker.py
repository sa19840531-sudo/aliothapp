"""Agent Run 后台 Worker 进程入口：消费 Redis 队列，驱动 Agent Loop 执行并写回结果。

独立进程入口，与 [main.py](main.py)（HTTP 入口）共享同一套 ``app/`` 分层代码，
仅通过 Redis 队列与 API 进程解耦（不共享内存、不要求同进程），符合
[services/README.md](../../README.md) "HTTP 服务与后台任务 Worker 是同一代码库的
两个入口"的约定。Redis 负责短期执行态，绑定业务任务的 Run 通过状态 Store 写入
`qc_agent_run`，并在成功结束后初始化 `qc_rule_execution`。

启动方式（本地开发）::

    python -m services.api.app.worker

优雅关闭：Linux/容器环境下监听 SIGINT/SIGTERM 触发平滑停止（处理完当前任务后退出，
不中途打断）；Windows 本地开发环境不支持 ``loop.add_signal_handler``，退化为仅能
通过 Ctrl+C（``KeyboardInterrupt``）中断，此时正在处理的任务会被直接取消。
"""

import asyncio
import json
import logging
import os
import signal
import socket
import uuid
from collections.abc import Awaitable, Mapping
from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from services.api.app.agents.bootstrap import build_default_main_agent
from services.api.app.agents.rule_judge.bootstrap import (
    build_h5_targeted_recapture_rule_judge_agent,
)
from services.api.app.agents.runtime.agent_factory import run_main_agent
from services.api.app.agents.runtime.aggregate_inspection_wakeup import (
    AggregateInspectionWakeupHandler,
)
from services.api.app.agents.runtime.app_inspection_publication_wakeup import (
    AppInspectionPublicationWakeupHandler,
)
from services.api.app.agents.runtime.h5_inspection_publication_wakeup import (
    H5InspectionPublicationWakeupHandler,
)
from services.api.app.agents.runtime.inspection_result_writer import CurrentRuleDecisionWriter
from services.api.app.agents.runtime.inspection_wakeup import (
    INSPECTION_WORK_AVAILABLE_MESSAGE_TYPE,
    CompositeLiveInspectionPublisher,
    InspectionWakeupHandler,
)
from services.api.app.agents.runtime.inspection_work_item_consumer import (
    InspectionJudgeDispatcher,
    InspectionWorkItemConsumer,
)
from services.api.app.agents.runtime.models import (
    ErrorCategory,
    ExecutionOutcome,
    RunResult,
    TerminationReason,
    is_review_termination_reason,
)
from services.api.app.agents.runtime.progress_observer import (
    TaskProgressObserverMiddleware,
    TaskProgressRecorder,
    build_task_progress_recorder,
)
from services.api.app.agents.runtime.run_context import AgentRunContext, bind_agent_run_context
from services.api.app.agents.runtime.task_execution_policy import (
    InvalidTaskExecutionPolicyError,
    TaskExecutionPolicyMiddleware,
    resolve_task_runtime_settings,
)
from services.api.app.agents.runtime.task_submission_wakeup import (
    TaskSubmissionWakeupHandler,
)
from services.api.app.agents.runtime.usage_meter import (
    bind_run_llm_usage_meter,
    flush_pending_llm_usage_events,
)
from services.api.app.agents.tools.page_capture_tools import close_page_capture_session
from services.api.app.agents.tools.rule_judge_tools import (
    judge_and_record_rule_for_current_run,
)
from services.api.app.application.action_executions import (
    APP_WORKFLOW_CANCELED_ERROR_CODE,
    APP_WORKFLOW_CANCELED_ERROR_MESSAGE,
)
from services.api.app.application.aggregate_inspection_execution import (
    AggregateInspectionExecutor,
)
from services.api.app.application.aggregate_inspection_publication import (
    AggregateInspectionPublicationService,
)
from services.api.app.application.aggregate_inspection_work_items import (
    AggregateInspectionWorkItemService,
)
from services.api.app.application.app_evidence_manifest import (
    AppEvidenceManifestFinalizer,
)
from services.api.app.application.app_exploration_reconciliation import (
    AppExplorationReconciler,
)
from services.api.app.application.app_inspection_publication import (
    AppPublicationSnapshotMismatch,
)
from services.api.app.application.app_inspection_publication_reconciliation import (
    AppInspectionPublicationReconciler,
)
from services.api.app.application.app_inspection_publisher import (
    build_default_app_capture_inspection_publisher,
)
from services.api.app.application.app_live_inspection_publish import (
    build_default_app_live_inspection_publish_service,
)
from services.api.app.application.app_manifest_reconciliation import (
    AppManifestBacklogReconciler,
)
from services.api.app.application.app_metrics import (
    APP_RUN_RECOVERY_OUTCOME_TOTAL,
    record_app_metric_safely,
)
from services.api.app.application.app_run_recovery import (
    AcquireAppRunExecutionLeaseCommand,
    AppRunExecutionLeaseGuard,
    AppRunRecoveryRepository,
    ReleaseAppRunExecutionLeaseCommand,
)
from services.api.app.application.app_runtime_composition import (
    build_app_workflow_handler,
)
from services.api.app.application.app_sealed_inspection_publisher import (
    AppSealedInspectionPublisherPort,
    build_default_app_sealed_inspection_publisher,
)
from services.api.app.application.app_workflow_dispatch import (
    AppWorkflowDispatcher,
    AppWorkflowHandler,
    H5WorkflowHandler,
    WebWorkflowHandler,
)
from services.api.app.application.app_workflow_router import (
    AppWorkflowRouter,
    WorkflowRoute,
)
from services.api.app.application.device_lease_recovery import (
    run_device_lease_recovery_loop,
)
from services.api.app.application.device_pool import is_postgres_device_lease_backend
from services.api.app.application.device_wait import describe_worker_device_concurrency
from services.api.app.application.evidence_lifecycle import (
    EvidenceLifecycleService,
    EvidenceLifecycleSweeper,
)
from services.api.app.application.evidence_request_admission import (
    EvidenceRequestAdmissionService,
)
from services.api.app.application.exploration_work_queue import ExplorerWorkType
from services.api.app.application.h5_evidence_handoff import (
    H5EvidenceHandoffPort,
    H5EvidenceHandoffService,
    H5EvidenceHandoffSummary,
)
from services.api.app.application.h5_evidence_manifest import (
    H5EvidenceManifestFinalizer,
)
from services.api.app.application.h5_exploration_cancellation import (
    RedisH5CancellationPort,
)
from services.api.app.application.h5_failed_run_finalization import terminate_failed_h5_task
from services.api.app.application.h5_live_inspection_publish import (
    H5LiveInspectionTaskGateReader,
    PostgresH5LiveInspectionTaskGateReader,
    build_default_h5_live_inspection_publish_service,
)
from services.api.app.application.h5_posthoc_engine_judge import (
    build_default_h5_posthoc_engine_judge_service,
)
from services.api.app.application.h5_runtime_composition import (
    build_h5_workflow_handler,
    is_h5_auth_review_error,
)
from services.api.app.application.h5_task_runtime_settings import (
    build_h5_task_runtime_settings,
)
from services.api.app.application.h5_workflow_shadow import (
    H5WorkflowShadowRecorder,
    build_h5_workflow_shadow_recorder,
)
from services.api.app.application.inspection_composition import (
    build_default_web_live_inspection_publish_service,
    build_default_web_page_inspection_notification,
)
from services.api.app.application.inspection_work_items import InspectionWorkItemService
from services.api.app.application.orchestration import (
    AdvanceTaskFinalizationCommand,
    BindSealedReportCommand,
    QcOrchestrator,
)
from services.api.app.application.page_exploration_summary import summarize_working_memory
from services.api.app.application.report_generation import ReportGenerationService
from services.api.app.application.report_payloads import normalize_report_payload
from services.api.app.application.rule_execution import (
    CandidatePageReference,
    RuleExecutionFinalizationResult,
    RuleExecutionService,
    RuntimePageInspectionFailureFallback,
    RuntimeRuleExecutionFallback,
)
from services.api.app.application.rule_judge import (
    RuleEngineConfigurationGuard,
)
from services.api.app.application.rule_result_reuse import (
    build_current_rule_reuse_planner,
    rule_result_reuse_enabled,
)
from services.api.app.application.run_usage import USAGE_KNOWN_KEYS, merge_usage_payloads
from services.api.app.application.supplemental_evidence_policy import (
    SupplementalEvidencePolicy,
    build_current_supplemental_evidence_policy,
    build_h5_supplemental_evidence_policy,
)
from services.api.app.application.task_finalization import (
    EvaluateCompletionGateCommand,
    TaskFinalizationService,
    TerminateTaskFinalizationCommand,
)
from services.api.app.application.task_insight import TaskInsightService
from services.api.app.application.task_query import TaskQueryService
from services.api.app.application.task_submission_delivery import (
    TaskSubmissionProjectionService,
)
from services.api.app.application.web_evidence_exit_gate import (
    EvidenceExitReservationSweeper,
    build_web_evidence_exit_gate,
)
from services.api.app.application.web_inspection_reconciliation import (
    ReconcileWebInspectionCommand,
    WebInspectionReconciliation,
)
from services.api.app.application.web_page_inspection_notification import (
    ReliableDeliveryWebPublicationWakeup,
)
from services.api.app.application.web_report_generation_audit import (
    AuditedWebFinalReportGenerator,
)
from services.api.app.application.web_runtime_composition import (
    build_web_formal_session_closer,
    build_web_workflow_handler,
)
from services.api.app.application.worker_execution_control import (
    WorkerWorkflowCanceled,
    WorkerWorkflowDeadlineExceeded,
    run_with_worker_controls,
)
from services.api.app.core.config import Settings, get_settings
from services.api.app.core.logging import configure_logging
from services.api.app.domain.evidence.lifecycle import EvidenceLifecyclePolicy
from services.api.app.domain.inspection import (
    AGGREGATE_INSPECTION_WORK_AVAILABLE_MESSAGE_TYPE,
    APP_INSPECTION_PUBLICATION_AVAILABLE_MESSAGE_TYPE,
    H5_INSPECTION_PUBLICATION_AVAILABLE_MESSAGE_TYPE,
)
from services.api.app.domain.page.catalog import (
    InvalidPageTypeCodeError,
)
from services.api.app.domain.page.registry import get_default_page_type_registry
from services.api.app.domain.task import (
    TASK_SUBMISSION_COMMITTED_MESSAGE_TYPE,
    H5InspectionNotificationMode,
    TaskExecutionMode,
    TaskExecutionStatus,
    TaskFinalizationState,
    TaskQualityOutcome,
    parse_h5_inspection_notification_mode,
    task_execution_mode_from_config,
)
from services.api.app.infrastructure.db.action_execution_repository import (
    PostgresActionExecutionRepository,
)
from services.api.app.infrastructure.db.aggregate_inspection_execution_repository import (
    PostgresAggregateInspectionExecutionRepository,
)
from services.api.app.infrastructure.db.aggregate_inspection_publication_repository import (
    PostgresAggregateInspectionPublicationRepository,
)
from services.api.app.infrastructure.db.aggregate_inspection_work_item_repository import (
    PostgresAggregateInspectionWorkItemRepository,
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
from services.api.app.infrastructure.db.app_inspection_publication_repository import (
    PostgresAppInspectionPublicationRepository,
)
from services.api.app.infrastructure.db.app_run_recovery_repository import (
    PostgresAppRunRecoveryRepository,
)
from services.api.app.infrastructure.db.event_inbox_repository import (
    PostgresEventInboxRepository,
)
from services.api.app.infrastructure.db.evidence_lifecycle_repository import (
    PostgresEvidenceLifecycleRepository,
)
from services.api.app.infrastructure.db.evidence_manifest_repository import (
    PostgresEvidenceManifestRepository,
)
from services.api.app.infrastructure.db.evidence_request_repository import (
    PostgresEvidenceRequestRepository,
)
from services.api.app.infrastructure.db.explorer_work_repository import (
    PostgresExplorerWorkRepository,
)
from services.api.app.infrastructure.db.finalization_recovery_fence import (
    FinalizationRecoveryFenceError,
    assert_finalization_recovery_fence,
)
from services.api.app.infrastructure.db.h5_cancellation_settlement_repository import (
    PostgresH5CancellationSettlementRepository,
)
from services.api.app.infrastructure.db.h5_evidence_handoff_repository import (
    PostgresH5EvidenceCaptureReader,
)
from services.api.app.infrastructure.db.h5_evidence_inventory_repository import (
    PostgresH5EvidenceInventoryRepository,
)
from services.api.app.infrastructure.db.h5_inspection_publication_repository import (
    PostgresH5InspectionPublicationRepository,
    WebActiveSessionWorkPendingError,
)
from services.api.app.infrastructure.db.h5_probe_terminal_repository import (
    PostgresH5ProbeTerminalSettlementRepository,
)
from services.api.app.infrastructure.db.h5_run_failure_repository import (
    PostgresH5RunFailureFactsRepository,
)
from services.api.app.infrastructure.db.inspection_work_item_repository import (
    PostgresInspectionWorkItemRepository,
)
from services.api.app.infrastructure.db.llm_usage_event_repository import (
    PostgresLLMUsageEventRepository,
)
from services.api.app.infrastructure.db.pool import get_postgres_pool
from services.api.app.infrastructure.db.surface_session_repository import (
    PostgresSurfaceSessionRepository,
)
from services.api.app.infrastructure.db.task_finalization_repository import (
    TaskFinalizationPersistenceError,
    build_default_task_finalization_service,
)
from services.api.app.infrastructure.db.task_insight_repository import (
    PostgresTaskInsightRepository,
)
from services.api.app.infrastructure.db.task_repository import PostgresTaskRepository
from services.api.app.infrastructure.db.task_submission_delivery_repository import (
    PostgresTaskSubmissionDeliveryRepository,
)
from services.api.app.infrastructure.db.web_coverage_snapshot_repository import (
    PostgresWebCoverageSnapshotRepository,
)
from services.api.app.infrastructure.db.web_formal_terminal_ledger_repository import (
    PostgresWebFormalTerminalLedgerRepository,
)
from services.api.app.infrastructure.db.web_report_generation_attempt_repository import (
    PostgresWebReportGenerationAttemptRepository,
)
from services.api.app.infrastructure.queue.inbox_wakeup import InboxGuardedWakeupHandler
from services.api.app.infrastructure.queue.redis_queue import (
    DEFAULT_WORKER_HEARTBEAT_TTL_SECONDS,
    AgentRunQueueStatus,
    AgentRunRecord,
    RedisAgentRunQueue,
    build_redis_client,
)
from services.api.app.infrastructure.queue.redis_stream import (
    DEFAULT_WAKEUP_STREAM_NAMES,
    RedisStreamWakeupConsumer,
    RedisWakeupDispatcher,
)
from services.api.app.infrastructure.queue.reliable_delivery_runtime import (
    ReliableDeliveryRuntime,
    build_default_reliable_delivery_runtime,
)
from services.api.app.infrastructure.queue.state_store import (
    AgentRunCancellationConflict,
    AgentRunStateStore,
    AgentRunTerminalStateConflict,
    PostgresAgentRunStateStore,
    build_agent_run_state_store,
)
from services.api.app.infrastructure.storage.factory import (
    build_evidence_artifact_store,
)
from services.api.app.infrastructure.storage.h5_evidence_object_verifier import (
    H5EvidenceObjectVerifier,
)

logger = logging.getLogger(__name__)

INCOMPLETE_REPORT_NOTICE = "最终报告完整性校验未通过，任务已转人工复核。"
_BUDGET_LIMIT_TRACE_ERROR_CODES = frozenset(
    {
        "PAGE_SAMPLING_LIMIT_REACHED",
        "NAVIGATION_DEPTH_LIMIT_REACHED",
    }
)
ORPHAN_RECONCILIATION_GRACE_SECONDS = DEFAULT_WORKER_HEARTBEAT_TTL_SECONDS * 2
WORKER_QUEUE_RETRY_SECONDS = 1.0
V2_DELIVERY_RELAY_INTERVAL_SECONDS = 0.5
V2_DELIVERY_SWEEP_INTERVAL_SECONDS = 30.0
V2_TASK_SUBMISSION_CONSUMER_GROUP = "alioth-v2-task-submission-consumer"
V2_TASK_SUBMISSION_STREAM_NAME = DEFAULT_WAKEUP_STREAM_NAMES[TASK_SUBMISSION_COMMITTED_MESSAGE_TYPE]
V2_INSPECTION_CONSUMER_GROUP = "alioth-v2-inspection-consumer"
V2_INSPECTION_STREAM_NAME = DEFAULT_WAKEUP_STREAM_NAMES[INSPECTION_WORK_AVAILABLE_MESSAGE_TYPE]
V2_H5_PUBLICATION_CONSUMER_GROUP = "alioth-v2-h5-publication-consumer"
V2_H5_PUBLICATION_STREAM_NAME = DEFAULT_WAKEUP_STREAM_NAMES[
    H5_INSPECTION_PUBLICATION_AVAILABLE_MESSAGE_TYPE
]
V2_APP_PUBLICATION_CONSUMER_GROUP = "alioth-v2-app-publication-consumer"
V2_APP_PUBLICATION_STREAM_NAME = DEFAULT_WAKEUP_STREAM_NAMES[
    APP_INSPECTION_PUBLICATION_AVAILABLE_MESSAGE_TYPE
]
V2_AGGREGATE_INSPECTION_CONSUMER_GROUP = "alioth-v2-aggregate-inspection-consumer"
V2_AGGREGATE_INSPECTION_STREAM_NAME = DEFAULT_WAKEUP_STREAM_NAMES[
    AGGREGATE_INSPECTION_WORK_AVAILABLE_MESSAGE_TYPE
]
V2_INSPECTION_RECEIVE_BLOCK_MILLISECONDS = 1000
V2_INSPECTION_STALE_CLAIM_INTERVAL_SECONDS = 30.0
V2_INSPECTION_STALE_CLAIM_MIN_IDLE_MILLISECONDS = 60_000


class RuleExecutionWriter(Protocol):
    async def finalize_missing_rule_executions(
        self,
        *,
        task_id: str,
        agent_run_id: str | None,
        finished_at: datetime,
        candidate_pages: list[CandidatePageReference],
        runtime_rule_fallbacks: list[RuntimeRuleExecutionFallback],
        runtime_page_failure_fallbacks: list[RuntimePageInspectionFailureFallback],
    ) -> RuleExecutionFinalizationResult:
        """补齐任务缺失的规则执行记录并返回候选规则缺口。"""


class FinalReportGenerator(Protocol):
    async def generate_task_report(
        self,
        task_id: str,
        *,
        agent_run_id: str | None = None,
        finalized_at: datetime | None = None,
        terminal_task_status: str | None = None,
        termination_reason: str | None = None,
        page_exploration: dict[str, Any] | None = None,
    ) -> dict:
        """在 Worker 完成规则收尾后生成最终报告。"""


class InspectionWorkItemReader(Protocol):
    async def inspection_work_items(
        self,
        task_id: str,
        *,
        control_mode: str | None = None,
    ) -> dict[str, Any]:
        """读取最终报告所需的持久化工作项安全投影。"""


class PageExplorationReader(Protocol):
    async def page_timeline(self, task_id: str) -> dict[str, Any]:
        """读取持久化页面、动作、评估与终态事实。"""

    async def app_stage(self, task_id: str) -> dict[str, Any]:
        """读取 App 封存 Result 的目标、覆盖与视觉质量事实。"""


class LiveActionTraceSink:
    """边执行边落库的动作留痕 sink：每步 fire-and-forget 写入 qc_agent_action_trace，
    让前端轮询在运行中就能看到轨迹；单步写入失败只记日志，终局批量 upsert 兜底。"""

    def __init__(self, *, state_store: Any, run_id: str, task_id: str) -> None:
        self._state_store = state_store
        self._run_id = run_id
        self._task_id = task_id
        self._pending: set[asyncio.Task] = set()
        self.entries: list[Any] = []

    def record(self, entry: Any) -> None:
        self.entries.append(entry)
        try:
            task = asyncio.get_running_loop().create_task(
                self._state_store.insert_action_trace_entry(
                    run_id=self._run_id,
                    task_id=self._task_id,
                    entry=_trace_entry_to_dict(entry),
                )
            )
        except RuntimeError:
            return
        self._pending.add(task)
        task.add_done_callback(self._on_insert_done)

    def _on_insert_done(self, task: asyncio.Task) -> None:
        self._pending.discard(task)
        if not task.cancelled() and task.exception() is not None:
            logger.warning(
                "run_id=%s 动作留痕实时写入失败（终局批量写入会兜底）：%s",
                self._run_id,
                task.exception(),
            )

    async def drain(self) -> None:
        if self._pending:
            await asyncio.gather(*self._pending, return_exceptions=True)


async def _process_one(
    queue: RedisAgentRunQueue,
    run_id: str,
    settings: Settings,
    *,
    state_store: AgentRunStateStore | None = None,
    rule_execution_writer: RuleExecutionWriter | None = None,
    final_report_generator: FinalReportGenerator | None = None,
    web_final_report_generator: FinalReportGenerator | None = None,
    task_finalization_service: TaskFinalizationService | None = None,
    worker_instance_id: str | None = None,
    app_workflow_handler: AppWorkflowHandler | None = None,
    sealed_inspection_publisher: AppSealedInspectionPublisherPort | None = None,
    app_publication_repository: PostgresAppInspectionPublicationRepository | None = None,
    h5_workflow_handler: H5WorkflowHandler | None = None,
    web_workflow_handler: WebWorkflowHandler | None = None,
    h5_evidence_handoff: H5EvidenceHandoffPort | None = None,
    h5_task_gate_reader: H5LiveInspectionTaskGateReader | None = None,
    web_inspection_reconciliation: Any | None = None,
    h5_shadow_recorder: H5WorkflowShadowRecorder | None = None,
    app_run_recovery: AppRunRecoveryRepository | None = None,
    rule_engine_configuration_guard: RuleEngineConfigurationGuard | None = None,
) -> None:
    """处理队列里的一个 run_id：拉取记录、驱动 Agent Loop、写回最终状态。"""
    state_store = state_store or build_agent_run_state_store()
    browser_full_inspection_enabled = (
        settings.workflow.h5_v2_mode == "on" or settings.workflow.web_v2_mode == "on"
    )
    h5_full_inspection_enabled = settings.workflow.h5_v2_mode == "on"
    if (
        settings.workflow.app_qc_enabled or browser_full_inspection_enabled
    ) and task_finalization_service is None:
        task_finalization_service = build_default_task_finalization_service()
    if h5_full_inspection_enabled and h5_evidence_handoff is None:
        h5_evidence_handoff = _build_default_h5_evidence_handoff()
    if h5_full_inspection_enabled and h5_task_gate_reader is None:
        h5_task_gate_reader = PostgresH5LiveInspectionTaskGateReader()
    record = await queue.get(run_id)
    if record is None:
        logger.warning("run_id=%s 在 Redis 中找不到对应记录，跳过。", run_id)
        return

    from services.api.app.domain.exploration.web_runtime_contract import (
        WEB_LEGACY_RUN_EXECUTION_NOT_SUPPORTED,
        is_legacy_web_execution,
    )

    if is_legacy_web_execution(record.task_config):
        if record.status in {AgentRunQueueStatus.QUEUED, AgentRunQueueStatus.RUNNING}:
            await queue.claim_running(record, worker_instance_id=worker_instance_id)
        if record.error_message == WEB_LEGACY_RUN_EXECUTION_NOT_SUPPORTED:
            # Redis 已拒绝领取；即使前次持久化中断，也可重试收口历史 Run 与 Task。
            await state_store.record_finished(record)
        return

    if record.status is not AgentRunQueueStatus.QUEUED:
        logger.info(
            "run_id=%s 的 Redis 投影已是 %s，跳过重复或过期唤醒",
            run_id,
            record.status.value,
        )
        return

    final_report_generator = _final_report_generator_for_record(
        record,
        default_generator=final_report_generator,
        web_generator=web_final_report_generator,
    )

    workflow_dispatcher = AppWorkflowDispatcher(
        router=AppWorkflowRouter(
            app_qc_enabled=settings.workflow.app_qc_enabled,
            h5_v2_mode=settings.workflow.h5_v2_mode,
            web_v2_mode=settings.workflow.web_v2_mode,
        ),
        handler=app_workflow_handler,
        h5_handler=h5_workflow_handler,
        web_handler=web_workflow_handler,
    )
    try:
        workflow_route = workflow_dispatcher.route(record, settings)
        # App 的规则就绪检查属于探索后的正式质检门禁。若在这里执行，
        # full_inspection 会在截图探索前失败，而 exploration_only 已经进入
        # 同一 App Runtime，造成两个 V2 入口看似使用了不同的页面探索能力。
        web_exploration_only = (
            workflow_route is WorkflowRoute.WEB_V2
            and task_execution_mode_from_config(record.task_config)
            is TaskExecutionMode.EXPLORATION_ONLY
        )
        if (
            workflow_route is not WorkflowRoute.APP
            and not web_exploration_only
            and rule_engine_configuration_guard is not None
        ):
            await rule_engine_configuration_guard.ensure_ready_for(
                task_execution_mode_from_config(record.task_config)
            )
    except Exception as exc:  # noqa: BLE001 - 路由或门禁错误必须收敛为 Run 失败
        logger.warning(
            "run_id=%s Workflow 路由或执行前门禁失败，拒绝继续执行：%s",
            run_id,
            exc,
        )
        started = await _start_dequeued_record(
            queue=queue,
            record=record,
            state_store=state_store,
            worker_instance_id=worker_instance_id,
        )
        if not started:
            return
        record.status = AgentRunQueueStatus.FAILED
        record.finished_at = datetime.now(UTC)
        record.termination_reason = TerminationReason.ERROR.value
        record.error_message = str(exc)
        record.error_category = ErrorCategory.INFRASTRUCTURE.value
        record.worker_instance_id = None
        if _is_locked_web_v2_run_record(record):
            await _finish_web_formal_inspection_run(
            queue=queue,
            record=record,
                settings=settings,
            state_store=state_store,
            progress_recorder=None,
            final_report_generator=final_report_generator,
            task_finalization_service=task_finalization_service,
                reconciliation=web_inspection_reconciliation,
                worker_instance_id=worker_instance_id,
                web_formal_session_closer=getattr(
                    web_workflow_handler,
                    "formal_session_closer",
                    None,
                ),
            )
            return
        await _finish_standard_terminal_run(
            queue=queue,
            record=record,
            state_store=state_store,
            progress_recorder=None,
            final_report_generator=final_report_generator,
            task_finalization_service=_formal_task_finalization_service(
                record,
                task_finalization_service,
            ),
        )
        return
    if workflow_route is WorkflowRoute.APP:
        await _process_app_one_with_run_lease(
            queue=queue,
            record=record,
            settings=settings,
            state_store=state_store,
            dispatcher=workflow_dispatcher,
            recovery=app_run_recovery or PostgresAppRunRecoveryRepository(),
            rule_execution_writer=rule_execution_writer,
            final_report_generator=final_report_generator,
            worker_instance_id=worker_instance_id,
            task_finalization_service=task_finalization_service,
            sealed_inspection_publisher=sealed_inspection_publisher,
            app_publication_repository=app_publication_repository,
            rule_engine_configuration_guard=rule_engine_configuration_guard,
        )
        return

    started = await _start_dequeued_record(
        queue=queue,
        record=record,
        state_store=state_store,
        worker_instance_id=worker_instance_id,
    )
    if not started:
        return
    if await _finish_canceled_run_if_requested(
        queue=queue,
        record=record,
        state_store=state_store,
        final_report_generator=final_report_generator,
        task_finalization_service=task_finalization_service,
    ):
        return
    if workflow_route is WorkflowRoute.H5_V2:
        await _process_h5_v2_one(
            queue,
            record,
            settings=settings,
            state_store=state_store,
            dispatcher=workflow_dispatcher,
            rule_execution_writer=rule_execution_writer,
            final_report_generator=final_report_generator,
            worker_instance_id=worker_instance_id,
            task_finalization_service=task_finalization_service,
            h5_evidence_handoff=h5_evidence_handoff,
            h5_task_gate_reader=h5_task_gate_reader,
        )
        return
    if workflow_route is WorkflowRoute.WEB_V2:
        await _process_web_v2_one(
            queue=queue,
            record=record,
            settings=settings,
            state_store=state_store,
            dispatcher=workflow_dispatcher,
            rule_execution_writer=rule_execution_writer,
            final_report_generator=final_report_generator,
            worker_instance_id=worker_instance_id,
            task_finalization_service=task_finalization_service,
            web_inspection_reconciliation=web_inspection_reconciliation,
            web_formal_session_closer=getattr(
                web_workflow_handler,
                "formal_session_closer",
                None,
            ),
        )
        return

    # 旁路过程观察者：把 Tool 调用流翻译成任务日志与页面节点状态，只观察不干预；
    # 未启用 postgres 长期状态时为 None，链路行为与之前完全一致。
    progress_recorder = build_task_progress_recorder(
        task_id=record.task_id,
        agent_run_id=record.run_id,
        task_config=record.task_config,
    )
    if progress_recorder is not None:
        await progress_recorder.on_run_started()

    try:
        runtime_settings = resolve_task_runtime_settings(
            settings.agent_runtime,
            record.loop_guard,
            record.page_sampling_policy,
        )
        policy_middleware = _task_execution_policy_middleware(record)
    except InvalidTaskExecutionPolicyError as exc:
        logger.warning("run_id=%s 任务执行策略无效：%s", run_id, exc)
        record.status = AgentRunQueueStatus.FAILED
        record.finished_at = datetime.now(UTC)
        record.termination_reason = TerminationReason.ERROR.value
        record.error_message = str(exc)
        record.error_category = ErrorCategory.UNKNOWN.value
        await _finish_standard_terminal_run(
            queue=queue,
            record=record,
            state_store=state_store,
            progress_recorder=progress_recorder,
            final_report_generator=final_report_generator,
            task_finalization_service=task_finalization_service,
        )
        return

    if await _is_cancel_requested_safely(queue, run_id, durable_source=state_store):
        record.status = AgentRunQueueStatus.CANCELED
        record.finished_at = datetime.now(UTC)
        record.termination_reason = TerminationReason.CANCELED.value
        record.worker_instance_id = None
        await _finish_standard_terminal_run(
            queue=queue,
            record=record,
            state_store=state_store,
            progress_recorder=progress_recorder,
            final_report_generator=final_report_generator,
            task_finalization_service=task_finalization_service,
        )
        return

    try:
        agent = (
            build_default_main_agent()
            if runtime_settings == settings.agent_runtime
            else build_default_main_agent(runtime_settings=runtime_settings)
        )
    except Exception as exc:
        # 多为 LLM 网关配置缺失/错误（如未配置 ALIOTH_LLM_API_KEY），发生在真正进入
        # Agent Loop 之前。必须在此显式写回 FAILED，否则记录会永远停留在 RUNNING——
        # run_id 已从队列弹出，不会被重新处理，调用方查询接口只会看到一个永不进展的
        # 假死状态，且看不到任何错误信息。
        logger.exception("run_id=%s 构建 Main Agent 失败，多为 LLM 网关配置问题。", run_id)
        record.status = AgentRunQueueStatus.FAILED
        record.finished_at = datetime.now(UTC)
        record.termination_reason = TerminationReason.ERROR.value
        record.error_message = str(exc)
        record.error_category = ErrorCategory.LLM_PROVIDER.value
        record.worker_instance_id = None
        await _finish_standard_terminal_run(
            queue=queue,
            record=record,
            state_store=state_store,
            progress_recorder=progress_recorder,
            final_report_generator=final_report_generator,
            task_finalization_service=task_finalization_service,
        )
        return

    async def is_cancelled() -> bool:
        return await _is_cancel_requested_safely(
            queue,
            run_id,
            durable_source=state_store,
        )

    live_sink: LiveActionTraceSink | None = None
    if record.task_id is not None and hasattr(state_store, "insert_action_trace_entry"):
        live_sink = LiveActionTraceSink(
            state_store=state_store, run_id=record.run_id, task_id=record.task_id
        )

    extra_middleware = [
        middleware
        for middleware in (
            policy_middleware,
            (
                TaskProgressObserverMiddleware(progress_recorder)
                if progress_recorder is not None
                else None
            ),
        )
        if middleware is not None
    ]

    worker_rss_before_bytes = _process_rss_bytes()
    worker_rss_after_bytes: int | None = None
    try:
        try:
            with bind_agent_run_context(
                AgentRunContext(
                    task_id=record.task_id,
                    agent_run_id=record.run_id,
                    task_config=dict(record.task_config or {}),
                )
            ):
                result = await run_main_agent(
                    agent,
                    record.message,
                    runtime_settings=runtime_settings,
                    is_cancelled=is_cancelled,
                    sink=live_sink,
                    **({"extra_middleware": extra_middleware} if extra_middleware else {}),
                )
        finally:
            if live_sink is not None:
                await live_sink.drain()
            try:
                await close_page_capture_session(record.run_id)
            except Exception:
                logger.exception("run_id=%s 关闭页面采集会话失败。", run_id)
            worker_rss_after_bytes = _process_rss_bytes()
    except Exception:
        # run_main_agent 内部已兜底绝大多数异常并转换为 RunResult；此处只捕获真正
        # 意料之外的基础设施级异常（如 Redis 写入失败本身），标记为 FAILED 而非
        # 让整个 Worker 协程崩溃退出。
        logger.exception("run_id=%s 处理过程中出现基础设施级异常。", run_id)
        record.status = AgentRunQueueStatus.FAILED
        record.finished_at = datetime.now(UTC)
        record.termination_reason = TerminationReason.ERROR.value
        record.error_category = ErrorCategory.INFRASTRUCTURE.value
        record.worker_instance_id = None
        await _finish_standard_terminal_run(
            queue=queue,
            record=record,
            state_store=state_store,
            progress_recorder=progress_recorder,
            final_report_generator=final_report_generator,
            task_finalization_service=task_finalization_service,
        )
        return

    _apply_result(record, result)
    _record_worker_resource_usage(
        record,
        rss_before_bytes=worker_rss_before_bytes,
        rss_after_bytes=worker_rss_after_bytes,
    )
    record.worker_instance_id = None
    if await _finish_standard_if_cancel_requested(
        queue=queue,
        record=record,
        state_store=state_store,
        progress_recorder=progress_recorder,
        final_report_generator=final_report_generator,
    ):
        return
    if workflow_route is WorkflowRoute.H5_SHADOW:
        if h5_shadow_recorder is None:
            logger.warning("run_id=%s H5 Shadow recorder 未配置，跳过只读投影", run_id)
        else:
            try:
                await h5_shadow_recorder.record(
                    task_id=record.task_id,
                    agent_run_id=record.run_id,
                    working_memory=result.working_memory.audit_snapshot(
                        action_trace_count=len(getattr(result.trace_sink, "entries", ()))
                    ),
                    legacy_conclusion=result.termination_reason.value,
                    legacy_input_tokens=(
                        result.usage.input_tokens if result.usage is not None else None
                    ),
                )
            except Exception:  # noqa: BLE001 - Shadow 失败不得污染 V1 正式执行事实
                logger.warning(
                    "run_id=%s H5 V2 只读 Shadow 记录失败",
                    run_id,
                    exc_info=True,
                )
    if await _finish_standard_if_cancel_requested(
        queue=queue,
        record=record,
        state_store=state_store,
        progress_recorder=progress_recorder,
        final_report_generator=final_report_generator,
    ):
        return
    legacy_report_enabled = True
    try:
        await _drain_snapshot_inspection_work(
            record,
            settings=settings,
            worker_instance_id=worker_instance_id,
        )
        if await _finish_standard_if_cancel_requested(
            queue=queue,
            record=record,
            state_store=state_store,
            progress_recorder=progress_recorder,
            final_report_generator=final_report_generator,
        ):
            return
        await _finalize_rule_executions(record, rule_execution_writer)
        if await _finish_standard_if_cancel_requested(
            queue=queue,
            record=record,
            state_store=state_store,
            progress_recorder=progress_recorder,
            final_report_generator=final_report_generator,
        ):
            return
        await _finalize_persisted_inspection_work(record, settings=settings)
        if await _finish_standard_if_cancel_requested(
            queue=queue,
            record=record,
            state_store=state_store,
            progress_recorder=progress_recorder,
            final_report_generator=final_report_generator,
        ):
            return
    except Exception as exc:
        logger.exception("run_id=%s 任务收尾失败。", run_id)
        record.status = AgentRunQueueStatus.FAILED
        record.finished_at = datetime.now(UTC)
        record.termination_reason = TerminationReason.ERROR.value
        record.error_message = str(exc)
        record.error_category = ErrorCategory.UNKNOWN.value
        legacy_report_enabled = False
    await _finish_standard_terminal_run(
        queue=queue,
        record=record,
        state_store=state_store,
        progress_recorder=progress_recorder,
        final_report_generator=final_report_generator,
        task_finalization_service=task_finalization_service,
        inspection_work_items_enabled=True,
        legacy_report_enabled=legacy_report_enabled,
    )


async def _start_dequeued_record(
    *,
    queue: RedisAgentRunQueue,
    record: AgentRunRecord,
    state_store: AgentRunStateStore,
    worker_instance_id: str | None,
) -> bool:
    """原子领取 queued Run 后推进 PG；启动失败时把记录放回队列。"""

    if not await queue.claim_running(
        record,
        worker_instance_id=worker_instance_id,
    ):
        logger.info(
            "run_id=%s 的 Redis 投影已被其他 Worker 领取或已终结，跳过重复启动",
            record.run_id,
        )
        return False
    try:
        await state_store.record_started(record)
    except Exception as exc:  # noqa: BLE001 - 出队失败必须恢复 Redis 状态
        logger.exception("run_id=%s 写入启动状态失败，正在恢复出队记录。", record.run_id)
        try:
            cancel_requested = await _is_cancel_requested_safely(
                queue,
                record.run_id,
                durable_source=state_store,
            )
        except RuntimeError:
            logger.warning(
                "run_id=%s 长期取消事实暂时不可判定，保持可恢复队列且不消耗失败预算。",
                record.run_id,
                exc_info=True,
            )
            await queue.requeue_after_cancellation_check_unavailable(
                record,
                error_message=str(exc),
            )
            return False
        if cancel_requested:
            _project_run_canceled(record)
            record.worker_instance_id = None
            await _finish_standard_terminal_run(
                queue=queue,
                record=record,
                state_store=state_store,
                progress_recorder=None,
                final_report_generator=None,
                task_finalization_service=None,
            )
            return False
        requeued = await queue.requeue_after_dispatch_failure(
            record,
            error_message=str(exc),
        )
        if not requeued:
            try:
                await _finish_standard_terminal_run(
                    queue=queue,
                    record=record,
                    state_store=state_store,
                    progress_recorder=None,
                    final_report_generator=None,
                    task_finalization_service=None,
                )
            except Exception:  # noqa: BLE001 - 数据库仍不可用时保留 Redis 失败终态
                logger.exception(
                    "run_id=%s 启动重试耗尽后写入失败终态失败。",
                    record.run_id,
                )
        return False
    return True


async def _finish_canceled_run_if_requested(
    *,
    queue: RedisAgentRunQueue,
    record: AgentRunRecord,
    state_store: AgentRunStateStore,
    final_report_generator: FinalReportGenerator | None,
    task_finalization_service: TaskFinalizationService | None,
) -> bool:
    if not await _is_cancel_requested_safely(
        queue,
        record.run_id,
        durable_source=state_store,
    ):
        return False
    _project_run_canceled(record)
    record.worker_instance_id = None
    await _finish_standard_terminal_run(
        queue=queue,
        record=record,
        state_store=state_store,
        progress_recorder=None,
        final_report_generator=final_report_generator,
        task_finalization_service=None,
    )
    return True


async def _process_app_one_with_run_lease(
    *,
    queue: RedisAgentRunQueue,
    record: AgentRunRecord,
    settings: Settings,
    state_store: AgentRunStateStore,
    dispatcher: AppWorkflowDispatcher,
    recovery: AppRunRecoveryRepository,
    rule_execution_writer: RuleExecutionWriter | None,
    final_report_generator: FinalReportGenerator | None,
    worker_instance_id: str | None,
    task_finalization_service: TaskFinalizationService | None,
    sealed_inspection_publisher: AppSealedInspectionPublisherPort | None,
    app_publication_repository: PostgresAppInspectionPublicationRepository | None,
    rule_engine_configuration_guard: RuleEngineConfigurationGuard | None,
) -> None:
    """先领取 PG Run 租约，再推进 Run 状态并覆盖完整 App Runtime。"""

    lease_owner = worker_instance_id or "app-worker"
    lease_token = str(uuid.uuid4())
    lease = await recovery.acquire(
        AcquireAppRunExecutionLeaseCommand(
            task_id=record.task_id,
            agent_run_id=record.run_id,
            lease_owner=lease_owner,
            lease_token=lease_token,
            lease_seconds=settings.worker.app_run_lease_seconds,
        )
    )
    if lease is None:
        logger.info(
            "run_id=%s 已由其他 Worker 持有 App Run 执行租约，跳过重复唤醒",
            record.run_id,
        )
        # 若是提交时 qc_agent_run 尚未落库的极窄竞态，周期 PG 扫描会在长期事实
        # 可见后补回同一 run_id；此处不能立即重排，否则活租约期间会形成热循环。
        return
    guard = AppRunExecutionLeaseGuard(
        repository=recovery,
        lease=lease,
        lease_seconds=settings.worker.app_run_lease_seconds,
        renew_interval_seconds=max(1, settings.worker.app_run_lease_seconds // 3),
    )
    deferred_finish: _DeferredAppRunFinish | None = None
    try:
        async with guard:
            started = await guard.run(
                _start_dequeued_record(
                    queue=queue,
                    record=record,
                    state_store=state_store,
                    worker_instance_id=worker_instance_id,
                )
            )
            if not started:
                return
            canceled = await guard.run(
                _finish_canceled_run_if_requested(
                    queue=queue,
                    record=record,
                    state_store=state_store,
                    final_report_generator=final_report_generator,
                    task_finalization_service=task_finalization_service,
                )
            )
            if canceled:
                return
            deferred_finish = await guard.run(
                _process_app_one(
                    queue,
                    record,
                    settings=settings,
                    state_store=state_store,
                    dispatcher=dispatcher,
                    rule_execution_writer=rule_execution_writer,
                    final_report_generator=final_report_generator,
                    worker_instance_id=worker_instance_id,
                    task_finalization_service=task_finalization_service,
                    sealed_inspection_publisher=sealed_inspection_publisher,
                    app_publication_repository=app_publication_repository,
                    rule_engine_configuration_guard=rule_engine_configuration_guard,
                    defer_terminal_finish=True,
                )
            )
            if deferred_finish is not None:
                # 报告与 Run 终态交接也在同一租约内；终态后只允许原 token 续租。
                await guard.run(
                    _finish_terminal_run(
                        queue=queue,
                        record=record,
                        state_store=state_store,
                        progress_recorder=deferred_finish.progress_recorder,
                        final_report_generator=final_report_generator,
                        task_finalization_service=(
                            task_finalization_service
                            if deferred_finish.formal_inspection_enabled
                            else None
                        ),
                        inspection_work_items_enabled=deferred_finish.formal_inspection_enabled,
                        legacy_report_enabled=deferred_finish.legacy_report_enabled,
                    )
                )
    finally:
        try:
            await recovery.release(
                ReleaseAppRunExecutionLeaseCommand(
                    task_id=record.task_id,
                    agent_run_id=record.run_id,
                    lease_owner=lease_owner,
                    lease_token=lease_token,
                )
            )
        except Exception:  # noqa: BLE001 - 过期租约可由 PostgreSQL 恢复扫描接管
            logger.warning(
                "run_id=%s 释放 App Run 执行租约失败",
                record.run_id,
                exc_info=True,
            )


@dataclass(frozen=True)
class _DeferredAppRunFinish:
    """由持有 App Run 租约的 Worker 执行下游终态交接。"""

    progress_recorder: TaskProgressRecorder | None
    legacy_report_enabled: bool
    formal_inspection_enabled: bool


@dataclass(frozen=True)
class _WorkerFinalizationDecision:
    gate_passed: bool
    gate_snapshot_id: str
    finalization_epoch: int
    block_reasons: tuple[str, ...]


async def _finish_terminal_run(
    *,
    queue: RedisAgentRunQueue,
    record: AgentRunRecord,
    state_store: AgentRunStateStore,
    progress_recorder: TaskProgressRecorder | None,
    final_report_generator: FinalReportGenerator | None,
    task_finalization_service: TaskFinalizationService | None,
    inspection_work_items_enabled: bool = False,
    legacy_report_enabled: bool = False,
) -> None:
    """统一持久化 Run 终态，并让 Completion Gate 独占 Task 终态写入。"""
    use_v2_finalization = record.task_id is not None and task_finalization_service is not None
    if not use_v2_finalization:
        if legacy_report_enabled:
            await _generate_final_report(
                record,
                final_report_generator,
                inspection_work_items_enabled=inspection_work_items_enabled,
            )
        await _reconcile_run_llm_usage_from_events(record)
        await state_store.record_finished(record)
        await _save_queue_projection(queue, record)
        await _finish_progress(progress_recorder, record)
        return

    await _reconcile_run_llm_usage_from_events(record)
    await state_store.record_run_finished(record)
    reconcile_legacy_candidates = legacy_report_enabled and not inspection_work_items_enabled
    try:
        await _generate_final_report(
            record,
            final_report_generator,
            finalized=False,
            inspection_work_items_enabled=inspection_work_items_enabled,
            legacy_candidate_reconciliation_enabled=reconcile_legacy_candidates,
        )
    except Exception:  # noqa: BLE001 - 预览报告失败不能阻断后续恢复
        logger.exception(
            "task_id=%s Completion Gate 预览报告生成失败，将保留可恢复收口状态",
            record.task_id,
        )
    preview_report = (
        deepcopy(record.final_report) if isinstance(record.final_report, dict) else None
    )
    await _reconcile_run_llm_usage_from_events(record)
    await state_store.record_run_finished(record)
    service = task_finalization_service
    if service is None:
        raise RuntimeError("Completion Gate Service 未注入")
    try:
        await _generate_final_report(
            record,
            final_report_generator,
            finalized=True,
            inspection_work_items_enabled=inspection_work_items_enabled,
            legacy_candidate_reconciliation_enabled=reconcile_legacy_candidates,
        )
        await _reconcile_run_llm_usage_from_events(record)
        _prepare_report_binding_metadata(record)
        decision = await _advance_task_finalization(record, service)
    except Exception:  # noqa: BLE001 - 最终报告失败时不封存，等待后台恢复
        logger.exception(
            "task_id=%s 最终报告生成或原子收口失败，将保留可恢复状态",
            record.task_id,
        )
        if preview_report is not None:
            record.final_report = preview_report
        else:
            record.final_report = None
        await _reconcile_run_llm_usage_from_events(record)
        await state_store.record_run_finished(record)
        await _save_queue_projection(queue, record)
        await _finish_progress(progress_recorder, record)
        return
    if decision.gate_passed:
        # 原子最终化已经在封存前写入 Agent Run 和报告绑定；此处只能更新短期队列视图。
        _attach_completion_gate(record, decision)
        await _save_queue_projection(queue, record)
    else:
        if preview_report is not None:
            record.final_report = preview_report
        _attach_completion_gate(record, decision)
        await _reconcile_run_llm_usage_from_events(record)
        await state_store.record_run_finished(record)
        await _save_queue_projection(queue, record)
    await _finish_progress(progress_recorder, record)


async def _reconcile_run_llm_usage_from_events(record: AgentRunRecord) -> None:
    """以 LLM 调用流水校正 Run 聚合成本，并保留非计费用量字段。"""

    if record.task_id is None or not os.getenv("ALIOTH_DATABASE_URL"):
        return
    try:
        await flush_pending_llm_usage_events()
        usage = await PostgresLLMUsageEventRepository().aggregate_for_agent_run(
            agent_run_id=record.run_id
        )
    except Exception:  # noqa: BLE001 - 计量聚合失败不得阻断业务终态写入
        logger.warning(
            "run_id=%s LLM 用量流水聚合失败，将保留当前 Run usage。",
            record.run_id,
            exc_info=True,
        )
        return
    if usage:
        record.usage = _replace_billable_usage_payload(record.usage, usage)


def _replace_billable_usage_payload(
    current: Mapping[str, Any] | None,
    billable_usage: Mapping[str, Any],
) -> dict[str, Any]:
    """用明细账覆盖计费字段，避免 meter 与 event 双算。"""

    payload = {
        key: value
        for key, value in dict(current or {}).items()
        if key not in USAGE_KNOWN_KEYS and key != "usage_event_count"
    }
    payload.update(dict(billable_usage))
    return payload


async def _save_queue_projection(
    queue: RedisAgentRunQueue,
    record: AgentRunRecord,
) -> None:
    """尽力更新 Redis 短期视图，断连不得回滚 PostgreSQL 事实。"""
    try:
        await queue.save(record)
    except Exception:  # noqa: BLE001 - Redis 只承载短期投影
        logger.warning(
            "run_id=%s Redis 短期视图更新失败，PostgreSQL 事实不受影响",
            record.run_id,
            exc_info=True,
        )


async def _record_run_finished_for_recovery(
    state_store: AgentRunStateStore,
    record: AgentRunRecord,
    *,
    reason: str,
) -> bool:
    """后台恢复写回 Run 终态；PG 已有权威终态时视为幂等完成。"""

    try:
        await state_store.record_run_finished(record)
    except AgentRunTerminalStateConflict:
        logger.debug(
            "run_id=%s %s 时发现 PostgreSQL 已有权威终态，跳过重复写回。",
            record.run_id,
            reason,
        )
        return False
    return True


async def _project_authoritative_finished_run(
    queue: RedisAgentRunQueue,
    state_store: AgentRunStateStore,
    *,
    run_id: str,
    reason: str,
) -> bool:
    """终态冲突后用 PostgreSQL 权威 Run 修正 Redis 短期投影。"""

    try:
        authoritative = await state_store.get_finished_record(run_id=run_id)
    except Exception:  # noqa: BLE001 - 投影修复失败不能覆盖原冲突语义
        logger.warning(
            "run_id=%s %s 后读取 PostgreSQL 权威终态失败。",
            run_id,
            reason,
            exc_info=True,
        )
        return False
    if authoritative is None:
        logger.info(
            "run_id=%s %s 后未读取到 PostgreSQL 权威终态，跳过 Redis 投影刷新。",
            run_id,
            reason,
        )
        return False
    authoritative.worker_instance_id = None
    authoritative.worker_missing_since = None
    await _save_queue_projection(queue, authoritative)
    logger.info(
        "run_id=%s %s 后已用 PostgreSQL 权威终态刷新 Redis 投影。",
        run_id,
        reason,
    )
    return True


async def _requeue_app_run_instead_of_closeout(
    queue: RedisAgentRunQueue,
    record: AgentRunRecord,
    *,
    worker_instance_id: str | None,
) -> None:
    """快照不一致时把同一 Run 放回队列，不写成失败终态，也不收口。"""

    usage = dict(record.usage or {})
    continues = int(usage.get("app_publication_snapshot_continues") or 0)
    usage["app_publication_snapshot_continues"] = continues + 1
    record.usage = usage
    record.finished_at = None
    record.termination_reason = None
    record.error_message = None
    record.error_category = None
    record.execution_outcome = None
    record.worker_missing_since = None
    if continues >= 1:
        logger.warning(
            "run_id=%s App Publication 来源、封存 Index 或任务快照仍不一致。"
            "本轮不收口，保留运行中的流程。",
            record.run_id,
        )
        record.status = AgentRunQueueStatus.RUNNING
        record.worker_instance_id = worker_instance_id
        await queue.save(record)
        return
    logger.warning(
        "run_id=%s App Publication 来源、封存 Index 或任务快照不一致。"
        "不结束剩余流程，也不收口，放回队列继续执行。",
        record.run_id,
    )
    record.status = AgentRunQueueStatus.QUEUED
    record.worker_instance_id = None
    restored = await queue.restore_queued(
        record,
        expected_running_worker_instance_id=worker_instance_id,
    )
    if not restored:
        logger.warning(
            "run_id=%s 快照不一致后的继续入队未写入新的队列项。",
            record.run_id,
        )


async def _process_app_one(
    queue: RedisAgentRunQueue,
    record: AgentRunRecord,
    *,
    settings: Settings,
    state_store: AgentRunStateStore,
    dispatcher: AppWorkflowDispatcher,
    rule_execution_writer: RuleExecutionWriter | None = None,
    final_report_generator: FinalReportGenerator | None = None,
    worker_instance_id: str | None = None,
    task_finalization_service: TaskFinalizationService | None = None,
    sealed_inspection_publisher: AppSealedInspectionPublisherPort | None = None,
    app_publication_repository: PostgresAppInspectionPublicationRepository | None = None,
    rule_engine_configuration_guard: RuleEngineConfigurationGuard | None = None,
    defer_terminal_finish: bool = False,
) -> _DeferredAppRunFinish | None:
    """执行已经路由到 App Workflow 的 Run，不触碰通用 H5 Main Agent。"""
    execution_mode = task_execution_mode_from_config(record.task_config)
    formal_inspection_enabled = execution_mode is TaskExecutionMode.FULL_INSPECTION
    progress_recorder = build_task_progress_recorder(
        task_id=record.task_id,
        agent_run_id=record.run_id,
        task_config=record.task_config,
    )
    if progress_recorder is not None:
        await progress_recorder.on_run_started()
    try:
        # App 与 H5 必须使用同一套 Run 级 LLM 计量边界。截图理解 Provider
        # 会把每次响应 usage 写入当前上下文；未绑定计量器时数据库会错误显示
        # ai_calls/total_tokens=0，即使真实视觉模型已经被调用。
        with bind_run_llm_usage_meter() as usage_meter:
            result = await run_with_worker_controls(
                record=record,
                default_duration_seconds=settings.agent_runtime.max_duration_seconds,
                operation=lambda: dispatcher.dispatch(
                    record=record, settings=settings, progress_recorder=progress_recorder
                ),
                is_cancel_requested=lambda: _is_cancel_requested_safely(
                    queue, record.run_id, durable_source=state_store
                ),
            )
        await usage_meter.flush_audit_events()
        metered_usage = usage_meter.snapshot()
        if metered_usage is not None:
            result.usage = metered_usage
        _apply_result(record, result)
    except AppPublicationSnapshotMismatch:
        await _requeue_app_run_instead_of_closeout(
            queue,
            record,
            worker_instance_id=worker_instance_id,
        )
        return None
    except Exception as exc:  # noqa: BLE001 - App Provider/Runtime 错误必须写回 Run
        _log_workflow_execution_error(record, exc, platform="App")
        record.status = AgentRunQueueStatus.FAILED
        record.finished_at = datetime.now(UTC)
        record.termination_reason = TerminationReason.ERROR.value
        record.error_message = str(exc)
        record.error_category = ErrorCategory.INFRASTRUCTURE.value
        _apply_workflow_control_error(record, exc)
    if await _is_cancel_requested_safely(
        queue,
        record.run_id,
        durable_source=state_store,
    ):
        await _project_run_canceled_and_cleanup(queue=queue, record=record)
        await _finish_app_terminal_run(
            queue=queue,
            record=record,
            state_store=state_store,
            progress_recorder=progress_recorder,
            final_report_generator=final_report_generator,
            task_finalization_service=None,
            inspection_work_items_enabled=False,
            legacy_report_enabled=False,
        )
        return
    # exploration_only 只保存 App Runtime/Closure 生成的探索证据；正式质检
    # 下游的 publisher、Inspection Work、Rule Execution、Completion Gate 和
    # report 都必须由这个持久化执行模式同时关闭。
    legacy_report_enabled = formal_inspection_enabled
    try:
        if (
            formal_inspection_enabled
            and record.termination_reason
            in {
                TerminationReason.COMPLETED.value,
                TerminationReason.INCOMPLETE.value,
            }
            and rule_engine_configuration_guard is not None
        ):
            # 页面探索及证据关闭已经完成后，才校验正式规则链路。这样规则配置
            # 阻断不会改变 `/v2/tasks` 与 `/v2/exploration-tasks` 的探索效果。
            await rule_engine_configuration_guard.ensure_ready_for(execution_mode)
        if formal_inspection_enabled and record.termination_reason in {
            TerminationReason.COMPLETED.value,
            TerminationReason.INCOMPLETE.value,
        }:
            if sealed_inspection_publisher is None:
                raise RuntimeError("App Worker 未配置 sealed Evidence Index 质检发布器")
            published = await sealed_inspection_publisher.publish(
                task_id=record.task_id,
                agent_run_id=record.run_id,
            )
            usage = dict(record.usage or {})
            summary = published.to_dict()
            usage["app_evidence_index_id"] = str(summary.get("evidence_index_id") or "")
            usage["app_evidence_index_outcome"] = str(
                summary.get("evidence_index_outcome") or summary.get("outcome") or ""
            )
            usage["app_exploration_outcome"] = str(summary.get("outcome") or "")
            usage["app_inspection_publication_id"] = str(summary.get("publication_id") or "")
            usage["app_inspection_publication_created"] = bool(summary.get("publication_created"))
            for source_key, usage_key in (
                ("indexed_capture_count", "app_indexed_capture_count"),
                ("publication_entry_count", "app_publication_entry_count"),
                ("review_entry_count", "app_publication_review_entry_count"),
                ("published_capture_count", "app_inspection_capture_count"),
                ("skipped_capture_count", "app_inspection_skipped_capture_count"),
                ("published_work_item_count", "app_inspection_work_item_count"),
                ("created_work_item_count", "app_inspection_created_work_item_count"),
            ):
                usage[usage_key] = int(summary.get(source_key) or 0)
            record.usage = usage
            if app_publication_repository is None:
                raise RuntimeError("App Worker 未配置 Publication 完成屏障仓储")
            publication_summary = await app_publication_repository.await_task_terminal(
                task_id=record.task_id
            )
            usage = dict(record.usage or {})
            usage.update(
                {
                    "app_inspection_publication_count": publication_summary.total_count,
                    "app_publication_expected_entry_count": (
                        publication_summary.expected_entry_count
                    ),
                    "app_publication_terminal_entry_count": (
                        publication_summary.terminal_entry_count
                    ),
                    "app_inspection_work_item_count": (publication_summary.linked_work_item_count),
                    "app_excluded_active_session_rule_count": (
                        publication_summary.explicit_exclusion_count
                    ),
                    "app_inspection_publication_failed_count": (publication_summary.failed_count),
                    "app_inspection_publication_needs_review_count": (
                        publication_summary.needs_review_count
                    ),
                }
            )
            record.usage = usage
            if publication_summary.expected_entry_count != publication_summary.terminal_entry_count:
                raise RuntimeError("App Publication Entry 完成数量不一致")
            if publication_summary.failed_count or publication_summary.needs_review_count:
                record.status = AgentRunQueueStatus.FAILED
                record.termination_reason = TerminationReason.INCOMPLETE.value
                record.execution_outcome = ExecutionOutcome.NEEDS_REVIEW.value
                if not record.error_message:
                    record.error_message = "App 规则 Publication 存在失败或需复核条目"
            if summary.get("outcome") == "evidence_insufficient":
                _mark_app_evidence_insufficient(record)
        if await _finish_standard_if_cancel_requested(
            queue=queue,
            record=record,
            state_store=state_store,
            progress_recorder=progress_recorder,
            final_report_generator=final_report_generator,
        ):
            return
        if formal_inspection_enabled:
            await _drain_snapshot_inspection_work(
                record,
                settings=settings,
                worker_instance_id=worker_instance_id,
            )
            if await _finish_standard_if_cancel_requested(
                queue=queue,
                record=record,
                state_store=state_store,
                progress_recorder=progress_recorder,
                final_report_generator=final_report_generator,
            ):
                return
            await _finalize_rule_executions(record, rule_execution_writer)
            if await _finish_standard_if_cancel_requested(
                queue=queue,
                record=record,
                state_store=state_store,
                progress_recorder=progress_recorder,
                final_report_generator=final_report_generator,
            ):
                return
            await _finalize_persisted_inspection_work(record, settings=settings)
    except AppPublicationSnapshotMismatch:
        await _requeue_app_run_instead_of_closeout(
            queue,
            record,
            worker_instance_id=worker_instance_id,
        )
        return None
    except Exception as exc:
        logger.exception("run_id=%s App Workflow 收口失败", record.run_id)
        record.status = AgentRunQueueStatus.FAILED
        record.finished_at = datetime.now(UTC)
        record.termination_reason = TerminationReason.ERROR.value
        record.error_message = str(exc)
        record.error_category = ErrorCategory.UNKNOWN.value
        record.execution_outcome = ExecutionOutcome.NEEDS_REVIEW.value
        legacy_report_enabled = False
    record.worker_instance_id = None
    if defer_terminal_finish:
        return _DeferredAppRunFinish(
            progress_recorder=progress_recorder,
            legacy_report_enabled=legacy_report_enabled,
            formal_inspection_enabled=formal_inspection_enabled,
        )
    await _finish_app_terminal_run(
        queue=queue,
        record=record,
        state_store=state_store,
        progress_recorder=progress_recorder,
        final_report_generator=final_report_generator,
        task_finalization_service=(
            task_finalization_service if formal_inspection_enabled else None
        ),
        inspection_work_items_enabled=formal_inspection_enabled,
        legacy_report_enabled=legacy_report_enabled,
    )
    return None


def _mark_app_evidence_insufficient(record: AgentRunRecord) -> None:
    """缺视觉等业务证据缺口必须进入复核，不能伪装为技术成功。"""

    record.status = AgentRunQueueStatus.FAILED
    record.termination_reason = TerminationReason.INCOMPLETE.value
    record.execution_outcome = ExecutionOutcome.NEEDS_REVIEW.value
    record.error_category = None
    if not record.error_message:
        record.error_message = "App 探索证据不足"
    notice = "App 探索证据不足，任务已转人工复核。"
    if record.final_text:
        if notice not in record.final_text:
            record.final_text = f"{record.final_text}\n\n{notice}"
    else:
        record.final_text = notice


async def _process_web_v2_one(
    queue: RedisAgentRunQueue,
    record: AgentRunRecord,
    *,
    settings: Settings,
    state_store: AgentRunStateStore,
    dispatcher: AppWorkflowDispatcher,
    rule_execution_writer: RuleExecutionWriter | None = None,
    final_report_generator: FinalReportGenerator | None = None,
    worker_instance_id: str | None = None,
    task_finalization_service: TaskFinalizationService | None = None,
    web_inspection_reconciliation: Any | None = None,
    web_formal_session_closer: Any | None = None,
    web_formal_terminal_ledger_finalizer: Any | None = None,
) -> None:
    """按锁定执行模式进入 Web 探索收口或正式质检收口。"""

    full_inspection = (
        task_execution_mode_from_config(record.task_config)
        is TaskExecutionMode.FULL_INSPECTION
    )
    if full_inspection and any(
        dependency is None
        for dependency in (
            web_inspection_reconciliation,
            task_finalization_service,
            final_report_generator,
            web_formal_session_closer,
        )
    ):
        raise RuntimeError("Web full_inspection Worker 正式收口依赖未完整注入")

    progress_recorder = build_task_progress_recorder(
        task_id=record.task_id,
        agent_run_id=record.run_id,
        task_config=record.task_config,
    )
    if progress_recorder is not None:
        await progress_recorder.on_run_started()
    try:
        result = await run_with_worker_controls(
            record=record,
            default_duration_seconds=settings.agent_runtime.max_duration_seconds,
            operation=lambda: dispatcher.dispatch_web_v2(
                record=record, settings=settings, progress_recorder=progress_recorder
            ),
            is_cancel_requested=lambda: _is_cancel_requested_safely(
                queue, record.run_id, durable_source=state_store
            ),
        )
        _apply_result(record, result)
    except Exception as exc:  # noqa: BLE001 - Web Provider/Runtime 错误必须写回 Run
        _log_workflow_execution_error(record, exc, platform="Web V2")
        record.status = AgentRunQueueStatus.FAILED
        record.finished_at = datetime.now(UTC)
        record.termination_reason = TerminationReason.ERROR.value
        record.error_message = str(exc)
        record.error_category = ErrorCategory.INFRASTRUCTURE.value
        _apply_workflow_control_error(record, exc)
        record.execution_outcome = ExecutionOutcome.NEEDS_REVIEW.value
    record.worker_instance_id = None
    if full_inspection:
        if await _finish_web_formal_if_cancel_requested(
            queue=queue,
            record=record,
            state_store=state_store,
            progress_recorder=progress_recorder,
            web_formal_session_closer=web_formal_session_closer,
            web_formal_terminal_ledger_finalizer=(
                web_formal_terminal_ledger_finalizer
            ),
        ):
            return
        await _finish_web_formal_inspection_run(
            queue=queue,
            record=record,
            settings=settings,
            state_store=state_store,
            progress_recorder=progress_recorder,
            final_report_generator=final_report_generator,
            task_finalization_service=task_finalization_service,
            reconciliation=web_inspection_reconciliation,
            worker_instance_id=worker_instance_id,
            web_formal_session_closer=web_formal_session_closer,
            web_formal_terminal_ledger_finalizer=(
                web_formal_terminal_ledger_finalizer
            ),
        )
        return
    await _finish_web_terminal_run(
        queue=queue,
        record=record,
        state_store=state_store,
        progress_recorder=progress_recorder,
        final_report_generator=final_report_generator,
    )


async def _finish_web_formal_inspection_run(
    *,
    queue: RedisAgentRunQueue,
    record: AgentRunRecord,
    settings: Settings,
    state_store: AgentRunStateStore,
    progress_recorder: TaskProgressRecorder | None,
    final_report_generator: FinalReportGenerator | None,
    task_finalization_service: TaskFinalizationService | None,
    reconciliation: Any,
    worker_instance_id: str | None,
    web_formal_session_closer: Any | None = None,
    web_formal_terminal_ledger_finalizer: Any | None = None,
) -> None:
    """先以 Web 持久化事实完成交接、快照和 Gap，再进入统一 Gate。"""

    if record.task_id is None or task_finalization_service is None:
        raise RuntimeError("Web 正式收口缺少 task_id 或 Task Finalization Service")
    snapshot = await task_finalization_service.get(task_id=record.task_id)
    if snapshot is None or snapshot.agent_run_id not in {None, record.run_id}:
        raise RuntimeError("Web 正式收口任务身份与当前 main Run 不匹配")
    command = ReconcileWebInspectionCommand(
        task_id=record.task_id,
        agent_run_id=record.run_id,
        finalization_epoch=snapshot.finalization_epoch,
    )
    # Task5 对账可能因 Publication/active-session 工作尚未排空而中断；
    # 先落 main Run 终态，后台恢复器才能按 task/run/epoch 重新取得权威输入。
    try:
        await state_store.record_run_finished(record)
    except AgentRunCancellationConflict:
        logger.info("run_id=%s 持久化取消标记阻止 Web 正式交接", record.run_id)
        await _finish_web_formal_canceled_run(
            queue=queue,
            record=record,
            state_store=state_store,
            progress_recorder=progress_recorder,
            web_formal_session_closer=web_formal_session_closer,
            web_formal_terminal_ledger_finalizer=(
                web_formal_terminal_ledger_finalizer
            ),
        )
        return
    result = await reconciliation.reconcile(command)
    await _reconcile_run_llm_usage_from_events(record)
    if result.status == "waiting_for_run_terminal":
        await _drain_snapshot_inspection_work(
            record,
            settings=settings,
            worker_instance_id=worker_instance_id,
        )
        result = await reconciliation.reconcile(command)
    if result.status == "gate_ready":
        _apply_web_reconciliation_execution_status(record, result)
        await _finish_standard_terminal_run(
        queue=queue,
        record=record,
        state_store=state_store,
        progress_recorder=progress_recorder,
        final_report_generator=final_report_generator,
        task_finalization_service=task_finalization_service,
        inspection_work_items_enabled=True,
            legacy_report_enabled=True,
        )
        return
    if result.status == "already_terminal":
        projected = await _project_authoritative_finished_run(
            queue,
            state_store,
            run_id=record.run_id,
            reason="Web 正式收口发现任务已封存",
        )
        if not projected:
            await _save_queue_projection(queue, record)
        return
    if result.status == "needs_review":
        record.execution_outcome = ExecutionOutcome.NEEDS_REVIEW.value
    elif result.status == "failed":
        record.status = AgentRunQueueStatus.FAILED
        record.execution_outcome = ExecutionOutcome.NEEDS_REVIEW.value
    await _save_queue_projection(queue, record)
    await _finish_progress(progress_recorder, record)


async def _process_h5_v2_one(
    queue: RedisAgentRunQueue,
    record: AgentRunRecord,
    *,
    settings: Settings,
    state_store: AgentRunStateStore,
    dispatcher: AppWorkflowDispatcher,
    rule_execution_writer: RuleExecutionWriter | None = None,
    final_report_generator: FinalReportGenerator | None = None,
    worker_instance_id: str | None = None,
    task_finalization_service: TaskFinalizationService | None = None,
    h5_evidence_handoff: H5EvidenceHandoffPort | None = None,
    h5_task_gate_reader: H5LiveInspectionTaskGateReader | None = None,
    h5_publication_repository: PostgresH5InspectionPublicationRepository | None = None,
    workflow_route: WorkflowRoute = WorkflowRoute.H5_V2,
) -> None:
    """执行浏览器 V2 Runtime，并复用正式规则质检收口。"""

    if workflow_route not in {WorkflowRoute.H5_V2, WorkflowRoute.WEB_V2}:
        raise ValueError("浏览器 V2 正式质检只接受 H5_V2 或 WEB_V2 路由")
    platform_label = "Web" if workflow_route is WorkflowRoute.WEB_V2 else "H5"
    progress_recorder = build_task_progress_recorder(
        task_id=record.task_id,
        agent_run_id=record.run_id,
        task_config=record.task_config,
    )
    if progress_recorder is not None:
        await progress_recorder.on_run_started()
    try:
        if workflow_route is WorkflowRoute.H5_V2:
            build_h5_task_runtime_settings(
                task_config=record.task_config,
                loop_guard=record.loop_guard,
                page_sampling_policy=record.page_sampling_policy,
                defaults=settings.agent_runtime,
            )
        with bind_run_llm_usage_meter() as usage_meter:
            if workflow_route is WorkflowRoute.WEB_V2:
                execute = dispatcher.dispatch_web_v2
            else:
                execute = dispatcher.dispatch_h5_v2
            result = await run_with_worker_controls(
                record=record,
                default_duration_seconds=settings.agent_runtime.max_duration_seconds,
                operation=lambda: execute(
                    record=record,
                    settings=settings,
                    progress_recorder=progress_recorder,
                ),
                is_cancel_requested=lambda: _is_cancel_requested_safely(
                    queue, record.run_id, durable_source=state_store,
                    h5=workflow_route is WorkflowRoute.H5_V2,
                ),
            )
        await usage_meter.flush_audit_events()
        metered_usage = usage_meter.snapshot()
        if metered_usage is not None:
            result.usage = metered_usage
        _apply_result(record, result)
    except Exception as exc:  # noqa: BLE001 - H5 Provider/Runtime 错误必须写回 Run
        _log_workflow_execution_error(record, exc, platform=f"{platform_label} V2")
        record.status = AgentRunQueueStatus.FAILED
        record.finished_at = datetime.now(UTC)
        record.termination_reason = TerminationReason.ERROR.value
        record.error_message = str(exc)
        record.error_category = ErrorCategory.INFRASTRUCTURE.value
        _apply_workflow_control_error(record, exc)
    await _attach_h5_v2_exploration_action_trace(record)
    if await _mark_h5_run_canceled_if_requested(
        queue=queue,
        record=record,
        state_store=state_store,
    ):
        record.worker_instance_id = None
        await _finish_h5_terminal_run(
            queue=queue,
            record=record,
            state_store=state_store,
            progress_recorder=progress_recorder,
            final_report_generator=final_report_generator,
            task_finalization_service=None,
            inspection_work_items_enabled=False,
            legacy_report_enabled=False,
        )
        return
    inspection_work_items_enabled = False
    legacy_report_enabled = False
    finalization_service: TaskFinalizationService | None = None
    review_terminal = is_review_termination_reason(record.termination_reason)
    if review_terminal:
        record.execution_outcome = ExecutionOutcome.NEEDS_REVIEW.value
    if (
        record.termination_reason
        not in {
            TerminationReason.COMPLETED.value,
            TerminationReason.INCOMPLETE.value,
        }
        and not review_terminal
    ):
        if await _finish_standard_if_cancel_requested(
            queue=queue,
            record=record,
            state_store=state_store,
            progress_recorder=progress_recorder,
            final_report_generator=final_report_generator,
        ):
            return
        record.worker_instance_id = None
        salvage_completed = await _handoff_failed_h5_run_evidence(
            record,
            queue=queue,
            state_store=state_store,
            settings=settings,
            worker_instance_id=worker_instance_id,
            rule_execution_writer=rule_execution_writer,
            h5_evidence_handoff=h5_evidence_handoff,
            h5_task_gate_reader=h5_task_gate_reader,
            h5_publication_repository=h5_publication_repository,
        )
        salvage_finalization_service = (
            _formal_task_finalization_service(record, task_finalization_service)
            if salvage_completed
            else None
        )
        salvage_uses_formal_gate = salvage_finalization_service is not None
        if await _finish_h5_completely_failed_run(
            queue=queue, record=record, settings=settings, state_store=state_store,
            progress_recorder=progress_recorder, worker_instance_id=worker_instance_id,
            task_finalization_service=task_finalization_service,
            work_drained=salvage_completed,
        ):
            return
        await _finish_h5_terminal_run(
            queue=queue,
            record=record,
            state_store=state_store,
            progress_recorder=progress_recorder,
            final_report_generator=final_report_generator,
            task_finalization_service=salvage_finalization_service,
            inspection_work_items_enabled=salvage_uses_formal_gate,
            legacy_report_enabled=salvage_uses_formal_gate,
        )
        return
    if record.execution_outcome == ExecutionOutcome.NEEDS_REVIEW.value and is_h5_auth_review_error(
        record.error_message
    ):
        # 认证降级时保留游客证据，但认证前/中间证据不可交接给 Judge。
        record.worker_instance_id = None
        await _finish_terminal_run(
            queue=queue,
            record=record,
            state_store=state_store,
            progress_recorder=progress_recorder,
            final_report_generator=final_report_generator,
            task_finalization_service=None,
            inspection_work_items_enabled=False,
            legacy_report_enabled=False,
        )
        return
    try:
        if record.task_id is None:
            raise ValueError(f"{platform_label} V2 证据交接必须绑定 task_id")
        if await _mark_h5_run_canceled_if_requested(
            queue=queue,
            record=record,
            state_store=state_store,
        ):
            record.worker_instance_id = None
            await _finish_h5_terminal_run(
                queue=queue,
                record=record,
                state_store=state_store,
                progress_recorder=progress_recorder,
                final_report_generator=final_report_generator,
                task_finalization_service=None,
                inspection_work_items_enabled=False,
                legacy_report_enabled=False,
            )
            return
        if h5_evidence_handoff is None:
            raise RuntimeError(f"{platform_label} V2 Worker 未配置证据交接服务")
        execution_mode, notification_mode = await _resolve_locked_h5_task_modes(
            record,
            gate_reader=h5_task_gate_reader,
        )
        handoff_summary = await h5_evidence_handoff.prepare(
            task_id=record.task_id,
            agent_run_id=record.run_id,
            execution_mode=execution_mode,
            notification_mode=notification_mode,
        )
        if await _finish_standard_if_cancel_requested(
            queue=queue,
            record=record,
            state_store=state_store,
            progress_recorder=progress_recorder,
            final_report_generator=final_report_generator,
        ):
            return
        usage = dict(record.usage or {})
        usage.update(
            {
                "h5_evidence_ready": handoff_summary.evidence_ready,
                "h5_judgeable_capture_count": handoff_summary.judgeable_capture_count,
                "h5_page_type_count": len(handoff_summary.page_types),
                "h5_published_work_item_count": (handoff_summary.published_work_item_count),
                "h5_evidence_total_count": handoff_summary.total_count,
                "h5_evidence_pass_count": handoff_summary.pass_count,
                "h5_evidence_insufficient_count": handoff_summary.insufficient_count,
                "h5_evidence_failed_count": handoff_summary.failed_count,
                "h5_inspection_publication_count": handoff_summary.publication_count,
            }
        )
        if execution_mode is TaskExecutionMode.FULL_INSPECTION:
            usage["h5_excluded_active_session_rule_count"] = (
                handoff_summary.excluded_active_session_rule_count
            )
        record.usage = usage
        if execution_mode is TaskExecutionMode.FULL_INSPECTION:
            if notification_mode is H5InspectionNotificationMode.ENFORCED:
                publication_repository = h5_publication_repository or (
                    PostgresH5InspectionPublicationRepository(
                        max_attempts=settings.inspection_work_items.max_attempts,
                        retry_delay_seconds=max(
                            1, int(settings.inspection_work_items.retry_base_seconds)
                        ),
                    )
                )
                publication_summary = await publication_repository.await_task_terminal(
                    task_id=record.task_id,
                    minimum_total_count=handoff_summary.publication_count,
                )
                handoff_summary = H5EvidenceHandoffSummary(
                    evidence_ready=handoff_summary.evidence_ready,
                    judgeable_capture_count=handoff_summary.judgeable_capture_count,
                    page_types=handoff_summary.page_types,
                    published_work_item_count=(publication_summary.published_work_item_count),
                    excluded_active_session_rule_count=(
                        publication_summary.excluded_active_session_rule_count
                    ),
                    total_count=handoff_summary.total_count,
                    pass_count=handoff_summary.pass_count,
                    insufficient_count=handoff_summary.insufficient_count,
                    failed_count=(handoff_summary.failed_count + publication_summary.failed_count),
                    publication_count=publication_summary.total_count,
                )
                usage = dict(record.usage or {})
                usage.update(
                    {
                        "h5_published_work_item_count": (
                            publication_summary.published_work_item_count
                        ),
                        "h5_excluded_active_session_rule_count": (
                            publication_summary.excluded_active_session_rule_count
                        ),
                        "h5_inspection_publication_failed_count": (
                            publication_summary.failed_count
                        ),
                        "h5_inspection_publication_needs_review_count": (
                            publication_summary.needs_review_count
                        ),
                    }
                )
                record.usage = usage
            await _drain_snapshot_inspection_work(
                record,
                settings=settings,
                worker_instance_id=worker_instance_id,
            )
            if await _finish_standard_if_cancel_requested(
                queue=queue,
                record=record,
                state_store=state_store,
                progress_recorder=progress_recorder,
                final_report_generator=final_report_generator,
            ):
                return
            await _finalize_rule_executions(record, rule_execution_writer)
            if await _finish_standard_if_cancel_requested(
                queue=queue,
                record=record,
                state_store=state_store,
                progress_recorder=progress_recorder,
                final_report_generator=final_report_generator,
            ):
                return
            await _finalize_persisted_inspection_work(record, settings=settings)
            if handoff_summary.excluded_active_session_rule_count > 0:
                excluded_count = handoff_summary.excluded_active_session_rule_count
                record.execution_outcome = "needs_review"
                notice = (
                    f"Session 关闭后有 {excluded_count} 条活动会话规则无法基于"
                    "不可变快照执行，任务已转人工复核。"
                )
                record.final_text = (
                    f"{record.final_text}\n\n{notice}" if record.final_text else notice
                )
            if handoff_summary.insufficient_count > 0:
                record.status = AgentRunQueueStatus.FAILED
                record.termination_reason = TerminationReason.INCOMPLETE.value
                record.execution_outcome = "needs_review"
                notice = (
                    f"部分 {platform_label} 预期动作证据不足，"
                    "已保留合格快照并转人工复核。"
                )
                record.final_text = (
                    f"{record.final_text}\n\n{notice}" if record.final_text else notice
                )
            if handoff_summary.failed_count > 0:
                record.status = AgentRunQueueStatus.FAILED
                record.termination_reason = TerminationReason.ERROR.value
                record.execution_outcome = "failed"
                record.error_message = "evidence_integrity_failed"
            formal_closure_blocked = (
                not handoff_summary.evidence_ready
                or handoff_summary.insufficient_count > 0
                or handoff_summary.failed_count > 0
                or handoff_summary.excluded_active_session_rule_count > 0
            )
            if (
                not handoff_summary.evidence_ready
                and handoff_summary.insufficient_count == 0
                and handoff_summary.failed_count == 0
                and handoff_summary.excluded_active_session_rule_count == 0
            ):
                record.status = AgentRunQueueStatus.FAILED
                record.termination_reason = TerminationReason.INCOMPLETE.value
                record.execution_outcome = "needs_review"
                notice = f"{platform_label} 证据库存尚未就绪，任务已转人工复核。"
                record.final_text = (
                    f"{record.final_text}\n\n{notice}" if record.final_text else notice
                )
            if formal_closure_blocked:
                # 合格快照仍可完成已发布检查；非通过库存只能生成 preview
                # 并经 Completion Gate 转复核，不能让任务长期停在 running。
                legacy_report_enabled = True
                finalization_service = task_finalization_service
            else:
                legacy_report_enabled = True
                finalization_service = task_finalization_service
            inspection_work_items_enabled = True
    except Exception as exc:
        logger.exception("run_id=%s %s V2 Workflow 收口失败", record.run_id, platform_label)
        record.status = AgentRunQueueStatus.FAILED
        record.finished_at = datetime.now(UTC)
        record.termination_reason = TerminationReason.ERROR.value
        record.error_message = str(exc)
        record.error_category = ErrorCategory.UNKNOWN.value
        inspection_work_items_enabled = False
        legacy_report_enabled = False
        finalization_service = None
    record.worker_instance_id = None
    if await _mark_h5_run_canceled_if_requested(
        queue=queue,
        record=record,
        state_store=state_store,
    ):
        inspection_work_items_enabled = False
        legacy_report_enabled = False
        finalization_service = None
    if await _finish_h5_completely_failed_run(
        queue=queue, record=record, settings=settings, state_store=state_store,
        progress_recorder=progress_recorder, worker_instance_id=worker_instance_id,
        task_finalization_service=task_finalization_service,
        work_drained=inspection_work_items_enabled,
    ):
        return
    await _finish_h5_terminal_run(
        queue=queue,
        record=record,
        state_store=state_store,
        progress_recorder=progress_recorder,
        final_report_generator=final_report_generator,
        task_finalization_service=finalization_service,
        inspection_work_items_enabled=inspection_work_items_enabled,
        legacy_report_enabled=legacy_report_enabled,
    )


async def _finish_h5_completely_failed_run(
    *, queue: RedisAgentRunQueue, record: AgentRunRecord, settings: Settings,
    state_store: AgentRunStateStore, progress_recorder: TaskProgressRecorder | None,
    worker_instance_id: str | None, task_finalization_service: TaskFinalizationService | None,
    work_drained: bool,
) -> bool:
    """排空后的完全技术失败写入 FAILED；已有目标页面或业务成果仍走原复核路径。"""
    if (
        record.status is not AgentRunQueueStatus.FAILED
        or not _is_locked_native_h5_v2_run_record(record)
        or task_execution_mode_from_config(record.task_config) is not TaskExecutionMode.FULL_INSPECTION
        or record.task_id is None
        or task_finalization_service is None
        or not callable(getattr(task_finalization_service, "terminate", None))
    ):
        return False
    if not work_drained:
        await _drain_snapshot_inspection_work(
            record, settings=settings, worker_instance_id=worker_instance_id,
        )
        await _finalize_rule_executions(record, None)
        await _finalize_persisted_inspection_work(record, settings=settings)
    if await _mark_h5_run_canceled_if_requested(
        queue=queue, record=record, state_store=state_store,
    ):
        await _finish_h5_terminal_run(
            queue=queue, record=record, state_store=state_store,
            progress_recorder=progress_recorder, final_report_generator=None,
            task_finalization_service=None,
            inspection_work_items_enabled=False, legacy_report_enabled=False,
        )
        return True
    failure_facts = PostgresH5RunFailureFactsRepository()
    facts = await failure_facts.fetch(
        task_id=record.task_id, agent_run_id=record.run_id,
    )
    if facts is None:
        raise RuntimeError("H5 失败收口缺少当前 Task/Run 事实")
    reason = facts.technical_failure_reason
    if not reason and record.termination_reason == TerminationReason.ERROR.value:
        reason = "H5_RUNTIME_FAILED"
    if not reason or facts.has_partial_results:
        return False
    await failure_facts.assert_drained(task_id=record.task_id, agent_run_id=record.run_id)
    record.status = AgentRunQueueStatus.FAILED
    record.termination_reason = TerminationReason.ERROR.value
    record.execution_outcome = "failed"
    record.error_category = record.error_category or ErrorCategory.INFRASTRUCTURE.value
    record.error_message = record.error_message or reason
    record.final_report = None
    record.final_text = "H5 目标页面未形成可质检成果，任务执行失败；诊断证据已保留。"
    await _reconcile_run_llm_usage_from_events(record)
    await state_store.record_run_finished(record)
    await terminate_failed_h5_task(
        task_finalization_service, task_id=record.task_id,
        agent_run_id=record.run_id, reason_code=reason,
    )
    await _save_queue_projection(queue, record)
    await _finish_progress(progress_recorder, record)
    return True


async def _handoff_failed_h5_run_evidence(
    record: AgentRunRecord,
    *,
    queue: RedisAgentRunQueue,
    state_store: AgentRunStateStore,
    settings: Settings,
    worker_instance_id: str | None,
    rule_execution_writer: RuleExecutionWriter | None,
    h5_evidence_handoff: H5EvidenceHandoffPort | None,
    h5_task_gate_reader: H5LiveInspectionTaskGateReader | None,
    h5_publication_repository: PostgresH5InspectionPublicationRepository | None,
) -> bool:
    """异常收尾的 H5 Run 仍交接已通过门禁的证据并完成判定收口。

    探索单点失败不应连坐已留存的合格证据：交接、判定与工作项门禁照常执行，
    但 Run 的失败结论（status/termination_reason/error_message）绝不被改写，
    正式报告也不在此路径封存。兜底自身失败只记录日志，不影响原始终态。
    """
    if record.task_id is None or h5_evidence_handoff is None:
        return False
    if record.status is not AgentRunQueueStatus.FAILED:
        return False
    try:
        execution_mode, notification_mode = await _resolve_locked_h5_task_modes(
            record,
            gate_reader=h5_task_gate_reader,
        )
        handoff_summary = await h5_evidence_handoff.prepare(
                task_id=record.task_id,
                agent_run_id=record.run_id,
            execution_mode=execution_mode,
            notification_mode=notification_mode,
        )
        usage = dict(record.usage or {})
        usage.update(
            {
                "h5_evidence_ready": handoff_summary.evidence_ready,
                "h5_judgeable_capture_count": handoff_summary.judgeable_capture_count,
                "h5_page_type_count": len(handoff_summary.page_types),
                "h5_published_work_item_count": (handoff_summary.published_work_item_count),
                "h5_evidence_total_count": handoff_summary.total_count,
                "h5_evidence_pass_count": handoff_summary.pass_count,
                "h5_evidence_insufficient_count": handoff_summary.insufficient_count,
                "h5_evidence_failed_count": handoff_summary.failed_count,
            }
        )
        if execution_mode is TaskExecutionMode.FULL_INSPECTION:
            usage["h5_excluded_active_session_rule_count"] = (
                handoff_summary.excluded_active_session_rule_count
            )
        record.usage = usage
        if execution_mode is not TaskExecutionMode.FULL_INSPECTION:
            return True
        if notification_mode is H5InspectionNotificationMode.ENFORCED:
            publication_repository = h5_publication_repository or (
                PostgresH5InspectionPublicationRepository(
                    max_attempts=settings.inspection_work_items.max_attempts,
                    retry_delay_seconds=max(
                        1, int(settings.inspection_work_items.retry_base_seconds)
                    ),
                )
            )
            publication_summary = await publication_repository.await_task_terminal(
                task_id=record.task_id,
                minimum_total_count=handoff_summary.publication_count,
            )
            usage = dict(record.usage or {})
            usage.update(
                {
                    "h5_published_work_item_count": (publication_summary.published_work_item_count),
                    "h5_excluded_active_session_rule_count": (
                        publication_summary.excluded_active_session_rule_count
                    ),
                    "h5_inspection_publication_count": publication_summary.total_count,
                    "h5_inspection_publication_failed_count": (publication_summary.failed_count),
                    "h5_inspection_publication_needs_review_count": (
                        publication_summary.needs_review_count
                    ),
                }
            )
            record.usage = usage
        if await _is_cancel_requested_safely(
            queue,
            record.run_id,
            durable_source=state_store,
        ):
            return False
        await _drain_snapshot_inspection_work(
            record,
            settings=settings,
            worker_instance_id=worker_instance_id,
        )
        await _finalize_rule_executions(record, rule_execution_writer)
        await _finalize_persisted_inspection_work(record, settings=settings)
        return True
    except Exception:  # noqa: BLE001 - 兜底交接失败不能改变原始失败结论
        logger.exception("run_id=%s 异常收尾的 H5 证据交接兜底未完成", record.run_id)
        return False


async def _resolve_locked_h5_task_modes(
    record: AgentRunRecord,
    *,
    gate_reader: H5LiveInspectionTaskGateReader | None,
) -> tuple[TaskExecutionMode, H5InspectionNotificationMode]:
    """生产路径以 PostgreSQL 任务快照为准，并拒绝队列投影与其冲突。"""

    projected_execution_mode = task_execution_mode_from_config(record.task_config)
    projected_notification_mode = parse_h5_inspection_notification_mode(record.task_config)
    if gate_reader is None:
        # 仅供显式注入依赖的单元测试入口；生产组合根始终提供 PostgreSQL Reader。
        return projected_execution_mode, projected_notification_mode
    if record.task_id is None:
        raise RuntimeError("H5 任务快照读取缺少 task_id")
    gate = await gate_reader.load_task_gate(task_id=record.task_id)
    if gate is None or gate.target_platform.casefold() not in {"h5", "web"}:
        raise RuntimeError("H5 任务锁定快照不存在或平台不一致")
    locked_execution_mode = task_execution_mode_from_config(gate.task_config)
    locked_notification_mode = parse_h5_inspection_notification_mode(gate.task_config)
    if projected_execution_mode is not locked_execution_mode:
        raise RuntimeError("H5 队列执行模式与 PostgreSQL 任务快照不一致")
    projected_mode_value = (record.task_config or {}).get("h5_inspection_notification_mode")
    if (
        projected_mode_value is not None
        and projected_notification_mode is not locked_notification_mode
    ):
        raise RuntimeError("H5 队列通知模式与 PostgreSQL 任务快照不一致")
    return locked_execution_mode, locked_notification_mode


async def _finish_standard_if_cancel_requested(
    *,
    queue: RedisAgentRunQueue,
    record: AgentRunRecord,
    state_store: AgentRunStateStore,
    progress_recorder: TaskProgressRecorder | None,
    final_report_generator: FinalReportGenerator | None,
) -> bool:
    """副作用边界确认取消后立即写短期投影并关闭通用 Run。"""

    if not await _is_cancel_requested_safely(
        queue,
        record.run_id,
        durable_source=state_store,
    ):
        return False
    await _project_run_canceled_and_cleanup(queue=queue, record=record)
    await _finish_standard_terminal_run(
        queue=queue,
        record=record,
        state_store=state_store,
        progress_recorder=progress_recorder,
        final_report_generator=final_report_generator,
        task_finalization_service=None,
        inspection_work_items_enabled=False,
        legacy_report_enabled=False,
    )
    return True


async def _finish_web_formal_if_cancel_requested(
    *,
    queue: RedisAgentRunQueue,
    record: AgentRunRecord,
    state_store: AgentRunStateStore,
    progress_recorder: TaskProgressRecorder | None,
    web_formal_session_closer: Any,
    web_formal_terminal_ledger_finalizer: Any | None = None,
) -> bool:
    """正式 Web 在取消终态前先幂等关闭持久 Session 与浏览器。"""

    if not await _is_cancel_requested_safely(
        queue,
        record.run_id,
        durable_source=state_store,
    ):
        return False
    await _finish_web_formal_canceled_run(
        queue=queue,
        record=record,
        state_store=state_store,
        progress_recorder=progress_recorder,
        web_formal_session_closer=web_formal_session_closer,
        web_formal_terminal_ledger_finalizer=(
            web_formal_terminal_ledger_finalizer
        ),
    )
    return True


async def _finish_web_formal_canceled_run(
    *,
    queue: RedisAgentRunQueue,
    record: AgentRunRecord,
    state_store: AgentRunStateStore,
    progress_recorder: TaskProgressRecorder | None,
    web_formal_session_closer: Any,
    web_formal_terminal_ledger_finalizer: Any | None = None,
) -> None:
    """先收束正式账本并释放 Web Session，再原子提交 Run/Task canceled。"""

    await _project_run_canceled_and_cleanup(queue=queue, record=record)
    try:
        await _terminalize_web_formal_ledgers_for_cancel(
            record=record,
            finalizer=web_formal_terminal_ledger_finalizer,
        )
        await _close_web_formal_session_for_cancel(
            record=record,
            web_formal_session_closer=web_formal_session_closer,
        )
    except Exception:  # noqa: BLE001 - 收束失败保留取消恢复记录，禁止进入 Gate
        record.cancellation_finalized = False
        await _save_queue_projection(queue, record)
        logger.exception(
            "run_id=%s Web 取消后的正式账本或 Session 收束失败，将由取消对账器重试。",
            record.run_id,
        )
        return
    await _finish_standard_terminal_run(
        queue=queue,
        record=record,
        state_store=state_store,
        progress_recorder=progress_recorder,
        final_report_generator=None,
        task_finalization_service=None,
        inspection_work_items_enabled=False,
        legacy_report_enabled=False,
    )


async def _terminalize_web_formal_ledgers_for_cancel(
    *,
    record: AgentRunRecord,
    finalizer: Any | None,
) -> None:
    if record.task_id is None:
        raise RuntimeError("Web 正式取消缺少 task_id")
    if finalizer is None:
        finalizer = PostgresWebFormalTerminalLedgerRepository()
    terminalize = getattr(finalizer, "terminalize", None)
    if not callable(terminalize):
        raise RuntimeError("Web 正式取消缺少持久账本终结器")
    await terminalize(
        task_id=record.task_id,
        agent_run_id=record.run_id,
        outcome="canceled",
        reason_code="WORKFLOW_CANCELED",
        reason_message="Web 正式任务已取消，收束未完成的持久账本",
    )


async def _close_web_formal_session_for_cancel(
    *,
    record: AgentRunRecord,
    web_formal_session_closer: Any,
) -> None:
    if record.task_id is None:
        raise RuntimeError("Web 正式取消缺少 task_id")
    close = getattr(web_formal_session_closer, "close", None)
    if not callable(close):
        raise RuntimeError("Web 正式取消缺少 durable Session closer")
    await close(
        task_id=record.task_id,
        session_id=f"web:{record.run_id}",
        cancel_active_commands=True,
    )


async def _finish_standard_terminal_run(
    *,
    queue: RedisAgentRunQueue,
    record: AgentRunRecord,
    state_store: AgentRunStateStore,
    progress_recorder: TaskProgressRecorder | None,
    final_report_generator: FinalReportGenerator | None,
    task_finalization_service: TaskFinalizationService | None,
    inspection_work_items_enabled: bool = False,
    legacy_report_enabled: bool = False,
) -> None:
    """让通用、App、H5 V1/Shadow 的收口统一服从持久化取消事实。"""

    if record.status is AgentRunQueueStatus.CANCELED:
        await _project_run_canceled_and_cleanup(queue=queue, record=record)
        task_finalization_service = None
        inspection_work_items_enabled = False
        legacy_report_enabled = False
        record.cancellation_finalized = True
    try:
        await _finish_terminal_run(
            queue=queue,
            record=record,
            state_store=state_store,
            progress_recorder=progress_recorder,
            final_report_generator=final_report_generator,
            task_finalization_service=task_finalization_service,
            inspection_work_items_enabled=inspection_work_items_enabled,
            legacy_report_enabled=legacy_report_enabled,
        )
    except AgentRunCancellationConflict:
        logger.info("run_id=%s 持久化取消标记赢得通用终态竞争", record.run_id)
        await _project_run_canceled_and_cleanup(queue=queue, record=record)
        record.cancellation_finalized = True
        try:
            await _finish_terminal_run(
                queue=queue,
                record=record,
                state_store=state_store,
                progress_recorder=progress_recorder,
                final_report_generator=final_report_generator,
                task_finalization_service=None,
                inspection_work_items_enabled=False,
                legacy_report_enabled=False,
            )
        except Exception:  # noqa: BLE001 - Redis 已投影取消，长期终态留给对账器重试
            record.cancellation_finalized = False
            await _save_queue_projection(queue, record)
            logger.exception(
                "run_id=%s 取消终态写入失败，将由对账器继续重试。",
                record.run_id,
            )
            return
    except AgentRunTerminalStateConflict:
        if record.status is not AgentRunQueueStatus.CANCELED:
            raise
        # PostgreSQL 已有其他权威终态（取消竞争落败且不可逆转），继续重试
        # 永远不可能获准写入；按幂等完成收口取消对账，终结每轮 5 秒的无限重试。
        logger.info(
            "run_id=%s 取消对账发现 PostgreSQL 已有其他权威终态，按幂等完成收口。",
            record.run_id,
        )
        await _mark_cancellation_finalized(queue, record)
        return
    except Exception:  # noqa: BLE001 - 只有已取消终态可安全延后长期写入
        if record.status is not AgentRunQueueStatus.CANCELED:
            raise
        record.cancellation_finalized = False
        await _save_queue_projection(queue, record)
        logger.exception(
            "run_id=%s 取消终态写入失败，将由对账器继续重试。",
            record.run_id,
        )
        return
    if record.status is AgentRunQueueStatus.CANCELED:
        await _mark_cancellation_finalized(queue, record)


async def _finish_web_terminal_run(
    *,
    queue: RedisAgentRunQueue,
    record: AgentRunRecord,
    state_store: AgentRunStateStore,
    progress_recorder: TaskProgressRecorder | None,
    final_report_generator: FinalReportGenerator | None,
) -> None:
    """封存 Web V2 终态；报告异常不得跳过 Run 与队列持久化。"""

    if record.status is AgentRunQueueStatus.CANCELED:
        await _finish_standard_terminal_run(
            queue=queue,
            record=record,
            state_store=state_store,
            progress_recorder=progress_recorder,
            final_report_generator=None,
            task_finalization_service=None,
            inspection_work_items_enabled=False,
            legacy_report_enabled=False,
        )
        return
    try:
        await _generate_final_report(
            record,
            final_report_generator,
            inspection_work_items_enabled=False,
            legacy_candidate_reconciliation_enabled=False,
        )
    except Exception:  # noqa: BLE001 - Web 报告失败必须生成最小终态快照
        logger.exception(
            "run_id=%s Web V2 最终报告生成失败，改用失败快照封存",
            record.run_id,
        )
        record.status = AgentRunQueueStatus.FAILED
        record.finished_at = record.finished_at or datetime.now(UTC)
        record.termination_reason = TerminationReason.ERROR.value
        record.error_message = "Web V2 最终报告生成失败"
        record.error_category = ErrorCategory.INFRASTRUCTURE.value
        record.execution_outcome = ExecutionOutcome.NEEDS_REVIEW.value
        page_exploration = await _page_exploration_for_report(record, reader=None)
        record.final_report = _web_final_report_failure_payload(
            record,
            page_exploration=page_exploration,
        )
    await _finish_standard_terminal_run(
        queue=queue,
        record=record,
        state_store=state_store,
        progress_recorder=progress_recorder,
        final_report_generator=None,
        task_finalization_service=None,
        inspection_work_items_enabled=False,
        legacy_report_enabled=False,
    )


async def _finish_app_terminal_run(
    *,
    queue: RedisAgentRunQueue,
    record: AgentRunRecord,
    state_store: AgentRunStateStore,
    progress_recorder: TaskProgressRecorder | None,
    final_report_generator: FinalReportGenerator | None,
    task_finalization_service: TaskFinalizationService | None,
    inspection_work_items_enabled: bool = False,
    legacy_report_enabled: bool = False,
) -> None:
    """保留 App 专用入口，并复用统一取消感知收口。"""

    await _finish_standard_terminal_run(
        queue=queue,
        record=record,
        state_store=state_store,
        progress_recorder=progress_recorder,
        final_report_generator=final_report_generator,
        task_finalization_service=task_finalization_service,
        inspection_work_items_enabled=inspection_work_items_enabled,
        legacy_report_enabled=legacy_report_enabled,
    )


async def _finish_h5_terminal_run(
    *,
    queue: RedisAgentRunQueue,
    record: AgentRunRecord,
    state_store: AgentRunStateStore,
    progress_recorder: TaskProgressRecorder | None,
    final_report_generator: FinalReportGenerator | None,
    task_finalization_service: TaskFinalizationService | None,
    inspection_work_items_enabled: bool,
    legacy_report_enabled: bool,
) -> None:
    """将持久化取消胜出的终态竞争投影为 H5 取消。"""

    await _finish_standard_terminal_run(
        queue=queue,
        record=record,
        state_store=state_store,
        progress_recorder=progress_recorder,
        final_report_generator=final_report_generator,
        task_finalization_service=task_finalization_service,
        inspection_work_items_enabled=inspection_work_items_enabled,
        legacy_report_enabled=legacy_report_enabled,
    )


async def _attach_h5_v2_exploration_action_trace(record: AgentRunRecord) -> None:
    """H5 V2 不走 FunctionMiddleware，需从持久化 Work 恢复工作项级审计留痕。"""

    if record.task_id is None or record.action_trace or not _is_locked_h5_v2_run_record(record):
        return
    try:
        entries = await _load_h5_v2_exploration_work_trace_entries(task_id=record.task_id)
    except Exception:  # noqa: BLE001 - 留痕补偿失败不能阻断任务收口
        logger.exception("run_id=%s H5 V2 exploration action trace 补偿失败", record.run_id)
        return
    if not entries:
        return
    record.action_trace = entries
    record.step_count = max(record.step_count or 0, len(entries))


async def _load_h5_v2_exploration_work_trace_entries(*, task_id: str) -> list[dict[str, Any]]:
    pool = await get_postgres_pool(os.getenv("ALIOTH_DATABASE_URL"))
    async with pool.acquire() as connection:
        rows = await connection.fetch(
            """
            SELECT
                work_id,
                work_type,
                source_capture_id::text AS source_capture_id,
                work_payload,
                status,
                attempt_count,
                max_attempts,
                started_at,
                finished_at,
                deadline,
                last_error_code,
                last_error_message
            FROM qc_v2_work_item
            WHERE task_id = $1::uuid
              AND work_type = 'exploration'
              AND status NOT IN ('pending', 'leased', 'running')
            ORDER BY started_at NULLS LAST, created_at, work_id
            """,
            task_id,
        )
    return _h5_v2_exploration_work_trace_entries_from_rows(rows)


def _h5_v2_exploration_work_trace_entries_from_rows(
    rows: list[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for step, row in enumerate(rows, start=1):
        payload = _mapping_payload(row.get("work_payload"))
        entries.append(
            {
                "step": step,
                "tool_name": "h5_exploration_work",
                "arguments": json.dumps(
                    _h5_trace_arguments(row, payload),
                    ensure_ascii=False,
                    default=str,
                ),
                "status": _h5_trace_status(row.get("status")),
                "duration_seconds": _duration_seconds(
                    row.get("started_at"),
                    row.get("finished_at"),
                ),
                "error_message": row.get("last_error_message"),
                "retry_count": max(0, int(row.get("attempt_count") or 1) - 1),
                "result": json.dumps(
                    _h5_trace_result(row),
                    ensure_ascii=False,
                    default=str,
                ),
            }
        )
    return entries


def _h5_trace_arguments(row: Mapping[str, Any], payload: Mapping[str, Any]) -> dict[str, Any]:
    candidate = _first_h5_candidate(payload)
    metadata = _mapping_payload(candidate.get("metadata"))
    arguments: dict[str, Any] = {
        "work_id": row.get("work_id"),
        "work_type": row.get("work_type"),
        "source_capture_id": row.get("source_capture_id"),
        "deadline": _isoformat_or_none(row.get("deadline")),
        "budget": payload.get("budget"),
        "frontier_snapshot_id": payload.get("frontier_snapshot_id"),
        "selected_candidate_id": candidate.get("candidate_id"),
        "semantic_target": candidate.get("semantic_target"),
        "purpose": metadata.get("purpose"),
        "target_keywords": metadata.get("target_keywords"),
    }
    return {key: value for key, value in arguments.items() if value is not None}


def _h5_trace_result(row: Mapping[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {
        "work_status": row.get("status"),
        "attempt_count": row.get("attempt_count"),
        "max_attempts": row.get("max_attempts"),
        "source_capture_id": row.get("source_capture_id"),
    }
    if row.get("last_error_code") is not None:
        result["error_code"] = row.get("last_error_code")
    if row.get("last_error_message") is not None:
        result["error_message"] = row.get("last_error_message")
    return result


def _first_h5_candidate(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    frontier = _mapping_payload(payload.get("frontier"))
    candidates = frontier.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        return {}
    first = candidates[0]
    return first if isinstance(first, Mapping) else {}


def _mapping_payload(value: Any) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, Mapping) else {}
    return {}


def _h5_trace_status(value: Any) -> str:
    status = str(value or "").lower()
    if status == "failed":
        return "error"
    if status == "cancelled":
        return "terminated"
    if status == "retry_wait":
        return "retried"
    if status in {"superseded", "succeeded"}:
        return "ok"
    return "skipped"


def _duration_seconds(started_at: Any, finished_at: Any) -> float | None:
    if not isinstance(started_at, datetime) or not isinstance(finished_at, datetime):
        return None
    return max(0.0, (finished_at - started_at).total_seconds())


def _isoformat_or_none(value: Any) -> str | None:
    return value.isoformat() if isinstance(value, datetime) else None


def _is_locked_h5_v2_run_record(record: AgentRunRecord) -> bool:
    """仅识别已锁定 TaskTarget 的正式 H5 V2 Run。"""

    target = (record.task_config or {}).get("_task_target")
    return isinstance(target, dict) and target.get("platform") in {"h5", "web"}


def _is_locked_web_v2_run_record(record: AgentRunRecord) -> bool:
    """仅识别锁定为正式质检的 Web V2 main Run。"""

    task_config = record.task_config or {}
    target = task_config.get("_task_target")
    return (
        isinstance(target, dict)
        and target.get("platform") == "web"
        and task_config.get("workflow_version") == "v2"
        and task_config.get("execution_mode") == "full_inspection"
        and task_config.get("web_inspection_notification_mode") == "enforced"
    )


def _final_report_generator_for_record(
    record: AgentRunRecord,
    *,
    default_generator: FinalReportGenerator | None,
    web_generator: FinalReportGenerator | None,
) -> FinalReportGenerator | None:
    """只有锁定的 Web V2 full/enforced Run 使用持久 attempt 包装器。"""

    if web_generator is not None and _is_locked_web_v2_run_record(record):
        return web_generator
    return default_generator


async def _mark_h5_run_canceled_if_requested(
    *,
    queue: RedisAgentRunQueue,
    record: AgentRunRecord,
    state_store: AgentRunStateStore,
) -> bool:
    """在 H5 handoff 与 Completion Gate 入口前让取消状态优先。"""

    if not await _is_cancel_requested_safely(
        queue,
        record.run_id,
        durable_source=state_store,
    ):
        return False
    await _project_run_canceled_and_cleanup(queue=queue, record=record)
    return True


def _project_run_canceled(record: AgentRunRecord) -> None:
    """将所有 Workflow 取消来源投影为同一可审计终态。"""

    already_canceled = record.status is AgentRunQueueStatus.CANCELED
    record.status = AgentRunQueueStatus.CANCELED
    record.finished_at = datetime.now(UTC)
    record.termination_reason = TerminationReason.CANCELED.value
    record.execution_outcome = ExecutionOutcome.NEEDS_REVIEW.value
    record.error_message = None
    record.error_category = None
    record.final_report = None
    if not already_canceled:
        record.cancellation_cleanup_completed = False
        record.cancellation_finalized = False


def _is_h5_run_record(record: AgentRunRecord) -> bool:
    """只从持久化平台身份判断取消清理范围，不读取 URL 猜测。"""

    task_config = record.task_config or {}
    if task_config.get("legacy_h5_compatible") is True:
        return True
    target = task_config.get("_task_target")
    return isinstance(target, dict) and target.get("platform") in {"h5", "web"}


async def _project_run_canceled_and_cleanup(
    *,
    queue: RedisAgentRunQueue,
    record: AgentRunRecord,
) -> bool:
    """先落 Redis 取消投影，再尽力清理 Work；失败由对账器继续重试。"""

    _project_run_canceled(record)
    record.worker_instance_id = None
    record.worker_missing_since = None
    await _save_queue_projection(queue, record)
    if record.cancellation_cleanup_completed or record.task_id is None:
        record.cancellation_cleanup_completed = True
        await _save_queue_projection(queue, record)
        return True
    try:
        if _is_h5_run_record(record):
            await _cancel_h5_nonterminal_work(task_id=record.task_id)
        else:
            await _cancel_app_nonterminal_work(task_id=record.task_id)
    except Exception as exc:  # noqa: BLE001 - 取消事实已落盘，清理失败必须交给对账器重试
        logger.warning(
            "task_id=%s run_id=%s 取消后的 Work/Action 清理失败 "
            "error_type=%s，将由对账器重试。",
            record.task_id,
            record.run_id,
            type(exc).__name__,
        )
        return False
    record.cancellation_cleanup_completed = True
    await _save_queue_projection(queue, record)
    return True


async def _mark_cancellation_finalized(
    queue: RedisAgentRunQueue,
    record: AgentRunRecord,
) -> None:
    """长期终态写入成功后关闭 Redis 取消对账标志。"""

    record.cancellation_finalized = True
    await _save_queue_projection(queue, record)


async def _cancel_h5_nonterminal_work(*, task_id: str) -> int:
    """原子取消 H5 当前 Epoch 的请求与 Work，再收束结果未知的动作。"""

    cancel_reason = "H5 Worker 在收口前确认统一 Run 取消"
    settlement = await PostgresH5CancellationSettlementRepository().settle_canceled(
        task_id=task_id,
        reason="H5_TASK_CANCELED",
    )
    legacy_work_count = await PostgresExplorerWorkRepository().cancel_nonterminal(
        task_id=task_id,
        reason=cancel_reason,
        work_types=(ExplorerWorkType.EXPLORATION, ExplorerWorkType.PROBE),
        h5_only=True,
    )
    action_count = await PostgresActionExecutionRepository().fail_nonterminal_for_task(
        task_id=task_id,
        error_code="WORKFLOW_CANCELED",
        error_message=cancel_reason,
        h5_only=True,
    )
    legacy_inspection_count = (
        await PostgresInspectionWorkItemRepository().cancel_nonterminal(
            task_id=task_id,
            reason=cancel_reason,
        )
    )
    return (
        settlement.total_count
        + legacy_work_count
        + action_count
        + legacy_inspection_count
    )


async def _fail_h5_orphaned_nonterminal_runtime(*, task_id: str) -> int:
    """孤儿 Run 写长期终态前，先收束 H5 Work 与结果未知的 Action。"""

    work_count = await PostgresExplorerWorkRepository().cancel_nonterminal(
        task_id=task_id,
        reason="H5 Worker 失联，探索工作已失去执行者",
        work_types=(ExplorerWorkType.EXPLORATION, ExplorerWorkType.PROBE),
        h5_only=True,
    )
    action_count = await PostgresActionExecutionRepository().fail_nonterminal_for_task(
        task_id=task_id,
        error_code="H5_WORKER_ORPHANED",
        error_message="H5 Worker 失联，动作结果未知",
        h5_only=True,
    )
    return work_count + action_count


async def _settle_web_exploration_orphan(*, task_id: str, run_id: str) -> bool:
    from services.api.app.infrastructure.db.web_exploration_orphan_repository import (
        PostgresWebExplorationOrphanRepository,
    )

    return await PostgresWebExplorationOrphanRepository().settle(task_id=task_id, run_id=run_id)


async def _cancel_app_nonterminal_work(*, task_id: str) -> int:
    """取消明确取消任务的 Work 与 Action，并保留两类清理失败。"""

    work_count = 0
    action_count = 0
    work_error: Exception | None = None
    action_error: Exception | None = None
    try:
        work_count = await PostgresExplorerWorkRepository().cancel_nonterminal(
            task_id=task_id,
            reason="App Worker 在收口前确认统一 Run 取消",
        )
    except Exception as exc:  # noqa: BLE001 - 仍需尝试收束动作账本
        work_error = exc
    try:
        action_count = await PostgresActionExecutionRepository().fail_nonterminal_for_task(
            task_id=task_id,
            error_code=APP_WORKFLOW_CANCELED_ERROR_CODE,
            error_message=APP_WORKFLOW_CANCELED_ERROR_MESSAGE,
        )
    except Exception as exc:  # noqa: BLE001 - 失败必须回到取消对账层
        action_error = exc
    if work_error is not None:
        if action_error is not None:
            raise work_error from action_error
        raise work_error
    if action_error is not None:
        raise action_error
    return work_count + action_count


async def _advance_task_finalization(
    record: AgentRunRecord,
    service: TaskFinalizationService,
    recovery_lease: Any | None = None,
) -> _WorkerFinalizationDecision:
    """把完整收口意图交给 Orchestrator，并映射为 Worker 短期投影。"""
    if record.task_id is None:
        raise ValueError("Task Finalization 必须绑定 task_id")
    if not isinstance(record.final_report, dict):
        raise RuntimeError("原子收口缺少最终报告载荷")
    progress = await QcOrchestrator(
        finalization_coordinator=service,
        completion_gate_preparer=getattr(service, "completion_gate_preparer", None),
    ).advance_task_finalization(
        AdvanceTaskFinalizationCommand(
            task_id=record.task_id,
            agent_run_id=record.run_id,
            report_id=f"task:{record.task_id}:run:{record.run_id}",
            report_version=2,
            execution_status=_task_execution_status(record),
            quality_outcome=_task_quality_outcome(record.final_report),
            final_report=record.final_report,
            recovery_lease_token=(
                recovery_lease.lease_token if recovery_lease is not None else None
            ),
            recovery_generation=(
                recovery_lease.recovery_generation if recovery_lease is not None else None
            ),
            recovery_lease_expires_at=(
                _recovery_lease_expiry(recovery_lease)
                if recovery_lease is not None
                else None
            ),
        )
    )
    return _WorkerFinalizationDecision(
        gate_passed=progress.gate_passed,
        gate_snapshot_id=progress.gate_snapshot_id,
        finalization_epoch=progress.finalization_epoch,
        block_reasons=progress.block_reasons,
    )


def _task_execution_status(record: AgentRunRecord) -> TaskExecutionStatus:
    if record.status is AgentRunQueueStatus.CANCELED:
        return TaskExecutionStatus.CANCELED
    if record.status is AgentRunQueueStatus.FAILED:
        if record.execution_outcome == "needs_review" or is_review_termination_reason(
            record.termination_reason
        ):
            return TaskExecutionStatus.NEEDS_REVIEW
        return TaskExecutionStatus.FAILED
    if record.execution_outcome == TaskExecutionStatus.COMPLETED_WITH_GAPS.value:
        return TaskExecutionStatus.COMPLETED_WITH_GAPS
    summary = record.final_report.get("summary", {}) if record.final_report else {}
    if record.execution_outcome == "needs_review":
        return TaskExecutionStatus.NEEDS_REVIEW
    if _is_h5_v2_review_required(record, summary=summary):
        return TaskExecutionStatus.NEEDS_REVIEW
    if summary.get("overall_status") in {"incomplete", "warning"}:
        return TaskExecutionStatus.COMPLETED_WITH_GAPS
    return TaskExecutionStatus.COMPLETED


def _is_h5_v2_review_required(
    record: AgentRunRecord,
    *,
    summary: Mapping[str, Any],
) -> bool:
    """识别正式 H5 V2 报告已经给出的人工复核终态。"""

    if not _is_locked_native_h5_v2_run_record(record):
        return False
    return (
        summary.get("overall_status") == "manual_review"
        or summary.get("business_completion_status") == "review_required"
    )


def _is_locked_native_h5_v2_run_record(record: AgentRunRecord) -> bool:
    """仅识别 TaskTarget 明确锁定为 H5 的 V2 Run。"""

    task_config = getattr(record, "task_config", None) or {}
    target = task_config.get("_task_target")
    return (
        isinstance(target, dict)
        and target.get("platform") == "h5"
        and task_config.get("workflow_version") == "v2"
    )


def _apply_web_reconciliation_execution_status(
    record: AgentRunRecord,
    result: Any,
) -> None:
    """把 Web 对账确认的覆盖缺口终态投影到原子收口命令。"""

    if (
        record.status is AgentRunQueueStatus.COMPLETED
        and getattr(result, "execution_status", None)
        == TaskExecutionStatus.COMPLETED_WITH_GAPS.value
    ):
        record.execution_outcome = TaskExecutionStatus.COMPLETED_WITH_GAPS.value


def _task_quality_outcome(report: dict[str, Any] | None) -> TaskQualityOutcome:
    summary = report.get("summary", {}) if isinstance(report, dict) else {}
    passed = _safe_nonnegative_int(summary.get("pass"))
    failed = _safe_nonnegative_int(summary.get("fail"))
    warning = _safe_nonnegative_int(summary.get("warning"))
    uncertain = sum(
        _safe_nonnegative_int(summary.get(status))
        for status in ("manual_review", "evidence_insufficient", "error")
    )
    if failed and (passed or warning or uncertain):
        return TaskQualityOutcome.MIXED
    if failed:
        return TaskQualityOutcome.FAIL
    if warning:
        return TaskQualityOutcome.WARNING
    if uncertain:
        return TaskQualityOutcome.MIXED if passed else TaskQualityOutcome.UNKNOWN
    if passed:
        return TaskQualityOutcome.PASS
    return TaskQualityOutcome.UNKNOWN


def _prepare_report_binding_metadata(record: AgentRunRecord) -> None:
    """为最终报告准备稳定绑定身份，门禁元数据由最终化事务覆盖。"""
    if record.task_id is None or not isinstance(record.final_report, dict):
        return
    record.final_report["report_id"] = f"task:{record.task_id}:run:{record.run_id}"
    record.final_report["report_binding_version"] = 2
    if _is_locked_web_v2_run_record(record):
        record.final_report["report_contract"] = {
            "target_platform": "web",
            "workflow_version": "v2",
            "execution_mode": "full_inspection",
            "notification_mode": "enforced",
        }


def _attach_completion_gate(
    record: AgentRunRecord,
    decision: _WorkerFinalizationDecision,
) -> None:
    if not isinstance(record.final_report, dict):
        return
    record.final_report["completion_gate"] = {
        "gate_passed": decision.gate_passed,
        "gate_snapshot_id": decision.gate_snapshot_id,
        "finalization_epoch": decision.finalization_epoch,
        "block_reasons": list(decision.block_reasons),
    }
    exploration = record.final_report.get("page_exploration")
    if isinstance(exploration, dict) and isinstance(exploration.get("h5_coverage"), dict):
        # 只有原子封存成功才证明这份报告 Coverage 被本次 Gate 接纳。
        exploration["h5_coverage"].update(
            gate_snapshot_id=decision.gate_snapshot_id if decision.gate_passed else None,
            gate_finalization_epoch=decision.finalization_epoch if decision.gate_passed else None,
        )
    summary = record.final_report.get("summary")
    block_reasons = set(decision.block_reasons)
    h5_review_ready = bool(block_reasons) and block_reasons <= {
        "review_policy_unsatisfied",
        "h5_coverage_scope_incomplete",
    }
    if (
        not decision.gate_passed
        and isinstance(exploration, dict)
        and isinstance(summary, dict)
        and _is_locked_native_h5_v2_run_record(record)
        and h5_review_ready
        and _task_execution_status(record)
        in {
            TaskExecutionStatus.NEEDS_REVIEW,
            TaskExecutionStatus.COMPLETED_WITH_GAPS,
        }
    ):
        terminal_reason = exploration.get("terminal_reason")
        if terminal_reason in {None, "completion_gate_pending"}:
            terminal_reason = (
                decision.block_reasons[0]
                if decision.block_reasons
                else "completion_gate_pending"
            )
        exploration.update(
            complete=False,
            quality_status="needs_review",
            requires_review=True,
            terminal_reason=terminal_reason,
        )
        summary.update(
            overall_status="manual_review",
            business_completion_status="review_required",
        )
    if decision.gate_passed:
        record.final_report["report_stage"] = "final"
        record.final_report["is_final"] = True
        return
    raw_task_config = getattr(record, "task_config", None)
    task_config = raw_task_config if isinstance(raw_task_config, dict) else {}
    is_h5_terminal_review = (
        _is_locked_native_h5_v2_run_record(record)
        and str(task_config.get("execution_mode") or "").casefold()
        == TaskExecutionMode.FULL_INSPECTION.value
        and getattr(record, "status", None)
        in {
            AgentRunQueueStatus.COMPLETED,
            AgentRunQueueStatus.FAILED,
            AgentRunQueueStatus.CANCELED,
        }
    )
    record.final_report["report_stage"] = (
        "final_review" if is_h5_terminal_review else "preview"
    )
    record.final_report["is_final"] = False
    if is_h5_terminal_review:
        record.final_report["report_availability"] = "preview"
        record.final_report["export_allowed"] = False
        record.final_report["export_ready"] = False
        record.final_report["incomplete_reasons"] = ["H5_REPORT_NOT_FINALIZED"]


def _is_final_report_payload(report: dict[str, Any] | None) -> bool:
    """判断报告是否已是可绑定正式报告，避免恢复器把 preview 改字段冒充 final。"""
    return (
        isinstance(report, dict)
        and report.get("report_stage") == "final"
        and report.get("is_final") is True
    )


async def _bind_final_report(
    record: AgentRunRecord,
    service: TaskFinalizationService,
    recovery_lease: Any | None = None,
) -> None:
    if record.task_id is None or not isinstance(record.final_report, dict):
        raise RuntimeError("最终报告缺少任务或报告载荷")
    _prepare_report_binding_metadata(record)
    report_id = str(record.final_report["report_id"])
    report_version = int(record.final_report["report_binding_version"])
    await QcOrchestrator(finalization_coordinator=service).bind_sealed_report(
        BindSealedReportCommand(
            task_id=record.task_id,
            agent_run_id=record.run_id,
            report_id=report_id,
            report_version=report_version,
            final_report=record.final_report,
            recovery_lease_token=(
                recovery_lease.lease_token if recovery_lease is not None else None
            ),
            recovery_generation=(
                recovery_lease.recovery_generation if recovery_lease is not None else None
            ),
            recovery_lease_expires_at=(
                _recovery_lease_expiry(recovery_lease)
                if recovery_lease is not None
                else None
            ),
        )
    )


async def _finish_progress(recorder: TaskProgressRecorder | None, record: AgentRunRecord) -> None:
    """通知过程观察者 Run 已终态；观察者内部自兜底，此处只做空值保护。"""
    if recorder is None:
        return
    status = {
        AgentRunQueueStatus.COMPLETED: "completed",
        AgentRunQueueStatus.CANCELED: "canceled",
    }.get(record.status, "failed")
    await recorder.on_run_finished(
        status=status,
        termination_reason=record.termination_reason,
        error_message=record.error_message,
        working_memory=record.working_memory,
    )


def _task_execution_policy_middleware(
    record: AgentRunRecord,
) -> TaskExecutionPolicyMiddleware | None:
    """为绑定了任务策略的 Run 构建独立页面边界中间件。"""
    if not (record.task_config or record.page_sampling_policy or record.loop_guard):
        return None
    return TaskExecutionPolicyMiddleware(
        task_config=record.task_config,
        page_sampling_policy=record.page_sampling_policy,
        loop_guard=record.loop_guard,
    )


def _log_workflow_execution_error(
    record: AgentRunRecord, error: Exception, *, platform: str
) -> None:
    """取消和预算耗尽是受控终止，真实 Provider 错误继续保留异常堆栈。"""
    if isinstance(error, WorkerWorkflowCanceled):
        logger.info("run_id=%s %s Workflow 已响应取消", record.run_id, platform)
    elif isinstance(error, WorkerWorkflowDeadlineExceeded):
        logger.warning("run_id=%s %s Workflow 冻结墙钟预算已耗尽", record.run_id, platform)
    else:
        logger.exception("run_id=%s %s Workflow 执行失败", record.run_id, platform)


def _apply_workflow_control_error(record: AgentRunRecord, error: Exception) -> None:
    """保留冻结预算耗尽或明确取消的因果，不能混作 Provider 执行异常。"""
    if isinstance(error, WorkerWorkflowCanceled):
        record.status = AgentRunQueueStatus.CANCELED
        record.termination_reason = TerminationReason.CANCELED.value
        record.error_message = None
        record.error_category = None
    elif isinstance(error, WorkerWorkflowDeadlineExceeded):
        record.termination_reason = TerminationReason.TIMEOUT.value
        record.execution_outcome = ExecutionOutcome.NEEDS_REVIEW.value
        record.error_category = None


def _apply_result(record: AgentRunRecord, result: RunResult) -> None:
    """把 ``RunResult`` 的内容写回 ``AgentRunRecord``（就地修改）。"""
    if result.termination_reason == TerminationReason.COMPLETED:
        record.status = AgentRunQueueStatus.COMPLETED
    elif result.termination_reason == TerminationReason.CANCELED:
        record.status = AgentRunQueueStatus.CANCELED
    else:
        record.status = AgentRunQueueStatus.FAILED
    record.finished_at = datetime.now(UTC)
    record.termination_reason = result.termination_reason.value
    record.final_text = result.final_text
    record.step_count = result.working_memory.step_count
    record.error_message = result.error_message
    record.error_category = result.error_category.value if result.error_category else None
    record.execution_outcome = result.execution_outcome.value
    record.evidence_index_id = result.evidence_index_id
    record.exploration_status = result.exploration_status
    record.evidence_gap = list(result.evidence_gap)
    record.page_timeline = list(result.page_timeline)
    record.web_exploration_summary = (
        dict(result.web_exploration_summary)
        if result.web_exploration_summary is not None
        else None
    )
    record.usage = (
        {
            "input_tokens": result.usage.input_tokens,
            "output_tokens": result.usage.output_tokens,
            "cache_hit_tokens": result.usage.cache_hit_tokens,
            "billable_input_tokens": result.usage.billable_input_tokens,
            "total_tokens": result.usage.total_tokens,
            "ai_calls": result.usage.ai_calls,
            "input_cost": result.usage.input_cost,
            "output_cost": result.usage.output_cost,
            "cache_hit_cost": result.usage.cache_hit_cost,
            "total_cost": result.usage.total_cost,
            "cost_currency": result.usage.cost_currency,
            "pricing_source": result.usage.pricing_source,
            "pricing_configured": result.usage.pricing_configured,
            "applied_pricing_tiers": list(result.usage.applied_pricing_tiers),
            "pricing_breakdown": list(result.usage.pricing_breakdown),
            "provider": result.usage.provider,
            "model": result.usage.model,
        }
        if result.usage is not None
        else None
    )
    entries = getattr(result.trace_sink, "entries", None)
    if entries is not None:
        record.action_trace = [_trace_entry_to_dict(entry) for entry in entries]
    record.working_memory = result.working_memory.audit_snapshot(
        action_trace_count=len(record.action_trace)
    )


def _record_worker_resource_usage(
    record: AgentRunRecord,
    *,
    rss_before_bytes: int | None,
    rss_after_bytes: int | None,
) -> None:
    """记录 Run 边界的 Worker RSS，仅用于趋势审计，不宣称为任务独占内存。"""

    usage = dict(record.usage or {})
    usage["worker_rss_before_bytes"] = rss_before_bytes
    usage["worker_rss_after_bytes"] = rss_after_bytes
    usage["worker_rss_delta_bytes"] = (
        rss_after_bytes - rss_before_bytes
        if rss_before_bytes is not None and rss_after_bytes is not None
        else None
    )
    record.usage = usage


def _process_rss_bytes() -> int | None:
    """读取 Linux Worker 当前常驻内存；其他运行环境不支持时返回 None。"""

    statm_path = Path("/proc/self/statm")
    try:
        fields = statm_path.read_text(encoding="ascii").split()
        resident_pages = int(fields[1])
        page_size = int(os.sysconf("SC_PAGE_SIZE"))
    except (IndexError, OSError, TypeError, ValueError):
        return None
    if resident_pages < 0 or page_size <= 0:
        return None
    return resident_pages * page_size


def _trace_entry_to_dict(entry: Any) -> dict:
    """把 ActionTraceEntry 转成可入 Redis 记录与审计表的字典。"""
    return {
        "step": entry.step,
        "tool_name": entry.tool_name,
        "arguments": entry.arguments,
        "status": entry.status,
        "duration_seconds": entry.duration_seconds,
        "error_message": entry.error_message,
        "retry_count": entry.retry_count,
        "result": getattr(entry, "result", None),
        "source_execution_ids": list(getattr(entry, "source_execution_ids", ())),
    }


def _h5_request_admitting_writer(
    policy: SupplementalEvidencePolicy,
) -> CurrentRuleDecisionWriter:
    return CurrentRuleDecisionWriter(
        request_admission=EvidenceRequestAdmissionService(
            repository=PostgresEvidenceRequestRepository(),
            policy=policy,
        ),
        supplemental_evidence_policy=policy,
    )


async def _drain_h5_closed_snapshots(
    record: AgentRunRecord, *, settings: Settings, worker_instance_id: str | None,
) -> int:
    if (
        record.task_id is None
        or not _is_locked_native_h5_v2_run_record(record)
        or not settings.supplemental_evidence.enabled
        or settings.workflow.h5_v2_mode != "on"
    ):
        return 0
    # 先核对终态主 Run 的失联现场，保留允许新会话恢复的请求，再开启补采。
    await PostgresH5ProbeTerminalSettlementRepository(
        allow_new_session_recovery=True,
    ).settle_requests_after_session_close(
        task_id=record.task_id, agent_run_id=record.run_id,
    )
    from services.api.app.application.h5_closed_session_recapture import (
        drain_h5_closed_session_snapshots,
    )
    return await drain_h5_closed_session_snapshots(
        record=record, settings=settings,
        worker_instance_id=worker_instance_id or socket.gethostname(),
    )


async def _drain_snapshot_inspection_work(
    record: AgentRunRecord,
    *,
    settings: Settings,
    worker_instance_id: str | None,
) -> None:
    """在报告收口前排空可恢复的 Snapshot 工作项。"""
    inspection_settings = settings.inspection_work_items
    if record.task_id is None or record.status not in {
        AgentRunQueueStatus.COMPLETED,
        AgentRunQueueStatus.FAILED,
    }:
        return
    h5_deadline = _h5_inspection_deadline(record)
    if _is_locked_native_h5_v2_run_record(record) and (
        h5_deadline is None or datetime.now(UTC) < h5_deadline
    ):
        await _drain_h5_closed_snapshots(record, settings=settings, worker_instance_id=worker_instance_id)
    service = InspectionWorkItemService(repository=PostgresInspectionWorkItemRepository())
    consumer_agent_run_id = record.run_id
    h5_publication_enforced = (
        parse_h5_inspection_notification_mode(record.task_config)
        is H5InspectionNotificationMode.ENFORCED
    )
    if h5_publication_enforced:
        consumer_agent_run_id = await service.find_active_consumer_run(task_id=record.task_id) or ""
        if not consumer_agent_run_id:
            if _is_locked_native_h5_v2_run_record(record):
                # 消费 Run 已结束不代表补采请求已结束，先结算失去会话的请求。
                await PostgresH5ProbeTerminalSettlementRepository().settle_requests_after_session_close(
                    task_id=record.task_id, agent_run_id=record.run_id,
                )
            if not await service.has_nonterminal_work(task_id=record.task_id):
                return
            raise RuntimeError("H5 enforced 工作项缺少持久化逻辑消费 Run")
    policy = build_current_supplemental_evidence_policy(
        enabled=settings.supplemental_evidence.enabled
    )
    consumer = InspectionWorkItemConsumer(
        service=service,
        lease_owner=(
            f"{worker_instance_id or socket.gethostname()}:{consumer_agent_run_id}:snapshot"
        ),
        lease_seconds=inspection_settings.lease_seconds,
        retry_backoff_seconds=inspection_settings.retry_base_seconds,
        supplemental_evidence_policy=policy,
        h5_result_writer=(
            _h5_request_admitting_writer(policy)
            if _is_locked_native_h5_v2_run_record(record)
            else None
        ),
        h5_closed_session_settlement=(
            PostgresH5ProbeTerminalSettlementRepository(
                allow_new_session_recovery=settings.supplemental_evidence.enabled,
            )
            if _is_locked_native_h5_v2_run_record(record) else None
        ),
        reuse_planner=build_current_rule_reuse_planner(
            enabled=rule_result_reuse_enabled(settings)
        ),
        judge=(
            build_h5_targeted_recapture_rule_judge_agent()
            if settings.supplemental_evidence.enabled
            and _is_locked_native_h5_v2_run_record(record)
            else None
        ),
    )

    async def drain_snapshot_before_deadline():
        if h5_deadline is None:
            return await consumer.drain_snapshot(
                task_id=record.task_id, consumer_agent_run_id=consumer_agent_run_id,
            )
        remaining = (h5_deadline - datetime.now(UTC)).total_seconds()
        if remaining > 0:
            timer = asyncio.timeout(remaining)
            try:
                async with timer:
                    return await consumer.drain_snapshot(
                        task_id=record.task_id, consumer_agent_run_id=consumer_agent_run_id,
                    )
            except TimeoutError:
                if not timer.expired():
                    raise
        usage = dict(record.usage or {})
        usage["h5_snapshot_deadline_exhausted"] = True
        record.usage = usage
        return None

    with bind_agent_run_context(
        AgentRunContext(
            task_id=record.task_id,
            agent_run_id=consumer_agent_run_id,
            task_config=dict(record.task_config or {}),
        )
    ):
        claimed_count = 0
        completed_count = 0
        recovered_count = 0
        deferred_count = 0
        for round_index in range(inspection_settings.max_attempts):
            result = await drain_snapshot_before_deadline()
            if result is None:
                break
            claimed_count += result.claimed_count
            completed_count += result.completed_count
            recovered_count += result.recovered_count
            deferred_count += result.deferred_count
            result_usage = getattr(result, "usage", None)
            if result_usage:
                if consumer_agent_run_id == record.run_id:
                    record.usage = merge_usage_payloads(record.usage, result_usage)
                else:
                    usage_recorded = await service.record_consumer_usage(
                        task_id=record.task_id,
                        consumer_agent_run_id=consumer_agent_run_id,
                        usage=result_usage,
                    )
                    if not usage_recorded:
                        logger.warning(
                            "task_id=%s consumer_run_id=%s 规则消费用量写入失败。",
                            record.task_id,
                            consumer_agent_run_id,
                        )
            if result.deferred_count == 0:
                break
            if round_index + 1 < inspection_settings.max_attempts:
                delay = inspection_settings.retry_base_seconds
                if h5_deadline is not None:
                    delay = min(delay, max(0, (h5_deadline - datetime.now(UTC)).total_seconds()))
                await asyncio.sleep(delay)
    h5_unfulfillable_count = 0
    h5_reopened_count = 0
    h5_replayed_work_count = 0
    h5_settled_work_count = 0
    h5_settled_rule_count = 0
    if _is_locked_native_h5_v2_run_record(record):
        h5_snapshot_settlement = PostgresH5ProbeTerminalSettlementRepository()
        # 补采请求可能在本轮进入收口前已经 satisfied/unfulfillable。旧逻辑只有
        # “本轮新终结了请求”才唤醒 waiting_evidence，导致既有 PASS 补采永远
        # 不重判，最后被通用门禁错误地批量判成 FINALIZATION_INCOMPLETE。
        # 每轮先终结失去活会话的请求，再统一结算/重开；有重开项就立即消费，
        # 直到没有可重判工作，之后才进入停滞结算和最终门禁。
        for _closure_round in range(inspection_settings.max_attempts + 1):
            if settings.supplemental_evidence.enabled and (
                h5_deadline is None or datetime.now(UTC) < h5_deadline
            ):
                await _drain_h5_closed_snapshots(
                    record, settings=settings, worker_instance_id=worker_instance_id,
                )
            h5_unfulfillable_count += (
                await h5_snapshot_settlement.settle_requests_after_session_close(
                    task_id=record.task_id, agent_run_id=record.run_id
                )
            )
            reopened_count = await service.reopen_waiting_evidence(task_id=record.task_id)
            h5_reopened_count += reopened_count
            if reopened_count <= 0:
                break
            for replay_round in range(inspection_settings.max_attempts):
                replay = await drain_snapshot_before_deadline()
                if replay is None:
                    break
                h5_replayed_work_count += replay.claimed_count
                claimed_count += replay.claimed_count
                completed_count += replay.completed_count
                recovered_count += replay.recovered_count
                deferred_count += replay.deferred_count
                replay_usage = getattr(replay, "usage", None)
                if replay_usage:
                    if consumer_agent_run_id == record.run_id:
                        record.usage = merge_usage_payloads(record.usage, replay_usage)
                    else:
                        usage_recorded = await service.record_consumer_usage(
                            task_id=record.task_id,
                            consumer_agent_run_id=consumer_agent_run_id,
                            usage=replay_usage,
                        )
                        if not usage_recorded:
                            logger.warning(
                                "task_id=%s consumer_run_id=%s H5 补采重判用量写入失败。",
                                record.task_id,
                                consumer_agent_run_id,
                            )
                if replay.deferred_count == 0:
                    break
                if replay_round + 1 < inspection_settings.max_attempts:
                    delay = inspection_settings.retry_base_seconds
                    if h5_deadline is not None:
                        delay = min(delay, max(0, (h5_deadline - datetime.now(UTC)).total_seconds()))
                    await asyncio.sleep(delay)
        if h5_unfulfillable_count > 0 or h5_reopened_count > 0:
            logger.info(
                "task_id=%s run_id=%s H5 会话关闭后补采收口：终结 %d 个请求，"
                "重开 %d 个工作项，重判领取 %d 个工作项。",
                record.task_id,
                record.run_id,
                h5_unfulfillable_count,
                h5_reopened_count,
                h5_replayed_work_count,
            )
        h5_closure = await service.settle_stalled_work_for_closure(
            task_id=record.task_id,
            # Main Agent 已结束且页面会话已关闭，不会再产生新的 Snapshot。
            # 此时任务尚未进入 session_draining，必须把这项收口事实显式传给仓储，
            # 否则已 unfulfillable 的补采会被通用最终门禁误判成技术失败。
            snapshot_generation_closed=True,
        )
        h5_settled_work_count = h5_closure.settled_work_item_count
        h5_settled_rule_count = h5_closure.settled_rule_count
        if h5_closure.settled:
            logger.info(
                "task_id=%s run_id=%s H5 收口阶段已将 %d 个停滞工作项、%d 条规则"
                "按正式证据不足结算：%s",
                record.task_id,
                record.run_id,
                h5_settled_work_count,
                h5_settled_rule_count,
                dict(h5_closure.reason_counts),
            )
    if h5_publication_enforced:
        await service.finish_h5_consumer_run_if_idle(
            task_id=record.task_id,
            consumer_agent_run_id=consumer_agent_run_id,
        )
    usage = dict(record.usage or {})
    usage["inspection_work_items_claimed"] = claimed_count
    usage["inspection_work_items_completed"] = completed_count
    usage["inspection_work_items_recovered"] = recovered_count
    usage["inspection_work_items_deferred"] = deferred_count
    if _is_locked_native_h5_v2_run_record(record):
        usage["h5_snapshot_recapture_unfulfillable_count"] = h5_unfulfillable_count
        usage["h5_snapshot_recapture_reopened_work_count"] = h5_reopened_count
        usage["h5_snapshot_recapture_replayed_work_count"] = h5_replayed_work_count
        usage["h5_snapshot_settled_work_count"] = h5_settled_work_count
        usage["h5_snapshot_settled_rule_count"] = h5_settled_rule_count
    record.usage = usage


# 语义规则批次默认允许 90 秒；收口停滞窗口必须覆盖完整批次预算并留出落库余量，
# 否则合法的慢响应会在自身超时前被最终门禁批量判死。
INSPECTION_FINALIZE_GRACE_SECONDS = 120.0
INSPECTION_FINALIZE_GRACE_POLL_SECONDS = 2.0
INSPECTION_FINALIZE_MAX_WAIT_SECONDS = 900.0
# H5 的规则批量消费可长于通用窗口，但最终化不能无限等待。
H5_INSPECTION_FINALIZE_MAX_WAIT_SECONDS = 5400.0
# 聚合判定只有非终态布尔可观测，无法区分在途与死件；等待窗只兜常规 LLM 收尾时长，
# 超窗的残余照旧由最终门禁失败关闭。
AGGREGATE_FINALIZE_GRACE_MAX_WAIT_SECONDS = 60.0


def _h5_inspection_deadline(record: AgentRunRecord) -> datetime | None:
    """只有原生 H5 使用原 Run 墙钟；App/Web 的现有收口策略不变。"""
    if not _is_locked_native_h5_v2_run_record(record) or getattr(record, "started_at", None) is None:
        return None
    limits = build_h5_task_runtime_settings(
        task_config=record.task_config,
        loop_guard=record.loop_guard,
        page_sampling_policy=record.page_sampling_policy,
        defaults=get_settings().agent_runtime,
    )
    return record.started_at + timedelta(seconds=limits.max_duration_seconds)


async def _wait_for_inflight_inspection_work(
    service: InspectionWorkItemService,
    *,
    task_id: str,
    grace_seconds: float = INSPECTION_FINALIZE_GRACE_SECONDS,
    poll_seconds: float = INSPECTION_FINALIZE_GRACE_POLL_SECONDS,
    max_wait_seconds: float = INSPECTION_FINALIZE_MAX_WAIT_SECONDS,
    absolute_deadline: datetime | None = None,
) -> float:
    """收口门禁前等待在途判定与可继续推进的积压排空，返回实际等待秒数。

    唤醒消费者按 LLM 速度逐项消费；发布批量落地后立即最终化会把尚未
    轮到的工作项整批打成失败（收口抢跑竞态）。因此只要仍存在活跃消费
    Run 能推进的工作（持有效租约的在途判定、可领取积压或计划重试），
    且总量在宽限窗内持续变化，就继续等待；无进展超过宽限窗、消费者
    消失或到达绝对上限时返回，剩余工作照旧由门禁失败关闭。
    """
    if absolute_deadline is not None:
        remaining = min(max_wait_seconds, (absolute_deadline - datetime.now(UTC)).total_seconds())
        if remaining <= 0:
            return 0.0
        started = asyncio.get_running_loop().time()
        timer = asyncio.timeout(remaining)
        try:
            async with timer:
                await _wait_for_inflight_inspection_work(
                    service, task_id=task_id, grace_seconds=grace_seconds,
                    poll_seconds=min(poll_seconds, remaining), max_wait_seconds=remaining,
                )
        except TimeoutError:
            if not timer.expired():
                raise
        return asyncio.get_running_loop().time() - started
    waited = 0.0
    stalled = 0.0
    best_outstanding: int | None = None
    while waited < max_wait_seconds:
        inflight = await service.count_inflight_leased_work(task_id=task_id)
        backlog = 0
        consumer_agent_run_id = await service.find_active_consumer_run(task_id=task_id)
        if consumer_agent_run_id:
            backlog = await service.count_waitable_nonterminal_work(
                task_id=task_id,
                consumer_agent_run_id=consumer_agent_run_id,
            )
        outstanding = inflight + backlog
        if outstanding <= 0:
            return waited
        if best_outstanding is None or outstanding < best_outstanding:
            best_outstanding = outstanding
            stalled = 0.0
        else:
            stalled += poll_seconds
            if stalled >= grace_seconds:
                return waited
        logger.info(
            "task_id=%s 收口门禁等待 %d 个在途判定与 %d 个可推进积压（已等 %.0fs）。",
            task_id,
            inflight,
            backlog,
            waited,
        )
        await asyncio.sleep(poll_seconds)
        waited += poll_seconds
    return waited


async def _wait_for_aggregate_inspection_work(
    service: AggregateInspectionWorkItemService,
    *,
    task_id: str,
    poll_seconds: float = INSPECTION_FINALIZE_GRACE_POLL_SECONDS,
    max_wait_seconds: float = AGGREGATE_FINALIZE_GRACE_MAX_WAIT_SECONDS,
    absolute_deadline: datetime | None = None,
) -> float:
    """有限窗内等聚合工作项排空，返回实际等待秒数。"""
    if absolute_deadline is not None:
        remaining = min(max_wait_seconds, (absolute_deadline - datetime.now(UTC)).total_seconds())
        if remaining <= 0:
            return 0.0
        started = asyncio.get_running_loop().time()
        timer = asyncio.timeout(remaining)
        try:
            async with timer:
                await _wait_for_aggregate_inspection_work(
                    service, task_id=task_id, poll_seconds=min(poll_seconds, remaining),
                    max_wait_seconds=remaining,
                )
        except TimeoutError:
            if not timer.expired():
                raise
        return asyncio.get_running_loop().time() - started
    waited = 0.0
    while waited < max_wait_seconds:
        if not await service.has_nonterminal_work(task_id=task_id):
            return waited
        await asyncio.sleep(poll_seconds)
        waited += poll_seconds
    return waited


async def _finalize_persisted_inspection_work(
    record: AgentRunRecord,
    *,
    settings: Settings,
) -> None:
    """用正式规则结果恢复工作项，并阻止非终态工作越过最终报告门禁。"""
    if record.task_id is None or record.status not in {
        AgentRunQueueStatus.COMPLETED,
        AgentRunQueueStatus.FAILED,
    }:
        return
    # 所有生产调用都紧跟在 _finalize_rule_executions 之后；后者已经等待页面级与
    # 聚合级工作排空。这里再次等待会让同一份停滞积压连续耗尽两次宽限窗口。
    page_service = InspectionWorkItemService(repository=PostgresInspectionWorkItemRepository())
    aggregate_service = AggregateInspectionWorkItemService(
        repository=PostgresAggregateInspectionWorkItemRepository()
    )
    h5_late_settlement = None
    if _is_locked_native_h5_v2_run_record(record):
        # H5 页面会话关闭后，规则消费者仍可能把已发布但尚未领取的 Snapshot
        # 推进到 waiting_evidence。首次排空发生在规则补记之前，不能覆盖这批晚到
        # 工作；最终门禁前必须按同一“不会再生成 Snapshot”事实再结算一次。
        h5_late_settlement = await page_service.settle_stalled_work_for_closure(
            task_id=record.task_id,
            snapshot_generation_closed=True,
        )
    page_result = await page_service.finalize_task_work(
        task_id=record.task_id,
        error_code="INSPECTION_WORK_ITEM_FINALIZATION_INCOMPLETE",
        error_message="Worker 收口时工作项仍缺少正式规则执行结果",
    )
    if h5_late_settlement is not None and page_result.deferred_retry_count > 0:
        # 晚到 Publication/消费者受原截止约束等待；不能覆盖有效租约或刷新宽限。
        deadline = _h5_inspection_deadline(record)
        remaining = (
            max(0.0, (deadline - datetime.now(UTC)).total_seconds())
            if deadline is not None else 0.0
        )
        loop = asyncio.get_running_loop()
        wait_until = loop.time() + min(remaining, INSPECTION_FINALIZE_GRACE_SECONDS)
        while page_result.deferred_retry_count > 0 and loop.time() < wait_until:
            await asyncio.sleep(min(
                INSPECTION_FINALIZE_GRACE_POLL_SECONDS, wait_until - loop.time(),
            ))
            settlement = await page_service.settle_stalled_work_for_closure(
                task_id=record.task_id, snapshot_generation_closed=True,
            )
            h5_late_settlement = replace(
                h5_late_settlement,
                settled_work_item_count=(h5_late_settlement.settled_work_item_count
                                         + settlement.settled_work_item_count),
                settled_rule_count=(h5_late_settlement.settled_rule_count
                                    + settlement.settled_rule_count),
                reason_counts={
                    reason: (h5_late_settlement.reason_counts.get(reason, 0)
                             + settlement.reason_counts.get(reason, 0))
                    for reason in (h5_late_settlement.reason_counts.keys()
                                   | settlement.reason_counts.keys())
                },
            )
            next_result = await page_service.finalize_task_work(
                task_id=record.task_id,
                error_code="INSPECTION_WORK_ITEM_FINALIZATION_INCOMPLETE",
                error_message="Worker 收口时工作项仍缺少正式规则执行结果",
            )
            page_result = replace(
                next_result,
                **{
                    field: getattr(page_result, field) + getattr(next_result, field)
                    for field in (
                        "recovered_work_item_count", "failed_work_item_count",
                        "active_session_failed_count", "abandoned_attempt_count",
                    )
                },
            )
    aggregate_result = await aggregate_service.finalize_task_work(
        task_id=record.task_id,
        error_code="AGGREGATE_INSPECTION_FINALIZATION_INCOMPLETE",
        error_message="Worker 收口时任务级聚合工作仍缺少正式判定结果",
    )
    page_nonterminal = await page_service.has_nonterminal_work(task_id=record.task_id)
    aggregate_nonterminal = await aggregate_service.has_nonterminal_work(task_id=record.task_id)
    if page_nonterminal or aggregate_nonterminal or (
        h5_late_settlement is not None and page_result.deferred_retry_count > 0
    ):
        raise RuntimeError("持久化质检工作项最终门禁未能关闭全部非终态工作")

    usage = dict(record.usage or {})
    usage["inspection_work_items_finalized_from_formal"] = page_result.recovered_work_item_count
    usage["inspection_work_items_failed_at_gate"] = page_result.failed_work_item_count
    usage["inspection_active_session_failed_at_gate"] = page_result.active_session_failed_count
    usage["inspection_attempts_abandoned_at_gate"] = page_result.abandoned_attempt_count
    if h5_late_settlement is not None:
        usage["h5_late_snapshot_settled_work_count"] = (
            h5_late_settlement.settled_work_item_count
        )
        usage["h5_late_snapshot_settled_rule_count"] = h5_late_settlement.settled_rule_count
    usage["aggregate_inspection_work_items_recovered_at_gate"] = (
        aggregate_result.recovered_work_item_count
    )
    usage["aggregate_inspection_work_items_failed_at_gate"] = (
        aggregate_result.failed_work_item_count
    )
    usage["aggregate_inspection_attempts_abandoned_at_gate"] = (
        aggregate_result.abandoned_attempt_count
    )
    record.usage = usage
    failed_work_item_count = (
        page_result.failed_work_item_count + aggregate_result.failed_work_item_count
    )
    if failed_work_item_count <= 0:
        return

    record.status = AgentRunQueueStatus.FAILED
    record.termination_reason = TerminationReason.INCOMPLETE.value
    record.execution_outcome = "needs_review"
    notice = (
        f"最终门禁发现 {failed_work_item_count} 个持久化质检工作项缺少正式结果，任务已转人工复核。"
    )
    record.final_text = f"{record.final_text}\n\n{notice}" if record.final_text else notice


async def _run_h5_posthoc_engine_judgement(record: AgentRunRecord) -> None:
    """收口补记跳过之前，对已留证配对采集做确定性补判；异常只记日志不阻断收口。"""
    if record.task_id is None:
        return
    try:
        service = build_default_h5_posthoc_engine_judge_service(
            judge_and_record=judge_and_record_rule_for_current_run,
        )
        with bind_agent_run_context(
            AgentRunContext(
                task_id=record.task_id,
                agent_run_id=record.run_id,
                task_config=dict(record.task_config or {}),
            )
        ):
            summary = await service.judge_pending(task_id=record.task_id)
    except Exception:  # noqa: BLE001 - 补判通道自身故障不得影响收口
        logger.exception("task_id=%s 收口确定性补判通道异常，继续收口。", record.task_id)
        return
    if summary.judged_pair_count or summary.target_rule_count:
        logger.info(
            "task_id=%s 收口确定性补判：目标 %d 条规则，判定 %d 个配对，新增覆盖：%s。",
            record.task_id,
            summary.target_rule_count,
            summary.judged_pair_count,
            "、".join(summary.covered_rule_codes) or "无",
        )


async def _finalize_rule_executions(
    record: AgentRunRecord, writer: RuleExecutionWriter | None
) -> None:
    if record.task_id is None or record.status not in {
        AgentRunQueueStatus.COMPLETED,
        AgentRunQueueStatus.FAILED,
    }:
        return
    # 确定性补判与收尾补记都以「已落库判定」为去重依据；必须先等在途判定排空，
    # 否则补记会与晚到的正式判定形成同规则双记录。页面级与聚合级各有独立队列，
    # 两条都要等（聚合竞态实例：Toast 规则判定晚补记 2.2 秒落库成矛盾双行）。
    inflight_service = InspectionWorkItemService(repository=PostgresInspectionWorkItemRepository())
    h5_deadline = _h5_inspection_deadline(record)
    h5_wait_options = {"absolute_deadline": h5_deadline} if h5_deadline is not None else {}
    grace_waited = await _wait_for_inflight_inspection_work(
        inflight_service,
        task_id=record.task_id,
        max_wait_seconds=(
            H5_INSPECTION_FINALIZE_MAX_WAIT_SECONDS
            if _is_locked_native_h5_v2_run_record(record)
            else INSPECTION_FINALIZE_MAX_WAIT_SECONDS
        ),
        **h5_wait_options,
    )
    aggregate_inflight_service = AggregateInspectionWorkItemService(
        repository=PostgresAggregateInspectionWorkItemRepository()
    )
    grace_waited += await _wait_for_aggregate_inspection_work(
        aggregate_inflight_service,
        task_id=record.task_id,
        **h5_wait_options,
    )
    if grace_waited > 0:
        usage = dict(record.usage or {})
        usage["inspection_finalize_grace_wait_seconds"] = (
            float(usage.get("inspection_finalize_grace_wait_seconds") or 0.0) + grace_waited
        )
        record.usage = usage
    await _run_h5_posthoc_engine_judgement(record)
    await _guard_h5_rule_backfill(record, inflight_service)
    writer = writer or PostgresTaskRepository()
    result = await writer.finalize_missing_rule_executions(
        task_id=record.task_id,
        agent_run_id=record.run_id,
        finished_at=record.finished_at or datetime.now(UTC),
        candidate_pages=_candidate_pages(
            record.action_trace,
            working_memory=record.working_memory,
        ),
        runtime_rule_fallbacks=_runtime_rule_fallbacks(record.working_memory),
        runtime_page_failure_fallbacks=_runtime_page_failure_fallbacks(record.working_memory),
    )
    if result.unresolved_candidate_count > 0:
        notice = (
            f"系统完整性校验发现 {result.unresolved_candidate_count} 条候选规则未完成判定，"
            "任务已转人工复核。"
        )
        record.final_text = f"{record.final_text}\n\n{notice}" if record.final_text else notice
    await _reconcile_run_llm_usage_from_events(record)


async def _guard_h5_rule_backfill(
    record: AgentRunRecord, service: InspectionWorkItemService,
) -> None:
    """H5 补记不能先于晚到工作结算，也不能替仍在途的正式判定下结论。"""
    if not _is_locked_native_h5_v2_run_record(record):
        return
    deadline = _h5_inspection_deadline(record)
    remaining = max(0.0, (deadline - datetime.now(UTC)).total_seconds()) if deadline else 0.0
    loop = asyncio.get_running_loop()
    wait_until = loop.time() + min(remaining, INSPECTION_FINALIZE_GRACE_SECONDS)
    while True:
        await service.settle_stalled_work_for_closure(
            task_id=record.task_id, snapshot_generation_closed=True,
        )
        inflight = await service.count_inflight_leased_work(task_id=record.task_id)
        nonterminal = await service.has_nonterminal_work(task_id=record.task_id)
        if not inflight and not nonterminal:
            return
        wait = wait_until - loop.time()
        if wait <= 0:
            raise RuntimeError(
                f"H5 正式判定资源尚未排空（有效租约={inflight}，非终态={nonterminal}），拒绝提前规则补记"
            )
        await asyncio.sleep(min(wait, INSPECTION_FINALIZE_GRACE_POLL_SECONDS))


async def _generate_final_report(
    record: AgentRunRecord,
    generator: FinalReportGenerator | None,
    *,
    finalized: bool = True,
    inspection_work_items_enabled: bool = False,
    inspection_work_item_reader: InspectionWorkItemReader | None = None,
    page_exploration_reader: PageExplorationReader | None = None,
    legacy_candidate_reconciliation_enabled: bool = True,
) -> None:
    if record.task_id is None or record.status not in {
        AgentRunQueueStatus.COMPLETED,
        AgentRunQueueStatus.FAILED,
        AgentRunQueueStatus.CANCELED,
    }:
        return
    report_generator = generator or ReportGenerationService(
        query_service=TaskQueryService(repository=PostgresTaskRepository())
    )
    page_exploration = await _page_exploration_for_report(
        record,
        reader=page_exploration_reader,
    )
    record.final_report = await report_generator.generate_task_report(
        record.task_id,
        agent_run_id=record.run_id,
        finalized_at=record.finished_at if finalized else None,
        terminal_task_status=_terminal_task_status(record) if finalized else None,
        termination_reason=record.termination_reason,
        page_exploration=page_exploration,
    )
    if legacy_candidate_reconciliation_enabled and _reconcile_incomplete_final_report(record):
        record.final_report = await report_generator.generate_task_report(
            record.task_id,
            agent_run_id=record.run_id,
            finalized_at=record.finished_at if finalized else None,
            terminal_task_status=_terminal_task_status(record) if finalized else None,
            termination_reason=record.termination_reason,
            page_exploration=page_exploration,
        )
    inspection_summary: dict[str, Any] | None = None
    if inspection_work_items_enabled:
        reader = inspection_work_item_reader or TaskInsightService(
            repository=PostgresTaskInsightRepository()
        )
        try:
            projection = await reader.inspection_work_items(
                record.task_id,
                control_mode="enforced",
            )
            raw_summary = projection.get("summary")
            if isinstance(raw_summary, dict):
                inspection_summary = raw_summary
        except Exception:  # noqa: BLE001 - 观测投影失败不得覆盖正式任务终态
            logger.exception(
                "task_id=%s 读取持久化质检工作项观测快照失败。",
                record.task_id,
            )
    record.final_report["run_observability"] = _run_observability(
        record,
        inspection_work_item_summary=inspection_summary,
    )
    record.final_report["report_stage"] = "final" if finalized else "preview"
    record.final_report["is_final"] = finalized
    record.final_report = normalize_report_payload(record.final_report)


async def _page_exploration_for_report(
    record: AgentRunRecord,
    *,
    reader: PageExplorationReader | None,
) -> dict[str, Any]:
    """H5/App V2 报告使用 PostgreSQL 封存事实，旧流程保持内存口径。"""

    task_config = record.task_config if isinstance(record.task_config, dict) else {}
    target = task_config.get("_task_target")
    target = target if isinstance(target, dict) else {}
    is_v2 = str(task_config.get("workflow_version") or "").casefold() == "v2"
    target_platform = str(target.get("platform") or "").casefold()
    is_web_v2 = is_v2 and target_platform == "web"
    is_h5_v2 = is_v2 and target_platform == "h5"
    is_app_v2 = (
        str(task_config.get("workflow_version") or "").casefold() == "v2"
        and str(target.get("platform") or "").casefold() == "app"
    )
    if (
        is_web_v2
        and task_config.get("execution_mode")
        == TaskExecutionMode.FULL_INSPECTION.value
    ):
        if record.task_id is None:
            raise RuntimeError("Web V2 正式报告缺少 task_id")
        coverage_reader = getattr(reader, "fetch_web_coverage_snapshot", None)
        if not callable(coverage_reader):
            coverage_reader = PostgresTaskInsightRepository().fetch_web_coverage_snapshot
        web_coverage = await coverage_reader(
            task_id=record.task_id,
            agent_run_id=record.run_id,
        )
        if not isinstance(web_coverage, dict):
            raise RuntimeError("Web V2 正式报告缺少数据库 Coverage Snapshot")
        coverage_status = str(web_coverage.get("coverage_status") or "")
        complete = coverage_status in {"complete", "completed_with_gaps"}
        from services.api.app.application.web_report_explanation import load_web_report_explanation

        explanation = await load_web_report_explanation(
            reader if callable(getattr(reader, "fetch_web_report_explanation", None))
            else PostgresTaskInsightRepository(), task_id=record.task_id, agent_run_id=record.run_id,
        )
        if not isinstance(explanation, dict) or (
            explanation.get("finalization_epoch") != web_coverage.get("finalization_epoch")
        ):
            raise RuntimeError("Web 正式报告解释缺失或周期不匹配")
        return {
            "tracking_available": True,
            "complete": complete,
            "quality_status": "complete" if complete else "needs_review",
            "requires_review": not complete,
            "web_coverage": dict(web_coverage),
            "web_report_explanation": explanation,
            "web_logical_pages": explanation.get("web_logical_pages", []),
            "web_identity_diagnostics": explanation.get("web_identity_diagnostics", []),
            "page_results": [],
            "action_results": [],
        }
    if not is_h5_v2 and not is_app_v2:
        if is_web_v2:
            summary = record.web_exploration_summary
            summary_available = isinstance(summary, dict)
            result = dict(summary) if summary_available else {
                "site_id": "unavailable",
                "catalog_version": "unavailable",
                "target_url": target.get("target_url") or target.get("url"),
                "menu_total": 0,
                "executed_same_origin_count": 0,
                "frontier_remaining": 1,
                "menu_ledger": [],
                "status_counts": {},
                "page_timeline": [],
                "page_type_keys": [],
                "error_code": "WEB_EXPLORATION_SUMMARY_UNAVAILABLE",
            }
            complete = (
                summary_available
                and _safe_nonnegative_int(result.get("frontier_remaining")) == 0
            )
            requires_review = (
                record.execution_outcome == "needs_review" or not summary_available
            )
            result.update(
                {
                    "tracking_available": summary_available,
                    "complete": complete,
                    "quality_status": (
                        "needs_review" if requires_review else "complete"
                    ),
                    "requires_review": requires_review,
                    "page_results": [],
                    "action_results": [],
                }
            )
            return result
        return summarize_working_memory(record.working_memory)
    if record.task_id is None:
        raise RuntimeError("H5 V2 最终报告缺少 task_id")
    resolved_reader = reader or TaskInsightService(repository=PostgresTaskInsightRepository())
    if is_app_v2:
        stage = await resolved_reader.app_stage(record.task_id)
        exploration = stage.get("app_exploration")
        if not isinstance(exploration, dict):
            raise RuntimeError("App V2 持久化探索结果缺失")
        coverage = exploration.get("coverage_summary")
        if not isinstance(coverage, dict):
            raise RuntimeError("App V2 持久化覆盖摘要缺失")
        complete = (
            coverage.get("coverage_complete") is True
            and coverage.get("visual_quality_satisfied") is True
            and exploration.get("outcome") == "exploration_completed"
        )
        return {
            "tracking_available": True,
            "complete": complete,
            "quality_status": "complete" if complete else "needs_review",
            "requires_review": not complete,
            "app_coverage": dict(coverage),
            "page_results": [],
            "action_results": [],
        }
    coverage_reader = getattr(reader, "fetch_h5_coverage_snapshot", None)
    if not callable(coverage_reader):
        coverage_reader = PostgresTaskInsightRepository().fetch_h5_coverage_snapshot
    coverage = await coverage_reader(task_id=record.task_id, agent_run_id=record.run_id)
    if not isinstance(coverage, dict):
        raise RuntimeError("H5 V2 报告缺少当前权威 Coverage Snapshot")
    timeline = await resolved_reader.page_timeline(record.task_id)
    if timeline.get("run_id") != record.run_id:
        raise RuntimeError("H5 V2 报告时间线与 Run 不一致")
    summary = timeline.get("exploration_summary")
    if not isinstance(summary, dict):
        raise RuntimeError("H5 V2 持久化页面探索摘要缺失")
    from services.api.app.application.h5_report_coverage import h5_page_exploration_for_report

    return h5_page_exploration_for_report(
        summary, coverage=coverage, task_id=record.task_id, agent_run_id=record.run_id,
    )


def _web_final_report_failure_payload(
    record: AgentRunRecord,
    *,
    page_exploration: dict[str, Any],
) -> dict[str, Any]:
    """在报告服务不可用时保留可查询、无异常细节泄露的 Web 失败快照。"""

    return normalize_report_payload(
        {
            "report_version": "v2",
            "report_stage": "final",
            "is_final": True,
            "agent_run_id": record.run_id,
            "termination_reason": record.termination_reason,
            "generated_at": datetime.now(UTC),
            "task": {
                "task_id": record.task_id,
                "status": _terminal_task_status(record),
            },
            "summary": {
                "overall_status": "error",
                "error_code": "WEB_FINAL_REPORT_GENERATION_FAILED",
            },
            "page_exploration": page_exploration,
            "target_page_coverage": [],
            "groups": [],
            "attention_results": [],
            "rule_results": [],
            "evidence_entries": [],
            "run_observability": _run_observability(record),
        }
    )


def _run_observability(
    record: AgentRunRecord,
    *,
    inspection_work_item_summary: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """生成不含业务正文的 Run 资源与审计载荷摘要。"""

    usage = dict(record.usage or {})
    working_memory_bytes = len(
        json.dumps(
            record.working_memory,
            ensure_ascii=False,
            default=str,
        ).encode("utf-8")
    )
    inspection_observability = {
        "claimed": usage.get("inspection_work_items_claimed", 0),
        "completed": usage.get("inspection_work_items_completed", 0),
        "recovered": usage.get("inspection_work_items_recovered", 0),
        "deferred": usage.get("inspection_work_items_deferred", 0),
        "finalized_from_formal": usage.get("inspection_work_items_finalized_from_formal", 0),
        "failed_at_gate": usage.get("inspection_work_items_failed_at_gate", 0),
        "active_session_failed_at_gate": usage.get("inspection_active_session_failed_at_gate", 0),
        "attempts_abandoned_at_gate": usage.get("inspection_attempts_abandoned_at_gate", 0),
    }
    if inspection_work_item_summary is not None:
        status_counts = inspection_work_item_summary.get("status_counts")
        status_counts = status_counts if isinstance(status_counts, dict) else {}
        attempt_status_counts = inspection_work_item_summary.get("attempt_status_counts")
        attempt_status_counts = (
            attempt_status_counts if isinstance(attempt_status_counts, dict) else {}
        )
        claimed_count = sum(
            _safe_nonnegative_int(value) for value in attempt_status_counts.values()
        )
        completed_count = sum(
            _safe_nonnegative_int(status_counts.get(status)) for status in ("succeeded", "partial")
        )
        inspection_observability["claimed"] = max(
            _safe_nonnegative_int(inspection_observability["claimed"]),
            claimed_count,
        )
        inspection_observability["completed"] = max(
            _safe_nonnegative_int(inspection_observability["completed"]),
            completed_count,
        )
        inspection_observability["snapshot"] = inspection_work_item_summary

    return {
        "input_tokens": usage.get("input_tokens"),
        "output_tokens": usage.get("output_tokens"),
        "total_tokens": usage.get("total_tokens"),
        "ai_calls": usage.get("ai_calls"),
        "worker_rss_before_bytes": usage.get("worker_rss_before_bytes"),
        "worker_rss_after_bytes": usage.get("worker_rss_after_bytes"),
        "worker_rss_delta_bytes": usage.get("worker_rss_delta_bytes"),
        "working_memory_bytes": working_memory_bytes,
        "action_trace_count": len(record.action_trace),
        "rss_scope": "worker_process",
        "inspection_work_items": inspection_observability,
    }


def _safe_nonnegative_int(value: Any) -> int:
    """把观测计数转换为非负整数，拒绝布尔值和异常载荷。"""
    if isinstance(value, bool):
        return 0
    try:
        result = int(value)
    except (TypeError, ValueError):
        return 0
    return max(result, 0)


def _reconcile_incomplete_final_report(record: AgentRunRecord) -> bool:
    """最终报告不完整时撤销假完成，并要求状态存储把任务转人工复核。"""
    if record.status != AgentRunQueueStatus.COMPLETED or not isinstance(record.final_report, dict):
        return False
    summary = record.final_report.get("summary")
    if not isinstance(summary, dict):
        return False
    # 业务问题或局部页面失败也会让 overall_status=incomplete，但只要逐页候选
    # 规则已经形成终态，Run 仍属于技术完成、任务进入 review。只有明确存在
    # 候选执行缺口时才撤销 completed，避免把页面级隔离重新升级成整 Run 失败。
    unresolved_candidate_rules = summary.get("unresolved_candidate_rules")
    try:
        unresolved_count = int(unresolved_candidate_rules or 0)
    except (TypeError, ValueError):
        unresolved_count = 0
    if unresolved_count <= 0:
        candidate_count = _safe_nonnegative_int(summary.get("candidate_rule_count"))
        completed_count = _safe_nonnegative_int(summary.get("completed_candidate_rules"))
        if (
            summary.get("candidate_execution_complete") is False
            and candidate_count <= 0
            and completed_count <= 0
        ):
            unresolved_count = 1
        else:
            return False

    if _is_budget_limited_candidate_gap(record):
        if record.execution_outcome != "needs_review":
            record.execution_outcome = "needs_review"
            return True
        return False

    record.status = AgentRunQueueStatus.FAILED
    record.termination_reason = TerminationReason.INCOMPLETE.value
    if "任务已转人工复核" not in (record.final_text or ""):
        record.final_text = (
            f"{record.final_text}\n\n{INCOMPLETE_REPORT_NOTICE}"
            if record.final_text
            else INCOMPLETE_REPORT_NOTICE
        )
    return True


def _is_budget_limited_candidate_gap(record: AgentRunRecord) -> bool:
    """识别采样预算耗尽导致的候选缺口，避免把受控收口误判为技术失败。"""
    report = record.final_report
    if not isinstance(report, dict):
        return False
    page_exploration = report.get("page_exploration")
    if not isinstance(page_exploration, dict):
        return False
    if page_exploration.get("complete") is not True:
        return False
    if _safe_nonnegative_int(page_exploration.get("pending_work_count")) > 0:
        return False
    if _safe_nonnegative_int(page_exploration.get("pending_action_count")) > 0:
        return False
    return _has_budget_limit_trace(record.action_trace)


def _has_budget_limit_trace(action_trace: list[dict]) -> bool:
    """从动作轨迹中确认是否存在明确的页面采样/导航深度预算阻断。"""
    for entry in action_trace:
        if not isinstance(entry, dict) or entry.get("status") != "denied":
            continue
        if _trace_error_code(entry) in _BUDGET_LIMIT_TRACE_ERROR_CODES:
            return True
    return False


def _trace_error_code(entry: dict) -> str | None:
    """兼容读取动作轨迹顶层或 Tool 结果内的错误码。"""
    error_code = entry.get("error_code")
    if isinstance(error_code, str) and error_code.strip():
        return error_code.strip()
    result = entry.get("result")
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except json.JSONDecodeError:
            return None
    if isinstance(result, dict):
        error_code = result.get("error_code")
        if isinstance(error_code, str) and error_code.strip():
            return error_code.strip()
    return None


def _terminal_task_status(record: AgentRunRecord) -> str | None:
    """根据 Run 终止原因确定最终业务任务状态。"""
    if record.execution_outcome == "needs_review":
        return "review"
    if record.status is AgentRunQueueStatus.CANCELED:
        return "canceled"
    if record.status != AgentRunQueueStatus.FAILED:
        return None
    if is_review_termination_reason(record.termination_reason):
        return "review"
    return "failed"


def _candidate_pages(
    action_trace: list[dict],
    *,
    working_memory: dict[str, Any] | None = None,
) -> list[CandidatePageReference]:
    """从本次运行轨迹提取成功规则匹配调用的页面引用。"""
    catalog = get_default_page_type_registry().catalog
    pages: dict[tuple[str, str], CandidatePageReference] = {}

    def remember(
        *,
        page_capture_id: str,
        page_type: str,
        candidate_rule_codes: tuple[str, ...] | None,
    ) -> None:
        key = (page_capture_id.strip(), page_type)
        existing = pages.get(key)
        if existing is None or (
            existing.candidate_rule_codes is None and candidate_rule_codes is not None
        ):
            pages[key] = CandidatePageReference(
                page_capture_id=key[0],
                page_type=key[1],
                candidate_rule_codes=candidate_rule_codes,
            )

    for entry in action_trace:
        if entry.get("tool_name") != "candidate_rules_tool" or entry.get("status") != "ok":
            continue
        arguments = entry.get("arguments")
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except json.JSONDecodeError:
                continue
        if not isinstance(arguments, dict):
            continue
        page_capture_id = arguments.get("page_capture_id")
        page_type = arguments.get("page_type")
        if not isinstance(page_capture_id, str) or not page_capture_id.strip():
            continue
        if not isinstance(page_type, str):
            continue
        try:
            normalized = catalog.require_code(page_type).code
        except InvalidPageTypeCodeError:
            continue
        remember(
            page_capture_id=page_capture_id,
            page_type=normalized,
            candidate_rule_codes=_candidate_rule_codes(entry.get("result")),
        )
    for page in _working_memory_candidate_pages(working_memory):
        page_capture_id = page.get("page_capture_id")
        detection = page.get("page_detection")
        page_type = (
            detection.get("page_type") if isinstance(detection, dict) else None
        ) or page.get("page_type")
        candidate_rules = page.get("candidate_rules")
        if (
            not isinstance(page_capture_id, str)
            or not page_capture_id.strip()
            or not isinstance(page_type, str)
            or not isinstance(candidate_rules, list)
        ):
            continue
        try:
            normalized = catalog.require_code(page_type).code
        except InvalidPageTypeCodeError:
            continue
        remember(
            page_capture_id=page_capture_id,
            page_type=normalized,
            candidate_rule_codes=_candidate_rule_codes({"rules": candidate_rules}),
        )
    return list(pages.values())


def _working_memory_candidate_pages(
    working_memory: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """提取审计快照中的页面，作为动作输出被截断时的精确候选恢复源。"""
    if not isinstance(working_memory, dict):
        return []
    pages: list[dict[str, Any]] = []
    for field in ("inspected_pages", "terminal_pages", "pending_pages"):
        value = working_memory.get(field)
        if isinstance(value, list):
            pages.extend(item for item in value if isinstance(item, dict))
    current = working_memory.get("current_page")
    if isinstance(current, dict):
        pages.append(current)
    return pages


def _candidate_rule_codes(result: Any) -> tuple[str, ...] | None:
    """读取本次候选查询的精确规则集合；旧轨迹缺失结果时返回兼容标记。"""
    payload = result
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            return None
    if not isinstance(payload, dict):
        return None
    if "rules" not in payload and isinstance(payload.get("text"), str):
        try:
            payload = json.loads(payload["text"])
        except json.JSONDecodeError:
            return None
        if not isinstance(payload, dict):
            return None
    rules = payload.get("rules")
    if not isinstance(rules, list):
        return None
    return tuple(
        dict.fromkeys(
            code
            for item in rules
            if isinstance(item, dict) and (code := str(item.get("rule_code") or "").strip())
        )
    )


def _runtime_rule_fallbacks(
    working_memory: dict[str, Any] | None,
) -> list[RuntimeRuleExecutionFallback]:
    """提取页面隔离或预算终止后已写入内存、尚未成功落库的规则终态。"""
    if not isinstance(working_memory, dict):
        return []
    fallbacks: list[RuntimeRuleExecutionFallback] = []
    seen: set[tuple[str, str, str]] = set()
    for collection_name in ("inspected_pages", "terminal_pages"):
        pages = working_memory.get(collection_name)
        if not isinstance(pages, list):
            continue
        for page in pages:
            if not isinstance(page, dict):
                continue
            page_capture_id = page.get("page_capture_id")
            result = page.get("result")
            if not isinstance(page_capture_id, str) or not isinstance(result, dict):
                continue
            rules = result.get("rules")
            failure = page.get("failure") if isinstance(page.get("failure"), dict) else {}
            failure_code = str(failure.get("code") or "").strip().upper()
            if not isinstance(rules, list):
                continue
            for rule in rules:
                if not isinstance(rule, dict) or rule.get("execution_status") not in {
                    "error",
                    "skipped",
                }:
                    continue
                rule_id = rule.get("rule_id")
                rule_code = rule.get("rule_code")
                if not isinstance(rule_id, str):
                    rule_id = None
                if not isinstance(rule_code, str):
                    rule_code = None
                if rule_id is None and rule_code is None:
                    continue
                key = (page_capture_id, rule_id or "", rule_code or "")
                if key in seen:
                    continue
                seen.add(key)
                fallbacks.append(
                    RuntimeRuleExecutionFallback(
                        page_capture_id=page_capture_id,
                        rule_id=rule_id,
                        rule_code=rule_code,
                        execution_status=str(rule.get("execution_status")),
                        result_summary=_optional_text(rule.get("result_summary")),
                        problem_desc=_optional_text(rule.get("problem_desc")),
                        reason=(
                            "run_budget_exhausted"
                            if failure_code
                            in {
                                "MAX_STEPS_REACHED",
                                "STUCK_DETECTED",
                                "CONTINUATION_LIMIT_REACHED",
                                "RUN_TIMEOUT",
                            }
                            else None
                        ),
                    )
                )
    return fallbacks


def _runtime_page_failure_fallbacks(
    working_memory: dict[str, Any] | None,
) -> list[RuntimePageInspectionFailureFallback]:
    """提取未完成候选匹配却已失败的已识别业务页面，供收尾补齐规则 error。"""
    if not isinstance(working_memory, dict):
        return []
    terminal_pages = working_memory.get("terminal_pages")
    if not isinstance(terminal_pages, list):
        return []
    fallbacks: list[RuntimePageInspectionFailureFallback] = []
    seen_page_capture_ids: set[str] = set()
    for page in terminal_pages:
        if not isinstance(page, dict) or page.get("inspection_status") != "failed":
            continue
        page_capture_id = _optional_text(page.get("page_capture_id"))
        page_type = _optional_text(page.get("page_type"))
        detection = page.get("page_detection")
        failure = page.get("failure")
        if (
            page_capture_id is None
            or page_type is None
            or page_capture_id in seen_page_capture_ids
            or not isinstance(detection, dict)
            or detection.get("rule_matching_allowed") is not True
            or not isinstance(failure, dict)
        ):
            continue
        failure_code = _optional_text(failure.get("code")) or "PAGE_INSPECTION_FAILED"
        failure_message = (
            _optional_text(failure.get("message")) or "页面在规则匹配完成前失败，规则无法继续判定。"
        )
        seen_page_capture_ids.add(page_capture_id)
        fallbacks.append(
            RuntimePageInspectionFailureFallback(
                page_capture_id=page_capture_id,
                page_type=page_type,
                failure_code=failure_code,
                failure_message=failure_message,
            )
        )
    return fallbacks


def _optional_text(value: Any) -> str | None:
    """将运行时快照中的非空文本规范化，避免把无效值传入收尾 SQL。"""
    return value.strip() if isinstance(value, str) and value.strip() else None


WORKER_HEARTBEAT_INTERVAL_SECONDS = 10


def _build_default_h5_evidence_handoff() -> H5EvidenceHandoffService:
    """组合 H5 已持久化证据门禁与正式快照质检发布器。"""
    settings = get_settings()
    publication_repository = PostgresH5InspectionPublicationRepository(
        max_attempts=settings.inspection_work_items.max_attempts,
        retry_delay_seconds=max(1, int(settings.inspection_work_items.retry_base_seconds)),
    )
    manifest_repository = PostgresEvidenceManifestRepository(
        permitted_task_statuses=("queued", "running", "review", "failed")
    )
    artifact_store = build_evidence_artifact_store(settings.evidence_storage)
    return H5EvidenceHandoffService(
        reader=PostgresH5EvidenceCaptureReader(),
        publisher=build_default_app_capture_inspection_publisher(),
        object_verifier=H5EvidenceObjectVerifier(artifact_store),
        inventory=PostgresH5EvidenceInventoryRepository(),
        publication_writer=publication_repository,
        active_session_coverage_reader=publication_repository,
        manifest_finalizer=H5EvidenceManifestFinalizer(
            repository=manifest_repository,
            finalizer=AppEvidenceManifestFinalizer(
                repository=manifest_repository,
                artifact_store=artifact_store,
            ),
        ),
    )


async def _worker_heartbeat_loop(
    queue: RedisAgentRunQueue,
    stop_event: asyncio.Event,
    *,
    instance_id: str | None = None,
    interval_seconds: float = WORKER_HEARTBEAT_INTERVAL_SECONDS,
) -> None:
    """进程级心跳：主循环存活期间周期写 Redis，供工作台系统状态接口判活。

    放在启动自检之后才开始跳——启动即崩（如规则引擎配置校验失败）的 Worker
    不会产生心跳，前端能据此提示"Worker 未运行"而不是让任务无限排队装死。
    单次写入失败只告警，不影响任务消费。
    """
    resolved_instance_id = instance_id or _new_worker_instance_id()
    worker_roles = get_settings().worker.roles
    while not stop_event.is_set():
        try:
            await queue.record_worker_heartbeat(resolved_instance_id, roles=worker_roles)
        except Exception:  # noqa: BLE001 - 心跳失败不能拖垮 Worker
            logger.warning("worker 心跳写入失败。", exc_info=True)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval_seconds)
        except TimeoutError:
            continue


async def _worker_loop(
    worker_id: int,
    queue: RedisAgentRunQueue,
    settings: Settings,
    stop_event: asyncio.Event,
    state_store: AgentRunStateStore,
    worker_instance_id: str | None = None,
    app_workflow_handler: AppWorkflowHandler | None = None,
    h5_workflow_handler: H5WorkflowHandler | None = None,
    web_workflow_handler: WebWorkflowHandler | None = None,
    h5_evidence_handoff: H5EvidenceHandoffPort | None = None,
    web_inspection_reconciliation: Any | None = None,
    final_report_generator: FinalReportGenerator | None = None,
    web_final_report_generator: FinalReportGenerator | None = None,
    h5_shadow_recorder: H5WorkflowShadowRecorder | None = None,
    task_finalization_service: TaskFinalizationService | None = None,
    sealed_inspection_publisher: AppSealedInspectionPublisherPort | None = None,
    app_publication_repository: PostgresAppInspectionPublicationRepository | None = None,
    app_run_recovery: AppRunRecoveryRepository | None = None,
    rule_engine_configuration_guard: RuleEngineConfigurationGuard | None = None,
    run_capacity: asyncio.BoundedSemaphore | None = None,
) -> None:
    logger.info("worker[%d] 已启动，开始消费队列。", worker_id)
    while not stop_event.is_set():
        capacity_acquired = False
        try:
            if run_capacity is not None:
                # 空队列不占共享槽；忙队列先排队领取许可，消息仍留在 Redis 中。
                if await queue.queue_depth() == 0:
                    try:
                        await asyncio.wait_for(
                            stop_event.wait(),
                            timeout=min(settings.worker.poll_timeout_seconds, 0.25),
                        )
                    except TimeoutError:
                        pass
                    continue
                await run_capacity.acquire()
                capacity_acquired = True
                if stop_event.is_set():
                    run_capacity.release()
                    break
            try:
                run_id = await queue.dequeue(
                    timeout=(
                        min(settings.worker.poll_timeout_seconds, 0.1)
                        if run_capacity is not None
                        else settings.worker.poll_timeout_seconds
                    )
                )
            except BaseException:
                if capacity_acquired:
                    run_capacity.release()
                raise
        except Exception:  # noqa: BLE001 - Redis 短断不能终止消费协程
            logger.warning("worker[%d] 从队列取任务失败，将继续重试。", worker_id, exc_info=True)
            try:
                await asyncio.wait_for(
                    stop_event.wait(),
                    timeout=WORKER_QUEUE_RETRY_SECONDS,
                )
            except TimeoutError:
                continue
            break
        if run_id is None:
            if capacity_acquired:
                run_capacity.release()
            continue
        try:
            await _process_one(
                queue,
                run_id,
                settings,
                state_store=state_store,
                worker_instance_id=worker_instance_id,
                app_workflow_handler=app_workflow_handler,
                h5_workflow_handler=h5_workflow_handler,
                web_workflow_handler=web_workflow_handler,
                h5_evidence_handoff=h5_evidence_handoff,
                web_inspection_reconciliation=web_inspection_reconciliation,
                final_report_generator=final_report_generator,
                web_final_report_generator=web_final_report_generator,
                h5_shadow_recorder=h5_shadow_recorder,
                task_finalization_service=task_finalization_service,
                sealed_inspection_publisher=sealed_inspection_publisher,
                app_publication_repository=app_publication_repository,
                app_run_recovery=app_run_recovery,
                rule_engine_configuration_guard=rule_engine_configuration_guard,
            )
        except WebActiveSessionWorkPendingError:
            logger.info(
                "worker[%d] run_id=%s Web active-session 正式工作尚未排空，"
                "等待后台收口重试。",
                worker_id,
                run_id,
            )
        except Exception:
            logger.exception("worker[%d] 处理 run_id=%s 时出现未预期异常。", worker_id, run_id)
        finally:
            if capacity_acquired:
                run_capacity.release()
    logger.info("worker[%d] 收到停止信号，已退出。", worker_id)


def _start_worker_loops(
    *,
    concurrency: int,
    queue: RedisAgentRunQueue,
    settings: Settings,
    stop_event: asyncio.Event,
    state_store: AgentRunStateStore,
    worker_instance_id: str,
    app_workflow_handler: AppWorkflowHandler | None,
    sealed_inspection_publisher: AppSealedInspectionPublisherPort,
    app_publication_repository: PostgresAppInspectionPublicationRepository | None = None,
    app_run_recovery: AppRunRecoveryRepository | None = None,
    h5_workflow_handler: H5WorkflowHandler | None = None,
    h5_evidence_handoff: H5EvidenceHandoffPort | None = None,
    h5_shadow_recorder: H5WorkflowShadowRecorder | None = None,
    task_finalization_service: TaskFinalizationService | None = None,
    rule_engine_configuration_guard: RuleEngineConfigurationGuard | None = None,
    web_workflow_handler: WebWorkflowHandler | None = None,
    run_capacity: asyncio.BoundedSemaphore | None = None,
) -> list[asyncio.Task[None]]:
    """为同一进程内的每个消费协程注入共享的 Workflow 组合根与规则门禁。"""
    return [
        asyncio.create_task(
            _worker_loop(
                worker_id,
                queue,
                settings,
                stop_event,
                state_store,
                worker_instance_id=worker_instance_id,
                app_workflow_handler=app_workflow_handler,
                h5_workflow_handler=h5_workflow_handler,
                h5_evidence_handoff=h5_evidence_handoff,
                h5_shadow_recorder=h5_shadow_recorder,
                task_finalization_service=task_finalization_service,
                sealed_inspection_publisher=sealed_inspection_publisher,
                app_publication_repository=app_publication_repository,
                app_run_recovery=app_run_recovery,
                rule_engine_configuration_guard=rule_engine_configuration_guard,
                web_workflow_handler=web_workflow_handler,
                run_capacity=run_capacity,
            )
        )
        for worker_id in range(concurrency)
    ]


def _start_web_worker_loops(
    *,
    concurrency: int,
    queue: RedisAgentRunQueue,
    settings: Settings,
    stop_event: asyncio.Event,
    state_store: AgentRunStateStore,
    worker_instance_id: str,
    web_workflow_handler: WebWorkflowHandler,
    web_inspection_reconciliation: Any | None = None,
    task_finalization_service: TaskFinalizationService | None = None,
    final_report_generator: FinalReportGenerator | None = None,
    web_final_report_generator: FinalReportGenerator | None = None,
    rule_engine_configuration_guard: RuleEngineConfigurationGuard | None = None,
    run_capacity: asyncio.BoundedSemaphore | None = None,
) -> list[asyncio.Task[None]]:
    """Web 专用消费协程连接独立队列，并按任务模式注入正式收口能力。"""

    return [
        asyncio.create_task(
            _worker_loop(
                worker_id,
                queue,
                settings,
                stop_event,
                state_store,
                worker_instance_id=worker_instance_id,
                web_workflow_handler=web_workflow_handler,
                web_inspection_reconciliation=web_inspection_reconciliation,
                task_finalization_service=task_finalization_service,
                final_report_generator=final_report_generator,
                web_final_report_generator=web_final_report_generator,
                rule_engine_configuration_guard=rule_engine_configuration_guard,
                run_capacity=run_capacity,
            )
        )
        for worker_id in range(concurrency)
    ]


async def _reconcile_canceled_run(
    queue: RedisAgentRunQueue,
    state_store: AgentRunStateStore,
    record: AgentRunRecord,
    *,
    web_formal_session_closer: Any | None = None,
    web_formal_terminal_ledger_finalizer: Any | None = None,
) -> None:
    """重试已取消 Run 尚未完成的 Work 清理与长期终态写入。"""

    if record.cancellation_cleanup_completed and record.cancellation_finalized:
        return
    await _project_run_canceled_and_cleanup(queue=queue, record=record)
    try:
        if _is_locked_web_v2_run_record(record):
            await _terminalize_web_formal_ledgers_for_cancel(
                record=record,
                finalizer=web_formal_terminal_ledger_finalizer,
            )
            await _close_web_formal_session_for_cancel(
                record=record,
                web_formal_session_closer=web_formal_session_closer,
            )
        await _finish_standard_terminal_run(
            queue=queue,
            record=record,
            state_store=state_store,
            progress_recorder=None,
            final_report_generator=None,
            task_finalization_service=None,
            inspection_work_items_enabled=False,
            legacy_report_enabled=False,
        )
    except Exception:  # noqa: BLE001 - 单条取消对账失败必须留待下一轮继续重试
        logger.exception(
            "run_id=%s 已取消运行记录的长期终态重试失败。",
            record.run_id,
        )


async def _is_cancel_requested_safely(
    queue: RedisAgentRunQueue,
    run_id: str,
    *,
    durable_source: AgentRunStateStore | None = None,
    h5: bool = False,
) -> bool:
    """同时读取短期与长期取消事实；长期读取失败时拒绝继续副作用。"""
    try:
        if await queue.is_cancel_requested(run_id):
            return True
    except Exception:  # noqa: BLE001 - Redis 丢失时继续读取长期事实
        logger.warning("run_id=%s 查询 Redis 取消信号失败。", run_id, exc_info=True)
    if durable_source is None:
        return False
    try:
        h5_reader = getattr(durable_source, "is_h5_cancel_requested", None)
        if h5 and callable(h5_reader):
            return await h5_reader(run_id)
        return await durable_source.is_cancel_requested(run_id)
    except Exception as exc:  # noqa: BLE001 - 取消事实不可判定时必须停止副作用
        raise RuntimeError("长期取消事实不可用，拒绝继续执行") from exc


async def _reconcile_orphaned_runs(
    queue: RedisAgentRunQueue,
    state_store: AgentRunStateStore,
    task_finalization_service: TaskFinalizationService | None = None,
    app_run_recovery: AppRunRecoveryRepository | None = None,
    web_formal_session_closer: Any | None = None,
    web_inspection_reconciliation: Any | None = None,
    web_final_report_generator: FinalReportGenerator | None = None,
) -> None:
    """两阶段确认失联 Worker，避免 Redis 短断导致活跃 Run 被误终结。"""
    records_by_id = {record.run_id: record for record in await queue.list_records()}
    from services.api.app.domain.exploration.web_runtime_contract import is_legacy_web_execution
    from services.api.app.infrastructure.queue.web_legacy_recovery import reconcile_legacy_web_run

    try:
        legacy_records = await state_store.list_legacy_web_recovery_records(limit=100)
    except Exception:  # noqa: BLE001 - 短断后下一轮继续从长期非终态恢复
        logger.warning("长期旧 Web 运行恢复扫描失败。", exc_info=True)
        legacy_records = []
    for legacy_record in legacy_records:
        records_by_id.setdefault(legacy_record.run_id, legacy_record)
    durable_cancellation_ids: set[str] = set()
    try:
        durable_records = await state_store.list_cancellation_recovery_records(limit=100)
    except Exception:  # noqa: BLE001 - PG 扫描短断不能阻止 Redis 内已有记录对账
        logger.warning("长期取消恢复扫描失败，本轮仅处理 Redis 快照。", exc_info=True)
        durable_records = []
    for durable_record in durable_records:
        durable_cancellation_ids.add(durable_record.run_id)
        records_by_id[durable_record.run_id] = durable_record

    for record in records_by_id.values():
        if (
            record.run_id in durable_cancellation_ids
            or record.status is AgentRunQueueStatus.CANCELED
        ):
            await _reconcile_canceled_run(
                queue,
                state_store,
                record,
                web_formal_session_closer=web_formal_session_closer,
            )
            continue
        if is_legacy_web_execution(record.task_config):
            try:
                await reconcile_legacy_web_run(queue, state_store, record)
            except Exception:  # noqa: BLE001 - 保留长期非终态供周期扫描重试
                logger.warning("旧 Web 运行收口暂未持久化，将自动重试。", exc_info=True)
            continue
        if record.status != AgentRunQueueStatus.RUNNING:
            continue
        if await queue.is_worker_instance_alive(record.worker_instance_id):
            if record.worker_missing_since is not None:
                record.worker_missing_since = None
                await queue.save(record)
            continue
        now = datetime.now(UTC)
        if record.worker_missing_since is None:
            record.worker_missing_since = now
            await queue.save(record)
            continue
        if (
            now - record.worker_missing_since
        ).total_seconds() < ORPHAN_RECONCILIATION_GRACE_SECONDS:
            continue

        current = await queue.get(record.run_id)
        if (
            current is None
            or current.status != AgentRunQueueStatus.RUNNING
            or current.worker_instance_id != record.worker_instance_id
        ):
            continue
        if await queue.is_worker_instance_alive(current.worker_instance_id):
            current.worker_missing_since = None
            await queue.save(current)
            continue

        try:
            cancel_requested = await _is_cancel_requested_safely(
                queue,
                current.run_id,
                durable_source=state_store,
            )
        except RuntimeError:
            logger.warning(
                "run_id=%s 长期取消事实不可判定，本轮不推进孤儿终态。",
                current.run_id,
                exc_info=True,
            )
            continue
        if app_run_recovery is not None and _is_locked_app_run(current):
            snapshot = await app_run_recovery.get_recovery_snapshot(
                agent_run_id=current.run_id,
            )
            if snapshot is None:
                # 进程可能死在 PG Run 已终态、Redis 终态投影尚未保存之间。
                # 此时只恢复长期终态投影，绝不能再按旧 RUNNING 快照写终态。
                finished = await state_store.get_finished_record(run_id=current.run_id)
                if finished is not None:
                    await _save_queue_projection(queue, finished)
                    continue
            if snapshot is not None:
                if snapshot.execution_lease_active:
                    # PostgreSQL 租约仍有效时，Redis 心跳缺失只能视为投影延迟。
                    continue
                if snapshot.ready and not cancel_requested:
                    recovered = snapshot.record.model_copy(deep=True)
                    recovered.status = AgentRunQueueStatus.QUEUED
                    recovered.worker_instance_id = None
                    recovered.worker_missing_since = None
                    recovered.finished_at = None
                    recovered.termination_reason = None
                    recovered.error_message = None
                    recovered.error_category = None
                    # 已完成心跳宽限、PG 无活跃执行租约，才允许替换这条失联 running 投影。
                    await queue.restore_queued(
                        recovered,
                        expected_running_worker_instance_id=current.worker_instance_id,
                    )
                    continue
                if not cancel_requested:
                    # future retry_wait / 尚未过期的 Bootstrap 只等待 PG 时间，不提前失败。
                    continue

        if not cancel_requested and _is_locked_web_v2_run_record(current):
            try:
                await _reconcile_orphaned_web_formal_run(
                    queue=queue,
                    state_store=state_store,
                    record=current,
                    task_finalization_service=task_finalization_service,
                    reconciliation=web_inspection_reconciliation,
                    final_report_generator=web_final_report_generator,
                    web_formal_session_closer=web_formal_session_closer,
                )
            except Exception:  # noqa: BLE001 - 单条 Web 恢复失败不得拖垮启动扫描
                logger.exception(
                    "run_id=%s Web full 孤儿恢复失败，将由下一轮继续重试。",
                    current.run_id,
                )
            continue

        target = current.task_config.get("_task_target", {})
        web_exploration = (
            isinstance(target, dict) and target.get("platform") == "web"
            and current.task_config.get("execution_mode") == "exploration_only"
        )
        if not cancel_requested and current.task_id is not None and web_exploration:
            try:
                authoritative = await state_store.get_finished_record(run_id=current.run_id)
                if authoritative is not None:
                    await _save_queue_projection(queue, authoritative)
                    continue
                if not await _settle_web_exploration_orphan(
                    task_id=current.task_id, run_id=current.run_id
                ):
                    continue
            except Exception:  # noqa: BLE001 - 租约或持久收束失败时不得先终结 Run
                logger.exception("run_id=%s Web 纯探索孤儿收束未完成，将重试。", current.run_id)
                continue
        elif not cancel_requested and current.task_id is not None and _is_h5_run_record(current):
            try:
                await _fail_h5_orphaned_nonterminal_runtime(task_id=current.task_id)
            except Exception:  # noqa: BLE001 - 收束失败时保留 running，下一轮继续重试
                logger.warning(
                    "run_id=%s H5 孤儿运行的非终态 Work/Action 收束失败，本轮不写长期终态。",
                    current.run_id,
                    exc_info=True,
                )
                continue

        current.status = (
            AgentRunQueueStatus.CANCELED if cancel_requested else AgentRunQueueStatus.FAILED
        )
        current.finished_at = now
        current.termination_reason = (
            TerminationReason.CANCELED.value if cancel_requested else TerminationReason.ERROR.value
        )
        if cancel_requested:
            _project_run_canceled(current)
        if not cancel_requested:
            current.error_category = ErrorCategory.INFRASTRUCTURE.value
            current.error_message = (
                "Worker 实例心跳持续失联并超过宽限期，运行记录已终结；请重新执行任务。"
            )
        current.worker_instance_id = None
        current.worker_missing_since = None
        try:
            finalization_service = _formal_task_finalization_service(
                current,
                task_finalization_service,
            )
            await _finish_standard_terminal_run(
                queue=queue,
                record=current,
                state_store=state_store,
                progress_recorder=None,
                final_report_generator=None,
                task_finalization_service=finalization_service,
            )
        except AgentRunTerminalStateConflict:
            logger.info(
                "run_id=%s 孤儿运行对账时发现 PostgreSQL 已有权威终态，跳过重复写回。",
                current.run_id,
            )
            await _project_authoritative_finished_run(
                queue,
                state_store,
                run_id=current.run_id,
                reason="孤儿运行对账终态冲突",
            )
        except Exception:  # noqa: BLE001 - 单条对账失败不能阻止 Worker 启动
            logger.exception("run_id=%s 孤儿运行记录写入长期终态失败。", current.run_id)


async def _reconcile_orphaned_web_formal_run(
    *,
    queue: RedisAgentRunQueue,
    state_store: AgentRunStateStore,
    record: AgentRunRecord,
    task_finalization_service: TaskFinalizationService | None,
    reconciliation: Any | None,
    final_report_generator: FinalReportGenerator | None,
    web_formal_session_closer: Any | None,
) -> None:
    """让 Web full 孤儿沿用持久对账与审计报告边界。"""

    if (
        record.task_id is None
        or task_finalization_service is None
        or reconciliation is None
        or final_report_generator is None
        or web_formal_session_closer is None
    ):
        logger.error(
            "run_id=%s Web full 孤儿缺少正式恢复依赖，本轮保留可恢复状态。",
            record.run_id,
        )
        return
    authoritative = await state_store.get_finished_record(run_id=record.run_id)
    recovered = authoritative or record.model_copy(deep=True)
    if authoritative is None:
        try:
            await _fail_h5_orphaned_nonterminal_runtime(task_id=record.task_id)
        except Exception:  # noqa: BLE001 - 清理失败时不得提前落 Run 终态
            logger.warning(
                "run_id=%s Web full 孤儿的探索 Work/Action 收束失败，本轮保留可恢复状态。",
                record.run_id,
                exc_info=True,
            )
            return
        recovered.status = AgentRunQueueStatus.FAILED
        recovered.finished_at = datetime.now(UTC)
        recovered.termination_reason = TerminationReason.ERROR.value
        recovered.error_category = ErrorCategory.INFRASTRUCTURE.value
        recovered.error_message = (
            "Worker 实例心跳持续失联并超过宽限期，运行记录已终结；请重新执行任务。"
        )
        recovered.worker_instance_id = None
        recovered.worker_missing_since = None
        try:
            await state_store.record_run_finished(recovered)
        except AgentRunCancellationConflict:
            await _reconcile_canceled_run(
                queue,
                state_store,
                record,
                web_formal_session_closer=web_formal_session_closer,
            )
            return
        except AgentRunTerminalStateConflict:
            authoritative = await state_store.get_finished_record(run_id=record.run_id)
            if authoritative is None:
                raise
            recovered = authoritative
    snapshot = await task_finalization_service.get(task_id=record.task_id)
    if snapshot is None or snapshot.agent_run_id not in {None, record.run_id}:
        raise RuntimeError("Web full 孤儿恢复的 Task/main Run 身份不一致")
    result = await reconciliation.reconcile(
        ReconcileWebInspectionCommand(
            task_id=record.task_id,
            agent_run_id=record.run_id,
            finalization_epoch=snapshot.finalization_epoch,
        )
    )
    if result.status == "gate_ready":
        _apply_web_reconciliation_execution_status(recovered, result)
        await _finish_standard_terminal_run(
            queue=queue,
            record=recovered,
            state_store=state_store,
            progress_recorder=None,
            final_report_generator=final_report_generator,
            task_finalization_service=task_finalization_service,
            inspection_work_items_enabled=True,
            legacy_report_enabled=True,
        )
        return
    if result.status == "already_terminal":
        recovered.worker_instance_id = None
        recovered.worker_missing_since = None
        await _save_queue_projection(queue, recovered)
        return
    if result.status == "needs_review":
        recovered.execution_outcome = ExecutionOutcome.NEEDS_REVIEW.value
    elif result.status == "failed":
        recovered.status = AgentRunQueueStatus.FAILED
        recovered.execution_outcome = ExecutionOutcome.NEEDS_REVIEW.value
    else:
        return
    await _save_queue_projection(queue, recovered)


async def _orphan_reconciliation_loop(
    queue: RedisAgentRunQueue,
    state_store: AgentRunStateStore,
    stop_event: asyncio.Event,
    *,
    task_finalization_service: TaskFinalizationService | None = None,
    app_run_recovery: AppRunRecoveryRepository | None = None,
    web_formal_session_closer: Any | None = None,
    web_inspection_reconciliation: Any | None = None,
    web_final_report_generator: FinalReportGenerator | None = None,
    interval_seconds: float = 5.0,
) -> None:
    """持续对账孤儿 Run，覆盖容器快速重启时旧心跳尚未过期的时间窗。"""
    while not stop_event.is_set():
        try:
            await _reconcile_orphaned_runs(
                queue,
                state_store,
                task_finalization_service,
                app_run_recovery,
                web_formal_session_closer,
                web_inspection_reconciliation,
                web_final_report_generator,
            )
        except Exception:  # noqa: BLE001 - 对账失败不能拖垮正常任务消费
            logger.warning("孤儿运行记录周期对账失败。", exc_info=True)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval_seconds)
        except TimeoutError:
            continue


class CompletionGateConfigurationError(ValueError):
    """正式收口依赖不完整时使用的稳定启动配置错误。"""


def _formal_task_finalization_service(
    record: AgentRunRecord,
    service: TaskFinalizationService | None,
) -> TaskFinalizationService | None:
    """仅把完整质检 Run 交给 Completion Gate。

    exploration_only 的证据封存已经由 App Runtime/Closure 完成，任何 Worker
    恢复或孤儿对账路径都不得因为进程级 Completion Gate 依赖存在而重新创建正式
    质检事实。
    """

    if service is None:
        return None
    try:
        mode = task_execution_mode_from_config(record.task_config)
    except ValueError:
        # 非法快照交给原有 fail-closed 路径处理，避免这里吞掉配置错误。
        return service
    return service if mode is TaskExecutionMode.FULL_INSPECTION else None


def _is_locked_app_run(record: AgentRunRecord) -> bool:
    """只按提交时锁定的 TaskTarget 识别 App，禁止用 URL/文件名猜测。"""

    target = record.task_config.get("_task_target")
    return isinstance(target, dict) and target.get("platform") == "app"


async def _reconcile_recoverable_app_runs(
    queue: RedisAgentRunQueue,
    repository: AppRunRecoveryRepository,
) -> int:
    """从 PostgreSQL 补回 Redis 丢失的 App Run 唤醒，返回新增队列项数量。"""

    restored = 0
    for snapshot in await repository.list_recovery_snapshots(limit=100):
        if not snapshot.ready:
            continue
        record = snapshot.record.model_copy(deep=True)
        record.status = AgentRunQueueStatus.QUEUED
        record.worker_instance_id = None
        record.worker_missing_since = None
        record.finished_at = None
        record.termination_reason = None
        record.error_message = None
        record.error_category = None
        if await queue.restore_queued(record):
            restored += 1
            record_app_metric_safely(
                APP_RUN_RECOVERY_OUTCOME_TOTAL,
                reason_code=snapshot.reason,
                outcome="restored",
            )
            logger.info(
                "run_id=%s 已按 PostgreSQL 恢复 App Run 唤醒，reason=%s",
                record.run_id,
                snapshot.reason,
            )
    return restored


async def _app_run_recovery_loop(
    queue: RedisAgentRunQueue,
    repository: AppRunRecoveryRepository,
    stop_event: asyncio.Event,
    *,
    interval_seconds: float,
) -> None:
    """周期扫描 PG；Redis/Outbox 丢消息只增加延迟，不改变业务事实。"""

    while not stop_event.is_set():
        try:
            await _reconcile_recoverable_app_runs(queue, repository)
        except Exception:  # noqa: BLE001 - 恢复扫描失败不能拖垮已有 Run
            logger.warning("App Run PostgreSQL 恢复扫描失败", exc_info=True)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval_seconds)
        except TimeoutError:
            continue


async def _app_exploration_reconciliation_loop(
    reconciler: AppExplorationReconciler,
    stop_event: asyncio.Event,
    *,
    interval_seconds: float,
) -> None:
    """周期修复 sealed Index 孤儿 Result，并对账独立技术失败事实。"""

    while not stop_event.is_set():
        try:
            summary = await reconciler.reconcile_once()
            if summary.repaired_results or summary.reconciled_failures or summary.error_count:
                logger.info(
                    "App 探索恢复对账完成：repaired_results=%d reconciled_failures=%d "
                    "pending=%d errors=%d",
                    summary.repaired_results,
                    summary.reconciled_failures,
                    summary.pending_facts,
                    summary.error_count,
                )
        except Exception:  # noqa: BLE001 - 对账短时失败不能拖垮现有任务
            logger.warning("App 探索失败与 Result 对账扫描失败", exc_info=True)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval_seconds)
        except TimeoutError:
            continue


async def _app_manifest_reconciliation_loop(
    reconciler: AppManifestBacklogReconciler,
    stop_event: asyncio.Event,
    *,
    interval_seconds: float,
) -> None:
    """周期晋级超龄 PENDING Manifest；对象或数据库短断只延后重试。"""

    while not stop_event.is_set():
        try:
            summary = await reconciler.reconcile_once()
            if summary.finalized or summary.failed:
                logger.info(
                    "App Manifest backlog 对账完成：finalized=%d failed=%d",
                    summary.finalized,
                    summary.failed,
                )
        except Exception:  # noqa: BLE001 - backlog 查询失败不能拖垮现有任务
            logger.warning("App Manifest backlog 扫描失败", exc_info=True)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval_seconds)
        except TimeoutError:
            continue


def validate_completion_gate_configuration(
    settings: Settings,
    *,
    state_store: AgentRunStateStore,
    task_finalization_service: TaskFinalizationService | None,
) -> None:
    """启用任一正式收口路径时拒绝缺失 durable Completion Gate 依赖。"""
    if not (
        settings.workflow.app_qc_enabled
        or settings.workflow.h5_v2_mode == "on"
        or settings.workflow.web_v2_mode == "on"
    ):
        return
    backend = (os.getenv("ALIOTH_AGENT_RUN_STATE_STORE_BACKEND") or "").strip().lower()
    if backend != "postgres":
        raise CompletionGateConfigurationError(
            "Completion Gate 要求 PostgreSQL State Store："
            "ALIOTH_AGENT_RUN_STATE_STORE_BACKEND=postgres"
        )
    if not (os.getenv("ALIOTH_DATABASE_URL") or "").strip():
        raise CompletionGateConfigurationError("Completion Gate 要求 ALIOTH_DATABASE_URL")
    if not isinstance(state_store, PostgresAgentRunStateStore):
        raise CompletionGateConfigurationError("Completion Gate 禁止使用 Noop 或内存 State Store")
    if task_finalization_service is None:
        raise CompletionGateConfigurationError("Completion Gate Service 未注入")


def validate_task_submission_configuration(
    settings: Settings,
    *,
    state_store: AgentRunStateStore,
) -> None:
    """正式 Worker 必须用 PostgreSQL 推进可靠提交生成的同一初始 Run。"""

    del settings
    backend = (os.getenv("ALIOTH_AGENT_RUN_STATE_STORE_BACKEND") or "").strip().lower()
    if backend != "postgres":
        raise CompletionGateConfigurationError(
            "任务提交可靠投影要求 PostgreSQL State Store："
            "ALIOTH_AGENT_RUN_STATE_STORE_BACKEND=postgres"
        )
    if not (os.getenv("ALIOTH_DATABASE_URL") or "").strip():
        raise CompletionGateConfigurationError("任务提交可靠投影要求 ALIOTH_DATABASE_URL")
    if not isinstance(state_store, PostgresAgentRunStateStore):
        raise CompletionGateConfigurationError("任务提交可靠投影禁止使用 Noop 或内存 State Store")


def validate_web_worker_configuration(settings: Settings) -> None:
    """Web 模式和专用消费角色必须成对启停，禁止任务只入队不消费。"""

    enabled = settings.workflow.web_v2_mode != "off"
    has_role = "web_explorer" in settings.worker.roles
    if enabled and not has_role:
        raise ValueError("Web V2 已开启，但 ALIOTH_WORKER_ROLES 未包含 web_explorer")
    if has_role and not enabled:
        raise ValueError("web_explorer 角色已启用，但 ALIOTH_WEB_V2_MODE 仍为 off")
    if settings.workflow.web_v2_mode == "on" and "judge" not in settings.worker.roles:
        raise ValueError("Web 完整质检已开启，但 ALIOTH_WORKER_ROLES 未包含 judge")


COMPLETION_GATE_RETRY_INTERVAL_SECONDS = 5.0
COMPLETION_GATE_RETRY_DEFER_SECONDS = 30.0


async def _retry_pending_task_finalizations(
    queue: RedisAgentRunQueue,
    state_store: AgentRunStateStore,
    service: TaskFinalizationService,
    final_report_generator: FinalReportGenerator | None = None,
    web_final_report_generator: FinalReportGenerator | None = None,
    web_inspection_reconciliation: Any | None = None,
    finalization_stall_timeout_seconds: float = 600.0,
    sealed_inspection_publisher: AppSealedInspectionPublisherPort | None = None,
    settings: Settings | None = None,
) -> int:
    """按 PostgreSQL 事实恢复 Run，重试 Gate 和缺失的最终报告绑定。"""
    recoverable_run_ids: set[str] | None = None
    pending_task_ids: set[str] | None = None
    leases_by_task: dict[str, Any] = {}
    legacy_task_ids: set[str] = set()
    claim_pending = getattr(service, "claim_recoverable_finalizations", None)
    if callable(claim_pending):
        leases = await claim_pending(
            limit=100,
            lease_owner=f"{socket.gethostname()}:{os.getpid()}:completion-gate",
            lease_seconds=COMPLETION_GATE_RETRY_DEFER_SECONDS,
        )
        leases_by_task = {lease.task_id: lease for lease in leases}
        snapshots = [lease.snapshot for lease in leases]
        pending_task_ids = set(leases_by_task)
        recoverable_run_ids = {
            snapshot.agent_run_id for snapshot in snapshots if snapshot.agent_run_id is not None
        }
    list_legacy = getattr(service, "list_recoverable_finalizations", None)
    if callable(list_legacy):
        legacy_snapshots = await list_legacy(limit=100)
        legacy_task_ids = {snapshot.task_id for snapshot in legacy_snapshots}
        legacy_run_ids = {
            snapshot.agent_run_id
            for snapshot in legacy_snapshots
            if snapshot.agent_run_id is not None
        }
        pending_task_ids = (pending_task_ids or set()) | legacy_task_ids
        recoverable_run_ids = (recoverable_run_ids or set()) | legacy_run_ids
        for snapshot in legacy_snapshots:
            await service.defer_finalization_retry(
                task_id=snapshot.task_id,
                retry_after_seconds=COMPLETION_GATE_RETRY_DEFER_SECONDS,
            )
    records_by_id: dict[str, AgentRunRecord] = {}
    list_finished = getattr(state_store, "list_finished_records", None)
    if callable(list_finished) and pending_task_ids:
        durable_records = await list_finished(
            task_ids=sorted(pending_task_ids),
            limit=100,
        )
        for record in durable_records:
            if recoverable_run_ids is None or record.run_id in recoverable_run_ids:
                records_by_id[record.run_id] = record
    try:
        queue_records = await queue.list_records()
    except Exception:  # noqa: BLE001 - Redis 断连不能阻断 PostgreSQL 恢复
        logger.warning("Completion Gate 读取 Redis 短期视图失败", exc_info=True)
        queue_records = []
    for record in queue_records:
        # PostgreSQL durable Run 始终优先；Redis 仅补充尚未落库的短期视图。
        records_by_id.setdefault(record.run_id, record)
    retried = 0
    for record in records_by_id.values():
        if (
            record.task_id is None
            or (recoverable_run_ids is not None and record.run_id not in recoverable_run_ids)
            or (pending_task_ids is not None and record.task_id not in pending_task_ids)
        ):
            continue
        lease = leases_by_task.get(record.task_id)
        if lease is None and record.task_id not in legacy_task_ids:
            continue
        if (
            task_execution_mode_from_config(record.task_config)
            is TaskExecutionMode.EXPLORATION_ONLY
        ):
            # exploration_only 没有 Completion Gate 状态机；即使 Redis/PG 仍保留
            # 历史短期投影，也不能在后台重试器中补建正式报告或质检工作项。
            continue
        effective_report_generator = _final_report_generator_for_record(
            record,
            default_generator=final_report_generator,
            web_generator=web_final_report_generator,
        )
        heartbeat: asyncio.Task[None] | None = None
        try:
            lease = await _renew_finalization_recovery_lease(service, lease)
            if lease is not None and callable(
                getattr(service, "renew_finalization_recovery_lease", None)
            ):
                heartbeat = asyncio.create_task(
                    _maintain_finalization_recovery_lease(service, lease)
                )
            current_snapshot = await service.get(task_id=record.task_id)
            if current_snapshot is None:
                continue
            if (
                _is_locked_app_run(record)
                and record.status in {AgentRunQueueStatus.COMPLETED, AgentRunQueueStatus.FAILED}
                and current_snapshot.state is TaskFinalizationState.SESSION_DRAINING
                and sealed_inspection_publisher is not None
            ):
                recover_handoff = getattr(
                    sealed_inspection_publisher, "recover_if_unpublished", None,
                )
                try:
                    # 超龄恢复可能先于 Publication 扫描；先用正式围栏补交接再判僵死。
                    if callable(recover_handoff):
                        await _await_with_recovery_heartbeat(
                            recover_handoff(
                                task_id=record.task_id, agent_run_id=record.run_id,
                                finalization_epoch=current_snapshot.finalization_epoch,
                            ),
                            heartbeat,
                        )
                except Exception:  # noqa: BLE001 - 损坏或缺失交接不豁免原超时终止
                    logger.warning(
                        "task_id=%s 超时前 App 正式快照交接未就绪，继续原收口检查",
                        record.task_id, exc_info=True,
                    )
            app_aggregate_recovery = getattr(
                getattr(service, "completion_gate_preparer", None),
                "prepare_app_aggregate_for_recovery", None,
            )
            if (
                _is_locked_app_run(record)
                and current_snapshot.state is TaskFinalizationState.SESSION_DRAINING
                and callable(app_aggregate_recovery)
            ):
                # 已排空快照的聚合义务先交接，避免无在途 Work 被误判为超龄僵死。
                try:
                    await _await_with_recovery_heartbeat(
                        app_aggregate_recovery(
                            task_id=record.task_id, agent_run_id=record.run_id,
                            finalization_epoch=current_snapshot.finalization_epoch,
                            recovery_lease_token=(lease.lease_token if lease else None),
                            recovery_generation=(lease.recovery_generation if lease else None),
                            recovery_lease_expires_at=(
                                _recovery_lease_expiry(lease) if lease else None
                            ),
                        ),
                        heartbeat,
                    )
                except Exception:  # noqa: BLE001 - 失败交接不能无限推迟技术性超时收口
                    logger.warning(
                        "task_id=%s 超时前 App 聚合交接失败，继续原收口检查",
                        record.task_id, exc_info=True,
                    )
            if (
                _is_locked_native_h5_v2_run_record(record)
                and current_snapshot.state is not TaskFinalizationState.SEALED
                and record.status in {AgentRunQueueStatus.COMPLETED, AgentRunQueueStatus.FAILED}
            ):
                # 先结算已终态 Run 的失联现场，再比较 Gate 是否无变化；否则
                # 遗留 draining 会话只会被反复延后，无法进入缺证正式收口。
                if settings is not None and settings.supplemental_evidence.enabled:
                    await _await_with_recovery_heartbeat(
                        _drain_h5_closed_snapshots(
                            record, settings=settings, worker_instance_id=None,
                        ), heartbeat,
                    )
                await _await_with_recovery_heartbeat(
                    PostgresH5ProbeTerminalSettlementRepository()
                    .settle_requests_after_session_close(
                        task_id=record.task_id, agent_run_id=record.run_id,
                    ),
                    heartbeat,
                )
            terminate_stalled = getattr(service, "terminate_stalled", None)
            app_gate_ready = False
            check_app_gate = getattr(service, "app_completion_ready_before_timeout", None)
            if (
                _is_locked_app_run(record) and callable(check_app_gate)
                and current_snapshot.state is TaskFinalizationState.SESSION_DRAINING
            ):
                try:
                    app_gate_ready = await _await_with_recovery_heartbeat(
                        check_app_gate(EvaluateCompletionGateCommand(
                            task_id=record.task_id, agent_run_id=record.run_id,
                            expected_finalization_epoch=current_snapshot.finalization_epoch,
                            expected_row_version=current_snapshot.row_version,
                            execution_status=_task_execution_status(record),
                            quality_outcome=_task_quality_outcome(record.final_report or {}),
                            recovery_lease_token=(lease.lease_token if lease else None),
                            recovery_generation=(lease.recovery_generation if lease else None),
                            recovery_lease_expires_at=(
                                _recovery_lease_expiry(lease) if lease else None
                            ),
                        )), heartbeat,
                    )
                except Exception:  # noqa: BLE001 - 门禁读取失败不豁免原超时终止
                    logger.warning(
                        "task_id=%s 超时前 App 完成门禁未就绪，继续原收口检查",
                        record.task_id, exc_info=True,
                    )
            if (
                callable(terminate_stalled)
                and not app_gate_ready
                and current_snapshot.state is TaskFinalizationState.SESSION_DRAINING
                and record.status in {AgentRunQueueStatus.COMPLETED, AgentRunQueueStatus.FAILED}
            ):
                terminal = await _await_with_recovery_heartbeat(
                    terminate_stalled(
                        TerminateTaskFinalizationCommand(
                            task_id=record.task_id,
                            agent_run_id=record.run_id,
                            expected_finalization_epoch=current_snapshot.finalization_epoch,
                            expected_row_version=current_snapshot.row_version,
                            execution_status=(
                                _task_execution_status(record)
                                if record.status is AgentRunQueueStatus.FAILED
                                else TaskExecutionStatus.NEEDS_REVIEW
                            ),
                            reason_code="FINALIZATION_STALLED_TIMEOUT",
                            recovery_lease_token=(lease.lease_token if lease else None),
                            recovery_generation=(lease.recovery_generation if lease else None),
                            recovery_lease_expires_at=(_recovery_lease_expiry(lease) if lease else None),
                        ),
                        minimum_terminal_age_seconds=finalization_stall_timeout_seconds,
                    ),
                    heartbeat,
                )
                if terminal is not None:
                    # 保留原 Main 终态及已有预览正文，不绑定虚假的正式报告。
                    record.worker_instance_id = None
                    record.worker_missing_since = None
                    await _save_queue_projection(queue, record)
                    retried += 1
                    continue
            if (
                _is_locked_web_v2_run_record(record)
                and web_inspection_reconciliation is not None
                and current_snapshot.state is not TaskFinalizationState.SEALED
            ):
                command = ReconcileWebInspectionCommand(
                    task_id=record.task_id,
                    agent_run_id=record.run_id,
                    finalization_epoch=current_snapshot.finalization_epoch,
                    recovery_lease_token=lease.lease_token,
                    recovery_generation=lease.recovery_generation,
                    recovery_lease_expires_at=_recovery_lease_expiry(lease),
                )
                web_result = await _await_with_recovery_heartbeat(
                    web_inspection_reconciliation.reconcile(command),
                    heartbeat,
                )
                if web_result.status == "waiting_for_run_terminal":
                    web_result = await _await_with_recovery_heartbeat(
                        web_inspection_reconciliation.reconcile(command),
                        heartbeat,
                    )
                lease = await _renew_finalization_recovery_lease(service, lease)
                if web_result.status != "gate_ready":
                    if lease is not None:
                        await _clear_finalization_retry_error(service, lease)
                    continue
                _apply_web_reconciliation_execution_status(record, web_result)
            if current_snapshot.state is TaskFinalizationState.SEALED:
                if not _is_final_report_payload(record.final_report):
                    if lease is not None:
                        await service.quarantine_missing_sealed_report(
                            task_id=record.task_id,
                            lease_token=lease.lease_token,
                            recovery_generation=lease.recovery_generation,
                            expected_status=lease.expected_status,
                            lease_expires_at=_recovery_lease_expiry(lease),
                        )
                    logger.error(
                            "task_id=%s 已封存但缺正式正文，已隔离等待人工复核",
                        record.task_id,
                    )
                    continue
                    await _await_with_recovery_heartbeat(
                        _generate_final_report(
                            record,
                            effective_report_generator,
                            finalized=True,
                            inspection_work_items_enabled=True,
                        ),
                        heartbeat,
                    )
                lease = await _renew_finalization_recovery_lease(service, lease)
                decision = await _await_with_recovery_heartbeat(
                    _advance_task_finalization(record, service, lease),
                    heartbeat,
                )
                _attach_completion_gate(record, decision)
                lease = await _renew_finalization_recovery_lease(service, lease)
                await _await_with_recovery_heartbeat(
                    _bind_final_report(record, service, lease),
                    heartbeat,
                )
                retried += 1
                _raise_if_recovery_heartbeat_failed(heartbeat)
                await _save_queue_projection(queue, record)
                if lease is not None:
                    await _clear_finalization_retry_error(service, lease)
                continue
            defer_unchanged = getattr(service, "defer_unchanged_completion_gate", None)
            if (
                current_snapshot.state is TaskFinalizationState.SESSION_DRAINING
                and callable(defer_unchanged)
                and await _await_with_recovery_heartbeat(
                    defer_unchanged(
                        EvaluateCompletionGateCommand(
                            task_id=record.task_id,
                            agent_run_id=record.run_id,
                            expected_finalization_epoch=current_snapshot.finalization_epoch,
                            expected_row_version=current_snapshot.row_version,
                            execution_status=_task_execution_status(record),
                            quality_outcome=_task_quality_outcome(record.final_report or {}),
                            recovery_lease_token=(lease.lease_token if lease else None),
                            recovery_generation=(lease.recovery_generation if lease else None),
                            recovery_lease_expires_at=(
                                _recovery_lease_expiry(lease) if lease else None
                            ),
                        )
                    ),
                    heartbeat,
                )
            ):
                # 未变的阻塞事实不再次生成报告、写入 Run 或创建 Gate 快照。
                if lease is not None:
                    await _clear_finalization_retry_error(service, lease)
                continue
            preview_report = (
                deepcopy(record.final_report) if isinstance(record.final_report, dict) else None
            )
            await _await_with_recovery_heartbeat(
                _generate_final_report(
                record,
                    effective_report_generator,
                finalized=True,
                inspection_work_items_enabled=True,
                ),
                heartbeat,
            )
            _prepare_report_binding_metadata(record)
            lease = await _renew_finalization_recovery_lease(service, lease)
            decision = await _await_with_recovery_heartbeat(
                _advance_task_finalization(record, service, lease),
                heartbeat,
            )
            lease = await _renew_finalization_recovery_lease(service, lease)
            retried += 1
            if decision.gate_passed:
                _attach_completion_gate(record, decision)
                _raise_if_recovery_heartbeat_failed(heartbeat)
                await _save_queue_projection(queue, record)
            else:
                if preview_report is not None:
                    record.final_report = preview_report
                else:
                    record.final_report = None
                _attach_completion_gate(record, decision)
                _raise_if_recovery_heartbeat_failed(heartbeat)
                await _record_run_finished_for_recovery(
                    state_store,
                    record,
                    reason="Completion Gate 重试写入预览报告",
                )
                await _save_queue_projection(queue, record)
            if lease is not None:
                await _clear_finalization_retry_error(
                    service,
                    lease,
                    expected_status=(
                        _recovery_terminal_task_status(record)
                        if decision.gate_passed
                        else lease.expected_status
                    ),
                )
        except Exception as exc:
            record_retry_error = getattr(service, "record_finalization_retry_error", None)
            if callable(record_retry_error) and record.task_id and lease is not None:
                try:
                    await record_retry_error(
                        task_id=record.task_id,
                        error_message=f"{type(exc).__name__}: {exc}",
                        lease_token=lease.lease_token,
                        recovery_generation=lease.recovery_generation,
                        expected_status=lease.expected_status,
                        lease_expires_at=_recovery_lease_expiry(lease),
                    )
                except Exception:  # noqa: BLE001 - 诊断写入失败不能覆盖原始收口异常
                    logger.warning(
                        "task_id=%s Completion Gate 重试错误诊断写入失败",
                        record.task_id,
                        exc_info=True,
                    )
            if not _is_recovery_fence_failure(exc, heartbeat):
                try:
                    latest = await service.get(task_id=record.task_id) if record.task_id else None
                    if latest is None or latest.state is not TaskFinalizationState.SEALED:
                        await _record_run_finished_for_recovery(
                            state_store,
                            record,
                            reason="Completion Gate 异常恢复写回",
                        )
                    await _save_queue_projection(queue, record)
                except Exception:  # noqa: BLE001 - 保留首次异常，下一轮继续扫描数据库任务
                    logger.exception(
                        "task_id=%s Completion Gate 失败后的报告快照持久化也失败。",
                        record.task_id,
                    )
            logger.warning(
                "task_id=%s Completion Gate 重试失败，将保留可恢复收口状态",
                record.task_id,
                exc_info=True,
            )
        finally:
            if heartbeat is not None:
                heartbeat.cancel()
                try:
                    await heartbeat
                except asyncio.CancelledError:
                    pass
                except Exception:  # noqa: BLE001 - 租约丢失已由 fenced 写保护
                    logger.warning(
                        "task_id=%s 收口恢复续租已停止",
                        record.task_id,
                        exc_info=True,
                    )
    return retried


async def _renew_finalization_recovery_lease(
    service: TaskFinalizationService,
    lease: Any | None,
) -> Any | None:
    """在潜在长操作边界续租；legacy H5/App 不进入 Web fenced 通道。"""

    if lease is None:
        return None
    renew = getattr(service, "renew_finalization_recovery_lease", None)
    if not callable(renew):
        return lease
    return await renew(
        task_id=lease.task_id,
        lease_token=lease.lease_token,
        recovery_generation=lease.recovery_generation,
        lease_seconds=COMPLETION_GATE_RETRY_DEFER_SECONDS,
    )


async def _await_with_recovery_heartbeat[T](
    operation: Awaitable[T],
    heartbeat: asyncio.Task[None] | None,
) -> T:
    """让续租失败抢占长操作；旧 holder 不得继续进入下一副作用。"""

    if heartbeat is None:
        return await operation
    if heartbeat.done():
        close = getattr(operation, "close", None)
        if callable(close):
            close()
        _raise_if_recovery_heartbeat_failed(heartbeat)
    operation_task = asyncio.ensure_future(operation)
    done, _pending = await asyncio.wait(
        {operation_task, heartbeat},
        return_when=asyncio.FIRST_COMPLETED,
    )
    if heartbeat in done:
        operation_task.cancel()
        try:
            await operation_task
        except asyncio.CancelledError:
            pass
        _raise_if_recovery_heartbeat_failed(heartbeat)
    return await operation_task


def _raise_if_recovery_heartbeat_failed(
    heartbeat: asyncio.Task[None] | None,
) -> None:
    if heartbeat is None or not heartbeat.done():
        return
    if heartbeat.cancelled():
        raise FinalizationRecoveryFenceError("收口恢复续租已取消")
    failure = heartbeat.exception()
    if failure is not None:
        raise FinalizationRecoveryFenceError("收口恢复 heartbeat 已丢失") from failure
    raise FinalizationRecoveryFenceError("收口恢复 heartbeat 意外停止")


def _is_recovery_fence_failure(
    error: BaseException,
    heartbeat: asyncio.Task[None] | None,
) -> bool:
    if isinstance(error, FinalizationRecoveryFenceError):
        return True
    if isinstance(error, TaskFinalizationPersistenceError) and "租约" in str(error):
        return True
    return heartbeat is not None and heartbeat.done()


async def _maintain_finalization_recovery_lease(
    service: TaskFinalizationService,
    lease: Any,
) -> None:
    """长时间生成/对账时持续续租，确保同 generation 不会中途失效。"""

    interval = max(1.0, COMPLETION_GATE_RETRY_DEFER_SECONDS / 3)
    while True:
        await asyncio.sleep(interval)
        await _renew_finalization_recovery_lease(service, lease)


def _recovery_lease_expiry(lease: Any) -> datetime:
    expires_at = getattr(lease, "lease_expires_at", None)
    if isinstance(expires_at, datetime):
        return expires_at
    return datetime.now(UTC) + timedelta(seconds=COMPLETION_GATE_RETRY_DEFER_SECONDS)


async def _clear_finalization_retry_error(
    service: TaskFinalizationService,
    lease: Any,
    *,
    expected_status: str | None = None,
) -> None:
    clear_error = getattr(service, "record_finalization_retry_success", None)
    if callable(clear_error):
        await clear_error(
            task_id=lease.task_id,
            lease_token=lease.lease_token,
            recovery_generation=lease.recovery_generation,
            expected_status=expected_status or lease.expected_status,
            lease_expires_at=_recovery_lease_expiry(lease),
        )


def _recovery_terminal_task_status(record: AgentRunRecord) -> str:
    """把已通过 Gate 的执行终态映射为 Task 状态，供租约 CAS 校验。"""

    execution_status = _task_execution_status(record)
    if execution_status in {
        TaskExecutionStatus.COMPLETED,
        TaskExecutionStatus.COMPLETED_WITH_GAPS,
    }:
        return "done"
    if execution_status is TaskExecutionStatus.NEEDS_REVIEW:
        return "review"
    if execution_status is TaskExecutionStatus.CANCELED:
        return "canceled"
    return "failed"


async def _completion_gate_retry_loop(
    queue: RedisAgentRunQueue,
    state_store: AgentRunStateStore,
    service: TaskFinalizationService,
    stop_event: asyncio.Event,
    *,
    final_report_generator: FinalReportGenerator | None = None,
    web_final_report_generator: FinalReportGenerator | None = None,
    web_inspection_reconciliation: Any | None = None,
    sealed_inspection_publisher: AppSealedInspectionPublisherPort | None = None,
    interval_seconds: float = COMPLETION_GATE_RETRY_INTERVAL_SECONDS,
    finalization_stall_timeout_seconds: float = 600.0,
    settings: Settings | None = None,
) -> None:
    """后台重试器：不依赖新的 Work Item 或人工操作触发收口。"""
    while not stop_event.is_set():
        try:
            await _retry_pending_task_finalizations(
                queue,
                state_store,
                service,
                final_report_generator=final_report_generator,
                web_final_report_generator=web_final_report_generator,
                web_inspection_reconciliation=web_inspection_reconciliation,
                finalization_stall_timeout_seconds=finalization_stall_timeout_seconds,
                sealed_inspection_publisher=sealed_inspection_publisher,
                settings=settings,
            )
        except Exception:
            logger.warning("Completion Gate 重试扫描失败", exc_info=True)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval_seconds)
        except TimeoutError:
            continue


def _new_worker_instance_id() -> str:
    """生成进程启动级唯一实例 ID，容器内 hostname/PID 相同也不会误认旧进程。"""
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex}"


async def _v2_reliable_delivery_loop(
    runtime: ReliableDeliveryRuntime,
    stop_event: asyncio.Event,
    *,
    relay_interval_seconds: float = V2_DELIVERY_RELAY_INTERVAL_SECONDS,
    sweep_interval_seconds: float = V2_DELIVERY_SWEEP_INTERVAL_SECONDS,
) -> None:
    """在现有 Worker 内驱动 V2 Outbox；失败只延迟唤醒，不改写数据库工作事实。"""
    if relay_interval_seconds <= 0 or sweep_interval_seconds <= 0:
        raise ValueError("Relay 和 Sweeper 周期必须大于 0")
    loop = asyncio.get_running_loop()
    next_sweep_at = 0.0
    while not stop_event.is_set():
        try:
            now = loop.time()
            if now >= next_sweep_at:
                await runtime.sweep_once()
                next_sweep_at = now + sweep_interval_seconds
            else:
                await runtime.relay_once()
        except Exception:  # noqa: BLE001 - Redis/数据库短断不能拖垮既有 Worker
            logger.warning("V2 可靠派发周期执行失败，将按数据库状态重试。", exc_info=True)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=relay_interval_seconds)
        except TimeoutError:
            continue


def _build_task_submission_consumer(
    redis_client: Any,
    queue: RedisAgentRunQueue,
    web_queue: RedisAgentRunQueue,
    settings: Settings,
    *,
    worker_instance_id: str,
) -> tuple[RedisStreamWakeupConsumer, RedisWakeupDispatcher]:
    """把 PostgreSQL 已提交的初始 Run 幂等投影到既有 Redis List。"""

    inspection_settings = settings.inspection_work_items
    stream_consumer = RedisStreamWakeupConsumer(
        redis_client,
        group_name=V2_TASK_SUBMISSION_CONSUMER_GROUP,
        consumer_name=f"{worker_instance_id}:task-submission",
        stream_names=(V2_TASK_SUBMISSION_STREAM_NAME,),
    )
    dispatcher = RedisWakeupDispatcher(
        consumer=stream_consumer,
        handler=InboxGuardedWakeupHandler(
            repository=PostgresEventInboxRepository(),
            handler=TaskSubmissionWakeupHandler(
                service=TaskSubmissionProjectionService(
                    repository=PostgresTaskSubmissionDeliveryRepository(),
                    queue=queue,
                    web_queue=web_queue,
                )
            ),
            consumer_name=V2_TASK_SUBMISSION_CONSUMER_GROUP,
            lease_owner=f"{worker_instance_id}:task-submission-inbox",
            lease_seconds=inspection_settings.lease_seconds,
            retry_delay_seconds=max(1, int(inspection_settings.retry_base_seconds)),
            max_attempts=inspection_settings.max_attempts,
        ),
    )
    return stream_consumer, dispatcher


def _build_v2_inspection_consumer(
    redis_client: Any,
    settings: Settings,
    *,
    worker_instance_id: str,
    supplemental_evidence_policy: SupplementalEvidencePolicy | None = None,
    supplemental_evidence_target_platforms: frozenset[str] | None = None,
) -> tuple[
    RedisStreamWakeupConsumer,
    RedisWakeupDispatcher,
    InspectionJudgeDispatcher,
]:
    """组合 inspection 唤醒消费的生产实现。"""
    inspection_settings = settings.inspection_work_items
    service = InspectionWorkItemService(repository=PostgresInspectionWorkItemRepository())
    stream_consumer = RedisStreamWakeupConsumer(
        redis_client,
        group_name=V2_INSPECTION_CONSUMER_GROUP,
        consumer_name=f"{worker_instance_id}:inspection",
        stream_names=(V2_INSPECTION_STREAM_NAME,),
    )
    live_publishers = [build_default_app_live_inspection_publish_service()]
    if (
        getattr(settings.workflow, "h5_v2_mode", "off") == "on"
        or getattr(settings.workflow, "web_v2_mode", "off") == "on"
    ):
        live_publishers.append(build_default_h5_live_inspection_publish_service())
    if getattr(settings.workflow, "web_v2_mode", "off") == "on":
        live_publishers.append(
            build_default_web_live_inspection_publish_service(settings=settings)
        )
    live_publisher = CompositeLiveInspectionPublisher(*live_publishers)
    effective_supplemental_evidence_policy = (
        supplemental_evidence_policy
        if supplemental_evidence_policy is not None
        else build_current_supplemental_evidence_policy(
            enabled=settings.supplemental_evidence.enabled
        )
    )
    work_item_consumer = InspectionWorkItemConsumer(
        service=service,
        lease_owner=f"{worker_instance_id}:inspection-judge",
        lease_seconds=inspection_settings.lease_seconds,
        retry_backoff_seconds=inspection_settings.retry_base_seconds,
        supplemental_evidence_policy=effective_supplemental_evidence_policy,
        h5_result_writer=(
            _h5_request_admitting_writer(effective_supplemental_evidence_policy)
            if settings.workflow.h5_v2_mode == "on"
            else None
        ),
        h5_closed_session_settlement=(
            PostgresH5ProbeTerminalSettlementRepository(
                allow_new_session_recovery=settings.supplemental_evidence.enabled,
            )
            if settings.workflow.h5_v2_mode == "on" else None
        ),
        supplemental_evidence_target_platforms=supplemental_evidence_target_platforms,
        reuse_planner=build_current_rule_reuse_planner(
            enabled=rule_result_reuse_enabled(settings)
        ),
        judge=(
            build_h5_targeted_recapture_rule_judge_agent()
            if settings.workflow.h5_v2_mode == "on"
            and effective_supplemental_evidence_policy.enabled
            else None
        ),
    )
    judge_dispatcher = InspectionJudgeDispatcher(
        service=service,
        consumer=work_item_consumer,
        live_publisher=live_publisher,
        batch_limit=getattr(inspection_settings, "dispatch_batch_limit", 1),
        publication_batch_limit=getattr(
            inspection_settings,
            "publication_batch_limit",
            1,
        ),
        publication_timeout_seconds=getattr(
            inspection_settings,
            "publication_timeout_seconds",
            30.0,
        ),
        queue_capacity=getattr(inspection_settings, "dispatch_queue_capacity", 128),
        retry_delay_seconds=inspection_settings.retry_base_seconds,
    )
    dispatcher = RedisWakeupDispatcher(
        consumer=stream_consumer,
        handler=InboxGuardedWakeupHandler(
            repository=PostgresEventInboxRepository(),
            handler=InspectionWakeupHandler(
                service=service,
                consumer=judge_dispatcher,
            ),
            consumer_name=V2_INSPECTION_CONSUMER_GROUP,
            lease_owner=f"{worker_instance_id}:inspection-inbox",
            lease_seconds=inspection_settings.lease_seconds,
            retry_delay_seconds=max(1, int(inspection_settings.retry_base_seconds)),
            max_attempts=inspection_settings.max_attempts,
        ),
    )
    return stream_consumer, dispatcher, judge_dispatcher


def _build_h5_publication_consumer(
    redis_client: Any,
    settings: Settings,
    *,
    worker_instance_id: str,
) -> tuple[RedisStreamWakeupConsumer, RedisWakeupDispatcher]:
    """组合 H5 Publication 专用 Stream 与数据库权威处理器。"""

    inspection_settings = settings.inspection_work_items
    repository = PostgresH5InspectionPublicationRepository(
        max_attempts=inspection_settings.max_attempts,
        retry_delay_seconds=max(1, int(inspection_settings.retry_base_seconds)),
    )
    stream_consumer = RedisStreamWakeupConsumer(
        redis_client,
        group_name=V2_H5_PUBLICATION_CONSUMER_GROUP,
        consumer_name=f"{worker_instance_id}:h5-publication",
        stream_names=(V2_H5_PUBLICATION_STREAM_NAME,),
    )
    dispatcher = RedisWakeupDispatcher(
        consumer=stream_consumer,
        handler=InboxGuardedWakeupHandler(
            repository=PostgresEventInboxRepository(),
            handler=H5InspectionPublicationWakeupHandler(
                repository=repository,
                publisher=build_default_app_capture_inspection_publisher(),
                lease_owner=f"{worker_instance_id}:h5-publication-wakeup",
                lease_seconds=inspection_settings.lease_seconds,
            ),
            consumer_name=V2_H5_PUBLICATION_CONSUMER_GROUP,
            lease_owner=f"{worker_instance_id}:h5-publication-inbox",
            lease_seconds=inspection_settings.lease_seconds,
            retry_delay_seconds=max(1, int(inspection_settings.retry_base_seconds)),
            max_attempts=inspection_settings.max_attempts,
        ),
    )
    return stream_consumer, dispatcher


def _build_app_publication_consumer(
    redis_client: Any,
    settings: Settings,
    *,
    worker_instance_id: str,
) -> tuple[RedisStreamWakeupConsumer, RedisWakeupDispatcher]:
    """组合 App sealed Index Publication 专用 Stream 与数据库处理器。"""

    inspection_settings = settings.inspection_work_items
    repository = PostgresAppInspectionPublicationRepository(
        max_attempts=inspection_settings.max_attempts,
        retry_delay_seconds=max(1, int(inspection_settings.retry_base_seconds)),
    )
    stream_consumer = RedisStreamWakeupConsumer(
        redis_client,
        group_name=V2_APP_PUBLICATION_CONSUMER_GROUP,
        consumer_name=f"{worker_instance_id}:app-publication",
        stream_names=(V2_APP_PUBLICATION_STREAM_NAME,),
    )
    dispatcher = RedisWakeupDispatcher(
        consumer=stream_consumer,
        handler=InboxGuardedWakeupHandler(
            repository=PostgresEventInboxRepository(),
            handler=AppInspectionPublicationWakeupHandler(
                repository=repository,
                publisher=build_default_app_capture_inspection_publisher(),
                lease_owner=f"{worker_instance_id}:app-publication-wakeup",
                lease_seconds=inspection_settings.lease_seconds,
            ),
            consumer_name=V2_APP_PUBLICATION_CONSUMER_GROUP,
            lease_owner=f"{worker_instance_id}:app-publication-inbox",
            lease_seconds=inspection_settings.lease_seconds,
            retry_delay_seconds=max(1, int(inspection_settings.retry_base_seconds)),
            max_attempts=inspection_settings.max_attempts,
        ),
    )
    return stream_consumer, dispatcher


def _build_aggregate_inspection_consumer(
    redis_client: Any,
    settings: Settings,
    *,
    worker_instance_id: str,
) -> tuple[RedisStreamWakeupConsumer, RedisWakeupDispatcher]:
    """组合 EvidenceInputSet 聚合质检专用 Stream 与数据库执行器。"""

    inspection_settings = settings.inspection_work_items
    stream_consumer = RedisStreamWakeupConsumer(
        redis_client,
        group_name=V2_AGGREGATE_INSPECTION_CONSUMER_GROUP,
        consumer_name=f"{worker_instance_id}:aggregate-inspection",
        stream_names=(V2_AGGREGATE_INSPECTION_STREAM_NAME,),
    )
    dispatcher = RedisWakeupDispatcher(
        consumer=stream_consumer,
        handler=InboxGuardedWakeupHandler(
            repository=PostgresEventInboxRepository(),
            handler=AggregateInspectionWakeupHandler(
                executor=AggregateInspectionExecutor(
                    repository=PostgresAggregateInspectionExecutionRepository(),
                    lease_owner=f"{worker_instance_id}:aggregate-inspection-wakeup",
                    lease_seconds=inspection_settings.lease_seconds,
                    retry_delay_seconds=max(
                        1,
                        int(inspection_settings.retry_base_seconds),
                    ),
                )
            ),
            consumer_name=V2_AGGREGATE_INSPECTION_CONSUMER_GROUP,
            lease_owner=f"{worker_instance_id}:aggregate-inspection-inbox",
            lease_seconds=inspection_settings.lease_seconds,
            retry_delay_seconds=max(1, int(inspection_settings.retry_base_seconds)),
            max_attempts=inspection_settings.max_attempts,
        ),
    )
    return stream_consumer, dispatcher


async def _v2_inspection_consumer_loop(
    stream_consumer: Any,
    dispatcher: Any,
    stop_event: asyncio.Event,
    *,
    receive_count: int = 10,
    receive_block_milliseconds: int = V2_INSPECTION_RECEIVE_BLOCK_MILLISECONDS,
    stale_claim_interval_seconds: float = V2_INSPECTION_STALE_CLAIM_INTERVAL_SECONDS,
    stale_claim_min_idle_milliseconds: int = V2_INSPECTION_STALE_CLAIM_MIN_IDLE_MILLISECONDS,
    stream_name: str = V2_INSPECTION_STREAM_NAME,
    log_name: str = "质检",
) -> None:
    """消费 inspection 唤醒并按 PostgreSQL 事实驱动质检；数据库确认后才 ACK。"""
    if receive_count <= 0 or receive_block_milliseconds < 0:
        raise ValueError("receive_count 必须大于 0，receive_block_milliseconds 不能小于 0")
    if stale_claim_interval_seconds <= 0:
        raise ValueError("stale_claim_interval_seconds 必须大于 0")
    loop = asyncio.get_running_loop()
    next_stale_claim_at = 0.0
    while not stop_event.is_set():
        try:
            now = loop.time()
            if now >= next_stale_claim_at:
                # ensure_groups 幂等；Redis 清空重建后也能自动恢复 Consumer Group。
                await stream_consumer.ensure_groups()
                stale_batch = await stream_consumer.claim_stale(
                    stream_name=stream_name,
                    min_idle_milliseconds=stale_claim_min_idle_milliseconds,
                )
                for wakeup in stale_batch.wakeups:
                    await dispatcher.dispatch(wakeup)
                pending_count_reader = getattr(stream_consumer, "pending_count", None)
                if callable(pending_count_reader):
                    pending_count = await pending_count_reader(stream_name=stream_name)
                    if pending_count:
                        logger.info(
                            "redis_stream_backlog stream_name=%s log_name=%s "
                            "pending_count=%d",
                            stream_name,
                            log_name,
                            pending_count,
                        )
                next_stale_claim_at = now + stale_claim_interval_seconds
            await dispatcher.receive_and_dispatch(
                count=receive_count,
                block_milliseconds=receive_block_milliseconds,
            )
        except WebActiveSessionWorkPendingError:
            logger.info(
                "V2 %s active-session Work 尚未就绪，等待 capture.committed 补发。",
                log_name,
            )
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=WORKER_QUEUE_RETRY_SECONDS)
            except TimeoutError:
                continue
        except Exception:  # noqa: BLE001 - Redis/数据库短断不能拖垮既有 Worker
            logger.warning(
                "V2 %s唤醒消费周期失败，将按数据库状态重试。",
                log_name,
                exc_info=True,
            )
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=WORKER_QUEUE_RETRY_SECONDS)
            except TimeoutError:
                continue


async def _h5_publication_reconciliation_loop(
    repository: PostgresH5InspectionPublicationRepository,
    stop_event: asyncio.Event,
    *,
    interval_seconds: float = 5.0,
) -> None:
    """周期补发 Publication 唤醒并回收过期租约。"""

    while not stop_event.is_set():
        try:
            await repository.reconcile()
        except Exception:  # noqa: BLE001 - 对账短故障不能拖垮 Worker
            logger.warning("H5 Publication 对账周期失败。", exc_info=True)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval_seconds)
        except TimeoutError:
            continue


async def _app_publication_reconciliation_loop(
    repository: PostgresAppInspectionPublicationRepository,
    stop_event: asyncio.Event,
    *,
    sealed_inspection_publisher: AppSealedInspectionPublisherPort | None = None,
    interval_seconds: float = 5.0,
) -> None:
    """周期恢复封存证据交接、App Publication 过期租约与到期重试。"""

    handoff = (
        AppInspectionPublicationReconciler(
            repository=repository, publisher=sealed_inspection_publisher
        )
        if sealed_inspection_publisher is not None
        else None
    )

    while not stop_event.is_set():
        if handoff is not None:
            try:
                await handoff.reconcile()
            except Exception:  # noqa: BLE001 - 交接扫描失败仍继续恢复已有 Publication
                logger.warning("App sealed Index 质检交接对账周期失败。", exc_info=True)
        try:
            await repository.reconcile()
        except Exception:  # noqa: BLE001 - 对账短故障不能拖垮 Worker
            logger.warning("App Publication 对账周期失败。", exc_info=True)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval_seconds)
        except TimeoutError:
            continue


async def _aggregate_inspection_reconciliation_loop(
    publication_repository: PostgresAggregateInspectionPublicationRepository,
    execution_repository: PostgresAggregateInspectionExecutionRepository,
    settings: Settings,
    stop_event: asyncio.Event,
    *,
    interval_seconds: float = 5.0,
) -> None:
    """恢复 session_draining 漏发布、过期租约和当前版本缺失唤醒。"""

    publisher = AggregateInspectionPublicationService(
        repository=publication_repository,
        max_attempts=settings.inspection_work_items.max_attempts,
    )
    while not stop_event.is_set():
        try:
            for (
                task_id,
                agent_run_id,
                finalization_epoch,
            ) in await publication_repository.list_session_draining():
                try:
                    await publisher.prepare(
                        task_id=task_id,
                        agent_run_id=agent_run_id,
                        finalization_epoch=finalization_epoch,
                    )
                except Exception:  # noqa: BLE001 - 单任务异常不得阻断其他恢复
                    logger.warning(
                        "task_id=%s 聚合候选发布对账失败。",
                        task_id,
                        exc_info=True,
                    )
            await execution_repository.reconcile()
        except Exception:  # noqa: BLE001 - 对账短故障不能拖垮 Worker
            logger.warning("聚合质检对账周期失败。", exc_info=True)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval_seconds)
        except TimeoutError:
            continue


async def _task_submission_reconciliation_loop(
    service: TaskSubmissionProjectionService,
    stop_event: asyncio.Event,
    *,
    interval_seconds: float = 10.0,
) -> None:
    """恢复 Redis/Stream 丢失的 pending 初始 Run，不创建第二个 Task/Run。"""

    while not stop_event.is_set():
        try:
            await service.reconcile(
                minimum_age_seconds=max(1, int(interval_seconds)),
                limit=100,
            )
        except Exception:  # noqa: BLE001 - 短期投影故障不得拖垮 Worker
            logger.warning("任务提交 Redis 投影对账失败。", exc_info=True)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval_seconds)
        except TimeoutError:
            continue


class _WebActiveSessionInspectionDrainer:
    """按 Web task/run/epoch 排空 active-session Work 与补证请求。"""

    def __init__(
        self,
        *,
        publisher: Any,
        consumer: InspectionWorkItemConsumer,
        max_drain_rounds: int = 10,
        closure_settlement: Any | None = None,
    ) -> None:
        if (
            isinstance(max_drain_rounds, bool)
            or not isinstance(max_drain_rounds, int)
            or max_drain_rounds <= 0
        ):
            raise ValueError("max_drain_rounds 必须为正整数")
        self._publisher = publisher
        self._consumer = consumer
        self._max_drain_rounds = max_drain_rounds
        self._closure_settlement = closure_settlement

    async def drain_before_close(
        self,
        *,
        task_id: str,
        agent_run_id: str,
        finalization_epoch: int,
        recovery_lease_token: str | None = None,
        recovery_generation: int | None = None,
        recovery_lease_expires_at: datetime | None = None,
    ) -> None:
        for _round in range(self._max_drain_rounds):
            if recovery_lease_token is not None:
                pool = await get_postgres_pool(os.getenv("ALIOTH_DATABASE_URL"))
                async with pool.acquire() as connection, connection.transaction():
                    await assert_finalization_recovery_fence(
                        connection,
                        task_id=task_id,
                        recovery_lease_token=recovery_lease_token,
                        recovery_generation=recovery_generation,
                        recovery_lease_expires_at=recovery_lease_expires_at,
                    )
            await self._publisher.publish_pending(
                task_id=task_id,
                agent_run_id=agent_run_id,
            )
            await self._consumer.drain_available(
                task_id=task_id,
                consumer_agent_run_id=agent_run_id,
                session_dependency="active_session",
                allow_terminal_web_owner_recovery=True,
            )
            pool = await get_postgres_pool(os.getenv("ALIOTH_DATABASE_URL"))
            async with pool.acquire() as connection:
                remaining = await connection.fetchval(
                    """
                    SELECT
                        (
                            SELECT count(*)
                            FROM qc_inspection_work_item AS work_item
                            WHERE work_item.task_id = $1::uuid
                              AND work_item.producer_agent_run_id = $2::uuid
                              AND work_item.finalization_epoch = $3
                              AND work_item.session_dependency = 'active_session'
                              AND work_item.control_mode = 'enforced'
                              AND work_item.status NOT IN (
                                  'succeeded', 'partial', 'failed',
                                  'cancelled', 'superseded'
                              )
                        ) + (
                            SELECT count(*)
                            FROM qc_evidence_request AS request
                            INNER JOIN qc_capture AS capture
                                ON capture.id = request.target_capture_id
                               AND capture.agent_run_id = $2::uuid
                            WHERE request.task_id = $1::uuid
                              AND request.session_dependency = 'active_session'
                              AND request.status IN ('pending', 'routed', 'running')
                        )
                    """,
                    task_id,
                    agent_run_id,
                    finalization_epoch,
                )
            if int(remaining or 0) == 0:
                return
        # 轮次用尽仍未排空：抛错会让 session_closer.close() 执行不到，浏览器不释放、
        # 收口无限重试。能结算就按证据不足结算并留放弃原因，继续往下关会话。
        if self._closure_settlement is None:
            raise WebActiveSessionWorkPendingError(
                "Web Session 关闭前 active-session 正式工作尚未排空"
            )
        settlement = await self._closure_settlement.settle_stalled_work_for_closure(
            task_id=task_id,
            active_session_agent_run_id=agent_run_id,
        )
        logger.warning(
            "task_id=%s run_id=%s Web 关闭前活会话规则未排空（剩余 %s），"
            "已按证据不足结算 %s 个工作项、%s 条规则后继续关闭会话。",
            task_id,
            agent_run_id,
            int(remaining or 0),
            settlement.settled_work_item_count,
            settlement.settled_rule_count,
        )


def _build_default_web_formal_inspection_dependencies(
    *,
    settings: Settings,
    worker_instance_id: str,
    task_finalization_service: TaskFinalizationService,
    publication_wakeup: ReliableDeliveryWebPublicationWakeup,
    session_closer: object,
) -> WebInspectionReconciliation:
    """组合 Web 最终交接、活会话排空与 Task 5 对账器。"""

    final_handoff = build_default_web_page_inspection_notification(
        settings=settings,
        wakeup=publication_wakeup,
    )
    inspection_settings = settings.inspection_work_items
    web_inspection_work_service = InspectionWorkItemService(
        repository=PostgresInspectionWorkItemRepository()
    )
    active_session_drainer = _WebActiveSessionInspectionDrainer(
        publisher=build_default_h5_live_inspection_publish_service(),
        consumer=InspectionWorkItemConsumer(
            service=web_inspection_work_service,
            lease_owner=f"{worker_instance_id}:web-active-inspection",
            lease_seconds=inspection_settings.lease_seconds,
            retry_backoff_seconds=inspection_settings.retry_base_seconds,
            supplemental_evidence_policy=build_current_supplemental_evidence_policy(
                enabled=settings.supplemental_evidence.enabled
            ),
            reuse_planner=build_current_rule_reuse_planner(
                enabled=rule_result_reuse_enabled(settings)
            ),
        ),
        # 轮次用尽时按证据不足结算再关会话，否则浏览器释放执行不到。
        closure_settlement=web_inspection_work_service,
    )
    snapshot_repository = PostgresWebCoverageSnapshotRepository()
    return WebInspectionReconciliation(
        state_reader=snapshot_repository,
        manifest_finalizer=None,
        publication_publisher=None,
        active_session_drainer=active_session_drainer,
        session_closer=session_closer,
        finalization=task_finalization_service,
        snapshot_repository=snapshot_repository,
        gap_rule_reader=snapshot_repository,
        rule_execution_service=RuleExecutionService(
            repository=PostgresTaskRepository()
        ),
        final_handoff=final_handoff,
        terminal_ledger_finalizer=PostgresWebFormalTerminalLedgerRepository(),
    )


async def _run_web_only_worker(settings: Settings) -> None:
    """启动只依赖 Web 队列与 Web Runtime 的隔离 Worker 进程。"""

    redis_client = build_redis_client(settings.queue)
    queue = RedisAgentRunQueue(
        redis_client,
        ttl_seconds=settings.queue.run_record_ttl_seconds,
        queue_key=settings.queue.agent_run_queue_key,
    )
    web_queue = RedisAgentRunQueue(
        redis_client,
        ttl_seconds=settings.queue.run_record_ttl_seconds,
        queue_key=settings.queue.web_agent_run_queue_key,
    )
    state_store = build_agent_run_state_store()
    validate_task_submission_configuration(settings, state_store=state_store)
    validate_web_worker_configuration(settings)
    worker_instance_id = _new_worker_instance_id()
    delivery_runtime = build_default_reliable_delivery_runtime(
        redis_client=redis_client,
        lease_owner=f"{worker_instance_id}:v2-outbox-relay",
    )
    web_workflow_handler = build_web_workflow_handler(
        settings,
        worker_instance_id,
        publication_wakeup=ReliableDeliveryWebPublicationWakeup(
            delivery_runtime.relay_once
        ),
    )
    web_formal_session_closer = web_workflow_handler.formal_session_closer
    task_finalization_service = None
    rule_engine_configuration_guard = None
    final_report_generator = ReportGenerationService(
        query_service=TaskQueryService(repository=PostgresTaskRepository())
    )
    web_final_report_generator = AuditedWebFinalReportGenerator(
        delegate=final_report_generator,
        ledger=PostgresWebReportGenerationAttemptRepository(),
    )
    validate_completion_gate_configuration(
        settings,
        state_store=state_store,
        task_finalization_service=task_finalization_service,
    )
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            pass
    await web_queue.record_worker_heartbeat(
        worker_instance_id,
        roles=settings.worker.roles,
    )
    await _reconcile_orphaned_runs(
        web_queue,
        state_store,
        web_formal_session_closer=web_formal_session_closer,
    )
    tasks = _start_web_worker_loops(
        concurrency=settings.worker.web_concurrency,
        queue=web_queue,
        settings=settings,
        stop_event=stop_event,
        state_store=state_store,
        worker_instance_id=worker_instance_id,
        web_workflow_handler=web_workflow_handler,
        task_finalization_service=task_finalization_service,
        final_report_generator=final_report_generator,
        web_final_report_generator=web_final_report_generator,
        rule_engine_configuration_guard=rule_engine_configuration_guard,
        run_capacity=asyncio.BoundedSemaphore(settings.worker.max_active_runs),
    )
    heartbeat_task = asyncio.create_task(
        _worker_heartbeat_loop(web_queue, stop_event, instance_id=worker_instance_id)
    )
    reconciliation_task = asyncio.create_task(
        _orphan_reconciliation_loop(
            web_queue,
            state_store,
            stop_event,
            web_formal_session_closer=web_formal_session_closer,
        )
    )
    task_submission_stream_consumer, task_submission_dispatcher = (
        _build_task_submission_consumer(
            redis_client,
            queue,
            web_queue,
            settings,
            worker_instance_id=worker_instance_id,
        )
    )
    task_submission_consumer_task = asyncio.create_task(
        _v2_inspection_consumer_loop(
            task_submission_stream_consumer,
            task_submission_dispatcher,
            stop_event,
            stream_name=V2_TASK_SUBMISSION_STREAM_NAME,
            log_name="任务提交",
        )
    )
    task_submission_reconciliation_task = asyncio.create_task(
        _task_submission_reconciliation_loop(
            TaskSubmissionProjectionService(
                repository=PostgresTaskSubmissionDeliveryRepository(),
                queue=queue,
                web_queue=web_queue,
            ),
            stop_event,
        )
    )
    delivery_task = asyncio.create_task(
        _v2_reliable_delivery_loop(delivery_runtime, stop_event)
    )
    logger.info(
        "Alioth Web Worker 已启动，并发度=%d，queue=%s，可靠提交投影已启用。",
        settings.worker.web_concurrency,
        settings.queue.web_agent_run_queue_key,
    )
    await stop_event.wait()
    logger.info("正在等待 Web worker 协程处理完当前任务后退出...")
    await asyncio.gather(
        *tasks,
        heartbeat_task,
        reconciliation_task,
        task_submission_consumer_task,
        task_submission_reconciliation_task,
        delivery_task,
        return_exceptions=True,
    )
    await delivery_runtime.close()
    await redis_client.aclose()


async def main(
    *,
    h5_supplemental_evidence_policy: SupplementalEvidencePolicy | None = None,
) -> None:
    configure_logging()
    settings = get_settings()
    roles = set(settings.worker.roles)
    if roles == {"web_explorer"}:
        await _run_web_only_worker(settings)
        return
    effective_h5_supplemental_evidence_policy = h5_supplemental_evidence_policy
    if effective_h5_supplemental_evidence_policy is None:
        effective_h5_supplemental_evidence_policy = (
            build_h5_supplemental_evidence_policy(
                enabled=settings.supplemental_evidence.enabled,
                h5_v2_mode=settings.workflow.h5_v2_mode,
            )
            if settings.workflow.h5_v2_mode == "on"
            else build_current_supplemental_evidence_policy(
                enabled=settings.supplemental_evidence.enabled
            )
        )
    effective_h5_supplemental_evidence_policy.validate_runtime_ready()
    rule_engine_configuration_guard = RuleEngineConfigurationGuard(PostgresTaskRepository())
    redis_client = build_redis_client(settings.queue)
    queue = RedisAgentRunQueue(
        redis_client,
        ttl_seconds=settings.queue.run_record_ttl_seconds,
        queue_key=settings.queue.agent_run_queue_key,
    )
    web_queue = RedisAgentRunQueue(
        redis_client,
        ttl_seconds=settings.queue.run_record_ttl_seconds,
        queue_key=settings.queue.web_agent_run_queue_key,
    )
    state_store = build_agent_run_state_store()
    browser_full_inspection_enabled = (
        settings.workflow.h5_v2_mode == "on" or settings.workflow.web_v2_mode == "on"
    )
    task_finalization_service = (
        build_default_task_finalization_service()
        if (settings.workflow.app_qc_enabled or browser_full_inspection_enabled)
        else None
    )
    retry_report_generator = (
        ReportGenerationService(query_service=TaskQueryService(repository=PostgresTaskRepository()))
        if task_finalization_service is not None
        else None
    )
    validate_completion_gate_configuration(
        settings,
        state_store=state_store,
        task_finalization_service=task_finalization_service,
    )
    validate_task_submission_configuration(settings, state_store=state_store)
    validate_web_worker_configuration(settings)
    worker_instance_id = _new_worker_instance_id()
    delivery_runtime = build_default_reliable_delivery_runtime(
        redis_client=redis_client,
        lease_owner=f"{worker_instance_id}:v2-outbox-relay",
    )
    app_workflow_handler = (
        build_app_workflow_handler(
            settings,
            worker_instance_id,
            queue=queue,
            state_store=state_store,
        )
        if settings.workflow.app_qc_enabled
        else None
    )
    sealed_inspection_publisher = build_default_app_sealed_inspection_publisher()
    app_publication_repository = PostgresAppInspectionPublicationRepository(
        max_attempts=settings.inspection_work_items.max_attempts,
        retry_delay_seconds=max(1, int(settings.inspection_work_items.retry_base_seconds)),
    )
    h5_evidence_handoff = (
        # 交接服务只传给浏览器 Runtime 外层阶段；不得注入纯 Explorer Runtime。
        # 外层仍在探索封存后发布 Inspection Work，并由独立 Rule Judge 消费。
        _build_default_h5_evidence_handoff()
        if settings.workflow.h5_v2_mode == "on"
        else None
    )
    h5_workflow_handler = (
        build_h5_workflow_handler(
            settings,
            worker_instance_id,
            cancellation=RedisH5CancellationPort(queue, durable_source=state_store),
            supplemental_evidence_policy=effective_h5_supplemental_evidence_policy,
        )
        if settings.workflow.h5_v2_mode == "on"
        else None
    )
    h5_shadow_recorder = (
        build_h5_workflow_shadow_recorder() if settings.workflow.h5_v2_mode == "shadow" else None
    )
    web_publication_wakeup = (
        ReliableDeliveryWebPublicationWakeup(delivery_runtime.relay_once)
        if settings.workflow.web_v2_mode != "off"
        else None
    )
    web_retry_report_generator = (
        AuditedWebFinalReportGenerator(
            delegate=retry_report_generator,
            ledger=PostgresWebReportGenerationAttemptRepository(),
        )
        if retry_report_generator is not None
        else None
    )
    web_workflow_handler = (
        build_web_workflow_handler(
            settings,
            worker_instance_id,
            publication_wakeup=web_publication_wakeup,
        )
        if settings.workflow.web_v2_mode != "off" and "web_explorer" in roles
        else None
    )
    web_formal_session_closer = getattr(
        web_workflow_handler,
        "formal_session_closer",
        None,
    )
    if settings.workflow.web_v2_mode == "on" and web_formal_session_closer is None:
        web_formal_session_closer = build_web_formal_session_closer(settings)
    web_inspection_reconciliation = None
    if settings.workflow.web_v2_mode == "on":
        if task_finalization_service is None:
            raise RuntimeError("Web full_inspection 缺少 Task Finalization Service")
        assert web_publication_wakeup is not None
        web_inspection_reconciliation = _build_default_web_formal_inspection_dependencies(
            settings=settings,
            worker_instance_id=worker_instance_id,
            task_finalization_service=task_finalization_service,
            publication_wakeup=web_publication_wakeup,
            session_closer=web_formal_session_closer,
        )
    app_run_recovery = (
        PostgresAppRunRecoveryRepository() if settings.workflow.app_qc_enabled else None
    )
    app_exploration_reconciler = (
        AppExplorationReconciler(
            failures=PostgresAppExplorationFailureRepository(),
            result_intents=PostgresAppExplorationResultIntentRepository(),
            evidence_indexes=PostgresAppEvidenceIndexRepository(),
            exploration_results=PostgresAppExplorationResultRepository(),
            sessions=PostgresSurfaceSessionRepository(),
        )
        if settings.workflow.app_qc_enabled
        else None
    )
    app_manifest_reconciler = None
    if settings.workflow.app_qc_enabled:
        manifest_repository = PostgresEvidenceManifestRepository(
            permitted_task_statuses=(
                "queued",
                "running",
                "review",
                "done",
                "failed",
                "canceled",
            )
        )
        app_manifest_reconciler = AppManifestBacklogReconciler(
            manifests=manifest_repository,
            finalizer=AppEvidenceManifestFinalizer(
                repository=manifest_repository,
                artifact_store=build_evidence_artifact_store(settings.evidence_storage),
            ),
        )
    await queue.record_worker_heartbeat(worker_instance_id, roles=settings.worker.roles)
    if app_run_recovery is not None:
        await _reconcile_recoverable_app_runs(queue, app_run_recovery)
    if app_exploration_reconciler is not None:
        await app_exploration_reconciler.reconcile_once()
    if app_manifest_reconciler is not None:
        await app_manifest_reconciler.reconcile_once()
    await _reconcile_orphaned_runs(
        queue,
        state_store,
        task_finalization_service,
        app_run_recovery,
        web_formal_session_closer,
        web_inspection_reconciliation,
        web_retry_report_generator,
    )
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            # Windows 不支持 add_signal_handler；本地开发时只能靠 Ctrl+C 触发的
            # KeyboardInterrupt 中断，详见模块文档字符串。
            pass

    concurrency = settings.worker.concurrency
    run_explorer = "explorer" in roles
    run_web_explorer = "web_explorer" in roles
    run_judge = "judge" in roles
    logger.info(
        "Alioth Worker 已启动，并发度=%d，roles=%s。",
        concurrency,
        ",".join(sorted(roles)),
    )
    concurrency_warning = describe_worker_device_concurrency(concurrency)
    if concurrency_warning:
        logger.warning("%s", concurrency_warning)
    heartbeat_task = asyncio.create_task(
        _worker_heartbeat_loop(queue, stop_event, instance_id=worker_instance_id)
    )
    reconciliation_task = asyncio.create_task(
        _orphan_reconciliation_loop(
            queue,
            state_store,
            stop_event,
            task_finalization_service=task_finalization_service,
            app_run_recovery=app_run_recovery,
            web_formal_session_closer=web_formal_session_closer,
            web_inspection_reconciliation=web_inspection_reconciliation,
            web_final_report_generator=web_retry_report_generator,
        )
    )
    app_run_recovery_task = None
    if app_run_recovery is not None:
        app_run_recovery_task = asyncio.create_task(
            _app_run_recovery_loop(
                queue,
                app_run_recovery,
                stop_event,
                interval_seconds=settings.worker.app_run_recovery_interval_seconds,
            )
        )
    device_lease_recovery_task = None
    if settings.workflow.app_qc_enabled and is_postgres_device_lease_backend():
        device_lease_recovery_task = asyncio.create_task(
            run_device_lease_recovery_loop(
                stop_event,
                interval_seconds=settings.worker.device_lease_recovery_interval_seconds,
                grace_seconds=settings.worker.device_lease_recovery_grace_seconds,
            )
        )
    app_exploration_reconciliation_task = None
    if app_exploration_reconciler is not None:
        app_exploration_reconciliation_task = asyncio.create_task(
            _app_exploration_reconciliation_loop(
                app_exploration_reconciler,
                stop_event,
                interval_seconds=settings.worker.app_run_recovery_interval_seconds,
            )
        )
    app_manifest_reconciliation_task = None
    if app_manifest_reconciler is not None:
        app_manifest_reconciliation_task = asyncio.create_task(
            _app_manifest_reconciliation_loop(
                app_manifest_reconciler,
                stop_event,
                interval_seconds=settings.worker.app_run_recovery_interval_seconds,
        )
    )
    completion_gate_retry_task = None
    if task_finalization_service is not None and run_judge:
        completion_gate_retry_task = asyncio.create_task(
            _completion_gate_retry_loop(
                queue,
                state_store,
                task_finalization_service,
                stop_event,
                final_report_generator=retry_report_generator,
                web_final_report_generator=web_retry_report_generator,
                web_inspection_reconciliation=web_inspection_reconciliation,
                finalization_stall_timeout_seconds=settings.worker.finalization_stall_timeout_seconds,
                sealed_inspection_publisher=sealed_inspection_publisher,
                settings=settings,
            )
        )
    delivery_task = asyncio.create_task(_v2_reliable_delivery_loop(delivery_runtime, stop_event))
    task_submission_stream_consumer, task_submission_dispatcher = _build_task_submission_consumer(
        redis_client,
        queue,
        web_queue,
        settings,
        worker_instance_id=worker_instance_id,
    )
    task_submission_consumer_task = asyncio.create_task(
        _v2_inspection_consumer_loop(
            task_submission_stream_consumer,
            task_submission_dispatcher,
            stop_event,
            stream_name=V2_TASK_SUBMISSION_STREAM_NAME,
            log_name="任务提交",
        )
    )
    task_submission_reconciliation_task = asyncio.create_task(
        _task_submission_reconciliation_loop(
            TaskSubmissionProjectionService(
                repository=PostgresTaskSubmissionDeliveryRepository(),
                queue=queue,
                web_queue=web_queue,
            ),
            stop_event,
        )
    )
    logger.info(
        "任务提交可靠投影已启动：stream=%s group=%s。",
        V2_TASK_SUBMISSION_STREAM_NAME,
        V2_TASK_SUBMISSION_CONSUMER_GROUP,
    )
    # E17：离场预留过期回收；门禁关闭时装配的是空门禁，回收为无操作
    evidence_exit_sweeper = EvidenceExitReservationSweeper(
        gate=build_web_evidence_exit_gate(),
        interval_seconds=settings.web_evidence_exit_gate.expire_sweep_interval_seconds,
        limit=settings.web_evidence_exit_gate.expire_sweep_limit,
    )
    evidence_exit_sweeper.start()
    lifecycle_settings = settings.evidence_lifecycle
    evidence_lifecycle_sweeper = EvidenceLifecycleSweeper(
        service=EvidenceLifecycleService(
            repository=PostgresEvidenceLifecycleRepository(),
            policy=EvidenceLifecyclePolicy(
                policy_version=lifecycle_settings.policy_version,
                retention_days=lifecycle_settings.retention_days,
                gc_enabled=lifecycle_settings.gc_enabled,
                batch_limit=lifecycle_settings.batch_limit,
            ),
        ),
        interval_seconds=lifecycle_settings.sweep_interval_seconds,
    )
    evidence_lifecycle_sweeper.start()
    inspection_consumer_task = None
    inspection_judge_task = None
    h5_publication_consumer_task = None
    h5_publication_reconciliation_task = None
    app_publication_consumer_task = None
    app_publication_reconciliation_task = None
    aggregate_inspection_consumer_task = None
    aggregate_inspection_reconciliation_task = None
    if run_judge:
        (
            inspection_stream_consumer,
            inspection_dispatcher,
            inspection_judge_dispatcher,
        ) = _build_v2_inspection_consumer(
            redis_client,
            settings,
            worker_instance_id=worker_instance_id,
            supplemental_evidence_policy=effective_h5_supplemental_evidence_policy,
            supplemental_evidence_target_platforms=(
                frozenset({"h5"})
                if settings.workflow.h5_v2_mode == "on"
                else None
            ),
        )
        inspection_consumer_task = asyncio.create_task(
            _v2_inspection_consumer_loop(
                inspection_stream_consumer,
                inspection_dispatcher,
                stop_event,
            )
        )
        inspection_judge_task = asyncio.create_task(
            inspection_judge_dispatcher.run(
                stop_event,
                concurrency=settings.inspection_work_items.judge_concurrency,
            )
        )
        logger.info(
            "V2 质检唤醒与独立 Judge worker 已启动：stream=%s group=%s "
            "judge_concurrency=%d batch_limit=%d publication_batch_limit=%d "
            "publication_timeout_seconds=%.1f queue_capacity=%d。",
            V2_INSPECTION_STREAM_NAME,
            V2_INSPECTION_CONSUMER_GROUP,
            settings.inspection_work_items.judge_concurrency,
            settings.inspection_work_items.dispatch_batch_limit,
            settings.inspection_work_items.publication_batch_limit,
            settings.inspection_work_items.publication_timeout_seconds,
            settings.inspection_work_items.dispatch_queue_capacity,
        )
        app_publication_stream_consumer, app_publication_dispatcher = (
            _build_app_publication_consumer(
                redis_client,
                settings,
                worker_instance_id=worker_instance_id,
            )
        )
        app_publication_consumer_task = asyncio.create_task(
            _v2_inspection_consumer_loop(
                app_publication_stream_consumer,
                app_publication_dispatcher,
                stop_event,
                stream_name=V2_APP_PUBLICATION_STREAM_NAME,
                log_name="App Publication",
                stale_claim_interval_seconds=max(
                    1.0, settings.inspection_work_items.retry_base_seconds
                ),
                stale_claim_min_idle_milliseconds=max(
                    1_000,
                    int(settings.inspection_work_items.retry_base_seconds * 1_000),
                ),
            )
        )
        app_publication_reconciliation_task = asyncio.create_task(
            _app_publication_reconciliation_loop(
                PostgresAppInspectionPublicationRepository(
                    max_attempts=settings.inspection_work_items.max_attempts,
                    retry_delay_seconds=max(
                        1, int(settings.inspection_work_items.retry_base_seconds)
                    ),
                ),
                stop_event,
                sealed_inspection_publisher=sealed_inspection_publisher,
            )
        )
        logger.info(
            "App Publication 唤醒消费已启动：stream=%s group=%s。",
            V2_APP_PUBLICATION_STREAM_NAME,
            V2_APP_PUBLICATION_CONSUMER_GROUP,
        )
        aggregate_stream_consumer, aggregate_dispatcher = _build_aggregate_inspection_consumer(
            redis_client,
            settings,
            worker_instance_id=worker_instance_id,
        )
        aggregate_inspection_consumer_task = asyncio.create_task(
            _v2_inspection_consumer_loop(
                aggregate_stream_consumer,
                aggregate_dispatcher,
                stop_event,
                stream_name=V2_AGGREGATE_INSPECTION_STREAM_NAME,
                log_name="聚合质检",
                stale_claim_interval_seconds=max(
                    1.0, settings.inspection_work_items.retry_base_seconds
                ),
                stale_claim_min_idle_milliseconds=max(
                    1_000,
                    int(settings.inspection_work_items.retry_base_seconds * 1_000),
                ),
            )
        )
        aggregate_inspection_reconciliation_task = asyncio.create_task(
            _aggregate_inspection_reconciliation_loop(
                PostgresAggregateInspectionPublicationRepository(),
                PostgresAggregateInspectionExecutionRepository(),
                settings,
                stop_event,
            )
        )
        logger.info(
            "聚合质检唤醒消费已启动：stream=%s group=%s。",
            V2_AGGREGATE_INSPECTION_STREAM_NAME,
            V2_AGGREGATE_INSPECTION_CONSUMER_GROUP,
        )
        # 消费端服务于数据库中已冻结的任务快照，不能随新任务部署默认值关闭。
        if run_judge:
            h5_publication_stream_consumer, h5_publication_dispatcher = (
                _build_h5_publication_consumer(
                    redis_client,
                    settings,
                    worker_instance_id=worker_instance_id,
                )
            )
            h5_publication_consumer_task = asyncio.create_task(
                _v2_inspection_consumer_loop(
                    h5_publication_stream_consumer,
                    h5_publication_dispatcher,
                    stop_event,
                    stream_name=V2_H5_PUBLICATION_STREAM_NAME,
                    log_name="H5 Publication",
                    stale_claim_interval_seconds=max(
                        1.0, settings.inspection_work_items.retry_base_seconds
                    ),
                    stale_claim_min_idle_milliseconds=max(
                        1_000,
                        int(settings.inspection_work_items.retry_base_seconds * 1_000),
                    ),
                )
            )
            h5_publication_reconciliation_task = asyncio.create_task(
                _h5_publication_reconciliation_loop(
                    PostgresH5InspectionPublicationRepository(
                        max_attempts=settings.inspection_work_items.max_attempts,
                        retry_delay_seconds=max(
                            1, int(settings.inspection_work_items.retry_base_seconds)
                        ),
                    ),
                    stop_event,
                )
            )
            logger.info(
                "H5 Publication 唤醒消费已启动：stream=%s group=%s。",
                V2_H5_PUBLICATION_STREAM_NAME,
                V2_H5_PUBLICATION_CONSUMER_GROUP,
            )
    tasks: list[asyncio.Task[None]] = []
    run_capacity = asyncio.BoundedSemaphore(settings.worker.max_active_runs)
    if run_explorer:
        tasks.extend(_start_worker_loops(
            concurrency=concurrency,
            queue=queue,
            settings=settings,
            stop_event=stop_event,
            state_store=state_store,
            worker_instance_id=worker_instance_id,
            app_workflow_handler=app_workflow_handler,
            sealed_inspection_publisher=sealed_inspection_publisher,
            app_publication_repository=app_publication_repository,
            h5_workflow_handler=h5_workflow_handler,
            h5_evidence_handoff=h5_evidence_handoff,
            h5_shadow_recorder=h5_shadow_recorder,
            task_finalization_service=task_finalization_service,
            app_run_recovery=app_run_recovery,
            rule_engine_configuration_guard=rule_engine_configuration_guard,
            run_capacity=run_capacity,
        ))
    if run_web_explorer:
        if web_workflow_handler is None:
            raise RuntimeError("Web Explorer 已启用但 Web Workflow handler 未配置")
        tasks.extend(
            _start_web_worker_loops(
                concurrency=settings.worker.web_concurrency,
                queue=web_queue,
                settings=settings,
                stop_event=stop_event,
                state_store=state_store,
                worker_instance_id=worker_instance_id,
                web_workflow_handler=web_workflow_handler,
                web_inspection_reconciliation=web_inspection_reconciliation,
                task_finalization_service=task_finalization_service,
                final_report_generator=retry_report_generator,
                web_final_report_generator=web_retry_report_generator,
                rule_engine_configuration_guard=rule_engine_configuration_guard,
                run_capacity=run_capacity,
            )
        )
    if not run_explorer and not run_web_explorer and not run_judge:
        raise RuntimeError(
            "ALIOTH_WORKER_ROLES 至少需要 explorer、web_explorer 或 judge 之一"
    )
    await stop_event.wait()
    logger.info("正在等待所有 worker 协程处理完当前任务后退出...")
    background_tasks = [
        *tasks,
        heartbeat_task,
        reconciliation_task,
        delivery_task,
        task_submission_consumer_task,
        task_submission_reconciliation_task,
    ]
    if inspection_consumer_task is not None:
        background_tasks.append(inspection_consumer_task)
    if inspection_judge_task is not None:
        background_tasks.append(inspection_judge_task)
    if h5_publication_consumer_task is not None:
        background_tasks.append(h5_publication_consumer_task)
    if h5_publication_reconciliation_task is not None:
        background_tasks.append(h5_publication_reconciliation_task)
    if app_publication_consumer_task is not None:
        background_tasks.append(app_publication_consumer_task)
    if app_publication_reconciliation_task is not None:
        background_tasks.append(app_publication_reconciliation_task)
    if aggregate_inspection_consumer_task is not None:
        background_tasks.append(aggregate_inspection_consumer_task)
    if aggregate_inspection_reconciliation_task is not None:
        background_tasks.append(aggregate_inspection_reconciliation_task)
    if completion_gate_retry_task is not None:
        background_tasks.append(completion_gate_retry_task)
    if app_run_recovery_task is not None:
        background_tasks.append(app_run_recovery_task)
    if device_lease_recovery_task is not None:
        background_tasks.append(device_lease_recovery_task)
    if app_exploration_reconciliation_task is not None:
        background_tasks.append(app_exploration_reconciliation_task)
    if app_manifest_reconciliation_task is not None:
        background_tasks.append(app_manifest_reconciliation_task)
    await asyncio.gather(*background_tasks, return_exceptions=True)
    await evidence_lifecycle_sweeper.stop()
    await evidence_exit_sweeper.stop()
    await delivery_runtime.close()
    await redis_client.aclose()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("收到 Ctrl+C，Worker 已中断退出。")
