"""探索、证据和质检工作状态的统一跨模块投影。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from services.api.app.application.evidence_requests import EvidenceRequestStatus
from services.api.app.application.exploration_work_queue import ExplorerWorkStatus
from services.api.app.application.inspection_work_items import InspectionWorkItemStatus


class WorkType(StrEnum):
    """V2 三类持久化工作。"""

    EXPLORATION = "exploration"
    EVIDENCE = "evidence"
    INSPECTION = "inspection"


class WorkLifecyclePhase(StrEnum):
    """跨模块可比较的工作生命周期阶段。"""

    PENDING = "pending"
    LEASED = "leased"
    RUNNING = "running"
    WAITING = "waiting"
    RETRY_WAIT = "retry_wait"
    TERMINAL = "terminal"


class WorkTerminalOutcome(StrEnum):
    """终态结果投影；不替代各领域原始状态。"""

    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    BOUNDED = "bounded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    SUPERSEDED = "superseded"
    EXPIRED = "expired"
    UNFULFILLABLE = "unfulfillable"


@dataclass(frozen=True, kw_only=True)
class WorkStatusProjection:
    """保留原始状态的统一读模型。"""

    work_type: WorkType
    raw_status: str
    phase: WorkLifecyclePhase
    terminal_outcome: WorkTerminalOutcome | None = None

    @property
    def is_terminal(self) -> bool:
        return self.phase is WorkLifecyclePhase.TERMINAL


_PROJECTIONS: dict[tuple[WorkType, str], tuple[WorkLifecyclePhase, WorkTerminalOutcome | None]] = {
    (WorkType.EXPLORATION, ExplorerWorkStatus.PENDING.value): (WorkLifecyclePhase.PENDING, None),
    (WorkType.EXPLORATION, ExplorerWorkStatus.LEASED.value): (WorkLifecyclePhase.LEASED, None),
    (WorkType.EXPLORATION, ExplorerWorkStatus.RUNNING.value): (WorkLifecyclePhase.RUNNING, None),
    (WorkType.EXPLORATION, ExplorerWorkStatus.RETRY_WAIT.value): (
        WorkLifecyclePhase.RETRY_WAIT,
        None,
    ),
    (WorkType.EXPLORATION, ExplorerWorkStatus.SUCCEEDED.value): (
        WorkLifecyclePhase.TERMINAL,
        WorkTerminalOutcome.SUCCEEDED,
    ),
    (WorkType.EXPLORATION, ExplorerWorkStatus.BOUNDED.value): (
        WorkLifecyclePhase.TERMINAL,
        WorkTerminalOutcome.BOUNDED,
    ),
    (WorkType.EXPLORATION, ExplorerWorkStatus.BUDGET_EXHAUSTED.value): (
        WorkLifecyclePhase.TERMINAL,
        WorkTerminalOutcome.BOUNDED,
    ),
    (WorkType.EXPLORATION, ExplorerWorkStatus.BLOCKED.value): (
        WorkLifecyclePhase.TERMINAL,
        WorkTerminalOutcome.BOUNDED,
    ),
    (WorkType.EXPLORATION, ExplorerWorkStatus.NO_PROGRESS.value): (
        WorkLifecyclePhase.TERMINAL,
        WorkTerminalOutcome.BOUNDED,
    ),
    (WorkType.EXPLORATION, ExplorerWorkStatus.FAILED.value): (
        WorkLifecyclePhase.TERMINAL,
        WorkTerminalOutcome.FAILED,
    ),
    (WorkType.EXPLORATION, ExplorerWorkStatus.CANCELLED.value): (
        WorkLifecyclePhase.TERMINAL,
        WorkTerminalOutcome.CANCELLED,
    ),
    (WorkType.EXPLORATION, ExplorerWorkStatus.SUPERSEDED.value): (
        WorkLifecyclePhase.TERMINAL,
        WorkTerminalOutcome.SUPERSEDED,
    ),
    (WorkType.EVIDENCE, EvidenceRequestStatus.PENDING.value): (WorkLifecyclePhase.PENDING, None),
    (WorkType.EVIDENCE, EvidenceRequestStatus.ROUTED.value): (WorkLifecyclePhase.LEASED, None),
    (WorkType.EVIDENCE, EvidenceRequestStatus.RUNNING.value): (WorkLifecyclePhase.RUNNING, None),
    (WorkType.EVIDENCE, EvidenceRequestStatus.SATISFIED.value): (
        WorkLifecyclePhase.TERMINAL,
        WorkTerminalOutcome.SUCCEEDED,
    ),
    (WorkType.EVIDENCE, EvidenceRequestStatus.UNFULFILLABLE.value): (
        WorkLifecyclePhase.TERMINAL,
        WorkTerminalOutcome.UNFULFILLABLE,
    ),
    (WorkType.EVIDENCE, EvidenceRequestStatus.EXPIRED.value): (
        WorkLifecyclePhase.TERMINAL,
        WorkTerminalOutcome.EXPIRED,
    ),
    (WorkType.EVIDENCE, EvidenceRequestStatus.CANCELLED.value): (
        WorkLifecyclePhase.TERMINAL,
        WorkTerminalOutcome.CANCELLED,
    ),
    (WorkType.INSPECTION, InspectionWorkItemStatus.PENDING.value): (
        WorkLifecyclePhase.PENDING,
        None,
    ),
    (WorkType.INSPECTION, InspectionWorkItemStatus.LEASED.value): (
        WorkLifecyclePhase.LEASED,
        None,
    ),
    (WorkType.INSPECTION, InspectionWorkItemStatus.RUNNING.value): (
        WorkLifecyclePhase.RUNNING,
        None,
    ),
    (WorkType.INSPECTION, InspectionWorkItemStatus.RETRY_WAIT.value): (
        WorkLifecyclePhase.RETRY_WAIT,
        None,
    ),
    (WorkType.INSPECTION, InspectionWorkItemStatus.WAITING_EVIDENCE.value): (
        WorkLifecyclePhase.WAITING,
        None,
    ),
    (WorkType.INSPECTION, InspectionWorkItemStatus.SUCCEEDED.value): (
        WorkLifecyclePhase.TERMINAL,
        WorkTerminalOutcome.SUCCEEDED,
    ),
    (WorkType.INSPECTION, InspectionWorkItemStatus.PARTIAL.value): (
        WorkLifecyclePhase.TERMINAL,
        WorkTerminalOutcome.PARTIAL,
    ),
    (WorkType.INSPECTION, InspectionWorkItemStatus.FAILED.value): (
        WorkLifecyclePhase.TERMINAL,
        WorkTerminalOutcome.FAILED,
    ),
    (WorkType.INSPECTION, InspectionWorkItemStatus.CANCELLED.value): (
        WorkLifecyclePhase.TERMINAL,
        WorkTerminalOutcome.CANCELLED,
    ),
    (WorkType.INSPECTION, InspectionWorkItemStatus.SUPERSEDED.value): (
        WorkLifecyclePhase.TERMINAL,
        WorkTerminalOutcome.SUPERSEDED,
    ),
}


def project_work_status(*, work_type: WorkType, raw_status: str) -> WorkStatusProjection:
    """把领域状态转换为冻结的跨模块读模型。"""
    if not isinstance(work_type, WorkType):
        raise ValueError("work_type 类型不正确")
    if not isinstance(raw_status, str) or not raw_status.strip():
        raise ValueError("raw_status 必须为非空字符串")
    normalized = raw_status.strip().lower()
    projection = _PROJECTIONS.get((work_type, normalized))
    if projection is None:
        raise ValueError(f"{work_type.value} 不支持状态 {raw_status}")
    phase, outcome = projection
    return WorkStatusProjection(
        work_type=work_type,
        raw_status=normalized,
        phase=phase,
        terminal_outcome=outcome,
    )
