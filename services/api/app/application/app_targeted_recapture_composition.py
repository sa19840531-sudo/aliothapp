"""App 定向补采的 Run/Epoch 绑定组合。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from services.api.app.application.app_evidence_recovery_runtime import (
    AppEvidenceRecoveryRuntime,
    AppRecoveryVisualFacts,
)
from services.api.app.application.app_targeted_recapture_planning import (
    AppTargetedRecaptureContextReader,
    AppTargetedRecapturePlanResolution,
    AppTargetedRecapturePlanService,
)
from services.api.app.application.evidence_recovery_replay import (
    EvidenceRecoveryReplayExecutor,
)
from services.api.app.application.evidence_recovery_restoration import (
    CaptureBackedRecoverySnapshotReader,
    EvidenceRecoveryRestorationExecutor,
)
from services.api.app.application.evidence_requests import EvidenceRequestLeaseFence
from services.api.app.application.page_graph import PageGraphService
from services.api.app.application.page_graph_contracts import PageGraphRepository
from services.api.app.application.restoration import (
    RestorationAttemptRepository,
    RestorationCoordinator,
)
from services.api.app.application.restored_probe_publication import (
    RestoredProbeOrchestrator,
    RestoredProbePublicationService,
)
from services.api.app.application.state_equivalence import StateEquivalenceGate
from services.api.app.application.targeted_recapture_restoration import (
    TargetedRecaptureRestorationOutcome,
    TargetedRecaptureRestorationService,
)
from services.api.app.domain.exploration import ExplorationBudget
from services.api.app.domain.inspection import EvidenceRequest, RestorationPlan
from services.api.app.domain.interaction import CaptureRef


class AppTargetedRecaptureCaptureReader(Protocol):
    """读取补采原 Capture 与恢复 Capture 的最小持久化端口。"""

    async def get_verified_capture(self, *, capture_id: str) -> Any | None: ...


class AppTargetedRecaptureSnapshotReader(Protocol):
    """从已校验 Capture 构造实际状态不变量。"""

    async def read(self, *, capture: CaptureRef) -> Any: ...


@dataclass(frozen=True, slots=True, kw_only=True)
class AppTargetedRecaptureRuntime:
    """固定一个 Agent Run/Epoch 的补采恢复执行入口。"""

    captures: AppTargetedRecaptureCaptureReader
    original_snapshots: AppTargetedRecaptureSnapshotReader
    planning: AppTargetedRecapturePlanService
    restoration_attempts: RestorationAttemptRepository
    restoration: TargetedRecaptureRestorationService
    agent_run_id: str
    finalization_epoch: int

    def __post_init__(self) -> None:
        _validate_run_scope(
            agent_run_id=self.agent_run_id,
            finalization_epoch=self.finalization_epoch,
        )

    async def restore_and_publish(
        self,
        *,
        request: EvidenceRequest,
        plan: RestorationPlan,
        budget: ExplorationBudget,
        attempt_no: int,
        request_lease: EvidenceRequestLeaseFence | None = None,
    ) -> TargetedRecaptureRestorationOutcome:
        if not isinstance(request, EvidenceRequest):
            raise TypeError("request 必须为 EvidenceRequest")
        if request_lease is not None and (
            request_lease.request_id != request.request_id
            or request_lease.task_id != request.task_id
            or request_lease.finalization_epoch != self.finalization_epoch
        ):
            raise ValueError("Evidence Request 租约与 App Runtime 执行域不一致")
        persisted = await self.captures.get_verified_capture(
            capture_id=request.target_capture_id
        )
        capture = getattr(persisted, "capture", None) if persisted is not None else None
        if (
            not isinstance(capture, CaptureRef)
            or capture.capture_id != request.target_capture_id
            or capture.task_id != request.task_id
        ):
            raise ValueError("Evidence Request 原 Capture 不存在或身份不一致")
        original_snapshot = await self.original_snapshots.read(capture=capture)
        if request_lease is None:
            return await self.restoration.restore_and_publish(
                request=request,
                plan=plan,
                original_snapshot=original_snapshot,
                budget=budget,
                attempt_no=attempt_no,
            )
        return await self.restoration.restore_and_publish(
            request=request,
            plan=plan,
            original_snapshot=original_snapshot,
            budget=budget,
            attempt_no=attempt_no,
            request_lease=request_lease,
        )

    async def resolve_plan(
        self,
        *,
        request: EvidenceRequest,
    ) -> AppTargetedRecapturePlanResolution:
        """按绑定的 Run/Epoch 读取实际上下文并解析恢复计划。"""

        return await self.planning.resolve(
            request=request,
            agent_run_id=self.agent_run_id,
            finalization_epoch=self.finalization_epoch,
        )

    async def resume_equivalent_publication(
        self,
        *,
        request: EvidenceRequest,
        budget: ExplorationBudget,
        request_lease: EvidenceRequestLeaseFence,
    ) -> TargetedRecaptureRestorationOutcome | None:
        """恢复完成但发布失败时，从持久化终态继续发布 Probe。"""

        if not isinstance(request, EvidenceRequest):
            raise TypeError("request 必须为 EvidenceRequest")
        if not isinstance(request_lease, EvidenceRequestLeaseFence):
            raise TypeError("request_lease 必须为 EvidenceRequestLeaseFence")
        if (
            request_lease.request_id != request.request_id
            or request_lease.task_id != request.task_id
            or request_lease.finalization_epoch != self.finalization_epoch
        ):
            raise ValueError("Evidence Request 租约与 App Runtime 执行域不一致")
        attempt = await self.restoration_attempts.find_equivalent_for_lease(
            fence=request_lease
        )
        if attempt is None:
            return None
        return await self.restoration.publish_equivalent(
            request=request,
            restoration_attempt=attempt,
            budget=budget,
            request_lease=request_lease,
        )


class AppTargetedRecaptureRuntimeFactory:
    """复用正式 App Runtime 端口构造定向补采链路。"""

    def __init__(
        self,
        *,
        page_graph: PageGraphRepository,
        planning_contexts: AppTargetedRecaptureContextReader,
        surface_sessions: Any,
        capture_coordinator: Any,
        captures: AppTargetedRecaptureCaptureReader,
        understanding: Any,
        observations: Any,
        original_snapshots: AppTargetedRecaptureSnapshotReader,
        restored_snapshots: AppTargetedRecaptureSnapshotReader,
        restoration_attempts: RestorationAttemptRepository,
        probe_orchestrator: RestoredProbeOrchestrator,
    ) -> None:
        self._page_graph = page_graph
        self._planning_contexts = planning_contexts
        self._surface_sessions = surface_sessions
        self._capture_coordinator = capture_coordinator
        self._captures = captures
        self._understanding = understanding
        self._observations = observations
        self._original_snapshots = original_snapshots
        self._restored_snapshots = restored_snapshots
        self._restoration_attempts = restoration_attempts
        self._probe_orchestrator = probe_orchestrator

    def build(
        self,
        *,
        agent_run_id: str,
        finalization_epoch: int,
    ) -> AppTargetedRecaptureRuntime:
        _validate_run_scope(
            agent_run_id=agent_run_id,
            finalization_epoch=finalization_epoch,
        )
        recovery_runtime = AppEvidenceRecoveryRuntime(
            surface_sessions=self._surface_sessions,
            capture_coordinator=self._capture_coordinator,
            facts=AppRecoveryVisualFacts(
                understanding=self._understanding,
                observations=self._observations,
                page_graph=self._page_graph,
            ),
        )
        replay = EvidenceRecoveryReplayExecutor(
            repository=self._page_graph,
            runtime=recovery_runtime,
        )
        restoration_executor = EvidenceRecoveryRestorationExecutor(
            replay=replay,
            snapshots=CaptureBackedRecoverySnapshotReader(
                captures=self._captures,
                snapshots=self._restored_snapshots,
            ),
            agent_run_id=agent_run_id,
            finalization_epoch=finalization_epoch,
        )
        coordinator = RestorationCoordinator(
            repository=self._restoration_attempts,
            executor=restoration_executor,
            equivalence_gate=StateEquivalenceGate(),
        )
        return AppTargetedRecaptureRuntime(
            captures=self._captures,
            original_snapshots=self._original_snapshots,
            planning=AppTargetedRecapturePlanService(
                contexts=self._planning_contexts,
                page_graph=PageGraphService(repository=self._page_graph),
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


def _validate_run_scope(*, agent_run_id: str, finalization_epoch: int) -> None:
    if (
        not isinstance(agent_run_id, str)
        or not agent_run_id.strip()
        or agent_run_id != agent_run_id.strip()
    ):
        raise ValueError("agent_run_id 必须为无首尾空白的非空字符串")
    if (
        isinstance(finalization_epoch, bool)
        or not isinstance(finalization_epoch, int)
        or finalization_epoch < 0
    ):
        raise ValueError("finalization_epoch 必须为非负整数")
