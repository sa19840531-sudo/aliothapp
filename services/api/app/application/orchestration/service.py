"""预算、证据请求与探索工作发布的确定性应用编排入口。"""

from __future__ import annotations

from services.api.app.application.orchestration.commands import (
    AdvanceTaskFinalizationCommand,
    BindSealedReportCommand,
    RouteEvidenceRequestCommand,
    StartExplorationCommand,
    TaskFinalizationProgress,
)
from services.api.app.application.orchestration.errors import OrchestrationWorkflowError
from services.api.app.application.orchestration.maf_workflow import (
    MicrosoftAgentFrameworkQcWorkflow,
)
from services.api.app.application.orchestration.ports import (
    CompletionGatePreparer,
    ExplorationWorkPublisher,
    ProbeWorkPublisher,
    TaskFinalizationCoordinator,
)
from services.api.app.application.report_payloads import stable_report_payload_hash
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
    TaskFinalizationSnapshot,
    TaskFinalizationState,
)


class QcOrchestrator:
    """只做状态转换与工作发布，不执行探索决策或规则判断。"""

    def __init__(
        self,
        *,
        exploration_publisher: ExplorationWorkPublisher | None = None,
        probe_publisher: ProbeWorkPublisher | None = None,
        finalization_coordinator: TaskFinalizationCoordinator | None = None,
        completion_gate_preparer: CompletionGatePreparer | None = None,
    ) -> None:
        self._finalization_coordinator = finalization_coordinator
        self._completion_gate_preparer = completion_gate_preparer
        self._workflow = MicrosoftAgentFrameworkQcWorkflow(
            exploration_publisher=exploration_publisher,
            probe_publisher=probe_publisher,
            finalization_coordinator=finalization_coordinator,
        )

    async def start_exploration(
        self,
        command: StartExplorationCommand,
    ) -> ExplorationWork:
        """通过 MAF Workflow 构造并发布稳定的 Exploration Work。"""
        return await self._workflow.start_exploration(command)

    async def prepare_exploration(
        self,
        command: StartExplorationCommand,
    ) -> ExplorationWork:
        """只构造后继 Work，不在父 Work 事务之外提前发布。"""
        return await self._workflow.prepare_exploration(command)

    async def route_evidence_request(
        self,
        command: RouteEvidenceRequestCommand,
    ) -> ProbeWork:
        """通过 MAF Workflow 把 Evidence Request 转换并发布为 Probe Work。"""
        return await self._workflow.route_evidence_request(command)

    async def begin_exploration_draining(
        self,
        command: BeginExplorationDrainingCommand,
    ) -> TaskFinalizationSnapshot:
        """通过 MAF Workflow 停止普通 Frontier 工作入口。"""
        return await self._workflow.begin_exploration_draining(command)

    async def begin_session_draining(
        self,
        command: BeginSessionDrainingCommand,
    ) -> TaskFinalizationSnapshot:
        """通过 MAF Workflow 把任务内 Surface Session 转入排空态。"""
        return await self._workflow.begin_session_draining(command)

    async def evaluate_completion_gate(
        self,
        command: EvaluateCompletionGateCommand,
    ) -> CompletionGateDecision:
        """通过 MAF Workflow 事务重算门禁，并只在放行时封存任务。"""
        return await self._workflow.evaluate_completion_gate(command)

    async def advance_task_finalization(
        self,
        command: AdvanceTaskFinalizationCommand,
    ) -> TaskFinalizationProgress:
        """按持久化状态推进 Draining，并以最终报告原子评估 Completion Gate。"""
        if not isinstance(command, AdvanceTaskFinalizationCommand):
            raise TypeError("command 必须为 AdvanceTaskFinalizationCommand")
        coordinator = self._require_finalization_coordinator()
        snapshot = await coordinator.get(task_id=command.task_id)
        if snapshot is None:
            raise OrchestrationWorkflowError(f"未找到任务收口快照：{command.task_id}")
        if snapshot.agent_run_id != command.agent_run_id:
            raise OrchestrationWorkflowError("任务收口 Run 与当前命令不匹配")
        if snapshot.state is TaskFinalizationState.SEALED:
            if snapshot.gate_snapshot_id is None:
                raise OrchestrationWorkflowError("已封存任务缺少 Completion Gate Snapshot")
            return TaskFinalizationProgress(
                gate_passed=True,
                gate_snapshot_id=snapshot.gate_snapshot_id,
                finalization_epoch=snapshot.finalization_epoch,
            )
        if snapshot.state is TaskFinalizationState.OPEN:
            snapshot = await self.begin_exploration_draining(
                BeginExplorationDrainingCommand(
                    task_id=command.task_id,
                    agent_run_id=command.agent_run_id,
                    expected_row_version=snapshot.row_version,
                    recovery_lease_token=command.recovery_lease_token,
                    recovery_generation=command.recovery_generation,
                    recovery_lease_expires_at=command.recovery_lease_expires_at,
                )
            )
        if snapshot.state is TaskFinalizationState.EXPLORATION_DRAINING:
            snapshot = await self.begin_session_draining(
                BeginSessionDrainingCommand(
                    task_id=command.task_id,
                    agent_run_id=command.agent_run_id,
                    expected_finalization_epoch=snapshot.finalization_epoch,
                    expected_row_version=snapshot.row_version,
                    recovery_lease_token=command.recovery_lease_token,
                    recovery_generation=command.recovery_generation,
                    recovery_lease_expires_at=command.recovery_lease_expires_at,
                )
            )
        if snapshot.state is not TaskFinalizationState.SESSION_DRAINING:
            raise OrchestrationWorkflowError(
                f"任务处于不可评估的收口状态：{snapshot.state.value}"
            )
        if self._completion_gate_preparer is not None:
            await self._completion_gate_preparer.prepare(
                task_id=command.task_id,
                agent_run_id=command.agent_run_id,
                finalization_epoch=snapshot.finalization_epoch,
                **_recovery_fence_kwargs(command),
            )
        decision = await coordinator.finalize_task_with_report(
            FinalizeTaskWithReportCommand(
                task_id=command.task_id,
                agent_run_id=command.agent_run_id,
                report_id=command.report_id,
                report_version=command.report_version,
                expected_finalization_epoch=snapshot.finalization_epoch,
                expected_row_version=snapshot.row_version,
                execution_status=command.execution_status,
                quality_outcome=command.quality_outcome,
                final_report=command.final_report,
                recovery_lease_token=command.recovery_lease_token,
                recovery_generation=command.recovery_generation,
                recovery_lease_expires_at=command.recovery_lease_expires_at,
            )
        )
        return TaskFinalizationProgress(
            gate_passed=decision.snapshot.gate_passed,
            gate_snapshot_id=decision.snapshot.gate_snapshot_id,
            finalization_epoch=decision.snapshot.finalization_epoch,
            block_reasons=tuple(item.value for item in decision.snapshot.block_reasons),
        )

    async def bind_sealed_report(
        self,
        command: BindSealedReportCommand,
    ) -> TaskFinalizationProgress:
        """校验 sealed/owner/gate 后恢复绑定不可变正式报告。"""
        if not isinstance(command, BindSealedReportCommand):
            raise TypeError("command 必须为 BindSealedReportCommand")
        coordinator = self._require_finalization_coordinator()
        snapshot = await coordinator.get(task_id=command.task_id)
        if snapshot is None:
            raise OrchestrationWorkflowError(f"未找到任务收口快照：{command.task_id}")
        if snapshot.agent_run_id != command.agent_run_id:
            raise OrchestrationWorkflowError("任务收口 Run 与当前命令不匹配")
        if snapshot.state is not TaskFinalizationState.SEALED:
            raise OrchestrationWorkflowError("只有已封存任务可以恢复绑定正式报告")
        if snapshot.gate_snapshot_id is None:
            raise OrchestrationWorkflowError("已封存任务缺少 Completion Gate Snapshot")
        await coordinator.bind_report(
            BindReportToGateCommand(
                report_id=command.report_id,
                report_version=command.report_version,
                report_payload_hash=stable_report_payload_hash(command.final_report),
                final_report=command.final_report,
                task_id=command.task_id,
                gate_snapshot_id=snapshot.gate_snapshot_id,
                finalization_epoch=snapshot.finalization_epoch,
                agent_run_id=command.agent_run_id,
                recovery_lease_token=command.recovery_lease_token,
                recovery_generation=command.recovery_generation,
                recovery_lease_expires_at=command.recovery_lease_expires_at,
            )
        )
        return TaskFinalizationProgress(
            gate_passed=True,
            gate_snapshot_id=snapshot.gate_snapshot_id,
            finalization_epoch=snapshot.finalization_epoch,
        )

    def _require_finalization_coordinator(self) -> TaskFinalizationCoordinator:
        coordinator = self._finalization_coordinator
        if coordinator is None:
            raise OrchestrationWorkflowError("Orchestrator 尚未配置 Task Finalization 端口")
        return coordinator


def _recovery_fence_kwargs(command: object) -> dict[str, object]:
    token = getattr(command, "recovery_lease_token", None)
    if token is None:
        return {}
    return {
        "recovery_lease_token": token,
        "recovery_generation": getattr(command, "recovery_generation", None),
        "recovery_lease_expires_at": getattr(
            command,
            "recovery_lease_expires_at",
            None,
        ),
    }
