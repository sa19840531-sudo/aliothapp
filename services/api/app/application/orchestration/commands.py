"""确定性 Orchestrator 的输入命令。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from services.api.app.application.evidence_requests import EvidenceRequestLeaseFence
from services.api.app.application.report_payloads import normalize_report_payload
from services.api.app.domain.exploration import (
    AppExplorationGoal,
    ExplorationBoundaries,
    ExplorationBudget,
    FrontierSnapshot,
)
from services.api.app.domain.inspection import (
    EvidenceRequest,
    EvidenceSessionDependency,
    EvidenceSourceGrade,
    RestorationPlan,
)
from services.api.app.domain.interaction import SurfaceKind
from services.api.app.domain.protocol import require_text
from services.api.app.domain.task import TaskExecutionStatus, TaskQualityOutcome


@dataclass(frozen=True, kw_only=True)
class StartExplorationCommand:
    """创建一条 Exploration Work 所需的确定性输入。"""

    task_id: str
    session_id: str
    branch_id: str
    objective: str
    surface_kind: SurfaceKind
    source_capture_id: str
    frontier_snapshot_id: str
    frontier: FrontierSnapshot
    boundaries: ExplorationBoundaries
    budget: ExplorationBudget
    deadline: datetime
    app_goal: AppExplorationGoal | None = None

    def __deepcopy__(self, _memo: dict[int, object]) -> StartExplorationCommand:
        """MAF 事件追踪复制消息时复用不可变命令。"""
        return self


@dataclass(frozen=True, kw_only=True)
class RouteEvidenceRequestCommand:
    """把 Evidence Request 转换为 Probe Work 所需的路由上下文。"""

    session_id: str
    branch_id: str
    surface_kind: SurfaceKind
    source_capture_id: str
    restoration_plan_hash: str
    restoration_attempt_id: str
    equivalence_assessment_id: str
    source_grade: EvidenceSourceGrade
    evidence_request: EvidenceRequest
    budget: ExplorationBudget
    deadline: datetime
    restoration_plan: RestorationPlan | None = None
    request_lease: EvidenceRequestLeaseFence | None = None

    def __post_init__(self) -> None:
        if self.request_lease is None:
            return
        if (
            self.request_lease.request_id != self.evidence_request.request_id
            or self.request_lease.task_id != self.evidence_request.task_id
        ):
            raise ValueError("Evidence Request 租约与路由请求身份不一致")
        # H5 复用仍受租约保护的原会话补采；恢复等价证明仍由发布服务及数据库验签。
        h5_snapshot = (
            self.evidence_request.session_dependency is EvidenceSessionDependency.SNAPSHOT
            and self.surface_kind is SurfaceKind.WEB
            and self.session_id.startswith("h5:")
            and self.restoration_plan is not None
            and self.restoration_plan.target_session_id == self.session_id
            and self.restoration_plan.surface_kind is SurfaceKind.WEB
        )
        if not h5_snapshot and (
            self.evidence_request.session_dependency is not EvidenceSessionDependency.ACTIVE_SESSION
        ):
            raise ValueError("只有 active_session Evidence Request 可以携带处理租约")

    def __deepcopy__(self, _memo: dict[int, object]) -> RouteEvidenceRequestCommand:
        """MAF 事件追踪复制消息时复用不可变命令。"""
        return self


@dataclass(frozen=True, kw_only=True)
class AdvanceTaskFinalizationCommand:
    """由 Worker 提交给 Orchestrator 的完整原子收口意图。"""

    task_id: str
    agent_run_id: str
    report_id: str
    report_version: int
    execution_status: TaskExecutionStatus
    quality_outcome: TaskQualityOutcome
    final_report: dict[str, object]
    recovery_lease_token: str | None = None
    recovery_generation: int | None = None
    recovery_lease_expires_at: datetime | None = None

    def __post_init__(self) -> None:
        for field_name in ("task_id", "agent_run_id", "report_id"):
            require_text(getattr(self, field_name), field_name)
        if (
            isinstance(self.report_version, bool)
            or not isinstance(self.report_version, int)
            or self.report_version <= 0
        ):
            raise ValueError("report_version 必须为正整数")
        if not isinstance(self.execution_status, TaskExecutionStatus):
            raise ValueError("execution_status 不受支持")
        if not isinstance(self.quality_outcome, TaskQualityOutcome):
            raise ValueError("quality_outcome 不受支持")
        if not isinstance(self.final_report, dict):
            raise ValueError("final_report 必须为对象")
        object.__setattr__(
            self,
            "final_report",
            normalize_report_payload(self.final_report),
        )
        _validate_recovery_fence(self)


@dataclass(frozen=True, kw_only=True)
class BindSealedReportCommand:
    """为已封存任务恢复绑定不可变正式报告。"""

    task_id: str
    agent_run_id: str
    report_id: str
    report_version: int
    final_report: dict[str, object]
    recovery_lease_token: str | None = None
    recovery_generation: int | None = None
    recovery_lease_expires_at: datetime | None = None

    def __post_init__(self) -> None:
        for field_name in ("task_id", "agent_run_id", "report_id"):
            require_text(getattr(self, field_name), field_name)
        if (
            isinstance(self.report_version, bool)
            or not isinstance(self.report_version, int)
            or self.report_version <= 0
        ):
            raise ValueError("report_version 必须为正整数")
        if not isinstance(self.final_report, dict):
            raise ValueError("final_report 必须为对象")
        object.__setattr__(
            self,
            "final_report",
            normalize_report_payload(self.final_report),
        )
        if (
            self.final_report.get("report_stage") != "final"
            or self.final_report.get("is_final") is not True
        ):
            raise ValueError("sealed 任务只能绑定正式报告")
        _validate_recovery_fence(self)


@dataclass(frozen=True, kw_only=True)
class TaskFinalizationProgress:
    """Orchestrator 推进收口后返回给 Worker 的稳定引用结果。"""

    gate_passed: bool
    gate_snapshot_id: str
    finalization_epoch: int
    block_reasons: tuple[str, ...] = ()


def _validate_recovery_fence(command: object) -> None:
    token = getattr(command, "recovery_lease_token", None)
    generation = getattr(command, "recovery_generation", None)
    expires_at = getattr(command, "recovery_lease_expires_at", None)
    if token is None and generation is None and expires_at is None:
        return
    require_text(token, "recovery_lease_token")
    if isinstance(generation, bool) or not isinstance(generation, int) or generation < 0:
        raise ValueError("recovery_generation 必须为非负整数")
    if not isinstance(expires_at, datetime):
        raise ValueError("recovery_lease_expires_at 必须为 datetime")
