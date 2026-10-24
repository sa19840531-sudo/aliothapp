"""Orchestrator 发布探索工作的应用层端口。"""

from __future__ import annotations

from typing import Protocol

from services.api.app.application.evidence_requests import EvidenceRequestLeaseFence
from services.api.app.application.task_finalization import (
    BeginExplorationDrainingCommand,
    BeginSessionDrainingCommand,
    BindReportToGateCommand,
    EvaluateCompletionGateCommand,
    FinalizeTaskWithReportCommand,
)
from services.api.app.domain.exploration import ExplorationWork, ProbeWork
from services.api.app.domain.task import (
    CompletionGateDecision,
    ReportGateBinding,
    TaskFinalizationSnapshot,
)


class ExplorationWorkPublisher(Protocol):
    """发布普通探索工作；持久化与 Outbox 实现由基础设施层提供。"""

    async def publish(self, work: ExplorationWork) -> None: ...


class ProbeWorkPublisher(Protocol):
    """发布证据补采工作；不直接调用 Explorer Agent。"""

    async def publish(
        self,
        work: ProbeWork,
        *,
        request_lease: EvidenceRequestLeaseFence | None = None,
    ) -> None: ...


class CompletionGatePreparer(Protocol):
    """Session Draining 后、Gate 评估前补齐期望的持久化工作事实。"""

    async def prepare(
        self,
        *,
        task_id: str,
        agent_run_id: str,
        finalization_epoch: int,
    ) -> object: ...


class TaskFinalizationCoordinator(Protocol):
    """MAF Workflow 调用的确定性任务收口端口。"""

    async def get(self, *, task_id: str) -> TaskFinalizationSnapshot | None: ...

    async def begin_exploration_draining(
        self,
        command: BeginExplorationDrainingCommand,
    ) -> TaskFinalizationSnapshot: ...

    async def begin_session_draining(
        self,
        command: BeginSessionDrainingCommand,
    ) -> TaskFinalizationSnapshot: ...

    async def evaluate_and_seal(
        self,
        command: EvaluateCompletionGateCommand,
    ) -> CompletionGateDecision: ...

    async def finalize_task_with_report(
        self,
        command: FinalizeTaskWithReportCommand,
    ) -> CompletionGateDecision: ...

    async def bind_report(
        self,
        command: BindReportToGateCommand,
    ) -> ReportGateBinding: ...
