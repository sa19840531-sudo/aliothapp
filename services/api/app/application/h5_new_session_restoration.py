"""H5 新会话启动后仍复用公共逐边恢复和状态等价门禁。"""

from dataclasses import replace
from typing import Any

from services.api.app.application.restoration import (
    RestorationExecutionError,
    RestorationExecutionRequest,
    RestorationExecutionResult,
)
from services.api.app.domain.inspection import RestorationStrategy


class H5NewSessionRestorationExecutor:
    """空路线只适用于入口本身；是否满足目标仍由新 Capture 等价门判断。"""

    def __init__(
        self,
        *,
        delegate: Any,
        runtime: Any,
        snapshots: Any,
        agent_run_id: str,
        finalization_epoch: int,
    ) -> None:
        self._delegate = delegate
        self._runtime = runtime
        self._snapshots = snapshots
        self._agent_run_id = agent_run_id
        self._epoch = finalization_epoch

    async def restore(self, request: RestorationExecutionRequest) -> RestorationExecutionResult:
        plan = request.plan
        if plan.strategy is not RestorationStrategy.NEW_SESSION_REPLAY:
            return await self._delegate.restore(request)
        try:
            capture, action_id = await self._runtime.prepare_new_session(
                plan=plan,
                agent_run_id=self._agent_run_id,
                finalization_epoch=self._epoch,
            )
        except Exception as error:
            raise RestorationExecutionError(
                error_code=getattr(error, "reason_code", None)
                or getattr(error, "error_code", None)
                or "H5_NEW_SESSION_BOOTSTRAP_FAILED",
                message="H5 新会话无法建立已验证的入口现场",
                unfulfillable=getattr(error, "retryable", True) is False,
            ) from error
        bootstrap_actions = (action_id,) if isinstance(action_id, str) else tuple(action_id)
        if plan.normalization_step_ids:
            restored = await self._delegate.restore(request)
            return replace(
                restored, action_execution_ids=(*bootstrap_actions, *restored.action_execution_ids)
            )
        snapshot = await self._snapshots.read(
            task_id=plan.task_id,
            session_id=plan.target_session_id,
            surface_kind=plan.surface_kind,
            capture_id=capture.capture_id,
        )
        return RestorationExecutionResult(
            restored_snapshot=snapshot,
            action_execution_ids=bootstrap_actions,
        )
