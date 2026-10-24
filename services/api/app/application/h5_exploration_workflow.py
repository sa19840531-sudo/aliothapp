"""H5 V2 首次导航、页面判定与后继 Exploration Work 编排。"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any, Literal, Protocol

from services.api.app.application.bounded_exploration_scheduler import (
    BOUNDED_EXPLORATION_STATE_KEY,
    BoundedExplorationScheduler,
    BoundedExplorationState,
    ExplorationStopReason,
    evidence_gap_page_types_from_facts,
    scheduler_state_from_metadata,
    update_scheduler_metadata,
)
from services.api.app.application.capture_coordination import (
    CaptureCoordinationCommand,
    CaptureCoordinationError,
    CaptureCoordinationResult,
    CaptureCoordinationStage,
)
from services.api.app.application.capture_coordinator import CaptureCoordinator
from services.api.app.application.exploration_scheduler_facts import (
    ExplorationSchedulerFactsReader,
    merge_exploration_scheduler_facts,
)
from services.api.app.application.explorer_turn import ExplorerTurnResult
from services.api.app.application.h5_adaptive_navigation import (
    H5InteractionPatternKnowledgePort,
)
from services.api.app.application.h5_auth_bootstrap import (
    H5AuthBootstrapCommand,
    H5AuthBootstrapPort,
    NoopH5AuthBootstrap,
)
from services.api.app.application.h5_exploration_observation import (
    _candidate_reobservation_identity,
    build_h5_frontier,
    h5_observation_allows_completion,
)
from services.api.app.application.h5_exploration_recovery import (
    H5BrowserRecoveryAttemptFacts,
    H5BrowserRecoveryFacts,
    H5FailureStage,
)
from services.api.app.application.h5_page_type_assessment_adapter import (
    H5_PAGE_TYPE_ASSESSMENT_ADAPTER_VERSION,
    H5PageTypeAssessmentAdapterResult,
    adapt_h5_page_detection_to_assessment,
)
from services.api.app.application.h5_popup_loop_policy import (
    next_popup_dismissal_reobserve_state,
)
from services.api.app.application.h5_safety_interstitial import (
    H5_SAFETY_INTERSTITIAL_CAPABILITY,
    H5_SAFETY_INTERSTITIAL_ERROR_CODE,
    H5_SAFETY_INTERSTITIAL_TARGET,
    H5_SAFETY_INTERSTITIAL_TARGET_MODE,
    h5_detection_is_safety_interstitial,
)
from services.api.app.application.h5_viewport import h5_viewport_parameters
from services.api.app.application.orchestration import StartExplorationCommand
from services.api.app.application.surface_sessions import SurfaceSessionRepository
from services.api.app.domain.exploration import (
    CandidateAction,
    ExplorationBoundaries,
    ExplorationBudget,
    ExplorationWork,
    H5AuthBootstrapResult,
)
from services.api.app.domain.exploration.h5_coverage import (
    H5_COVERAGE_METADATA_KEY,
    H5CoverageSnapshot,
)
from services.api.app.domain.exploration.h5_frontier import (
    H5_TASK_FRONTIER_METADATA_KEY,
    H5FrontierEntryStatus,
    H5TaskFrontierSnapshot,
    _transition_snapshot,
    canonical_h5_route_identity,
    compact_h5_task_frontier,
    is_h5_list_scroll_coverage_candidate,
    is_h5_obstruction_close_candidate,
    select_next_h5_entry,
)
from services.api.app.domain.interaction import (
    ActionIntent,
    CaptureDirective,
    CaptureProfile,
    CaptureRef,
    InteractionOperation,
    InteractionResultStatus,
    InteractionRiskLevel,
    SurfaceDriver,
    SurfaceKind,
    SurfaceSessionSpec,
)
from services.api.app.domain.protocol import require_aware_datetime, stable_protocol_hash

logger = logging.getLogger(__name__)


class H5ExplorationOrchestratorPort(Protocol):
    async def start_exploration(self, command: StartExplorationCommand) -> ExplorationWork: ...

    async def prepare_exploration(self, command: StartExplorationCommand) -> ExplorationWork: ...


class H5PageDetectorPort(Protocol):
    async def detect(
        self,
        *,
        capture: CaptureRef,
        task_id: str,
        agent_run_id: str,
    ) -> dict[str, Any]: ...


# Surface Session 的归属标识：工作流侧注册与命令闸门侧续期必须用同一个值，
# 否则 register_active 的 ON CONFLICT 因 session_owner 不匹配而返回空行，
# 报「Surface Session 归属无效」。App 侧同理固定为 "app-workflow"。
# 不可带 worker 实例号——同一 Session 可能被不同 Worker 接手。
H5_SURFACE_SESSION_OWNER = "h5-workflow"
_BOUNDED_SCHEDULER = BoundedExplorationScheduler()
_STALE_POPUP_ABSENT_REASON = "popup_obstruction_absent_on_stable_page"
_VISUAL_ACTION_REOBSERVATION_ERROR_CODES = frozenset(
    {
        "VISUAL_TARGET_SOURCE_STATE_STALE",
        "VISUAL_TARGET_NOT_UNIQUE",
        "VISUAL_TRANSITION_EVIDENCE_INSUFFICIENT",
    }
)
_POLICY_BLOCKED_ACTION_ERROR_CODES = frozenset(
    {
        "COMMAND_TARGET_RISK_UNDERRATED",
        "COMMAND_TARGET_SEMANTICS_UNTRUSTED",
    }
)
_INTERACTION_PATTERN_FAILURE_CODES = frozenset(
    {
        "BOTTOM_NAVIGATION_TRANSITION_NOT_OBSERVED",
        "CLICK_TARGET_NOT_ACTIONABLE",
        "CLICK_TARGET_NOT_FOUND",
        "SEMANTIC_TARGET_AMBIGUOUS",
        "SEMANTIC_TARGET_NOT_FOUND",
        "STRUCTURAL_NAVIGATION_TARGET_MISMATCH",
    }
)


def h5_action_failure_is_policy_blocked(error_code: str) -> bool:
    """判断动作是否在 Driver 调用前被 H5 安全策略确定性拒绝。"""
    return error_code in _POLICY_BLOCKED_ACTION_ERROR_CODES


class H5FreshCandidateRebindRejected(ValueError):
    """来源已恢复，但当前 Capture 无法为 selected sibling 建立可信新绑定。"""

    error_code = "h5_frontier_fresh_rebind_rejected"


class H5PageTypeAssessmentPersistencePort(Protocol):
    """H5 页面评估写入端口；生产组合根必须提供 PostgreSQL 实现。"""

    async def upsert(
        self,
        *,
        task_id: str,
        agent_run_id: str,
        session_id: str,
        page_capture_id: str,
        detection_result: Mapping[str, object],
        assessment_payload: Mapping[str, object],
    ) -> object: ...

    async def list_for_run(
        self,
        *,
        task_id: str,
        agent_run_id: str,
    ) -> list[object]: ...


class H5SurfaceBootstrapPort(Protocol):
    async def start(self, command: RunH5WorkflowCommand) -> CaptureCoordinationResult: ...


@dataclass(frozen=True, kw_only=True)
class RunH5WorkflowCommand:
    """H5 V2 首轮所需的锁定任务与探索边界。"""

    task_id: str
    agent_run_id: str
    session_id: str
    target_url: str
    session_owner: str
    objective: str
    boundaries: ExplorationBoundaries
    budget: ExplorationBudget
    sampling_policy_summary: dict[str, object]
    observed_at: datetime
    deadline: datetime

    def __post_init__(self) -> None:
        for field_name in (
            "task_id",
            "agent_run_id",
            "session_id",
            "target_url",
            "session_owner",
            "objective",
        ):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip() or value != value.strip():
                raise ValueError(f"{field_name} 必须为无首尾空白的非空字符串")
        if not self.target_url.startswith(("http://", "https://")):
            raise ValueError("target_url 必须为 HTTP(S) URL")
        if not isinstance(self.boundaries, ExplorationBoundaries):
            raise TypeError("boundaries 必须为 ExplorationBoundaries")
        if not isinstance(self.budget, ExplorationBudget):
            raise TypeError("budget 必须为 ExplorationBudget")
        if not isinstance(self.sampling_policy_summary, dict):
            raise TypeError("sampling_policy_summary 必须为对象")
        require_aware_datetime(self.observed_at, "observed_at")
        require_aware_datetime(self.deadline, "deadline")
        if self.deadline <= self.observed_at:
            raise ValueError("deadline 必须晚于 observed_at")


@dataclass(frozen=True, kw_only=True)
class H5SafetyInterstitialBypassResult:
    """安全中间页旁路动作及其动作后 Capture。"""

    coordination: CaptureCoordinationResult

    @property
    def final_capture(self) -> CaptureRef:
        return self.coordination.after_capture


class H5SafetyInterstitialBypassPort(Protocol):
    async def bypass(
        self,
        *,
        command: RunH5WorkflowCommand,
        source_capture: CaptureRef,
    ) -> H5SafetyInterstitialBypassResult: ...


@dataclass(frozen=True, kw_only=True)
class ContinueH5ExplorationCommand:
    """动作后 Capture 生成 H5 后继 Work 的锁定上下文。"""

    prior_work: ExplorationWork
    turn: ExplorerTurnResult

    def __post_init__(self) -> None:
        if not isinstance(self.prior_work, ExplorationWork):
            raise TypeError("prior_work 必须为 ExplorationWork")
        if self.prior_work.surface_kind is not SurfaceKind.WEB:
            raise ValueError("H5 后继只接受 Web Exploration Work")
        if not isinstance(self.turn, ExplorerTurnResult) or self.turn.coordination is None:
            raise ValueError("H5 后继需要完成动作前后 Capture 的 Explorer Turn")
        if self.turn.is_terminal:
            raise ValueError("Explorer 局部终态不能生成后继 Work")
        if (
            self.turn.work_id != self.prior_work.work_id
            or self.turn.task_id != self.prior_work.task_id
        ):
            raise ValueError("Explorer Turn 与父 Work 身份不一致")


@dataclass(frozen=True, kw_only=True)
class ReobserveH5ExplorationCommand:
    """可恢复的 Action 失败后从已提交的动作前 Capture 重建 Frontier。"""

    prior_work: ExplorationWork
    before_capture: CaptureRef
    error_code: str
    retryable: bool

    def __post_init__(self) -> None:
        if not isinstance(self.prior_work, ExplorationWork):
            raise TypeError("prior_work 必须为 ExplorationWork")
        if self.prior_work.surface_kind is not SurfaceKind.WEB:
            raise ValueError("H5 重观察只接受 Web Exploration Work")
        if not isinstance(self.before_capture, CaptureRef):
            raise TypeError("before_capture 必须为 CaptureRef")
        if (
            self.before_capture.task_id != self.prior_work.task_id
            or self.before_capture.session_id != self.prior_work.session_id
            or self.before_capture.surface_kind is not SurfaceKind.WEB
            or self.before_capture.role != "before_action"
        ):
            raise ValueError("动作前 Capture 与父 Work 归属不一致")
        if (
            not isinstance(self.error_code, str)
            or not self.error_code.strip()
            or self.error_code != self.error_code.strip()
        ):
            raise ValueError("error_code 必须为无首尾空白的非空字符串")
        if not isinstance(self.retryable, bool):
            raise TypeError("retryable 必须为布尔值")


@dataclass(frozen=True, kw_only=True)
class RecoverH5ExplorationCommand:
    """浏览器丢失后由持久化 Frontier 生成恢复 Work。"""

    prior_work: ExplorationWork
    capture: CaptureRef
    detection_result: Mapping[str, object]
    assessment: H5PageTypeAssessmentAdapterResult
    failure_stage: str
    recovery_attempt_count: int
    elapsed_ms: int
    restoration_attempt_id: str
    equivalence_assessment_id: str
    action_execution_id: str | None = None
    inventory_status: str | None = None
    equivalent: bool = True
    reason_code: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.prior_work, ExplorationWork):
            raise TypeError("prior_work 必须为 ExplorationWork")
        if not isinstance(self.capture, CaptureRef):
            raise TypeError("capture 必须为 CaptureRef")
        if self.capture.task_id != self.prior_work.task_id:
            raise ValueError("恢复 Capture 与 Work 任务不一致")
        if not isinstance(self.detection_result, Mapping):
            raise TypeError("detection_result 必须为对象")
        if not isinstance(self.assessment, H5PageTypeAssessmentAdapterResult):
            raise TypeError("assessment 必须为 H5PageTypeAssessmentAdapterResult")
        for field_name in (
            "failure_stage",
            "restoration_attempt_id",
            "equivalence_assessment_id",
        ):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip() or value != value.strip():
                raise ValueError(f"{field_name} 必须为非空且无首尾空白的字符串")
        for field_name in ("recovery_attempt_count", "elapsed_ms"):
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{field_name} 必须为非负整数")
        if self.action_execution_id is not None and not self.action_execution_id.strip():
            raise ValueError("action_execution_id 不能为空")
        if self.inventory_status not in {None, "insufficient"}:
            raise ValueError("inventory_status 不受支持")
        if not isinstance(self.equivalent, bool):
            raise TypeError("equivalent 必须为布尔值")
        if self.reason_code is not None and not self.reason_code.strip():
            raise ValueError("reason_code 不能为空")


@dataclass(frozen=True, kw_only=True)
class H5ExplorationResult:
    """页面判定、统一评估与 Exploration Work 的引用型结果。"""

    detection_result: dict[str, Any]
    page_type_assessment: H5PageTypeAssessmentAdapterResult
    work: ExplorationWork
    auth_result: H5AuthBootstrapResult | None = None
    coordination: CaptureCoordinationResult | None = None


class H5SurfaceBootstrapper:
    """建立 Web Surface Session，并以双采边界执行受信任首跳。"""

    def __init__(
        self,
        *,
        driver: SurfaceDriver,
        surface_sessions: SurfaceSessionRepository,
        capture_coordinator: CaptureCoordinator,
    ) -> None:
        self._driver = driver
        self._surface_sessions = surface_sessions
        self._capture_coordinator = capture_coordinator

    @staticmethod
    def capture_profile() -> CaptureProfile:
        return CaptureProfile(
            profile_id="h5-v2-state-transition.v1",
            before=CaptureDirective(
                role="before_action",
                artifact_types=("screenshot", "dom", "visible_text", "ui_snapshot"),
            ),
            after=CaptureDirective(
                role="after_action",
                artifact_types=(
                    "screenshot",
                    "dom",
                    "visible_text",
                    "ui_snapshot",
                    "action_trace",
                ),
            ),
        )

    async def start(self, command: RunH5WorkflowCommand) -> CaptureCoordinationResult:
        if not isinstance(command, RunH5WorkflowCommand):
            raise TypeError("command 必须为 RunH5WorkflowCommand")
        spec = SurfaceSessionSpec(
            task_id=command.task_id,
            session_id=command.session_id,
            surface_kind=SurfaceKind.WEB,
            options={
                "target_url": command.target_url,
                **h5_viewport_parameters(),
            },
        )
        session = await self._driver.start_session(spec)
        try:
            await self._surface_sessions.register_active(
                spec=spec,
                session_owner=command.session_owner,
                primary_surface_ref=session.primary_surface_ref,
            )
        except (Exception, asyncio.CancelledError) as error:
            # 登记前已启动的物理会话不能靠数据库收尾；清理失败也不能覆盖原错误。
            try:
                await self._driver.close_session(
                    session=session, reason="surface_session_registration_failed",
                )
            except (Exception, asyncio.CancelledError) as cleanup_error:
                error.add_note(f"H5 首采会话关闭失败: {cleanup_error}")
                logger.exception("H5 首采登记失败后回收异常 task_id=%s", command.task_id)
            raise
        intent_id = f"h5-bootstrap:{command.agent_run_id}"
        intent = ActionIntent(
            task_id=command.task_id,
            session_id=command.session_id,
            intent_id=intent_id,
            idempotency_key=intent_id,
            surface_kind=SurfaceKind.WEB,
            source_capture_id=f"h5-entry:{command.agent_run_id}",
            action_type=InteractionOperation.NAVIGATE,
            semantic_target="H5 任务锁定入口",
            # 用途文案不能出现“提交/支付/授权”等风控词：导航意图没有可信控件语义，
            # 命令风控会按兜底文案保守拒绝，整条 H5 探索会在第一步被拦死。
            purpose="打开任务锁定的 H5 目标 URL",
            capture_profile=self.capture_profile(),
            deadline=command.deadline,
            parameters={
                "destination_url": command.target_url,
                "initial_surface_action": True,
                **h5_viewport_parameters(),
            },
            risk_level=InteractionRiskLevel.LOW,
        )
        return await self._capture_coordinator.coordinate(
            CaptureCoordinationCommand(
                agent_run_id=command.agent_run_id,
                intent=intent,
            )
        )


class CoordinatedH5SafetyInterstitialBypass:
    """通过 CaptureCoordinator 执行已验签安全中间页旁路动作。"""

    def __init__(self, *, capture_coordinator: CaptureCoordinator) -> None:
        self._capture_coordinator = capture_coordinator

    async def bypass(
        self,
        *,
        command: RunH5WorkflowCommand,
        source_capture: CaptureRef,
    ) -> H5SafetyInterstitialBypassResult:
        intent_id = f"h5-safety-bypass:{command.agent_run_id}:{source_capture.capture_id}"
        intent = ActionIntent(
            task_id=command.task_id,
            session_id=command.session_id,
            intent_id=intent_id,
            idempotency_key=intent_id,
            surface_kind=SurfaceKind.WEB,
            source_capture_id=source_capture.capture_id,
            action_type=InteractionOperation.CLICK,
            semantic_target=H5_SAFETY_INTERSTITIAL_TARGET,
            purpose="通过 H5 安全中间页后继续正式页面探索",
            capture_profile=H5SurfaceBootstrapper.capture_profile(),
            deadline=command.deadline,
            parameters={
                "target_mode": H5_SAFETY_INTERSTITIAL_TARGET_MODE,
                "safety_interstitial_capability": H5_SAFETY_INTERSTITIAL_CAPABILITY,
                **h5_viewport_parameters(),
            },
            risk_level=InteractionRiskLevel.LOW,
        )
        return H5SafetyInterstitialBypassResult(
            coordination=await self._capture_coordinator.coordinate(
                CaptureCoordinationCommand(
                    agent_run_id=command.agent_run_id,
                    intent=intent,
                )
            )
        )


class H5ExplorationWorkflow:
    """以持久化 Capture 为唯一页面判定输入，发布首条或后继 Work。"""

    def __init__(
        self,
        *,
        bootstrapper: H5SurfaceBootstrapPort,
        auth_bootstrap: H5AuthBootstrapPort | None = None,
        safety_interstitial_bypass: H5SafetyInterstitialBypassPort | None = None,
        detector: H5PageDetectorPort,
        orchestrator: H5ExplorationOrchestratorPort,
        assessment_repository: H5PageTypeAssessmentPersistencePort,
        logical_surface_effective: bool = False,
        logical_targets_effective: bool = False,
        h5_visual_coordinate_mode: Literal["off", "shadow", "enforced"] = "off",
        scheduler_facts_reader: ExplorationSchedulerFactsReader | None = None,
        interaction_pattern_knowledge: H5InteractionPatternKnowledgePort | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if assessment_repository is None:
            raise TypeError("assessment_repository 必须显式提供 H5 评估持久化端口")
        self._bootstrapper = bootstrapper
        self._auth_bootstrap = auth_bootstrap or NoopH5AuthBootstrap()
        self._safety_interstitial_bypass = safety_interstitial_bypass
        self._detector = detector
        self._orchestrator = orchestrator
        self._assessment_repository = assessment_repository
        if not isinstance(logical_surface_effective, bool):
            raise TypeError("logical_surface_effective 必须为布尔值")
        if not isinstance(logical_targets_effective, bool):
            raise TypeError("logical_targets_effective 必须为布尔值")
        if h5_visual_coordinate_mode not in {"off", "shadow", "enforced"}:
            raise ValueError("h5_visual_coordinate_mode 必须为 off、shadow 或 enforced")
        self._logical_surface_effective = logical_surface_effective
        self._logical_targets_effective = bool(
            logical_surface_effective and logical_targets_effective
        )
        self._h5_visual_coordinate_mode = h5_visual_coordinate_mode
        self._scheduler_facts_reader = scheduler_facts_reader
        self._interaction_pattern_knowledge = interaction_pattern_knowledge
        self._now = now or (lambda: datetime.now(UTC))

    async def _record_interaction_pattern_outcome(
        self,
        *,
        candidate: CandidateAction | None,
        task_id: str,
        page_capture_id: str,
        outcome: Literal["success", "failure"],
        reason_code: str,
    ) -> None:
        if self._interaction_pattern_knowledge is None or candidate is None:
            return
        parameters = candidate.metadata.get("action_parameters")
        if not isinstance(parameters, Mapping):
            return
        signature = parameters.get("interaction_pattern_signature")
        if (
            not (
                parameters.get("navigation_family") == "adaptive_snapshot"
                or parameters.get("target_mode") == "page_tab_navigation"
            )
            or not isinstance(signature, str)
            or re.fullmatch(r"[0-9a-f]{64}", signature) is None
        ):
            return
        try:
            await self._interaction_pattern_knowledge.record_outcome(
                pattern_signature=signature,
                task_id=task_id,
                page_capture_id=page_capture_id,
                outcome=outcome,
                reason_code=reason_code,
            )
        except Exception:  # noqa: BLE001 - 知识反馈失败不能改变任务业务终态
            logger.warning(
                "记录 H5 自适应交互模式结果失败：task_id=%s capture_id=%s",
                task_id,
                page_capture_id,
                exc_info=True,
            )

    async def run(self, command: RunH5WorkflowCommand) -> H5ExplorationResult:
        if not isinstance(command, RunH5WorkflowCommand):
            raise TypeError("command 必须为 RunH5WorkflowCommand")
        coordination = await self._bootstrapper.start(command)
        if coordination.interaction.status is not InteractionResultStatus.SUCCEEDED:
            # 首跳落点越界仍要保留已完成的 before/after 诊断证据，
            # 但不得把失败的导航结果继续交给认证、页面识别或 Frontier。
            interaction = coordination.interaction
            raise CaptureCoordinationError(
                stage=CaptureCoordinationStage.ACTION,
                error_code=interaction.error_code or "H5_BOOTSTRAP_NAVIGATION_FAILED",
                message=interaction.error_message or "H5 首跳导航未成功，已停止目标页面质检",
                before_capture=coordination.before_capture,
                interaction=interaction,
                after_capture=coordination.after_capture,
                retryable=interaction.retryable,
            )
        auth_result = await self._auth_bootstrap.run(
            H5AuthBootstrapCommand(
                task_id=command.task_id,
                agent_run_id=command.agent_run_id,
                session_id=command.session_id,
                target_url=command.target_url,
                deadline=command.deadline,
            ),
            initial_capture=coordination.after_capture,
        )
        capture = auth_result.final_capture
        if (
            capture.task_id != command.task_id
            or capture.session_id != command.session_id
            or capture.surface_kind is not SurfaceKind.WEB
        ):
            raise RuntimeError("H5 Auth Bootstrap 返回的根 Capture 归属不一致")
        detection, assessment = await self._detect_and_assess(
            capture=capture,
            task_id=command.task_id,
            agent_run_id=command.agent_run_id,
        )
        safety_bypass_metadata: dict[str, Any] | None = None
        if self._should_bypass_safety_interstitial(detection=detection, assessment=assessment):
            await self._persist_assessment(
                session_id=command.session_id,
                page_capture_id=capture.capture_id,
                detection=detection,
                assessment=assessment,
                task_id=command.task_id,
                agent_run_id=command.agent_run_id,
            )
            assert self._safety_interstitial_bypass is not None
            blocked_capture_id = capture.capture_id
            safety_result = await self._safety_interstitial_bypass.bypass(
                command=command,
                source_capture=capture,
            )
            capture = safety_result.final_capture
            if (
                capture.task_id != command.task_id
                or capture.session_id != command.session_id
                or capture.surface_kind is not SurfaceKind.WEB
            ):
                raise RuntimeError("H5 安全中间页旁路返回的 Capture 归属不一致")
            safety_bypass_metadata = {
                "status": "bypassed",
                "blocked_capture_id": blocked_capture_id,
                "before_capture_id": safety_result.coordination.before_capture.capture_id,
                "result_capture_id": capture.capture_id,
                "action_execution_id": safety_result.coordination.interaction.action_execution_id,
            }
            detection, assessment = await self._detect_and_assess(
                capture=capture,
                task_id=command.task_id,
                agent_run_id=command.agent_run_id,
            )
        await self._persist_assessment(
            session_id=command.session_id,
            page_capture_id=capture.capture_id,
            detection=detection,
            assessment=assessment,
            task_id=command.task_id,
            agent_run_id=command.agent_run_id,
        )
        scheduler_facts = await self._read_scheduler_facts(
            task_id=command.task_id,
            agent_run_id=command.agent_run_id,
            session_id=command.session_id,
        )
        initial_facts_metadata = merge_exploration_scheduler_facts(
            {
                "agent_run_id": command.agent_run_id,
                "before_capture_id": (
                    capture.parent_capture_id or coordination.before_capture.capture_id
                ),
                "action_execution_id": (
                    capture.action_execution_id or coordination.interaction.action_execution_id
                ),
                "sampling_policy_summary": dict(command.sampling_policy_summary),
                "journey_steps": tuple(command.sampling_policy_summary.get("journey_steps", ())),
                "h5_auth": auth_result.to_safe_dict(),
            },
            scheduler_facts,
            surface="h5",
        )
        initial_lineage_metadata = update_scheduler_metadata(
            initial_facts_metadata,
            scheduler=_BOUNDED_SCHEDULER,
            max_actions=command.budget.max_actions,
            max_duration_ms=command.budget.remaining_duration_ms,
            deadline=command.deadline,
            required_page_types=_h5_required_page_types_from_policy(
                command.sampling_policy_summary
            ),
            evidence_gap_page_types=evidence_gap_page_types_from_facts(
                initial_facts_metadata,
                known_page_types=_h5_required_page_types_from_policy(
                    command.sampling_policy_summary
                ),
            ),
            now=command.observed_at,
        )
        frontier = build_h5_frontier(
            detection_result=detection,
            observed_at=command.observed_at,
            navigation_depth=0,
            lineage_metadata=initial_lineage_metadata,
            logical_surface_effective=self._logical_surface_effective,
            logical_targets_effective=self._logical_targets_effective,
            h5_visual_coordinate_mode=self._h5_visual_coordinate_mode,
            source_task_id=command.task_id,
        )
        if safety_bypass_metadata is not None:
            frontier = replace(
                frontier,
                observation=replace(
                    frontier.observation,
                    metadata={
                        **frontier.observation.metadata,
                        "h5_safety_interstitial": safety_bypass_metadata,
                    },
                ),
            )
        task_frontier = H5TaskFrontierSnapshot.create(
            max_pages=_max_pages(command.sampling_policy_summary),
            sampling_policy_summary=command.sampling_policy_summary,
        ).merge(
            source_capture_id=capture.capture_id,
            source_state_version=frontier.observation.state_version,
            source_location=frontier.observation.location,
            navigation_depth=0,
            candidates=frontier.candidates,
            max_depth=command.budget.max_depth,
            now=command.observed_at,
        )
        task_frontier = task_frontier.record_observed_page_type(
            source_capture_id=capture.capture_id,
            source_state_version=frontier.observation.state_version,
            source_location=frontier.observation.location,
            navigation_depth=0,
            page_type=frontier.observation.page_type,
            observed_at=command.observed_at,
        )
        frontier = _with_task_frontier(
            frontier=frontier,
            assessment=assessment.to_dict(),
            snapshot=_select_next_task_entry(
                task_frontier,
                active_state_version=frontier.observation.state_version,
                active_source_location=frontier.observation.location,
                active_page_type=frontier.observation.page_type,
                active_page_state=frontier.observation.page_state,
                active_overlay_signature=frontier.observation.overlay_signature,
                active_observed_at=frontier.observation.observed_at,
                scheduler_state=scheduler_state_from_metadata(
                    frontier.observation.metadata,
                    max_actions=command.budget.max_actions,
                    max_duration_ms=command.budget.remaining_duration_ms,
                    deadline=command.deadline,
                    required_page_types=_h5_required_page_types_from_policy(
                        command.sampling_policy_summary
                    ),
                ),
            ),
        )
        work = await self._orchestrator.start_exploration(
            StartExplorationCommand(
                task_id=command.task_id,
                session_id=command.session_id,
                branch_id=f"h5:{capture.capture_id}",
                objective=command.objective,
                surface_kind=SurfaceKind.WEB,
                source_capture_id=capture.capture_id,
                frontier_snapshot_id=frontier.snapshot_id,
                frontier=frontier,
                boundaries=command.boundaries,
                budget=command.budget,
                deadline=command.deadline,
            )
        )
        return H5ExplorationResult(
            detection_result=detection,
            page_type_assessment=assessment,
            work=work,
            auth_result=auth_result,
            coordination=coordination,
        )

    def _should_bypass_safety_interstitial(
        self,
        *,
        detection: Mapping[str, Any],
        assessment: H5PageTypeAssessmentAdapterResult,
    ) -> bool:
        return (
            self._safety_interstitial_bypass is not None
            and assessment.execution_status == "blocked"
            and assessment.error_code == H5_SAFETY_INTERSTITIAL_ERROR_CODE
            and h5_detection_is_safety_interstitial(detection)
        )

    async def prepare_after_action(
        self,
        command: ContinueH5ExplorationCommand,
    ) -> H5ExplorationResult:
        if not isinstance(command, ContinueH5ExplorationCommand):
            raise TypeError("command 必须为 ContinueH5ExplorationCommand")
        prior = command.prior_work
        coordination = command.turn.coordination
        assert coordination is not None
        capture = coordination.after_capture
        detection, assessment = await self._detect_and_assess(
            capture=capture,
            task_id=prior.task_id,
            agent_run_id=_agent_run_id(prior),
        )
        await self._persist_assessment(
            session_id=capture.session_id,
            page_capture_id=capture.capture_id,
            detection=detection,
            assessment=assessment,
            task_id=prior.task_id,
            agent_run_id=_agent_run_id(prior),
        )
        observed_at = max(
            self._now(),
            prior.frontier.observation.observed_at,
        )
        elapsed_ms = max(
            0,
            int((observed_at - prior.frontier.observation.observed_at).total_seconds() * 1_000),
        )
        lineage_metadata: dict[str, Any] = {
            "agent_run_id": _agent_run_id(prior),
            "parent_work_id": prior.work_id,
            "parent_branch_id": prior.branch_id,
            "parent_capture_id": prior.source_capture_id,
            "parent_state_version": prior.frontier.observation.state_version,
            "before_capture_id": coordination.before_capture.capture_id,
            "action_execution_id": coordination.interaction.action_execution_id,
            "sampling_policy_summary": dict(
                prior.frontier.observation.metadata.get(
                    "sampling_policy_summary",
                    {},
                )
            ),
            "journey_steps": tuple(prior.frontier.observation.metadata.get("journey_steps", ())),
        }
        _carry_h5_coverage(prior=prior, metadata=lineage_metadata)
        lineage_metadata = merge_exploration_scheduler_facts(
            lineage_metadata,
            await self._read_scheduler_facts(
                task_id=prior.task_id,
                agent_run_id=_agent_run_id(prior),
                session_id=prior.session_id,
                finalization_epoch=_scheduler_fact_epoch(prior.frontier.observation.metadata),
            ),
            surface="h5",
        )
        detected_location = detection.get("page_url")
        if isinstance(detected_location, str):
            lineage_metadata.update(
                _history_parent_metadata(
                    prior=prior,
                    next_location=detected_location,
                )
            )
        prior_auth = prior.frontier.observation.metadata.get("h5_auth")
        if isinstance(prior_auth, Mapping):
            lineage_metadata["h5_auth"] = dict(prior_auth)
        line_loading_reobserve_count = _next_line_loading_reobserve_count(
            prior=prior,
            turn=command.turn,
        )
        lineage_metadata["line_loading_reobserve_count"] = line_loading_reobserve_count
        startup_ad_countdown_reobserve_count = _next_startup_ad_countdown_reobserve_count(
            prior=prior,
            turn=command.turn,
        )
        lineage_metadata["startup_ad_countdown_reobserve_count"] = (
            startup_ad_countdown_reobserve_count
        )
        startup_ad_dismissal_count = _next_startup_ad_dismissal_count(
            prior=prior,
            turn=command.turn,
        )
        lineage_metadata["startup_ad_dismissal_count"] = startup_ad_dismissal_count
        flutter_bootstrap_reobserve_count = _next_flutter_bootstrap_reobserve_count(
            prior=prior,
            turn=command.turn,
        )
        lineage_metadata["flutter_bootstrap_reobserve_count"] = flutter_bootstrap_reobserve_count
        insufficient_reobserve_count = _next_insufficient_reobserve_count(
            prior=prior,
            turn=command.turn,
        )
        lineage_metadata["insufficient_reobserve_count"] = insufficient_reobserve_count
        ambiguous_launch_shell_reobserve_count = _next_ambiguous_launch_shell_reobserve_count(
            prior=prior,
            turn=command.turn,
        )
        lineage_metadata["ambiguous_launch_shell_reobserve_count"] = (
            ambiguous_launch_shell_reobserve_count
        )
        logical_reobserve_count = _next_logical_reobserve_count(
            prior=prior,
            turn=command.turn,
        )
        lineage_metadata["logical_reobserve_count"] = logical_reobserve_count
        prior_logical_identity = prior.frontier.observation.metadata.get(
            "logical_surface_identity_fingerprint"
        )
        if isinstance(prior_logical_identity, str) and prior_logical_identity.strip():
            lineage_metadata["logical_reobserve_identity_fingerprint"] = prior_logical_identity
        capability_projection_reobserve_count = _next_capability_projection_reobserve_count(
            prior=prior,
            turn=command.turn,
        )
        lineage_metadata["capability_projection_reobserve_count"] = (
            capability_projection_reobserve_count
        )
        popup_absence_candidate = _completed_popup_absence_reobservation_candidate(
            prior=prior,
            turn=command.turn,
        )
        if popup_absence_candidate is not None:
            lineage_metadata["popup_absence_reobserve_completed"] = True
            prior_popup_dismissal_state = prior.frontier.observation.metadata.get(
                "popup_dismissal_reobserve_state"
            )
            if isinstance(prior_popup_dismissal_state, Mapping):
                # 失败关闭后的无副作用 Observe 只刷新页面事实，不能清空跨签名
                # 连续失败计数；否则动态素材每轮换签名时永远触发不了止损。
                lineage_metadata["popup_dismissal_reobserve_state"] = dict(
                    prior_popup_dismissal_state
                )
            obstruction_signature = popup_absence_candidate.metadata.get(
                "popup_absence_obstruction_signature"
            )
            if isinstance(obstruction_signature, str) and obstruction_signature.strip():
                lineage_metadata["popup_absence_obstruction_signature"] = (
                    obstruction_signature.strip()
                )
        frontier = build_h5_frontier(
            detection_result=detection,
            observed_at=observed_at,
            navigation_depth=_next_depth(prior, command.turn),
            visited_state_ids=_append_unique(
                prior.frontier.visited_state_ids,
                prior.frontier.observation.state_version,
            ),
            executed_candidate_ids=_append_unique(
                _executed_candidate_ids(prior),
                _selected_candidate_id(command.turn),
            ),
            lineage_metadata=lineage_metadata,
            logical_surface_effective=self._logical_surface_effective,
            logical_targets_effective=self._logical_targets_effective,
            h5_visual_coordinate_mode=self._h5_visual_coordinate_mode,
            source_task_id=prior.task_id,
        )
        selected_candidate = next(
            (
                candidate
                for candidate in prior.frontier.candidates
                if candidate.candidate_id == _selected_candidate_id(command.turn)
            ),
            None,
        )
        interaction_status = getattr(coordination.interaction.status, "value", None)
        frontier = replace(
            frontier,
            observation=replace(
                frontier.observation,
                metadata=update_scheduler_metadata(
                    frontier.observation.metadata,
                    scheduler=_BOUNDED_SCHEDULER,
                    max_actions=prior.budget.max_actions,
                    max_duration_ms=prior.budget.remaining_duration_ms,
                    deadline=prior.deadline,
                    candidate=selected_candidate,
                    required_page_types=_h5_required_page_types(prior),
                    evidence_gap_page_types=_h5_evidence_gap_page_types_from_metadata(
                        frontier.observation.metadata,
                        known_page_types=_h5_required_page_types(prior),
                    ),
                    missing_page_types=_h5_missing_page_types(prior),
                    action_claimed=(
                        selected_candidate is not None
                        and not _h5_scheduler_observation_candidate(selected_candidate)
                    ),
                    retryable_failure=interaction_status in {"failed", "timed_out"},
                    progressed=(
                        _h5_scheduler_observation_candidate(selected_candidate)
                        or frontier.observation.state_version
                        != prior.frontier.observation.state_version
                    ),
                    state_fingerprint=(
                        None
                        if _h5_scheduler_observation_candidate(selected_candidate)
                        else frontier.observation.state_version
                    ),
                    page_fingerprint=(
                        None
                        if _h5_scheduler_observation_candidate(selected_candidate)
                        else _h5_page_fingerprint(frontier)
                    ),
                    popup_fingerprint=_h5_popup_fingerprint(frontier),
                    return_fingerprint=_h5_return_fingerprint(
                        prior=prior,
                        candidate=selected_candidate,
                        frontier=frontier,
                    ),
                    elapsed_duration_ms=elapsed_ms,
                    now=observed_at,
                ),
            ),
        )
        task_frontier = _task_frontier_from_work(prior)
        selected_entry_id = _selected_entry_id(prior, command.turn)
        arrival_failure_reason = _arrival_page_type_failure_reason(
            snapshot=task_frontier,
            entry_id=selected_entry_id,
            actual_page_type=frontier.observation.page_type,
        )
        if coordination.interaction.error_code == "LOGICAL_TRANSITION_UNVERIFIED":
            # Driver 已执行并完成重观察，但没有形成可信业务状态变化。它只能证明
            # 当前候选未到达，不能证明整个探索器发生技术失败；保留失败原因并让
            # Frontier 继续探索其他入口。
            task_frontier = task_frontier.mark_skipped(
                selected_entry_id,
                reason="h5_logical_transition_unverified",
                finished_at=observed_at,
            )
        elif (
            frontier.observation.metadata.get("catalog_disposition") == "skipped"
            and frontier.observation.metadata.get("catalog_skip_reason") == "uncatalogued_page_type"
        ):
            task_frontier = task_frontier.mark_skipped(
                selected_entry_id,
                reason="uncatalogued_page_type",
                finished_at=observed_at,
            )
        elif frontier.observation.metadata.get("requires_auth") is True:
            task_frontier = task_frontier.mark_blocked(
                selected_entry_id,
                reason="authentication_required",
                finished_at=observed_at,
            )
        elif (
            frontier.observation.page_type == "line_loading"
            and frontier.observation.metadata.get("line_loading_reobserve_exhausted") is True
        ):
            task_frontier = task_frontier.mark_failed(
                selected_entry_id,
                reason="line_loading_reobserve_limit_exceeded",
                finished_at=observed_at,
            )
        elif (
            frontier.observation.page_type == "startup_ad"
            and frontier.observation.metadata.get("startup_ad_countdown_reobserve_exhausted")
            is True
        ):
            task_frontier = task_frontier.mark_failed(
                selected_entry_id,
                reason="startup_ad_countdown_reobserve_limit_exceeded",
                finished_at=observed_at,
            )
        elif (
            frontier.observation.page_type == "startup_ad"
            and frontier.observation.metadata.get("startup_ad_dismissal_exhausted") is True
        ):
            task_frontier = task_frontier.mark_failed(
                selected_entry_id,
                reason="startup_ad_dismissal_limit_exceeded",
                finished_at=observed_at,
            )
        elif frontier.observation.metadata.get("flutter_bootstrap_reobserve_exhausted") is True:
            task_frontier = task_frontier.mark_failed(
                selected_entry_id,
                reason="flutter_bootstrap_reobserve_limit_exceeded",
                finished_at=observed_at,
            )
        elif frontier.observation.metadata.get("insufficient_reobserve_exhausted") is True:
            task_frontier = task_frontier.mark_failed(
                selected_entry_id,
                reason="insufficient_reobserve_limit_exceeded",
                finished_at=observed_at,
            )
        elif (
            frontier.observation.metadata.get("ambiguous_launch_shell_reobserve_exhausted") is True
        ):
            task_frontier = task_frontier.mark_failed(
                selected_entry_id,
                reason="ambiguous_launch_shell_reobserve_limit_exceeded",
                finished_at=observed_at,
            )
        elif frontier.observation.metadata.get("logical_reobserve_exhausted") is True:
            if _observed_scope_was_already_covered(prior=prior, frontier=frontier):
                task_frontier = task_frontier.mark_skipped(
                    selected_entry_id,
                    reason="h5_logical_transition_reobserve_redundant",
                    finished_at=observed_at,
                )
            else:
                task_frontier = task_frontier.mark_skipped(
                    selected_entry_id,
                    reason="h5_logical_transition_unstable",
                    finished_at=observed_at,
                )
        elif _turn_executes_logical_reobserve(prior=prior, turn=command.turn):
            task_frontier = task_frontier.mark_skipped(
                selected_entry_id,
                reason="h5_logical_transition_reobserved",
                finished_at=observed_at,
            )
        elif frontier.observation.metadata.get("capability_projection_reobserve_exhausted") is True:
            task_frontier = task_frontier.mark_failed(
                selected_entry_id,
                reason="capability_projection_reobserve_limit_exceeded",
                finished_at=observed_at,
            )
        elif arrival_failure_reason is not None:
            actual_page_type = frontier.observation.page_type
            if (
                arrival_failure_reason.startswith("arrival_page_type_mismatch:")
                and isinstance(actual_page_type, str)
                and actual_page_type.strip()
            ):
                task_frontier = task_frontier.mark_executed_with_observed_page_type(
                    selected_entry_id,
                    observed_page_type=actual_page_type,
                    discrepancy_reason=arrival_failure_reason,
                    finished_at=observed_at,
                )
                task_frontier = task_frontier.record_observed_page_type(
                    source_capture_id=capture.capture_id,
                    source_state_version=frontier.observation.state_version,
                    source_location=frontier.observation.location,
                    navigation_depth=_next_depth(prior, command.turn),
                    page_type=actual_page_type,
                    observed_at=observed_at,
                )
            else:
                task_frontier = task_frontier.mark_failed(
                    selected_entry_id,
                    reason=arrival_failure_reason,
                    finished_at=observed_at,
                )
        else:
            task_frontier = task_frontier.mark_executed(
                selected_entry_id,
                finished_at=observed_at,
            )
            task_frontier = task_frontier.record_observed_page_type(
                source_capture_id=capture.capture_id,
                source_state_version=frontier.observation.state_version,
                source_location=frontier.observation.location,
                navigation_depth=_next_depth(prior, command.turn),
                page_type=frontier.observation.page_type,
                observed_at=observed_at,
            )
        state_changed = (
            frontier.observation.state_version
            != prior.frontier.observation.state_version
        )
        if (
            state_changed
            and coordination.interaction.error_code
            != "LOGICAL_TRANSITION_UNVERIFIED"
        ):
            await self._record_interaction_pattern_outcome(
                candidate=selected_candidate,
                task_id=prior.task_id,
                page_capture_id=capture.capture_id,
                outcome="success",
                reason_code="verified_state_transition",
            )
        else:
            await self._record_interaction_pattern_outcome(
                candidate=selected_candidate,
                task_id=prior.task_id,
                page_capture_id=capture.capture_id,
                outcome="failure",
                reason_code=(
                    "h5_logical_transition_unverified"
                    if coordination.interaction.error_code
                    == "LOGICAL_TRANSITION_UNVERIFIED"
                    else "state_transition_not_observed"
                ),
            )
        task_frontier = task_frontier.merge(
            source_capture_id=capture.capture_id,
            source_state_version=frontier.observation.state_version,
            source_location=frontier.observation.location,
            navigation_depth=_next_depth(prior, command.turn),
            parent_state_version=(
                prior.frontier.observation.state_version
                if frontier.observation.state_version != prior.frontier.observation.state_version
                else None
            ),
            candidates=frontier.candidates,
            max_depth=prior.budget.max_depth,
            now=observed_at,
        )
        frontier = _with_task_frontier(
            frontier=frontier,
            assessment=assessment.to_dict(),
            snapshot=_select_next_task_entry(
                task_frontier,
                active_state_version=frontier.observation.state_version,
                active_source_location=frontier.observation.location,
                active_page_type=frontier.observation.page_type,
                active_page_state=frontier.observation.page_state,
                active_overlay_signature=frontier.observation.overlay_signature,
                active_observed_at=frontier.observation.observed_at,
                scheduler_state=scheduler_state_from_metadata(
                    frontier.observation.metadata,
                    max_actions=prior.budget.max_actions,
                    max_duration_ms=prior.budget.remaining_duration_ms,
                    deadline=prior.deadline,
                    required_page_types=_h5_required_page_types(prior),
                    evidence_gap_page_types=_h5_evidence_gap_page_types_from_metadata(
                        frontier.observation.metadata,
                        known_page_types=_h5_required_page_types(prior),
                    ),
                ),
                **_history_parent_selection_preference(frontier),
            ),
        )
        work = await self._orchestrator.prepare_exploration(
            StartExplorationCommand(
                task_id=prior.task_id,
                session_id=prior.session_id,
                branch_id=f"h5:{capture.capture_id}",
                objective=prior.objective,
                surface_kind=SurfaceKind.WEB,
                source_capture_id=capture.capture_id,
                frontier_snapshot_id=frontier.snapshot_id,
                frontier=frontier,
                boundaries=prior.boundaries,
                budget=ExplorationBudget(
                    max_actions=prior.budget.max_actions,
                    remaining_actions=max(prior.budget.remaining_actions - 1, 0),
                    max_depth=prior.budget.max_depth,
                    remaining_duration_ms=max(
                        prior.budget.remaining_duration_ms - elapsed_ms,
                        0,
                    ),
                ),
                deadline=prior.deadline,
            )
        )
        return H5ExplorationResult(
            detection_result=detection,
            page_type_assessment=assessment,
            work=work,
            coordination=coordination,
        )

    async def prepare_after_action_failure(
        self,
        command: ReobserveH5ExplorationCommand,
    ) -> H5ExplorationResult:
        """关闭失败候选，并从动作前 Capture 重新识别当前最上层页面。"""
        if not isinstance(command, ReobserveH5ExplorationCommand):
            raise TypeError("command 必须为 ReobserveH5ExplorationCommand")
        prior = command.prior_work
        capture = command.before_capture
        task_frontier = _task_frontier_from_work(prior)
        selected = [
            entry
            for entry in task_frontier.entries
            if entry.status is H5FrontierEntryStatus.SELECTED
        ]
        if len(selected) != 1:
            raise ValueError("H5 Action 重观察必须唯一绑定 selected Frontier entry")
        stale_entry = selected[0]
        prior_metadata = prior.frontier.observation.metadata
        current_visual_reobservation = (
            command.error_code in _VISUAL_ACTION_REOBSERVATION_ERROR_CODES
        )
        prior_failure_count_raw = prior_metadata.get("action_reobservation_failure_count")
        same_visual_source = bool(
            current_visual_reobservation
            and prior_metadata.get("action_reobserve_error_code")
            in _VISUAL_ACTION_REOBSERVATION_ERROR_CODES
            and prior_metadata.get("action_reobservation_source_state_version")
            == stale_entry.source_state_version
            and prior_metadata.get("action_reobservation_session_id") == prior.session_id
        )
        if (
            same_visual_source
            and isinstance(prior_failure_count_raw, int)
            and not isinstance(prior_failure_count_raw, bool)
            and prior_failure_count_raw >= 1
        ):
            raise ValueError("视觉动作重观察预算已耗尽")
        agent_run_id = _agent_run_id(prior)
        replay = await self._accepted_assessment_replay(
            capture=capture,
            task_id=prior.task_id,
            agent_run_id=agent_run_id,
        )
        if replay is None:
            detection, assessment = await self._detect_and_assess(
                capture=capture,
                task_id=prior.task_id,
                agent_run_id=agent_run_id,
            )
            await self._persist_assessment(
                session_id=capture.session_id,
                page_capture_id=capture.capture_id,
                detection=detection,
                assessment=assessment,
                task_id=prior.task_id,
                agent_run_id=agent_run_id,
            )
        else:
            detection, assessment = replay
        observed_at = max(self._now(), prior.frontier.observation.observed_at)
        elapsed_ms = max(
            0,
            int((observed_at - prior.frontier.observation.observed_at).total_seconds() * 1_000),
        )
        terminal_reason = f"action_failure_reobserved:{command.error_code}"
        source_state_refresh_only = command.error_code == "LOGICAL_SOURCE_STATE_STALE"
        if h5_action_failure_is_policy_blocked(command.error_code):
            task_frontier = task_frontier.mark_blocked(
                stale_entry.entry_id,
                reason=terminal_reason,
                finished_at=observed_at,
            )
        else:
            task_frontier = task_frontier.mark_skipped(
                stale_entry.entry_id,
                reason=terminal_reason,
                finished_at=observed_at,
            )
        if command.error_code in _INTERACTION_PATTERN_FAILURE_CODES:
            await self._record_interaction_pattern_outcome(
                candidate=stale_entry.candidate,
                task_id=prior.task_id,
                page_capture_id=capture.capture_id,
                outcome="failure",
                reason_code=command.error_code.casefold(),
            )
        failed_candidate_identity = {
            "action_type": stale_entry.candidate.action_type.value,
            "semantic_target": stale_entry.candidate.semantic_target,
            "expected_next_page_type": stale_entry.candidate.metadata.get(
                "expected_next_page_type"
            ),
        }
        prior_failed_identity = prior_metadata.get("action_reobservation_candidate_identity")
        prior_error_code = prior_metadata.get("action_reobserve_error_code")
        same_failure_occurrence = bool(
            same_visual_source
            or (
                isinstance(prior_failed_identity, Mapping)
                and dict(prior_failed_identity) == failed_candidate_identity
                and prior_error_code == command.error_code
            )
        )
        prior_failure_count = (
            prior_failure_count_raw
            if same_failure_occurrence
            and isinstance(prior_failure_count_raw, int)
            and not isinstance(prior_failure_count_raw, bool)
            and prior_failure_count_raw >= 0
            else 0
        )
        lineage_metadata: dict[str, Any] = {
            "agent_run_id": _agent_run_id(prior),
            "parent_work_id": prior.work_id,
            "parent_branch_id": prior.branch_id,
            "parent_capture_id": prior.source_capture_id,
            "parent_state_version": prior.frontier.observation.state_version,
            "before_capture_id": capture.capture_id,
            "failed_action_execution_id": capture.action_execution_id,
            "action_reobserve_error_code": command.error_code,
            "action_reobservation_retryable": command.retryable,
            "action_reobservation_candidate_identity": failed_candidate_identity,
            "action_reobservation_failure_count": prior_failure_count + 1,
            "action_reobservation_source_state_version": stale_entry.source_state_version,
            "action_reobservation_session_id": prior.session_id,
            "h5_action_reobservation": {
                "schema_version": "h5-action-reobservation.v1",
                "task_id": prior.task_id,
                "session_id": prior.session_id,
                "failed_work_id": prior.work_id,
                "before_capture_id": capture.capture_id,
                "error_code": command.error_code,
                "retryable": command.retryable,
            },
            "sampling_policy_summary": dict(
                prior.frontier.observation.metadata.get("sampling_policy_summary", {})
            ),
            "journey_steps": tuple(prior.frontier.observation.metadata.get("journey_steps", ())),
        }
        if command.retryable:
            # failure_count 包含首次动作失败；H5 按稳定候选身份限制重试，
            # 不能再用队列累计失败淘汰同队列内其它尚未尝试的业务入口。
            lineage_metadata["action_reobservation_failure_max"] = (
                _BOUNDED_SCHEDULER.policy.retry_limit
            )
        _carry_h5_coverage(prior=prior, metadata=lineage_metadata)
        lineage_metadata = merge_exploration_scheduler_facts(
            lineage_metadata,
            await self._read_scheduler_facts(
                task_id=prior.task_id,
                agent_run_id=agent_run_id,
                session_id=prior.session_id,
                finalization_epoch=_scheduler_fact_epoch(prior_metadata),
            ),
            surface="h5",
        )
        _carry_h5_reobservation_lineage(prior=prior, metadata=lineage_metadata)
        detected_location = detection.get("page_url")
        if isinstance(detected_location, str):
            lineage_metadata.update(
                _history_parent_metadata(prior=prior, next_location=detected_location)
            )
        prior_auth = prior.frontier.observation.metadata.get("h5_auth")
        if isinstance(prior_auth, Mapping):
            lineage_metadata["h5_auth"] = dict(prior_auth)
        popup_dismissal_state = next_popup_dismissal_reobserve_state(
            prior_state=prior.frontier.observation.metadata.get("popup_dismissal_reobserve_state"),
            candidate=stale_entry.candidate,
            error_code=command.error_code,
        )
        if popup_dismissal_state is not None:
            lineage_metadata["popup_dismissal_reobserve_state"] = popup_dismissal_state
        frontier = build_h5_frontier(
            detection_result=detection,
            observed_at=observed_at,
            navigation_depth=stale_entry.navigation_depth,
            visited_state_ids=tuple(task_frontier.visited_state_ids),
            executed_candidate_ids=tuple(task_frontier.executed_candidate_ids),
            lineage_metadata=lineage_metadata,
            logical_surface_effective=self._logical_surface_effective,
            logical_targets_effective=self._logical_targets_effective,
            h5_visual_coordinate_mode=self._h5_visual_coordinate_mode,
            source_task_id=prior.task_id,
        )
        frontier = replace(
            frontier,
            observation=replace(
                frontier.observation,
                metadata=update_scheduler_metadata(
                    frontier.observation.metadata,
                    scheduler=_BOUNDED_SCHEDULER,
                    max_actions=prior.budget.max_actions,
                    max_duration_ms=prior.budget.remaining_duration_ms,
                    deadline=prior.deadline,
                    candidate=stale_entry.candidate,
                    required_page_types=_h5_required_page_types(prior),
                    evidence_gap_page_types=_h5_evidence_gap_page_types_from_metadata(
                        frontier.observation.metadata,
                        known_page_types=_h5_required_page_types(prior),
                    ),
                    missing_page_types=_h5_missing_page_types(prior),
                    action_claimed=not source_state_refresh_only,
                    retryable_failure=(
                        command.retryable and not source_state_refresh_only
                    ),
                    # 当前候选已经明确进入 skipped/blocked 终态，活动 Frontier
                    # 单调缩小；应视为调度进展，不能让少量失效入口触发全局
                    # no_progress_limit 并误杀仍可执行的兄弟业务分支。
                    progressed=True,
                    state_fingerprint=frontier.observation.state_version,
                    page_fingerprint=_h5_page_fingerprint(frontier),
                    popup_fingerprint=_h5_popup_fingerprint(frontier),
                    return_fingerprint=_h5_return_fingerprint(
                        prior=prior,
                        candidate=stale_entry.candidate,
                        frontier=frontier,
                    ),
                    elapsed_duration_ms=elapsed_ms,
                    now=observed_at,
                ),
            ),
        )
        task_frontier = task_frontier.merge(
            source_capture_id=capture.capture_id,
            source_state_version=frontier.observation.state_version,
            source_location=frontier.observation.location,
            navigation_depth=stale_entry.navigation_depth,
            parent_state_version=stale_entry.parent_state_version,
            candidates=frontier.candidates,
            max_depth=prior.budget.max_depth,
            now=observed_at,
        )
        frontier = _with_task_frontier(
            frontier=frontier,
            assessment=assessment.to_dict(),
            snapshot=_select_next_task_entry(
                task_frontier,
                active_state_version=frontier.observation.state_version,
                active_source_location=frontier.observation.location,
                active_page_type=frontier.observation.page_type,
                active_page_state=frontier.observation.page_state,
                active_overlay_signature=frontier.observation.overlay_signature,
                active_observed_at=frontier.observation.observed_at,
                scheduler_state=scheduler_state_from_metadata(
                    frontier.observation.metadata,
                    max_actions=prior.budget.max_actions,
                    max_duration_ms=prior.budget.remaining_duration_ms,
                    deadline=prior.deadline,
                    required_page_types=_h5_required_page_types(prior),
                    evidence_gap_page_types=_h5_evidence_gap_page_types_from_metadata(
                        frontier.observation.metadata,
                        known_page_types=_h5_required_page_types(prior),
                    ),
                ),
                **_history_parent_selection_preference(frontier),
            ),
        )
        elapsed_ms = max(
            0,
            int((observed_at - prior.frontier.observation.observed_at).total_seconds() * 1_000),
        )
        work = await self._orchestrator.prepare_exploration(
            StartExplorationCommand(
                task_id=prior.task_id,
                session_id=prior.session_id,
                branch_id="h5-reobserve:"
                + stable_protocol_hash(
                    {
                        "parent_work_id": prior.work_id,
                        "before_capture_id": capture.capture_id,
                    }
                ),
                objective=prior.objective,
                surface_kind=SurfaceKind.WEB,
                source_capture_id=capture.capture_id,
                frontier_snapshot_id=frontier.snapshot_id,
                frontier=frontier,
                boundaries=prior.boundaries,
                budget=ExplorationBudget(
                    max_actions=prior.budget.max_actions,
                    remaining_actions=max(prior.budget.remaining_actions - 1, 0),
                    max_depth=prior.budget.max_depth,
                    remaining_duration_ms=max(
                        prior.budget.remaining_duration_ms - elapsed_ms,
                        0,
                    ),
                ),
                deadline=prior.deadline,
            )
        )
        return H5ExplorationResult(
            detection_result=detection,
            page_type_assessment=assessment,
            work=work,
        )

    async def prepare_browser_recovery(
        self,
        command: RecoverH5ExplorationCommand,
    ) -> H5ExplorationResult:
        """持久化重基线评估，并从原任务 Frontier 生成稳定恢复 Work。"""
        if not isinstance(command, RecoverH5ExplorationCommand):
            raise TypeError("command 必须为 RecoverH5ExplorationCommand")
        prior = command.prior_work
        capture = command.capture
        detection = dict(command.detection_result)
        if str(detection.get("page_capture_id") or "") != capture.capture_id:
            raise ValueError("恢复页面判定结果与 Capture 身份不一致")
        await self._persist_assessment(
            session_id=capture.session_id,
            page_capture_id=capture.capture_id,
            detection=detection,
            assessment=command.assessment,
            task_id=prior.task_id,
            agent_run_id=_agent_run_id(prior),
        )
        task_frontier = _task_frontier_from_work(prior)
        failure_stage = H5FailureStage(command.failure_stage)
        if failure_stage in {H5FailureStage.ACTION, H5FailureStage.AFTER_CAPTURE}:
            selected = [
                entry
                for entry in task_frontier.entries
                if entry.status is H5FrontierEntryStatus.SELECTED
            ]
            if len(selected) != 1:
                raise ValueError("未知 Action 恢复必须唯一绑定 selected Frontier entry")
            task_frontier = task_frontier.mark_failed(
                selected[0].entry_id,
                reason="h5_browser_loss_action_unknown",
                finished_at=max(self._now(), selected[0].selected_at),
            )
        agent_run_id = _agent_run_id(prior)
        attempt_payload = prior.frontier.observation.metadata.get("h5_browser_recovery_attempt")
        if isinstance(attempt_payload, Mapping):
            attempt_facts = H5BrowserRecoveryAttemptFacts.from_dict(attempt_payload)
        else:
            attempt_facts = H5BrowserRecoveryAttemptFacts.create(
                task_id=prior.task_id,
                agent_run_id=agent_run_id,
                source_session_id=prior.session_id,
                failed_work_id=prior.work_id,
                source_state_version=prior.frontier.observation.state_version,
                frontier_snapshot_id=prior.frontier_snapshot_id,
                failure_stage=failure_stage,
                recovery_attempt_count=command.recovery_attempt_count,
                status="started",
                error_code="browser_session_lost",
                action_execution_id=command.action_execution_id,
                before_capture_id=(
                    prior.source_capture_id
                    if failure_stage is not H5FailureStage.BEFORE_CAPTURE
                    else None
                ),
                after_capture_id=None,
                interaction_status=None,
                interaction_result_capture_id=None,
                started_at=max(self._now(), prior.frontier.observation.observed_at).isoformat(),
                deadline=prior.deadline.isoformat(),
                remaining_duration_ms=prior.budget.remaining_duration_ms,
                action_cost=(1 if failure_stage is H5FailureStage.BEFORE_CAPTURE else 2),
                remaining_actions_after_reservation=max(
                    prior.budget.remaining_actions
                    - (1 if failure_stage is H5FailureStage.BEFORE_CAPTURE else 2),
                    0,
                ),
                last_safe_location=prior.frontier.observation.location,
                reason_code=None,
            )
        lineage_metadata = dict(prior.frontier.observation.metadata)
        lineage_metadata = merge_exploration_scheduler_facts(
            lineage_metadata,
            await self._read_scheduler_facts(
                task_id=prior.task_id,
                agent_run_id=agent_run_id,
                session_id=capture.session_id,
                finalization_epoch=_scheduler_fact_epoch(prior.frontier.observation.metadata),
            ),
            surface="h5",
        )
        if command.equivalent and command.reason_code is None:
            existing_audit = lineage_metadata.get(
                "h5_browser_recovery_parent_attempt_audit",
                (),
            )
            attempt_history = lineage_metadata.get(
                "h5_browser_recovery_attempt_history",
                (),
            )
            if not isinstance(existing_audit, (list, tuple)) or not isinstance(
                attempt_history,
                (list, tuple),
            ):
                raise ValueError("H5 browser recovery parent attempt audit 无效")
            audit_by_hash: dict[str, dict[str, object]] = {}
            for payload in (*existing_audit, *attempt_history):
                historical = H5BrowserRecoveryAttemptFacts.from_dict(payload)
                audit_by_hash[historical.payload_hash] = historical.to_dict()
            completed_attempt = attempt_facts.complete(reason_code=None)
            audit_by_hash[completed_attempt.payload_hash] = completed_attempt.to_dict()
            lineage_metadata.pop("h5_browser_recovery_attempt", None)
            lineage_metadata.pop("h5_browser_recovery_attempt_history", None)
            lineage_metadata["h5_browser_recovery_parent_attempt_audit"] = list(
                audit_by_hash.values()
            )
        frontier = build_h5_frontier(
            detection_result=detection,
            observed_at=max(self._now(), prior.frontier.observation.observed_at),
            navigation_depth=_recovery_navigation_depth(task_frontier),
            visited_state_ids=prior.frontier.visited_state_ids,
            executed_candidate_ids=_executed_candidate_ids(prior),
            lineage_metadata={
                **lineage_metadata,
                "parent_work_id": prior.work_id,
                "parent_branch_id": prior.branch_id,
                "parent_capture_id": prior.source_capture_id,
                "h5_browser_recovery": H5BrowserRecoveryFacts.create(
                    task_id=prior.task_id,
                    agent_run_id=agent_run_id,
                    failed_work_id=prior.work_id,
                    source_session_id=prior.session_id,
                    target_session_id=capture.session_id,
                    failure_stage=failure_stage,
                    recovery_attempt_count=command.recovery_attempt_count,
                    original_source_capture_id=prior.source_capture_id,
                    rebaseline_before_capture_id=(
                        capture.parent_capture_id or _raise_missing_recovery_capture_identity()
                    ),
                    rebaseline_after_capture_id=capture.capture_id,
                    failed_action_execution_id=command.action_execution_id,
                    restoration_action_execution_id=(
                        capture.action_execution_id or _raise_missing_recovery_capture_identity()
                    ),
                    inventory_status=command.inventory_status,
                    source_state_version=prior.frontier.observation.state_version,
                    target_state_version=str(detection.get("state_version") or "").strip()
                    or build_h5_frontier(
                        detection_result=detection,
                        observed_at=max(self._now(), prior.frontier.observation.observed_at),
                        navigation_depth=_recovery_navigation_depth(task_frontier),
                        logical_surface_effective=self._logical_surface_effective,
                        logical_targets_effective=self._logical_targets_effective,
                        h5_visual_coordinate_mode=self._h5_visual_coordinate_mode,
                        source_task_id=prior.task_id,
                    ).observation.state_version,
                    equivalent=command.equivalent,
                    reason_code=command.reason_code,
                ).to_dict(),
            },
            logical_surface_effective=self._logical_surface_effective,
            logical_targets_effective=self._logical_targets_effective,
            h5_visual_coordinate_mode=self._h5_visual_coordinate_mode,
            source_task_id=prior.task_id,
        )
        if failure_stage in {H5FailureStage.ACTION, H5FailureStage.AFTER_CAPTURE}:
            task_frontier = task_frontier.merge(
                source_capture_id=capture.capture_id,
                source_state_version=frontier.observation.state_version,
                source_location=frontier.observation.location,
                navigation_depth=_recovery_navigation_depth(task_frontier),
                candidates=frontier.candidates,
                max_depth=prior.budget.max_depth,
                now=max(self._now(), prior.frontier.observation.observed_at),
            )
            task_frontier = _select_next_task_entry(
                task_frontier,
                active_state_version=frontier.observation.state_version,
                active_source_location=frontier.observation.location,
                active_page_type=frontier.observation.page_type,
                active_page_state=frontier.observation.page_state,
                active_overlay_signature=frontier.observation.overlay_signature,
                active_observed_at=frontier.observation.observed_at,
                scheduler_state=scheduler_state_from_metadata(
                    prior.frontier.observation.metadata,
                    max_actions=prior.budget.max_actions,
                    max_duration_ms=prior.budget.remaining_duration_ms,
                    deadline=prior.deadline,
                    required_page_types=_h5_required_page_types(prior),
                    evidence_gap_page_types=_h5_evidence_gap_page_types_from_metadata(
                        frontier.observation.metadata,
                        known_page_types=_h5_required_page_types(prior),
                    ),
                ),
            )
        frontier = _with_task_frontier(
            frontier=frontier,
            snapshot=task_frontier,
            assessment=command.assessment.to_dict(),
        )
        attempt = prior.frontier.observation.metadata.get("h5_browser_recovery_attempt")
        already_reserved = (
            isinstance(attempt, Mapping)
            and attempt.get("recovery_attempt_count") == command.recovery_attempt_count
        )
        action_cost = (
            0 if already_reserved else (1 if failure_stage is H5FailureStage.BEFORE_CAPTURE else 2)
        )
        remaining_actions = max(prior.budget.remaining_actions - action_cost, 0)
        remaining_duration = max(
            prior.budget.remaining_duration_ms - command.elapsed_ms,
            0,
        )
        budget = ExplorationBudget(
            max_actions=prior.budget.max_actions,
            remaining_actions=remaining_actions,
            max_depth=prior.budget.max_depth,
            remaining_duration_ms=remaining_duration,
        )
        if not command.equivalent or command.reason_code is not None:
            failure_frontier = _with_task_frontier(
                frontier=prior.frontier,
                snapshot=task_frontier,
            )
            failure_frontier = replace(
                failure_frontier,
                observation=replace(
                    failure_frontier.observation,
                    metadata={
                        **dict(failure_frontier.observation.metadata),
                        "h5_browser_recovery": frontier.observation.metadata["h5_browser_recovery"],
                    },
                ),
            )
            failure_frontier = replace(
                failure_frontier,
                observation=replace(
                    failure_frontier.observation,
                    metadata={
                        **dict(failure_frontier.observation.metadata),
                        "h5_browser_recovery_attempt": attempt_facts.complete(
                            reason_code=command.reason_code
                        ).to_dict(),
                    },
                ),
            )
            work = replace(
                prior,
                frontier_snapshot_id=failure_frontier.snapshot_id,
                frontier=failure_frontier,
                budget=budget,
            )
        else:
            work = await self._orchestrator.prepare_exploration(
                StartExplorationCommand(
                    task_id=prior.task_id,
                    session_id=capture.session_id,
                    branch_id=(
                        f"h5-recovery:{prior.work_id}:{command.failure_stage}:"
                        f"{command.recovery_attempt_count}"
                    ),
                    objective=prior.objective,
                    surface_kind=SurfaceKind.WEB,
                    source_capture_id=capture.capture_id,
                    frontier_snapshot_id=frontier.snapshot_id,
                    frontier=frontier,
                    boundaries=prior.boundaries,
                    budget=budget,
                    deadline=prior.deadline,
                )
            )
        return H5ExplorationResult(
            detection_result=detection,
            page_type_assessment=command.assessment,
            work=work,
        )

    async def _read_scheduler_facts(
        self,
        *,
        task_id: str,
        agent_run_id: str,
        session_id: str,
        finalization_epoch: int | None = None,
    ) -> Mapping[str, object] | None:
        """读取当前 H5 Run/Epoch 的真实 Coverage、规则缺口和会话范围。"""

        if self._scheduler_facts_reader is None:
            return None
        facts = await self._scheduler_facts_reader.read_h5_facts(
            task_id=task_id,
            agent_run_id=agent_run_id,
            session_id=session_id,
            finalization_epoch=finalization_epoch,
        )
        if not isinstance(facts, Mapping):
            raise TypeError("H5 调度事实读取器必须返回 Mapping")
        return facts

    async def _detect_and_assess(
        self,
        *,
        capture: CaptureRef,
        task_id: str,
        agent_run_id: str,
    ) -> tuple[dict[str, Any], H5PageTypeAssessmentAdapterResult]:
        detection, assessment = await self._detect_and_adapt_once(
            capture=capture,
            task_id=task_id,
            agent_run_id=agent_run_id,
        )
        if _is_transient_page_type_detection_failure(assessment):
            detection, assessment = await self._detect_and_adapt_once(
                capture=capture,
                task_id=task_id,
                agent_run_id=agent_run_id,
            )
        return dict(detection), assessment

    async def _detect_and_adapt_once(
        self,
        *,
        capture: CaptureRef,
        task_id: str,
        agent_run_id: str,
    ) -> tuple[Mapping[str, Any], H5PageTypeAssessmentAdapterResult]:
        """对同一不可变 Capture 执行一次识别与合同适配。"""
        detection = await self._detector.detect(
            capture=capture,
            task_id=task_id,
            agent_run_id=agent_run_id,
        )
        if str(detection.get("page_capture_id") or "") != capture.capture_id:
            raise ValueError("页面判定结果与 Capture 身份不一致")
        assessment = adapt_h5_page_detection_to_assessment(
            task_id=task_id,
            agent_run_id=agent_run_id,
            detection_result=detection,
            assessed_at=self._now(),
        )
        return detection, assessment

    async def _accepted_assessment_replay(
        self,
        *,
        capture: CaptureRef,
        task_id: str,
        agent_run_id: str,
    ) -> tuple[dict[str, Any], H5PageTypeAssessmentAdapterResult] | None:
        """同一不可变 Capture 已有完整 accepted 事实时直接复用。"""
        records = await self._assessment_repository.list_for_run(
            task_id=task_id,
            agent_run_id=agent_run_id,
        )
        for record in records:
            replay = _accepted_assessment_replay_from_record(
                record,
                task_id=task_id,
                agent_run_id=agent_run_id,
                session_id=capture.session_id,
                page_capture_id=capture.capture_id,
            )
            if replay is not None:
                return replay
        return None

    async def _persist_assessment(
        self,
        *,
        session_id: str,
        page_capture_id: str,
        detection: Mapping[str, object],
        assessment: H5PageTypeAssessmentAdapterResult,
        task_id: str,
        agent_run_id: str,
    ) -> None:
        """在发布 Work 前持久化 accepted、blocked 或 skipped 评估结果。"""
        await self._assessment_repository.upsert(
            task_id=task_id,
            agent_run_id=agent_run_id,
            session_id=session_id,
            page_capture_id=page_capture_id,
            detection_result=detection,
            assessment_payload=assessment.to_dict(),
        )


_TRANSIENT_PAGE_TYPE_DETECTION_ERRORS = frozenset(
    {
        "PAGE_TYPE_ASSESSMENT_PAGE_TYPE_REQUIRED",
        "PAGE_TYPE_ASSESSMENT_UNKNOWN_PAGE_TYPE",
    }
)


def _is_transient_page_type_detection_failure(
    assessment: H5PageTypeAssessmentAdapterResult,
) -> bool:
    """只有模型未返回可用页型时允许对同一截图重试一次。"""
    return (
        assessment.execution_status == "blocked"
        and assessment.retryable
        and assessment.error_code in _TRANSIENT_PAGE_TYPE_DETECTION_ERRORS
    )


def _accepted_assessment_replay_from_record(
    record: object,
    *,
    task_id: str,
    agent_run_id: str,
    session_id: str,
    page_capture_id: str,
) -> tuple[dict[str, Any], H5PageTypeAssessmentAdapterResult] | None:
    """只复用身份、审计引用和评估结论均完整一致的 accepted 记录。"""
    expected_identity = {
        "task_id": task_id,
        "agent_run_id": agent_run_id,
        "session_id": session_id,
        "page_capture_id": page_capture_id,
    }
    if any(_record_field(record, name) != value for name, value in expected_identity.items()):
        return None
    payload = _record_field(record, "assessment_payload")
    if not isinstance(payload, Mapping):
        return None
    detection = payload.get("detection_result")
    adapter = payload.get("adapter_result")
    if not isinstance(detection, Mapping) or not isinstance(adapter, Mapping):
        return None
    page_type_assessment = adapter.get("page_type_assessment")
    if (
        adapter.get("protocol_version") != H5_PAGE_TYPE_ASSESSMENT_ADAPTER_VERSION
        or adapter.get("execution_status") != "accepted"
        or adapter.get("retryable") is not False
        or not isinstance(page_type_assessment, Mapping)
    ):
        return None
    record_page_type = _record_field(record, "page_type")
    record_status = _record_field(record, "status")
    record_detection_status = _record_field(record, "detection_status")
    if (
        not isinstance(record_page_type, str)
        or not record_page_type
        or not isinstance(record_status, str)
        or not record_status
        or detection.get("page_capture_id") != page_capture_id
        or detection.get("page_type") != record_page_type
        or detection.get("detection_status") != record_detection_status
        or page_type_assessment.get("page_type") != record_page_type
        or page_type_assessment.get("status") != record_status
        or _text_sequence(page_type_assessment.get("page_capture_ids")) != (page_capture_id,)
    ):
        return None
    evidence_ids = _text_sequence(page_type_assessment.get("evidence_artifact_ids"))
    if not evidence_ids or evidence_ids != _text_sequence(detection.get("evidence_artifact_ids")):
        return None
    record_page_state = _record_field(record, "page_state")
    detection_page_state = detection.get("page_state")
    if record_page_state is not None and detection_page_state != record_page_state:
        return None
    page_states = _text_sequence(page_type_assessment.get("page_states"))
    if page_states and page_states != (str(detection_page_state or "unknown"),):
        return None
    detection_catalog = detection.get("catalog_sha256")
    assessment_catalog = page_type_assessment.get("catalog_sha256")
    if (
        detection_catalog is not None
        and assessment_catalog is not None
        and detection_catalog != assessment_catalog
    ):
        return None
    replay_detection = deepcopy(dict(detection))
    replay_assessment = H5PageTypeAssessmentAdapterResult(
        execution_status="accepted",
        page_type_assessment=deepcopy(dict(page_type_assessment)),
        retryable=False,
        protocol_version=H5_PAGE_TYPE_ASSESSMENT_ADAPTER_VERSION,
    )
    return replay_detection, replay_assessment


def _record_field(record: object, name: str) -> object:
    if isinstance(record, Mapping):
        return record.get(name)
    return getattr(record, name, None)


def _text_sequence(value: object) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, str | bytes):
        return ()
    if any(not isinstance(item, str) or not item for item in value):
        return ()
    return tuple(value)


def _history_parent_metadata(
    *,
    prior: ExplorationWork,
    next_location: str,
) -> dict[str, str]:
    """记录浏览器历史直接父页，并让同 URL 弹窗动作继承该来源。"""
    current_location = prior.frontier.observation.location
    if _same_history_location(current_location, next_location):
        metadata = prior.frontier.observation.metadata
        parent_state = metadata.get("history_parent_state_version")
        parent_location = metadata.get("history_parent_location")
        if (
            isinstance(parent_state, str)
            and parent_state.strip()
            and isinstance(parent_location, str)
            and parent_location.strip()
        ):
            return {
                "history_parent_state_version": parent_state,
                "history_parent_location": parent_location,
            }
        return {}
    return {
        "history_parent_state_version": prior.frontier.observation.state_version,
        "history_parent_location": current_location,
    }


def _same_history_location(left: str, right: str) -> bool:
    return canonical_h5_route_identity(left) == canonical_h5_route_identity(right)


def _next_depth(work: ExplorationWork, turn: ExplorerTurnResult) -> int:
    decision = turn.decision
    if not isinstance(decision, ActionIntent):
        raise ValueError("H5 后继需要 ActionIntent")
    candidate_id = decision.parameters.get("candidate_id")
    for candidate in work.frontier.candidates:
        if candidate.candidate_id == candidate_id:
            return candidate.navigation_depth
    raise ValueError("ActionIntent 未绑定当前 Frontier 候选")


def _next_line_loading_reobserve_count(
    *,
    prior: ExplorationWork,
    turn: ExplorerTurnResult,
) -> int:
    raw_count = prior.frontier.observation.metadata.get("line_loading_reobserve_count", 0)
    count = raw_count if isinstance(raw_count, int) and not isinstance(raw_count, bool) else 0
    decision = turn.decision
    if (
        prior.frontier.observation.page_type == "line_loading"
        and isinstance(decision, ActionIntent)
        and decision.action_type is InteractionOperation.OBSERVE
    ):
        return count + 1
    return count


def _next_startup_ad_countdown_reobserve_count(
    *,
    prior: ExplorationWork,
    turn: ExplorerTurnResult,
) -> int:
    raw_count = prior.frontier.observation.metadata.get(
        "startup_ad_countdown_reobserve_count",
        0,
    )
    count = raw_count if isinstance(raw_count, int) and not isinstance(raw_count, bool) else 0
    decision = turn.decision
    if prior.frontier.observation.page_type != "startup_ad" or not isinstance(
        decision,
        ActionIntent,
    ):
        return count
    candidate_id = decision.parameters.get("candidate_id")
    selected = next(
        (
            candidate
            for candidate in prior.frontier.candidates
            if candidate.candidate_id == candidate_id
        ),
        None,
    )
    parameters = selected.metadata.get("action_parameters") if selected is not None else None
    if (
        selected is not None
        and selected.action_type is InteractionOperation.CLICK
        and isinstance(parameters, Mapping)
        and parameters.get("target_mode") == "startup_ad_dismissal"
    ):
        # 当前广告层已经验真清除，下一层的倒计时等待预算从零开始；
        # 广告关闭总次数由 startup_ad_dismissal_count 独立保持单调上限。
        return 0
    if decision.action_type is InteractionOperation.OBSERVE:
        return count + 1
    return count


def _next_startup_ad_dismissal_count(
    *,
    prior: ExplorationWork,
    turn: ExplorerTurnResult,
) -> int:
    """累计连续开屏关闭次数；只信任 Frontier 已签发的平台能力。"""
    raw_count = prior.frontier.observation.metadata.get("startup_ad_dismissal_count", 0)
    count = raw_count if isinstance(raw_count, int) and not isinstance(raw_count, bool) else 0
    decision = turn.decision
    if prior.frontier.observation.page_type != "startup_ad" or not isinstance(
        decision,
        ActionIntent,
    ):
        return count
    candidate_id = decision.parameters.get("candidate_id")
    selected = next(
        (
            candidate
            for candidate in prior.frontier.candidates
            if candidate.candidate_id == candidate_id
        ),
        None,
    )
    if selected is None:
        return count
    parameters = selected.metadata.get("action_parameters")
    if (
        selected.action_type is InteractionOperation.CLICK
        and isinstance(parameters, Mapping)
        and parameters.get("target_mode") == "startup_ad_dismissal"
    ):
        return count + 1
    return count


def _completed_popup_absence_reobservation_candidate(
    *,
    prior: ExplorationWork,
    turn: ExplorerTurnResult,
) -> CandidateAction | None:
    """返回本轮完成的平台弹窗自动退场 Observe，并保留其遮挡签名。"""
    decision = turn.decision
    if (
        not isinstance(decision, ActionIntent)
        or decision.action_type is not InteractionOperation.OBSERVE
    ):
        return None
    candidate_id = decision.parameters.get("candidate_id")
    selected = next(
        (
            candidate
            for candidate in prior.frontier.candidates
            if candidate.candidate_id == candidate_id
        ),
        None,
    )
    if (
        selected is not None
        and selected.action_type is InteractionOperation.OBSERVE
        and selected.semantic_target in {"重新观察已自动退场的弹窗", "重新核对弹窗状态"}
        and selected.risk_level is InteractionRiskLevel.LOW
    ):
        return selected
    return None


def _next_flutter_bootstrap_reobserve_count(
    *,
    prior: ExplorationWork,
    turn: ExplorerTurnResult,
) -> int:
    raw_count = prior.frontier.observation.metadata.get(
        "flutter_bootstrap_reobserve_count",
        0,
    )
    count = raw_count if isinstance(raw_count, int) and not isinstance(raw_count, bool) else 0
    risk_flags = prior.frontier.observation.metadata.get("risk_flags")
    normalized_risk_flags = (
        {str(item).strip().casefold() for item in risk_flags if str(item).strip()}
        if isinstance(risk_flags, (list, tuple, set, frozenset))
        else set()
    )
    decision = turn.decision
    if (
        prior.frontier.observation.page_type is None
        and prior.frontier.observation.page_state == "needs_more_observation"
        and "deterministic_flutter_bootstrap_pending" in normalized_risk_flags
        and isinstance(decision, ActionIntent)
        and decision.action_type is InteractionOperation.OBSERVE
    ):
        return count + 1
    return count


def _next_insufficient_reobserve_count(
    *,
    prior: ExplorationWork,
    turn: ExplorerTurnResult,
) -> int:
    """只累计平台签发的普通未稳定页 Observe，避免其他观察动作串账。"""

    raw_count = prior.frontier.observation.metadata.get("insufficient_reobserve_count", 0)
    count = raw_count if isinstance(raw_count, int) and not isinstance(raw_count, bool) else 0
    decision = turn.decision
    if (
        isinstance(decision, ActionIntent)
        and decision.action_type is InteractionOperation.OBSERVE
    ):
        candidate_id = decision.parameters.get("candidate_id")
        selected = next(
            (
                candidate
                for candidate in prior.frontier.candidates
                if candidate.candidate_id == candidate_id
            ),
            None,
        )
        parameters = selected.metadata.get("action_parameters") if selected is not None else None
        if (
            selected is not None
            and isinstance(parameters, Mapping)
            and parameters.get("target_mode") == "insufficient_reobserve"
        ):
            return count + 1
    if (
        isinstance(decision, ActionIntent)
        and decision.action_type is not InteractionOperation.OBSERVE
    ):
        return 0
    return count


def _next_ambiguous_launch_shell_reobserve_count(
    *,
    prior: ExplorationWork,
    turn: ExplorerTurnResult,
) -> int:
    """只累计平台签发的模糊启动壳层零触碰 Observe。"""
    raw_count = prior.frontier.observation.metadata.get(
        "ambiguous_launch_shell_reobserve_count",
        0,
    )
    count = raw_count if isinstance(raw_count, int) and not isinstance(raw_count, bool) else 0
    decision = turn.decision
    if (
        prior.frontier.observation.metadata.get("ambiguous_launch_shell") is True
        and isinstance(decision, ActionIntent)
        and decision.action_type is InteractionOperation.OBSERVE
    ):
        candidate_id = decision.parameters.get("candidate_id")
        selected = next(
            (
                candidate
                for candidate in prior.frontier.candidates
                if candidate.candidate_id == candidate_id
            ),
            None,
        )
        parameters = selected.metadata.get("action_parameters") if selected is not None else None
        if (
            isinstance(parameters, Mapping)
            and parameters.get("target_mode") == "ambiguous_launch_shell_reobserve"
        ):
            return count + 1
    return count


def _next_logical_reobserve_count(
    *,
    prior: ExplorationWork,
    turn: ExplorerTurnResult,
) -> int:
    """只累计平台签发的逻辑页面复验 Observe。"""
    raw_count = prior.frontier.observation.metadata.get("logical_reobserve_count", 0)
    count = raw_count if isinstance(raw_count, int) and not isinstance(raw_count, bool) else 0
    decision = turn.decision
    if (
        not isinstance(decision, ActionIntent)
        or decision.action_type is not InteractionOperation.OBSERVE
    ):
        return count
    candidate_id = decision.parameters.get("candidate_id")
    selected = next(
        (
            candidate
            for candidate in prior.frontier.candidates
            if candidate.candidate_id == candidate_id
        ),
        None,
    )
    parameters = selected.metadata.get("action_parameters") if selected is not None else None
    if (
        selected is not None
        and isinstance(parameters, Mapping)
        and parameters.get("target_mode") == "logical_surface_reobserve"
    ):
        return count + 1
    return count


def _next_capability_projection_reobserve_count(
    *,
    prior: ExplorationWork,
    turn: ExplorerTurnResult,
) -> int:
    """只累计平台签发的能力投影失败重观察，避免普通 Observe 串账。"""
    raw_count = prior.frontier.observation.metadata.get(
        "capability_projection_reobserve_count",
        0,
    )
    count = raw_count if isinstance(raw_count, int) and not isinstance(raw_count, bool) else 0
    decision = turn.decision
    if (
        not isinstance(decision, ActionIntent)
        or decision.action_type is not InteractionOperation.OBSERVE
    ):
        return count
    candidate_id = decision.parameters.get("candidate_id")
    selected = next(
        (
            candidate
            for candidate in prior.frontier.candidates
            if candidate.candidate_id == candidate_id
        ),
        None,
    )
    parameters = selected.metadata.get("action_parameters") if selected is not None else None
    if (
        selected is not None
        and isinstance(parameters, Mapping)
        and parameters.get("target_mode") == "capability_projection_reobserve"
    ):
        return count + 1
    return count


def _turn_executes_logical_reobserve(
    *,
    prior: ExplorationWork,
    turn: ExplorerTurnResult,
) -> bool:
    decision = turn.decision
    if not isinstance(decision, ActionIntent):
        return False
    candidate_id = decision.parameters.get("candidate_id")
    selected = next(
        (
            candidate
            for candidate in prior.frontier.candidates
            if candidate.candidate_id == candidate_id
        ),
        None,
    )
    parameters = selected.metadata.get("action_parameters") if selected is not None else None
    return bool(
        selected is not None
        and selected.action_type is InteractionOperation.OBSERVE
        and isinstance(parameters, Mapping)
        and parameters.get("target_mode") == "logical_surface_reobserve"
    )


def _observed_scope_was_already_covered(
    *,
    prior: ExplorationWork,
    frontier: object,
) -> bool:
    """判断当前逻辑状态是否已有可信覆盖事实，避免重复复验把任务改成失败。"""
    observation = getattr(frontier, "observation", None)
    page_type = getattr(observation, "page_type", None)
    location = getattr(observation, "location", None)
    page_state = getattr(observation, "page_state", None)
    observed_identity = (page_type, location, page_state)
    if not all(isinstance(value, str) and value.strip() for value in observed_identity):
        return False
    raw_coverage = prior.frontier.observation.metadata.get(H5_COVERAGE_METADATA_KEY)
    if not isinstance(raw_coverage, Mapping):
        return False
    try:
        coverage = H5CoverageSnapshot.from_dict(raw_coverage)
    except (TypeError, ValueError):
        return False
    route_identity = canonical_h5_route_identity(location)
    normalized_page_type = page_type.strip().casefold()
    normalized_page_state = page_state.strip().casefold()
    return any(
        str(scope.get("assessment_status") or "") == "covered"
        and str(scope.get("route_identity") or "") == route_identity
        and str(scope.get("page_type") or "").strip().casefold() == normalized_page_type
        and str(scope.get("page_state") or "").strip().casefold() == normalized_page_state
        for scope in coverage.scopes
    )


def _selected_candidate_id(turn: ExplorerTurnResult) -> str:
    decision = turn.decision
    if not isinstance(decision, ActionIntent):
        raise ValueError("H5 后继需要 ActionIntent")
    value = decision.parameters.get("candidate_id")
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError("ActionIntent 缺少当前 Frontier candidate_id")
    return value


def _executed_candidate_ids(work: ExplorationWork) -> tuple[str, ...]:
    raw = work.frontier.observation.metadata.get("executed_candidate_ids")
    if not isinstance(raw, (list, tuple)):
        return ()
    values: list[str] = []
    for item in raw:
        if isinstance(item, str) and item.strip() and item == item.strip() and item not in values:
            values.append(item)
    return tuple(values)


def _task_frontier_from_work(work: ExplorationWork) -> H5TaskFrontierSnapshot:
    payload = work.frontier.observation.metadata.get(H5_TASK_FRONTIER_METADATA_KEY)
    if isinstance(payload, Mapping):
        return H5TaskFrontierSnapshot.from_dict(payload)
    snapshot = H5TaskFrontierSnapshot.create(
        max_pages=_max_pages(work.frontier.observation.metadata.get("sampling_policy_summary", {})),
        sampling_policy_summary=work.frontier.observation.metadata.get(
            "sampling_policy_summary", {}
        ),
    ).merge(
        source_capture_id=work.source_capture_id,
        source_state_version=work.frontier.observation.state_version,
        source_location=work.frontier.observation.location,
        navigation_depth=int(work.frontier.observation.metadata.get("navigation_depth", 0)),
        candidates=work.frontier.candidates,
        max_depth=work.budget.max_depth,
        now=work.frontier.observation.observed_at,
    )
    selected_id = _selected_candidate_id_from_work(work)
    entry = next(
        (item for item in snapshot.pending_entries if item.candidate.candidate_id == selected_id),
        None,
    )
    if entry is None:
        raise ValueError("H5 Work 缺少可恢复的 selected Frontier entry")
    return snapshot.select_entry(entry.entry_id, selected_at=work.frontier.observation.observed_at)


def _h5_required_page_types_from_policy(
    policy: Mapping[str, object] | None,
) -> tuple[str, ...]:
    """读取任务冻结的 H5 必检页型；不接受候选自报优先级。"""

    if not isinstance(policy, Mapping):
        return ()
    for key in ("required_page_types", "include_page_types"):
        values = policy.get(key)
        if isinstance(values, (list, tuple, set, frozenset)):
            return tuple(
                sorted(
                    {
                        value.strip().casefold()
                        for value in values
                        if isinstance(value, str) and value.strip()
                    }
                )
            )
    return ()


def _h5_required_page_types(work: ExplorationWork) -> tuple[str, ...]:
    coverage = work.frontier.observation.metadata.get(H5_COVERAGE_METADATA_KEY)
    if isinstance(coverage, Mapping):
        values = coverage.get("required_page_types")
        if isinstance(values, (list, tuple, set, frozenset)):
            return tuple(
                sorted(
                    {
                        value.strip().casefold()
                        for value in values
                        if isinstance(value, str) and value.strip()
                    }
                )
            )
    return _h5_required_page_types_from_policy(
        work.frontier.observation.metadata.get("sampling_policy_summary")
    )


def _h5_evidence_gap_page_types(work: ExplorationWork) -> tuple[str, ...]:
    """读取 H5 Coverage/证据 writer 已落盘的页面缺口事实。"""

    metadata = work.frontier.observation.metadata
    return _h5_evidence_gap_page_types_from_metadata(
        metadata,
        known_page_types=_h5_required_page_types(work),
    )


def _h5_evidence_gap_page_types_from_metadata(
    metadata: Mapping[str, object],
    *,
    known_page_types: Sequence[str] = (),
) -> tuple[str, ...]:
    """从已经合并的 H5 Coverage/规则事实投影页面证据缺口。"""

    return evidence_gap_page_types_from_facts(
        metadata,
        known_page_types=known_page_types,
    )


def _h5_missing_page_types(work: ExplorationWork) -> tuple[str, ...]:
    required = set(_h5_required_page_types(work))
    coverage = work.frontier.observation.metadata.get(H5_COVERAGE_METADATA_KEY)
    covered: set[str] = set()
    if isinstance(coverage, Mapping):
        raw_covered = coverage.get("covered_page_types")
        if isinstance(raw_covered, (list, tuple, set, frozenset)):
            covered = {
                value.strip().casefold()
                for value in raw_covered
                if isinstance(value, str) and value.strip()
            }
    else:
        raw_covered = work.frontier.observation.metadata.get("covered_page_types")
        if isinstance(raw_covered, (list, tuple, set, frozenset)):
            covered = {
                value.strip().casefold()
                for value in raw_covered
                if isinstance(value, str) and value.strip()
            }
    # ``missing_page_types`` 只表达尚未覆盖的必检页型；证据已存在但质量不
    # 足时由独立 evidence-gap 队列承接，避免同一页型同时被当成必检和补采。
    return tuple(sorted(required - covered))


def _h5_page_fingerprint(frontier) -> str | None:
    metadata = frontier.observation.metadata
    for key in ("logical_surface_identity_fingerprint", "business_page_identity"):
        value = metadata.get(key)
        if isinstance(value, str) and value.strip():
            identity = value.strip()
            scroll_material = _scroll_state_material(metadata)
            return (
                identity
                if not scroll_material
                else stable_protocol_hash(
                    {"page_identity": identity, "scroll_state": scroll_material}
                )
            )
    return stable_protocol_hash(
        {
            "location": frontier.observation.location,
            "page_type": frontier.observation.page_type,
            "page_state": frontier.observation.page_state,
            "scroll_state": _scroll_state_material(metadata),
        }
    )


def _scroll_state_material(metadata: Mapping[str, Any]) -> dict[str, object]:
    """保留页面内滚动/Tab 状态，避免同一页合法状态被判成重复页。"""

    keys = (
        "scroll_state",
        "scroll_y",
        "scroll_offset",
        "scroll_position",
        "scroll_top",
        "visible_range",
        "viewport_offset",
        "scroll_signature",
        "scroll_container_signature",
        "active_tab",
        "active_tab_index",
    )
    return {
        key: metadata[key]
        for key in keys
        if key in metadata and isinstance(metadata[key], (str, int, float, bool, list, tuple, dict))
    }


def _h5_popup_fingerprint(frontier) -> str | None:
    value = frontier.observation.overlay_signature
    if isinstance(value, str) and value.strip():
        return value.strip()
    metadata = frontier.observation.metadata
    for key in ("obstruction_signature", "popup_signature", "overlay_signature"):
        value = metadata.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _h5_return_fingerprint(
    *,
    prior: ExplorationWork,
    candidate: CandidateAction | None,
    frontier,
) -> str | None:
    if candidate is None or candidate.action_type is not InteractionOperation.BACK:
        return None
    return stable_protocol_hash(
        {
            "source": prior.frontier.observation.location,
            "target": frontier.observation.location,
            # 候选 ID 通常绑定本轮 Capture；将其放进返回签名会把同一条
            # A→B→A→B 路径误认为每次都是新循环，导致无法止损。
            "target_page_type": frontier.observation.page_type,
        }
    )


def _h5_scheduler_observation_candidate(
    candidate: CandidateAction | None,
) -> bool:
    """判断由页面生命周期合同管理的无副作用重观察候选。

    这类候选不代表一次新的业务导航动作，动作额度和循环止损由各自的
    line_loading/startup/Flutter 重观察上限负责；仍会进入调度账本的时间和
    失败统计，避免观察轮绕过全局时间窗口。
    """

    if candidate is None or candidate.action_type is not InteractionOperation.OBSERVE:
        return False
    parameters = candidate.metadata.get("action_parameters")
    target_mode = parameters.get("target_mode") if isinstance(parameters, Mapping) else None
    if isinstance(target_mode, str) and target_mode.endswith("_reobserve"):
        return True
    return any(
        key.endswith("_reobserve_attempt") for key in candidate.metadata if isinstance(key, str)
    )


def _rebind_selected_task_entry_to_fresh_frontier(
    *,
    snapshot: H5TaskFrontierSnapshot,
    frontier,
) -> H5TaskFrontierSnapshot:
    """按既有业务身份把 selected sibling 换成当前 Capture 的可信候选。"""
    selected = tuple(
        entry for entry in snapshot.entries if entry.status is H5FrontierEntryStatus.SELECTED
    )
    if len(selected) != 1:
        raise ValueError("H5 来源恢复缺少唯一 selected Frontier entry")
    selected_entry = selected[0]
    selected_identity = _candidate_reobservation_identity(selected_entry.candidate)
    matches = tuple(
        candidate
        for candidate in frontier.candidates
        if _candidate_reobservation_identity(candidate) == selected_identity
    )
    if len(matches) != 1:
        raise H5FreshCandidateRebindRejected("H5 来源恢复缺少唯一同业务身份的新鲜候选")
    fresh_candidate = matches[0]
    selected_metadata = selected_entry.candidate.metadata
    if "source_refresh_attempt_count" in selected_metadata:
        fresh_candidate = replace(fresh_candidate, metadata={
            **fresh_candidate.metadata,
            "source_refresh_attempt_count": selected_metadata["source_refresh_attempt_count"],
        })
    if selected_metadata.get("logical_target_required") is True:
        fresh_metadata = fresh_candidate.metadata
        observation = frontier.observation
        observation_metadata = observation.metadata
        old_identity = selected_metadata.get("source_identity_fingerprint")
        old_page_type = selected_metadata.get("source_page_type")
        fresh_identity = fresh_metadata.get("source_identity_fingerprint")
        fresh_state = fresh_metadata.get("source_state_fingerprint")
        fresh_page_type = fresh_metadata.get("source_page_type")
        if (
            not isinstance(observation_metadata, Mapping)
            or observation_metadata.get("logical_targets_effective") is not True
            or fresh_metadata.get("logical_target_required") is not True
            or not isinstance(old_identity, str)
            or fresh_identity != old_identity
            or observation_metadata.get("logical_surface_identity_fingerprint") != old_identity
            or not isinstance(fresh_state, str)
            or observation_metadata.get("logical_surface_state_fingerprint") != fresh_state
            or not isinstance(old_page_type, str)
            or not isinstance(fresh_page_type, str)
            or fresh_page_type.casefold() != old_page_type.casefold()
            or not isinstance(observation.page_type, str)
            or observation.page_type.casefold() != old_page_type.casefold()
        ):
            raise H5FreshCandidateRebindRejected("H5 来源恢复的新鲜候选缺少可信逻辑绑定")
    if fresh_candidate.navigation_depth != selected_entry.candidate.navigation_depth:
        raise H5FreshCandidateRebindRejected("H5 来源恢复的新鲜候选导航深度不一致")
    observation = frontier.observation
    rebound_entry = replace(
        selected_entry,
        candidate=fresh_candidate,
        source_capture_id=observation.capture_id,
        source_state_version=observation.state_version,
        source_location=observation.location,
        lineage=tuple(dict.fromkeys((*selected_entry.lineage, observation.state_version))),
    )
    return _transition_snapshot(
        snapshot,
        entry_id=selected_entry.entry_id,
        replacement=rebound_entry,
        active_state_version=observation.state_version,
    )


def rebind_h5_selected_entry_to_current_observation(
    work: ExplorationWork,
) -> ExplorationWork:
    """把已由当前观察重新证明的 selected 候选绑定到当前 Capture。"""

    payload = work.frontier.observation.metadata.get(H5_TASK_FRONTIER_METADATA_KEY)
    if not isinstance(payload, Mapping):
        raise ValueError("H5 当前来源重绑定缺少 Task Frontier")
    snapshot = H5TaskFrontierSnapshot.from_dict(payload)
    selected = tuple(
        entry for entry in snapshot.entries if entry.status is H5FrontierEntryStatus.SELECTED
    )
    if len(selected) != 1:
        raise ValueError("H5 当前来源重绑定缺少唯一 selected Frontier entry")
    selected_entry = selected[0]
    observation = work.frontier.observation
    if (
        selected_entry.source_capture_id == observation.capture_id
        and selected_entry.source_state_version == observation.state_version
        and selected_entry.source_location == observation.location
    ):
        return work

    candidate = selected_entry.candidate
    candidate_metadata = dict(candidate.metadata)
    if candidate_metadata.get("logical_target_required") is True:
        observation_metadata = observation.metadata
        current_identity = observation_metadata.get(
            "logical_surface_identity_fingerprint"
        )
        current_state = observation_metadata.get("logical_surface_state_fingerprint")
        if (
            observation_metadata.get("logical_targets_effective") is not True
            or not isinstance(current_identity, str)
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", current_identity)
            or not isinstance(current_state, str)
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", current_state)
            or not isinstance(observation.page_type, str)
            or not observation.page_type.strip()
        ):
            raise H5FreshCandidateRebindRejected(
                "H5 当前观察缺少可信逻辑来源，不能重绑定旧候选"
            )
        candidate_metadata.update(
            {
                "source_identity_fingerprint": current_identity,
                "source_state_fingerprint": current_state,
                "source_page_type": observation.page_type.casefold(),
                "source_page_state": observation.page_state.casefold(),
            }
        )
    candidate_metadata["current_source_rebind"] = {
        "protocol_version": "h5-current-source-rebind.v1",
        "original_candidate_id": candidate.candidate_id,
        "original_source_capture_id": selected_entry.source_capture_id,
        "original_source_state_version": selected_entry.source_state_version,
        "current_source_capture_id": observation.capture_id,
        "current_source_state_version": observation.state_version,
    }
    rebound_candidate = replace(
        candidate,
        candidate_id="h5-candidate:"
        + stable_protocol_hash(
            {
                "protocol_version": "h5-current-source-rebind.v1",
                "action_type": candidate.action_type.value,
                "semantic_target": candidate.semantic_target,
                "expected_next_page_type": candidate_metadata.get(
                    "expected_next_page_type"
                ),
                "action_parameters": candidate_metadata.get("action_parameters"),
                "source_capture_id": observation.capture_id,
                "source_state_version": observation.state_version,
                "source_identity_fingerprint": candidate_metadata.get(
                    "source_identity_fingerprint"
                ),
                "source_state_fingerprint": candidate_metadata.get(
                    "source_state_fingerprint"
                ),
            }
        ),
        metadata=candidate_metadata,
    )
    rebound_entry = replace(
        selected_entry,
        candidate=rebound_candidate,
        source_capture_id=observation.capture_id,
        source_state_version=observation.state_version,
        source_location=observation.location,
        lineage=tuple(
            dict.fromkeys((*selected_entry.lineage, observation.state_version))
        ),
    )
    snapshot = _transition_snapshot(
        snapshot,
        entry_id=selected_entry.entry_id,
        replacement=rebound_entry,
        active_state_version=observation.state_version,
    )
    frontier = _with_task_frontier(frontier=work.frontier, snapshot=snapshot)
    return replace(
        work,
        source_capture_id=observation.capture_id,
        frontier_snapshot_id=frontier.snapshot_id,
        frontier=frontier,
    )


def _with_task_frontier(
    *,
    frontier,
    snapshot: H5TaskFrontierSnapshot,
    assessment: Mapping[str, object] | None = None,
):
    # Frontier 随 ExplorationWork 内嵌；仅折叠已完成历史，活动候选不裁剪。
    snapshot = compact_h5_task_frontier(snapshot)
    selected = next(
        (entry for entry in snapshot.entries if entry.status.value == "selected"),
        None,
    )
    projected_candidates = (selected.candidate,) if selected is not None else ()
    projected_candidate_ids = {candidate.candidate_id for candidate in projected_candidates}
    metadata = dict(frontier.observation.metadata)
    metadata[H5_TASK_FRONTIER_METADATA_KEY] = snapshot.to_dict()
    metadata["h5_task_frontier_selected_entry_id"] = (
        selected.entry_id if selected is not None else None
    )
    if assessment is not None:
        coverage = H5CoverageSnapshot.advance(
            previous=(
                metadata.get(H5_COVERAGE_METADATA_KEY)
                if isinstance(metadata.get(H5_COVERAGE_METADATA_KEY), Mapping)
                else None
            ),
            frontier=snapshot,
            location=frontier.observation.location,
            capture_id=frontier.observation.capture_id,
            page_type=frontier.observation.page_type,
            page_state=frontier.observation.page_state,
            assessment=assessment,
            requires_auth=metadata.get("requires_auth") is True,
            overlay_present=frontier.observation.overlay_signature is not None,
        )
        metadata[H5_COVERAGE_METADATA_KEY] = coverage.to_dict()
        metadata["objective_satisfied"] = coverage.completion_ready
    elif isinstance(metadata.get(H5_COVERAGE_METADATA_KEY), Mapping):
        coverage = H5CoverageSnapshot.from_dict(metadata[H5_COVERAGE_METADATA_KEY])
        coverage = coverage.reconcile_frontier(
            frontier=snapshot,
            requires_auth=metadata.get("requires_auth") is True,
            overlay_present=frontier.observation.overlay_signature is not None,
        )
        metadata[H5_COVERAGE_METADATA_KEY] = coverage.to_dict()
        metadata["objective_satisfied"] = coverage.completion_ready
    elif snapshot.entries:
        metadata["objective_satisfied"] = (
            selected is None
            and snapshot.pending_count == 0
            and snapshot.blocked_count == 0
            and snapshot.failed_count == 0
            and metadata.get("requires_auth") is not True
            and frontier.observation.overlay_signature is None
        )
    if not h5_observation_allows_completion(frontier.observation):
        metadata["objective_satisfied"] = False
    return replace(
        frontier,
        snapshot_id=snapshot.snapshot_id,
        observation=replace(frontier.observation, metadata=metadata),
        candidates=projected_candidates,
        blocked_candidate_ids=tuple(
            candidate_id
            for candidate_id in frontier.blocked_candidate_ids
            if candidate_id in projected_candidate_ids
        ),
        visited_candidate_ids=tuple(
            candidate_id
            for candidate_id in frontier.visited_candidate_ids
            if candidate_id in projected_candidate_ids
        ),
    )


def _carry_h5_coverage(*, prior: ExplorationWork, metadata: dict[str, Any]) -> None:
    prior_metadata = prior.frontier.observation.metadata
    coverage = prior_metadata.get(H5_COVERAGE_METADATA_KEY)
    if isinstance(coverage, Mapping):
        metadata[H5_COVERAGE_METADATA_KEY] = dict(coverage)
    scheduler_state = prior_metadata.get(BOUNDED_EXPLORATION_STATE_KEY)
    if isinstance(scheduler_state, Mapping):
        # 后继 Work 会重新构造 lineage metadata；没有显式携带这份账本时，
        # update_scheduler_metadata 只能创建零计数状态，跨轮或失败重观察会
        # 绕过总动作、时间和重试预算。
        metadata[BOUNDED_EXPLORATION_STATE_KEY] = dict(scheduler_state)


def _carry_h5_reobservation_lineage(
    *,
    prior: ExplorationWork,
    metadata: dict[str, Any],
) -> None:
    """失败后继原样继承共享重观察预算，禁止借 stale 分支清零绕过上限。"""
    prior_metadata = prior.frontier.observation.metadata
    counter_keys = (
        "line_loading_reobserve_count",
        "startup_ad_countdown_reobserve_count",
        "startup_ad_dismissal_count",
        "flutter_bootstrap_reobserve_count",
        "insufficient_reobserve_count",
        "ambiguous_launch_shell_reobserve_count",
        "logical_reobserve_count",
        "capability_projection_reobserve_count",
    )
    for key in counter_keys:
        value = prior_metadata.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            metadata[key] = value
    for key in (
        "logical_reobserve_identity_fingerprint",
        "popup_absence_obstruction_signature",
    ):
        value = prior_metadata.get(key)
        if isinstance(value, str) and value.strip():
            metadata[key] = value.strip()
    if prior_metadata.get("popup_absence_reobserve_completed") is True:
        metadata["popup_absence_reobserve_completed"] = True


def _select_next_task_entry(
    snapshot: H5TaskFrontierSnapshot,
    *,
    active_state_version: str,
    active_source_location: str | None = None,
    active_page_type: str | None = None,
    active_page_state: str | None = None,
    active_overlay_signature: str | None = None,
    active_observed_at: datetime | None = None,
    preferred_source_state_version: str | None = None,
    preferred_source_location: str | None = None,
    scheduler_state: BoundedExplorationState | None = None,
) -> H5TaskFrontierSnapshot:
    snapshot = _skip_stale_popup_entries(
        snapshot,
        active_state_version=active_state_version,
        active_source_location=active_source_location,
        active_page_type=active_page_type,
        active_page_state=active_page_state,
        active_overlay_signature=active_overlay_signature,
        finished_at=active_observed_at,
    )
    selection_scheduler_state = _h5_candidate_scoped_scheduler_state(scheduler_state)
    # 历史父页、同页入口与普通 selector 必须共享同一批允许执行的候选。
    allowed_candidates = _BOUNDED_SCHEDULER.filter_candidates(
        tuple(entry.candidate for entry in snapshot.pending_entries),
        state=selection_scheduler_state,
        missing_page_types=(
            frozenset(snapshot.sampling_policy_summary["journey_steps"])
            - snapshot.covered_page_types
        ),
        now=active_observed_at,
    )
    allowed_ids = {candidate.candidate_id for candidate in allowed_candidates}
    if not allowed_ids:
        if snapshot.pending_count > 0 and scheduler_state is not None:
            reason = _scheduler_pending_settlement_reason(
                scheduler_state,
                observed_at=active_observed_at,
            )
            for entry in snapshot.pending_entries:
                snapshot = snapshot.mark_pending_skipped(
                    entry.entry_id,
                    reason=reason,
                    active_state_version=active_state_version,
                    finished_at=active_observed_at,
                )
        return snapshot
    if preferred_source_state_version is not None and preferred_source_location is not None:
        preferred = next(
            (
                entry
                for entry in snapshot.pending_entries
                if entry.source_state_version == preferred_source_state_version
                and _same_history_location(entry.source_location, preferred_source_location)
                and entry.candidate.candidate_id in allowed_ids
            ),
            None,
        )
        if preferred is not None:
            return snapshot.select_entry(preferred.entry_id)
    same_page = _same_active_page_pending_entry(
        snapshot,
        active_source_location=active_source_location,
        active_page_type=active_page_type,
        allowed_candidate_ids=allowed_ids,
    )
    if (
        isinstance(active_page_state, str)
        and active_page_state.casefold() == "stable"
        and active_overlay_signature is None
        and same_page is not None
        and _same_page_uncovered_branch_rank(
            candidate=same_page.candidate,
            covered_page_types=snapshot.covered_page_types,
        )
        == 0
    ):
        # “我的”页局部结构可能在一次返回帧里短暂漏检；已登记且仍未覆盖的
        # 低风险 profile sibling 必须先在当前同页重解析，禁止新底栏候选把它
        # 挤到离页之后，再通过 navigate 根地址恢复并重触发开屏广告。
        return snapshot.select_entry(same_page.entry_id)
    selected = select_next_h5_entry(
        snapshot,
        active_state_version=active_state_version,
        scheduler=_BOUNDED_SCHEDULER,
        scheduler_state=selection_scheduler_state,
        now=active_observed_at,
    )
    if selected is None:
        return snapshot
    if selected.source_state_version == active_state_version:
        return snapshot.select_entry(selected.entry_id)
    if same_page is not None:
        return snapshot.select_entry(same_page.entry_id)
    return snapshot.select_entry(selected.entry_id)


def _h5_candidate_scoped_scheduler_state(
    state: BoundedExplorationState | None,
) -> BoundedExplorationState | None:
    """H5 不用队列累计失败淘汰尚未尝试的兄弟入口。"""
    if state is None or not state.retry_attempts:
        return state
    return replace(state, retry_attempts={})


def _scheduler_pending_settlement_reason(
    state: BoundedExplorationState,
    *,
    observed_at: datetime | None,
) -> str:
    stop_reason = state.stop_reason(now=observed_at)
    if stop_reason is ExplorationStopReason.NONE:
        return "bounded_exploration:candidate_retry_budget_exhausted"
    return f"bounded_exploration:{stop_reason.value}"


def _skip_stale_popup_entries(
    snapshot: H5TaskFrontierSnapshot,
    *,
    active_state_version: str,
    active_source_location: str | None,
    active_page_type: str | None,
    active_page_state: str | None,
    active_overlay_signature: str | None,
    finished_at: datetime | None,
) -> H5TaskFrontierSnapshot:
    """稳定无遮罩页会证伪同页旧弹窗候选，跳过后继续处理真实 sibling。"""
    if (
        not isinstance(active_source_location, str)
        or not active_source_location.strip()
        or not isinstance(active_page_type, str)
        or not active_page_type.strip()
        or not isinstance(active_page_state, str)
        or active_page_state.casefold() != "stable"
        or active_overlay_signature is not None
    ):
        return snapshot
    updated = snapshot
    for entry in snapshot.pending_entries:
        candidate = entry.candidate
        parameters = candidate.metadata.get("action_parameters")
        keywords = candidate.metadata.get("target_keywords")
        if not isinstance(parameters, Mapping) or not isinstance(
            keywords, (list, tuple, set, frozenset)
        ):
            continue
        if not (
            _same_history_location(entry.source_location, active_source_location)
            and str(candidate.metadata.get("source_page_type") or "").casefold()
            == active_page_type.casefold()
            and candidate.semantic_target == "关闭当前弹窗"
            and candidate.action_type is InteractionOperation.CLICK
            and parameters.get("target_mode") == "popup_dismissal"
            and {"popup_dismissal", "close_control"}.issubset(
                {str(value).casefold() for value in keywords}
            )
        ):
            continue
        updated = updated.mark_pending_skipped(
            entry.entry_id,
            reason=_STALE_POPUP_ABSENT_REASON,
            active_state_version=active_state_version,
            finished_at=finished_at,
        )
    return updated


def _same_active_page_pending_entry(
    snapshot: H5TaskFrontierSnapshot,
    *,
    active_source_location: str | None,
    active_page_type: str | None,
    allowed_candidate_ids: set[str] | None = None,
):
    """状态版本变化但仍在同一业务页时，优先耗尽该页的安全本地候选。"""
    if (
        not isinstance(active_source_location, str)
        or not active_source_location.strip()
        or not isinstance(active_page_type, str)
        or not active_page_type.strip()
    ):
        return None
    page_type = active_page_type.casefold()
    candidates = [
        (index, entry)
        for index, entry in enumerate(snapshot.pending_entries)
        if _same_history_location(entry.source_location, active_source_location)
        and str(entry.candidate.metadata.get("source_page_type") or "").casefold() == page_type
        and (allowed_candidate_ids is None or entry.candidate.candidate_id in allowed_candidate_ids)
    ]
    if not candidates:
        return None
    return min(
        candidates,
        key=lambda pair: (
            # 同页仍有清障关闭候选时先清障，其余候选会被遮罩层拦截。
            0 if is_h5_obstruction_close_candidate(pair[1].candidate) else 1,
            _same_page_uncovered_branch_rank(
                candidate=pair[1].candidate,
                covered_page_types=snapshot.covered_page_types,
            ),
            0 if is_h5_list_scroll_coverage_candidate(pair[1].candidate) else 1,
            0 if pair[1].candidate.risk_level is InteractionRiskLevel.LOW else 1,
            -pair[1].candidate.exploration_value,
            pair[0],
        ),
    )[1]


def _same_page_uncovered_branch_rank(
    *,
    candidate,
    covered_page_types: frozenset[str],
) -> int:
    """同页回退先覆盖新页型，避免页内变体耗尽跨页导航预算。"""
    raw_page_type = candidate.metadata.get("expected_next_page_type")
    page_type = (
        raw_page_type.strip().casefold()
        if isinstance(raw_page_type, str) and raw_page_type.strip()
        else None
    )
    source_page_type = str(candidate.metadata.get("source_page_type") or "").casefold()
    raw_keywords = candidate.metadata.get("target_keywords")
    keywords = (
        {str(value).strip().casefold() for value in raw_keywords if str(value).strip()}
        if isinstance(raw_keywords, (list, tuple, set, frozenset))
        else set()
    )
    parameters = candidate.metadata.get("action_parameters")
    verified_detail = (
        "list_detail_navigation" in keywords
        and candidate.action_type is InteractionOperation.CLICK
        and candidate.risk_level is InteractionRiskLevel.LOW
        and isinstance(parameters, Mapping)
        and parameters.get("target_mode") == "list_detail_navigation"
    )
    return (
        0
        if page_type is not None
        and page_type != source_page_type
        and page_type not in covered_page_types
        and (
            bool(keywords & {"bottom_navigation", "page_tab_navigation", "profile_local_action"})
            or verified_detail
        )
        else 1
    )


def _history_parent_selection_preference(frontier: object) -> dict[str, str]:
    """详情/子页无本地候选时先返回历史父页，再处理更远 sibling。"""
    candidates = getattr(frontier, "candidates", ())
    observation = getattr(frontier, "observation", None)
    if candidates or observation is None:
        return {}
    metadata = getattr(observation, "metadata", {})
    if not isinstance(metadata, Mapping):
        return {}
    parent_state = metadata.get("history_parent_state_version")
    parent_location = metadata.get("history_parent_location")
    current_location = getattr(observation, "location", None)
    if (
        not isinstance(parent_state, str)
        or not parent_state.strip()
        or not isinstance(parent_location, str)
        or not parent_location.strip()
        or not isinstance(current_location, str)
        or _same_history_location(current_location, parent_location)
    ):
        return {}
    return {
        "preferred_source_state_version": parent_state,
        "preferred_source_location": parent_location,
    }


def _arrival_page_type_failure_reason(
    *,
    snapshot: H5TaskFrontierSnapshot,
    entry_id: str,
    actual_page_type: str | None,
) -> str | None:
    """仅在实际到达页型与候选预期一致时允许计入覆盖。"""
    selected = [
        entry
        for entry in snapshot.entries
        if entry.entry_id == entry_id and entry.status.value == "selected"
    ]
    if len(selected) != 1:
        raise ValueError("H5 Frontier selected entry 不存在")
    raw_expected = selected[0].candidate.metadata.get("expected_next_page_type")
    expected = (
        raw_expected.strip().casefold()
        if isinstance(raw_expected, str) and raw_expected.strip()
        else None
    )
    if expected is None:
        return None
    actual = (
        actual_page_type.strip().casefold()
        if isinstance(actual_page_type, str) and actual_page_type.strip()
        else None
    )
    action_parameters = selected[0].candidate.metadata.get("action_parameters")
    if (
        isinstance(action_parameters, Mapping)
        and action_parameters.get("target_mode") == "popup_dismissal"
        and actual is not None
    ):
        # 弹窗关闭后的页型描述遮挡下方真实业务页。浏览器层已经验证
        # 弹窗消失，此处不能再要求底层页型等于关闭前的识别结果。
        return None
    if actual == expected:
        return None
    if actual is None:
        return f"arrival_page_type_unverified:expected={expected}"
    return f"arrival_page_type_mismatch:expected={expected}:actual={actual}"


def _selected_entry_id(work: ExplorationWork, turn: ExplorerTurnResult) -> str:
    snapshot = _task_frontier_from_work(work)
    value = work.frontier.observation.metadata.get("h5_task_frontier_selected_entry_id")
    candidate_id = _selected_candidate_id(turn)
    selected = [item for item in snapshot.entries if item.status.value == "selected"]
    if (
        len(selected) != 1
        or value != selected[0].entry_id
        or selected[0].candidate.candidate_id != candidate_id
    ):
        raise ValueError("ActionIntent 未绑定 selected H5 Frontier entry")
    return selected[0].entry_id


def _selected_candidate_id_from_work(work: ExplorationWork) -> str:
    if len(work.frontier.candidates) != 1:
        raise ValueError("历史 H5 Work 必须只包含一个待执行候选")
    return work.frontier.candidates[0].candidate_id


def _max_pages(policy: object) -> int | None:
    if not isinstance(policy, Mapping):
        return None
    value = policy.get("max_pages")
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def _recovery_navigation_depth(snapshot: H5TaskFrontierSnapshot) -> int:
    selected = [entry for entry in snapshot.entries if entry.status.value == "selected"]
    return selected[0].navigation_depth if len(selected) == 1 else 0


def _agent_run_id(work: ExplorationWork) -> str:
    value = work.frontier.observation.metadata.get("agent_run_id")
    if isinstance(value, str) and value.strip():
        return value.strip()
    raise ValueError("H5 Work 缺少显式 agent_run_id metadata，禁止从 Session 身份推断")


def _scheduler_fact_epoch(metadata: Mapping[str, object]) -> int | None:
    """取上一轮已读取的阶段版本，防止旧 Work 跨 epoch 借用事实。"""

    scope = metadata.get("exploration_scheduler_facts_scope")
    if not isinstance(scope, Mapping):
        return None
    value = scope.get("finalization_epoch")
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None


def _append_unique(values: tuple[str, ...], value: str) -> tuple[str, ...]:
    return values if value in values else (*values, value)


def _raise_missing_recovery_capture_identity() -> str:
    raise ValueError("H5 rebaseline Capture 缺少 parent/action execution 审计身份")
