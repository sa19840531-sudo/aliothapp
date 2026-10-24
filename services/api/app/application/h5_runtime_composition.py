"""H5 V2 Explorer 的生产组合根与完整消费循环。"""

from __future__ import annotations

import asyncio
import logging
import math
import re
import traceback
from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, Protocol
from urllib.parse import urlsplit

from services.api.app.agents.explorer.h5_contracts import H5ExplorerAgent
from services.api.app.agents.explorer.maf_agent import (
    ExplorerAgentOutputError,
    ExplorerModelUnavailableError,
    ExplorerPrematureTerminalError,
)
from services.api.app.agents.runtime.action_trace import InMemoryActionTraceSink
from services.api.app.agents.runtime.models import (
    ErrorCategory,
    ExecutionOutcome,
    RunResult,
    TerminationReason,
)
from services.api.app.agents.runtime.run_context import (
    AgentRunContext,
    bind_agent_run_context,
)
from services.api.app.agents.runtime.usage_meter import get_current_llm_usage_meter
from services.api.app.agents.runtime.working_memory import WorkingMemory
from services.api.app.agents.tools.page_detection_tools import (
    detect_page_for_agent,
    persist_page_detection_for_current_run,
)
from services.api.app.application.app_exploration_lease import (
    AppExplorationLeaseLostError,
)
from services.api.app.application.app_workflow_dispatch import (
    H5RuntimeProvider,
    H5V2RuntimeExecutor,
    H5WorkflowHandler,
    RuntimeBackedH5WorkflowHandler,
    task_target_from_dict,
)
from services.api.app.application.capture_coordination import (
    CaptureCoordinationCommand,
    CaptureCoordinationError,
    CaptureCoordinationStage,
)
from services.api.app.application.capture_profiles import CaptureProfilePolicy
from services.api.app.application.exploration_scheduler_facts import (
    ExplorationSchedulerFactsReader,
)
from services.api.app.application.exploration_work_queue import (
    ExplorerWorkStatus,
)
from services.api.app.application.explorer_turn import ExplorerTurnExecutor
from services.api.app.application.h5_adaptive_navigation import (
    H5InteractionPatternKnowledgePort,
)
from services.api.app.application.h5_age_verification import (
    H5_AGE_VERIFICATION_CAPABILITY,
    H5_AGE_VERIFICATION_KEYWORDS,
    H5_AGE_VERIFICATION_PAGE_STATE,
    H5_AGE_VERIFICATION_PURPOSE,
    H5_AGE_VERIFICATION_TARGET,
    H5_AGE_VERIFICATION_TARGET_MODE,
)
from services.api.app.application.h5_auth_audit import SurfacePhaseH5AuthAudit
from services.api.app.application.h5_auth_bootstrap import (
    H5AuthBootstrapPort,
    H5AuthBootstrapService,
    NoopH5AuthBootstrap,
)
from services.api.app.application.h5_auth_navigation import (
    CoordinatedH5AuthNavigation,
    DetectedH5AuthPageReader,
)
from services.api.app.application.h5_auth_page_analyzer import H5AuthPageAnalyzer
from services.api.app.application.h5_exploration_cancellation import (
    H5CancellationCheckpoint,
    H5CancellationPort,
    H5ExplorationCanceled,
)
from services.api.app.application.h5_exploration_observation import (
    build_h5_frontier,
    h5_candidate_purpose,
    h5_observation_allows_completion,
)
from services.api.app.application.h5_exploration_recovery import (
    H5BrowserRecoveryAttemptFacts,
    H5ExplorationRecovery,
    H5FailureStage,
    H5RecoveryAttemptReservation,
    H5RecoveryContext,
    H5RecoveryOutcome,
    _source_restoration_assessment_accepts,
    apply_h5_frontier_restoration_result,
    build_h5_recovery_session_id,
    h5_frontier_type_drift_is_proven,
    h5_global_bottom_navigation_index,
    restore_h5_frontier_source,
)
from services.api.app.application.h5_exploration_turn import (
    H5ExplorationTurnOutcome,
    H5ExplorationTurnRunner,
    H5FrontierRecoveryWorkResult,
)
from services.api.app.application.h5_exploration_workflow import (
    H5_SURFACE_SESSION_OWNER,
    CoordinatedH5SafetyInterstitialBypass,
    H5ExplorationResult,
    H5ExplorationWorkflow,
    H5FreshCandidateRebindRejected,
    H5PageTypeAssessmentPersistencePort,
    H5SurfaceBootstrapper,
    RecoverH5ExplorationCommand,
    RunH5WorkflowCommand,
    _rebind_selected_task_entry_to_fresh_frontier,
    _select_next_task_entry,
    _with_task_frontier,
)
from services.api.app.application.h5_frontier_graph_route import (
    H5FrontierGraphOutcome,
    plan_h5_frontier_graph_steps,
)
from services.api.app.application.h5_line_loading_recovery import (
    H5_LINE_LOADING_LOGIN_RETRY_CAPABILITY,
    H5_LINE_LOADING_LOGIN_RETRY_KEYWORDS,
    H5_LINE_LOADING_LOGIN_RETRY_PURPOSE,
    H5_LINE_LOADING_LOGIN_RETRY_TARGET,
    H5_LINE_LOADING_LOGIN_RETRY_TARGET_MODE,
)
from services.api.app.application.h5_list_detail_navigation import (
    H5_LIST_DETAIL_TARGET,
    h5_list_detail_expected_page_type,
    h5_list_detail_selector_allowed,
)
from services.api.app.application.h5_live_inspection_publish import (
    H5ActiveSessionInspectionDrainService,
    build_default_h5_active_session_inspection_drain_service,
)
from services.api.app.application.h5_llm_budget import h5_llm_budget_error_code
from services.api.app.application.h5_page_type_assessment_adapter import (
    adapt_h5_page_detection_to_assessment,
)
from services.api.app.application.h5_payment_horizontal_collection import (
    H5_PAYMENT_HORIZONTAL_TARGET,
    H5_PAYMENT_HORIZONTAL_TARGET_MODE,
    h5_payment_horizontal_action_parameters,
    h5_payment_horizontal_context_supported,
    h5_payment_horizontal_keywords,
    h5_payment_horizontal_purpose,
)
from services.api.app.application.h5_popup_loop_policy import (
    H5_POPUP_DISMISSAL_STALLED_REASON_CODE,
    evaluate_h5_business_page_popup_loop,
    popup_dismissal_reobserve_exhausted,
)
from services.api.app.application.h5_profile_activity_navigation import (
    H5_PROFILE_ACTIVITY_EXPECTED_PAGE_TYPE,
    H5_PROFILE_ACTIVITY_SOURCE_PAGE_TYPE,
    H5_PROFILE_ACTIVITY_TARGET_MODE,
    h5_profile_activity_capability_for_target,
)
from services.api.app.application.h5_profile_payment_navigation import (
    H5_PROFILE_PAYMENT_EXPECTED_PAGE_TYPE,
    H5_PROFILE_PAYMENT_SOURCE_PAGE_TYPE,
    H5_PROFILE_PAYMENT_TARGET,
    H5_PROFILE_PAYMENT_TARGET_MODE,
    h5_profile_payment_capability_from_keywords,
    h5_profile_payment_entry_label,
)
from services.api.app.application.h5_profile_settings_navigation import (
    H5_PROFILE_FAVORITES_PAGE_BACK_CAPABILITY,
    H5_PROFILE_FAVORITES_PAGE_BACK_EXPECTED_PAGE_TYPE,
    H5_PROFILE_FAVORITES_PAGE_BACK_TARGET_MODE,
    H5_PROFILE_SETTINGS_EXPECTED_PAGE_TYPE,
    H5_PROFILE_SETTINGS_SOURCE_PAGE_TYPE,
    H5_PROFILE_SETTINGS_TARGET_MODE,
    H5_SETTINGS_PAGE_BACK_CAPABILITY,
    H5_SETTINGS_PAGE_BACK_SOURCE_PAGE_TYPE,
    H5_SETTINGS_PAGE_BACK_TARGET_MODE,
    h5_profile_favorites_page_back_source_type_allowed,
    h5_profile_settings_capability_for_target,
)
from services.api.app.application.h5_profile_visual_navigation import (
    h5_profile_visual_entry_contract,
)
from services.api.app.application.h5_structural_navigation import (
    H5_BOTTOM_NAVIGATION_FAMILY_ADAPTIVE,
    h5_bottom_navigation_action_parameters,
    h5_bottom_navigation_family,
    h5_match_bottom_navigation_target,
    h5_match_page_tab_target,
)
from services.api.app.application.h5_task_runtime_settings import (
    H5TaskRuntimeSettings,
    build_h5_task_runtime_settings,
)
from services.api.app.application.h5_viewport import h5_viewport_parameters
from services.api.app.application.orchestration import ExplorationOrchestrator
from services.api.app.application.supplemental_evidence_policy import (
    SupplementalEvidencePolicy,
)
from services.api.app.core.config import Settings
from services.api.app.core.h5_auth_config import FileH5CredentialConfigResolver
from services.api.app.domain.exploration import (
    CandidateAction,
    ExplorationBoundaries,
    ExplorationBudget,
    ExplorationTerminal,
    ExplorationTerminalStatus,
    ExplorationWork,
    H5AuthBootstrapResult,
    SideEffectPolicy,
    consume_exploration_action_budget,
)
from services.api.app.domain.exploration.h5_frontier import (
    H5_TASK_FRONTIER_METADATA_KEY,
    H5FrontierEntryStatus,
    H5TaskFrontierEntry,
    H5TaskFrontierSnapshot,
    canonical_h5_route_identity,
)
from services.api.app.domain.exploration.h5_page_state import (
    h5_page_state_has_overlay,
    h5_page_state_supports_popup_dismissal,
)
from services.api.app.domain.exploration.h5_visual_navigation import (
    VISUAL_TARGET_RISK_LEVELS,
    verify_visual_navigation_capability,
)
from services.api.app.domain.interaction import (
    ActionIntent,
    CaptureKind,
    CaptureRef,
    InteractionOperation,
    InteractionResult,
    InteractionResultStatus,
    InteractionRiskLevel,
    SurfaceKind,
    SurfaceSessionSpec,
)
from services.api.app.domain.protocol import stable_protocol_hash, thaw_json_mapping
from services.api.app.domain.task import (
    TaskExecutionMode,
    TaskTargetPlatform,
    task_execution_mode_from_config,
)
from services.api.app.infrastructure.browser.capture_runtime import (
    WebCaptureRuntime,
    get_or_create_web_capture_runtime,
)
from services.api.app.infrastructure.browser.h5_secret_input_runtime import (
    PlaywrightH5SecretInputRuntime,
)
from services.api.app.infrastructure.db.action_execution_repository import (
    PostgresActionExecutionRepository,
)
from services.api.app.infrastructure.db.exploration_scheduler_facts_repository import (
    PostgresExplorationSchedulerFactsReader,
)
from services.api.app.infrastructure.db.explorer_work_repository import (
    PostgresExplorationWorkPublisher,
    PostgresExplorerWorkRepository,
)
from services.api.app.infrastructure.db.h5_interaction_pattern_repository import (
    PostgresH5InteractionPatternRepository,
)
from services.api.app.infrastructure.db.h5_page_type_assessment_repository import (
    H5PageTypeAssessmentRepository,
)
from services.api.app.infrastructure.db.llm_usage_event_repository import (
    PostgresLLMUsageEventRepository,
)
from services.api.app.infrastructure.db.pool import get_postgres_pool
from services.api.app.infrastructure.db.surface_phase_repository import (
    PostgresSurfacePhaseRepository,
)
from services.api.app.infrastructure.queue.redis_queue import AgentRunRecord

logger = logging.getLogger(__name__)

# 重领等待必须覆盖队列 retry_delay_seconds（20s），否则重领扑空提前收场；
# 连续重领 2 次恰好用满工作项 3 次尝试预算。
_DEFAULT_RETRY_WAIT_SECONDS = 22.0
_H5_SHUTDOWN_TIMEOUT_SECONDS = 10.0
_DEFAULT_MAX_RETRY_RECLAIMS = 2
_POPUP_DISMISSAL_TARGET = "关闭当前弹窗"
_POPUP_DISMISSAL_CAPABILITIES = frozenset(
    {
        "dialog_content_close",
        "dialog_active_ad_close",
        "dialog_text_ad_close",
        "dialog_accessible_close",
        "dialog_acknowledgement",
        "dialog_generic_close",
        "dialog_overlay",
        "home_suspend_close",
        "overlay_ad_close",
        "homepage_payment_close",
        "flutter_app_open_popup_close",
        "flutter_centered_ad_popup_close",
        "flutter_centered_ad_popup_close_above",
        "flutter_centered_scroll_popup_close_below",
        "flutter_image_popup_top_right_close",
        "flutter_account_notice_top_right_close",
        "flutter_marketing_announcement_confirm",
        "modal_local_safe_acknowledgement",
        "rendered_modal_close",
        "profile_credential_screenshot_confirm",
        "visible_text_acknowledgement",
        "visible_text_close",
        "visible_text_skip",
        "visible_text_later",
    }
)
H5_AUTH_REVIEW_ERROR_PREFIX = "h5_auth_bootstrap:"
H5_COVERAGE_NO_INVENTORY_REVIEW_CODE = "h5_coverage_scope_unready_without_inventory"
H5_COVERAGE_BLOCKED_SCOPE_REVIEW_CODE = "h5_coverage_blocked_scope_terminal"
H5_COVERAGE_WORK_DEAD_END_REVIEW_CODE = "h5_coverage_work_dead_end_terminal"
H5_EXPLORATION_NO_CLAIMABLE_WORK_REVIEW_CODE = "h5_exploration_no_claimable_work_after_lease_lost"
H5_ACTION_BUDGET_EXHAUSTED_REVIEW_CODE = "h5_action_budget_exhausted"
H5_CAPABILITY_PROJECTION_REOBSERVE_EXHAUSTED_REASON_CODE = (
    "h5_capability_projection_reobserve_exhausted"
)
H5_FRONTIER_EMPTY_WITH_ACTIVE_OBSTRUCTION_REASON_CODE = "h5_frontier_empty_with_active_obstruction"
H5_FRONTIER_OBJECTIVE_NOT_SATISFIED_REASON_CODE = "task_frontier_objective_not_satisfied"
H5_FRONTIER_CLEANLY_EXHAUSTED_REASON_CODE = "h5_frontier_cleanly_exhausted"
H5_FRONTIER_PAGE_BUDGET_EXHAUSTED_REASON_CODE = "h5_frontier_page_budget_exhausted"
_H5_EXPLORATION_OBJECTIVE = (
    "在只读安全边界内自动探索 H5 页面，覆盖可达页面状态，并记录可追溯的页面观察与操作前后 Capture。"
)


@dataclass(frozen=True, kw_only=True)
class H5FastReviewSnapshot:
    """H5 运行循环早停所需的最小完成事实。"""

    assessment_scope_ready: bool
    completion_ready: bool
    inventory_item_count: int
    target_coverage_ready: bool = False
    coverage_revision: int | None = None
    covered_page_type_count: int = 0
    blocking_scope_count: int = 0
    active_work_count: int = 0
    claimable_work_count: int = 0
    retry_waiting_work_count: int = 0
    exhausted_work_count: int = 0
    settled_work_count: int = 0

    @property
    def coverage_unready_without_inventory(self) -> bool:
        return (
            not self.assessment_scope_ready
            and not self.completion_ready
            and not self.target_coverage_ready
            and self.inventory_item_count == 0
            and self.coverage_revision is not None
            and self.covered_page_type_count == 0
            and self.blocking_scope_count > 0
            and self.active_work_count == 0
            and self.claimable_work_count == 0
            and self.retry_waiting_work_count == 0
        )

    @property
    def blocked_scope_requires_review(self) -> bool:
        return (
            not self.completion_ready
            and not self.target_coverage_ready
            and self.inventory_item_count > 0
            and self.coverage_revision is not None
            and self.blocking_scope_count > 0
            and self.active_work_count == 0
            and self.claimable_work_count == 0
            and self.retry_waiting_work_count == 0
        )

    @property
    def work_dead_end_requires_review(self) -> bool:
        return (
            not self.completion_ready
            and not self.target_coverage_ready
            and self.inventory_item_count > 0
            and self.coverage_revision is not None
            and self.active_work_count == 0
            and self.claimable_work_count == 0
            and self.retry_waiting_work_count == 0
            and (self.exhausted_work_count > 0 or self.settled_work_count > 0)
        )

    @property
    def fast_review_error_code(self) -> str | None:
        if self.target_coverage_ready:
            return None
        if self.coverage_unready_without_inventory:
            return H5_COVERAGE_NO_INVENTORY_REVIEW_CODE
        if self.blocked_scope_requires_review:
            return H5_COVERAGE_BLOCKED_SCOPE_REVIEW_CODE
        if self.work_dead_end_requires_review:
            return H5_COVERAGE_WORK_DEAD_END_REVIEW_CODE
        return None


class H5FastReviewProbe(Protocol):
    async def read(
        self,
        *,
        task_id: str,
        agent_run_id: str,
    ) -> H5FastReviewSnapshot | None: ...


class H5RuntimePort(Protocol):
    runtime_settings: H5TaskRuntimeSettings

    async def start(self, command: RunH5WorkflowCommand) -> H5ExplorationResult: ...

    async def run_one_turn(
        self,
        *,
        task_id: str,
        agent_run_id: str,
    ) -> H5ExplorationTurnOutcome: ...

    async def next_exploration_claim_at(self, *, task_id: str) -> datetime | None: ...

    async def prepare_close(self, *, task_id: str) -> None: ...

    async def close(self, *, task_id: str, session_id: str) -> None: ...


class H5CompletionAwareExplorer:
    """将平台根 Frontier 完成信号确定性映射为局部 completed。"""

    def __init__(
        self,
        *,
        delegate: H5ExplorerAgent,
        h5_visual_coordinate_mode: Literal["off", "shadow", "enforced"] = "off",
    ) -> None:
        if h5_visual_coordinate_mode not in {"off", "shadow", "enforced"}:
            raise ValueError("h5_visual_coordinate_mode 必须为 off、shadow 或 enforced")
        self._delegate = delegate
        self._h5_visual_coordinate_mode = h5_visual_coordinate_mode

    async def decide_next(
        self,
        work: ExplorationWork,
    ) -> ActionIntent | ExplorationTerminal:
        # 无关闭控件的弹窗在失败重观察路径上跨后继工作累计达上限：放弃该页，
        # 不再重观察（否则每次另开后继、计数归零，无限卡死 —— 91PORN 线上实锤）。
        # 同签名与签名无关（倒计时/轮播弹窗签名逐轮漂移）两个维度任一达限即止损。
        dismissal_stalled = popup_dismissal_reobserve_exhausted(
            work.frontier.observation.metadata.get("popup_dismissal_reobserve_state")
        )
        if dismissal_stalled is not None:
            stalled_count, stalled_limit = dismissal_stalled
            return ExplorationTerminal(
                work_id=work.work_id,
                task_id=work.task_id,
                status=ExplorationTerminalStatus.BOUNDED,
                reason=f"popup_dismissal_stalled:{stalled_count}/{stalled_limit}",
                outcome=ExplorationTerminalStatus.BOUNDED.value,
                reason_code=H5_POPUP_DISMISSAL_STALLED_REASON_CODE,
            )
        if (
            work.frontier.observation.metadata.get("capability_projection_reobserve_exhausted")
            is True
        ):
            return ExplorationTerminal(
                work_id=work.work_id,
                task_id=work.task_id,
                status=ExplorationTerminalStatus.BOUNDED,
                reason="capability_projection_reobserve_limit_exceeded",
                outcome=ExplorationTerminalStatus.BOUNDED.value,
                reason_code=(H5_CAPABILITY_PROJECTION_REOBSERVE_EXHAUSTED_REASON_CODE),
            )
        snapshot: H5TaskFrontierSnapshot | None = None
        payload = work.frontier.observation.metadata.get(H5_TASK_FRONTIER_METADATA_KEY)
        if isinstance(payload, Mapping):
            snapshot = H5TaskFrontierSnapshot.from_dict(payload)
        if snapshot is not None:
            popup_loop = evaluate_h5_business_page_popup_loop(
                snapshot=snapshot,
                location=work.frontier.observation.location,
                page_type=work.frontier.observation.page_type,
                current_candidates=work.frontier.candidates,
            )
            if popup_loop is not None and popup_loop.limit_reached:
                return ExplorationTerminal(
                    work_id=work.work_id,
                    task_id=work.task_id,
                    status=ExplorationTerminalStatus.BOUNDED,
                    reason=(
                        f"{popup_loop.reason_code}:{popup_loop.completed_count}/{popup_loop.limit}"
                    ),
                    outcome=ExplorationTerminalStatus.BOUNDED.value,
                    reason_code=popup_loop.reason_code,
                )
        if not work.frontier.candidates:
            if (
                work.frontier.observation.overlay_signature is not None
                or work.frontier.observation.metadata.get("obstruction_blocking") is True
            ):
                return ExplorationTerminal(
                    work_id=work.work_id,
                    task_id=work.task_id,
                    status=ExplorationTerminalStatus.BLOCKED,
                    reason=(
                        "H5 Frontier 没有可执行候选，但当前页面仍存在活动遮挡，无法安全继续探索。"
                    ),
                    outcome=ExplorationTerminalStatus.BLOCKED.value,
                    reason_code=H5_FRONTIER_EMPTY_WITH_ACTIVE_OBSTRUCTION_REASON_CODE,
                )
            if (
                not h5_observation_allows_completion(work.frontier.observation)
                and work.frontier.observation.metadata.get("requires_auth") is not True
            ):
                return ExplorationTerminal(
                    work_id=work.work_id, task_id=work.task_id,
                    status=ExplorationTerminalStatus.BOUNDED,
                    reason="h5_surface_observation_incomplete",
                    outcome=ExplorationTerminalStatus.BOUNDED.value,
                    reason_code="H5_SURFACE_OBSERVATION_INCOMPLETE",
                )
            if snapshot is not None and _h5_page_frontier_cleanly_exhausted(
                work=work,
                snapshot=snapshot,
            ):
                return ExplorationTerminal(
                    work_id=work.work_id,
                    task_id=work.task_id,
                    status=ExplorationTerminalStatus.COMPLETED,
                    reason="H5 Task Frontier 已安全耗尽。",
                    outcome=ExplorationTerminalStatus.COMPLETED.value,
                    reason_code=H5_FRONTIER_CLEANLY_EXHAUSTED_REASON_CODE,
                )
            if (
                snapshot is not None
                and not any(
                    entry.status is H5FrontierEntryStatus.SELECTED for entry in snapshot.entries
                )
                and snapshot.pending_count == 0
                and snapshot.blocked_count == 0
                and snapshot.failed_count > 0
            ):
                return ExplorationTerminal(
                    work_id=work.work_id,
                    task_id=work.task_id,
                    status=ExplorationTerminalStatus.BOUNDED,
                    reason="task_frontier_objective_not_satisfied:frontier_failed",
                )
            if snapshot is not None and _h5_frontier_page_budget_exhausted(snapshot):
                return ExplorationTerminal(
                    work_id=work.work_id,
                    task_id=work.task_id,
                    status=ExplorationTerminalStatus.BOUNDED,
                    reason="H5 页面采样已达到 max_pages 上限。",
                    outcome=ExplorationTerminalStatus.BOUNDED.value,
                    reason_code=H5_FRONTIER_PAGE_BUDGET_EXHAUSTED_REASON_CODE,
                )
            if snapshot is not None and snapshot.blocked_count > 0:
                authentication_blocked = any(
                    entry.status is H5FrontierEntryStatus.BLOCKED
                    and entry.last_failure_reason == "authentication_required"
                    for entry in snapshot.entries
                )
                reason = (
                    "task_frontier_blocked:authentication_required"
                    if authentication_blocked
                    else "task_frontier_blocked"
                )
                return ExplorationTerminal(
                    work_id=work.work_id,
                    task_id=work.task_id,
                    status=ExplorationTerminalStatus.BLOCKED,
                    reason=reason,
                )
            if snapshot is not None and snapshot.pending_count > 0:
                return ExplorationTerminal(
                    work_id=work.work_id,
                    task_id=work.task_id,
                    status=ExplorationTerminalStatus.BOUNDED,
                    reason="task_frontier_pending_without_executable_candidate",
                )
        if work.frontier.observation.metadata.get(
            "requires_auth"
        ) is True and not _has_unblocked_h5_candidate(work):
            return ExplorationTerminal(
                work_id=work.work_id,
                task_id=work.task_id,
                status=ExplorationTerminalStatus.BLOCKED,
                reason="requires_auth：页面没有游客、关闭或其他安全候选。",
            )
        if (
            not work.frontier.candidates
            and work.frontier.observation.metadata.get("objective_satisfied") is True
        ):
            return ExplorationTerminal(
                work_id=work.work_id,
                task_id=work.task_id,
                status=ExplorationTerminalStatus.COMPLETED,
                reason="根层 Frontier 已完成且没有剩余安全候选。",
                outcome=ExplorationTerminalStatus.COMPLETED.value,
                reason_code=H5_FRONTIER_CLEANLY_EXHAUSTED_REASON_CODE,
            )
        try:
            decision = await self._delegate.decide_next(work)
        except (ExplorerAgentOutputError, ExplorerModelUnavailableError) as exc:
            fallback = _platform_selected_h5_action_intent(
                work,
                h5_visual_coordinate_mode=self._h5_visual_coordinate_mode,
                fallback_reason_code=exc.error_code,
            )
            if fallback is None:
                raise
            decision = fallback
        if (
            isinstance(decision, ExplorationTerminal)
            and decision.status is ExplorationTerminalStatus.COMPLETED
            and (
                not h5_observation_allows_completion(work.frontier.observation)
                or (
                    work.frontier.observation.metadata.get("objective_satisfied") is not True
                    and not (
                        snapshot is not None
                        and _h5_page_frontier_cleanly_exhausted(work=work, snapshot=snapshot)
                    )
                )
            )
        ):
            return ExplorationTerminal(
                work_id=work.work_id,
                task_id=work.task_id,
                status=ExplorationTerminalStatus.BOUNDED,
                reason="task_frontier_objective_not_satisfied",
            )
        if isinstance(decision, ActionIntent):
            decision = replace(
                decision,
                parameters={
                    **dict(decision.parameters),
                    **h5_viewport_parameters(),
                },
            )
            _validate_h5_selected_candidate_binding(
                work=work,
                decision=decision,
                h5_visual_coordinate_mode=self._h5_visual_coordinate_mode,
            )
        return decision


def _h5_frontier_page_budget_exhausted(snapshot: H5TaskFrontierSnapshot) -> bool:
    blocked_entries = tuple(
        entry
        for entry in snapshot.entries
        if entry.status is H5FrontierEntryStatus.BLOCKED
    )
    return (
        bool(blocked_entries)
        and snapshot.pending_count == 0
        and snapshot.failed_count == 0
        and not any(
            entry.status is H5FrontierEntryStatus.SELECTED for entry in snapshot.entries
        )
        and all(
            entry.last_failure_reason == "max_pages_reached"
            for entry in blocked_entries
        )
    )


