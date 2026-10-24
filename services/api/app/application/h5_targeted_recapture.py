"""H5 活会话定向补采的计划、恢复与租约消费。"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Callable, Mapping
from contextvars import ContextVar
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from time import monotonic
from typing import Any, Protocol

from services.api.app.application.evidence_recovery_replay import (
    EvidenceRecoveryReplayError,
    EvidenceRecoveryReplayExecutor,
)
from services.api.app.application.evidence_recovery_restoration import (
    CaptureBackedRecoverySnapshotReader,
    EvidenceRecoveryRestorationExecutor,
)
from services.api.app.application.evidence_recovery_route import (
    EvidenceRecoveryRouteValidationError,
    plan_evidence_recovery_route,
)
from services.api.app.application.evidence_requests import (
    EvidenceRequestClaimDisposition,
    EvidenceRequestLeaseReleaseDisposition,
    EvidenceRequestProcessingLease,
    EvidenceRequestRepository,
    EvidenceRequestStatus,
    ReleaseEvidenceRequestLeaseCommand,
    SettleEvidenceRequestLeaseCommand,
)
from services.api.app.application.h5_recapture_lease import (
    H5RecaptureDeadlineError,
    H5RecaptureLeaseLostError,
    run_h5_recapture_stage,
)
from services.api.app.application.h5_session_journey import (
    H5SessionJourneyDeadline,
    H5SessionJourneyLost,
    h5_session_journey,
)
from services.api.app.application.page_graph import PageGraphService
from services.api.app.application.restoration import (
    RestorationAttemptRepository,
    RestorationCoordinator,
)
from services.api.app.application.restored_probe_publication import (
    RestoredProbeOrchestrator,
    RestoredProbePublicationError,
    RestoredProbePublicationService,
)
from services.api.app.application.state_equivalence import StateEquivalenceGate
from services.api.app.application.supplemental_evidence_policy import (
    SupplementalEvidenceOperation,
    SupplementalEvidencePolicy,
)
from services.api.app.application.targeted_recapture_restoration import (
    TargetedRecaptureRestorationOutcome,
    TargetedRecaptureRestorationService,
)
from services.api.app.domain.exploration import (
    ExplorationBudget,
    consume_exploration_action_budget,
)
from services.api.app.domain.exploration.page_graph import canonical_url_identity
from services.api.app.domain.inspection import (
    EvidenceReplayPolicy,
    EvidenceRequest,
    EvidenceRequiredEquivalence,
    EvidenceSessionDependency,
    EvidenceSideEffectPolicy,
    EvidenceSourceGrade,
    InvariantComparison,
    RestorationAttempt,
    RestorationAttemptStatus,
    RestorationPlan,
    RestorationStrategy,
    RestorationValidationError,
    StateInvariantDimension,
    StateInvariantSnapshot,
    build_restoration_attempt_id,
    build_restoration_plan,
    build_state_equivalence_assessment_id,
    fingerprint_invariant,
    grade_meets_minimum,
)
from services.api.app.domain.interaction import CaptureRef, SurfaceKind
from services.api.app.domain.protocol import require_aware_datetime, stable_protocol_hash
from services.api.app.infrastructure.db.evidence_request_repository import (
    EvidenceRequestPersistenceError,
)

DEFAULT_H5_TARGETED_RECAPTURE_LEASE_SECONDS = 240

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True, kw_only=True)
class H5TargetedRecaptureContext:
    """当前 H5 Task/Run/Epoch 活会话的权威补采上下文。"""

    task_id: str
    agent_run_id: str
    finalization_epoch: int
    session_id: str
    current_capture_id: str
    target_state_id: str | None = None
    target_identity: str | None = None
    current_identity: str | None = None
    target_url: str | None = None
    current_url: str | None = None
    source_session_closed: bool = False
    replay_session_id: str | None = None
    entry_url: str | None = None
    entry_normalization_required: bool = False
    isolated_display: bool = False
    target_document_identity: str | None = None
    current_document_identity: str | None = None
    target_state_fingerprint: str | None = None
    current_state_fingerprint: str | None = None


class H5TargetedRecaptureContextReader(Protocol):
    async def read(
        self,
        *,
        request: EvidenceRequest,
    ) -> H5TargetedRecaptureContext | None: ...


def _same_h5_logical_surface(context: H5TargetedRecaptureContext) -> bool:
    """动态 Capture 只在已确认逻辑身份和规范 URL 同时一致时允许就地换绑。"""

    if (
        not context.current_identity
        or context.current_identity != context.target_identity
    ):
        return False
    current_url = canonical_url_identity(context.current_url)
    target_url = canonical_url_identity(context.target_url)
    return current_url is not None and current_url == target_url


def _same_h5_continuing_surface(
    context: H5TargetedRecaptureContext, *, require_state: bool = True,
) -> bool:
    """连续采集可以换编号，原文档、完整地址与实际逻辑状态不能换。"""
    document = context.target_document_identity
    state = context.target_state_fingerprint
    return bool(
        isinstance(document, str) and document.strip()
        and document == context.current_document_identity
        and (not require_state or (
            isinstance(state, str) and re.fullmatch(r"[0-9a-f]{64}", state)
            and state == context.current_state_fingerprint))
        and context.target_url == context.current_url
        and _same_h5_logical_surface(context)
    )


class H5TargetedRecaptureCaptureReader(Protocol):
    async def get_verified_capture(self, *, capture_id: str) -> Any | None: ...


class H5RestorationAttemptRepository(RestorationAttemptRepository, Protocol):
    async def get(self, *, attempt_id: str) -> RestorationAttempt | None: ...


class H5TargetedRecaptureSnapshotReader(Protocol):
    async def read(self, *, capture: CaptureRef) -> Any: ...


class H5TargetedRecaptureManifestFinalizer(Protocol):
    """补采前裁决目标 Capture 的 Manifest，可信读取仍只接受 pass。"""

    async def finalize_capture(self, *, task_id: str, capture_id: str) -> None: ...


class H5FinalizingRecoverySnapshotReader:
    """先裁决 H5 恢复 Capture，再交给公共可信快照读取器。"""

    def __init__(
        self,
        *,
        delegate: CaptureBackedRecoverySnapshotReader,
        manifest_finalizer: H5TargetedRecaptureManifestFinalizer,
    ) -> None:
        self._delegate = delegate
        self._manifest_finalizer = manifest_finalizer

    async def read(
        self,
        *,
        task_id: str,
        session_id: str,
        surface_kind: SurfaceKind,
        capture_id: str,
    ) -> StateInvariantSnapshot:
        await self._manifest_finalizer.finalize_capture(
            task_id=task_id,
            capture_id=capture_id,
        )
        return await self._delegate.read(
            task_id=task_id,
            session_id=session_id,
            surface_kind=surface_kind,
            capture_id=capture_id,
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class H5TargetedRecapturePlanResolution:
    plan: RestorationPlan | None
    reason_code: str | None = None
    retryable: bool = False

    @property
    def executable(self) -> bool:
        return self.plan is not None


class H5TargetedRecapturePlanService:
    """规划 H5 就地补帧或同一活会话内的可信页面图恢复。"""

    def __init__(
        self,
        *,
        contexts: H5TargetedRecaptureContextReader,
        page_graph: Any | None = None,
        clock: Callable[[], datetime] | None = None,
        max_duration: timedelta = timedelta(minutes=3),
        new_session_runtime: Any | None = None,
        closed_session_replay: Callable[..., Any] | None = None,
    ) -> None:
        if max_duration <= timedelta(0):
            raise ValueError("max_duration 必须大于零")
        self._contexts = contexts
        self._page_graph = page_graph
        self._clock = clock or (lambda: datetime.now(UTC))
        self._max_duration = max_duration
        self._new_session_runtime = new_session_runtime
        self._closed_session_replay = closed_session_replay

    async def resolve(
        self,
        *,
        request: EvidenceRequest,
        agent_run_id: str,
        finalization_epoch: int,
        deadline: datetime | None = None,
    ) -> H5TargetedRecapturePlanResolution:
        if deadline is not None:
            require_aware_datetime(deadline, "deadline")
        now = self._clock()
        if now >= request.expires_at:
            return H5TargetedRecapturePlanResolution(
                plan=None,
                reason_code="EVIDENCE_REQUEST_EXPIRED",
            )
        if request.session_dependency not in {
            EvidenceSessionDependency.ACTIVE_SESSION,
            EvidenceSessionDependency.SNAPSHOT,
        }:
            return H5TargetedRecapturePlanResolution(
                plan=None,
                reason_code="H5_RECAPTURE_SESSION_DEPENDENCY_UNSUPPORTED",
            )
        context = await self._contexts.read(request=request)
        if context is None:
            return H5TargetedRecapturePlanResolution(
                plan=None,
                reason_code="H5_RECAPTURE_CONTEXT_UNAVAILABLE",
                retryable=True,
            )
        if (
            context.task_id != request.task_id
            or context.agent_run_id != agent_run_id
            or context.finalization_epoch != finalization_epoch
        ):
            raise EvidenceRecoveryReplayError(
                reason_code="H5_RECAPTURE_CONTEXT_SCOPE_MISMATCH",
                message="H5 补采上下文与当前 Task/Run/Epoch 不一致",
            )
        deadline = min(request.expires_at, now + self._max_duration, deadline or request.expires_at)
        current = self._clock()
        if current >= deadline:
            return H5TargetedRecapturePlanResolution(
                plan=None,
                reason_code=("EVIDENCE_REQUEST_EXPIRED" if current >= request.expires_at
                             else "H5_RECAPTURE_DEADLINE_EXHAUSTED"),
            )
        interaction = request.required_interaction
        if (not context.source_session_closed and interaction is not None
            and interaction.parameters.get("goal_capability") == "payment_plan_selection"
            and request.session_dependency is EvidenceSessionDependency.SNAPSHOT
            and request.replay_policy is EvidenceReplayPolicy.NEW_SESSION_ALLOWED
            and request.required_equivalence is EvidenceRequiredEquivalence.LOGICAL_EQUIVALENT
            and request.side_effect_policy is EvidenceSideEffectPolicy.READ_ONLY
            and grade_meets_minimum(EvidenceSourceGrade.C, request.minimum_source_grade)
            and self._new_session_runtime is not None):
            # 展示补证会永久隔离该文档的异步出站；不能封锁还要继续探索的主 SPA。
            reader = getattr(self._contexts, "read_isolated_display_context", None)
            replay = await reader(request=request) if callable(reader) else None
            if (replay is None or replay.task_id != request.task_id
                or replay.agent_run_id != agent_run_id
                or replay.finalization_epoch != finalization_epoch
                or replay.session_id != context.session_id or replay.source_session_closed
                or not replay.replay_session_id or replay.replay_session_id == context.session_id):
                return H5TargetedRecapturePlanResolution(
                    plan=None, reason_code="H5_PAYMENT_ISOLATION_CONTEXT_UNAVAILABLE",
                )
            frozen_deadline = getattr(self._new_session_runtime, "deadline", None)
            if isinstance(frozen_deadline, datetime):
                deadline = min(deadline, frozen_deadline)
            if deadline <= self._clock():
                return H5TargetedRecapturePlanResolution(
                    plan=None, reason_code="H5_NEW_SESSION_DEADLINE_EXHAUSTED",
                )
            return await self._resolve_new_session(
                request=request, context=replace(replay, isolated_display=True), deadline=deadline,
            )
        if context.source_session_closed:
            frozen_deadline = getattr(self._new_session_runtime, "deadline", None)
            if isinstance(frozen_deadline, datetime):
                deadline = min(deadline, frozen_deadline)
            if deadline <= current:
                return H5TargetedRecapturePlanResolution(
                    plan=None, reason_code="H5_NEW_SESSION_DEADLINE_EXHAUSTED",
                )
            return await self._resolve_new_session(
                request=request, context=context, deadline=deadline,
            )
        if (
            context.current_capture_id == request.target_capture_id
            or _same_h5_continuing_surface(
                context, require_state=(request.required_equivalence
                                        is not EvidenceRequiredEquivalence.LOGICAL_EQUIVALENT),
            )
            or (
                (
                    request.session_dependency is EvidenceSessionDependency.SNAPSHOT
                    or (
                        request.replay_policy is EvidenceReplayPolicy.SAME_CONTEXT_ALLOWED
                        and request.required_equivalence
                        is EvidenceRequiredEquivalence.LOGICAL_EQUIVALENT
                    )
                )
                and _same_h5_logical_surface(context)
            )
        ):
            return self._build_plan(
                request=request,
                context=context,
                strategy=RestorationStrategy.ORIGINAL_SURFACE_PIN,
                deadline=deadline,
            )
        if request.session_dependency is EvidenceSessionDependency.ACTIVE_SESSION and not (
            request.replay_policy is EvidenceReplayPolicy.SAME_CONTEXT_ALLOWED
            and request.required_equivalence is EvidenceRequiredEquivalence.LOGICAL_EQUIVALENT
            and self._page_graph is not None
        ):
            return H5TargetedRecapturePlanResolution(
                plan=None,
                reason_code="H5_ACTIVE_SESSION_TARGET_DRIFTED",
            )
        if (
            self._page_graph is None
            or context.target_state_id is None
            or context.target_identity is None
        ):
            return H5TargetedRecapturePlanResolution(
                plan=None,
                reason_code="H5_TARGET_GRAPH_STATE_UNAVAILABLE",
                retryable=self._page_graph is not None,
            )
        try:
            response = await self._page_graph.get_recovery_route(
                task_id=request.task_id,
                agent_run_id=agent_run_id,
                finalization_epoch=finalization_epoch,
                source_state_id=context.current_capture_id,
                target_identity=context.target_identity,
                limit=20,
            )
            route = plan_evidence_recovery_route(
                response,
                task_id=request.task_id,
                agent_run_id=agent_run_id,
                finalization_epoch=finalization_epoch,
                source_state_id=context.current_capture_id,
                target_state_id=context.target_state_id,
                target_identity=context.target_identity,
            )
        except EvidenceRecoveryRouteValidationError:
            return H5TargetedRecapturePlanResolution(
                plan=None,
                reason_code="PAGE_GRAPH_ROUTE_CONTRACT_INVALID",
            )
        if route.route is None:
            return H5TargetedRecapturePlanResolution(
                plan=None,
                reason_code=route.reason_code or "TRUSTED_RECOVERY_ROUTE_UNAVAILABLE",
            )
        return self._build_plan(
            request=request,
            context=context,
            strategy=RestorationStrategy.SAME_CONTEXT_NEW_SURFACE,
            deadline=deadline,
            replay_execution_plan_id=route.route.route_id,
            normalization_step_ids=route.route.transition_ids,
        )

    async def can_defer_snapshot(
        self, *, request: EvidenceRequest, agent_run_id: str, finalization_epoch: int,
    ) -> bool:
        """离页前只读验证入口到目标的恢复计划，不创建会话或修改请求。"""
        reader = getattr(self._contexts, "read_snapshot_replay_context", None)
        if not callable(reader) or (
            self._new_session_runtime is None and not callable(self._closed_session_replay)
        ):
            return False
        now = self._clock()
        if (
            now >= request.expires_at
            or request.side_effect_policy is not EvidenceSideEffectPolicy.READ_ONLY
        ):
            return False
        context = await reader(request=request)
        if (
            context is None or context.task_id != request.task_id
            or context.agent_run_id != agent_run_id
            or context.finalization_epoch != finalization_epoch
            or not context.current_identity
            or context.source_session_closed
        ):
            return False
        result = await self._resolve_new_session(
            request=request, context=context,
            deadline=min(request.expires_at, now + self._max_duration),
            remember=False,
        )
        return result.executable

    async def _resolve_new_session(
        self, *, request: EvidenceRequest, context: H5TargetedRecaptureContext,
        deadline: datetime,
        remember: bool = True,
    ) -> H5TargetedRecapturePlanResolution:
        if (
            request.session_dependency is not EvidenceSessionDependency.SNAPSHOT
            or request.replay_policy is not EvidenceReplayPolicy.NEW_SESSION_ALLOWED
            or request.required_equivalence is not EvidenceRequiredEquivalence.LOGICAL_EQUIVALENT
        ):
            return H5TargetedRecapturePlanResolution(
                plan=None, reason_code="H5_NEW_SESSION_RECAPTURE_CONSTRAINT_UNSUPPORTED",
            )
        if not grade_meets_minimum(EvidenceSourceGrade.C, request.minimum_source_grade):
            return H5TargetedRecapturePlanResolution(
                plan=None, reason_code="H5_NEW_SESSION_SOURCE_GRADE_INSUFFICIENT",
            )
        if self._new_session_runtime is None and (
            remember or not callable(self._closed_session_replay)
        ):
            return H5TargetedRecapturePlanResolution(
                plan=None, reason_code="H5_SNAPSHOT_NEW_SESSION_RUNTIME_UNAVAILABLE",
            )
        if not context.entry_url or not context.replay_session_id or not context.target_identity:
            return H5TargetedRecapturePlanResolution(
                plan=None, reason_code="H5_NEW_SESSION_ENTRY_PROOF_UNAVAILABLE",
            )
        steps: tuple[str, ...] = ()
        if not _same_h5_logical_surface(context):
            if self._page_graph is None or not context.target_state_id:
                return H5TargetedRecapturePlanResolution(
                    plan=None, reason_code="H5_TARGET_GRAPH_STATE_UNAVAILABLE",
                )
            try:
                response = await self._page_graph.get_recovery_route(
                    task_id=request.task_id, agent_run_id=context.agent_run_id,
                    finalization_epoch=context.finalization_epoch,
                    source_state_id=context.current_capture_id,
                    target_identity=context.target_identity, limit=20,
                )
                route = plan_evidence_recovery_route(
                    response, task_id=request.task_id, agent_run_id=context.agent_run_id,
                    finalization_epoch=context.finalization_epoch,
                    source_state_id=context.current_capture_id,
                    target_state_id=context.target_state_id,
                    target_identity=context.target_identity,
                )
            except EvidenceRecoveryRouteValidationError:
                return H5TargetedRecapturePlanResolution(
                    plan=None, reason_code="PAGE_GRAPH_ROUTE_CONTRACT_INVALID",
                )
            if route.route is None:
                return H5TargetedRecapturePlanResolution(
                    plan=None, reason_code=route.reason_code,
                )
            steps = route.route.transition_ids
        route_id = "h5-new-session-route:" + stable_protocol_hash({
            "task_id": request.task_id, "agent_run_id": context.agent_run_id,
            "epoch": context.finalization_epoch, "entry_capture": context.current_capture_id,
            "target_capture": request.target_capture_id, "steps": steps,
        })
        resolution = self._build_plan(
            request=request, context=context, strategy=RestorationStrategy.NEW_SESSION_REPLAY,
            deadline=deadline, replay_execution_plan_id=route_id, normalization_step_ids=steps,
        )
        if resolution.plan is not None and remember:
            await self._new_session_runtime.remember_new_session_plan(
                plan=resolution.plan, context=context,
            )
        return resolution

    @staticmethod
    def _build_plan(
        *,
        request: EvidenceRequest,
        context: H5TargetedRecaptureContext,
        strategy: RestorationStrategy,
        deadline: datetime,
        replay_execution_plan_id: str | None = None,
        normalization_step_ids: tuple[str, ...] = (),
    ) -> H5TargetedRecapturePlanResolution:
        try:
            plan = build_restoration_plan(
                request=request,
                source_session_id=context.session_id,
                target_session_id=(
                    context.replay_session_id
                    if strategy is RestorationStrategy.NEW_SESSION_REPLAY else context.session_id
                ),
                surface_kind=SurfaceKind.WEB,
                strategy=strategy,
                deadline=deadline,
                replay_execution_plan_id=replay_execution_plan_id,
                normalization_step_ids=normalization_step_ids,
            )
        except RestorationValidationError:
            return H5TargetedRecapturePlanResolution(
                plan=None,
                reason_code="H5_RECAPTURE_REQUEST_CONSTRAINT_UNSUPPORTED",
            )
        return H5TargetedRecapturePlanResolution(plan=plan)


@dataclass(frozen=True, slots=True, kw_only=True)
class H5TargetedRecaptureRuntime:
    """绑定一个 H5 Run/Epoch 的恢复执行入口。"""

    captures: H5TargetedRecaptureCaptureReader
    snapshots: H5TargetedRecaptureSnapshotReader
    planning: H5TargetedRecapturePlanService
    restoration_attempts: H5RestorationAttemptRepository
    restoration: TargetedRecaptureRestorationService
    agent_run_id: str
    finalization_epoch: int
    manifest_finalizer: H5TargetedRecaptureManifestFinalizer | None = None
    recovery_runtime: Any | None = None

    async def resolve_plan(
        self,
        *,
        request: EvidenceRequest,
        deadline: datetime | None = None,
    ) -> H5TargetedRecapturePlanResolution:
        return await self.planning.resolve(
            request=request,
            agent_run_id=self.agent_run_id,
            finalization_epoch=self.finalization_epoch,
            deadline=deadline,
        )

    async def restore_and_publish(
        self,
        *,
        request: EvidenceRequest,
        plan: RestorationPlan,
        budget: ExplorationBudget,
        attempt_no: int,
        request_lease: Any,
    ) -> TargetedRecaptureRestorationOutcome:
        if self.manifest_finalizer is not None:
            await self.manifest_finalizer.finalize_capture(
                task_id=request.task_id,
                capture_id=request.target_capture_id,
            )
        persisted = await self.captures.get_verified_capture(
            capture_id=request.target_capture_id
        )
        capture = getattr(persisted, "capture", None) if persisted is not None else None
        if (
            not isinstance(capture, CaptureRef)
            or capture.task_id != request.task_id
            or capture.session_id != plan.source_session_id
            or capture.surface_kind is not SurfaceKind.WEB
        ):
            raise EvidenceRecoveryReplayError(
                reason_code="H5_RECAPTURE_TARGET_CAPTURE_SCOPE_MISMATCH",
                message="H5 补采目标 Capture 不存在或执行域不一致",
            )
        original = await self.snapshots.read(capture=capture)
        try:
            return await self.restoration.restore_and_publish(
                request=request,
                plan=plan,
                original_snapshot=original,
                budget=budget,
                attempt_no=attempt_no,
                request_lease=request_lease,
            )
        except asyncio.CancelledError:
            try:
                attempt = await self.restoration_attempts.get(
                    attempt_id=build_restoration_attempt_id(
                        plan_id=plan.plan_id, attempt_no=attempt_no,
                    )
                )
                if (isinstance(attempt, RestorationAttempt) and attempt.plan == plan
                    and attempt.status is RestorationAttemptStatus.RUNNING):
                    await self.restoration_attempts.complete(
                        attempt=replace(
                            attempt, status=RestorationAttemptStatus.FAILED,
                            error_code="H5_RESTORATION_INTERRUPTED",
                            error_message="H5 恢复执行被取消，未证明到达目标",
                            finished_at=max(datetime.now(UTC), attempt.started_at),
                        ), expected_row_version=attempt.row_version,
                    )
            except Exception:  # noqa: BLE001 - 版本冲突不能覆盖原取消
                logger.exception("H5 取消后结算恢复尝试失败 request_id=%s", request.request_id)
            raise

    async def resume_equivalent_publication(
        self,
        *,
        request: EvidenceRequest,
        budget: ExplorationBudget,
        request_lease: Any,
    ) -> TargetedRecaptureRestorationOutcome | None:
        attempt = await self.restoration_attempts.find_equivalent_for_lease(
            fence=request_lease
        )
        if attempt is None:
            return None
        if attempt.plan.strategy is RestorationStrategy.NEW_SESSION_REPLAY:
            can_resume = getattr(self.recovery_runtime, "can_resume_new_session", None)
            if not callable(can_resume) or not await can_resume(attempt=attempt):
                return None
        return await self.restoration.publish_equivalent(
            request=request,
            restoration_attempt=attempt,
            budget=budget,
            request_lease=request_lease,
        )


class H5TargetedRecaptureRuntimeFactory:
    """把 H5 Runtime 端口接到公共恢复、等价判断和安全 Probe 发布。"""

    def __init__(
        self,
        *,
        contexts: H5TargetedRecaptureContextReader,
        page_graph: Any,
        recovery_runtime: Any,
        captures: H5TargetedRecaptureCaptureReader,
        snapshots: H5TargetedRecaptureSnapshotReader,
        manifest_finalizer: H5TargetedRecaptureManifestFinalizer | None = None,
        restoration_attempts: H5RestorationAttemptRepository,
        probe_orchestrator: RestoredProbeOrchestrator,
        closed_session_replay: Callable[..., Any] | None = None,
    ) -> None:
        self._contexts = contexts
        self._page_graph = page_graph
        self._recovery_runtime = recovery_runtime
        self._captures = captures
        self._snapshots = snapshots
        self._manifest_finalizer = manifest_finalizer
        self._restoration_attempts = restoration_attempts
        self._probe_orchestrator = probe_orchestrator
        self._closed_session_replay = closed_session_replay

    def build(
        self,
        *,
        agent_run_id: str,
        finalization_epoch: int,
    ) -> H5TargetedRecaptureRuntime:
        replay = EvidenceRecoveryReplayExecutor(
            repository=self._page_graph,
            runtime=self._recovery_runtime,
        )
        recovery_snapshots = CaptureBackedRecoverySnapshotReader(
            captures=self._captures,
            snapshots=self._snapshots,
        )
        if self._manifest_finalizer is not None:
            recovery_snapshots = H5FinalizingRecoverySnapshotReader(
                delegate=recovery_snapshots,
                manifest_finalizer=self._manifest_finalizer,
            )
        executor = EvidenceRecoveryRestorationExecutor(
            replay=replay, snapshots=recovery_snapshots,
            agent_run_id=agent_run_id, finalization_epoch=finalization_epoch,
        )
        new_session_runtime = (
            self._recovery_runtime
            if callable(getattr(self._recovery_runtime, "prepare_new_session", None)) else None
        )
        if new_session_runtime is not None:
            from services.api.app.application.h5_new_session_restoration import (
                H5NewSessionRestorationExecutor,
            )
            executor = H5NewSessionRestorationExecutor(
                delegate=executor, runtime=new_session_runtime, snapshots=recovery_snapshots,
                agent_run_id=agent_run_id, finalization_epoch=finalization_epoch,
            )
        coordinator = RestorationCoordinator(
            repository=self._restoration_attempts,
            executor=executor,
            equivalence_gate=H5StateEquivalenceGate(),
        )
        return H5TargetedRecaptureRuntime(
            captures=self._captures,
            snapshots=self._snapshots,
            manifest_finalizer=self._manifest_finalizer,
            recovery_runtime=self._recovery_runtime,
            planning=H5TargetedRecapturePlanService(
                contexts=self._contexts,
                page_graph=PageGraphService(repository=self._page_graph),
                new_session_runtime=new_session_runtime,
                closed_session_replay=self._closed_session_replay,
            ),
            restoration_attempts=self._restoration_attempts,
            restoration=TargetedRecaptureRestorationService(
                coordinator=coordinator,
                probe_publisher=RestoredProbePublicationService(
                    orchestrator=self._probe_orchestrator,
                ),
            ),
            agent_run_id=agent_run_id,
            finalization_epoch=finalization_epoch,
        )


class H5TargetedRecaptureRuntimeBuilder(Protocol):
    def build(
        self,
        *,
        agent_run_id: str,
        finalization_epoch: int,
    ) -> H5TargetedRecaptureRuntime: ...


class H5StateEquivalenceGate:
    """把 H5 取证目标与恢复状态不变量分开后交给公共等价门禁。"""

    def __init__(self) -> None:
        self._delegate = StateEquivalenceGate()

    def assess(
        self,
        *,
        request: EvidenceRequest,
        plan: RestorationPlan,
        restoration_attempt_id: str,
        original: StateInvariantSnapshot,
        restored: StateInvariantSnapshot,
    ) -> Any:
        target = request.target_invariants
        structure = target.get("structure")
        layer_target = structure.get("layer_target") if isinstance(structure, Mapping) else None
        semantic = target.get("semantic")
        observed_state = semantic.get("page_state") if isinstance(semantic, Mapping) else None
        requested_roles = semantic.get("capture_roles") if isinstance(semantic, Mapping) else None
        type_resolution = _h5_derived_type_resolution(request, original, restored)
        original_type = original.business.get("page_type")
        restored_type = restored.business.get("page_type")
        if type_resolution:
            restored = _replace_invariant(restored, "business", "page_type", original_type)
        # 关闭遮挡后的同一业务页是 base_page 补采目标，不是应被拒绝的跨层漂移。
        # 仅适配已确认逻辑身份的 popup -> stable；显式状态集合与权限锁定不适配。
        cleared_base_page = (
            layer_target == "base_page"
            and (observed_state is None or observed_state == "popup")
            and not (isinstance(requested_roles, (list, tuple)) and "before" in requested_roles)
            and request.required_equivalence is EvidenceRequiredEquivalence.LOGICAL_EQUIVALENT
            and original.surface_kind is SurfaceKind.WEB
            and restored.surface_kind is SurfaceKind.WEB
            and original.session_id.startswith("h5:")
            and restored.session_id.startswith("h5:")
            and original.semantic.get("page_state") == "popup"
            and restored.semantic.get("page_state") == "stable"
            and original.structure.get("layer_target") == "overlay"
            and restored.structure.get("layer_target") == "base_page"
            and original.structure.get("identity_status") == "confirmed"
            and restored.structure.get("identity_status") == "confirmed"
            and original.structure.get("is_external") is False
            and restored.structure.get("is_external") is False
            and bool(original.business.get("logical_page_key"))
            and original.business == restored.business
        )
        if cleared_base_page:
            original = _replace_invariant(original, "structure", "layer_target", layer_target)
            if observed_state == "popup":
                # 只适配原帧的观察状态；规则显式要求的状态集合不能被清障后态替代。
                restored = _replace_invariant(restored, "semantic", "page_state", "popup")
        if layer_target is not None:
            original_layer = original.structure.get("layer_target")
            restored_layer = restored.structure.get("layer_target")
            if layer_target == "any" or original_layer == restored_layer:
                # layer_target 声明 Probe 最终要采哪一层，不要求恢复前已处于该层；
                # 非 any 时恢复 Capture 仍必须和原 Capture 处于同一实际层级。
                original = _replace_invariant(
                    original, "structure", "layer_target", layer_target
                )
                restored = _replace_invariant(
                    restored, "structure", "layer_target", layer_target
                )
        region = semantic.get("region") if isinstance(semantic, Mapping) else None
        if region is not None:
            # region 是后续 Probe 的取证范围，不是页面状态快照中的已观测字段。
            original = _replace_invariant(original, "semantic", "region", region)
            restored = _replace_invariant(restored, "semantic", "region", region)
        capture_roles = semantic.get("capture_roles") if isinstance(semantic, Mapping) else None
        if capture_roles is not None:
            # 缺失的采集角色由后续 Probe 补齐，不属于恢复前的页面状态。
            original = _replace_invariant(original, "semantic", "capture_roles", capture_roles)
            restored = _replace_invariant(restored, "semantic", "capture_roles", capture_roles)
        accepted_states = semantic.get("page_state") if isinstance(semantic, Mapping) else None
        if isinstance(accepted_states, (list, tuple)) and accepted_states:
            accepted = tuple(str(item) for item in accepted_states)
            if original.semantic.get("page_state") in accepted:
                original = _replace_invariant(
                    original,
                    "semantic",
                    "page_state",
                    list(accepted_states),
                )
            if restored.semantic.get("page_state") in accepted:
                restored = _replace_invariant(
                    restored,
                    "semantic",
                    "page_state",
                    list(accepted_states),
                )
        assessment = self._delegate.assess(
            request=request,
            plan=plan,
            restoration_attempt_id=restoration_attempt_id,
            original=original,
            restored=restored,
        )
        comparisons = list(assessment.comparisons)
        if (request.required_equivalence is EvidenceRequiredEquivalence.SAME_SURFACE_CONTINUATION
            or request.replay_policy is EvidenceReplayPolicy.SAME_SURFACE_ONLY):
            # 规划预检不能替代实际恢复后验；同 Session 重载或换状态也必须拒绝。
            keys = ("document_identity",)
            if (request.required_equivalence
                is EvidenceRequiredEquivalence.SAME_SURFACE_CONTINUATION):
                keys += ("logical_state_fingerprint",)
            for key in keys:
                left, right = original.structure.get(key), restored.structure.get(key)
                valid = isinstance(left, str) and bool(left.strip())
                right_valid = isinstance(right, str) and bool(right.strip())
                if key == "logical_state_fingerprint":
                    valid = valid and bool(re.fullmatch(r"[0-9a-f]{64}", left))
                    right_valid = right_valid and bool(re.fullmatch(r"[0-9a-f]{64}", right))
                expected = fingerprint_invariant(left, scope=request.request_id)
                comparisons.append(InvariantComparison(
                    dimension=StateInvariantDimension.STRUCTURE, key=f"original_surface:{key}",
                    expected_fingerprint=expected,
                    original_fingerprint=expected if valid else None,
                    restored_fingerprint=(fingerprint_invariant(right, scope=request.request_id)
                                          if right_valid else None),
                    matched=valid and right_valid and left == right,
                ))
        if not type_resolution and len(comparisons) == len(assessment.comparisons):
            return assessment
        # 独立证明及原标签进入持久化评估身份，不改原 Capture、请求或规则页型。
        proof_hash = fingerprint_invariant(
            [original.capture_id, restored.capture_id, original_type, restored_type,
             type_resolution, dict(original.structure), dict(restored.structure)],
            scope=request.request_id,
        )
        if type_resolution:
            comparisons.append(InvariantComparison(
                dimension=StateInvariantDimension.STRUCTURE,
                key=f"derived_page_type:{original_type}->{restored_type}:{type_resolution}",
                expected_fingerprint=proof_hash, original_fingerprint=proof_hash,
                restored_fingerprint=proof_hash, matched=True,
            ))
        comparisons = tuple(comparisons)
        version = "alioth-h5-state-equivalence.v3"
        return replace(assessment, comparisons=comparisons, evaluator_version=version,
            passed=assessment.passed and all(item.matched for item in comparisons),
            assessment_id=build_state_equivalence_assessment_id(
                restoration_attempt_id=restoration_attempt_id, request_id=request.request_id,
                task_id=request.task_id, original_capture_id=original.capture_id,
                restored_capture_id=restored.capture_id,
                required_equivalence=request.required_equivalence,
                source_grade=plan.source_grade, minimum_source_grade=request.minimum_source_grade,
                comparisons=comparisons, evaluator_version=version,
            ))


def _h5_derived_type_resolution(
    request: EvidenceRequest,
    original: StateInvariantSnapshot,
    restored: StateInvariantSnapshot,
) -> str | None:
    """只纠正低风险正文派生标签；支付、真实视频、精确状态合同不适配。"""
    types = {original.business.get("page_type"), restored.business.get("page_type")}
    if len(types) != 2 or not types <= {"home", "video_list", "content_list", "content_detail"}:
        return None
    if request.required_equivalence is not EvidenceRequiredEquivalence.LOGICAL_EQUIVALENT:
        return None
    if not all(snapshot.surface_kind is SurfaceKind.WEB
               and snapshot.session_id.startswith("h5:")
               and snapshot.structure.get("identity_status") == "confirmed"
               and snapshot.structure.get("is_external") is False
               for snapshot in (original, restored)):
        return None
    original_business = {
        key: value for key, value in original.business.items() if key != "page_type"
    }
    restored_business = {
        key: value for key, value in restored.business.items() if key != "page_type"
    }
    if (not original_business.get("logical_page_key") or not original_business.get("url_pattern")
            or original_business != restored_business
            or original.environment != restored.environment
            or restored.semantic.get("page_state") != "stable"
            or restored.structure.get("layer_target") != "base_page"):
        return None
    if original.semantic.get("page_state") == "stable":
        if original.structure.get("layer_target") == "base_page" and all(
            original.structure.get(key)
            and original.structure.get(key) == restored.structure.get(key)
            for key in ("document_identity", "logical_state_fingerprint")
        ):
            return "same_document_and_logical_state"
    identity = str(original_business["logical_page_key"]).removeprefix("logical:")
    if (original.semantic.get("page_state") == "popup"
            and original.structure.get("layer_target") == "overlay"
            and restored.structure.get("logical_transition_proof") == "trusted"
            and restored.structure.get("transition_reason") == "trusted_obstruction_clearance"
            and restored.structure.get("transition_before_identity") == identity
            and restored.structure.get("transition_after_identity") == identity
            and restored.structure.get("transition_before_document")
            == restored.structure.get("transition_after_document")
            == restored.structure.get("document_identity")
            and restored.structure.get("document_identity")
            and restored.structure.get("obstruction_count") == "0"
            and restored.structure.get("loading") is False
            and restored.structure.get("evidence_quality") == "high"):
        return "trusted_obstruction_clearance"
    return None


class H5TargetedRecaptureDisposition(StrEnum):
    EMPTY = "empty"
    PROBE_PUBLISHED = "probe_published"
    RETRY_SCHEDULED = "retry_scheduled"
    UNFULFILLABLE = "unfulfillable"
    EXPIRED = "expired"
    LEASE_REJECTED = "lease_rejected"


@dataclass(frozen=True, slots=True, kw_only=True)
class H5TargetedRecaptureOutcome:
    disposition: H5TargetedRecaptureDisposition
    request_id: str | None = None
    reason_code: str | None = None
    request: EvidenceRequest | None = None


class H5TargetedRecaptureConsumer:
    """按公共租约领取一条当前 H5 Run 的请求并完成安全派发。"""

    def __init__(
        self,
        *,
        requests: EvidenceRequestRepository,
        runtime_factory: H5TargetedRecaptureRuntimeBuilder,
        policy: SupplementalEvidencePolicy,
        lease_owner: str,
        lease_seconds: int = DEFAULT_H5_TARGETED_RECAPTURE_LEASE_SECONDS,
        retry_delay_seconds: int = 5,
        clock: Callable[[], datetime] | None = None,
        gap_history: Any | None = None,
        budget_reader: Any | None = None,
        session_command_gate: Any | None = None,
    ) -> None:
        self._requests = requests
        self._runtime_factory = runtime_factory
        self._policy = policy
        self._lease_owner = lease_owner
        self._lease_seconds = lease_seconds
        self._retry_delay = timedelta(seconds=retry_delay_seconds)
        self._clock = clock or (lambda: datetime.now(UTC))
        self._gap_history = gap_history
        self._budget_reader = budget_reader
        self._session_command_gate = session_command_gate
        self._stage_scope = ContextVar("h5_recapture_stage", default=False)

    async def consume_next(
        self,
        *,
        task_id: str,
        agent_run_id: str,
        finalization_epoch: int,
        budget: ExplorationBudget,
        deadline: datetime,
        include_snapshot: bool = False,
        deferred_snapshot_request_ids: tuple[str, ...] = (),
        h5_request_id: str | None = None,
    ) -> H5TargetedRecaptureOutcome:
        if not isinstance(budget, ExplorationBudget):
            raise TypeError("budget 必须为 ExplorationBudget")
        require_aware_datetime(deadline, "deadline")
        self._policy.require_enabled(SupplementalEvidenceOperation.EXECUTE_PROBE)
        effective_deadline = min(
            deadline, self._now() + timedelta(milliseconds=budget.remaining_duration_ms),
        )
        if not callable(getattr(self._requests, "renew_h5_lease", None)):
            # 旧端口不能续租时只使用领取前可证明的窗口，不能在过期后继续外部操作。
            effective_deadline = min(
                effective_deadline,
                self._now() + timedelta(seconds=max(0, self._lease_seconds - 1)),
            )
        session_dependencies = (EvidenceSessionDependency.ACTIVE_SESSION,)
        if include_snapshot:
            session_dependencies = (
                EvidenceSessionDependency.ACTIVE_SESSION,
                EvidenceSessionDependency.SNAPSHOT,
            )
        claim = await self._requests.claim_next(
            task_id=task_id,
            agent_run_id=agent_run_id,
            finalization_epoch=finalization_epoch,
            lease_owner=self._lease_owner,
            lease_seconds=self._lease_seconds,
            session_dependencies=session_dependencies,
            **({"deferred_h5_snapshot_request_ids": deferred_snapshot_request_ids}
               if deferred_snapshot_request_ids else {}),
            **({"h5_request_id": h5_request_id} if h5_request_id else {}),
        )
        if claim.disposition is EvidenceRequestClaimDisposition.EMPTY:
            return H5TargetedRecaptureOutcome(
                disposition=H5TargetedRecaptureDisposition.EMPTY
            )
        lease = claim.lease
        if lease is None:
            raise RuntimeError("H5 补采请求已领取但缺少租约")
        now = self._now()
        if now >= lease.request.expires_at:
            return await self._settle(
                lease=lease,
                status=EvidenceRequestStatus.EXPIRED,
                disposition=H5TargetedRecaptureDisposition.EXPIRED,
                reason_code="EVIDENCE_REQUEST_EXPIRED",
            )
        effective_deadline = min(effective_deadline, lease.request.expires_at)
        if now >= effective_deadline:
            return await self._settle(
                lease=lease,
                status=EvidenceRequestStatus.UNFULFILLABLE,
                disposition=H5TargetedRecaptureDisposition.UNFULFILLABLE,
                reason_code="H5_RECAPTURE_DEADLINE_EXHAUSTED",
            )
        if budget.resolved_remaining_business_actions == 0:
            return await self._settle(
                lease=lease, status=EvidenceRequestStatus.UNFULFILLABLE,
                disposition=H5TargetedRecaptureDisposition.UNFULFILLABLE,
                reason_code="H5_RECAPTURE_ACTION_BUDGET_EXHAUSTED",
            )
        try:
            return await self._run_stage(lease, agent_run_id, effective_deadline, lambda:
                self._consume_claimed(
                    lease=lease, task_id=task_id, agent_run_id=agent_run_id,
                    finalization_epoch=finalization_epoch, budget=budget,
                    effective_deadline=effective_deadline,
                ))
        except (H5RecaptureDeadlineError, H5SessionJourneyDeadline):
            return await self._settle_execution_deadline(lease=lease)
        except H5RecaptureLeaseLostError:
            return self._lease_rejected(lease=lease)
        except H5SessionJourneyLost as exc:
            return await self._settle(
                lease=lease, status=EvidenceRequestStatus.UNFULFILLABLE,
                disposition=H5TargetedRecaptureDisposition.UNFULFILLABLE,
                reason_code=exc.error_code,
            )
        except asyncio.CancelledError:
            try:
                await self._release(lease=lease, reason_code="H5_RECAPTURE_INTERRUPTED")
            except Exception:  # noqa: BLE001 - 取消不能被围栏清理覆盖
                logger.exception("H5 旅程中断释放失败 request_id=%s", lease.request.request_id)
            raise
        except RestoredProbePublicationError as exc:
            if not exc.retryable:
                return await self._settle(
                    lease=lease,
                    status=EvidenceRequestStatus.UNFULFILLABLE,
                    disposition=H5TargetedRecaptureDisposition.UNFULFILLABLE,
                    reason_code=exc.reason_code,
                )
            return await self._release(lease=lease, reason_code=exc.reason_code)
        except Exception as exc:  # noqa: BLE001 - 请求必须按租约留下可重试事实
            from services.api.app.application.h5_llm_budget import h5_llm_budget_error_code

            budget_error = h5_llm_budget_error_code(exc)
            reason_code = str(
                budget_error or getattr(exc, "error_code", "")
                or getattr(exc, "reason_code", "")
                or f"H5_RECAPTURE_RUNTIME_{type(exc).__name__.upper()}"
            )
            logger.warning(
                "task_id=%s request_id=%s H5 补采执行失败 attempt=%s reason=%s",
                lease.request.task_id,
                lease.request.request_id,
                lease.attempt_count,
                reason_code,
                exc_info=True,
            )
            if budget_error or getattr(exc, "retryable", True) is False:
                return await self._settle(
                    lease=lease,
                    status=EvidenceRequestStatus.UNFULFILLABLE,
                    disposition=H5TargetedRecaptureDisposition.UNFULFILLABLE,
                    reason_code=reason_code,
                )
            return await self._release(
                lease=lease,
                reason_code=reason_code,
            )

    async def _consume_claimed(
        self, *, lease, task_id, agent_run_id, finalization_epoch, budget, effective_deadline,
    ):
        if self._gap_history is not None:
            prior_id = await self._gap_history.has_unchanged_gap(
                request=lease.request, agent_run_id=agent_run_id,
                finalization_epoch=finalization_epoch,
            )
            if prior_id:
                return await self._settle(
                    lease=lease, status=EvidenceRequestStatus.UNFULFILLABLE,
                    disposition=H5TargetedRecaptureDisposition.UNFULFILLABLE,
                    reason_code=f"H5_RECAPTURE_UNCHANGED_GAP:{prior_id}",
                )
        runtime = self._runtime_factory.build(
            agent_run_id=agent_run_id, finalization_epoch=finalization_epoch,
        )
        session_id = None
        if self._session_command_gate is not None:
            # 先校验并固化原始制品，不能把尚未结算的 Manifest 当成永久补证失败。
            manifest_finalizer = getattr(runtime, "manifest_finalizer", None)
            if manifest_finalizer is not None:
                await manifest_finalizer.finalize_capture(
                    task_id=task_id, capture_id=lease.request.target_capture_id,
                )
            persisted = await runtime.captures.get_verified_capture(
                capture_id=lease.request.target_capture_id,
            )
            capture = getattr(persisted, "capture", None)
            if (not isinstance(capture, CaptureRef) or capture.task_id != task_id
                    or capture.capture_id != lease.request.target_capture_id
                    or capture.surface_kind is not SurfaceKind.WEB
                    or not capture.session_id.startswith("h5:")):
                return await self._settle(
                    lease=lease, status=EvidenceRequestStatus.UNFULFILLABLE,
                    disposition=H5TargetedRecaptureDisposition.UNFULFILLABLE,
                    reason_code="H5_RECAPTURE_TARGET_CAPTURE_SCOPE_MISMATCH",
                )
            session_id = capture.session_id
        token = self._stage_scope.set(True)
        try:
            async with h5_session_journey(
                gate=self._session_command_gate, task_id=task_id, session_id=session_id,
                deadline=effective_deadline, clock=self._now,
            ):
                return await self._consume_in_journey(
                    runtime=runtime, lease=lease, task_id=task_id, agent_run_id=agent_run_id,
                    finalization_epoch=finalization_epoch, budget=budget,
                    effective_deadline=effective_deadline,
                )
        finally:
            self._stage_scope.reset(token)

    async def _consume_in_journey(
        self, *, runtime, lease, task_id, agent_run_id, finalization_epoch, budget,
        effective_deadline,
    ):
        resumed = await self._run_stage(lease, agent_run_id, effective_deadline, lambda:
            runtime.resume_equivalent_publication(
            request=lease.request,
            budget=self._budget_at(budget=budget, deadline=effective_deadline),
            request_lease=lease.fence,
        ))
        if resumed is not None:
            return H5TargetedRecaptureOutcome(
                disposition=H5TargetedRecaptureDisposition.PROBE_PUBLISHED,
                request_id=lease.request.request_id,
                request=lease.request,
            )
        if self._budget_reader is not None and await self._run_stage(
            lease, agent_run_id, effective_deadline, lambda: self._budget_reader.is_exhausted(
            task_id=task_id, agent_run_id=agent_run_id,
            finalization_epoch=finalization_epoch,
        )):
            return await self._settle(
                lease=lease, status=EvidenceRequestStatus.UNFULFILLABLE,
                disposition=H5TargetedRecaptureDisposition.UNFULFILLABLE,
                reason_code="h5_llm_call_budget_exhausted",
            )
        if self._now() >= effective_deadline:
            return await self._settle_execution_deadline(lease=lease)
        resolution = await self._run_stage(lease, agent_run_id, effective_deadline, lambda:
            runtime.resolve_plan(
            request=lease.request, deadline=effective_deadline,
        ))
        if self._now() >= effective_deadline:
            return await self._settle_execution_deadline(lease=lease)
        if resolution.plan is None:
            if resolution.retryable:
                return await self._release(
                    lease=lease,
                    reason_code=resolution.reason_code or "H5_RECAPTURE_PLAN_UNAVAILABLE",
                )
            return await self._settle(
                lease=lease,
                status=(
                    EvidenceRequestStatus.EXPIRED
                    if resolution.reason_code == "EVIDENCE_REQUEST_EXPIRED"
                    else EvidenceRequestStatus.UNFULFILLABLE
                ),
                disposition=(
                    H5TargetedRecaptureDisposition.EXPIRED
                    if resolution.reason_code == "EVIDENCE_REQUEST_EXPIRED"
                    else H5TargetedRecaptureDisposition.UNFULFILLABLE
                ),
                reason_code=resolution.reason_code or "H5_RECAPTURE_PLAN_UNAVAILABLE",
            )
        if self._now() >= resolution.plan.deadline:
            return await self._settle_execution_deadline(lease=lease)
        # 新会话首跳也是真实动作；恢复后仍须为 Probe 留一个业务动作。
        restoration_actions = len(resolution.plan.normalization_step_ids) + int(
            resolution.plan.strategy is RestorationStrategy.NEW_SESSION_REPLAY
        )
        if restoration_actions + 1 > budget.resolved_remaining_business_actions:
            return await self._settle(
                lease=lease, status=EvidenceRequestStatus.UNFULFILLABLE,
                disposition=H5TargetedRecaptureDisposition.UNFULFILLABLE,
                reason_code="H5_RECAPTURE_ACTION_BUDGET_EXHAUSTED",
            )
        probe_budget = self._budget_at(budget=budget, deadline=effective_deadline)
        for _ in range(restoration_actions):
            probe_budget = consume_exploration_action_budget(
                probe_budget, counts_toward_business=True, elapsed_duration_ms=0,
            )
        result = await self._run_stage(
            lease, agent_run_id, min(effective_deadline, resolution.plan.deadline), lambda:
            runtime.restore_and_publish(
            request=lease.request,
            plan=resolution.plan,
            budget=probe_budget,
            attempt_no=lease.attempt_count,
            request_lease=lease.fence,
        ))
        attempt = result.restoration_attempt
        if attempt.status is RestorationAttemptStatus.EQUIVALENT and result.probe_work is not None:
            return H5TargetedRecaptureOutcome(
                disposition=H5TargetedRecaptureDisposition.PROBE_PUBLISHED,
                request_id=lease.request.request_id,
                request=lease.request,
            )
        if attempt.status is RestorationAttemptStatus.UNFULFILLABLE:
            return await self._settle(
                lease=lease,
                status=EvidenceRequestStatus.UNFULFILLABLE,
                disposition=H5TargetedRecaptureDisposition.UNFULFILLABLE,
                reason_code=attempt.error_code or "H5_RESTORATION_UNFULFILLABLE",
            )
        return await self._release(
            lease=lease,
            reason_code=attempt.error_code or f"H5_RESTORATION_{attempt.status.value.upper()}",
        )

    async def _run_stage(self, lease, agent_run_id, deadline, operation):
        if self._stage_scope.get():
            # 整段外层守卫持续续租与截止取消，内部阶段不重复启动续租器。
            return await operation()
        return await run_h5_recapture_stage(
            operation=operation, requests=self._requests, lease=lease, agent_run_id=agent_run_id,
            lease_seconds=self._lease_seconds, deadline=deadline, now=self._now,
        )

    def _budget_at(self, *, budget: ExplorationBudget, deadline: datetime) -> ExplorationBudget:
        return replace(budget, remaining_duration_ms=min(
            budget.remaining_duration_ms,
            max(0, int((deadline - self._now()).total_seconds() * 1_000)),
        ))

    async def _settle_execution_deadline(
        self, *, lease: EvidenceRequestProcessingLease,
    ) -> H5TargetedRecaptureOutcome:
        expired = self._now() >= lease.request.expires_at
        return await self._settle(
            lease=lease,
            status=(EvidenceRequestStatus.EXPIRED if expired
                    else EvidenceRequestStatus.UNFULFILLABLE),
            disposition=(H5TargetedRecaptureDisposition.EXPIRED if expired
                         else H5TargetedRecaptureDisposition.UNFULFILLABLE),
            reason_code=("EVIDENCE_REQUEST_EXPIRED" if expired
                         else "H5_RECAPTURE_DEADLINE_EXHAUSTED"),
        )

    async def can_defer_snapshot_requests(
        self, *, requests: tuple[EvidenceRequest, ...], agent_run_id: str,
        finalization_epoch: int,
    ) -> bool:
        """只有已验证可信现场路线或入口重放路线的快照请求可延后。"""
        self._policy.require_enabled(SupplementalEvidenceOperation.EXECUTE_PROBE)
        runtime = self._runtime_factory.build(
            agent_run_id=agent_run_id, finalization_epoch=finalization_epoch,
        )
        for request in requests:
            if request.session_dependency is not EvidenceSessionDependency.SNAPSHOT:
                return False
            if self._budget_reader is not None and await self._budget_reader.is_exhausted(
                task_id=request.task_id, agent_run_id=agent_run_id,
                finalization_epoch=finalization_epoch,
            ):
                return False
            resolution = await runtime.resolve_plan(request=request)
            if (
                (resolution.plan is not None
                 and resolution.plan.strategy is RestorationStrategy.ORIGINAL_SURFACE_PIN)
                or (resolution.plan is None
                    and getattr(resolution, "reason_code", None)
                    == "TRUSTED_RECOVERY_ROUTE_UNAVAILABLE")
            ):
                # 当前标签无返程边不等于入口无法重放；仍须通过既有只读预检。
                planning = getattr(runtime, "planning", None)
                preflight = getattr(planning, "can_defer_snapshot", None)
                if callable(preflight) and await preflight(
                    request=request, agent_run_id=agent_run_id,
                    finalization_epoch=finalization_epoch,
                ):
                    continue
            if (
                resolution.plan is None
                or resolution.plan.strategy is not RestorationStrategy.SAME_CONTEXT_NEW_SURFACE
                or not resolution.plan.normalization_step_ids
            ):
                return False
        return bool(requests)

    async def _release(
        self,
        *,
        lease: EvidenceRequestProcessingLease,
        reason_code: str,
    ) -> H5TargetedRecaptureOutcome:
        command = ReleaseEvidenceRequestLeaseCommand(
            request_id=lease.request.request_id,
            task_id=lease.request.task_id,
            finalization_epoch=lease.finalization_epoch,
            lease_owner=lease.lease_owner,
            lease_token=lease.lease_token,
            row_version=lease.row_version,
            next_attempt_at=self._now() + self._retry_delay,
            reason_code=reason_code,
        )
        try:
            released = await self._requests.release_lease(command=command)
        except EvidenceRequestPersistenceError:
            return self._lease_rejected(lease=lease)
        disposition = {
            EvidenceRequestLeaseReleaseDisposition.RETRY_SCHEDULED: (
                H5TargetedRecaptureDisposition.RETRY_SCHEDULED
            ),
            EvidenceRequestLeaseReleaseDisposition.REQUEST_EXPIRED: (
                H5TargetedRecaptureDisposition.EXPIRED
            ),
            EvidenceRequestLeaseReleaseDisposition.RETRY_EXHAUSTED: (
                H5TargetedRecaptureDisposition.UNFULFILLABLE
            ),
        }[released.disposition]
        return H5TargetedRecaptureOutcome(
            disposition=disposition,
            request_id=lease.request.request_id,
            reason_code=reason_code,
        )

    async def _settle(
        self,
        *,
        lease: EvidenceRequestProcessingLease,
        status: EvidenceRequestStatus,
        disposition: H5TargetedRecaptureDisposition,
        reason_code: str,
    ) -> H5TargetedRecaptureOutcome:
        command = SettleEvidenceRequestLeaseCommand(
            request_id=lease.request.request_id,
            task_id=lease.request.task_id,
            finalization_epoch=lease.finalization_epoch,
            lease_owner=lease.lease_owner,
            lease_token=lease.lease_token,
            row_version=lease.row_version,
            target_status=status,
            resolution_reason=reason_code,
        )
        try:
            await self._requests.settle_lease(command=command)
        except EvidenceRequestPersistenceError:
            return self._lease_rejected(lease=lease)
        return H5TargetedRecaptureOutcome(
            disposition=disposition,
            request_id=lease.request.request_id,
            reason_code=reason_code,
        )

    def _lease_rejected(
        self, *, lease: EvidenceRequestProcessingLease,
    ) -> H5TargetedRecaptureOutcome:
        # 旧许可被数据库拒绝后停止本次处理，不重复写状态，也不伪造释放或进展。
        logger.warning(
            "H5 补采租约收尾被拒绝 task_id=%s request_id=%s attempt=%s row_version=%s",
            lease.request.task_id, lease.request.request_id,
            lease.attempt_count, lease.row_version,
            exc_info=True,
        )
        return H5TargetedRecaptureOutcome(
            disposition=H5TargetedRecaptureDisposition.LEASE_REJECTED,
            request_id=lease.request.request_id,
            reason_code="H5_RECAPTURE_LEASE_REJECTED",
        )

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("clock 必须返回带时区时间")
        return value


@dataclass(frozen=True, slots=True, kw_only=True)
class H5SnapshotCheckpointRequest:
    """检查点完整请求事实；持有现场许可的请求不能被延后检查忽略。"""

    request: EvidenceRequest
    finalization_epoch: int
    blocks_current_surface: bool


class H5TargetedRecaptureDrainService:
    """把当前 H5 Task 的 epoch 查询与单次租约消费封装为排空端口。"""

    def __init__(
        self,
        *,
        consumer: H5TargetedRecaptureConsumer,
        scope_reader: Any,
        budget: ExplorationBudget,
        inspection_wakeup: Any | None = None,
        observation_sharing: Any | None = None,
    ) -> None:
        self._consumer = consumer
        self._scope_reader = scope_reader
        self._budget = budget
        self._inspection_wakeup = inspection_wakeup
        self._observation_sharing = observation_sharing

    async def can_defer_snapshot_requests(
        self, *, task_id: str, agent_run_id: str, expected_request_count: int,
    ) -> bool:
        """延后不是终结：请求保留，关闭前仍由原活会话补采链处理。"""
        if not 0 < expected_request_count <= 50:
            return False
        inventory_reader = getattr(self._scope_reader, "list_snapshot_checkpoint_requests", None)
        if not callable(inventory_reader):
            return False
        inventory = await inventory_reader(task_id=task_id, agent_run_id=agent_run_id)
        if len(inventory) != expected_request_count or not inventory:
            return False
        epochs = {item.finalization_epoch for item in inventory}
        if (
            len(epochs) != 1 or any(item.blocks_current_surface for item in inventory)
            or len({item.request.request_id for item in inventory}) != len(inventory)
            or any(item.request.task_id != task_id for item in inventory)
        ):
            return False
        epoch = next(iter(epochs))
        # 逐请求验证恢复合同，不以一条可恢复请求代替其余请求的证明。
        for item in inventory:
            if not await self._consumer.can_defer_snapshot_requests(
                requests=(item.request,), agent_run_id=agent_run_id, finalization_epoch=epoch,
            ):
                return False
        return True

    async def drain_one(
        self,
        *,
        task_id: str,
        agent_run_id: str,
        deadline: datetime,
        include_snapshot: bool = False,
        defer_recoverable_snapshots: bool = False,
    ) -> bool:
        epoch = await self._scope_reader.read_h5_epoch(
            task_id=task_id,
            agent_run_id=agent_run_id,
        )
        if epoch is None:
            return False
        deferred: list[str] = []
        inventory_reader = getattr(self._scope_reader, "list_deferred_snapshot_requests", None)
        if (include_snapshot and callable(inventory_reader)
            and (defer_recoverable_snapshots or self._observation_sharing is not None)):
            requests = await inventory_reader(
                task_id=task_id, agent_run_id=agent_run_id, include_ready=True,
            )
            for request in requests:
                interaction = getattr(request, "required_interaction", None)
                if not defer_recoverable_snapshots and (
                    interaction is None
                    or interaction.parameters.get("goal_capability") != "payment_plan_selection"
                ):
                    continue
                # 主探索只延后有可信回放证明的选档，避免封闭主 SPA；未证明的请求
                # 仍沿原合同处理。独立恢复阶段不再延后，不刷新预算或提前记完成。
                if await self._consumer.can_defer_snapshot_requests(
                    requests=(request,), agent_run_id=agent_run_id, finalization_epoch=epoch,
                ):
                    deferred.append(request.request_id)
        from contextlib import nullcontext

        sharing = self._observation_sharing
        batch_started = monotonic()
        batch_deadline = (min(deadline, datetime.now(UTC) + timedelta(
            milliseconds=self._budget.remaining_duration_ms,
        )) if sharing is not None else deadline)
        with sharing.share_observations() if sharing is not None else nullcontext():
            outcome = await self._consumer.consume_next(
                task_id=task_id, agent_run_id=agent_run_id, finalization_epoch=epoch,
                budget=self._budget, deadline=batch_deadline, include_snapshot=include_snapshot,
                **({"deferred_snapshot_request_ids": tuple(deferred)} if deferred else {}),
            )
            list_compatible = getattr(
                self._scope_reader, "list_compatible_observation_requests", None,
            )
            if (sharing is not None and callable(list_compatible) and outcome.request is not None
                and outcome.disposition is H5TargetedRecaptureDisposition.PROBE_PUBLISHED
                and sharing.has_shared_observation(
                    task_id=task_id, agent_run_id=agent_run_id,
                    finalization_epoch=epoch, request=outcome.request,
                )):
                candidates = await list_compatible(
                    request=outcome.request, agent_run_id=agent_run_id, finalization_epoch=epoch,
                )
                batch_budget = consume_exploration_action_budget(
                    self._budget, counts_toward_business=True,
                    elapsed_duration_ms=int((monotonic() - batch_started) * 1_000),
                )
                for candidate in candidates:
                    if (batch_budget.resolved_remaining_business_actions == 0
                        or batch_budget.remaining_duration_ms == 0):
                        break
                    if candidate.request_id in deferred:
                        continue
                    if not await sharing.shared_observation_is_current(
                        task_id=task_id, agent_run_id=agent_run_id,
                        finalization_epoch=epoch, request=outcome.request,
                    ):
                        break
                    sibling_started = monotonic()
                    sibling = await self._consumer.consume_next(
                        task_id=task_id, agent_run_id=agent_run_id, finalization_epoch=epoch,
                        budget=batch_budget, deadline=batch_deadline,
                        include_snapshot=include_snapshot,
                        h5_request_id=candidate.request_id,
                    )
                    if sibling.disposition is H5TargetedRecaptureDisposition.PROBE_PUBLISHED:
                        batch_budget = consume_exploration_action_budget(
                            batch_budget, counts_toward_business=True,
                            elapsed_duration_ms=int((monotonic() - sibling_started) * 1_000),
                        )
                    elif (self._inspection_wakeup is not None and sibling.disposition in {
                        H5TargetedRecaptureDisposition.UNFULFILLABLE,
                        H5TargetedRecaptureDisposition.EXPIRED,
                    }):
                        await self._inspection_wakeup.reopen_waiting_evidence(task_id=task_id)
                    if sibling.disposition is not H5TargetedRecaptureDisposition.PROBE_PUBLISHED:
                        break
        if (
            self._inspection_wakeup is not None
            and outcome.disposition
            in {
                H5TargetedRecaptureDisposition.UNFULFILLABLE,
                H5TargetedRecaptureDisposition.EXPIRED,
            }
        ):
            await self._inspection_wakeup.reopen_waiting_evidence(task_id=task_id)
        # 安排下一次尝试没有新增证据，不能刷新探索的无进展计时器。
        return outcome.disposition not in {
            H5TargetedRecaptureDisposition.EMPTY,
            H5TargetedRecaptureDisposition.RETRY_SCHEDULED,
            H5TargetedRecaptureDisposition.LEASE_REJECTED,
        }


def _replace_invariant(
    snapshot: StateInvariantSnapshot,
    dimension: str,
    key: str,
    value: object,
) -> StateInvariantSnapshot:
    values = dict(snapshot.values_for(StateInvariantDimension(dimension)))
    values[key] = value
    return replace(snapshot, **{dimension: values})
