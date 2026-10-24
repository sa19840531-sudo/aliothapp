"""把 App Evidence Request 解析为受执行域约束的恢复计划。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from services.api.app.application.evidence_recovery_route import (
    EvidenceRecoveryRouteValidationError,
    plan_evidence_recovery_route,
)
from services.api.app.domain.inspection import (
    EvidenceReplayPolicy,
    EvidenceRequest,
    EvidenceRequiredEquivalence,
    RestorationPlan,
    RestorationStrategy,
    build_restoration_plan,
)
from services.api.app.domain.interaction import SurfaceKind


@dataclass(frozen=True, slots=True, kw_only=True)
class AppTargetedRecaptureContext:
    """从权威 Capture、Session 与页面图读取的补采上下文。"""

    task_id: str
    agent_run_id: str
    finalization_epoch: int
    source_session_id: str
    surface_kind: SurfaceKind
    current_capture_id: str
    target_state_id: str | None
    target_identity: str | None


class AppTargetedRecaptureContextReader(Protocol):
    """读取 Evidence Request 当前执行上下文的最小端口。"""

    async def read(
        self,
        *,
        request: EvidenceRequest,
    ) -> AppTargetedRecaptureContext | None: ...


class AppTargetedRecapturePageGraph(Protocol):
    """只查询可信恢复路线，不操作 Surface。"""

    async def get_recovery_route(
        self,
        *,
        task_id: str,
        agent_run_id: str,
        finalization_epoch: int,
        source_state_id: str,
        target_identity: str,
        limit: int = 20,
    ) -> dict[str, object]: ...


@dataclass(frozen=True, slots=True, kw_only=True)
class AppTargetedRecapturePlanResolution:
    """计划解析结果；无可执行计划时保留确定性原因。"""

    plan: RestorationPlan | None
    reason_code: str | None = None

    def __post_init__(self) -> None:
        if self.plan is None:
            if not isinstance(self.reason_code, str) or not self.reason_code.strip():
                raise ValueError("无恢复计划时必须提供 reason_code")
            return
        if self.plan.strategy is RestorationStrategy.UNFULFILLABLE:
            if not isinstance(self.reason_code, str) or not self.reason_code.strip():
                raise ValueError("不可满足计划必须提供 reason_code")
        elif self.reason_code is not None:
            raise ValueError("可执行计划不能携带 reason_code")

    @property
    def executable(self) -> bool:
        return self.plan is not None and self.plan.strategy is not RestorationStrategy.UNFULFILLABLE


class AppTargetedRecapturePlanService:
    """按当前实际状态和可信页面图生成 App 恢复计划。"""

    def __init__(
        self,
        *,
        contexts: AppTargetedRecaptureContextReader,
        page_graph: AppTargetedRecapturePageGraph,
        clock: Callable[[], datetime] | None = None,
        max_duration: timedelta = timedelta(minutes=10),
    ) -> None:
        if max_duration <= timedelta(0):
            raise ValueError("max_duration 必须大于零")
        self._contexts = contexts
        self._page_graph = page_graph
        self._clock = clock or (lambda: datetime.now(UTC))
        self._max_duration = max_duration

    async def resolve(
        self,
        *,
        request: EvidenceRequest,
        agent_run_id: str,
        finalization_epoch: int,
    ) -> AppTargetedRecapturePlanResolution:
        if not isinstance(request, EvidenceRequest):
            raise TypeError("request 必须为 EvidenceRequest")
        _validate_run_scope(
            agent_run_id=agent_run_id,
            finalization_epoch=finalization_epoch,
        )
        now = self._clock()
        if now >= request.expires_at:
            return AppTargetedRecapturePlanResolution(
                plan=None,
                reason_code="EVIDENCE_REQUEST_EXPIRED",
            )
        context = await self._contexts.read(request=request)
        if context is None:
            return AppTargetedRecapturePlanResolution(
                plan=None,
                reason_code="RECAPTURE_CONTEXT_UNAVAILABLE",
            )
        _validate_context(
            context=context,
            request=request,
            agent_run_id=agent_run_id,
            finalization_epoch=finalization_epoch,
        )
        deadline = min(request.expires_at, now + self._max_duration)
        if request.required_equivalence is EvidenceRequiredEquivalence.ORIGINAL_ATOMIC:
            return _unfulfillable(
                request=request,
                context=context,
                deadline=deadline,
                reason_code="ORIGINAL_ATOMIC_RECAPTURE_FORBIDDEN",
            )
        if context.current_capture_id == request.target_capture_id:
            return AppTargetedRecapturePlanResolution(
                plan=build_restoration_plan(
                    request=request,
                    source_session_id=context.source_session_id,
                    target_session_id=context.source_session_id,
                    surface_kind=context.surface_kind,
                    strategy=RestorationStrategy.ORIGINAL_SURFACE_PIN,
                    deadline=deadline,
                )
            )
        if (
            request.replay_policy
            in {EvidenceReplayPolicy.FORBIDDEN, EvidenceReplayPolicy.SAME_SURFACE_ONLY}
            or request.required_equivalence
            is EvidenceRequiredEquivalence.SAME_SURFACE_CONTINUATION
        ):
            return _unfulfillable(
                request=request,
                context=context,
                deadline=deadline,
                reason_code="ORIGINAL_SURFACE_NO_LONGER_CURRENT",
            )
        if context.target_state_id is None or context.target_identity is None:
            return _unfulfillable(
                request=request,
                context=context,
                deadline=deadline,
                reason_code="TARGET_GRAPH_STATE_UNAVAILABLE",
            )
        response = await self._page_graph.get_recovery_route(
            task_id=request.task_id,
            agent_run_id=agent_run_id,
            finalization_epoch=finalization_epoch,
            source_state_id=context.current_capture_id,
            target_identity=context.target_identity,
            limit=20,
        )
        try:
            route_plan = plan_evidence_recovery_route(
                response,
                task_id=request.task_id,
                agent_run_id=agent_run_id,
                finalization_epoch=finalization_epoch,
                source_state_id=context.current_capture_id,
                target_state_id=context.target_state_id,
                target_identity=context.target_identity,
            )
        except EvidenceRecoveryRouteValidationError:
            return AppTargetedRecapturePlanResolution(
                plan=None,
                reason_code="PAGE_GRAPH_ROUTE_CONTRACT_INVALID",
            )
        if route_plan.route is None:
            return _unfulfillable(
                request=request,
                context=context,
                deadline=deadline,
                reason_code=str(route_plan.reason_code),
            )
        return AppTargetedRecapturePlanResolution(
            plan=build_restoration_plan(
                request=request,
                source_session_id=context.source_session_id,
                target_session_id=context.source_session_id,
                surface_kind=context.surface_kind,
                strategy=RestorationStrategy.SAME_APP_SESSION,
                deadline=deadline,
                replay_execution_plan_id=route_plan.route.route_id,
                normalization_step_ids=route_plan.route.transition_ids,
            )
        )


def _unfulfillable(
    *,
    request: EvidenceRequest,
    context: AppTargetedRecaptureContext,
    deadline: datetime,
    reason_code: str,
) -> AppTargetedRecapturePlanResolution:
    return AppTargetedRecapturePlanResolution(
        plan=build_restoration_plan(
            request=request,
            source_session_id=context.source_session_id,
            target_session_id=context.source_session_id,
            surface_kind=context.surface_kind,
            strategy=RestorationStrategy.UNFULFILLABLE,
            deadline=deadline,
        ),
        reason_code=reason_code,
    )


def _validate_run_scope(*, agent_run_id: str, finalization_epoch: int) -> None:
    if not isinstance(agent_run_id, str) or not agent_run_id.strip():
        raise ValueError("agent_run_id 不能为空")
    if isinstance(finalization_epoch, bool) or not isinstance(finalization_epoch, int):
        raise TypeError("finalization_epoch 必须为整数")
    if finalization_epoch < 0:
        raise ValueError("finalization_epoch 必须为非负整数")


def _validate_context(
    *,
    context: AppTargetedRecaptureContext,
    request: EvidenceRequest,
    agent_run_id: str,
    finalization_epoch: int,
) -> None:
    if context.surface_kind is SurfaceKind.WEB:
        raise ValueError("App 补采上下文不能使用 Web Surface")
    expected = {
        "task_id": request.task_id,
        "agent_run_id": agent_run_id,
        "finalization_epoch": finalization_epoch,
    }
    for field_name, value in expected.items():
        if getattr(context, field_name) != value:
            raise ValueError(f"补采上下文 {field_name} 与 Runtime 执行域不一致")
