"""H5 完全技术失败沿用任务最终化命令，保留失败门禁审计且不生成业务报告。"""

from services.api.app.application.task_finalization import (
    BeginExplorationDrainingCommand,
    BeginSessionDrainingCommand,
    TaskFinalizationService,
    TerminateTaskFinalizationCommand,
)
from services.api.app.domain.task import TaskExecutionStatus, TaskFinalizationState


async def terminate_failed_h5_task(
    service: TaskFinalizationService,
    *,
    task_id: str,
    agent_run_id: str,
    reason_code: str,
) -> None:
    """只推进当前 Run 拥有的 Draining，不把失败伪装为完成门禁放行。"""
    snapshot = await service.get(task_id=task_id)
    if snapshot is None or snapshot.agent_run_id != agent_run_id:
        raise RuntimeError("H5 失败收口的 Task/Run 所有权不一致")
    if snapshot.state is TaskFinalizationState.SEALED:
        if snapshot.execution_status is not TaskExecutionStatus.FAILED:
            raise RuntimeError("H5 已封存为其他结论，不能改写为失败")
        return
    if snapshot.state is TaskFinalizationState.OPEN:
        snapshot = await service.begin_exploration_draining(
            BeginExplorationDrainingCommand(
                task_id=task_id,
                agent_run_id=agent_run_id,
                expected_row_version=snapshot.row_version,
            )
        )
    if snapshot.state is TaskFinalizationState.EXPLORATION_DRAINING:
        snapshot = await service.begin_session_draining(
            BeginSessionDrainingCommand(
                task_id=task_id,
                agent_run_id=agent_run_id,
                expected_finalization_epoch=snapshot.finalization_epoch,
                expected_row_version=snapshot.row_version,
            )
        )
    if snapshot.state is not TaskFinalizationState.SESSION_DRAINING:
        raise RuntimeError("H5 失败收口尚未到达 Session Draining")
    await service.terminate(
        TerminateTaskFinalizationCommand(
            task_id=task_id,
            agent_run_id=agent_run_id,
            expected_finalization_epoch=snapshot.finalization_epoch,
            expected_row_version=snapshot.row_version,
            execution_status=TaskExecutionStatus.FAILED,
            reason_code=reason_code,
        )
    )
