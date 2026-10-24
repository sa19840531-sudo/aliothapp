"""在状态等价恢复完成后发布正式 Probe Work。"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Protocol

from services.api.app.application.evidence_requests import EvidenceRequestLeaseFence
from services.api.app.application.orchestration.commands import (
    RouteEvidenceRequestCommand,
)
from services.api.app.domain.exploration import ExplorationBudget, ProbeWork
from services.api.app.domain.inspection import (
    EvidenceRequest,
    RestorationAttempt,
    RestorationAttemptStatus,
)


class RestoredProbePublicationError(RuntimeError):
    """恢复结果不能作为 Probe 来源时的结构化错误。"""

    retryable = False

    def __init__(self, *, reason_code: str, message: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code


class RestoredProbeOrchestrator(Protocol):
    """发布 Probe 所需的最小 Orchestrator 端口。"""

    async def route_evidence_request(
        self,
        command: RouteEvidenceRequestCommand,
    ) -> ProbeWork: ...


class RestoredProbePublicationService:
    """验签等价恢复终态，并从恢复后的新 Capture 发布 Probe。"""

    def __init__(
        self,
        *,
        orchestrator: RestoredProbeOrchestrator,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._orchestrator = orchestrator
        self._clock = clock or (lambda: datetime.now(UTC))

    async def publish(
        self,
        *,
        request: EvidenceRequest,
        restoration_attempt: RestorationAttempt,
        budget: ExplorationBudget,
        request_lease: EvidenceRequestLeaseFence | None = None,
    ) -> ProbeWork:
        if not isinstance(request, EvidenceRequest):
            raise TypeError("request 必须为 EvidenceRequest")
        if not isinstance(restoration_attempt, RestorationAttempt):
            raise TypeError("restoration_attempt 必须为 RestorationAttempt")
        if not isinstance(budget, ExplorationBudget):
            raise TypeError("budget 必须为 ExplorationBudget")

        attempt = restoration_attempt
        plan = attempt.plan
        assessment = attempt.assessment
        if (
            attempt.status is not RestorationAttemptStatus.EQUIVALENT
            or assessment is None
            or not assessment.passed
            or attempt.restored_capture_id is None
        ):
            raise _publication_error(
                "RESTORATION_NOT_EQUIVALENT",
                "只有通过状态等价门禁的恢复结果才能发布 Probe",
            )
        if (
            plan.request_id != request.request_id
            or plan.task_id != request.task_id
            or plan.original_capture_id != request.target_capture_id
            or assessment.request_id != request.request_id
            or assessment.task_id != request.task_id
            or assessment.restoration_attempt_id != attempt.attempt_id
            or assessment.restored_capture_id != attempt.restored_capture_id
            or assessment.source_grade is not plan.source_grade
        ):
            raise _publication_error(
                "RESTORATION_RESULT_IDENTITY_MISMATCH",
                "恢复结果、状态等价证明与 Evidence Request 身份不一致",
            )
        now = self._clock()
        deadline = min(plan.deadline, request.expires_at)
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("clock 必须返回带时区时间")
        if now >= deadline:
            raise _publication_error(
                "RESTORATION_RESULT_EXPIRED",
                "恢复结果已超过 Probe 截止时间",
            )

        return await self._orchestrator.route_evidence_request(
            RouteEvidenceRequestCommand(
                session_id=plan.target_session_id,
                branch_id=f"probe:{request.request_id}",
                surface_kind=plan.surface_kind,
                source_capture_id=attempt.restored_capture_id,
                restoration_plan_hash=plan.plan_hash,
                restoration_plan=plan,
                restoration_attempt_id=attempt.attempt_id,
                equivalence_assessment_id=assessment.assessment_id,
                source_grade=assessment.source_grade,
                evidence_request=request,
                budget=budget,
                deadline=deadline,
                request_lease=request_lease,
            )
        )


def _publication_error(reason_code: str, message: str) -> RestoredProbePublicationError:
    return RestoredProbePublicationError(reason_code=reason_code, message=message)
