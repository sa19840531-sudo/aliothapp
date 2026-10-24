"""定向补采恢复、等价判断与 Probe 发布的顺序编排。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from services.api.app.application.evidence_requests import EvidenceRequestLeaseFence
from services.api.app.application.restoration import RestorationCoordinator
from services.api.app.domain.exploration import ExplorationBudget, ProbeWork
from services.api.app.domain.inspection import (
    EvidenceRequest,
    RestorationAttempt,
    RestorationAttemptStatus,
    RestorationPlan,
    StateInvariantSnapshot,
)


class EquivalentRestorationProbePublisher(Protocol):
    """只接收已完成恢复尝试的 Probe 发布端口。"""

    async def publish(
        self,
        *,
        request: EvidenceRequest,
        restoration_attempt: RestorationAttempt,
        budget: ExplorationBudget,
        request_lease: EvidenceRequestLeaseFence | None = None,
    ) -> ProbeWork: ...


@dataclass(frozen=True, slots=True, kw_only=True)
class TargetedRecaptureRestorationOutcome:
    """恢复终态及其可选 Probe 引用。"""

    restoration_attempt: RestorationAttempt
    probe_work: ProbeWork | None


class TargetedRecaptureRestorationService:
    """先完成恢复和等价门禁，再按同一证明发布 Probe。"""

    def __init__(
        self,
        *,
        coordinator: RestorationCoordinator,
        probe_publisher: EquivalentRestorationProbePublisher,
    ) -> None:
        self._coordinator = coordinator
        self._probe_publisher = probe_publisher

    async def restore_and_publish(
        self,
        *,
        request: EvidenceRequest,
        plan: RestorationPlan,
        original_snapshot: StateInvariantSnapshot,
        budget: ExplorationBudget,
        attempt_no: int,
        request_lease: EvidenceRequestLeaseFence | None = None,
    ) -> TargetedRecaptureRestorationOutcome:
        attempt = await self._coordinator.restore(
            request=request,
            plan=plan,
            attempt_no=attempt_no,
            original_snapshot=original_snapshot,
        )
        if attempt.status is not RestorationAttemptStatus.EQUIVALENT:
            return TargetedRecaptureRestorationOutcome(
                restoration_attempt=attempt,
                probe_work=None,
            )
        if request_lease is None:
            probe = await self._probe_publisher.publish(
                request=request,
                restoration_attempt=attempt,
                budget=budget,
            )
        else:
            probe = await self._probe_publisher.publish(
                request=request,
                restoration_attempt=attempt,
                budget=budget,
                request_lease=request_lease,
            )
        return TargetedRecaptureRestorationOutcome(
            restoration_attempt=attempt,
            probe_work=probe,
        )

    async def publish_equivalent(
        self,
        *,
        request: EvidenceRequest,
        restoration_attempt: RestorationAttempt,
        budget: ExplorationBudget,
        request_lease: EvidenceRequestLeaseFence | None = None,
    ) -> TargetedRecaptureRestorationOutcome:
        """复用已持久化等价结果，只重试 Probe 发布。"""

        if request_lease is None:
            probe = await self._probe_publisher.publish(
                request=request,
                restoration_attempt=restoration_attempt,
                budget=budget,
            )
        else:
            probe = await self._probe_publisher.publish(
                request=request,
                restoration_attempt=restoration_attempt,
                budget=budget,
                request_lease=request_lease,
            )
        return TargetedRecaptureRestorationOutcome(
            restoration_attempt=restoration_attempt,
            probe_work=probe,
        )
