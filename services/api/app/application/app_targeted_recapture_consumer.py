"""App Evidence Request 的单次租约消费编排。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Protocol

from services.api.app.application.app_targeted_recapture_composition import (
    AppTargetedRecaptureRuntime,
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
from services.api.app.application.restored_probe_publication import (
    RestoredProbePublicationError,
)
from services.api.app.application.supplemental_evidence_policy import (
    SupplementalEvidenceOperation,
    SupplementalEvidencePolicy,
)
from services.api.app.domain.exploration import (
    ExplorationBudget,
    consume_exploration_action_budget,
)
from services.api.app.domain.inspection import (
    EvidenceRequest,
    RestorationAttemptStatus,
    RestorationPlan,
    build_restoration_plan,
)

_RECAPTURE_DEADLINE_EXHAUSTED_REASON = "RECAPTURE_DEADLINE_EXHAUSTED"


class AppTargetedRecaptureRuntimeBuilder(Protocol):
    """按当前 Run/Epoch 构造 App 补采运行时。"""

    def build(
        self,
        *,
        agent_run_id: str,
        finalization_epoch: int,
    ) -> AppTargetedRecaptureRuntime: ...


class AppTargetedRecaptureDisposition(StrEnum):
    """一次请求消费的确定性结果。"""

    EMPTY = "empty"
    PROBE_PUBLISHED = "probe_published"
    RETRY_SCHEDULED = "retry_scheduled"
    UNFULFILLABLE = "unfulfillable"
    EXPIRED = "expired"


@dataclass(frozen=True, slots=True, kw_only=True)
class AppTargetedRecaptureConsumerOutcome:
    """消费者结果只描述请求状态，不替代仓储权威事实。"""

    disposition: AppTargetedRecaptureDisposition
    request_id: str | None = None
    reason_code: str | None = None
    consumed_action_count: int = 0

    def __post_init__(self) -> None:
        if (
            isinstance(self.consumed_action_count, bool)
            or not isinstance(self.consumed_action_count, int)
            or self.consumed_action_count < 0
        ):
            raise ValueError("consumed_action_count 必须为非负整数")


class AppTargetedRecaptureConsumer:
    """先续发已完成恢复，再按租约执行一次恢复或可靠结算。"""

    def __init__(
        self,
        *,
        requests: EvidenceRequestRepository,
        runtime_factory: AppTargetedRecaptureRuntimeBuilder,
        policy: SupplementalEvidencePolicy,
        lease_owner: str,
        lease_seconds: int = 60,
        retry_delay_seconds: int = 5,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not isinstance(policy, SupplementalEvidencePolicy):
            raise TypeError("policy 必须为 SupplementalEvidencePolicy")
        if not isinstance(lease_owner, str) or not lease_owner.strip():
            raise ValueError("lease_owner 不能为空")
        for value, field_name in (
            (lease_seconds, "lease_seconds"),
            (retry_delay_seconds, "retry_delay_seconds"),
        ):
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"{field_name} 必须为整数")
            if value <= 0:
                raise ValueError(f"{field_name} 必须大于零")
        self._requests = requests
        self._runtime_factory = runtime_factory
        self._policy = policy
        self._lease_owner = lease_owner
        self._lease_seconds = lease_seconds
        self._retry_delay = timedelta(seconds=retry_delay_seconds)
        self._clock = clock or (lambda: datetime.now(UTC))

    async def consume_next(
        self,
        *,
        task_id: str,
        agent_run_id: str,
        finalization_epoch: int,
        budget: ExplorationBudget,
        deadline: datetime | None = None,
    ) -> AppTargetedRecaptureConsumerOutcome:
        if not isinstance(budget, ExplorationBudget):
            raise TypeError("budget 必须为 ExplorationBudget")
        self._policy.require_enabled(SupplementalEvidenceOperation.EXECUTE_PROBE)
        _validate_optional_deadline(deadline)
        started_at = self._now()
        effective_deadline = _effective_deadline(
            started_at=started_at,
            budget=budget,
            deadline=deadline,
        )
        claim = await self._requests.claim_next(
            task_id=task_id,
            agent_run_id=agent_run_id,
            finalization_epoch=finalization_epoch,
            lease_owner=self._lease_owner,
            lease_seconds=self._lease_seconds,
        )
        if claim.disposition is EvidenceRequestClaimDisposition.EMPTY:
            return AppTargetedRecaptureConsumerOutcome(
                disposition=AppTargetedRecaptureDisposition.EMPTY
            )
        lease = claim.lease
        if lease is None:
            raise RuntimeError("已领取 Evidence Request 缺少处理租约")
        request = lease.request
        now = self._now()
        if now >= effective_deadline:
            return await self._settle_deadline(lease=lease)
        if budget.resolved_remaining_business_actions == 0:
            return await self._settle(
                lease=lease,
                status=EvidenceRequestStatus.UNFULFILLABLE,
                disposition=AppTargetedRecaptureDisposition.UNFULFILLABLE,
                reason_code="RECAPTURE_ACTION_BUDGET_EXHAUSTED",
            )
        resume_budget = self._budget_at(
            budget=budget,
            effective_deadline=effective_deadline,
            now=now,
        )
        if resume_budget.remaining_duration_ms == 0:
            return await self._settle_deadline(lease=lease)
        runtime = self._runtime_factory.build(
            agent_run_id=agent_run_id,
            finalization_epoch=finalization_epoch,
        )

        try:
            resumed = await runtime.resume_equivalent_publication(
                request=request,
                budget=resume_budget,
                request_lease=lease.fence,
            )
        except Exception as exc:
            return await self._release_runtime_error(lease=lease, error=exc)
        if resumed is None and self._now() >= effective_deadline:
            return await self._settle_deadline(lease=lease)
        if resumed is not None:
            return AppTargetedRecaptureConsumerOutcome(
                disposition=AppTargetedRecaptureDisposition.PROBE_PUBLISHED,
                request_id=request.request_id,
            )

        try:
            resolution = await runtime.resolve_plan(request=request)
        except Exception as exc:
            return await self._release_runtime_error(lease=lease, error=exc)
        now = self._now()
        if now >= effective_deadline:
            return await self._settle_deadline(lease=lease)
        if resolution.plan is None:
            reason_code = resolution.reason_code or "RECAPTURE_PLAN_UNAVAILABLE"
            if reason_code == "EVIDENCE_REQUEST_EXPIRED":
                return await self._settle(
                    lease=lease,
                    status=EvidenceRequestStatus.EXPIRED,
                    disposition=AppTargetedRecaptureDisposition.EXPIRED,
                    reason_code=reason_code,
                )
            return await self._release(lease=lease, reason_code=reason_code)
        if not resolution.executable:
            return await self._settle(
                lease=lease,
                status=EvidenceRequestStatus.UNFULFILLABLE,
                disposition=AppTargetedRecaptureDisposition.UNFULFILLABLE,
                reason_code=resolution.reason_code or "RECAPTURE_PLAN_UNFULFILLABLE",
            )

        current_budget = self._budget_at(
            budget=budget,
            effective_deadline=effective_deadline,
            now=now,
        )
        if current_budget.remaining_duration_ms == 0:
            return await self._settle_deadline(lease=lease)
        restoration_action_count = len(resolution.plan.normalization_step_ids)
        if restoration_action_count + 1 > current_budget.resolved_remaining_business_actions:
            return await self._settle(
                lease=lease,
                status=EvidenceRequestStatus.UNFULFILLABLE,
                disposition=AppTargetedRecaptureDisposition.UNFULFILLABLE,
                reason_code="RECAPTURE_ACTION_BUDGET_EXHAUSTED",
            )
        probe_budget = current_budget
        for _ in range(restoration_action_count):
            probe_budget = consume_exploration_action_budget(
                probe_budget,
                counts_toward_business=True,
                elapsed_duration_ms=0,
            )

        now = self._now()
        execution_deadline = min(resolution.plan.deadline, effective_deadline)
        if now >= execution_deadline:
            return await self._settle_deadline(lease=lease)
        execution_plan = _tighten_plan_deadline(
            request=request,
            plan=resolution.plan,
            deadline=execution_deadline,
        )

        try:
            result = await runtime.restore_and_publish(
                request=request,
                plan=execution_plan,
                budget=probe_budget,
                attempt_no=lease.attempt_count,
                request_lease=lease.fence,
            )
        except Exception as exc:
            return await self._release_runtime_error(
                lease=lease,
                error=exc,
                consumed_action_count=restoration_action_count,
            )
        attempt = result.restoration_attempt
        if attempt.status is RestorationAttemptStatus.EQUIVALENT and result.probe_work is not None:
            return AppTargetedRecaptureConsumerOutcome(
                disposition=AppTargetedRecaptureDisposition.PROBE_PUBLISHED,
                request_id=request.request_id,
                consumed_action_count=restoration_action_count,
            )
        if attempt.status is RestorationAttemptStatus.UNFULFILLABLE:
            return await self._settle(
                lease=lease,
                status=EvidenceRequestStatus.UNFULFILLABLE,
                disposition=AppTargetedRecaptureDisposition.UNFULFILLABLE,
                reason_code=attempt.error_code or "RESTORATION_UNFULFILLABLE",
                consumed_action_count=len(attempt.action_execution_ids),
            )
        released = await self._release(
            lease=lease,
            reason_code=attempt.error_code or f"RESTORATION_{attempt.status.value.upper()}",
        )
        return AppTargetedRecaptureConsumerOutcome(
            disposition=released.disposition,
            request_id=released.request_id,
            reason_code=released.reason_code,
            consumed_action_count=len(attempt.action_execution_ids),
        )

    async def _settle_deadline(
        self,
        *,
        lease: EvidenceRequestProcessingLease,
    ) -> AppTargetedRecaptureConsumerOutcome:
        return await self._settle(
            lease=lease,
            status=EvidenceRequestStatus.UNFULFILLABLE,
            disposition=AppTargetedRecaptureDisposition.UNFULFILLABLE,
            reason_code=_RECAPTURE_DEADLINE_EXHAUSTED_REASON,
        )

    def _budget_at(
        self,
        *,
        budget: ExplorationBudget,
        effective_deadline: datetime,
        now: datetime | None = None,
    ) -> ExplorationBudget:
        current = self._now() if now is None else now
        remaining_to_deadline_ms = max(
            int((effective_deadline - current).total_seconds() * 1_000),
            0,
        )
        return replace(
            budget,
            remaining_duration_ms=min(
                budget.remaining_duration_ms,
                remaining_to_deadline_ms,
            ),
        )

    async def _release_runtime_error(
        self,
        *,
        lease: EvidenceRequestProcessingLease,
        error: Exception,
        consumed_action_count: int = 0,
    ) -> AppTargetedRecaptureConsumerOutcome:
        if isinstance(error, RestoredProbePublicationError) and not error.retryable:
            return await self._settle(
                lease=lease,
                status=EvidenceRequestStatus.UNFULFILLABLE,
                disposition=AppTargetedRecaptureDisposition.UNFULFILLABLE,
                reason_code=error.reason_code,
                consumed_action_count=consumed_action_count,
            )
        released = await self._release(
            lease=lease,
            reason_code=f"RECAPTURE_RUNTIME_{type(error).__name__.upper()}",
        )
        return AppTargetedRecaptureConsumerOutcome(
            disposition=released.disposition,
            request_id=released.request_id,
            reason_code=released.reason_code,
            consumed_action_count=consumed_action_count,
        )

    async def _release(
        self,
        *,
        lease: EvidenceRequestProcessingLease,
        reason_code: str,
    ) -> AppTargetedRecaptureConsumerOutcome:
        released = await self._requests.release_lease(
            command=ReleaseEvidenceRequestLeaseCommand(
                request_id=lease.request.request_id,
                task_id=lease.request.task_id,
                finalization_epoch=lease.finalization_epoch,
                lease_owner=lease.lease_owner,
                lease_token=lease.lease_token,
                row_version=lease.row_version,
                next_attempt_at=self._now() + self._retry_delay,
                reason_code=reason_code,
            )
        )
        if released.disposition is EvidenceRequestLeaseReleaseDisposition.RETRY_SCHEDULED:
            disposition = AppTargetedRecaptureDisposition.RETRY_SCHEDULED
        elif released.disposition is EvidenceRequestLeaseReleaseDisposition.REQUEST_EXPIRED:
            disposition = AppTargetedRecaptureDisposition.EXPIRED
        else:
            disposition = AppTargetedRecaptureDisposition.UNFULFILLABLE
        return AppTargetedRecaptureConsumerOutcome(
            disposition=disposition,
            request_id=lease.request.request_id,
            reason_code=reason_code,
        )

    async def _settle(
        self,
        *,
        lease: EvidenceRequestProcessingLease,
        status: EvidenceRequestStatus,
        disposition: AppTargetedRecaptureDisposition,
        reason_code: str,
        consumed_action_count: int = 0,
    ) -> AppTargetedRecaptureConsumerOutcome:
        await self._requests.settle_lease(
            command=SettleEvidenceRequestLeaseCommand(
                request_id=lease.request.request_id,
                task_id=lease.request.task_id,
                finalization_epoch=lease.finalization_epoch,
                lease_owner=lease.lease_owner,
                lease_token=lease.lease_token,
                row_version=lease.row_version,
                target_status=status,
                resolution_reason=reason_code,
            )
        )
        return AppTargetedRecaptureConsumerOutcome(
            disposition=disposition,
            request_id=lease.request.request_id,
            reason_code=reason_code,
            consumed_action_count=consumed_action_count,
        )

    def _now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("clock 必须返回带时区时间")
        return now


def _validate_optional_deadline(deadline: datetime | None) -> None:
    if deadline is None:
        return
    if not isinstance(deadline, datetime):
        raise TypeError("deadline 必须为带时区的 datetime")
    if deadline.tzinfo is None or deadline.utcoffset() is None:
        raise ValueError("deadline 必须为带时区的 datetime")


def _effective_deadline(
    *,
    started_at: datetime,
    budget: ExplorationBudget,
    deadline: datetime | None,
) -> datetime:
    budget_deadline = started_at + timedelta(milliseconds=budget.remaining_duration_ms)
    if deadline is None:
        return budget_deadline
    return min(budget_deadline, deadline)


def _tighten_plan_deadline(
    *,
    request: EvidenceRequest,
    plan: RestorationPlan,
    deadline: datetime,
) -> RestorationPlan:
    if plan.deadline <= deadline:
        return plan
    return build_restoration_plan(
        request=request,
        source_session_id=plan.source_session_id,
        target_session_id=plan.target_session_id,
        surface_kind=plan.surface_kind,
        strategy=plan.strategy,
        deadline=deadline,
        replay_execution_plan_id=plan.replay_execution_plan_id,
        normalization_step_ids=plan.normalization_step_ids,
    )