class H5PlatformSelectedExplorer:
    """执行平台已验签的唯一 H5 Frontier 候选，不重复请求模型选路。"""

    def __init__(
        self,
        *,
        h5_visual_coordinate_mode: Literal["off", "shadow", "enforced"] = "off",
    ) -> None:
        if h5_visual_coordinate_mode not in {"off", "shadow", "enforced"}:
            raise ValueError("h5_visual_coordinate_mode 必须为 off、shadow 或 enforced")
        self._h5_visual_coordinate_mode = h5_visual_coordinate_mode

    async def decide_next(
        self,
        work: ExplorationWork,
    ) -> ActionIntent | ExplorationTerminal:
        decision = _platform_selected_h5_action_intent(
            work,
            h5_visual_coordinate_mode=self._h5_visual_coordinate_mode,
        )
        if decision is None:
            raise ExplorerPrematureTerminalError("H5 Work 缺少唯一已验签的 selected 候选")
        return decision


def _h5_page_frontier_cleanly_exhausted(
    *,
    work: ExplorationWork,
    snapshot: H5TaskFrontierSnapshot,
) -> bool:
    """只判断页面探索是否干净耗尽，不读取质检覆盖或 Judge 结论。"""

    metadata = work.frontier.observation.metadata
    return bool(
        h5_observation_allows_completion(work.frontier.observation)
        and not any(entry.status is H5FrontierEntryStatus.SELECTED for entry in snapshot.entries)
        and snapshot.pending_count == 0
        and snapshot.blocked_count == 0
        and snapshot.failed_count == 0
        and metadata.get("requires_auth") is not True
        and work.frontier.observation.overlay_signature is None
    )


def _has_unblocked_h5_candidate(work: ExplorationWork) -> bool:
    blocked_ids = set(work.frontier.blocked_candidate_ids)
    return any(candidate.candidate_id not in blocked_ids for candidate in work.frontier.candidates)


def _platform_selected_h5_action_intent(
    work: ExplorationWork,
    *,
    h5_visual_coordinate_mode: Literal["off", "shadow", "enforced"] = "off",
    fallback_reason_code: str | None = None,
) -> ActionIntent | None:
    """仅把唯一已验签的 selected H5 候选确定性映射为执行意图。"""
    if work.surface_kind is not SurfaceKind.WEB or len(work.frontier.candidates) != 1:
        return None
    candidate = work.frontier.candidates[0]
    if (
        candidate.candidate_id in work.frontier.blocked_candidate_ids
        or candidate.risk_level not in {InteractionRiskLevel.LOW, InteractionRiskLevel.MEDIUM}
    ):
        return None
    payload = work.frontier.observation.metadata.get(H5_TASK_FRONTIER_METADATA_KEY)
    if not isinstance(payload, Mapping):
        return None
    snapshot = H5TaskFrontierSnapshot.from_dict(payload)
    selected = [
        entry for entry in snapshot.entries if entry.status is H5FrontierEntryStatus.SELECTED
    ]
    if (
        len(selected) != 1
        or work.frontier.observation.metadata.get("h5_task_frontier_selected_entry_id")
        != selected[0].entry_id
        or selected[0].candidate != candidate
    ):
        return None
    parameters = _h5_candidate_parameters(
        work=work,
        candidate=candidate,
        h5_visual_coordinate_mode=h5_visual_coordinate_mode,
    )
    if fallback_reason_code is not None:
        parameters = {
            **parameters,
            "explorer_decision_fallback": {
                "schema_version": "h5-explorer-decision-fallback.v1",
                "reason_code": fallback_reason_code,
            },
        }
    digest = stable_protocol_hash(
        {
            "work_id": work.work_id,
            "source_capture_id": work.source_capture_id,
            "candidate_id": candidate.candidate_id,
            "decision_source": "platform_selected_candidate_fallback",
        }
    )
    identity = f"h5-platform-fallback:{digest}"
    return ActionIntent(
        task_id=work.task_id,
        session_id=work.session_id,
        intent_id=identity,
        idempotency_key=identity,
        surface_kind=work.surface_kind,
        source_capture_id=work.source_capture_id,
        action_type=candidate.action_type,
        semantic_target=candidate.semantic_target,
        parameters=parameters,
        purpose=h5_candidate_purpose(candidate),
        risk_level=candidate.risk_level,
        capture_profile=CaptureProfilePolicy.for_surface(
            work.surface_kind,
            candidate.action_type,
        ),
        deadline=work.deadline,
    )


@dataclass(frozen=True, kw_only=True)
class H5V2Runtime:
    """H5 首轮、动作轮和 Web Session 的单一运行时边界。"""

    capture_runtime: WebCaptureRuntime
    workflow: H5ExplorationWorkflow
    turn_runner: H5ExplorationTurnRunner
    runtime_settings: H5TaskRuntimeSettings
    cancellation: H5CancellationCheckpoint | None = None
    session_lineage: object | None = None
    fast_review_probe: H5FastReviewProbe | None = None
    active_session_inspection: H5ActiveSessionInspectionDrainService | None = None

    async def start(self, command: RunH5WorkflowCommand) -> H5ExplorationResult:
        register = getattr(self.session_lineage, "register", None)
        if callable(register):
            register(command.session_id)
        return await self.workflow.run(command)

    async def run_one_turn(
        self,
        *,
        task_id: str,
        agent_run_id: str,
    ) -> H5ExplorationTurnOutcome:
        return await self.turn_runner.run_one(
            task_id=task_id,
            agent_run_id=agent_run_id,
        )

    async def next_exploration_claim_at(self, *, task_id: str) -> datetime | None:
        return await self.turn_runner.next_claim_at(task_id=task_id)

    async def read_fast_review_snapshot(
        self,
        *,
        task_id: str,
        agent_run_id: str,
    ) -> H5FastReviewSnapshot | None:
        if self.fast_review_probe is None:
            return None
        return await self.fast_review_probe.read(
            task_id=task_id,
            agent_run_id=agent_run_id,
        )

    async def drain_active_session_inspection(
        self,
        *,
        task_id: str,
        agent_run_id: str,
        deadline: datetime,
    ) -> object:
        if self.active_session_inspection is None:
            raise RuntimeError("H5 V2 Runtime 缺少关闭前活会话规则排空器")
        return await self.active_session_inspection.drain_before_close(
            task_id=task_id,
            agent_run_id=agent_run_id,
            deadline=deadline,
        )

    async def inspect_current_h5_surface(
        self,
        *,
        task_id: str,
        agent_run_id: str,
        deadline: datetime,
    ) -> object:
        if self.active_session_inspection is None:
            raise RuntimeError("H5 V2 Runtime 缺少活会话规则检查点")
        return await self.active_session_inspection.run_current_surface_checkpoint(
            task_id=task_id,
            agent_run_id=agent_run_id,
            deadline=deadline,
        )

    async def prepare_close(self, *, task_id: str) -> None:
        """先冻结探索侧写入，再建立活会话规则排空屏障。"""

        # Run 收场时残余的未终态探索工作（如基础设施重试耗尽停在 retry_wait 的
        # 工作项）必须先有界收束。冻结完成后，规则排空器才能观察到稳定的最终
        # Capture 集合；正常 completed 收场无残余，此步为幂等空操作。
        await self.turn_runner.cancel_nonterminal(
            task_id=task_id,
            reason="H5 探索收场：未终态探索工作有界收束",
        )
        reconcile = getattr(self.turn_runner, "reconcile_terminal_frontier", None)
        if callable(reconcile):
            await reconcile(task_id=task_id)

    async def close(self, *, task_id: str, session_id: str) -> None:
        sessions = (
            tuple(getattr(self.session_lineage, "session_ids", ()))
            if self.session_lineage is not None
            else (session_id,)
        )
        failures: list[BaseException] = []
        for current_session_id in reversed(sessions or (session_id,)):
            try:
                repository = self.capture_runtime.surface_sessions
                snapshot = (
                    await repository.get(session_id=current_session_id)
                    if repository is not None else None
                )
                if snapshot is not None and snapshot.task_id != task_id:
                    raise RuntimeError("H5 关闭谱系包含其他任务的 Session")
                if snapshot is not None and snapshot.status.value in {"lost", "closed"}:
                    try:
                        await self.capture_runtime.close_browser_session(current_session_id)
                    finally:
                        await self.capture_runtime.session_command_gate.release_session(
                            task_id=task_id, session_id=current_session_id,
                        )
                else:
                    await self.capture_runtime.close_h5_exploration_session(
                        task_id=task_id, session_id=current_session_id,
                    )
            except (Exception, asyncio.CancelledError) as error:
                error.add_note(f"H5 关闭会话: {current_session_id}")
                failures.append(error)
        if failures:
            # 一个 recovery 会话失败不能阻止主会话和其他恢复会话收场。
            first = next(
                (error for error in failures if isinstance(error, asyncio.CancelledError)),
                failures[0],
            )
            for other in failures:
                if other is not first:
                    first.add_note(f"其他 H5 会话关闭失败: {type(other).__name__}: {other}")
            raise first


class H5SessionLineageTracker:
    """单个 Run 内登记 canonical 与 recovery Session，供最终关闭和跨 worker 接管。"""

    def __init__(self) -> None:
        self._session_ids: list[str] = []

    @property
    def session_ids(self) -> tuple[str, ...]:
        return tuple(self._session_ids)

    def register(self, session_id: str) -> None:
        if session_id not in self._session_ids:
            self._session_ids.append(session_id)


class PersistingH5PageDetector:
    """以持久化 Page Capture 为输入，并将识别事实写回当前 Run。"""

    def __init__(
        self,
        *,
        h5_visual_coordinate_mode: Literal["off", "shadow", "enforced"] = "off",
        interaction_pattern_knowledge: H5InteractionPatternKnowledgePort | None = None,
    ) -> None:
        if h5_visual_coordinate_mode not in {"off", "shadow", "enforced"}:
            raise ValueError("h5_visual_coordinate_mode 必须为 off、shadow 或 enforced")
        self._h5_visual_coordinate_mode = h5_visual_coordinate_mode
        self._interaction_pattern_knowledge = interaction_pattern_knowledge
        self._capture_detections: dict[tuple[str, str, CaptureRef], dict[str, Any]] = {}

    async def detect(
        self,
        *,
        capture: CaptureRef,
        task_id: str,
        agent_run_id: str,
    ) -> dict[str, Any]:
        key = (task_id, agent_run_id, capture)
        cached = self._capture_detections.get(key)
        if cached is not None:
            return deepcopy(cached)
        detection_options: dict[str, object] = {
            "page_capture_id": capture.capture_id,
            "visual_coordinate_mode": self._h5_visual_coordinate_mode,
            "recognition_guide_enforced": True,
        }
        if self._interaction_pattern_knowledge is not None:
            detection_options["interaction_pattern_knowledge"] = (
                self._interaction_pattern_knowledge
            )
        result = await detect_page_for_agent(
            **detection_options,
        )
        persisted = await persist_page_detection_for_current_run(
            result=result,
            finalize_evidence_readiness=False,
        )
        # 只复用同一不可变 Capture；新帧必须重新识别，不按 URL 或页型借用结果。
        if len(self._capture_detections) >= 64:
            self._capture_detections.pop(next(iter(self._capture_detections)))
        self._capture_detections[key] = deepcopy(persisted)
        return deepcopy(persisted)


