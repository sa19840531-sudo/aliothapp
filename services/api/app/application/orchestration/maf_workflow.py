"""基于 Microsoft Agent Framework Workflow 的确定性编排实现。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Never

from agent_framework import (
    Executor,
    Workflow,
    WorkflowBuilder,
    WorkflowContext,
    WorkflowRunResult,
    WorkflowRunState,
    handler,
)

from services.api.app.application.evidence_requests import EvidenceRequestLeaseFence
from services.api.app.application.orchestration.commands import (
    RouteEvidenceRequestCommand,
    StartExplorationCommand,
)
from services.api.app.application.orchestration.errors import OrchestrationWorkflowError
from services.api.app.application.orchestration.ports import (
    ExplorationWorkPublisher,
    ProbeWorkPublisher,
    TaskFinalizationCoordinator,
)
from services.api.app.application.task_finalization import (
    BeginExplorationDrainingCommand,
    BeginSessionDrainingCommand,
    EvaluateCompletionGateCommand,
)
from services.api.app.domain.exploration import (
    ExplorationWork,
    ProbeWork,
    build_exploration_work,
    build_probe_work,
)
from services.api.app.domain.task import CompletionGateDecision, TaskFinalizationSnapshot

START_EXPLORATION_WORKFLOW_NAME = "alioth.v2.orchestrator.start-exploration.v1"
ROUTE_EVIDENCE_REQUEST_WORKFLOW_NAME = "alioth.v2.orchestrator.route-evidence-request.v1"
BEGIN_EXPLORATION_DRAINING_WORKFLOW_NAME = "alioth.v2.orchestrator.begin-exploration-draining.v1"
BEGIN_SESSION_DRAINING_WORKFLOW_NAME = "alioth.v2.orchestrator.begin-session-draining.v1"
EVALUATE_COMPLETION_GATE_WORKFLOW_NAME = "alioth.v2.orchestrator.completion-gate.v1"

@dataclass(frozen=True, slots=True)
class _ExplorationWorkMessage:
    """允许 MAF 安全复制不可变 Exploration Work 的内部消息。"""

    work: ExplorationWork

    def __deepcopy__(self, _memo: dict[int, object]) -> _ExplorationWorkMessage:
        return self

@dataclass(frozen=True, slots=True)
class _ProbeWorkMessage:
    """允许 MAF 安全复制不可变 Probe Work 的内部消息。"""

    work: ProbeWork
    request_lease: EvidenceRequestLeaseFence | None = None

    def __deepcopy__(self, _memo: dict[int, object]) -> _ProbeWorkMessage:
        return self

@dataclass(frozen=True, slots=True)
class _TaskFinalizationMessage:
    """允许 MAF 安全复制不可变任务收口快照。"""

    finalization: TaskFinalizationSnapshot

    def __deepcopy__(self, _memo: dict[int, object]) -> _TaskFinalizationMessage:
        return self

@dataclass(frozen=True, slots=True)
class _CompletionGateDecisionMessage:
    """允许 MAF 安全复制不可变 Completion Gate 结论。"""

    decision: CompletionGateDecision

    def __deepcopy__(self, _memo: dict[int, object]) -> _CompletionGateDecisionMessage:
        return self

def prepare_exploration_work(command: StartExplorationCommand) -> ExplorationWork:
    """以同一构造器准备稳定 Work，但不触发发布副作用。"""
    if not isinstance(command, StartExplorationCommand):
        raise TypeError("command 必须为 StartExplorationCommand")
    return build_exploration_work(
        task_id=command.task_id,
        session_id=command.session_id,
        branch_id=command.branch_id,
        objective=command.objective,
        surface_kind=command.surface_kind,
        source_capture_id=command.source_capture_id,
        frontier_snapshot_id=command.frontier_snapshot_id,
        frontier=command.frontier,
        boundaries=command.boundaries,
        budget=command.budget,
        deadline=command.deadline,
        app_goal=command.app_goal,
    )

class BuildExplorationWorkExecutor(Executor):
    """只根据命令构造稳定的 Exploration Work。"""

    def __init__(self) -> None:
        super().__init__(id="build_exploration_work")

    @handler
    async def build(
        self,
        command: StartExplorationCommand,
        ctx: WorkflowContext[_ExplorationWorkMessage],
    ) -> None:
        work = prepare_exploration_work(command)
        await ctx.send_message(_ExplorationWorkMessage(work))

class PublishExplorationWorkExecutor(Executor):
    """通过持久化发布端口提交 Exploration Work。"""

    def __init__(self, publisher: ExplorationWorkPublisher) -> None:
        super().__init__(id="publish_exploration_work")
        self._publisher = publisher

    @handler
    async def publish(
        self,
        message: _ExplorationWorkMessage,
        ctx: WorkflowContext[Never, _ExplorationWorkMessage],
    ) -> None:
        await self._publisher.publish(message.work)
        await ctx.yield_output(message)

class BuildProbeWorkExecutor(Executor):
    """把 Evidence Request 确定性转换为 Probe Work。"""

    def __init__(self) -> None:
        super().__init__(id="build_probe_work")

    @handler
    async def build(
        self,
        command: RouteEvidenceRequestCommand,
        ctx: WorkflowContext[_ProbeWorkMessage],
    ) -> None:
        work = build_probe_work(
            session_id=command.session_id,
            branch_id=command.branch_id,
            surface_kind=command.surface_kind,
            source_capture_id=command.source_capture_id,
            restoration_plan_hash=command.restoration_plan_hash,
            restoration_plan=command.restoration_plan,
            restoration_attempt_id=command.restoration_attempt_id,
            equivalence_assessment_id=command.equivalence_assessment_id,
            source_grade=command.source_grade,
            evidence_request=command.evidence_request,
            budget=command.budget,
            deadline=command.deadline,
        )
        await ctx.send_message(_ProbeWorkMessage(work, command.request_lease))

class PublishProbeWorkExecutor(Executor):
    """通过持久化发布端口提交 Probe Work。"""

    def __init__(self, publisher: ProbeWorkPublisher) -> None:
        super().__init__(id="publish_probe_work")
        self._publisher = publisher

    @handler
    async def publish(
        self,
        message: _ProbeWorkMessage,
        ctx: WorkflowContext[Never, _ProbeWorkMessage],
    ) -> None:
        if message.request_lease is None:
            await self._publisher.publish(message.work)
        else:
            await self._publisher.publish(
                message.work,
                request_lease=message.request_lease,
            )
        await ctx.yield_output(message)

class BeginExplorationDrainingExecutor(Executor):
    """调用任务收口端口，不作任何 Agent 决策。"""

    def __init__(self, coordinator: TaskFinalizationCoordinator) -> None:
        super().__init__(id="begin_exploration_draining")
        self._coordinator = coordinator

    @handler
    async def begin(
        self,
        command: BeginExplorationDrainingCommand,
        ctx: WorkflowContext[Never, _TaskFinalizationMessage],
    ) -> None:
        result = await self._coordinator.begin_exploration_draining(command)
        await ctx.yield_output(_TaskFinalizationMessage(result))

class BeginSessionDrainingExecutor(Executor):
    """调用任务级 Session Draining 端口。"""

    def __init__(self, coordinator: TaskFinalizationCoordinator) -> None:
        super().__init__(id="begin_session_draining")
        self._coordinator = coordinator

    @handler
    async def begin(
        self,
        command: BeginSessionDrainingCommand,
        ctx: WorkflowContext[Never, _TaskFinalizationMessage],
    ) -> None:
        result = await self._coordinator.begin_session_draining(command)
        await ctx.yield_output(_TaskFinalizationMessage(result))

class EvaluateCompletionGateExecutor(Executor):
    """调用事务完成门禁端口；自然语言或 Redis 状态不参与判定。"""

    def __init__(self, coordinator: TaskFinalizationCoordinator) -> None:
        super().__init__(id="evaluate_completion_gate")
        self._coordinator = coordinator

    @handler
    async def evaluate(
        self,
        command: EvaluateCompletionGateCommand,
        ctx: WorkflowContext[Never, _CompletionGateDecisionMessage],
    ) -> None:
        result = await self._coordinator.evaluate_and_seal(command)
        await ctx.yield_output(_CompletionGateDecisionMessage(result))

def build_start_exploration_workflow(
    publisher: ExplorationWorkPublisher,
) -> Workflow:
    """创建单次调用专用的探索工作流，避免跨请求共享运行状态。"""
    build_executor = BuildExplorationWorkExecutor()
    publish_executor = PublishExplorationWorkExecutor(publisher)
    return (
        WorkflowBuilder(
            start_executor=build_executor,
            output_from=[publish_executor],
            name=START_EXPLORATION_WORKFLOW_NAME,
            description="构造并发布 V2 Exploration Work",
            max_iterations=4,
        )
        .add_edge(build_executor, publish_executor)
        .build()
    )

def build_route_evidence_request_workflow(
    publisher: ProbeWorkPublisher,
) -> Workflow:
    """创建单次调用专用的证据补采路由工作流。"""
    build_executor = BuildProbeWorkExecutor()
    publish_executor = PublishProbeWorkExecutor(publisher)
    return (
        WorkflowBuilder(
            start_executor=build_executor,
            output_from=[publish_executor],
            name=ROUTE_EVIDENCE_REQUEST_WORKFLOW_NAME,
            description="把 Evidence Request 转换并发布为 Probe Work",
            max_iterations=4,
        )
        .add_edge(build_executor, publish_executor)
        .build()
    )

def build_begin_exploration_draining_workflow(
    coordinator: TaskFinalizationCoordinator,
) -> Workflow:
    """创建单次调用专用的 Exploration Draining 工作流。"""
    executor = BeginExplorationDrainingExecutor(coordinator)
    return WorkflowBuilder(
        start_executor=executor,
        output_from=[executor],
        name=BEGIN_EXPLORATION_DRAINING_WORKFLOW_NAME,
        description="停止普通 Frontier 工作并进入 Exploration Draining",
        max_iterations=2,
    ).build()

def build_begin_session_draining_workflow(
    coordinator: TaskFinalizationCoordinator,
) -> Workflow:
    """创建单次调用专用的 Session Draining 工作流。"""
    executor = BeginSessionDrainingExecutor(coordinator)
    return WorkflowBuilder(
        start_executor=executor,
        output_from=[executor],
        name=BEGIN_SESSION_DRAINING_WORKFLOW_NAME,
        description="把任务内 Surface Session 转入排空态",
        max_iterations=2,
    ).build()

def build_evaluate_completion_gate_workflow(
    coordinator: TaskFinalizationCoordinator,
) -> Workflow:
    """创建单次调用专用的事务 Completion Gate 工作流。"""
    executor = EvaluateCompletionGateExecutor(coordinator)
    return WorkflowBuilder(
        start_executor=executor,
        output_from=[executor],
        name=EVALUATE_COMPLETION_GATE_WORKFLOW_NAME,
        description="事务重算八项完成事实并封存任务",
        max_iterations=2,
    ).build()

def _require_single_idle_output[
    MessageT: (_ExplorationWorkMessage, _ProbeWorkMessage),
    WorkT: (ExplorationWork, ProbeWork),
](
    result: WorkflowRunResult,
    expected_message_type: type[MessageT],
    expected_work_type: type[WorkT],
) -> WorkT:
    """只接受正常收敛且仅有一个指定类型输出的工作流结果。"""
    final_state = result.get_final_state()
    if final_state is not WorkflowRunState.IDLE:
        raise OrchestrationWorkflowError(f"编排工作流未正常收敛：{final_state.value}")

    outputs = result.get_outputs()
    if len(outputs) != 1 or not isinstance(outputs[0], expected_message_type):
        raise OrchestrationWorkflowError(
            f"编排工作流输出无效：期望 1 个 {expected_work_type.__name__}，实际 {len(outputs)} 个"
        )
    work = outputs[0].work
    if not isinstance(work, expected_work_type):
        raise OrchestrationWorkflowError(
            f"编排工作流输出类型错误：期望 {expected_work_type.__name__}"
        )
    return work

def require_exploration_work_output(
    result: WorkflowRunResult,
) -> ExplorationWork:
    """只接受正常收敛且唯一的 Exploration Work 输出。"""
    return _require_single_idle_output(
        result,
        _ExplorationWorkMessage,
        ExplorationWork,
    )

def _require_finalization_output(result: WorkflowRunResult) -> TaskFinalizationSnapshot:
    final_state = result.get_final_state()
    if final_state is not WorkflowRunState.IDLE:
        raise OrchestrationWorkflowError(f"编排工作流未正常收敛：{final_state.value}")
    outputs = result.get_outputs()
    if len(outputs) != 1 or not isinstance(outputs[0], _TaskFinalizationMessage):
        raise OrchestrationWorkflowError("编排工作流未产生唯一 TaskFinalizationSnapshot")
    finalization = outputs[0].finalization
    if not isinstance(finalization, TaskFinalizationSnapshot):
        raise OrchestrationWorkflowError("编排工作流返回了无效 TaskFinalizationSnapshot")
    return finalization

def _require_completion_gate_output(result: WorkflowRunResult) -> CompletionGateDecision:
    final_state = result.get_final_state()
    if final_state is not WorkflowRunState.IDLE:
        raise OrchestrationWorkflowError(f"编排工作流未正常收敛：{final_state.value}")
    outputs = result.get_outputs()
    if len(outputs) != 1 or not isinstance(outputs[0], _CompletionGateDecisionMessage):
        raise OrchestrationWorkflowError("编排工作流未产生唯一 CompletionGateDecision")
    decision = outputs[0].decision
    if not isinstance(decision, CompletionGateDecision):
        raise OrchestrationWorkflowError("编排工作流返回了无效 CompletionGateDecision")
    return decision

class MicrosoftAgentFrameworkQcWorkflow:
    """用 MAF Workflow 执行确定性 V2 编排，不创建任何 Agent。"""

    def __init__(
        self,
        *,
        exploration_publisher: ExplorationWorkPublisher | None = None,
        probe_publisher: ProbeWorkPublisher | None = None,
        finalization_coordinator: TaskFinalizationCoordinator | None = None,
    ) -> None:
        self._exploration_publisher = exploration_publisher
        self._probe_publisher = probe_publisher
        self._finalization_coordinator = finalization_coordinator

    async def start_exploration(
        self,
        command: StartExplorationCommand,
    ) -> ExplorationWork:
        publisher = self._require_exploration_publisher()
        workflow = build_start_exploration_workflow(publisher)
        result = await workflow.run(command, include_status_events=True)
        return require_exploration_work_output(result)

    async def prepare_exploration(
        self,
        command: StartExplorationCommand,
    ) -> ExplorationWork:
        """准备稳定后继 Work，由调用方负责在父租约事务内发布。"""
        return prepare_exploration_work(command)

    async def route_evidence_request(
        self,
        command: RouteEvidenceRequestCommand,
    ) -> ProbeWork:
        publisher = self._require_probe_publisher()
        workflow = build_route_evidence_request_workflow(publisher)
        result = await workflow.run(command, include_status_events=True)
        return _require_single_idle_output(
            result,
            _ProbeWorkMessage,
            ProbeWork,
        )

    async def begin_exploration_draining(
        self,
        command: BeginExplorationDrainingCommand,
    ) -> TaskFinalizationSnapshot:
        coordinator = self._require_finalization_coordinator()
        workflow = build_begin_exploration_draining_workflow(coordinator)
        result = await workflow.run(command, include_status_events=True)
        return _require_finalization_output(result)

    async def begin_session_draining(
        self,
        command: BeginSessionDrainingCommand,
    ) -> TaskFinalizationSnapshot:
        coordinator = self._require_finalization_coordinator()
        workflow = build_begin_session_draining_workflow(coordinator)
        result = await workflow.run(command, include_status_events=True)
        return _require_finalization_output(result)

    async def evaluate_completion_gate(
        self,
        command: EvaluateCompletionGateCommand,
    ) -> CompletionGateDecision:
        coordinator = self._require_finalization_coordinator()
        workflow = build_evaluate_completion_gate_workflow(coordinator)
        result = await workflow.run(command, include_status_events=True)
        return _require_completion_gate_output(result)

    def _require_finalization_coordinator(self) -> TaskFinalizationCoordinator:
        if self._finalization_coordinator is None:
            raise OrchestrationWorkflowError("Orchestrator 尚未配置 Task Finalization 端口")
        return self._finalization_coordinator

    def _require_exploration_publisher(self) -> ExplorationWorkPublisher:
        if self._exploration_publisher is None:
            raise OrchestrationWorkflowError("Orchestrator 尚未配置 Exploration Work 发布端口")
        return self._exploration_publisher

    def _require_probe_publisher(self) -> ProbeWorkPublisher:
        if self._probe_publisher is None:
            raise OrchestrationWorkflowError("Orchestrator 尚未配置 Probe Work 发布端口")
        return self._probe_publisher
