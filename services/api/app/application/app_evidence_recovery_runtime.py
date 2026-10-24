"""把 App 定向补采恢复步骤接入统一 Interaction/Capture 运行时。"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from services.api.app.application.app_screenshot_exploration import (
    AppScreenshotUnderstandingPort,
    AppVisualObservationRepositoryPort,
    ensure_app_capture_visual_observation,
)
from services.api.app.application.capture_coordination import (
    CaptureCoordinationCommand,
    CaptureCoordinationResult,
)
from services.api.app.application.capture_profiles import CaptureProfilePolicy
from services.api.app.application.evidence_recovery_replay import (
    RecoveryGraphStep,
    RecoveryObservation,
    RecoveryStepReceipt,
)
from services.api.app.application.page_graph_contracts import PageGraphRepository
from services.api.app.domain.exploration import AppVisualAnalysis
from services.api.app.domain.inspection import RestorationPlan
from services.api.app.domain.interaction import (
    ActionIntent,
    CaptureDirective,
    CaptureRef,
    InteractionOperation,
    InteractionResultStatus,
    InteractionRiskLevel,
    SurfaceKind,
    SurfaceSessionStatus,
    TimedObservationIntent,
)

_SUPPORTED_REPLAY_ACTIONS = frozenset(
    {
        InteractionOperation.CLICK,
        InteractionOperation.BACK,
        InteractionOperation.OBSERVE,
        InteractionOperation.SCROLL,
    }
)


class AppEvidenceRecoveryRuntimeError(RuntimeError):
    """App 恢复步骤不能通过现有安全运行时执行。"""

    retryable = False

    def __init__(self, *, reason_code: str, message: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code


@dataclass(frozen=True, slots=True, kw_only=True)
class AppRecoveryCandidateBinding:
    """从当前视觉事实唯一绑定出的 App 动作候选。"""

    candidate_id: str
    purpose: str
    action_type: InteractionOperation
    semantic_target: str | None

    def __post_init__(self) -> None:
        _required_text(self.candidate_id, "candidate_id")
        _required_text(self.purpose, "purpose")
        if self.action_type not in _SUPPORTED_REPLAY_ACTIONS:
            raise ValueError("action_type 不是当前允许重放的 App 动作")
        if self.action_type is InteractionOperation.CLICK:
            _required_text(self.semantic_target, "semantic_target")
        elif self.action_type is InteractionOperation.SCROLL:
            # 有界滚动允许无语义标签，但仍须是白名单内低风险动作。
            if self.semantic_target is not None:
                _required_text(self.semantic_target, "semantic_target")
        elif self.semantic_target is not None:
            _required_text(self.semantic_target, "semantic_target")


class AppRecoveryCaptureCoordinator(Protocol):
    """正式 App 补采只能使用的 CaptureCoordinator 最小端口。"""

    async def capture_observation(
        self,
        *,
        agent_run_id: str,
        intent: TimedObservationIntent,
        allow_draining: bool = False,
        semantic_role: str = "surface_reobserve",
    ) -> CaptureRef: ...

    async def coordinate(
        self,
        command: CaptureCoordinationCommand,
    ) -> CaptureCoordinationResult: ...


class AppRecoverySurfaceSessions(Protocol):
    """读取当前 Session Capture 游标与 Epoch。"""

    async def get(self, *, session_id: str) -> Any | None: ...


class AppRecoveryFacts(Protocol):
    """从新 Capture 读取页面事实，并在新事实上绑定候选。"""

    async def read(
        self,
        *,
        capture: CaptureRef,
        agent_run_id: str,
        finalization_epoch: int,
    ) -> RecoveryObservation: ...

    async def bind(
        self,
        *,
        source: RecoveryObservation,
        step: RecoveryGraphStep,
    ) -> AppRecoveryCandidateBinding: ...


class AppRecoveryVisualFacts:
    """从新截图的视觉事实和页面图投影生成恢复观察与候选绑定。"""

    def __init__(
        self,
        *,
        understanding: AppScreenshotUnderstandingPort,
        observations: AppVisualObservationRepositoryPort,
        page_graph: PageGraphRepository,
    ) -> None:
        self._understanding = understanding
        self._observations = observations
        self._page_graph = page_graph

    async def read(
        self,
        *,
        capture: CaptureRef,
        agent_run_id: str,
        finalization_epoch: int,
    ) -> RecoveryObservation:
        if not isinstance(capture, CaptureRef):
            raise TypeError("capture 必须为 CaptureRef")
        saved = await ensure_app_capture_visual_observation(
            capture=capture,
            understanding=self._understanding,
            observations=self._observations,
            mismatch_message="补采恢复视觉事实与新 Capture 身份不一致",
        )
        analysis = getattr(saved, "analysis", None)
        if not isinstance(analysis, AppVisualAnalysis) or analysis.capture_id != capture.capture_id:
            raise _runtime_error(
                "RECOVERY_VISUAL_FACT_MISMATCH",
                "补采恢复未取得与新 Capture 绑定的视觉事实",
            )
        graph = await self._page_graph.get_graph(
            task_id=capture.task_id,
            agent_run_id=agent_run_id,
            finalization_epoch=finalization_epoch,
        )
        state = _current_page_graph_state(
            graph,
            capture=capture,
            agent_run_id=agent_run_id,
            finalization_epoch=finalization_epoch,
        )
        observed_at = state.get("captured_at") or getattr(saved, "observed_at", None)
        if not isinstance(observed_at, datetime) or observed_at.utcoffset() is None:
            raise _runtime_error(
                "RECOVERY_PAGE_STATE_UNAVAILABLE",
                "补采恢复页面状态缺少带时区的观察时间",
            )
        return RecoveryObservation(
            capture_id=capture.capture_id,
            page_identity=_required_graph_text(
                state.get("logical_page_key"),
                "logical_page_key",
            ),
            state_fingerprint=_required_graph_text(
                state.get("state_fingerprint"),
                "state_fingerprint",
            ),
            observed_at=observed_at,
        )

    async def bind(
        self,
        *,
        source: RecoveryObservation,
        step: RecoveryGraphStep,
    ) -> AppRecoveryCandidateBinding:
        if not isinstance(source, RecoveryObservation):
            raise TypeError("source 必须为 RecoveryObservation")
        if not isinstance(step, RecoveryGraphStep):
            raise TypeError("step 必须为 RecoveryGraphStep")
        try:
            action_type = InteractionOperation(step.action_kind)
        except ValueError as exc:
            raise _runtime_error(
                "RECOVERY_ACTION_NOT_SUPPORTED",
                "页面图动作类型不是当前 App Runtime 支持的恢复动作",
            ) from exc
        saved = await self._observations.get_by_capture_id(capture_id=source.capture_id)
        analysis = getattr(saved, "analysis", None) if saved is not None else None
        if not isinstance(analysis, AppVisualAnalysis) or analysis.capture_id != source.capture_id:
            raise _runtime_error(
                "RECOVERY_VISUAL_FACT_MISMATCH",
                "恢复动作缺少当前 Capture 的视觉事实",
            )
        candidates = tuple(
            candidate
            for candidate in analysis.candidates
            if (
                candidate.source_capture_id == source.capture_id
                and candidate.action_type is action_type
                and (
                    (
                        action_type is InteractionOperation.SCROLL
                        and step.semantic_target is None
                    )
                    or candidate.semantic_name == step.semantic_target
                )
                and candidate.risk_level is InteractionRiskLevel.LOW
                and candidate.confidence >= 0.70
            )
        )
        if not candidates:
            raise _runtime_error(
                "RECOVERY_CANDIDATE_NOT_FOUND",
                "当前视觉事实中没有可重新绑定的唯一低风险候选",
            )
        if len(candidates) != 1:
            raise _runtime_error(
                "RECOVERY_CANDIDATE_AMBIGUOUS",
                "当前视觉事实包含多个同语义候选，拒绝猜测性执行",
            )
        candidate = candidates[0]
        return AppRecoveryCandidateBinding(
            candidate_id=candidate.candidate_id,
            purpose=candidate.purpose,
            action_type=candidate.action_type,
            semantic_target=candidate.semantic_name,
        )


class AppEvidenceRecoveryRuntime:
    """重新观察 App Surface，并通过 Coordinator 单步执行恢复动作。"""

    def __init__(
        self,
        *,
        surface_sessions: AppRecoverySurfaceSessions,
        capture_coordinator: AppRecoveryCaptureCoordinator,
        facts: AppRecoveryFacts,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._surface_sessions = surface_sessions
        self._capture_coordinator = capture_coordinator
        self._facts = facts
        self._now = now or (lambda: datetime.now(UTC))

    async def observe(
        self,
        *,
        task_id: str,
        session_id: str,
        plan: RestorationPlan,
        agent_run_id: str,
        finalization_epoch: int,
    ) -> RecoveryObservation:
        self._validate_scope(
            task_id=task_id,
            session_id=session_id,
            plan=plan,
            agent_run_id=agent_run_id,
            finalization_epoch=finalization_epoch,
        )
        session = await self._surface_sessions.get(session_id=session_id)
        current_capture_id = _validated_session_capture_id(
            session,
            task_id=task_id,
            session_id=session_id,
            surface_kind=plan.surface_kind,
            finalization_epoch=finalization_epoch,
        )
        identity = _stable_identity(
            plan.plan_id,
            agent_run_id,
            str(finalization_epoch),
            current_capture_id,
            "observe",
        )
        capture = await self._capture_coordinator.capture_observation(
            agent_run_id=agent_run_id,
            intent=TimedObservationIntent(
                task_id=task_id,
                session_id=session_id,
                phase_run_id=f"targeted-recapture:{plan.plan_id}",
                decision_id=f"recovery-source:{identity}",
                observation_id=f"recovery-observation:{identity}",
                source_capture_id=current_capture_id,
                surface_kind=plan.surface_kind,
                delay_ms=0,
                directive=CaptureDirective(
                    role="observation",
                    artifact_types=(
                        "screenshot",
                        "ui_tree",
                        "ui_snapshot",
                        "visible_text",
                    ),
                ),
                deadline=plan.deadline,
            ),
            semantic_role="surface_reobserve",
        )
        _require_capture_scope(
            capture,
            task_id=task_id,
            session_id=session_id,
            surface_kind=plan.surface_kind,
            role="observation",
            parent_capture_id=current_capture_id,
        )
        return await self._read_observation(
            capture=capture,
            agent_run_id=agent_run_id,
            finalization_epoch=finalization_epoch,
        )

    async def execute(
        self,
        *,
        task_id: str,
        session_id: str,
        plan: RestorationPlan,
        agent_run_id: str,
        finalization_epoch: int,
        step: RecoveryGraphStep,
        source: RecoveryObservation,
    ) -> RecoveryStepReceipt:
        self._validate_scope(
            task_id=task_id,
            session_id=session_id,
            plan=plan,
            agent_run_id=agent_run_id,
            finalization_epoch=finalization_epoch,
        )
        if not isinstance(step, RecoveryGraphStep):
            raise TypeError("step 必须为 RecoveryGraphStep")
        if not isinstance(source, RecoveryObservation):
            raise TypeError("source 必须为 RecoveryObservation")
        try:
            action_type = InteractionOperation(step.action_kind)
        except ValueError as exc:
            raise _runtime_error(
                "RECOVERY_ACTION_NOT_SUPPORTED",
                "页面图动作类型不是当前 App Runtime 支持的恢复动作",
            ) from exc
        if action_type not in _SUPPORTED_REPLAY_ACTIONS:
            raise _runtime_error(
                "RECOVERY_ACTION_NOT_SUPPORTED",
                "当前 App 恢复只允许可从新视觉事实重新绑定的低风险动作",
            )
        binding = await self._facts.bind(source=source, step=step)
        semantic_matches = (
            action_type is InteractionOperation.SCROLL
            and step.semantic_target is None
        ) or binding.semantic_target == step.semantic_target
        if (
            binding.action_type is not action_type
            or not semantic_matches
        ):
            raise _runtime_error(
                "RECOVERY_CANDIDATE_BINDING_MISMATCH",
                "当前视觉候选与页面图恢复步骤不一致",
            )
        identity = _stable_identity(
            plan.plan_id,
            step.transition_id,
            source.capture_id,
        )
        intent = ActionIntent(
            task_id=task_id,
            session_id=session_id,
            intent_id=f"targeted-recapture-intent:{identity}",
            idempotency_key=f"targeted-recapture:{identity}",
            surface_kind=plan.surface_kind,
            source_capture_id=source.capture_id,
            action_type=action_type,
            semantic_target=binding.semantic_target,
            purpose=binding.purpose,
            capture_profile=CaptureProfilePolicy.for_surface(
                plan.surface_kind,
                action_type,
            ),
            deadline=plan.deadline,
            parameters={
                "candidate_id": binding.candidate_id,
                "evidence_request_id": plan.request_id,
                "restoration_plan_id": plan.plan_id,
                "page_graph_transition_id": step.transition_id,
                "supplemental_evidence_source": "targeted_recapture",
            },
            risk_level=InteractionRiskLevel.LOW,
        )
        result = await self._capture_coordinator.coordinate(
            CaptureCoordinationCommand(
                agent_run_id=agent_run_id,
                intent=intent,
            )
        )
        self._require_coordination_result(
            result,
            intent=intent,
            source=source,
        )
        after = await self._read_observation(
            capture=result.after_capture,
            agent_run_id=agent_run_id,
            finalization_epoch=finalization_epoch,
        )
        return RecoveryStepReceipt(
            transition_id=step.transition_id,
            action_execution_id=result.interaction.action_execution_id,
            after=after,
        )

    async def _read_observation(
        self,
        *,
        capture: CaptureRef,
        agent_run_id: str,
        finalization_epoch: int,
    ) -> RecoveryObservation:
        observation = await self._facts.read(
            capture=capture,
            agent_run_id=agent_run_id,
            finalization_epoch=finalization_epoch,
        )
        if observation.capture_id != capture.capture_id:
            raise _runtime_error(
                "RECOVERY_OBSERVATION_IDENTITY_MISMATCH",
                "页面事实与本次新 Capture 身份不一致",
            )
        return observation

    def _validate_scope(
        self,
        *,
        task_id: str,
        session_id: str,
        plan: RestorationPlan,
        agent_run_id: str,
        finalization_epoch: int,
    ) -> None:
        _required_text(task_id, "task_id")
        _required_text(session_id, "session_id")
        _required_text(agent_run_id, "agent_run_id")
        if not isinstance(plan, RestorationPlan):
            raise TypeError("plan 必须为 RestorationPlan")
        if plan.task_id != task_id or plan.target_session_id != session_id:
            raise _runtime_error(
                "RECOVERY_RUNTIME_SCOPE_MISMATCH",
                "App 恢复运行面与 RestorationPlan 作用域不一致",
            )
        if plan.surface_kind not in {SurfaceKind.ANDROID, SurfaceKind.IOS}:
            raise _runtime_error(
                "RECOVERY_RUNTIME_SCOPE_MISMATCH",
                "App 恢复运行面不接受 Web Surface",
            )
        if (
            isinstance(finalization_epoch, bool)
            or not isinstance(finalization_epoch, int)
            or finalization_epoch < 0
        ):
            raise ValueError("finalization_epoch 必须为非负整数")
        if self._now() >= plan.deadline:
            raise _runtime_error(
                "RECOVERY_DEADLINE_EXHAUSTED",
                "恢复计划已到截止时间，禁止继续运行面操作",
            )

    @staticmethod
    def _require_coordination_result(
        result: CaptureCoordinationResult,
        *,
        intent: ActionIntent,
        source: RecoveryObservation,
    ) -> None:
        if not isinstance(result, CaptureCoordinationResult):
            raise TypeError("CaptureCoordinator 必须返回 CaptureCoordinationResult")
        before = result.before_capture
        after = result.after_capture
        interaction = result.interaction
        action_execution_id = interaction.action_execution_id
        if interaction.status is not InteractionResultStatus.SUCCEEDED:
            raise _runtime_error(
                "RECOVERY_ACTION_FAILED",
                "页面图恢复动作未成功完成，停止当前路线",
            )
        if (
            before.task_id != intent.task_id
            or before.session_id != intent.session_id
            or before.surface_kind is not intent.surface_kind
            or before.role != "before_action"
            or before.parent_capture_id != source.capture_id
            or before.action_execution_id != action_execution_id
            or interaction.task_id != intent.task_id
            or interaction.session_id != intent.session_id
            or interaction.intent_id != intent.intent_id
            or interaction.source_capture_id != before.capture_id
            or interaction.result_capture_id != after.capture_id
            or after.task_id != intent.task_id
            or after.session_id != intent.session_id
            or after.surface_kind is not intent.surface_kind
            or after.role != "after_action"
            or after.parent_capture_id != before.capture_id
            or after.action_execution_id != action_execution_id
        ):
            raise _runtime_error(
                "RECOVERY_ACTION_CAPTURE_BINDING_MISMATCH",
                "恢复动作的 before/action/after 审计绑定不完整",
            )


def _validated_session_capture_id(
    session: Any,
    *,
    task_id: str,
    session_id: str,
    surface_kind: SurfaceKind,
    finalization_epoch: int,
) -> str:
    if (
        session is None
        or getattr(session, "task_id", None) != task_id
        or getattr(session, "session_id", None) != session_id
        or getattr(session, "surface_kind", None) is not surface_kind
        or getattr(session, "status", None) is not SurfaceSessionStatus.ACTIVE
        or getattr(session, "finalization_epoch", None) != finalization_epoch
    ):
        raise _runtime_error(
            "RECOVERY_SESSION_NOT_ACTIVE",
            "目标 App Session 不存在、非 ACTIVE 或已切换执行 Epoch",
        )
    capture_id = getattr(session, "current_capture_id", None)
    try:
        return _required_text(capture_id, "current_capture_id")
    except (TypeError, ValueError) as exc:
        raise _runtime_error(
            "RECOVERY_CURRENT_CAPTURE_MISSING",
            "目标 App Session 缺少当前 Capture 游标",
        ) from exc


def _current_page_graph_state(
    graph: Mapping[str, Any],
    *,
    capture: CaptureRef,
    agent_run_id: str,
    finalization_epoch: int,
) -> Mapping[str, Any]:
    if (
        not isinstance(graph, Mapping)
        or graph.get("status") != "available"
        or graph.get("task_id") != capture.task_id
        or graph.get("agent_run_id") != agent_run_id
        or graph.get("finalization_epoch") != finalization_epoch
    ):
        raise _runtime_error(
            "RECOVERY_PAGE_STATE_UNAVAILABLE",
            "补采恢复页面图尚未形成当前执行域的完整投影",
        )
    states = graph.get("states")
    if not isinstance(states, Sequence) or isinstance(states, (str, bytes)):
        raise _runtime_error(
            "RECOVERY_PAGE_STATE_UNAVAILABLE",
            "补采恢复页面图状态集合格式无效",
        )
    matches = tuple(
        state
        for state in states
        if isinstance(state, Mapping) and state.get("state_id") == capture.capture_id
    )
    if len(matches) != 1:
        raise _runtime_error(
            "RECOVERY_PAGE_STATE_UNAVAILABLE",
            "新 Capture 没有唯一页面图状态投影",
        )
    state = matches[0]
    if (
        state.get("task_id") != capture.task_id
        or state.get("agent_run_id") != agent_run_id
        or state.get("surface_session_id") != capture.session_id
        or state.get("surface_kind") != capture.surface_kind.value
        or state.get("finalization_epoch") != finalization_epoch
        or state.get("identity_status") != "confirmed"
        or state.get("counts_as_business_page") is not True
        or state.get("is_overlay") is True
        or state.get("is_external") is True
    ):
        raise _runtime_error(
            "RECOVERY_PAGE_STATE_UNAVAILABLE",
            "新 Capture 的页面图状态不是当前执行域内已确认业务页面",
        )
    return state


def _require_capture_scope(
    capture: CaptureRef,
    *,
    task_id: str,
    session_id: str,
    surface_kind: SurfaceKind,
    role: str,
    parent_capture_id: str,
) -> None:
    if (
        not isinstance(capture, CaptureRef)
        or capture.task_id != task_id
        or capture.session_id != session_id
        or capture.surface_kind is not surface_kind
        or capture.role != role
        or capture.parent_capture_id != parent_capture_id
        or capture.action_execution_id is not None
    ):
        raise _runtime_error(
            "RECOVERY_OBSERVATION_CAPTURE_MISMATCH",
            "恢复前独立观察 Capture 的身份或血缘不完整",
        )


def _stable_identity(*parts: str) -> str:
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()


def _runtime_error(reason_code: str, message: str) -> AppEvidenceRecoveryRuntimeError:
    return AppEvidenceRecoveryRuntimeError(reason_code=reason_code, message=message)


def _required_text(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError(f"{field_name} 必须为无首尾空白的非空字符串")
    return value


def _required_graph_text(value: Any, field_name: str) -> str:
    try:
        return _required_text(value, field_name)
    except (TypeError, ValueError) as exc:
        raise _runtime_error(
            "RECOVERY_PAGE_STATE_UNAVAILABLE",
            f"补采恢复页面状态缺少 {field_name}",
        ) from exc