class PostgresH5FastReviewProbe:
    """读取 H5 早停所需的覆盖与证据库存事实。"""

    def __init__(self, *, pool: Any | None = None, database_url: str | None = None) -> None:
        self._pool = pool
        self._database_url = database_url

    async def read(
        self,
        *,
        task_id: str,
        agent_run_id: str,
    ) -> H5FastReviewSnapshot | None:
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                SELECT
                    coverage.revision AS coverage_revision,
                    coverage.assessment_scope_ready,
                    coverage.completion_ready,
                    coverage.target_coverage_ready,
                    COALESCE(coverage.covered_page_type_count, 0)::integer
                        AS covered_page_type_count,
                    COALESCE(coverage.blocking_scope_count, 0)::integer
                        AS blocking_scope_count,
                    COALESCE(inventory.item_count, 0)::integer AS inventory_item_count,
                    COALESCE(work.active_work_count, 0)::integer AS active_work_count,
                    COALESCE(work.claimable_work_count, 0)::integer
                        AS claimable_work_count,
                    COALESCE(work.retry_waiting_work_count, 0)::integer
                        AS retry_waiting_work_count,
                    COALESCE(work.exhausted_work_count, 0)::integer
                        AS exhausted_work_count,
                    COALESCE(work.settled_work_count, 0)::integer
                        AS settled_work_count
                FROM qc_task AS task
                LEFT JOIN LATERAL (
                    SELECT
                        snapshot.revision,
                        snapshot.assessment_scope_ready,
                        snapshot.completion_ready,
                        (
                            snapshot.assessment_scope_ready
                            AND jsonb_array_length(
                                COALESCE(snapshot.required_page_types, '[]'::jsonb)
                            ) > 0
                            AND jsonb_array_length(
                                COALESCE(snapshot.covered_page_types, '[]'::jsonb)
                            ) > 0
                            AND jsonb_array_length(
                                COALESCE(snapshot.missing_page_types, '[]'::jsonb)
                            ) = 0
                        ) AS target_coverage_ready,
                        jsonb_array_length(
                            COALESCE(snapshot.covered_page_types, '[]'::jsonb)
                        )::integer AS covered_page_type_count,
                        count(scope.*) FILTER (
                            WHERE scope.value->>'assessment_status'
                                IN (
                                    'requires_auth',
                                    'blocked_by_safety',
                                    'not_found',
                                    'evidence_insufficient',
                                    'incomplete'
                                )
                        )::integer AS blocking_scope_count
                    FROM qc_h5_coverage_snapshot AS snapshot
                    LEFT JOIN LATERAL jsonb_array_elements(
                        COALESCE(snapshot.coverage_scopes, '[]'::jsonb)
                    ) AS scope(value) ON TRUE
                    WHERE snapshot.task_id = task.id
                      AND snapshot.agent_run_id = $2::uuid
                      AND snapshot.finalization_epoch = task.finalization_epoch
                    GROUP BY
                        snapshot.coverage_snapshot_id,
                        snapshot.revision,
                        snapshot.assessment_scope_ready,
                        snapshot.completion_ready,
                        snapshot.required_page_types,
                        snapshot.covered_page_types,
                        snapshot.missing_page_types,
                        snapshot.created_at
                    ORDER BY snapshot.revision DESC, snapshot.created_at DESC
                    LIMIT 1
                ) AS coverage ON TRUE
                LEFT JOIN LATERAL (
                    SELECT count(*)::integer AS item_count
                    FROM qc_h5_evidence_inventory_item AS inventory
                    WHERE inventory.task_id = task.id
                      AND inventory.agent_run_id = $2::uuid
                ) AS inventory ON TRUE
                LEFT JOIN LATERAL (
                    SELECT
                        count(*) FILTER (
                            WHERE work_item.work_type = 'exploration'
                              AND work_item.status IN ('leased', 'running')
                              AND work_item.lease_expires_at > now()
                              AND work_item.attempt_count < work_item.max_attempts
                              AND work_item.deadline > now()
                        )::integer AS active_work_count,
                        count(*) FILTER (
                            WHERE work_item.work_type = 'exploration'
                              AND work_item.attempt_count < work_item.max_attempts
                              AND work_item.deadline > now()
                              AND (
                                (
                                    work_item.status IN ('pending', 'retry_wait')
                                    AND work_item.next_attempt_at <= now()
                                )
                                OR (
                                    work_item.status IN ('leased', 'running')
                                    AND work_item.lease_expires_at <= now()
                                )
                              )
                        )::integer AS claimable_work_count,
                        count(*) FILTER (
                            WHERE work_item.work_type = 'exploration'
                              AND work_item.status IN ('pending', 'retry_wait')
                              AND work_item.next_attempt_at > now()
                              AND work_item.attempt_count < work_item.max_attempts
                              AND work_item.deadline > now()
                        )::integer AS retry_waiting_work_count,
                        count(*) FILTER (
                            WHERE work_item.work_type = 'exploration'
                              AND work_item.status IN (
                                'pending', 'retry_wait', 'leased', 'running'
                              )
                              AND (
                                work_item.attempt_count >= work_item.max_attempts
                                OR work_item.deadline <= now()
                              )
                        )::integer AS exhausted_work_count,
                        count(*) FILTER (
                            WHERE work_item.work_type = 'exploration'
                              AND work_item.status IN (
                                'succeeded',
                                'bounded',
                                'budget_exhausted',
                                'blocked',
                                'no_progress',
                                'failed',
                                'cancelled',
                                'canceled',
                                'superseded'
                              )
                        )::integer AS settled_work_count
                    FROM qc_v2_work_item AS work_item
                    WHERE work_item.task_id = task.id
                      AND work_item.finalization_epoch = task.finalization_epoch
                ) AS work ON TRUE
                WHERE task.id = $1::uuid
                  AND task.target_platform IN ('h5', 'web')
                """,
                task_id,
                agent_run_id,
            )
        if row is None or row["coverage_revision"] is None:
            return None
        return H5FastReviewSnapshot(
            coverage_revision=int(row["coverage_revision"]),
            assessment_scope_ready=bool(row["assessment_scope_ready"]),
            completion_ready=bool(row["completion_ready"]),
            target_coverage_ready=bool(row["target_coverage_ready"]),
            inventory_item_count=int(row["inventory_item_count"]),
            covered_page_type_count=int(row["covered_page_type_count"]),
            blocking_scope_count=int(row["blocking_scope_count"]),
            active_work_count=int(row["active_work_count"]),
            claimable_work_count=int(row["claimable_work_count"]),
            retry_waiting_work_count=int(row["retry_waiting_work_count"]),
            exhausted_work_count=int(row["exhausted_work_count"]),
            settled_work_count=int(row["settled_work_count"]),
        )

    async def _get_pool(self) -> Any:
        if self._pool is not None:
            return self._pool
        return await get_postgres_pool(self._database_url)


class H5SourceObservationMoved(ValueError):
    """同执行域内现场已推进，重领后重新观察，不终结整条探索。"""

    error_code = "H5_SOURCE_OBSERVATION_MOVED"
    retryable = True


class H5CoordinatorFrontierRecovery:
    """以 CaptureCoordinator 恢复 H5 sibling 来源并完成 rebaseline。"""

    def __init__(
        self,
        *,
        capture_coordinator: object,
        detector: PersistingH5PageDetector,
        assessment_repository: H5PageTypeAssessmentPersistencePort,
        cancellation: H5CancellationCheckpoint | None = None,
        now: Callable[[], datetime] | None = None,
        session_lineage: H5SessionLineageTracker | None = None,
        h5_visual_coordinate_mode: Literal["off", "shadow", "enforced"] = "off",
        source_task_id: str | None = None,
        surface_sessions: object | None = None,
        captures: object | None = None,
        page_graph: object | None = None,
    ) -> None:
        if assessment_repository is None:
            raise TypeError("assessment_repository 必须显式提供 H5 评估持久化端口")
        if h5_visual_coordinate_mode not in {"off", "shadow", "enforced"}:
            raise ValueError("h5_visual_coordinate_mode 必须为 off、shadow 或 enforced")
        if source_task_id is not None and (
            not isinstance(source_task_id, str)
            or not source_task_id.strip()
            or source_task_id != source_task_id.strip()
        ):
            raise ValueError("source_task_id 必须为无首尾空白的非空字符串")
        if h5_visual_coordinate_mode == "enforced" and source_task_id is None:
            raise ValueError("enforced 视觉来源恢复必须显式绑定 source_task_id")
        self._capture_coordinator = capture_coordinator
        self._detector = detector
        self._assessment_repository = assessment_repository
        self._cancellation = cancellation or H5CancellationCheckpoint()
        self._now = now or (lambda: datetime.now(UTC))
        self._session_lineage = session_lineage
        self._h5_visual_coordinate_mode = h5_visual_coordinate_mode
        self._source_task_id = source_task_id
        self._surface_sessions = surface_sessions
        self._captures = captures
        self._page_graph = page_graph

    async def synchronize(self, *, work: ExplorationWork, agent_run_id: str) -> ExplorationWork:
        """补采可推进会话现场，领取后必须重新绑定持久化的最新 Capture。"""
        if self._surface_sessions is None or self._captures is None:
            return work
        session = await self._surface_sessions.get(session_id=work.session_id)
        if (
            session is None or session.task_id != work.task_id
            or session.surface_kind is not SurfaceKind.WEB
            or session.status.value != "active"
            or not (
                (work.session_id == f"h5:{agent_run_id}"
                 and session.session_owner == H5_SURFACE_SESSION_OWNER)
                or (work.session_id.startswith(f"h5:{agent_run_id}:recovery:")
                    and session.session_owner == f"h5-browser-recovery:{agent_run_id}")
            )
        ):
            raise ValueError("H5 主探索现场不属于当前 Task/Run 或已关闭")
        if session.current_capture_id == work.source_capture_id:
            return work
        persisted = await self._captures.get_verified_observation_capture(
            capture_id=session.current_capture_id,
        )
        capture = getattr(persisted, "capture", None)
        if (
            not isinstance(capture, CaptureRef) or capture.task_id != work.task_id
            or capture.session_id != work.session_id or capture.surface_kind is not SurfaceKind.WEB
            or capture.capture_id != session.current_capture_id
        ):
            raise ValueError("H5 最新现场缺少同任务同会话的可信 Capture")
        detection = await self._detector.detect(
            capture=capture, task_id=work.task_id, agent_run_id=agent_run_id,
        )
        latest = await self._surface_sessions.get(session_id=work.session_id)
        if latest is None or any(
            getattr(latest, field) != getattr(session, field)
            for field in (
                "task_id", "session_owner", "status", "finalization_epoch", "surface_kind",
            )
        ):
            raise ValueError("H5 现场同步期间会话已变化，禁止执行旧能力")
        if (
            latest.current_capture_id != session.current_capture_id
            or latest.command_sequence != session.command_sequence
        ):
            raise H5SourceObservationMoved("H5 现场同步期间同会话已有新动作，需要重新观察")
        snapshot = H5TaskFrontierSnapshot.from_dict(
            work.frontier.observation.metadata[H5_TASK_FRONTIER_METADATA_KEY]
        )
        selected = next((item for item in snapshot.entries
                         if item.status is H5FrontierEntryStatus.SELECTED), None)
        frontier = build_h5_frontier(
            detection_result=detection, observed_at=self._now(),
            navigation_depth=selected.navigation_depth if selected else 0,
            lineage_metadata=dict(work.frontier.observation.metadata),
            logical_surface_effective=_logical_surface_enabled_for_work(work),
            logical_targets_effective=_logical_targets_enabled_for_work(work),
            h5_visual_coordinate_mode=self._h5_visual_coordinate_mode,
            source_task_id=work.task_id,
        )
        if frontier.observation.capture_id != capture.capture_id:
            raise ValueError("H5 最新现场识别返回了其他 Capture")
        # 补采首次到达新业务页时先采一次普通观察，不能先恢复旧来源丢掉现场。
        # 原补采帧仍维持 evidence_only，由正常 after_action 完成发布，防止递归补采。
        location = urlsplit(frontier.observation.location)
        assessment = adapt_h5_page_detection_to_assessment(
            task_id=work.task_id, agent_run_id=agent_run_id, detection_result=detection,
            assessed_at=self._now(),
        )
        assessed = assessment.page_type_assessment or {}
        if (
            capture.role == "probe_result_replay"
            and frontier.observation.page_type not in snapshot.covered_page_types
            and h5_observation_allows_completion(frontier.observation)
            and assessed.get("status") == "covered"
            and detection.get("rule_matching_allowed") is True
            and location.hostname in work.boundaries.allowed_hosts
            and location.scheme in {"http", "https"}
            and (
                not work.boundaries.allowed_route_prefixes
                or any(location.path.startswith(prefix)
                       for prefix in work.boundaries.allowed_route_prefixes)
            )
            and InteractionOperation.OBSERVE in work.boundaries.allowed_operations
            and self._now() < work.deadline
            and work.budget.remaining_actions > 0
            and work.budget.remaining_duration_ms > 0
        ):
            handoff = CandidateAction(
                candidate_id="h5-handoff:" + stable_protocol_hash(
                    {"capture_id": capture.capture_id}
                ),
                semantic_target="观察补采发现的新业务页", action_type=InteractionOperation.OBSERVE,
                risk_level=InteractionRiskLevel.LOW, exploration_value=90,
                navigation_depth=selected.navigation_depth if selected else 0,
                expected_surface_kind=SurfaceKind.WEB,
                metadata={
                    "expected_next_page_type": frontier.observation.page_type,
                    "source_page_type": frontier.observation.page_type,
                    "purpose": "在当前现场采集正常探索证据，保留原补证来源与待探索目标。",
                    "action_parameters": {"target_mode": "h5_recapture_business_handoff"},
                },
            )
            handoff_snapshot = snapshot.merge(
                source_capture_id=capture.capture_id,
                source_state_version=frontier.observation.state_version,
                source_location=frontier.observation.location,
                navigation_depth=handoff.navigation_depth,
                candidates=(handoff, *frontier.candidates),
                max_depth=work.budget.max_depth, now=frontier.observation.observed_at,
            )
            handoff_entry = next((item for item in handoff_snapshot.pending_entries
                                 if item.candidate.candidate_id == handoff.candidate_id), None)
            if handoff_entry is not None:
                if selected is not None:
                    handoff_snapshot = handoff_snapshot.defer_selected(selected.entry_id)
                snapshot = handoff_snapshot.select_entry(handoff_entry.entry_id)
                metadata = dict(frontier.observation.metadata)
                metadata.pop("h5_frontier_source_recovery_pending", None)
                frontier = _with_task_frontier(
                    frontier=replace(
                        frontier, observation=replace(frontier.observation, metadata=metadata)
                    ),
                    snapshot=snapshot,
                )
                return replace(work, source_capture_id=capture.capture_id,
                               frontier_snapshot_id=frontier.snapshot_id, frontier=frontier)
        if selected is not None:
            try:
                snapshot = _rebind_selected_task_entry_to_fresh_frontier(
                    snapshot=snapshot, frontier=frontier,
                )
                # 同页现场同步不能只更新 selected，须保留新帧中其他实际入口。
                snapshot = snapshot.merge(
                    source_capture_id=frontier.observation.capture_id,
                    source_state_version=frontier.observation.state_version,
                    source_location=frontier.observation.location,
                    navigation_depth=selected.navigation_depth,
                    candidates=frontier.candidates,
                    max_depth=work.budget.max_depth,
                    now=frontier.observation.observed_at,
                )
                metadata = dict(frontier.observation.metadata)
                metadata.pop("h5_frontier_source_recovery_pending", None)
                frontier = replace(frontier, observation=replace(
                    frontier.observation, metadata=metadata,
                ))
            except H5FreshCandidateRebindRejected:
                # 同页旧目标消失不应丢弃新入口；跨页或逻辑身份不同仍禁止合并。
                old_identity = selected.candidate.metadata.get("source_identity_fingerprint")
                if (
                    frontier.observation.location == selected.source_location
                    and frontier.observation.page_type
                    and (frontier.observation.page_type == (
                            selected.candidate.metadata.get("source_page_type")
                            or work.frontier.observation.page_type
                        ) or h5_frontier_type_drift_is_proven(entry=selected, frontier=frontier))
                    and (
                        selected.candidate.metadata.get("logical_target_required") is not True
                        or (old_identity and old_identity == frontier.observation.metadata.get(
                            "logical_surface_identity_fingerprint"
                        ))
                    )
                ):
                    snapshot = snapshot.merge(
                        source_capture_id=frontier.observation.capture_id,
                        source_state_version=frontier.observation.state_version,
                        source_location=frontier.observation.location,
                        navigation_depth=selected.navigation_depth,
                        candidates=frontier.candidates,
                        max_depth=work.budget.max_depth,
                        now=frontier.observation.observed_at,
                    )
                    parameters = selected.candidate.metadata.get("action_parameters")
                    if (
                        old_identity
                        and isinstance(parameters, Mapping)
                        and parameters.get("target_mode") == "popup_dismissal"
                        and selected.candidate.metadata.get("source_page_state") == "popup"
                        and frontier.observation.page_state == "stable"
                        and frontier.observation.overlay_signature is None
                        and frontier.observation.metadata.get(
                            "logical_transition_proof"
                        ) == "trusted"
                        and (
                            frontier.observation.metadata.get(
                                "obstruction_snapshot_evidence_status"
                            ) == "ready"
                            # 正文截断不等于独立弹层普查缺失；仅接受完整的零弹层事实。
                            or (
                                frontier.observation.metadata.get(
                                    "obstruction_modal_census_complete"
                                ) is True
                                and type(frontier.observation.metadata.get(
                                    "obstruction_modal_census_count"
                                )) is int
                                and frontier.observation.metadata.get(
                                    "obstruction_modal_census_count"
                                ) == 0
                            )
                        )
                        and frontier.observation.metadata.get("obstruction_blocking") is False
                        and frontier.observation.metadata.get(
                            "logical_surface_identity_fingerprint"
                        ) == old_identity
                    ):
                        # 补采已证明旧遮挡消失，不能重开地址去复活它；改选新帧入口。
                        snapshot = snapshot.mark_skipped(
                            selected.entry_id, reason="popup_obstruction_absent_on_stable_page",
                        )
                        snapshot = _select_next_task_entry(
                            snapshot, active_state_version=frontier.observation.state_version,
                            active_source_location=frontier.observation.location,
                            active_page_type=frontier.observation.page_type,
                            active_page_state=frontier.observation.page_state,
                            active_observed_at=frontier.observation.observed_at,
                        )
                        metadata = dict(frontier.observation.metadata)
                        metadata.pop("h5_frontier_source_recovery_pending", None)
                        frontier = _with_task_frontier(frontier=replace(
                            frontier, observation=replace(frontier.observation, metadata=metadata),
                        ), snapshot=snapshot)
                        return replace(work, source_capture_id=capture.capture_id,
                                       frontier_snapshot_id=frontier.snapshot_id, frontier=frontier)
                # 不能在补采后的另一页面点击旧入口；保留条目，走已有来源恢复。
                frontier = replace(frontier, observation=replace(
                    frontier.observation, metadata={
                        **frontier.observation.metadata,
                        "h5_frontier_source_recovery_pending": True,
                    },
                ))
        frontier = _with_task_frontier(frontier=frontier, snapshot=snapshot)
        return replace(work, source_capture_id=capture.capture_id,
                       frontier_snapshot_id=frontier.snapshot_id, frontier=frontier)

    async def restore(
        self,
        *,
        work: ExplorationWork,
        agent_run_id: str,
        entry: object,
    ) -> H5FrontierRecoveryWorkResult:
        from services.api.app.domain.exploration.h5_frontier import H5TaskFrontierEntry

        if not isinstance(entry, H5TaskFrontierEntry):
            raise TypeError("entry 必须为 H5TaskFrontierEntry")
        if self._source_task_id is not None and self._source_task_id != work.task_id:
            raise ValueError("H5 来源恢复的 source_task_id 与 Work 不一致")
        snapshot = H5TaskFrontierSnapshot.from_dict(
            work.frontier.observation.metadata[H5_TASK_FRONTIER_METADATA_KEY]
        )
        selected_entries = tuple(
            item for item in snapshot.entries if item.status is H5FrontierEntryStatus.SELECTED
        )
        if len(selected_entries) != 1:
            raise ValueError("H5 来源恢复必须绑定唯一 selected Frontier 条目")
        selected_entry = selected_entries[0]
        if selected_entry.entry_id != entry.entry_id:
            raise ValueError("H5 来源恢复 entry 与 Work selected Frontier 条目不一致")
        selected_action_parameters = selected_entry.candidate.metadata.get("action_parameters")
        if self._h5_visual_coordinate_mode != "enforced" and _has_visual_navigation_provenance(
            candidate=selected_entry.candidate,
            action_parameters=selected_action_parameters,
        ):
            raise ValueError("H5 Frontier 视觉导航能力无效或未处于 enforced 模式")
        logical_recovery_target = _trusted_logical_recovery_candidate(selected_entry.candidate)
        browser = _CoordinatorRecoveryBrowser(
            work=work,
            agent_run_id=agent_run_id,
            capture_coordinator=self._capture_coordinator,
            detector=self._detector,
            assessment_repository=self._assessment_repository,
            cancellation=self._cancellation,
            now=self._now,
            source_state_version=selected_entry.source_state_version,
            surface_sessions=self._surface_sessions,
            page_graph=self._page_graph,
            h5_visual_coordinate_mode=self._h5_visual_coordinate_mode,
        )
        result = await restore_h5_frontier_source(
            entry=selected_entry,
            current_location=work.frontier.observation.location,
            browser=browser,
            deadline=work.deadline,
            now=self._now,
        )
        work = replace(work, budget=browser.remaining_budget)
        if not result.restored:
            logger.warning(
                "h5_frontier_source_restore_failed task_id=%s run_id=%s work_id=%s "
                "entry_id=%s attempt=%s route=%s reason=%s cause=%s",
                work.task_id, agent_run_id, work.work_id, selected_entry.entry_id,
                result.restoration_attempt_count, result.route, result.reason_code,
                result.failure_cause_code,
            )
        updated_snapshot = apply_h5_frontier_restoration_result(
            snapshot=snapshot,
            result=result,
        )
        entry_exhausted = False
        reselected = False
        current_frontier = work.frontier
        current_capture_id = work.source_capture_id
        if isinstance(result.capture, CaptureRef) and isinstance(result.detection, dict):
            if (
                result.capture.task_id != work.task_id
                or result.capture.session_id != work.session_id
                or result.detection.get("page_capture_id") != result.capture.capture_id
            ):
                raise ValueError("H5 来源恢复现场与任务、会话或截图不一致")
            lineage_metadata = dict(work.frontier.observation.metadata)
            lineage_metadata.pop("h5_frontier_source_recovery_pending", None)
            current_frontier = build_h5_frontier(
                detection_result=result.detection,
                observed_at=self._now(),
                navigation_depth=selected_entry.navigation_depth,
                lineage_metadata=lineage_metadata,
                logical_surface_effective=(
                    _logical_surface_enabled_for_work(work) or logical_recovery_target
                ),
                logical_targets_effective=(
                    _logical_targets_enabled_for_work(work) or logical_recovery_target
                ),
                trusted_source_restoration=logical_recovery_target and result.restored,
                h5_visual_coordinate_mode=self._h5_visual_coordinate_mode,
                source_task_id=self._source_task_id or work.task_id,
            )
            current_capture_id = result.capture.capture_id
        if result.restored:
            if not isinstance(result.capture, CaptureRef) or not isinstance(result.detection, dict):
                raise RuntimeError("H5 来源恢复缺少持久化 Capture 或页面判定")
            frontier = current_frontier
            # 可信恢复已带回新帧；旧目标消失不应丢弃这帧新识别的业务入口。
            # 合并仍使用原深度与预算，旧 selected 的重新验签和失败收口不变。
            if (
                frontier.observation.page_type
                == selected_entry.candidate.metadata.get("source_page_type")
                and (
                    not logical_recovery_target
                    or frontier.observation.metadata.get("logical_surface_identity_fingerprint")
                    == selected_entry.candidate.metadata.get("source_identity_fingerprint")
                )
            ):
                updated_snapshot = updated_snapshot.merge(
                    source_capture_id=frontier.observation.capture_id,
                    source_state_version=frontier.observation.state_version,
                    source_location=frontier.observation.location,
                    navigation_depth=selected_entry.navigation_depth,
                    candidates=frontier.candidates,
                    max_depth=work.budget.max_depth,
                    now=frontier.observation.observed_at,
                )
            restored_selected_entry = next(
                item
                for item in updated_snapshot.entries
                if item.status is H5FrontierEntryStatus.SELECTED
            )
            selected_action_parameters = restored_selected_entry.candidate.metadata.get(
                "action_parameters"
            )
            visual_recovery_target = bool(
                self._h5_visual_coordinate_mode == "enforced"
                and _has_visual_navigation_provenance(
                    candidate=restored_selected_entry.candidate,
                    action_parameters=selected_action_parameters,
                )
            )
            if visual_recovery_target or logical_recovery_target:
                try:
                    updated_snapshot = _rebind_selected_task_entry_to_fresh_frontier(
                        snapshot=updated_snapshot,
                        frontier=frontier,
                    )
                except H5FreshCandidateRebindRejected as exc:
                    if result.restoration_attempt_count < 2:
                        # 动态页面首次恢复时可能仍在重绘导航。保留同一 selected
                        # sibling 和新鲜来源，下一轮重新观察后再做一次绑定；禁止
                        # 使用旧坐标，也不能因一次瞬态缺失直接丢失业务分支。
                        frontier = _with_task_frontier(
                            frontier=frontier,
                            snapshot=updated_snapshot,
                        )
                        restored_work = replace(
                            work,
                            source_capture_id=result.capture.capture_id,
                            frontier_snapshot_id=frontier.snapshot_id,
                            frontier=frontier,
                        )
                        return H5FrontierRecoveryWorkResult(
                            work=restored_work,
                            restored=False,
                            reason_code=exc.error_code,
                            reselected_sibling=False,
                        )
                    # 连续两次新鲜 Capture 都未提供唯一同业务绑定，才淘汰当前
                    # sibling；绝不退化成旧候选点击，也不连坐其他兄弟入口。
                    updated_snapshot = updated_snapshot.mark_skipped(
                        restored_selected_entry.entry_id,
                        reason=exc.error_code,
                    )
                    updated_snapshot = _select_next_task_entry(
                        updated_snapshot,
                        active_state_version=frontier.observation.state_version,
                        active_source_location=frontier.observation.location,
                        active_page_type=frontier.observation.page_type,
                        active_page_state=frontier.observation.page_state,
                        active_observed_at=frontier.observation.observed_at,
                    )
                    reselected = any(
                        item.status is H5FrontierEntryStatus.SELECTED
                        for item in updated_snapshot.entries
                    )
                    frontier = _with_task_frontier(
                        frontier=frontier,
                        snapshot=updated_snapshot,
                    )
                    restored_work = replace(
                        work,
                        source_capture_id=result.capture.capture_id,
                        frontier_snapshot_id=frontier.snapshot_id,
                        frontier=frontier,
                    )
                    return H5FrontierRecoveryWorkResult(
                        work=restored_work,
                        restored=False,
                        reason_code=exc.error_code,
                        reselected_sibling=reselected,
                    )
            frontier = _with_task_frontier(frontier=frontier, snapshot=updated_snapshot)
            restored_work = replace(
                work,
                source_capture_id=result.capture.capture_id,
                frontier_snapshot_id=frontier.snapshot_id,
                frontier=frontier,
            )
        else:
            entry_exhausted = any(
                item.entry_id == entry.entry_id and item.status is H5FrontierEntryStatus.FAILED
                for item in updated_snapshot.entries
            )
            payload_snapshot: H5TaskFrontierSnapshot = updated_snapshot
            assessment = getattr(result.assessment, "page_type_assessment", None)
            source_url = urlsplit(selected_entry.source_location)
            fresh_url = urlsplit(current_frontier.observation.location)
            usable_fresh_source = bool(
                result.reason_code == "h5_frontier_restore_state_mismatch"
                and isinstance(result.capture, CaptureRef)
                and getattr(result.assessment, "execution_status", None) == "accepted"
                and isinstance(assessment, Mapping)
                and assessment.get("status") == "covered"
                and assessment.get("page_type") == current_frontier.observation.page_type
                and result.capture.capture_id in (assessment.get("page_capture_ids") or [])
                and current_frontier.observation.page_state == "stable"
                and current_frontier.observation.overlay_signature is None
                and current_frontier.observation.metadata.get("detection_status") == "recognized"
                and current_frontier.observation.metadata.get("requires_auth") is not True
                and (source_url.scheme, source_url.hostname, source_url.port)
                == (fresh_url.scheme, fresh_url.hostname, fresh_url.port)
                and fresh_url.hostname in work.boundaries.allowed_hosts
                and (not work.boundaries.allowed_route_prefixes or any(
                    fresh_url.path.startswith(prefix)
                    for prefix in work.boundaries.allowed_route_prefixes
                ))
            )
            if usable_fresh_source:
                # 新帧是独立来源，不宣称它恢复了旧栏目，也不复用旧内容能力。
                payload_snapshot = payload_snapshot.merge(
                    source_capture_id=current_capture_id,
                    source_state_version=current_frontier.observation.state_version,
                    source_location=current_frontier.observation.location,
                    navigation_depth=selected_entry.navigation_depth,
                    candidates=current_frontier.candidates,
                    max_depth=work.budget.max_depth,
                    now=current_frontier.observation.observed_at,
                )
                old_identity = selected_entry.candidate.metadata.get("source_identity_fingerprint")
                fresh_identity = current_frontier.observation.metadata.get(
                    "logical_surface_identity_fingerprint"
                )
                if (entry_exhausted and old_identity and fresh_identity
                        and old_identity != fresh_identity):
                    # 两次真实恢复已证明这一精确来源不可达；兄弟条目共用失败事实，
                    # 不伪造其尝试次数，也不连坐其他 Capture/状态或新帧入口。
                    for sibling in payload_snapshot.pending_entries:
                        if (
                            sibling.source_capture_id == selected_entry.source_capture_id
                            and sibling.source_state_version == selected_entry.source_state_version
                            and sibling.source_location == selected_entry.source_location
                            and sibling.candidate.metadata.get("source_identity_fingerprint")
                            == old_identity
                        ):
                            payload_snapshot = payload_snapshot.mark_pending_skipped(
                                sibling.entry_id, reason="h5_shared_source_restore_exhausted",
                                active_state_version=current_frontier.observation.state_version,
                                finished_at=current_frontier.observation.observed_at,
                            )
            if entry_exhausted:
                # 恢复耗尽只作废这一条 sibling：必须立即改选下一条 pending，否则
                # 重领到的 Work 既无 selected 绑定也无候选，decide_next 会以
                # task_frontier_not_exhausted_cleanly 把整个 Run 有界收束，撇下
                # 全部未探候选。改选后的条目若跨状态，重领时照常触发来源恢复。
                payload_snapshot = _select_next_task_entry(
                    payload_snapshot,
                    active_state_version=current_frontier.observation.state_version,
                )
                reselected = any(
                    item.status is H5FrontierEntryStatus.SELECTED
                    for item in payload_snapshot.entries
                )
            frontier = _with_task_frontier(frontier=current_frontier, snapshot=payload_snapshot)
            fresh_selected = any(
                item.status is H5FrontierEntryStatus.SELECTED
                and item.source_capture_id == current_capture_id
                and item.source_state_version == current_frontier.observation.state_version
                and item.source_location == current_frontier.observation.location
                for item in payload_snapshot.entries
            )
            frontier = replace(
                frontier,
                observation=replace(
                    frontier.observation,
                    metadata={
                        **frontier.observation.metadata,
                        "h5_frontier_source_recovery_pending": not (
                            usable_fresh_source and fresh_selected
                        ),
                        "h5_frontier_source_restore_failure": {
                            "entry_id": selected_entry.entry_id,
                            "reason_code": result.reason_code,
                            "cause_code": result.failure_cause_code,
                            "route": result.route,
                            "attempt_count": result.restoration_attempt_count,
                        },
                    },
                ),
            )
            restored_work = replace(
                work,
                source_capture_id=current_capture_id,
                frontier_snapshot_id=frontier.snapshot_id,
                frontier=frontier,
            )
        reason_code = result.reason_code
        if not result.restored and entry_exhausted:
            reason_code = "h5_frontier_restore_exhausted"
        return H5FrontierRecoveryWorkResult(
            work=restored_work,
            restored=result.restored,
            reason_code=reason_code,
            reselected_sibling=reselected,
        )


class H5BrowserLossActionLedgerPort(Protocol):
    async def fail_h5_browser_loss_unknown(
        self,
        *,
        task_id: str,
        agent_run_id: str,
        session_id: str,
        action_execution_id: str,
        source_capture_id: str,
        failure_stage: str,
    ) -> object: ...


class H5CoordinatorBrowserLossRecovery:
    """复用 Web Runtime 与 H5 Workflow 的浏览器丢失恢复生产适配器。"""

    def __init__(
        self,
        *,
        capture_runtime: WebCaptureRuntime,
        detector: PersistingH5PageDetector,
        workflow: H5ExplorationWorkflow,
        action_ledger: H5BrowserLossActionLedgerPort | None = None,
        cancellation: H5CancellationCheckpoint | None = None,
        now: Callable[[], datetime] | None = None,
        session_lineage: H5SessionLineageTracker | None = None,
    ) -> None:
        self._capture_runtime = capture_runtime
        self._detector = detector
        self._workflow = workflow
        self._action_ledger = action_ledger
        self._cancellation = cancellation or H5CancellationCheckpoint()
        self._now = now or (lambda: datetime.now(UTC))
        self._session_lineage = session_lineage

    async def recover(
        self,
        *,
        work: ExplorationWork,
        agent_run_id: str,
        error: CaptureCoordinationError,
        attempt_reserved: bool = False,
    ) -> H5RecoveryOutcome:
        stage = H5FailureStage(error.stage.value)
        metadata = work.frontier.observation.metadata
        previous = metadata.get("h5_browser_recovery_attempt")
        attempt_count = (
            int(previous.get("recovery_attempt_count", 0)) if isinstance(previous, Mapping) else 0
        )
        if attempt_reserved:
            attempt_count = max(attempt_count - 1, 0)
        snapshot = H5TaskFrontierSnapshot.from_dict(metadata[H5_TASK_FRONTIER_METADATA_KEY])
        action_execution_id = None
        if error.interaction is not None:
            action_execution_id = error.interaction.action_execution_id
        elif error.before_capture is not None:
            action_execution_id = error.before_capture.action_execution_id
        elif isinstance(previous, Mapping):
            value = previous.get("action_execution_id")
            action_execution_id = str(value) if value is not None else None
        if stage is not H5FailureStage.BEFORE_CAPTURE and action_execution_id is None:
            runtime = _H5BrowserLossRuntime(
                capture_runtime=self._capture_runtime,
                detector=self._detector,
                workflow=self._workflow,
                cancellation=self._cancellation,
                now=self._now,
            )
            await runtime.record_source_loss(
                context=H5RecoveryContext(
                    task_id=work.task_id,
                    agent_run_id=agent_run_id,
                    session_id=work.session_id,
                    stage=stage,
                    last_safe_location=work.frontier.observation.location,
                    action_execution_id=None,
                    frontier_snapshot=snapshot,
                    recovery_attempt_count=attempt_count,
                    max_recovery_attempts=2,
                    entry_origin=_trusted_entry_origin(work),
                    failed_work=work,
                    failure_error_code=error.error_code,
                )
            )
            await runtime.checkpoint(
                context=H5RecoveryContext(
                    task_id=work.task_id,
                    agent_run_id=agent_run_id,
                    session_id=work.session_id,
                    stage=stage,
                    last_safe_location=work.frontier.observation.location,
                    action_execution_id=None,
                    frontier_snapshot=snapshot,
                    recovery_attempt_count=attempt_count,
                    max_recovery_attempts=2,
                    entry_origin=_trusted_entry_origin(work),
                    failed_work=work,
                    failure_error_code=error.error_code,
                ),
                stage="before_recovery_decision",
            )
            return H5RecoveryOutcome(
                retry_original_action=False,
                inventory_status="insufficient",
                supersede_failed_work=False,
                rebaseline_steps=(),
                recovery_work=None,
                recovery_attempt_count=attempt_count,
                reason_code="h5_browser_recovery_missing_action_execution_id",
            )
        context = H5RecoveryContext(
            task_id=work.task_id,
            agent_run_id=agent_run_id,
            session_id=work.session_id,
            stage=stage,
            last_safe_location=work.frontier.observation.location,
            action_execution_id=action_execution_id,
            frontier_snapshot=snapshot,
            recovery_attempt_count=attempt_count,
            max_recovery_attempts=(1 if stage is H5FailureStage.BEFORE_CAPTURE else 2),
            entry_origin=_trusted_entry_origin(work),
            failed_work=work,
            failure_error_code=error.error_code,
        )
        runtime = _H5BrowserLossRuntime(
            capture_runtime=self._capture_runtime,
            detector=self._detector,
            workflow=self._workflow,
            cancellation=self._cancellation,
            now=self._now,
            session_lineage=self._session_lineage,
        )
        await runtime.record_source_loss(context=context)
        if stage is not H5FailureStage.BEFORE_CAPTURE:
            attempt_facts = (
                H5BrowserRecoveryAttemptFacts.from_dict(previous)
                if isinstance(previous, Mapping)
                else None
            )
            before_capture_id = (
                attempt_facts.before_capture_id
                if attempt_facts is not None
                else (error.before_capture.capture_id if error.before_capture is not None else None)
            )
            if before_capture_id is None:
                await runtime.checkpoint(
                    context=context,
                    stage="before_recovery_decision",
                )
                return H5RecoveryOutcome(
                    retry_original_action=False,
                    inventory_status="insufficient",
                    supersede_failed_work=False,
                    rebaseline_steps=(),
                    recovery_work=None,
                    recovery_attempt_count=attempt_count,
                    reason_code="h5_browser_recovery_missing_before_capture_id",
                )
            if self._action_ledger is None:
                raise RuntimeError("H5 unknown Action 恢复缺少 Action ledger 端口")
            await self._action_ledger.fail_h5_browser_loss_unknown(
                task_id=work.task_id,
                agent_run_id=agent_run_id,
                session_id=work.session_id,
                action_execution_id=action_execution_id,
                source_capture_id=before_capture_id,
                failure_stage=stage.value,
            )
        attempt = work.frontier.observation.metadata.get("h5_browser_recovery_attempt")
        if isinstance(attempt, Mapping) and attempt.get("reason_code") == (
            "h5_browser_recovery_budget_exhausted"
        ):
            await runtime.checkpoint(context=context, stage="before_recovery_decision")
            return H5RecoveryOutcome(
                retry_original_action=False,
                inventory_status=(
                    None if stage is H5FailureStage.BEFORE_CAPTURE else "insufficient"
                ),
                supersede_failed_work=False,
                rebaseline_steps=(),
                recovery_work=None,
                recovery_attempt_count=attempt_count,
                reason_code="h5_browser_recovery_budget_exhausted",
            )
        return await H5ExplorationRecovery(runtime=runtime).recover(context=context)

    async def reserve_attempt(
        self,
        *,
        work: ExplorationWork,
        agent_run_id: str,
        error: CaptureCoordinationError,
    ) -> H5RecoveryAttemptReservation:
        """先扣减原动作/恢复动作预算并记录 started，再允许重开浏览器。"""
        stage = H5FailureStage(error.stage.value)
        metadata = work.frontier.observation.metadata
        previous = metadata.get("h5_browser_recovery_attempt")
        if isinstance(previous, Mapping) and previous.get("status") == "started":
            return H5RecoveryAttemptReservation(work=work, reserved=False)
        previous_facts = (
            H5BrowserRecoveryAttemptFacts.from_dict(previous)
            if isinstance(previous, Mapping)
            else None
        )
        history_payload = metadata.get("h5_browser_recovery_attempt_history", ())
        if not isinstance(history_payload, (list, tuple)):
            raise ValueError("H5 browser recovery attempt history 必须为列表")
        history = [dict(item) for item in history_payload]
        if previous_facts is not None and previous_facts.status == "completed":
            if not any(item.get("payload_hash") == previous_facts.payload_hash for item in history):
                history.append(previous_facts.to_dict())
        previous_count = (
            int(previous.get("recovery_attempt_count", 0)) if isinstance(previous, Mapping) else 0
        )
        attempt_count = previous_count + 1
        cost = 1 if stage is H5FailureStage.BEFORE_CAPTURE else 2
        budget_exhausted = (
            work.budget.remaining_actions < cost
            or work.budget.remaining_duration_ms <= 0
            or self._now() >= work.deadline
        )
        frontier = replace(
            work.frontier,
            observation=replace(
                work.frontier.observation,
                metadata={
                    **dict(metadata),
                    "agent_run_id": agent_run_id,
                    "h5_browser_recovery_attempt_history": history,
                    "h5_browser_recovery_attempt": H5BrowserRecoveryAttemptFacts.create(
                        task_id=work.task_id,
                        agent_run_id=agent_run_id,
                        source_session_id=work.session_id,
                        failed_work_id=work.work_id,
                        source_state_version=work.frontier.observation.state_version,
                        frontier_snapshot_id=work.frontier_snapshot_id,
                        failure_stage=stage,
                        recovery_attempt_count=attempt_count,
                        status="started",
                        error_code=error.error_code,
                        action_execution_id=(
                            error.interaction.action_execution_id
                            if error.interaction is not None
                            else (
                                error.before_capture.action_execution_id
                                if error.before_capture is not None
                                else (
                                    previous_facts.action_execution_id
                                    if previous_facts is not None
                                    else None
                                )
                            )
                        ),
                        before_capture_id=(
                            error.before_capture.capture_id
                            if error.before_capture is not None
                            else (
                                previous_facts.before_capture_id
                                if previous_facts is not None
                                else None
                            )
                        ),
                        after_capture_id=(
                            error.after_capture.capture_id
                            if error.after_capture is not None
                            else (
                                previous_facts.after_capture_id
                                if previous_facts is not None
                                else None
                            )
                        ),
                        interaction_status=(
                            error.interaction.status.value
                            if error.interaction is not None
                            else (
                                previous_facts.interaction_status
                                if previous_facts is not None
                                else None
                            )
                        ),
                        interaction_result_capture_id=(
                            error.interaction.result_capture_id
                            if error.interaction is not None
                            else (
                                previous_facts.interaction_result_capture_id
                                if previous_facts is not None
                                else None
                            )
                        ),
                        started_at=self._now().isoformat(),
                        deadline=work.deadline.isoformat(),
                        remaining_duration_ms=work.budget.remaining_duration_ms,
                        action_cost=(0 if budget_exhausted else cost),
                        remaining_actions_after_reservation=(
                            work.budget.remaining_actions
                            if budget_exhausted
                            else work.budget.remaining_actions - cost
                        ),
                        last_safe_location=work.frontier.observation.location,
                        reason_code=(
                            "h5_browser_recovery_budget_exhausted" if budget_exhausted else None
                        ),
                    ).to_dict(),
                },
            ),
        )
        reserved_budget = work.budget
        if not budget_exhausted:
            for _ in range(cost):
                reserved_budget = consume_exploration_action_budget(
                    reserved_budget,
                    counts_toward_business=True,
                    elapsed_duration_ms=0,
                )
        return H5RecoveryAttemptReservation(
            work=replace(
                work,
                frontier=frontier,
                frontier_snapshot_id=frontier.snapshot_id,
                budget=reserved_budget,
            ),
            reserved=True,
        )


class _H5BrowserLossRuntime:
    """一次恢复调用的短生命周期适配器，不持有第二套浏览器实现。"""

    def __init__(
        self,
        *,
        capture_runtime: WebCaptureRuntime,
        detector: PersistingH5PageDetector,
        workflow: H5ExplorationWorkflow,
        cancellation: H5CancellationCheckpoint,
        now: Callable[[], datetime],
        session_lineage: H5SessionLineageTracker | None = None,
    ) -> None:
        self._capture_runtime = capture_runtime
        self._detector = detector
        self._workflow = workflow
        self._cancellation = cancellation
        self._now = now
        self._coordination = None
        self._target_session_id: str | None = None
        self._started_at = now()
        self._session_lineage = session_lineage

    async def checkpoint(self, *, context: H5RecoveryContext, stage: str) -> None:
        work = _require_failed_work(context)
        if self._now() >= work.deadline or work.budget.remaining_duration_ms <= 0:
            await self._mark_target_lost(context=context, reason="budget_exhausted")
            raise RuntimeError("H5 浏览器恢复时间预算已耗尽")
        try:
            await self._cancellation.raise_if_requested(
                task_id=context.task_id,
                agent_run_id=context.agent_run_id,
                stage=f"browser_recovery:{stage}",
            )
        except H5ExplorationCanceled:
            await self._mark_target_lost(context=context, reason="canceled")
            raise

    async def reopen(self, *, context: H5RecoveryContext) -> None:
        sessions = self._capture_runtime.surface_sessions
        if sessions is None:
            raise RuntimeError("H5 浏览器恢复缺少 Surface Session 事实源")
        source = await sessions.get(session_id=context.session_id)
        if source is None:
            raise RuntimeError("H5 浏览器恢复找不到来源 Surface Session")
        if source.status.value != "lost":
            raise RuntimeError("H5 浏览器恢复要求来源 Surface Session 已持久化为 lost")
        # lost只证明不可再用，不证明物理操作停止；关闭失败不得创建后继会话。
        await self._capture_runtime.close_browser_session(context.session_id)
        target_session_id = build_h5_recovery_session_id(
            agent_run_id=context.agent_run_id,
            work_id=_require_failed_work(context).work_id,
            stage=context.stage,
            recovery_attempt_count=context.recovery_attempt_count + 1,
        )
        self._target_session_id = target_session_id
        driver = self._capture_runtime.surface_driver
        if driver is None:
            raise RuntimeError("H5 浏览器恢复缺少 Web Surface Driver")
        spec = SurfaceSessionSpec(
            task_id=context.task_id,
            session_id=target_session_id,
            surface_kind=SurfaceKind.WEB,
            options={
                "target_url": context.last_safe_location,
                **h5_viewport_parameters(),
            },
        )
        session = await driver.start_session(spec)
        try:
            await sessions.register_active(
                spec=spec,
                session_owner=f"h5-browser-recovery:{context.agent_run_id}",
                primary_surface_ref=session.primary_surface_ref,
            )
            if self._session_lineage is not None:
                self._session_lineage.register(target_session_id)
        except BaseException:
            await self._capture_runtime.close_browser_session(target_session_id)
            raise

    async def record_source_loss(self, *, context: H5RecoveryContext) -> None:
        sessions = self._capture_runtime.surface_sessions
        if sessions is None:
            raise RuntimeError("H5 浏览器恢复缺少 Surface Session 事实源")
        source = await sessions.get(session_id=context.session_id)
        if source is None:
            raise RuntimeError("H5 浏览器恢复找不到来源 Surface Session")
        if source.status.value != "lost":
            attempt_no = context.recovery_attempt_count + 1
            await sessions.mark_lost(
                task_id=context.task_id,
                session_id=context.session_id,
                expected_row_version=source.row_version,
                reason=(
                    f"h5_browser_loss:stage={context.stage.value};"
                    f"action={context.action_execution_id or 'unknown'};"
                    f"error={context.failure_error_code or 'browser_lost'};"
                    f"attempt={attempt_no}"
                ),
            )
        await self._capture_runtime.close_browser_session(context.session_id)

    async def navigate(
        self,
        *,
        context: H5RecoveryContext,
        destination_url: str,
    ) -> None:
        work = _require_failed_work(context)
        target_session_id = self._require_target_session_id()
        attempt_no = context.recovery_attempt_count + 1
        intent_id = f"h5-browser-recovery:{work.work_id}:{context.stage.value}:{attempt_no}"
        intent = ActionIntent(
            task_id=context.task_id,
            session_id=target_session_id,
            intent_id=intent_id,
            idempotency_key=intent_id,
            surface_kind=SurfaceKind.WEB,
            source_capture_id=work.source_capture_id,
            action_type=InteractionOperation.NAVIGATE,
            semantic_target=destination_url,
            purpose="恢复丢失的 H5 浏览器会话并重建可信基线",
            capture_profile=H5SurfaceBootstrapper.capture_profile(),
            deadline=work.deadline,
            parameters={
                "destination_url": destination_url,
                "h5_browser_recovery": True,
                "recovery_source_session_id": context.session_id,
                "failure_stage": context.stage.value,
                **h5_viewport_parameters(),
            },
            risk_level=InteractionRiskLevel.LOW,
        )
        try:
            self._coordination = await self._capture_runtime.h5_exploration_coordinator.coordinate(
                CaptureCoordinationCommand(agent_run_id=context.agent_run_id, intent=intent)
            )
        except BaseException:
            await self._mark_target_lost(context=context, reason="rebaseline_navigation_failed")
            raise

    async def capture(self, *, context: H5RecoveryContext) -> CaptureRef:
        del context
        if self._coordination is None:
            raise RuntimeError("H5 浏览器恢复尚未完成受控导航")
        return self._coordination.after_capture

    async def detect(
        self,
        *,
        context: H5RecoveryContext,
        capture: CaptureRef,
    ) -> dict[str, Any]:
        try:
            return await self._detector.detect(
                capture=capture,
                task_id=context.task_id,
                agent_run_id=context.agent_run_id,
            )
        except BaseException:
            await self._mark_target_lost(context=context, reason="rebaseline_detection_failed")
            raise

    async def assess(
        self,
        *,
        context: H5RecoveryContext,
        capture: CaptureRef,
        detection: dict[str, Any],
    ) -> object:
        del capture
        try:
            return adapt_h5_page_detection_to_assessment(
                task_id=context.task_id,
                agent_run_id=context.agent_run_id,
                detection_result=detection,
                assessed_at=self._now(),
            )
        except BaseException:
            await self._mark_target_lost(
                context=context,
                reason="rebaseline_assessment_failed",
            )
            raise

    async def state_equivalent(
        self,
        *,
        context: H5RecoveryContext,
        capture: CaptureRef,
        detection: dict[str, Any],
        assessment: object,
    ) -> bool:
        del capture, assessment
        try:
            frontier = build_h5_frontier(
                detection_result=detection,
                observed_at=self._now(),
                navigation_depth=0,
                logical_surface_effective=_logical_surface_enabled_for_work(
                    _require_failed_work(context)
                ),
                logical_targets_effective=_logical_targets_enabled_for_work(
                    _require_failed_work(context)
                ),
            )
            expected = context.frontier_snapshot.active_state_version
            if expected is None:
                expected = _require_failed_work(context).frontier.observation.state_version
            return frontier.observation.state_version == expected and _same_h5_location(
                frontier.observation.location,
                context.last_safe_location,
            )
        except BaseException:
            await self._mark_target_lost(
                context=context,
                reason="state_equivalence_failed",
            )
            raise

    async def persist_recovery(
        self,
        *,
        context: H5RecoveryContext,
        capture: CaptureRef,
        detection: dict[str, Any],
        assessment: object,
        equivalent: bool,
        reason_code: str | None,
        recovery_attempt_count: int,
    ) -> ExplorationWork | None:
        if not equivalent or reason_code is not None:
            await self._mark_target_lost(
                context=context,
                reason=reason_code or "rebaseline_rejected",
            )
        work = _require_failed_work(context)
        suffix = f"{work.work_id}:{context.stage.value}:{recovery_attempt_count}"
        try:
            if not hasattr(assessment, "execution_status"):
                raise RuntimeError("H5 浏览器恢复缺少类型化页面评估")
            result = await self._workflow.prepare_browser_recovery(
                RecoverH5ExplorationCommand(
                    prior_work=work,
                    capture=capture,
                    detection_result=detection,
                    assessment=assessment,
                    failure_stage=context.stage.value,
                    recovery_attempt_count=recovery_attempt_count,
                    elapsed_ms=context.elapsed_ms
                    + max(
                        0,
                        int((self._now() - self._started_at).total_seconds() * 1_000),
                    ),
                    restoration_attempt_id=f"h5-restoration:{suffix}",
                    equivalence_assessment_id=f"h5-state-equivalence:{suffix}",
                    action_execution_id=context.action_execution_id,
                    inventory_status=(
                        None if context.stage is H5FailureStage.BEFORE_CAPTURE else "insufficient"
                    ),
                    equivalent=equivalent,
                    reason_code=reason_code,
                )
            )
        except BaseException:
            await self._mark_target_lost(
                context=context,
                reason="recovery_work_preparation_failed",
            )
            raise
        return result.work

    def _require_target_session_id(self) -> str:
        if self._target_session_id is None:
            raise RuntimeError("H5 恢复 Session 尚未建立")
        return self._target_session_id

    async def _mark_target_lost(
        self,
        *,
        context: H5RecoveryContext,
        reason: str,
    ) -> None:
        if self._target_session_id is None:
            return
        sessions = self._capture_runtime.surface_sessions
        if sessions is None:
            return
        snapshot = await sessions.get(session_id=self._target_session_id)
        if snapshot is None or snapshot.status.value == "lost":
            return
        await sessions.mark_lost(
            task_id=context.task_id,
            session_id=self._target_session_id,
            expected_row_version=snapshot.row_version,
            reason=f"h5_browser_recovery:{reason}",
        )


class _CoordinatorRecoveryBrowser:
    """把受控恢复动作限定在现有 CaptureCoordinator 原子边界内。"""

    def __init__(
        self,
        *,
        work: ExplorationWork,
        agent_run_id: str,
        capture_coordinator: object,
        detector: PersistingH5PageDetector,
        assessment_repository: H5PageTypeAssessmentPersistencePort,
        cancellation: H5CancellationCheckpoint,
        now: Callable[[], datetime],
        source_state_version: str,
        surface_sessions: object | None = None,
        page_graph: object | None = None,
        h5_visual_coordinate_mode: Literal["off", "shadow", "enforced"] = "off",
    ) -> None:
        self._work = work
        self._agent_run_id = agent_run_id
        self._capture_coordinator = capture_coordinator
        self._detector = detector
        self._assessment_repository = assessment_repository
        self._cancellation = cancellation
        self._now = now
        self._source_state_version = source_state_version
        self._coordination = None
        self.remaining_budget = work.budget
        self._surface_sessions = surface_sessions
        self._page_graph = page_graph
        self._h5_visual_coordinate_mode = h5_visual_coordinate_mode

    async def restore_source_path(self, *, entry, capture, detection):
        """同 URL 不同栏目按可信图逐步恢复，不复用历史坐标或扩大预算。"""
        if self._page_graph is None or self._surface_sessions is None:
            return None
        if entry.entry_id != self._selected_frontier_entry().entry_id:
            raise ValueError("H5 栏目恢复必须绑定当前 selected 条目")
        session = await self._surface_sessions.get(session_id=self._work.session_id)
        if not self._graph_session_matches(session, capture):
            return None
        graph = await self._page_graph.get_graph(
            task_id=self._work.task_id, agent_run_id=self._agent_run_id,
            finalization_epoch=session.finalization_epoch,
        )
        steps = plan_h5_frontier_graph_steps(
            graph=graph, entry=entry, task_id=self._work.task_id,
            agent_run_id=self._agent_run_id, session_id=self._work.session_id,
            finalization_epoch=session.finalization_epoch, current_capture_id=capture.capture_id,
        )
        if not steps or len(steps) > self.remaining_budget.resolved_remaining_business_actions:
            return None
        failure_code = None
        for step in steps:
            budget = self.remaining_budget
            if (budget.resolved_remaining_business_actions <= 0
                    or budget.remaining_duration_ms <= 0 or self._now() >= self._work.deadline):
                failure_code = "H5_FRONTIER_GRAPH_BUDGET_EXHAUSTED"
                break
            latest = await self._surface_sessions.get(session_id=self._work.session_id)
            if (not self._graph_session_matches(latest, capture)
                    or latest.finalization_epoch != session.finalization_epoch):
                failure_code = "H5_FRONTIER_GRAPH_SESSION_CHANGED"
                break
            frontier = build_h5_frontier(
                detection_result=detection, observed_at=self._now(),
                navigation_depth=entry.navigation_depth,
                logical_surface_effective=_logical_surface_enabled_for_work(self._work),
                logical_targets_effective=_logical_targets_enabled_for_work(self._work),
                h5_visual_coordinate_mode=self._h5_visual_coordinate_mode,
                source_task_id=self._work.task_id,
            )
            if (frontier.observation.capture_id != capture.capture_id
                    or frontier.observation.metadata.get("logical_surface_identity_fingerprint")
                    != step.source_identity
                    or frontier.observation.overlay_signature is not None
                    or frontier.observation.metadata.get("requires_auth") is True
                    or frontier.observation.metadata.get("detection_status") != "recognized"
                    or frontier.observation.page_state != "stable"):
                failure_code = "H5_FRONTIER_GRAPH_SOURCE_MISMATCH"
                break
            candidates = tuple(candidate for candidate in frontier.candidates if
                               candidate.action_type.value == step.action_kind
                               and candidate.semantic_target == step.semantic_target
                               and candidate.risk_level is InteractionRiskLevel.LOW
                               and candidate.candidate_id not in frontier.blocked_candidate_ids
                               and candidate.action_type in self._work.boundaries.allowed_operations
                               and candidate.expected_surface_kind in {None, SurfaceKind.WEB}
                               and candidate.navigation_depth <= budget.max_depth)
            if len(candidates) != 1:
                failure_code = "H5_FRONTIER_GRAPH_CANDIDATE_NOT_UNIQUE"
                break
            candidate = candidates[0]
            transient = replace(self._work, source_capture_id=capture.capture_id,
                                frontier=frontier, frontier_snapshot_id=frontier.snapshot_id)
            parameters = _h5_candidate_parameters(
                work=transient, candidate=candidate,
                h5_visual_coordinate_mode=self._h5_visual_coordinate_mode,
            )
            intent_id = "h5-frontier-graph:" + stable_protocol_hash({
                "work_id": self._work.work_id, "entry_id": entry.entry_id,
                "transition_id": step.transition_id, "capture_id": capture.capture_id,
                "candidate_id": candidate.candidate_id,
                "attempt": self._selected_restoration_attempt_number(),
            }).removeprefix("sha256:")
            await self._cancellation.raise_if_requested(
                task_id=self._work.task_id, agent_run_id=self._agent_run_id,
                stage="before_frontier_graph_replay",
            )
            started_at = self._now()
            try:
                self._coordination = await self._capture_coordinator.coordinate(
                    CaptureCoordinationCommand(agent_run_id=self._agent_run_id, intent=ActionIntent(
                        task_id=self._work.task_id, session_id=self._work.session_id,
                        intent_id=intent_id, idempotency_key=intent_id,
                        surface_kind=SurfaceKind.WEB, source_capture_id=capture.capture_id,
                        action_type=candidate.action_type,
                        semantic_target=candidate.semantic_target,
                        purpose="按可信历史路线恢复 H5 栏目来源",
                        capture_profile=CaptureProfilePolicy.for_surface(
                            surface_kind=SurfaceKind.WEB, operation=candidate.action_type,
                        ), deadline=self._work.deadline, risk_level=candidate.risk_level,
                        parameters={**parameters, "h5_frontier_restore": True,
                                    "h5_frontier_graph_transition_id": step.transition_id},
                    )),
                )
                capture = self._coordination.after_capture
                detection = None
                if not self._graph_capture_matches(capture):
                    return H5FrontierGraphOutcome(
                        capture=capture, detection=None, arrived=False,
                        failure_cause_code="H5_FRONTIER_GRAPH_CAPTURE_SCOPE_INVALID",
                    )
                detection = await self.detect(capture=capture)
                assessment = await self.assess(capture=capture, detection=detection)
                logical = detection.get("logical_surface", {})
                if (self._coordination.interaction.status is not InteractionResultStatus.SUCCEEDED
                        or not _source_restoration_assessment_accepts(assessment)
                        or detection.get("page_capture_id") != capture.capture_id
                        or not isinstance(logical, Mapping)
                        or logical.get("identity_fingerprint") != step.target_identity):
                    failure_code = (getattr(self._coordination.interaction, "error_code", None)
                                    or "H5_FRONTIER_GRAPH_STEP_UNVERIFIED")
                    break
            except H5ExplorationCanceled:
                raise
            except CaptureCoordinationError as error:
                failure_code = error.error_code
                # 动作前已重采时保留新现场，失败不能重新绑定旧截图。
                fresh = error.after_capture or error.before_capture
                if isinstance(fresh, CaptureRef) and self._graph_capture_matches(fresh):
                    capture = fresh
                    detection = None
                    try:
                        detection = await self.detect(capture=capture)
                    except H5ExplorationCanceled:
                        raise
                    except Exception:  # noqa: BLE001 - 已留存的新现场不退回旧截图
                        pass
                break
            except Exception as error:  # noqa: BLE001 - 已发生的动作保留现场与原因，不伪造到达
                return H5FrontierGraphOutcome(
                    capture=capture, detection=detection, arrived=False,
                    failure_cause_code=getattr(error, "error_code", None) or type(error).__name__,
                )
            finally:
                self.remaining_budget = consume_exploration_action_budget(
                    budget, counts_toward_business=True,
                    elapsed_duration_ms=max(
                        int((self._now() - started_at).total_seconds() * 1000), 0,
                    ),
                )
        else:
            return H5FrontierGraphOutcome(capture=capture, detection=detection, arrived=True)
        return H5FrontierGraphOutcome(capture=capture, detection=detection, arrived=False,
                                     failure_cause_code=failure_code)

    def _graph_capture_matches(self, capture) -> bool:
        return bool(isinstance(capture, CaptureRef) and capture.task_id == self._work.task_id
                    and capture.session_id == self._work.session_id
                    and capture.surface_kind is SurfaceKind.WEB)

    def _graph_session_matches(self, session, capture) -> bool:
        return bool(self._graph_capture_matches(capture) and session is not None
                    and session.task_id == self._work.task_id
                    and session.surface_kind is SurfaceKind.WEB
                    and session.status.value == "active"
                    and session.current_capture_id == capture.capture_id
                    and ((self._work.session_id == f"h5:{self._agent_run_id}"
                          and session.session_owner == H5_SURFACE_SESSION_OWNER)
                         or (self._work.session_id.startswith(f"h5:{self._agent_run_id}:recovery:")
                             and session.session_owner
                             == f"h5-browser-recovery:{self._agent_run_id}")))

    async def is_history_ancestor(self, *, source_location: str, current_location: str) -> bool:
        payload = self._work.frontier.observation.metadata[H5_TASK_FRONTIER_METADATA_KEY]
        snapshot = H5TaskFrontierSnapshot.from_dict(payload)
        observation = self._work.frontier.observation
        direct_parent_state = observation.metadata.get("parent_state_version")
        history_parent_state = observation.metadata.get("history_parent_state_version")
        history_parent_location = observation.metadata.get("history_parent_location")
        normalized_source = canonical_h5_route_identity(source_location)
        normalized_current = canonical_h5_route_identity(current_location)
        # 滚动、补采和弹窗也会产生父状态；同 URL 的状态关系不能证明浏览器后退目标。
        if normalized_source == normalized_current:
            return False
        source_is_direct_parent = direct_parent_state == self._source_state_version
        source_is_history_parent = (
            history_parent_state == self._source_state_version
            and isinstance(history_parent_location, str)
            and normalized_source == canonical_h5_route_identity(history_parent_location)
        )
        source_entries = tuple(
            entry
            for entry in snapshot.entries
            if entry.source_state_version == self._source_state_version
            and normalized_source == canonical_h5_route_identity(entry.source_location)
        )
        intermediate_global_navigation_parent = bool(
            isinstance(history_parent_state, str)
            and history_parent_state.strip()
            and isinstance(history_parent_location, str)
            and _same_h5_origin(source_location, history_parent_location)
            and _same_h5_origin(current_location, history_parent_location)
            and canonical_h5_route_identity(history_parent_location) != normalized_current
            and any(
                entry.status is H5FrontierEntryStatus.SELECTED
                and h5_global_bottom_navigation_index(entry.candidate) is not None
                for entry in source_entries
            )
        )
        return (
            (
                source_is_direct_parent
                or source_is_history_parent
                or intermediate_global_navigation_parent
            )
            and normalized_current == canonical_h5_route_identity(observation.location)
            and bool(source_entries)
        )

    async def back(self) -> None:
        await self._coordinate(operation=InteractionOperation.BACK, destination_url=None)

    async def navigate(self, destination_url: str) -> None:
        await self._coordinate(
            operation=InteractionOperation.NAVIGATE,
            destination_url=destination_url,
        )

    async def capture(self) -> CaptureRef:
        if self._coordination is None:
            raise RuntimeError("H5 来源恢复尚未执行受控动作")
        await self._cancellation.raise_if_requested(
            task_id=self._work.task_id,
            agent_run_id=self._agent_run_id,
            stage="after_recovery_capture_commit",
        )
        return self._coordination.after_capture

    async def detect(self, *, capture: CaptureRef) -> dict[str, Any]:
        return await self._detector.detect(
            capture=capture,
            task_id=self._work.task_id,
            agent_run_id=self._agent_run_id,
        )

    async def assess(self, *, capture: CaptureRef, detection: dict[str, Any]) -> object:
        assessment = adapt_h5_page_detection_to_assessment(
            task_id=self._work.task_id,
            agent_run_id=self._agent_run_id,
            detection_result=detection,
            assessed_at=self._now(),
        )
        await self._assessment_repository.upsert(
            task_id=self._work.task_id,
            agent_run_id=self._agent_run_id,
            session_id=self._work.session_id,
            page_capture_id=capture.capture_id,
            detection_result=detection,
            assessment_payload=assessment.to_dict(),
        )
        return assessment

    async def dismiss_startup_ad(
        self,
        *,
        capture: CaptureRef,
        detection: dict[str, Any],
    ) -> CaptureRef | None:
        """消费已有同会话倒计时观察，再执行当前帧签发的唯一关闭能力。"""
        reobserve_count = 0
        source_location = canonical_h5_route_identity(str(detection.get("page_url") or ""))
        while True:
            budget = self.remaining_budget
            available = (
                budget.resolved_remaining_obstruction_actions
                if budget.resolved_max_obstruction_actions else budget.remaining_actions
            )
            if reobserve_count and (
                available <= 0 or budget.remaining_duration_ms <= 0
                or self._now() >= self._work.deadline
            ):
                return None
            if (
                detection.get("page_capture_id") != capture.capture_id
                or detection.get("detection_status") != "recognized"
            ):
                return None
            if str(detection.get("page_type") or "").casefold() != "startup_ad":
                return capture if reobserve_count else None
            if canonical_h5_route_identity(str(detection.get("page_url") or "")) != source_location:
                return None
            frontier = build_h5_frontier(
                detection_result=detection,
                observed_at=self._now(),
                navigation_depth=0,
                lineage_metadata={"startup_ad_countdown_reobserve_count": reobserve_count},
                logical_surface_effective=_logical_surface_enabled_for_work(self._work),
                logical_targets_effective=_logical_targets_enabled_for_work(self._work),
            )
            transient_work = replace(
                self._work, source_capture_id=capture.capture_id,
                frontier_snapshot_id=frontier.snapshot_id, frontier=frontier,
            )
            closes = tuple(
                candidate for candidate in frontier.candidates
                if _trusted_startup_ad_candidate_parameters_for_page(
                    page_type=frontier.observation.page_type, candidate=candidate,
                ) or _trusted_popup_candidate_parameters_for_state(
                    page_state=frontier.observation.page_state, candidate=candidate,
                ).get("popup_dismissal_capability") == "rendered_modal_close"
            )
            if closes:
                if len(closes) != 1:
                    return None
                mode = closes[0].metadata["action_parameters"]["target_mode"]
                return await self._coordinate_obstruction_candidate(
                    capture=capture, frontier=frontier, candidate=closes[0], operation=mode,
                )
            observations = tuple(
                candidate for candidate in frontier.candidates
                if _trusted_startup_ad_countdown_reobserve_candidate_parameters(
                    work=transient_work, candidate=candidate,
                )
            )
            if (
                len(observations) != 1 or available <= 0
                or budget.remaining_duration_ms <= 0 or self._now() >= self._work.deadline
            ):
                return None
            started_at = self._now()
            try:
                capture = await self._coordinate_obstruction_candidate(
                    capture=capture, frontier=frontier, candidate=observations[0],
                    operation="startup_ad_countdown_reobserve",
                )
                detection = await self.detect(capture=capture)
            finally:
                self.remaining_budget = consume_exploration_action_budget(
                    budget, counts_toward_business=False,
                    elapsed_duration_ms=max(
                        int((self._now() - started_at).total_seconds() * 1000), 0,
                    ),
                )
            reobserve_count += 1

    async def dismiss_popup(
        self,
        *,
        capture: CaptureRef,
        detection: dict[str, Any],
    ) -> CaptureRef | None:
        """仅执行当前恢复 Capture 上由检测层签发的唯一弹窗关闭能力。"""
        if (
            detection.get("page_capture_id") != capture.capture_id
            or detection.get("detection_status") != "recognized"
            or not h5_page_state_supports_popup_dismissal(detection.get("page_state"))
        ):
            return None
        frontier = build_h5_frontier(
            detection_result=detection,
            observed_at=self._now(),
            navigation_depth=0,
            logical_surface_effective=_logical_surface_enabled_for_work(self._work),
            logical_targets_effective=_logical_targets_enabled_for_work(self._work),
        )
        candidates = tuple(
            candidate
            for candidate in frontier.candidates
            if _trusted_popup_candidate_parameters_for_state(
                page_state=frontier.observation.page_state,
                candidate=candidate,
            )
        )
        if len(candidates) != 1:
            return None
        return await self._coordinate_obstruction_candidate(
            capture=capture,
            frontier=frontier,
            candidate=candidates[0],
            operation="popup_dismissal",
        )

    async def _coordinate_obstruction_candidate(
        self,
        *,
        capture: CaptureRef,
        frontier,
        candidate,
        operation: str,
    ) -> CaptureRef:
        """用短哈希身份执行已经完整验签的恢复期清障候选。"""
        restoration_attempt = self._selected_restoration_attempt_number()
        transient_work = replace(
            self._work,
            source_capture_id=capture.capture_id,
            frontier_snapshot_id=frontier.snapshot_id,
            frontier=frontier,
        )
        intent_digest = stable_protocol_hash(
            {
                "work_id": self._work.work_id,
                "source_capture_id": capture.capture_id,
                "candidate_id": candidate.candidate_id,
                "operation": operation,
                "restoration_attempt": restoration_attempt,
            }
        ).removeprefix("sha256:")
        intent_id = f"h5-frontier-obstruction:{intent_digest}"
        await self._cancellation.raise_if_requested(
            task_id=self._work.task_id,
            agent_run_id=self._agent_run_id,
            stage=f"before_recovery_{operation}",
        )
        try:
            self._coordination = await self._capture_coordinator.coordinate(
                CaptureCoordinationCommand(
                    agent_run_id=self._agent_run_id,
                    intent=ActionIntent(
                        task_id=self._work.task_id,
                        session_id=self._work.session_id,
                        intent_id=intent_id,
                        idempotency_key=intent_id,
                        surface_kind=self._work.surface_kind,
                        source_capture_id=capture.capture_id,
                        action_type=candidate.action_type,
                        semantic_target=candidate.semantic_target,
                        purpose=h5_candidate_purpose(candidate),
                        capture_profile=H5SurfaceBootstrapper.capture_profile(),
                        deadline=self._work.deadline,
                        parameters={
                            **_h5_candidate_parameters(
                                work=transient_work,
                                candidate=candidate,
                            ),
                            "h5_frontier_restoration_attempt": restoration_attempt,
                        },
                        risk_level=candidate.risk_level,
                    ),
                )
            )
        except CaptureCoordinationError as error:
            fresh_capture = await self._fresh_capture_after_obstruction_changed(
                error=error,
                operation=operation,
                source_capture=capture,
            )
            if fresh_capture is None:
                raise
            return fresh_capture
        return self._coordination.after_capture

    async def _fresh_capture_after_obstruction_changed(
        self,
        *,
        error: CaptureCoordinationError,
        operation: str,
        source_capture: CaptureRef,
    ) -> CaptureRef | None:
        """关闭前现场已变化时重新识别，不重放旧候选或重新导航触发广告。"""
        fresh_capture = error.before_capture
        if (
            error.stage is not CaptureCoordinationStage.ACTION
            or not isinstance(fresh_capture, CaptureRef)
            or fresh_capture.task_id != self._work.task_id
            or fresh_capture.session_id != self._work.session_id
            or fresh_capture.surface_kind is not SurfaceKind.WEB
            or fresh_capture.capture_kind is not CaptureKind.PAGE
            or fresh_capture.role != "before_action"
            or fresh_capture.capture_id == source_capture.capture_id
        ):
            return None
        if operation == "popup_dismissal" and error.error_code in {
            "POPUP_DISMISSAL_NOT_FOUND", "POPUP_MODAL_SCOPE_MISMATCH",
        }:
            # 外层会重新识别和评估，新关闭动作仍需验签，并占用原弹窗链预算。
            return fresh_capture
        if operation != "startup_ad_dismissal" or error.error_code not in {
            "STARTUP_AD_DISMISSAL_NOT_FOUND", "STARTUP_AD_DISMISSAL_NOT_OBSERVED",
        }:
            return None
        detection = await self.detect(capture=fresh_capture)
        if (
            detection.get("page_capture_id") != fresh_capture.capture_id
            or detection.get("detection_status") != "recognized"
            or str(detection.get("page_type") or "").casefold() == "startup_ad"
        ):
            return None
        return fresh_capture

    async def _coordinate(
        self,
        *,
        operation: InteractionOperation,
        destination_url: str | None,
    ) -> None:
        await self._cancellation.raise_if_requested(
            task_id=self._work.task_id,
            agent_run_id=self._agent_run_id,
            stage="before_recovery_action",
        )
        restoration_attempt = self._selected_restoration_attempt_number()
        # 同一 Work 会依次恢复多个 sibling，幂等身份必须绑定条目与本轮真实来源。
        intent_digest = stable_protocol_hash(
            {
                "work_id": self._work.work_id,
                "entry_id": self._selected_frontier_entry().entry_id,
                "source_capture_id": self._work.source_capture_id,
                "source_state_version": self._source_state_version,
                "restoration_attempt": restoration_attempt,
                "operation": operation.value,
                "destination_url": destination_url,
            }
        ).removeprefix("sha256:")
        intent_id = f"h5-frontier-restore:{intent_digest}"
        parameters = {
            "h5_frontier_restore": True,
            "h5_frontier_restoration_attempt": restoration_attempt,
            **h5_viewport_parameters(),
        }
        if operation is InteractionOperation.BACK:
            parameters["navigation_depth"] = self._selected_source_navigation_depth()
        if destination_url is not None:
            parameters["destination_url"] = destination_url
        intent = ActionIntent(
            task_id=self._work.task_id,
            session_id=self._work.session_id,
            intent_id=intent_id,
            idempotency_key=intent_id,
            surface_kind=self._work.surface_kind,
            source_capture_id=self._work.source_capture_id,
            action_type=operation,
            semantic_target=(destination_url or "浏览器历史返回"),
            purpose="恢复 H5 Frontier sibling 的来源页面",
            capture_profile=H5SurfaceBootstrapper.capture_profile(),
            deadline=self._work.deadline,
            parameters=parameters,
            risk_level=InteractionRiskLevel.LOW,
        )
        self._coordination = await self._capture_coordinator.coordinate(
            CaptureCoordinationCommand(agent_run_id=self._agent_run_id, intent=intent)
        )

    def _selected_frontier_entry(self) -> H5TaskFrontierEntry:
        """读取当前恢复所绑定的唯一 selected Frontier 条目。"""
        payload = self._work.frontier.observation.metadata[H5_TASK_FRONTIER_METADATA_KEY]
        snapshot = H5TaskFrontierSnapshot.from_dict(payload)
        selected = [
            entry
            for entry in snapshot.entries
            if entry.status is H5FrontierEntryStatus.SELECTED
            and entry.source_state_version == self._source_state_version
        ]
        if len(selected) != 1:
            raise RuntimeError("H5 来源恢复缺少唯一 selected Frontier 条目")
        return selected[0]

    def _selected_restoration_attempt_number(self) -> int:
        """同一次恢复重放保持稳定，下一次恢复使用新的幂等身份。"""
        return self._selected_frontier_entry().restoration_attempt_count + 1

    def _selected_source_navigation_depth(self) -> int:
        """只从当前 selected 来源项签发 back 深度，禁止调用方自行伪造。"""
        selected = self._selected_frontier_entry()
        if selected.navigation_depth <= 0:
            raise RuntimeError("H5 back 恢复缺少唯一且大于零的来源导航深度")
        return selected.navigation_depth


def _same_h5_origin(left: str, right: str) -> bool:
    """比较可信 H5 URL 的规范化源，非法端口或凭据一律拒绝。"""
    try:
        origins = []
        for value in (left, right):
            parsed = urlsplit(value)
            scheme = parsed.scheme.casefold()
            if (
                scheme not in {"http", "https"}
                or not parsed.hostname
                or parsed.username is not None
                or parsed.password is not None
            ):
                return False
            origins.append(
                (
                    scheme,
                    parsed.hostname.casefold(),
                    parsed.port or (443 if scheme == "https" else 80),
                )
            )
    except ValueError:
        return False
    return origins[0] == origins[1]


def _logical_surface_enabled_for_work(work: ExplorationWork) -> bool:
    """恢复路径只沿用已投影到来源 Work 的逻辑页面判定模式。"""
    identity = work.frontier.observation.metadata.get("logical_surface_identity_fingerprint")
    return bool(isinstance(identity, str) and re.fullmatch(r"sha256:[0-9a-f]{64}", identity))


def _logical_targets_enabled_for_work(work: ExplorationWork) -> bool:
    """恢复路径只沿用已由第三开关投影的统一目标模式。"""
    return bool(
        _logical_surface_enabled_for_work(work)
        and work.frontier.observation.metadata.get("logical_targets_effective") is True
    )


def _trusted_logical_recovery_candidate(candidate: object) -> bool:
    """只让已密封 Frontier 的可信逻辑目标恢复其来源能力开关。"""
    metadata = getattr(candidate, "metadata", None)
    if not isinstance(metadata, Mapping):
        return False
    source_identity = metadata.get("source_identity_fingerprint")
    source_state = metadata.get("source_state_fingerprint")
    source_page_type = metadata.get("source_page_type")
    expected_page_type = metadata.get("expected_next_page_type")
    action_parameters = metadata.get("action_parameters")
    known_destination = bool(
        isinstance(expected_page_type, str) and expected_page_type.strip()
    )
    unknown_bottom_navigation_destination = bool(
        expected_page_type is None
        and getattr(candidate, "action_type", None) is InteractionOperation.CLICK
        and isinstance(action_parameters, Mapping)
        and action_parameters.get("target_mode") == "bottom_navigation"
        and isinstance(action_parameters.get("navigation_index"), int)
        and not isinstance(action_parameters.get("navigation_index"), bool)
        and action_parameters["navigation_index"] >= 1
        and isinstance(action_parameters.get("navigation_label"), str)
        and action_parameters["navigation_label"].strip()
        and action_parameters["navigation_label"]
        == action_parameters["navigation_label"].strip()
    )
    return bool(
        metadata.get("logical_target_required") is True
        and isinstance(source_identity, str)
        and re.fullmatch(r"sha256:[0-9a-f]{64}", source_identity)
        and isinstance(source_state, str)
        and re.fullmatch(r"sha256:[0-9a-f]{64}", source_state)
        and isinstance(source_page_type, str)
        and source_page_type.strip()
        and (known_destination or unknown_bottom_navigation_destination)
        and getattr(candidate, "action_type", None)
        in {
            InteractionOperation.CLICK,
            InteractionOperation.NAVIGATE,
            InteractionOperation.OBSERVE,
        }
        and getattr(candidate, "risk_level", None) is InteractionRiskLevel.LOW
    )


class PostgresH5RuntimeProvider:
    """组合 Web Capture、控制面仓储、H5 Explorer 与租约 Runner。"""

    def __init__(
        self,
        *,
        worker_instance_id: str,
        cancellation: H5CancellationPort | None = None,
        supplemental_evidence_policy: SupplementalEvidencePolicy | None = None,
        capture_runtime_factory: Callable[..., WebCaptureRuntime] = (
            get_or_create_web_capture_runtime
        ),
    ) -> None:
        if not worker_instance_id or worker_instance_id != worker_instance_id.strip():
            raise ValueError("worker_instance_id 必须为非空且无首尾空白的字符串")
        self._worker_instance_id = worker_instance_id
        self._capture_runtime_factory = capture_runtime_factory
        self._cancellation = H5CancellationCheckpoint(cancellation)
        self._supplemental_evidence_policy = supplemental_evidence_policy

    async def build(self, *, record: AgentRunRecord, settings: Settings) -> H5V2Runtime:
        from services.api.app.infrastructure.db.page_graph_repository import (
            PostgresPageGraphRepository,
        )

        runtime_settings = build_h5_task_runtime_settings(
            task_config=record.task_config,
            loop_guard=record.loop_guard,
            page_sampling_policy=record.page_sampling_policy,
            defaults=settings.agent_runtime,
        )
        target_url = _h5_target_url(record)
        if record.task_config.get("h5_shortflows"):
            from services.api.app.infrastructure.db.h5_shortflow_activation import (
                activate_h5_shortflows_for_run,
            )

            await activate_h5_shortflows_for_run(
                task_id=record.task_id, agent_run_id=record.run_id,
                task_config=record.task_config,
            )
        visual_coordinate_mode = settings.workflow.h5_visual_coordinate_runtime_mode
        capture_runtime = self._capture_runtime_factory(
            h5_visual_coordinate_mode=visual_coordinate_mode,
        )
        if capture_runtime.surface_driver is None or capture_runtime.surface_sessions is None:
            raise RuntimeError("Web Capture Runtime 缺少 H5 Surface Driver 或 Session 仓储")
        queue = PostgresExplorerWorkRepository()
        orchestrator = ExplorationOrchestrator(
            exploration_publisher=PostgresExplorationWorkPublisher(
                repository=queue,
                emit_wakeup=False,
            ),
        )
        interaction_pattern_knowledge = PostgresH5InteractionPatternRepository()
        detector = PersistingH5PageDetector(
            h5_visual_coordinate_mode=visual_coordinate_mode,
            interaction_pattern_knowledge=interaction_pattern_knowledge,
        )
        assessment_repository = H5PageTypeAssessmentRepository()
        scheduler_facts_reader: ExplorationSchedulerFactsReader = (
            PostgresExplorationSchedulerFactsReader()
        )
        workflow = H5ExplorationWorkflow(
            bootstrapper=H5SurfaceBootstrapper(
                driver=capture_runtime.surface_driver,
                surface_sessions=capture_runtime.surface_sessions,
                capture_coordinator=capture_runtime.h5_exploration_coordinator,
            ),
            auth_bootstrap=_build_h5_auth_bootstrap(
                target_url=target_url,
                settings=settings,
                capture_runtime=capture_runtime,
                detector=detector,
            ),
            safety_interstitial_bypass=CoordinatedH5SafetyInterstitialBypass(
                capture_coordinator=capture_runtime.h5_exploration_coordinator,
            ),
            detector=detector,
            orchestrator=orchestrator,
            assessment_repository=assessment_repository,
            logical_surface_effective=settings.workflow.h5_logical_surface_effective,
            logical_targets_effective=settings.workflow.h5_logical_targets_effective,
            h5_visual_coordinate_mode=visual_coordinate_mode,
            scheduler_facts_reader=scheduler_facts_reader,
            interaction_pattern_knowledge=interaction_pattern_knowledge,
        )
        session_lineage = H5SessionLineageTracker()
        from services.api.app.agents.explorer.bootstrap import (
            build_default_h5_explorer_agent,
            build_h5_shortflow_agent,
        )
        from services.api.app.application.h5_adaptive_journey import H5AdaptiveJourneyRunner
        from services.api.app.application.h5_checkpoint_observation import H5CheckpointObserver
        from services.api.app.application.h5_checkpoint_planning import H5CheckpointPlanner
        from services.api.app.infrastructure.db.checkpoint_execution_repository import (
            PostgresCheckpointExecutionRepository,
        )
        from services.api.app.infrastructure.db.journey_definition_repository import (
            PostgresJourneyDefinitionRepository,
        )

        adaptive_journey = H5AdaptiveJourneyRunner(
            repository=PostgresCheckpointExecutionRepository(observer=H5CheckpointObserver(
                snapshots=getattr(capture_runtime, "trusted_ui_snapshot_reader", None),
                plans=PostgresJourneyDefinitionRepository(),
            )),
            planner=H5CheckpointPlanner(
                delegate_factory=build_default_h5_explorer_agent,
                shortflow_delegate_factory=build_h5_shortflow_agent,
            ),
        )
        runner = H5ExplorationTurnRunner(
            queue=queue,
            turn_executor=ExplorerTurnExecutor(
                explorer=H5CompletionAwareExplorer(
                    delegate=H5PlatformSelectedExplorer(
                        h5_visual_coordinate_mode=visual_coordinate_mode,
                    ),
                    h5_visual_coordinate_mode=visual_coordinate_mode,
                ),
                capture_coordinator=capture_runtime.h5_exploration_coordinator,
                before_side_effect=self._cancellation.before_side_effect,
            ),
            continuation=workflow,
            recovery=H5CoordinatorFrontierRecovery(
                capture_coordinator=capture_runtime.h5_exploration_coordinator,
                detector=detector,
                assessment_repository=assessment_repository,
                cancellation=self._cancellation,
                h5_visual_coordinate_mode=visual_coordinate_mode,
                source_task_id=record.task_id,
                surface_sessions=capture_runtime.surface_sessions,
                captures=capture_runtime.h5_capture_recovery,
                page_graph=PostgresPageGraphRepository(),
            ),
            browser_loss_recovery=H5CoordinatorBrowserLossRecovery(
                capture_runtime=capture_runtime,
                detector=detector,
                workflow=workflow,
                action_ledger=PostgresActionExecutionRepository(),
                session_lineage=session_lineage,
                cancellation=self._cancellation,
            ),
            lease_owner=f"{self._worker_instance_id}:h5-explorer",
            cancellation=self._cancellation,
            session_lineage=session_lineage,
            emit_wakeup=False,
            session_command_gate=capture_runtime.session_command_gate,
            adaptive_journey=adaptive_journey,
        )
        return H5V2Runtime(
            capture_runtime=capture_runtime,
            workflow=workflow,
            turn_runner=runner,
            runtime_settings=runtime_settings,
            cancellation=self._cancellation,
            session_lineage=session_lineage,
            fast_review_probe=PostgresH5FastReviewProbe(),
            active_session_inspection=(
                build_default_h5_active_session_inspection_drain_service(
                    worker_instance_id=self._worker_instance_id,
                    settings=settings,
                    supplemental_evidence_policy=self._supplemental_evidence_policy,
                    capture_runtime=capture_runtime,
                    detector=detector,
                    assessment_repository=assessment_repository,
                    recapture_budget=ExplorationBudget(
                        max_actions=min(runtime_settings.max_actions, 3),
                        remaining_actions=min(runtime_settings.max_actions, 3),
                        max_depth=min(runtime_settings.max_depth, 2),
                        remaining_duration_ms=min(
                            int(runtime_settings.max_duration_seconds * 1_000),
                            180_000,
                        ),
                    ),
                    cancellation=self._cancellation,
                )
            ),
        )


def _build_h5_auth_bootstrap(
    *,
    target_url: str,
    settings: Settings,
    capture_runtime: WebCaptureRuntime,
    detector: PersistingH5PageDetector,
) -> H5AuthBootstrapPort:
    """按开关和锁定目标域名装配 H5 主动登录；关闭时保持原探索路径。"""
    if not settings.h5_auth.enabled:
        return NoopH5AuthBootstrap()
    config_path = settings.h5_auth.config_path
    if config_path is None:
        raise RuntimeError("启用 H5 主动登录时缺少配置文件路径")
    hostname = urlsplit(target_url).hostname
    if hostname is None:
        raise RuntimeError("H5 目标 URL 缺少 hostname")
    credential_resolver = FileH5CredentialConfigResolver.from_path(
        config_path,
        authorized_hostnames=(hostname,),
    )
    snapshot_reader = capture_runtime.trusted_ui_snapshot_reader
    return H5AuthBootstrapService(
        credential_resolver=credential_resolver,
        page_reader=DetectedH5AuthPageReader(
            detector=detector,
            snapshot_reader=snapshot_reader,
            analyzer=H5AuthPageAnalyzer(),
        ),
        navigator=CoordinatedH5AuthNavigation(
            coordinator=capture_runtime.h5_exploration_coordinator,
        ),
        secret_runtime=capture_runtime.coordinate_secret_input_runtime(
            PlaywrightH5SecretInputRuntime(
                credential_resolver=credential_resolver,
                snapshot_reader=snapshot_reader,
                target_resolver=capture_runtime.h5_target_resolver,
                browser=capture_runtime.h5_secret_form_browser,
                session_command_gate=capture_runtime.session_command_gate,
            )
        ),
        audit=SurfacePhaseH5AuthAudit(
            repository=PostgresSurfacePhaseRepository(),
            session_command_gate=capture_runtime.session_command_gate,
        ),
    )


class H5AuditedUsageReader(Protocol):
    async def usage_totals_for_task(self, *, task_id: str) -> tuple[int, int]: ...


class ProductionH5V2RuntimeExecutor:
    """执行首采判定、纯 Explorer 循环和 Session 关闭。"""

    def __init__(
        self,
        *,
        worker_instance_id: str,
        active_session_inspection: object | None = None,
        now: Callable[[], datetime] | None = None,
        retry_wait_seconds: float = _DEFAULT_RETRY_WAIT_SECONDS,
        max_retry_reclaims: int = _DEFAULT_MAX_RETRY_RECLAIMS,
        audited_usage_reader: H5AuditedUsageReader | None = None,
        physical_budget_repository: object | None = None,
    ) -> None:
        if not worker_instance_id or worker_instance_id != worker_instance_id.strip():
            raise ValueError("worker_instance_id 必须为非空且无首尾空白的字符串")
        if retry_wait_seconds < 0 or max_retry_reclaims < 0:
            raise ValueError("H5 重试配置不能为负数")
        self._worker_instance_id = worker_instance_id
        self._active_session_inspection = active_session_inspection
        self._now = now or (lambda: datetime.now(UTC))
        self._retry_wait_seconds = retry_wait_seconds
        self._max_retry_reclaims = max_retry_reclaims
        self._audited_usage_reader = audited_usage_reader
        self._physical_budget_repository = physical_budget_repository

    async def run(
        self,
        runtime: H5RuntimePort,
        *,
        record: AgentRunRecord,
        settings: Settings,
        progress_recorder: object | None = None,
    ) -> RunResult:
        del settings, progress_recorder
        runtime_settings = runtime.runtime_settings
        if not isinstance(runtime_settings, H5TaskRuntimeSettings):
            raise TypeError("H5 Runtime 必须携带 H5TaskRuntimeSettings")
        execution_mode = task_execution_mode_from_config(record.task_config)
        task_target = task_target_from_dict((record.task_config or {}).get("_task_target"))
        h5_incremental_inspection = (
            execution_mode is TaskExecutionMode.FULL_INSPECTION
            and task_target.platform is TaskTargetPlatform.H5
        )
        target_url = _h5_target_url(record)
        session_id = f"h5:{record.run_id}"
        now = self._now()
        deadline = (record.started_at or now) + timedelta(
            seconds=runtime_settings.max_duration_seconds
        )
        if now >= deadline:
            return _failed_result(message="h5_task_deadline_exhausted", step_count=0)
        command = RunH5WorkflowCommand(
            task_id=record.task_id,
            agent_run_id=record.run_id,
            session_id=session_id,
            target_url=target_url,
            # 归属标识不可带 worker 实例号：同一 Session 可能被不同 Worker 接手，
            # 且闸门侧续期必须给出同一个值，否则 register_active 匹配不到行。
            session_owner=H5_SURFACE_SESSION_OWNER,
            objective=_h5_exploration_objective(runtime_settings),
            boundaries=_h5_boundaries(target_url, max_depth=runtime_settings.max_depth),
            budget=ExplorationBudget(
                max_actions=runtime_settings.max_actions,
                remaining_actions=runtime_settings.max_actions,
                max_depth=runtime_settings.max_depth,
                remaining_duration_ms=max(0, int((deadline - now).total_seconds() * 1_000)),
            ),
            sampling_policy_summary=runtime_settings.sampling_policy_summary,
            observed_at=now,
            deadline=deadline,
        )
        outcome = H5ExplorationTurnOutcome(claimed=False)
        auth_result = None
        consumed_turns = 0
        failure: Exception | None = None
        session_attempted = False
        context = AgentRunContext(
            task_id=record.task_id,
            agent_run_id=record.run_id,
            task_config=dict(record.task_config or {}),
            h5_llm_budget=True,
        )
        with bind_agent_run_context(context):
            try:
                if self._physical_budget_repository is not None:
                    await self._physical_budget_repository.initialize(
                        task_id=record.task_id, agent_run_id=record.run_id,
                        max_calls=runtime_settings.max_ai_calls, deadline=command.deadline,
                    )
                await _raise_if_runtime_canceled(
                    runtime,
                    task_id=record.task_id,
                    agent_run_id=record.run_id,
                    stage="before_session_open",
                )
                budget_error = await self._usage_budget_error(
                    runtime_settings, task_id=record.task_id,
                )
                if budget_error is not None:
                    return _outcome_result(
                        H5ExplorationTurnOutcome(
                            claimed=False, final_status=ExplorerWorkStatus.BUDGET_EXHAUSTED,
                            error_code=budget_error,
                        ),
                        step_count=0,
                    )
                session_attempted = True
                start_result = await runtime.start(command)
                auth_result = getattr(start_result, "auth_result", None)
                if auth_result is not None and auth_result.needs_review:
                    outcome = _fast_review_outcome(
                        error_code=f"{H5_AUTH_REVIEW_ERROR_PREFIX}{auth_result.reason_code}"
                    )
                else:
                    if h5_incremental_inspection:
                        await self._inspect_current_h5_surface(
                            runtime,
                            task_id=record.task_id,
                            agent_run_id=record.run_id,
                            deadline=command.deadline,
                        )
                    fast_review = await _read_h5_fast_review_snapshot(
                        runtime,
                        task_id=record.task_id,
                        agent_run_id=record.run_id,
                    )
                    fast_review_error_code = _fast_review_h5_coverage_error_code(fast_review)
                    if fast_review_error_code is not None:
                        outcome = _fast_review_outcome(error_code=fast_review_error_code)
                    else:
                        outcome, consumed_turns = await self._consume(
                            runtime,
                            task_id=record.task_id,
                            agent_run_id=record.run_id,
                            runtime_settings=runtime_settings,
                            deadline=command.deadline,
                            inspect_current_surface=h5_incremental_inspection,
                        )
            except Exception as exc:  # noqa: BLE001 - 组合根统一收敛外部失败
                failure = exc
            finally:
                if session_attempted:
                    shutdown_canceled = None
                    try:
                        async with asyncio.timeout(_H5_SHUTDOWN_TIMEOUT_SECONDS):
                            await self._prepare_runtime_close(
                                runtime,
                                task_id=record.task_id,
                            )
                    except asyncio.CancelledError as exc:
                        shutdown_canceled = exc
                    except H5ExplorationCanceled as exc:
                        failure = exc
                    except Exception as exc:  # noqa: BLE001 - 冻结失败必须阻止伪成功
                        if failure is None:
                            failure = exc
                        else:
                            logger.exception(
                                "task_id=%s run_id=%s H5 失败收场时冻结探索也失败。",
                                record.task_id,
                                record.run_id,
                            )
                    if (
                        execution_mode is TaskExecutionMode.FULL_INSPECTION
                        and shutdown_canceled is None
                    ):
                        try:
                            # 截止后仅允许排空器读取和结算；禁止无限卡在外部调用。
                            async with asyncio.timeout(max(
                                _H5_SHUTDOWN_TIMEOUT_SECONDS,
                                (command.deadline - self._now()).total_seconds(),
                            )):
                                await self._drain_active_session_inspection(
                                    runtime,
                                    task_id=record.task_id,
                                    agent_run_id=record.run_id,
                                    deadline=command.deadline,
                                )
                        except asyncio.CancelledError as exc:
                            shutdown_canceled = exc
                        except H5ExplorationCanceled as exc:
                            failure = exc
                        except Exception as exc:  # noqa: BLE001 - 规则排空失败必须阻止伪成功
                            if failure is None:
                                failure = exc
                            else:
                                logger.exception(
                                    "task_id=%s run_id=%s H5 失败收场时活会话规则排空也失败。",
                                    record.task_id,
                                    record.run_id,
                                )
                    try:
                        async with asyncio.timeout(_H5_SHUTDOWN_TIMEOUT_SECONDS):
                            await runtime.close(task_id=record.task_id, session_id=session_id)
                    except Exception as exc:  # noqa: BLE001 - 关闭失败也必须进入人工复核
                        if failure is None:
                            failure = exc
                    if shutdown_canceled is not None:
                        raise shutdown_canceled
        if isinstance(failure, H5ExplorationCanceled):
            return RunResult(
                termination_reason=TerminationReason.CANCELED,
                working_memory=WorkingMemory(step_count=consumed_turns),
                trace_sink=InMemoryActionTraceSink(),
                final_text="H5 探索已取消",
                error_message=str(failure),
                execution_outcome=ExecutionOutcome.NEEDS_REVIEW,
            )
        if failure is not None:
            cause_type, error_number = _failure_fingerprint(failure)
            failure_stage, failure_error_code = _failure_context(failure)
            logger.error(
                "H5 V2 Runtime 执行失败：task_id=%s run_id=%s "
                "failure_type=%s stage=%s error_code=%s cause_type=%s errno=%s "
                "call_path=%s detail=%s",
                record.task_id,
                record.run_id,
                type(failure).__name__,
                failure_stage,
                failure_error_code,
                cause_type,
                error_number,
                # 仅记录文件名、函数和行号，不展开异常链、源码或局部变量。
                " > ".join(
                    f"{frame.filename.rsplit('/', 1)[-1]}:{frame.name}:{frame.lineno}"
                    for frame in traceback.extract_tb(failure.__traceback__)[-8:]
                ) or "unavailable",
                # 只有指纹没有消息时，SURFACE_DRIVER_FAILED 这类错误无从定位
                str(_failure_message(failure))[:800],
            )
            message = _failure_message(failure)
            if outcome.error_code and outcome.error_code not in message:
                # 探索首因与收尾失败均须保留，不能用未排空异常掩盖预算或恢复原因。
                message = f"{outcome.error_code}; H5 收尾失败：{message}"
            return _failed_result(
                message=message,
                step_count=consumed_turns,
            )
        return _outcome_result(
            outcome,
            step_count=consumed_turns,
            auth_result=auth_result,
        )

    @staticmethod
    async def _prepare_runtime_close(
        runtime: H5RuntimePort,
        *,
        task_id: str,
    ) -> None:
        prepare_close = getattr(runtime, "prepare_close", None)
        if callable(prepare_close):
            await prepare_close(task_id=task_id)

    async def _drain_active_session_inspection(
        self,
        runtime: H5RuntimePort,
        *,
        task_id: str,
        agent_run_id: str,
        deadline: datetime,
    ) -> None:
        drainer = self._active_session_inspection
        if drainer is not None:
            await drainer.drain_before_close(
                task_id=task_id,
                agent_run_id=agent_run_id,
                deadline=deadline,
            )
            return
        runtime_drainer = getattr(runtime, "drain_active_session_inspection", None)
        if callable(runtime_drainer):
            await runtime_drainer(
                task_id=task_id,
                agent_run_id=agent_run_id,
                deadline=deadline,
            )

    async def _inspect_current_h5_surface(
        self,
        runtime: H5RuntimePort,
        *,
        task_id: str,
        agent_run_id: str,
        deadline: datetime,
    ) -> None:
        checkpoint = getattr(runtime, "inspect_current_h5_surface", None)
        if callable(checkpoint):
            await checkpoint(
                task_id=task_id,
                agent_run_id=agent_run_id,
                deadline=deadline,
            )

    async def _consume(
        self,
        runtime: H5RuntimePort,
        *,
        task_id: str,
        agent_run_id: str,
        runtime_settings: H5TaskRuntimeSettings,
        deadline: datetime,
        inspect_current_surface: bool,
    ) -> tuple[H5ExplorationTurnOutcome, int]:
        outcome = H5ExplorationTurnOutcome(claimed=False)
        consumed = 0
        retries = 0
        lease_reclaims = 0
        while consumed < runtime_settings.max_actions:
            if self._now() >= deadline:
                outcome = H5ExplorationTurnOutcome(
                    claimed=False, final_status=ExplorerWorkStatus.BUDGET_EXHAUSTED,
                    error_code="h5_task_deadline_exhausted",
                )
                break
            budget_error = await self._usage_budget_error(
                runtime_settings, task_id=task_id,
            )
            if budget_error is not None:
                outcome = H5ExplorationTurnOutcome(
                    claimed=False,
                    final_status=ExplorerWorkStatus.BUDGET_EXHAUSTED,
                    error_code=budget_error,
                )
                break
            try:
                outcome = await runtime.run_one_turn(
                    task_id=task_id,
                    agent_run_id=agent_run_id,
                )
            except AppExplorationLeaseLostError:
                if lease_reclaims >= self._max_retry_reclaims:
                    outcome = _fast_review_outcome(
                        error_code=H5_EXPLORATION_NO_CLAIMABLE_WORK_REVIEW_CODE
                    )
                    break
                next_claim_at = await runtime.next_exploration_claim_at(task_id=task_id)
                if next_claim_at is None:
                    fast_review = await _read_h5_fast_review_snapshot(
                        runtime,
                        task_id=task_id,
                        agent_run_id=agent_run_id,
                    )
                    outcome = _fast_review_outcome(
                        error_code=(
                            _fast_review_h5_coverage_error_code(fast_review)
                            or H5_EXPLORATION_NO_CLAIMABLE_WORK_REVIEW_CODE
                        )
                    )
                    break
                lease_reclaims += 1
                remaining = (next_claim_at - self._now()).total_seconds()
                if remaining > 0:
                    await asyncio.sleep(min(
                        remaining, max(0, (deadline - self._now()).total_seconds())
                    ))
                continue
            if outcome.claimed:
                consumed += 1
                if inspect_current_surface and self._now() < deadline:
                    await self._inspect_current_h5_surface(
                        runtime,
                        task_id=task_id,
                        agent_run_id=agent_run_id,
                        deadline=deadline,
                    )
            fast_review = await _read_h5_fast_review_snapshot(
                runtime,
                task_id=task_id,
                agent_run_id=agent_run_id,
            )
            fast_review_error_code = _fast_review_h5_coverage_error_code(fast_review)
            if fast_review_error_code is not None and (
                not outcome.claimed or _requires_follow_up(outcome)
            ):
                outcome = _fast_review_outcome(error_code=fast_review_error_code)
                break
            if _requires_follow_up(outcome):
                budget_error = await self._usage_budget_error(
                    runtime_settings, task_id=task_id,
                )
                if budget_error is not None:
                    outcome = H5ExplorationTurnOutcome(
                        claimed=outcome.claimed,
                        work_id=outcome.work_id,
                        final_status=ExplorerWorkStatus.BUDGET_EXHAUSTED,
                        turn=outcome.turn,
                        result_capture_id=outcome.result_capture_id,
                        successor_work_id=outcome.successor_work_id,
                        error_code=budget_error,
                    )
                    break
            if outcome.final_status is ExplorerWorkStatus.RETRY_WAIT:
                # 连续重投上限防的是原地打转的基础设施抖动；换一条 Frontier
                # sibling 继续探索是实质进展（上一条已被永久判死），不计入上限，
                # 否则前沿还剩几条就被 2 次连投掐死（实测 91蜜桃剩 6 条即收场）。
                # 外层 consumed < max_actions 仍是总预算兜底。
                if outcome.reselected_sibling or outcome.restored_source:
                    retries = 0
                    if self._retry_wait_seconds:
                        await asyncio.sleep(min(self._retry_wait_seconds,
                            max(0, (deadline - self._now()).total_seconds())))
                    continue
                if retries >= self._max_retry_reclaims:
                    break
                retries += 1
                if self._retry_wait_seconds:
                    await asyncio.sleep(min(self._retry_wait_seconds,
                        max(0, (deadline - self._now()).total_seconds())))
                continue
            retries = 0
            if not _requires_follow_up(outcome):
                break
        if consumed >= runtime_settings.max_actions and _requires_follow_up(outcome):
            outcome = H5ExplorationTurnOutcome(
                claimed=outcome.claimed,
                work_id=outcome.work_id,
                final_status=ExplorerWorkStatus.BUDGET_EXHAUSTED,
                turn=outcome.turn,
                result_capture_id=outcome.result_capture_id,
                successor_work_id=outcome.successor_work_id,
                error_code=H5_ACTION_BUDGET_EXHAUSTED_REVIEW_CODE,
            )
        return outcome, consumed

    async def _usage_budget_error(
        self,
        settings: H5TaskRuntimeSettings,
        *,
        task_id: str,
    ) -> str | None:
        meter = get_current_llm_usage_meter()
        if self._audited_usage_reader is not None and meter is not None:
            await meter.flush_audit_events(timeout_seconds=5.0)
            if any(
                not task.done() or task.cancelled() or task.exception() is not None
                for task in meter.audit_tasks
            ):
                raise RuntimeError("h5_llm_usage_audit_unavailable")
        audited_usage = (
            await self._audited_usage_reader.usage_totals_for_task(
                task_id=task_id,
            )
            if self._audited_usage_reader is not None
            else None
        )
        return _h5_usage_budget_error(settings, audited_usage=audited_usage)


def build_h5_workflow_handler(
    settings: Settings,
    worker_instance_id: str,
    *,
    provider: H5RuntimeProvider | None = None,
    executor: H5V2RuntimeExecutor | None = None,
    cancellation: H5CancellationPort | None = None,
    supplemental_evidence_policy: SupplementalEvidencePolicy | None = None,
) -> H5WorkflowHandler:
    """构建 Worker 生命周期内复用的唯一 H5 V2 入口。"""
    if settings.workflow.h5_v2_mode != "on":
        raise ValueError("只有 h5_v2_mode=on 才能构建正式 H5 V2 Handler")
    if executor is None:
        from services.api.app.infrastructure.db.h5_llm_budget_repository import (
            PostgresH5LLMBudgetRepository,
        )

        executor = ProductionH5V2RuntimeExecutor(
            worker_instance_id=worker_instance_id,
            audited_usage_reader=PostgresLLMUsageEventRepository(),
            physical_budget_repository=PostgresH5LLMBudgetRepository(),
        )
    return RuntimeBackedH5WorkflowHandler(
        provider=provider
        or PostgresH5RuntimeProvider(
            worker_instance_id=worker_instance_id,
            cancellation=cancellation,
            supplemental_evidence_policy=supplemental_evidence_policy,
        ),
        executor=executor,
    )


def _h5_target_url(record: AgentRunRecord) -> str:
    payload = (record.task_config or {}).get("_task_target")
    target = task_target_from_dict(payload)
    # Web 与 H5 共用浏览器统一管线（路由层已显式放行，平台身份保留在任务行）。
    if (
        target.platform not in (TaskTargetPlatform.H5, TaskTargetPlatform.WEB)
        or not target.target_url
    ):
        raise ValueError("H5 V2 Runtime 只接受已锁定 target_url 的浏览器平台任务（h5/web）")
    return target.target_url


def _trusted_entry_origin(work: ExplorationWork) -> str:
    parsed = urlsplit(work.frontier.observation.location)
    if (
        parsed.scheme.casefold() not in {"http", "https"}
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ValueError("H5 恢复入口必须为 HTTP(S) URL")
    if not work.boundaries.allowed_hosts:
        raise ValueError("H5 恢复缺少允许主机边界")
    host = (parsed.hostname or "").casefold()
    if host not in {item.casefold() for item in work.boundaries.allowed_hosts}:
        raise ValueError("H5 恢复入口主机不在持久化允许边界")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("H5 恢复入口端口无效") from exc
    default_port = 443 if parsed.scheme.casefold() == "https" else 80
    suffix = f":{port}" if port is not None and port != default_port else ""
    return f"{parsed.scheme.casefold()}://{host}{suffix}"


def _same_h5_location(left: str, right: str) -> bool:
    return canonical_h5_route_identity(left) == canonical_h5_route_identity(right)


def _require_failed_work(context: H5RecoveryContext) -> ExplorationWork:
    if not isinstance(context.failed_work, ExplorationWork):
        raise RuntimeError("H5 浏览器恢复缺少失败 Work")
    return context.failed_work


def _h5_boundaries(target_url: str, *, max_depth: int) -> ExplorationBoundaries:
    host = (urlsplit(target_url).hostname or "").strip().casefold()
    if not host:
        raise ValueError("H5 target_url 缺少有效主机名")
    return ExplorationBoundaries(
        allowed_hosts=(host,),
        allowed_operations=(
            InteractionOperation.CLICK,
            InteractionOperation.SCROLL,
            InteractionOperation.BACK,
            InteractionOperation.OBSERVE,
            InteractionOperation.NAVIGATE,
            InteractionOperation.SEEK_MEDIA,
        ),
        max_navigation_depth=max_depth,
        side_effect_policy=SideEffectPolicy.READ_ONLY,
    )


async def _raise_if_runtime_canceled(
    runtime: object,
    *,
    task_id: str,
    agent_run_id: str,
    stage: str,
) -> None:
    checkpoint = getattr(runtime, "cancellation", None)
    if isinstance(checkpoint, H5CancellationCheckpoint):
        await checkpoint.raise_if_requested(
            task_id=task_id,
            agent_run_id=agent_run_id,
            stage=stage,
        )


def _validate_h5_selected_candidate_binding(
    *,
    work: ExplorationWork,
    decision: ActionIntent,
    h5_visual_coordinate_mode: Literal["off", "shadow", "enforced"] = "off",
) -> None:
    """阻止模型绕过已持久化的 H5 Frontier selected entry。"""
    payload = work.frontier.observation.metadata.get(H5_TASK_FRONTIER_METADATA_KEY)
    if payload is None:
        return
    if not isinstance(payload, Mapping):
        raise ValueError("H5 任务 Frontier 快照无效")
    snapshot = H5TaskFrontierSnapshot.from_dict(payload)
    selected = [
        entry for entry in snapshot.entries if entry.status is H5FrontierEntryStatus.SELECTED
    ]
    selected_entry_id = work.frontier.observation.metadata.get("h5_task_frontier_selected_entry_id")
    if (
        len(selected) != 1
        or selected_entry_id != selected[0].entry_id
        or tuple(work.frontier.candidates) != (selected[0].candidate,)
    ):
        raise ValueError("ActionIntent 必须绑定 selected H5 Frontier candidate")
    candidate = selected[0].candidate
    if decision.parameters.get("candidate_id") != candidate.candidate_id:
        raise ValueError("ActionIntent 必须绑定 selected H5 Frontier candidate")
    if decision.action_type is not candidate.action_type:
        raise ValueError("ActionIntent action_type 与 selected H5 Frontier candidate 不一致")
    if decision.semantic_target != candidate.semantic_target:
        raise ValueError("ActionIntent semantic_target 与 selected H5 Frontier candidate 不一致")
    if decision.risk_level is not candidate.risk_level:
        raise ValueError("ActionIntent risk_level 与 selected H5 Frontier candidate 不一致")
    if (
        candidate.expected_surface_kind is not None
        and decision.surface_kind is not candidate.expected_surface_kind
    ):
        raise ValueError("ActionIntent surface_kind 与 selected H5 Frontier candidate 不一致")
    if decision.parameters.get("navigation_depth") != candidate.navigation_depth:
        raise ValueError("ActionIntent navigation_depth 与 selected H5 Frontier candidate 不一致")
    if decision.purpose != h5_candidate_purpose(candidate):
        raise ValueError("ActionIntent purpose 与 selected H5 Frontier candidate 不一致")
    expected_parameters = _h5_candidate_parameters(
        work=work,
        candidate=candidate,
        h5_visual_coordinate_mode=h5_visual_coordinate_mode,
    )
    actual_parameters = thaw_json_mapping(decision.parameters)
    fallback = actual_parameters.pop("explorer_decision_fallback", None)
    if fallback is not None:
        if not isinstance(fallback, Mapping) or (
            not decision.intent_id.startswith("h5-platform-fallback:")
            or fallback.get("schema_version") != "h5-explorer-decision-fallback.v1"
            or fallback.get("reason_code")
            not in {
                ExplorerAgentOutputError.error_code,
                ExplorerPrematureTerminalError.error_code,
                ExplorerModelUnavailableError.error_code,
            }
        ):
            raise ValueError("ActionIntent 模型降级审计无效")
    if actual_parameters != expected_parameters:
        raise ValueError("ActionIntent parameters 与 selected H5 Frontier candidate 不一致")


def _trusted_startup_ad_candidate_parameters(
    *,
    work: ExplorationWork,
    candidate,
) -> dict[str, str]:
    return _trusted_startup_ad_candidate_parameters_for_page(
        page_type=work.frontier.observation.page_type,
        candidate=candidate,
    )


def _trusted_age_verification_candidate_parameters(
    *,
    work: ExplorationWork,
    candidate,
) -> dict[str, str]:
    """Runtime 重验完整的 H5 成年确认双向合同。"""
    expected = {
        "target_mode": H5_AGE_VERIFICATION_TARGET_MODE,
        "age_verification_capability": H5_AGE_VERIFICATION_CAPABILITY,
    }
    keywords = candidate.metadata.get("target_keywords")
    normalized_keywords = (
        {str(item).strip() for item in keywords if str(item).strip()}
        if isinstance(keywords, (list, tuple, set, frozenset))
        else set()
    )
    if (
        candidate.metadata.get("action_parameters") != expected
        or work.frontier.observation.page_type is not None
        or work.frontier.observation.page_state.casefold() != H5_AGE_VERIFICATION_PAGE_STATE
        or candidate.semantic_target != H5_AGE_VERIFICATION_TARGET
        or candidate.action_type is not InteractionOperation.CLICK
        or candidate.risk_level is not InteractionRiskLevel.LOW
        or candidate.navigation_depth != 0
        or candidate.metadata.get("purpose") != H5_AGE_VERIFICATION_PURPOSE
        or not H5_AGE_VERIFICATION_KEYWORDS.issubset(normalized_keywords)
    ):
        return {}
    return expected


def _trusted_line_loading_reobserve_candidate_parameters(
    *,
    work: ExplorationWork,
    candidate,
) -> dict[str, Any]:
    """重验平台签发的线路页同会话重观察参数。"""
    attempt = candidate.metadata.get("line_loading_reobserve_attempt")
    if isinstance(attempt, bool) or not isinstance(attempt, int) or not 1 <= attempt <= 3:
        return {}
    expected = {
        "target_mode": "line_loading_reobserve",
        "line_loading_reobserve_attempt": attempt,
    }
    keywords = candidate.metadata.get("target_keywords")
    normalized_keywords = (
        {str(item).strip().casefold() for item in keywords if str(item).strip()}
        if isinstance(keywords, (list, tuple, set, frozenset))
        else set()
    )
    if (
        candidate.metadata.get("action_parameters") != expected
        or (work.frontier.observation.page_type or "").casefold() != "line_loading"
        # 页面模型可能把线路壳上的“登录失败/重试”对话框收敛为
        # line_loading/login_required；平台仍只签发无副作用的同会话观察，
        # Runtime 必须接受该自产候选，避免在执行前被双向契约误拒绝。
        or work.frontier.observation.page_state.casefold()
        not in {"loading", "line_loading", "login_required"}
        or candidate.semantic_target != "重新观察线路加载结果"
        or candidate.action_type is not InteractionOperation.OBSERVE
        or candidate.risk_level is not InteractionRiskLevel.LOW
        or candidate.navigation_depth != 0
        or not {"line_loading", "same_session_reobserve"}.issubset(normalized_keywords)
    ):
        return {}
    return expected


def _trusted_line_loading_login_retry_candidate_parameters(
    *,
    work: ExplorationWork,
    candidate,
) -> dict[str, str]:
    """重验精确线路登录失败 Dialog 签发的一次性重试能力。"""
    raw = candidate.metadata.get("action_parameters")
    signature = candidate.metadata.get("obstruction_signature")
    expected = {
        "target_mode": H5_LINE_LOADING_LOGIN_RETRY_TARGET_MODE,
        "line_loading_login_retry_capability": (H5_LINE_LOADING_LOGIN_RETRY_CAPABILITY),
        "obstruction_signature": signature,
    }
    keywords = candidate.metadata.get("target_keywords")
    normalized_keywords = (
        {str(item).strip() for item in keywords if str(item).strip()}
        if isinstance(keywords, (list, tuple, set, frozenset))
        else set()
    )
    if (
        not isinstance(signature, str)
        or not _valid_obstruction_signature(signature)
        or raw != expected
        or (work.frontier.observation.page_type or "").casefold() != "line_loading"
        or work.frontier.observation.page_state.casefold() != "login_required"
        or candidate.semantic_target != H5_LINE_LOADING_LOGIN_RETRY_TARGET
        or candidate.action_type is not InteractionOperation.CLICK
        or candidate.risk_level is not InteractionRiskLevel.LOW
        or candidate.navigation_depth != 0
        or candidate.metadata.get("purpose") != H5_LINE_LOADING_LOGIN_RETRY_PURPOSE
        or not H5_LINE_LOADING_LOGIN_RETRY_KEYWORDS.issubset(normalized_keywords)
    ):
        return {}
    return expected


def _trusted_startup_ad_countdown_reobserve_candidate_parameters(
    *,
    work: ExplorationWork,
    candidate,
) -> dict[str, Any]:
    """重验平台签发的开屏广告同会话重观察参数。"""
    attempt = candidate.metadata.get("startup_ad_countdown_reobserve_attempt")
    if isinstance(attempt, bool) or not isinstance(attempt, int) or not 1 <= attempt <= 10:
        return {}
    expected = {
        "target_mode": "startup_ad_countdown_reobserve",
        "startup_ad_countdown_reobserve_attempt": attempt,
    }
    keywords = candidate.metadata.get("target_keywords")
    normalized_keywords = (
        {str(item).strip().casefold() for item in keywords if str(item).strip()}
        if isinstance(keywords, (list, tuple, set, frozenset))
        else set()
    )
    if (
        candidate.metadata.get("action_parameters") != expected
        or (work.frontier.observation.page_type or "").casefold() != "startup_ad"
        or work.frontier.observation.page_state.casefold()
        not in {
            "countdown",
            "countdown_active",
            "stable",
            "ad_visible",
            "popup",
            "stable_ad_visible",
            "startup_ad_visible",
            "countdown_ad",
            "stable_ad_with_skip_countdown",
        }
        or candidate.semantic_target != "重新观察启动广告倒计时"
        or candidate.action_type is not InteractionOperation.OBSERVE
        or candidate.risk_level is not InteractionRiskLevel.LOW
        or candidate.navigation_depth != 0
        or not {
            "startup_ad",
            "countdown",
            "same_session_reobserve",
        }.issubset(normalized_keywords)
    ):
        return {}
    return expected


def _trusted_flutter_bootstrap_reobserve_candidate_parameters(
    *,
    work: ExplorationWork,
    candidate,
) -> dict[str, Any]:
    """重验平台签发的 Flutter 空启动壳同会话重观察参数。"""
    attempt = candidate.metadata.get("flutter_bootstrap_reobserve_attempt")
    if isinstance(attempt, bool) or not isinstance(attempt, int) or not 1 <= attempt <= 5:
        return {}
    expected = {
        "target_mode": "flutter_bootstrap_reobserve",
        "flutter_bootstrap_reobserve_attempt": attempt,
    }
    keywords = candidate.metadata.get("target_keywords")
    normalized_keywords = (
        {str(item).strip().casefold() for item in keywords if str(item).strip()}
        if isinstance(keywords, (list, tuple, set, frozenset))
        else set()
    )
    risk_flags = work.frontier.observation.metadata.get("risk_flags")
    normalized_risk_flags = (
        {str(item).strip().casefold() for item in risk_flags if str(item).strip()}
        if isinstance(risk_flags, (list, tuple, set, frozenset))
        else set()
    )
    if (
        candidate.metadata.get("action_parameters") != expected
        or work.frontier.observation.page_type is not None
        or work.frontier.observation.page_state.casefold() != "needs_more_observation"
        or work.frontier.observation.metadata.get("detection_status") != "insufficient"
        or "deterministic_flutter_bootstrap_pending" not in normalized_risk_flags
        or candidate.semantic_target != "重新观察 Flutter 启动页"
        or candidate.action_type is not InteractionOperation.OBSERVE
        or candidate.risk_level is not InteractionRiskLevel.LOW
        or candidate.navigation_depth != 0
        or not {"flutter_bootstrap", "same_session_reobserve"}.issubset(normalized_keywords)
    ):
        return {}
    return expected


def _trusted_insufficient_reobserve_candidate_parameters(
    *,
    work: ExplorationWork,
    candidate,
) -> dict[str, Any]:
    """重验平台签发的普通未稳定页同会话重观察参数。"""

    attempt = candidate.metadata.get("insufficient_reobserve_attempt")
    if isinstance(attempt, bool) or not isinstance(attempt, int) or not 1 <= attempt <= 3:
        return {}
    expected = {
        "target_mode": "insufficient_reobserve",
        "insufficient_reobserve_attempt": attempt,
    }
    keywords = candidate.metadata.get("target_keywords")
    normalized_keywords = (
        {str(item).strip().casefold() for item in keywords if str(item).strip()}
        if isinstance(keywords, (list, tuple, set, frozenset))
        else set()
    )
    risk_flags = work.frontier.observation.metadata.get("risk_flags")
    normalized_risk_flags = (
        {str(item).strip().casefold() for item in risk_flags if str(item).strip()}
        if isinstance(risk_flags, (list, tuple, set, frozenset))
        else set()
    )
    if (
        candidate.metadata.get("action_parameters") != expected
        or work.frontier.observation.page_type is not None
        or work.frontier.observation.page_state.casefold() != "needs_more_observation"
        or work.frontier.observation.metadata.get("detection_status") != "insufficient"
        or "deterministic_flutter_bootstrap_pending" in normalized_risk_flags
        or candidate.semantic_target != "重新观察未稳定页面"
        or candidate.action_type is not InteractionOperation.OBSERVE
        or candidate.risk_level is not InteractionRiskLevel.LOW
        or candidate.navigation_depth != 0
        or candidate.metadata.get("purpose") != "等待页面稳定，并在当前浏览器会话重新采集页面。"
        or not {"insufficient", "same_session_reobserve"}.issubset(normalized_keywords)
    ):
        return {}
    return expected


def _trusted_ambiguous_launch_shell_reobserve_candidate_parameters(
    *,
    work: ExplorationWork,
    candidate,
) -> dict[str, Any]:
    """重验平台签发的模糊启动壳层零触碰复观察参数。"""
    attempt = candidate.metadata.get("ambiguous_launch_shell_reobserve_attempt")
    if isinstance(attempt, bool) or not isinstance(attempt, int) or not 1 <= attempt <= 3:
        return {}
    expected = {
        "target_mode": "ambiguous_launch_shell_reobserve",
        "ambiguous_launch_shell_reobserve_attempt": attempt,
    }
    keywords = candidate.metadata.get("target_keywords")
    normalized_keywords = (
        {str(item).strip().casefold() for item in keywords if str(item).strip()}
        if isinstance(keywords, (list, tuple, set, frozenset))
        else set()
    )
    observation = work.frontier.observation
    if (
        candidate.metadata.get("action_parameters") != expected
        or observation.page_type is not None
        or observation.page_state.casefold() != "loading"
        or observation.metadata.get("detection_status") != "ambiguous"
        or observation.metadata.get("ambiguous_launch_shell") is not True
        or candidate.semantic_target != "重新观察模糊启动壳层"
        or candidate.action_type is not InteractionOperation.OBSERVE
        or candidate.risk_level is not InteractionRiskLevel.LOW
        or candidate.navigation_depth != 0
        or candidate.metadata.get("purpose")
        != "启动壳层页型仍有冲突，在当前浏览器会话零触碰重新采集。"
        or not {
            "ambiguous_launch_shell",
            "same_session_reobserve",
            "zero_touch",
        }.issubset(normalized_keywords)
    ):
        return {}
    return expected


def _trusted_logical_surface_reobserve_candidate_parameters(
    *,
    work: ExplorationWork,
    candidate,
) -> dict[str, Any]:
    """重验确认逻辑页面上由 Frontier 签发的同会话复验参数。"""
    attempt = candidate.metadata.get("logical_reobserve_attempt")
    if isinstance(attempt, bool) or not isinstance(attempt, int) or not 1 <= attempt <= 2:
        return {}
    expected = {
        "target_mode": "logical_surface_reobserve",
        "logical_reobserve_attempt": attempt,
    }
    keywords = candidate.metadata.get("target_keywords")
    normalized_keywords = (
        {str(item).strip().casefold() for item in keywords if str(item).strip()}
        if isinstance(keywords, (list, tuple, set, frozenset))
        else set()
    )
    metadata = work.frontier.observation.metadata
    source_identity = candidate.metadata.get("source_identity_fingerprint")
    source_state = candidate.metadata.get("source_state_fingerprint")
    if (
        candidate.metadata.get("action_parameters") != expected
        or source_identity != metadata.get("logical_surface_identity_fingerprint")
        or source_state != metadata.get("logical_surface_state_fingerprint")
        or metadata.get("logical_transition_proof") not in {"uncertain", "insufficient"}
        or candidate.semantic_target != "重新观察 SPA/Flutter 逻辑页面转换"
        or candidate.action_type is not InteractionOperation.OBSERVE
        or candidate.risk_level is not InteractionRiskLevel.LOW
        or candidate.metadata.get("purpose") != "复验 SPA/Flutter 逻辑页面转换"
        or not {"logical_surface", "same_session_reobserve"}.issubset(normalized_keywords)
    ):
        return {}
    return expected


def _trusted_capability_projection_reobserve_candidate_parameters(
    *,
    work: ExplorationWork,
    candidate,
) -> dict[str, Any]:
    """重验平台因能力投影拒绝而签发的同会话重观察。"""
    attempt = candidate.metadata.get("capability_projection_reobserve_attempt")
    if isinstance(attempt, bool) or not isinstance(attempt, int) or not 1 <= attempt <= 3:
        return {}
    expected = {
        "target_mode": "capability_projection_reobserve",
        "capability_projection_reobserve_attempt": attempt,
    }
    keywords = candidate.metadata.get("target_keywords")
    normalized_keywords = (
        {str(item).strip().casefold() for item in keywords if str(item).strip()}
        if isinstance(keywords, (list, tuple, set, frozenset))
        else set()
    )
    rejections = work.frontier.observation.metadata.get("capability_projection_rejections")
    if (
        candidate.metadata.get("action_parameters") != expected
        or not h5_page_state_supports_popup_dismissal(work.frontier.observation.page_state)
        or not isinstance(rejections, (list, tuple))
        or not rejections
        or candidate.semantic_target != "重新观察能力投影失败页面"
        or candidate.action_type is not InteractionOperation.OBSERVE
        or candidate.risk_level is not InteractionRiskLevel.LOW
        or not {
            "capability_projection_rejected",
            "same_session_reobserve",
        }.issubset(normalized_keywords)
    ):
        return {}
    return expected


def _trusted_startup_ad_candidate_parameters_for_page(
    *,
    page_type: str | None,
    candidate,
) -> dict[str, str]:
    raw = candidate.metadata.get("action_parameters")
    if not isinstance(raw, Mapping):
        return {}
    signature = raw.get("obstruction_signature")
    if not _valid_obstruction_signature(signature):
        return {}
    expected = {
        "target_mode": "startup_ad_dismissal",
        "startup_ad_capability": "countdown_close",
        "obstruction_signature": signature,
    }
    keywords = candidate.metadata.get("target_keywords")
    normalized_keywords = (
        {str(item).strip().casefold() for item in keywords if str(item).strip()}
        if isinstance(keywords, (list, tuple, set, frozenset))
        else set()
    )
    if raw != expected:
        return {}
    if (
        page_type is None
        or page_type.casefold() != "startup_ad"
        or candidate.semantic_target != "跳过"
        or candidate.action_type is not InteractionOperation.CLICK
        or candidate.risk_level is not InteractionRiskLevel.LOW
        or candidate.metadata.get("expected_next_page_type") != "home"
        or not {
            "startup_ad",
            "close_control",
            "startup_ad_dismissal",
        }.issubset(normalized_keywords)
    ):
        return {}
    return dict(expected)


def _trusted_popup_candidate_parameters(
    *,
    work: ExplorationWork,
    candidate,
) -> dict[str, str]:
    raw = candidate.metadata.get("action_parameters")
    if not isinstance(raw, Mapping):
        return {}
    capability = raw.get("popup_dismissal_capability")
    signature = raw.get("obstruction_signature")
    if not isinstance(capability, str) or not isinstance(signature, str):
        return {}
    return _trusted_popup_candidate_parameters_for_state(
        page_state=work.frontier.observation.page_state,
        candidate=candidate,
    )


def _trusted_popup_candidate_parameters_for_state(
    *,
    page_state: str,
    candidate,
) -> dict[str, str]:
    raw = candidate.metadata.get("action_parameters")
    if not isinstance(raw, Mapping):
        return {}
    capability = raw.get("popup_dismissal_capability")
    signature = raw.get("obstruction_signature")
    if not isinstance(capability, str) or not isinstance(signature, str):
        return {}
    expected = {
        "target_mode": "popup_dismissal",
        "popup_dismissal_capability": capability,
        "obstruction_signature": signature,
    }
    target_keywords = candidate.metadata.get("target_keywords")
    normalized_keywords = (
        {str(item).strip() for item in target_keywords if str(item).strip()}
        if isinstance(target_keywords, (list, tuple, set, frozenset))
        else set()
    )
    if (
        dict(raw) != expected
        or capability not in _POPUP_DISMISSAL_CAPABILITIES
        or not _valid_obstruction_signature(signature)
        or candidate.metadata.get("obstruction_signature") != signature
        or not h5_page_state_supports_popup_dismissal(page_state)
        or candidate.semantic_target != _POPUP_DISMISSAL_TARGET
        or candidate.action_type is not InteractionOperation.CLICK
        or candidate.risk_level is not InteractionRiskLevel.LOW
        or candidate.navigation_depth != 0
        or not {"popup_dismissal", "close_control", capability}.issubset(normalized_keywords)
    ):
        return {}
    return expected


def _trusted_profile_payment_visual_candidate_parameters(
    *,
    work: ExplorationWork,
    candidate,
) -> dict[str, object]:
    """重验“我的”页白名单入口的截图坐标证据，拒绝模型扩展参数。"""
    raw = candidate.metadata.get("action_parameters")
    if not isinstance(raw, Mapping):
        return {}
    expected_keys = {
        "target_mode",
        "visual_entry_kind",
        "visual_source_capture_id",
        "visual_source_url",
        "visual_source_artifact_id",
        "visual_source_sha256",
        "screenshot_index",
        "left_ratio",
        "top_ratio",
        "right_ratio",
        "bottom_ratio",
        "visual_confidence",
    }
    if set(raw) != expected_keys:
        return {}
    capture_id = raw.get("visual_source_capture_id")
    source_url = raw.get("visual_source_url")
    artifact_id = raw.get("visual_source_artifact_id")
    checksum = raw.get("visual_source_sha256")
    screenshot_index = raw.get("screenshot_index")
    contract = h5_profile_visual_entry_contract(
        candidate.semantic_target,
        entry_kind=raw.get("visual_entry_kind"),
    )
    if (
        raw.get("target_mode") != "visual_coordinate"
        or contract is None
        or not isinstance(capture_id, str)
        or not capture_id.strip()
        or not isinstance(source_url, str)
        or not source_url.strip()
        or not _same_h5_location(source_url, work.frontier.observation.location)
        or not isinstance(artifact_id, str)
        or not artifact_id.strip()
        or len(artifact_id) > 256
        or not isinstance(checksum, str)
        or not checksum.startswith("sha256:")
        or not _valid_sha256_digest(checksum.removeprefix("sha256:"))
        or isinstance(screenshot_index, bool)
        or not isinstance(screenshot_index, int)
        or screenshot_index < 1
    ):
        return {}
    ratios = tuple(
        _trusted_visual_ratio(raw.get(key))
        for key in (
            "left_ratio",
            "top_ratio",
            "right_ratio",
            "bottom_ratio",
            "visual_confidence",
        )
    )
    if any(value is None for value in ratios):
        return {}
    left, top, right, bottom, confidence = ratios
    assert left is not None
    assert top is not None
    assert right is not None
    assert bottom is not None
    assert confidence is not None
    keywords = candidate.metadata.get("target_keywords")
    normalized_keywords = (
        {str(item).strip() for item in keywords if str(item).strip()}
        if isinstance(keywords, (list, tuple, set, frozenset))
        else set()
    )
    if (
        left >= right
        or top >= bottom
        or confidence < contract.minimum_confidence
        or (right - left) * (bottom - top) > contract.maximum_area_ratio
        or (work.frontier.observation.page_type or "").casefold() != "profile"
        or work.frontier.observation.page_state.casefold() not in {"stable", "normal", "loaded"}
        or candidate.metadata.get("source_page_type") != "profile"
        or candidate.metadata.get("expected_next_page_type") != contract.expected_page_type
        or candidate.action_type is not InteractionOperation.CLICK
        or candidate.risk_level is not InteractionRiskLevel.MEDIUM
        or candidate.navigation_depth != 1
        or not {
            contract.target_keyword,
            "profile_local_action",
            f"page_type:{contract.expected_page_type}",
            f"entry:{contract.target}",
            "evidence:screenshot",
        }.issubset(normalized_keywords)
    ):
        return {}
    return dict(raw)


def _trusted_visual_ratio(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    number = float(value)
    return number if math.isfinite(number) and 0 <= number <= 1 else None


def _valid_sha256_digest(value: str) -> bool:
    return len(value) == 64 and all(character in "0123456789abcdef" for character in value)


def _valid_obstruction_signature(value: object) -> bool:
    if not isinstance(value, str) or not value.startswith("h5-obstruction:"):
        return False
    digest = value.removeprefix("h5-obstruction:")
    return len(digest) == 64 and all(character in "0123456789abcdef" for character in digest)


def _trusted_structural_navigation_candidate_parameters(
    *,
    work: ExplorationWork,
    candidate,
) -> dict[str, object]:
    raw = candidate.metadata.get("action_parameters")
    if not isinstance(raw, Mapping):
        return {}
    target_mode = raw.get("target_mode")
    navigation_index = raw.get("navigation_index")
    navigation_label = raw.get("navigation_label")
    navigation_family = (
        h5_bottom_navigation_family(raw.get("navigation_family"))
        if target_mode == "bottom_navigation"
        else None
    )
    interaction_pattern_signature = raw.get("interaction_pattern_signature")
    adaptive_pattern = bool(
        (
            target_mode == "page_tab_navigation"
            and interaction_pattern_signature is not None
        )
        or (
            target_mode == "bottom_navigation"
            and navigation_family == H5_BOTTOM_NAVIGATION_FAMILY_ADAPTIVE
        )
    )
    if (
        target_mode not in {"bottom_navigation", "page_tab_navigation"}
        or isinstance(navigation_index, bool)
        or not isinstance(navigation_index, int)
        or navigation_index < 1
        or not isinstance(navigation_label, str)
        or not navigation_label.strip()
        or (target_mode == "bottom_navigation" and navigation_family is None)
        or (target_mode == "page_tab_navigation" and raw.get("navigation_family") is not None)
        or (
            adaptive_pattern
            and (
                not isinstance(interaction_pattern_signature, str)
                or not _valid_sha256_digest(interaction_pattern_signature)
            )
        )
    ):
        return {}
    bottom_match = (
        h5_match_bottom_navigation_target(candidate.semantic_target)
        if target_mode == "bottom_navigation"
        else None
    )
    page_tab_index = (
        h5_match_page_tab_target(candidate.semantic_target)
        if target_mode == "page_tab_navigation"
        else None
    )
    expected_raw = (
        h5_bottom_navigation_action_parameters(
            family=navigation_family,
            index=navigation_index,
            label=navigation_label,
            interaction_pattern_signature=(
                interaction_pattern_signature
                if navigation_family == H5_BOTTOM_NAVIGATION_FAMILY_ADAPTIVE
                else None
            ),
        )
        if target_mode == "bottom_navigation" and navigation_family is not None
        else {
            "target_mode": target_mode,
            "navigation_index": navigation_index,
            "navigation_label": navigation_label,
            **(
                {"interaction_pattern_signature": interaction_pattern_signature}
                if interaction_pattern_signature is not None
                else {}
            ),
        }
    )
    keywords = candidate.metadata.get("target_keywords")
    normalized_keywords = (
        {str(item).strip() for item in keywords if str(item).strip()}
        if isinstance(keywords, (list, tuple, set, frozenset))
        else set()
    )
    observation_page_type = (work.frontier.observation.page_type or "").casefold()
    observation_page_state = work.frontier.observation.page_state.casefold()
    knowledge_source = candidate.metadata.get("navigation_knowledge_source")
    knowledge_confidence = candidate.metadata.get("navigation_knowledge_confidence")
    current_targets = work.frontier.observation.metadata.get("global_bottom_navigation_targets")
    if (
        (
            target_mode == "bottom_navigation"
            and bottom_match != (navigation_family, navigation_index)
        )
        or (
            target_mode == "page_tab_navigation"
            and page_tab_index != navigation_index
        )
        or dict(raw) != expected_raw
        or candidate.action_type is not InteractionOperation.CLICK
        or candidate.risk_level is not InteractionRiskLevel.LOW
        or candidate.navigation_depth != 1
        or observation_page_type in {"", "line_loading", "startup_ad"}
        or (
            h5_page_state_has_overlay(observation_page_state)
            and not (
                target_mode == "bottom_navigation"
                and candidate.metadata.get("independent_bottom_navigation") is True
                and work.frontier.observation.metadata.get(
                    "captcha_verification_required"
                ) is not True
                and isinstance(current_targets, (list, tuple))
                and sum(
                    isinstance(target, Mapping)
                    and target.get("semantic_target") == candidate.semantic_target
                    and target.get("action_parameters") == dict(raw)
                    for target in current_targets
                ) == 1
            )
        )
        or target_mode not in normalized_keywords
        or navigation_label not in normalized_keywords
        or knowledge_source not in {"static", "exact", "vector", "contextual"}
        or isinstance(knowledge_confidence, bool)
        or not isinstance(knowledge_confidence, int | float)
        or not 0 <= float(knowledge_confidence) <= 1
    ):
        return {}
    return dict(raw)


def _trusted_list_detail_candidate_parameters(
    *,
    work: ExplorationWork,
    candidate,
) -> dict[str, str]:
    """重验列表首卡能力，拒绝模型或陈旧 Work 扩展结构 selector。"""
    raw = candidate.metadata.get("action_parameters")
    if not isinstance(raw, Mapping):
        return {}
    selector = raw.get("list_detail_selector")
    source_page_type = raw.get("source_page_type")
    expected_page_type = raw.get("expected_page_type")
    if not all(
        isinstance(value, str) and value.strip()
        for value in (selector, source_page_type, expected_page_type)
    ):
        return {}
    assert isinstance(selector, str)
    assert isinstance(source_page_type, str)
    assert isinstance(expected_page_type, str)
    expected = {
        "target_mode": "list_detail_navigation",
        "list_detail_selector": selector,
        "source_page_type": source_page_type,
        "expected_page_type": expected_page_type,
    }
    keywords = candidate.metadata.get("target_keywords")
    normalized_keywords = (
        {str(item).strip() for item in keywords if str(item).strip()}
        if isinstance(keywords, (list, tuple, set, frozenset))
        else set()
    )
    observation_page_type = (work.frontier.observation.page_type or "").casefold()
    if (
        dict(raw) != expected
        or source_page_type != observation_page_type
        or expected_page_type != h5_list_detail_expected_page_type(source_page_type)
        or not h5_list_detail_selector_allowed(
            source_page_type=source_page_type,
            selector=selector,
        )
        or work.frontier.observation.page_state.casefold()
        not in {"stable", "normal", "feed", "loaded", "notice"}
        or candidate.semantic_target != H5_LIST_DETAIL_TARGET
        or candidate.action_type is not InteractionOperation.CLICK
        or candidate.risk_level is not InteractionRiskLevel.LOW
        or candidate.navigation_depth != 2
        or candidate.metadata.get("expected_next_page_type") != expected_page_type
        or not {"nested_content", "list_detail_navigation"}.issubset(normalized_keywords)
    ):
        return {}
    return expected


def _trusted_profile_settings_candidate_parameters(
    *,
    work: ExplorationWork,
    candidate,
) -> dict[str, str]:
    """重验“我的”页设置入口结构能力，拒绝模型自造 selector 或参数。"""
    raw = candidate.metadata.get("action_parameters")
    capability = h5_profile_settings_capability_for_target(candidate.semantic_target)
    expected = {
        "target_mode": H5_PROFILE_SETTINGS_TARGET_MODE,
        "settings_entry_capability": capability,
        "source_page_type": H5_PROFILE_SETTINGS_SOURCE_PAGE_TYPE,
        "expected_page_type": H5_PROFILE_SETTINGS_EXPECTED_PAGE_TYPE,
    }
    keywords = candidate.metadata.get("target_keywords")
    normalized_keywords = (
        {str(item).strip() for item in keywords if str(item).strip()}
        if isinstance(keywords, (list, tuple, set, frozenset))
        else set()
    )
    if (
        capability is None
        or raw != expected
        or (work.frontier.observation.page_type or "").casefold()
        != H5_PROFILE_SETTINGS_SOURCE_PAGE_TYPE
        or work.frontier.observation.page_state.casefold() not in {"stable", "normal", "loaded"}
        or candidate.action_type is not InteractionOperation.CLICK
        or candidate.risk_level is not InteractionRiskLevel.LOW
        or candidate.navigation_depth != 1
        or candidate.metadata.get("source_page_type") != H5_PROFILE_SETTINGS_SOURCE_PAGE_TYPE
        or candidate.metadata.get("expected_next_page_type")
        != H5_PROFILE_SETTINGS_EXPECTED_PAGE_TYPE
        or not {"settings_entry", "profile_local_action", "page_type:settings"}.issubset(
            normalized_keywords
        )
        or not (
            {"evidence:class", "evidence:attributes", "evidence:structure"} & normalized_keywords
        )
    ):
        return {}
    return dict(expected)


def _trusted_profile_payment_candidate_parameters(
    *,
    work: ExplorationWork,
    candidate,
) -> dict[str, str]:
    """重验会员入口结构能力，拒绝 selector 或参数注入。"""
    keywords = candidate.metadata.get("target_keywords")
    normalized_keywords = (
        {str(item).strip() for item in keywords if str(item).strip()}
        if isinstance(keywords, (list, tuple, set, frozenset))
        else set()
    )
    capability = h5_profile_payment_capability_from_keywords(normalized_keywords)
    entry_label = h5_profile_payment_entry_label(capability or "")
    expected = {
        "target_mode": H5_PROFILE_PAYMENT_TARGET_MODE,
        "payment_entry_capability": capability,
        "source_page_type": H5_PROFILE_PAYMENT_SOURCE_PAGE_TYPE,
        "expected_page_type": H5_PROFILE_PAYMENT_EXPECTED_PAGE_TYPE,
    }
    if (
        capability is None
        or candidate.metadata.get("action_parameters") != expected
        or (work.frontier.observation.page_type or "").casefold()
        != H5_PROFILE_PAYMENT_SOURCE_PAGE_TYPE
        or work.frontier.observation.page_state.casefold() not in {"stable", "normal", "loaded"}
        or candidate.semantic_target != H5_PROFILE_PAYMENT_TARGET
        or candidate.action_type is not InteractionOperation.CLICK
        or candidate.risk_level is not InteractionRiskLevel.LOW
        or candidate.navigation_depth != 1
        or candidate.metadata.get("source_page_type") != H5_PROFILE_PAYMENT_SOURCE_PAGE_TYPE
        or candidate.metadata.get("expected_next_page_type")
        != H5_PROFILE_PAYMENT_EXPECTED_PAGE_TYPE
        or candidate.metadata.get("purpose") != f"探索我的页会员支付入口：{entry_label}"
        or not {
            "profile_vip_payment_entry",
            "profile_local_action",
            "page_type:payment",
            f"entry:{entry_label}",
            "evidence:structure",
        }.issubset(normalized_keywords)
    ):
        return {}
    return {key: str(value) for key, value in expected.items()}


def _trusted_payment_horizontal_collection_candidate_parameters(
    *,
    work: ExplorationWork,
    candidate: CandidateAction,
) -> dict[str, object]:
    """重验会员横向集合只读边界，拒绝扩大段数或改为点击。"""
    source_page_type = (work.frontier.observation.page_type or "").strip().casefold()
    source_page_state = work.frontier.observation.page_state.strip().casefold()
    if not h5_payment_horizontal_context_supported(
        page_type=source_page_type,
        page_state=source_page_state,
    ):
        return {}
    expected = h5_payment_horizontal_action_parameters(
        source_page_type=source_page_type,
    )
    keywords = candidate.metadata.get("target_keywords")
    normalized_keywords = (
        {str(item).strip() for item in keywords if str(item).strip()}
        if isinstance(keywords, (list, tuple, set, frozenset))
        else set()
    )
    if (
        candidate.metadata.get("action_parameters") != expected
        or candidate.semantic_target != H5_PAYMENT_HORIZONTAL_TARGET
        or candidate.action_type is not InteractionOperation.SCROLL
        or candidate.risk_level is not InteractionRiskLevel.LOW
        or candidate.navigation_depth != 0
        or candidate.metadata.get("source_page_type") != source_page_type
        or candidate.metadata.get("purpose")
        != h5_payment_horizontal_purpose(source_page_type)
        or not set(h5_payment_horizontal_keywords(source_page_type)).issubset(
            normalized_keywords
        )
    ):
        return {}
    return dict(expected)


def _trusted_settings_page_back_candidate_parameters(
    *,
    work: ExplorationWork,
    candidate,
) -> dict[str, str]:
    """重验 Flutter 设置页页面内返回能力，拒绝普通 back 冒用。"""
    expected = {
        "target_mode": H5_SETTINGS_PAGE_BACK_TARGET_MODE,
        "settings_page_back_capability": H5_SETTINGS_PAGE_BACK_CAPABILITY,
        "source_page_type": H5_SETTINGS_PAGE_BACK_SOURCE_PAGE_TYPE,
    }
    keywords = candidate.metadata.get("target_keywords")
    normalized_keywords = (
        {str(item).strip() for item in keywords if str(item).strip()}
        if isinstance(keywords, (list, tuple, set, frozenset))
        else set()
    )
    if (
        candidate.metadata.get("action_parameters") != expected
        or (work.frontier.observation.page_type or "").casefold()
        != H5_SETTINGS_PAGE_BACK_SOURCE_PAGE_TYPE
        or work.frontier.observation.page_state.casefold() not in {"stable", "normal", "loaded"}
        or candidate.semantic_target != "browser_history"
        or candidate.action_type is not InteractionOperation.BACK
        or candidate.risk_level is not InteractionRiskLevel.LOW
        or candidate.navigation_depth != 1
        or candidate.metadata.get("source_page_type") != H5_SETTINGS_PAGE_BACK_SOURCE_PAGE_TYPE
        or not {
            "nested_return",
            H5_SETTINGS_PAGE_BACK_CAPABILITY,
            "evidence:structure",
        }.issubset(normalized_keywords)
    ):
        return {}
    return dict(expected)


def _trusted_profile_favorites_page_back_candidate_parameters(
    *,
    work: ExplorationWork,
    candidate,
) -> dict[str, str]:
    """重验“我的收藏”全屏页返回能力，拒绝普通列表 back 冒用。"""
    source_page_type = (work.frontier.observation.page_type or "").casefold()
    expected = {
        "target_mode": H5_PROFILE_FAVORITES_PAGE_BACK_TARGET_MODE,
        "profile_favorites_page_back_capability": (H5_PROFILE_FAVORITES_PAGE_BACK_CAPABILITY),
        "source_page_type": source_page_type,
        "expected_page_type": H5_PROFILE_FAVORITES_PAGE_BACK_EXPECTED_PAGE_TYPE,
    }
    keywords = candidate.metadata.get("target_keywords")
    normalized_keywords = (
        {str(item).strip() for item in keywords if str(item).strip()}
        if isinstance(keywords, (list, tuple, set, frozenset))
        else set()
    )
    if (
        candidate.metadata.get("action_parameters") != expected
        or not h5_profile_favorites_page_back_source_type_allowed(source_page_type)
        or work.frontier.observation.page_state.casefold()
        not in {"stable", "normal", "loaded", "stable_empty", "empty_state", "empty"}
        or candidate.semantic_target != "browser_history"
        or candidate.action_type is not InteractionOperation.BACK
        or candidate.risk_level is not InteractionRiskLevel.LOW
        or candidate.navigation_depth != 1
        or candidate.metadata.get("source_page_type") != source_page_type
        or candidate.metadata.get("expected_next_page_type")
        != H5_PROFILE_FAVORITES_PAGE_BACK_EXPECTED_PAGE_TYPE
        or not {
            "nested_return",
            H5_PROFILE_FAVORITES_PAGE_BACK_CAPABILITY,
            "evidence:structure",
        }.issubset(normalized_keywords)
    ):
        return {}
    return dict(expected)


def _trusted_profile_activity_candidate_parameters(
    *,
    work: ExplorationWork,
    candidate,
) -> dict[str, str]:
    """重验个人中心签到结构能力，拒绝模型自造活动入口。"""
    raw = candidate.metadata.get("action_parameters")
    capability = h5_profile_activity_capability_for_target(candidate.semantic_target)
    expected = {
        "target_mode": H5_PROFILE_ACTIVITY_TARGET_MODE,
        "activity_entry_capability": capability,
        "source_page_type": H5_PROFILE_ACTIVITY_SOURCE_PAGE_TYPE,
        "expected_page_type": H5_PROFILE_ACTIVITY_EXPECTED_PAGE_TYPE,
    }
    keywords = candidate.metadata.get("target_keywords")
    normalized_keywords = (
        {str(item).strip() for item in keywords if str(item).strip()}
        if isinstance(keywords, (list, tuple, set, frozenset))
        else set()
    )
    if (
        capability is None
        or raw != expected
        or (work.frontier.observation.page_type or "").casefold()
        != H5_PROFILE_ACTIVITY_SOURCE_PAGE_TYPE
        or work.frontier.observation.page_state.casefold() not in {"stable", "normal", "loaded"}
        or candidate.action_type is not InteractionOperation.CLICK
        or candidate.risk_level is not InteractionRiskLevel.LOW
        or candidate.navigation_depth != 1
        or candidate.metadata.get("source_page_type") != H5_PROFILE_ACTIVITY_SOURCE_PAGE_TYPE
        or candidate.metadata.get("expected_next_page_type")
        != H5_PROFILE_ACTIVITY_EXPECTED_PAGE_TYPE
        or not {
            "activity_entry",
            "profile_local_action",
            "page_type:activity",
            "evidence:structure",
        }.issubset(normalized_keywords)
    ):
        return {}
    return dict(expected)


def _trusted_recapture_handoff_candidate_parameters(
    *, work: ExplorationWork, candidate: CandidateAction,
) -> dict[str, Any]:
    """交接只允许同帧普通观察，不携带补采、坐标或导航参数。"""
    expected = {"target_mode": "h5_recapture_business_handoff"}
    observation = work.frontier.observation
    if (
        candidate.metadata.get("action_parameters") != expected
        or candidate.candidate_id != "h5-handoff:" + stable_protocol_hash(
            {"capture_id": observation.capture_id}
        )
        or candidate.action_type is not InteractionOperation.OBSERVE
        or candidate.risk_level is not InteractionRiskLevel.LOW
        or candidate.expected_surface_kind is not SurfaceKind.WEB
        or candidate.metadata.get("source_page_type") != observation.page_type
        or candidate.metadata.get("expected_next_page_type") != observation.page_type
        or not h5_observation_allows_completion(observation)
    ):
        return {}
    return expected


def _trusted_probe_display_candidate_parameters(
    *, work: ExplorationWork, candidate
) -> dict[str, Any]:
    """定向展示目标必须由当前补证帧签发，不能降级为模型文本点击。"""
    from services.api.app.application.h5_probe_targets import PROBE_TARGET_LABELS, PROBE_TARGET_MODE

    raw = candidate.metadata.get("action_parameters")
    if not isinstance(raw, Mapping):
        return {}
    goal = raw.get("goal_capability")
    if (
        goal not in PROBE_TARGET_LABELS
        or raw.get("target_mode") != PROBE_TARGET_MODE
        or set(raw) != {"target_mode", "goal_capability", "source_page_type", "probe_subject_key"}
        or not isinstance(raw.get("probe_subject_key"), str)
        or not re.fullmatch(r"[0-9a-f]{64}", raw["probe_subject_key"])
        or candidate.semantic_target != PROBE_TARGET_LABELS[goal]
        or candidate.action_type is not InteractionOperation.CLICK
        or candidate.risk_level is not InteractionRiskLevel.LOW
        or candidate.navigation_depth != 0
        or candidate.expected_surface_kind is not SurfaceKind.WEB
        or raw["source_page_type"] != work.frontier.observation.page_type
        or work.frontier.observation.page_state != "stable"
        or candidate.candidate_id
        != "h5-probe-target:" + stable_protocol_hash({"capture_id": work.source_capture_id, **raw})
    ):
        return {}
    return dict(raw)


def _trusted_h5_capability_candidate_parameters(
    *,
    work: ExplorationWork,
    candidate,
) -> dict[str, Any]:
    """仅把候选交给其声明能力对应的校验器，避免无关校验器读取运行态。"""
    raw = candidate.metadata.get("action_parameters")
    if not isinstance(raw, Mapping):
        return {}
    validator = {
        "h5_probe_display_target": _trusted_probe_display_candidate_parameters,
        "h5_recapture_business_handoff": _trusted_recapture_handoff_candidate_parameters,
        H5_AGE_VERIFICATION_TARGET_MODE: (_trusted_age_verification_candidate_parameters),
        H5_LINE_LOADING_LOGIN_RETRY_TARGET_MODE: (
            _trusted_line_loading_login_retry_candidate_parameters
        ),
        "line_loading_reobserve": _trusted_line_loading_reobserve_candidate_parameters,
        "startup_ad_countdown_reobserve": (
            _trusted_startup_ad_countdown_reobserve_candidate_parameters
        ),
        "flutter_bootstrap_reobserve": (_trusted_flutter_bootstrap_reobserve_candidate_parameters),
        "insufficient_reobserve": _trusted_insufficient_reobserve_candidate_parameters,
        "ambiguous_launch_shell_reobserve": (
            _trusted_ambiguous_launch_shell_reobserve_candidate_parameters
        ),
        "logical_surface_reobserve": (_trusted_logical_surface_reobserve_candidate_parameters),
        "capability_projection_reobserve": (
            _trusted_capability_projection_reobserve_candidate_parameters
        ),
        "visual_navigation_coordinate": (_trusted_visual_navigation_candidate_parameters),
        "startup_ad_dismissal": _trusted_startup_ad_candidate_parameters,
        "popup_dismissal": _trusted_popup_candidate_parameters,
        "visual_coordinate": _trusted_profile_payment_visual_candidate_parameters,
        "bottom_navigation": _trusted_structural_navigation_candidate_parameters,
        "page_tab_navigation": _trusted_structural_navigation_candidate_parameters,
        "list_detail_navigation": _trusted_list_detail_candidate_parameters,
        "h5_detail_scroll_coverage": _trusted_detail_scroll_candidate_parameters,
        "h5_list_scroll_coverage": _trusted_list_scroll_candidate_parameters,
        "h5_video_timed_capture": _trusted_video_timed_candidate_parameters,
        H5_PROFILE_SETTINGS_TARGET_MODE: _trusted_profile_settings_candidate_parameters,
        H5_PROFILE_PAYMENT_TARGET_MODE: _trusted_profile_payment_candidate_parameters,
        H5_PAYMENT_HORIZONTAL_TARGET_MODE: (
            _trusted_payment_horizontal_collection_candidate_parameters
        ),
        H5_SETTINGS_PAGE_BACK_TARGET_MODE: (_trusted_settings_page_back_candidate_parameters),
        H5_PROFILE_FAVORITES_PAGE_BACK_TARGET_MODE: (
            _trusted_profile_favorites_page_back_candidate_parameters
        ),
        H5_PROFILE_ACTIVITY_TARGET_MODE: _trusted_profile_activity_candidate_parameters,
    }.get(raw.get("target_mode"))
    if validator is None:
        return {}
    return validator(work=work, candidate=candidate)


def _trusted_detail_scroll_candidate_parameters(
    *,
    work: ExplorationWork,
    candidate: CandidateAction,
) -> dict[str, Any]:
    """重验 H5 详情页两屏采集边界，禁止执行端扩大滚动段数。"""
    source_page_type = (work.frontier.observation.page_type or "").strip().casefold()
    expected = {
        "target_mode": "h5_detail_scroll_coverage",
        "scroll_max_segments": 2,
        "split_scroll_screenshots": True,
        "source_page_type": source_page_type,
    }
    raw = candidate.metadata.get("action_parameters")
    keywords = candidate.metadata.get("target_keywords")
    normalized_keywords = (
        {str(item).strip().casefold() for item in keywords if str(item).strip()}
        if isinstance(keywords, (list, tuple, set, frozenset))
        else set()
    )
    if (
        source_page_type not in {"content_detail", "video_detail"}
        or work.frontier.observation.page_state.casefold() not in {"stable", "normal", "loaded"}
        or raw != expected
        or candidate.action_type is not InteractionOperation.SCROLL
        or candidate.risk_level is not InteractionRiskLevel.LOW
        or candidate.navigation_depth != 0
        or candidate.semantic_target != "采集详情页第二屏内容"
        or not {
            "detail_scroll_coverage",
            "two_screen_sequence",
            f"page_type:{source_page_type}",
        }.issubset(normalized_keywords)
    ):
        return {}
    return dict(expected)


def _trusted_list_scroll_candidate_parameters(
    *,
    work: ExplorationWork,
    candidate: CandidateAction,
) -> dict[str, Any]:
    source_page_type = (work.frontier.observation.page_type or "").strip().casefold()
    expected = {
        "target_mode": "h5_list_scroll_coverage",
        "scroll_max_segments": 8,
        "split_scroll_screenshots": True,
        "source_page_type": source_page_type,
    }
    keywords = candidate.metadata.get("target_keywords")
    if (
        source_page_type not in {"home", "content_list", "post_list", "video_list"}
        or work.frontier.observation.page_state.casefold() not in {"stable", "normal", "loaded"}
        or candidate.metadata.get("action_parameters") != expected
        or candidate.action_type is not InteractionOperation.SCROLL
        or candidate.risk_level is not InteractionRiskLevel.LOW
        or candidate.navigation_depth != 0
        or candidate.semantic_target != "采集列表至底部"
        or not isinstance(keywords, (list, tuple))
        or not {"list_scroll_coverage", "bounded_to_bottom"}.issubset(
            {str(item).casefold() for item in keywords}
        )
    ):
        return {}
    return dict(expected)


def _trusted_video_timed_candidate_parameters(
    *,
    work: ExplorationWork,
    candidate: CandidateAction,
) -> dict[str, Any]:
    expected = {"target_mode": "h5_video_timed_capture", "frame_count": 3, "interval_ms": 1000}
    keywords = candidate.metadata.get("target_keywords")
    if (
        work.frontier.observation.page_type != "video_detail"
        or work.frontier.observation.page_state.casefold() not in {"stable", "normal", "loaded"}
        or candidate.metadata.get("action_parameters") != expected
        or candidate.action_type is not InteractionOperation.TIMED_CAPTURE
        or candidate.risk_level is not InteractionRiskLevel.LOW
        or candidate.navigation_depth != 0
        or candidate.semantic_target != "采集视频播放多帧"
        or not isinstance(keywords, (list, tuple))
        or "video_timed_capture" not in {str(item).casefold() for item in keywords}
    ):
        return {}
    return dict(expected)


def _trusted_visual_navigation_candidate_parameters(
    *,
    work: ExplorationWork,
    candidate: CandidateAction,
    h5_visual_coordinate_mode: Literal["off", "shadow", "enforced"] = "off",
) -> dict[str, Any]:
    """只在 Enforced Work 上把完整密封视觉能力传给 Resolver。"""
    raw = candidate.metadata.get("action_parameters")
    if not isinstance(raw, Mapping):
        return {}
    metadata = work.frontier.observation.metadata
    capability_payload = raw.get("visual_navigation_capability")
    if (
        raw.get("target_mode") != "visual_navigation_coordinate"
        or h5_visual_coordinate_mode != "enforced"
        or metadata.get("h5_visual_coordinate_mode") != "enforced"
        or metadata.get("logical_targets_effective") is not True
        or not isinstance(capability_payload, Mapping)
    ):
        return {}
    try:
        capability = verify_visual_navigation_capability(thaw_json_mapping(capability_payload))
    except ValueError:
        return {}
    expected_risk = VISUAL_TARGET_RISK_LEVELS.get(capability.target_kind)
    if (
        candidate.action_type is not InteractionOperation.CLICK
        or candidate.semantic_target != f"进入 {capability.expected_page_type} 页面"
        or candidate.risk_level.value != expected_risk
        or candidate.metadata.get("expected_next_page_type") != capability.expected_page_type
        or work.task_id != capability.source_task_id
        or work.source_capture_id != capability.source_capture_id
        or work.frontier.observation.page_type != capability.source_page_type
        or metadata.get("logical_surface_identity_fingerprint")
        != capability.source_identity_fingerprint
        or metadata.get("logical_surface_state_fingerprint") != capability.source_state_fingerprint
    ):
        return {}
    return {
        "target_mode": "visual_navigation_coordinate",
        "visual_navigation_capability": capability.to_dict(),
    }


def _has_visual_navigation_provenance(
    *,
    candidate: CandidateAction,
    action_parameters: object,
) -> bool:
    """识别不能降级为文本点击的视觉能力或显式视觉来源标记。"""
    if isinstance(action_parameters, Mapping) and (
        "visual_navigation_capability" in action_parameters
        or action_parameters.get("target_mode") == "visual_navigation_coordinate"
        or action_parameters.get("visual_navigation_provenance") is not None
        or action_parameters.get("visual_target_kind") is not None
    ):
        return True
    return bool(
        candidate.metadata.get("visual_navigation_provenance") is not None
        or candidate.metadata.get("visual_navigation_target") is True
        or candidate.metadata.get("visual_target_kind") is not None
    )


def _logical_source_state_stale_error(
    *,
    work: ExplorationWork,
    candidate: CandidateAction,
) -> CaptureCoordinationError:
    """把合法但过期的逻辑候选收口为可重观察的无副作用失败。"""
    digest = stable_protocol_hash(
        {
            "work_id": work.work_id,
            "source_capture_id": work.source_capture_id,
            "candidate_id": candidate.candidate_id,
            "error_code": "LOGICAL_SOURCE_STATE_STALE",
        }
    ).removeprefix("sha256:")
    intent_id = f"h5-logical-source-stale:{digest}"
    message = "H5 Frontier logical target 来源页面状态已变化，需要重新观察"
    before_capture = CaptureRef(
        task_id=work.task_id,
        session_id=work.session_id,
        capture_id=work.source_capture_id,
        surface_kind=work.surface_kind,
        capture_kind=CaptureKind.PAGE,
        role="before_action",
    )
    interaction = InteractionResult(
        task_id=work.task_id,
        session_id=work.session_id,
        intent_id=intent_id,
        action_execution_id=f"action-execution:{digest}",
        source_capture_id=work.source_capture_id,
        status=InteractionResultStatus.BLOCKED,
        error_code="LOGICAL_SOURCE_STATE_STALE",
        error_message=message,
        retryable=True,
    )
    return CaptureCoordinationError(
        stage=CaptureCoordinationStage.ACTION,
        error_code="LOGICAL_SOURCE_STATE_STALE",
        message=message,
        cause_type="LogicalSourceStateStale",
        before_capture=before_capture,
        interaction=interaction,
        retryable=True,
    )


def _h5_candidate_parameters(
    *,
    work: ExplorationWork,
    candidate,
    h5_visual_coordinate_mode: Literal["off", "shadow", "enforced"] = "off",
) -> dict[str, Any]:
    """由持久化 H5 候选派生唯一可执行参数集，模型参数不得扩展。"""
    raw_action_parameters = candidate.metadata.get("action_parameters")
    visual_navigation_provenance = _has_visual_navigation_provenance(
        candidate=candidate,
        action_parameters=raw_action_parameters,
    )
    trusted_capability_parameters = (
        _trusted_visual_navigation_candidate_parameters(
            work=work,
            candidate=candidate,
            h5_visual_coordinate_mode=h5_visual_coordinate_mode,
        )
        if visual_navigation_provenance
        else _trusted_h5_capability_candidate_parameters(
            work=work,
            candidate=candidate,
        )
    )
    if visual_navigation_provenance and not trusted_capability_parameters:
        raise ValueError("H5 Frontier 视觉导航能力无效或未处于 enforced 模式")
    if (
        isinstance(raw_action_parameters, Mapping)
        and raw_action_parameters.get("target_mode") in {"bottom_navigation", "page_tab_navigation"}
        and not trusted_capability_parameters
    ):
        # 不丢参数降级点击；过期或矛盾的结构证明按原有零副作用路径有界重观察。
        raise _logical_source_state_stale_error(work=work, candidate=candidate)
    if (
        isinstance(raw_action_parameters, Mapping)
        and raw_action_parameters.get("target_mode") in {
            "h5_list_scroll_coverage", "h5_video_timed_capture",
            "h5_probe_display_target", "list_detail_navigation", "profile_payment_navigation",
        }
        and not trusted_capability_parameters
    ):
        raise ValueError("H5 Frontier 有界采集能力无效")
    parameters: dict[str, Any] = {
        **trusted_capability_parameters,
        **h5_viewport_parameters(),
        "candidate_id": candidate.candidate_id,
        "frontier_snapshot_id": work.frontier_snapshot_id,
        "navigation_depth": candidate.navigation_depth,
    }
    expected_page_type = candidate.metadata.get("expected_next_page_type")
    if expected_page_type is not None:
        if not isinstance(expected_page_type, str) or not expected_page_type.strip():
            raise ValueError("H5 Frontier candidate expected_next_page_type 无效")
        parameters["expected_next_page_type"] = expected_page_type
    if candidate.metadata.get("logical_target_required") is True:
        metadata = work.frontier.observation.metadata
        source_identity = candidate.metadata.get("source_identity_fingerprint")
        source_state = candidate.metadata.get("source_state_fingerprint")
        if (
            not isinstance(source_identity, str)
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", source_identity)
            or not isinstance(source_state, str)
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", source_state)
        ):
            raise ValueError("H5 Frontier logical target 来源状态无效")
        if (
            metadata.get("logical_targets_effective") is not True
            or source_identity != metadata.get("logical_surface_identity_fingerprint")
            or source_state != metadata.get("logical_surface_state_fingerprint")
        ):
            raise _logical_source_state_stale_error(work=work, candidate=candidate)
        parameters["logical_target"] = {
            "protocol_version": "logical_target.v1",
            "source_identity_fingerprint": source_identity,
            "source_state_fingerprint": source_state,
            "source_page_type": candidate.metadata.get("source_page_type"),
            "expected_page_type": expected_page_type,
            # 滚动保留预期页型，但验证滚动效果，不要求进入另一业务页。
            "expects_logical_navigation": (
                bool(expected_page_type)
                and candidate.action_type is not InteractionOperation.SCROLL
            ),
            **(
                {"expects_scroll_effect": True}
                if candidate.action_type is InteractionOperation.SCROLL
                else {}
            ),
        }
    if candidate.action_type is InteractionOperation.NAVIGATE:
        destination_url = candidate.metadata.get("destination_url")
        if not isinstance(destination_url, str) or not destination_url.strip():
            raise ValueError("H5 Frontier navigate candidate 缺少 destination_url")
        parameters["destination_url"] = destination_url
    if candidate.action_type is InteractionOperation.SEEK_MEDIA:
        target_fraction = candidate.metadata.get("target_fraction")
        if (
            isinstance(target_fraction, bool)
            or not isinstance(target_fraction, (int, float))
            or not 0.05 <= float(target_fraction) <= 0.95
        ):
            raise ValueError("H5 Frontier seek_media candidate 缺少有界 target_fraction")
        parameters["target_fraction"] = float(target_fraction)
    return parameters


def _h5_exploration_objective(settings: H5TaskRuntimeSettings) -> str:
    """将锁定的用户旅程加入 Explorer 目标，保留基础只读约束。"""
    if not settings.journey_steps:
        return _H5_EXPLORATION_OBJECTIVE
    return (
        _H5_EXPLORATION_OBJECTIVE
        + "\n优先覆盖锁定用户旅程步骤："
        + "、".join(settings.journey_steps)
    )


def _requires_follow_up(outcome: H5ExplorationTurnOutcome) -> bool:
    if not outcome.claimed:
        return False
    if outcome.final_status is ExplorerWorkStatus.RETRY_WAIT:
        return True
    if outcome.final_status is ExplorerWorkStatus.SUPERSEDED:
        return bool(outcome.successor_work_id)
    return (
        outcome.final_status is ExplorerWorkStatus.SUCCEEDED
        and outcome.turn is not None
        and not outcome.turn.is_terminal
        and bool(outcome.successor_work_id)
    )


def _h5_usage_budget_error(
    settings: H5TaskRuntimeSettings,
    *,
    audited_usage: tuple[int, int] | None = None,
) -> str | None:
    """合并本地未落库计量与跨消费者审计流水，避免重复相加。"""
    meter = get_current_llm_usage_meter()
    usage = meter.snapshot() if meter is not None else None
    if usage is None and audited_usage is None:
        return None
    ai_calls = max(
        usage.ai_calls if usage is not None else 0,
        audited_usage[0] if audited_usage else 0,
    )
    total_tokens = max(
        usage.total_tokens or 0 if usage is not None else 0,
        audited_usage[1] if audited_usage else 0,
    )
    if (
        settings.max_total_tokens is not None
        and total_tokens >= settings.max_total_tokens
    ):
        return "h5_llm_token_budget_exhausted"
    if ai_calls >= settings.max_ai_calls:
        return "h5_llm_call_budget_exhausted"
    return None


async def _read_h5_fast_review_snapshot(
    runtime: H5RuntimePort,
    *,
    task_id: str,
    agent_run_id: str,
) -> H5FastReviewSnapshot | None:
    reader = getattr(runtime, "read_fast_review_snapshot", None)
    if not callable(reader):
        return None
    snapshot = await reader(task_id=task_id, agent_run_id=agent_run_id)
    if snapshot is not None and not isinstance(snapshot, H5FastReviewSnapshot):
        raise TypeError("H5 快速复核探针返回值无效")
    return snapshot


def _fast_review_h5_coverage_error_code(
    snapshot: H5FastReviewSnapshot | None,
) -> str | None:
    """判定是否早停转人工，并把判据计数写进日志。

    这条早停会让整个任务不再往下探索，此前在任何地方都不留痕：排障时只看得到
    「跑了两帧就收了」，看不出是哪个计数触发的，只能靠猜。
    """
    if snapshot is None:
        return None
    error_code = snapshot.fast_review_error_code
    if error_code is None:
        return None
    logger.warning(
        "h5_fast_review_stop code=%s scope_ready=%s completion_ready=%s "
        "target_coverage_ready=%s covered_page_types=%s blocking_scope=%s "
        "inventory=%s active_work=%s claimable_work=%s retry_waiting_work=%s",
        error_code,
        snapshot.assessment_scope_ready,
        snapshot.completion_ready,
        snapshot.target_coverage_ready,
        snapshot.covered_page_type_count,
        snapshot.blocking_scope_count,
        snapshot.inventory_item_count,
        snapshot.active_work_count,
        snapshot.claimable_work_count,
        snapshot.retry_waiting_work_count,
    )
    return error_code


def _fast_review_outcome(*, error_code: str) -> H5ExplorationTurnOutcome:
    return H5ExplorationTurnOutcome(
        claimed=False,
        final_status=ExplorerWorkStatus.BLOCKED,
        error_code=error_code,
    )


def _failed_result(*, message: str, step_count: int) -> RunResult:
    return RunResult(
        termination_reason=TerminationReason.ERROR,
        working_memory=WorkingMemory(step_count=step_count),
        trace_sink=InMemoryActionTraceSink(),
        error_message=message,
        error_category=ErrorCategory.INFRASTRUCTURE,
        execution_outcome=ExecutionOutcome.NEEDS_REVIEW,
    )


def _failure_fingerprint(failure: Exception) -> tuple[str, int | None]:
    declared_cause_type = getattr(failure, "cause_type", None)
    interaction = getattr(failure, "interaction", None)
    state_summary = getattr(interaction, "state_summary", None)
    if not declared_cause_type and isinstance(state_summary, Mapping):
        declared_cause_type = state_summary.get("cause_type")
    cursor: BaseException = failure
    seen: set[int] = set()
    error_number: int | None = None
    while id(cursor) not in seen:
        seen.add(id(cursor))
        if isinstance(cursor, OSError) and isinstance(cursor.errno, int):
            error_number = cursor.errno
        next_error = cursor.__cause__ or cursor.__context__
        if next_error is None:
            break
        cursor = next_error
    cause_type = str(declared_cause_type).strip() if declared_cause_type else type(cursor).__name__
    return cause_type, error_number


def _failure_context(failure: Exception) -> tuple[str | None, str | None]:
    stage = getattr(failure, "stage", None)
    stage_value = getattr(stage, "value", stage)
    error_code = getattr(failure, "error_code", None) or h5_llm_budget_error_code(failure)
    return (
        str(stage_value).strip() if stage_value else None,
        str(error_code).strip() if error_code else None,
    )


def _failure_message(failure: Exception) -> str:
    cause_type, error_number = _failure_fingerprint(failure)
    stage, error_code = _failure_context(failure)
    details: list[str] = []
    if stage is not None:
        details.append(f"stage={stage}")
    if error_code is not None:
        details.append(f"error_code={error_code}")
    details.append(f"cause_type={cause_type}")
    if error_number is not None:
        details.append(f"errno={error_number}")
    return f"{failure} [{', '.join(details)}]"


def _outcome_result(
    outcome: H5ExplorationTurnOutcome,
    *,
    step_count: int,
    auth_result: H5AuthBootstrapResult | None = None,
) -> RunResult:
    if outcome.final_status is ExplorerWorkStatus.CANCELLED:
        return RunResult(
            termination_reason=TerminationReason.CANCELED,
            working_memory=WorkingMemory(step_count=step_count),
            trace_sink=InMemoryActionTraceSink(),
            final_text="H5 Explorer 已取消",
            error_message=outcome.error_code,
            execution_outcome=ExecutionOutcome.NEEDS_REVIEW,
        )
    if auth_result is not None and auth_result.needs_review:
        return RunResult(
            termination_reason=TerminationReason.INCOMPLETE,
            working_memory=WorkingMemory(step_count=step_count),
            trace_sink=InMemoryActionTraceSink(),
            final_text="H5 Explorer 已完成游客可达范围，认证阶段需要人工复核",
            error_message=f"{H5_AUTH_REVIEW_ERROR_PREFIX}{auth_result.reason_code}",
            execution_outcome=ExecutionOutcome.NEEDS_REVIEW,
        )
    turn = outcome.turn
    completed = (
        outcome.claimed
        and outcome.final_status is ExplorerWorkStatus.SUCCEEDED
        and turn is not None
        and turn.is_terminal
        and isinstance(turn.decision, ExplorationTerminal)
        and turn.decision.status is ExplorationTerminalStatus.COMPLETED
    )
    if completed:
        return RunResult(
            termination_reason=TerminationReason.COMPLETED,
            working_memory=WorkingMemory(step_count=step_count),
            trace_sink=InMemoryActionTraceSink(),
            final_text="H5 Explorer 已完成受限 Frontier 探索",
            execution_outcome=ExecutionOutcome.SUCCESS,
        )
    if outcome.final_status is ExplorerWorkStatus.RETRY_WAIT:
        # 走到这里说明可重试的基础设施故障（如 LLM 网关 5xx）耗尽了执行器
        # 重领预算：已探索证据仍然有效，按未完成转人工，不作为异常终结。
        return RunResult(
            termination_reason=TerminationReason.INCOMPLETE,
            working_memory=WorkingMemory(step_count=step_count),
            trace_sink=InMemoryActionTraceSink(),
            final_text="H5 Explorer 因基础设施暂时性故障重试耗尽而中止，已保留探索证据",
            error_message=outcome.error_code,
            execution_outcome=ExecutionOutcome.NEEDS_REVIEW,
        )
    if outcome.final_status in {
        ExplorerWorkStatus.BLOCKED,
        ExplorerWorkStatus.BOUNDED,
        ExplorerWorkStatus.BUDGET_EXHAUSTED,
        ExplorerWorkStatus.NO_PROGRESS,
    }:
        reason = _outcome_terminal_reason(outcome)
        error_message = outcome.error_code or reason
        if error_message == H5_COVERAGE_NO_INVENTORY_REVIEW_CODE:
            final_text = "H5 Explorer 覆盖范围无法形成可判定证据，已转人工复核"
        elif error_message == H5_COVERAGE_BLOCKED_SCOPE_REVIEW_CODE:
            final_text = "H5 Explorer 页面覆盖已到达阻塞终态，已转人工复核"
        elif error_message == H5_COVERAGE_WORK_DEAD_END_REVIEW_CODE:
            final_text = "H5 Explorer 可执行工作已耗尽但覆盖未完成，已转人工复核"
        elif error_message == H5_EXPLORATION_NO_CLAIMABLE_WORK_REVIEW_CODE:
            final_text = "H5 Explorer 未发现可继续领取的探索工作，已转人工复核"
        elif error_message == H5_ACTION_BUDGET_EXHAUSTED_REVIEW_CODE:
            final_text = "H5 Explorer 已达到本次动作预算上限，已转人工复核"
        elif _is_auth_blocked_reason(error_message):
            final_text = "H5 Explorer 遇到认证阻断，已转人工复核"
        else:
            final_text = "H5 Explorer 已到达有界探索终止条件，已转人工复核"
        return RunResult(
            termination_reason=TerminationReason.INCOMPLETE,
            working_memory=WorkingMemory(step_count=step_count),
            trace_sink=InMemoryActionTraceSink(),
            final_text=final_text,
            error_message=error_message,
            execution_outcome=ExecutionOutcome.NEEDS_REVIEW,
        )
    if outcome.final_status is ExplorerWorkStatus.FAILED:
        return _failed_result(
            message=outcome.error_code or "H5 Explorer 单轮执行未成功",
            step_count=step_count,
        )
    return RunResult(
        termination_reason=TerminationReason.INCOMPLETE,
        working_memory=WorkingMemory(step_count=step_count),
        trace_sink=InMemoryActionTraceSink(),
        final_text="H5 Explorer 尚未形成 completed 终态",
        error_message=outcome.error_code,
        execution_outcome=ExecutionOutcome.NEEDS_REVIEW,
    )


def _outcome_terminal_reason(outcome: H5ExplorationTurnOutcome) -> str | None:
    turn = outcome.turn
    decision = getattr(turn, "decision", None) if turn is not None else None
    if isinstance(decision, ExplorationTerminal):
        return decision.reason
    return None


def _is_auth_blocked_reason(reason: str | None) -> bool:
    if not reason:
        return False
    normalized = reason.casefold()
    return (
        normalized.startswith(H5_AUTH_REVIEW_ERROR_PREFIX)
        or "requires_auth" in normalized
        or "authentication_required" in normalized
    )


def is_h5_auth_review_error(error_message: str | None) -> bool:
    """只识别认证引导产生的安全原因码，避免误截断普通探索复核。"""
    return bool(
        error_message
        and error_message.startswith(H5_AUTH_REVIEW_ERROR_PREFIX)
        and len(error_message) > len(H5_AUTH_REVIEW_ERROR_PREFIX)
    )
