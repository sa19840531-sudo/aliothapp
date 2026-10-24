"""Action Execution 持久化状态机的应用端口。"""

from __future__ import annotations

from typing import Protocol

from services.api.app.application.surface_sessions import SessionCommandPermit
from services.api.app.domain.interaction import (
    ActionExecutionSnapshot,
    ActionExecutionStatus,
    ActionIntent,
    InteractionResult,
)

APP_WORKFLOW_CANCELED_ERROR_CODE = "APP_WORKFLOW_CANCELED"
APP_WORKFLOW_CANCELED_ERROR_MESSAGE = (
    "App 任务已取消，未继续采集 After；已派发动作结果可能未知，未派发动作不视为已执行"
)


class ActionExecutionRepository(Protocol):
    """动作账本的幂等创建、状态推进与完成门禁查询。"""

    async def plan(
        self,
        *,
        intent: ActionIntent,
        permit: SessionCommandPermit,
    ) -> ActionExecutionSnapshot: ...

    async def find_intent_for_work(
        self,
        *,
        task_id: str,
        session_id: str,
        exploration_work_id: str,
    ) -> ActionIntent | None:
        """读取已经绑定当前 App Work 的动作意图，恢复时不得重新询问模型。"""

    async def recover(
        self,
        *,
        action_execution_id: str,
        permit: SessionCommandPermit,
        expected_row_version: int,
    ) -> ActionExecutionSnapshot:
        """在新 Session 命令租约下恢复或阻断旧动作，绝不再次执行设备动作。"""

    async def resume_planned_with_capture(
        self,
        *,
        action_execution_id: str,
        permit: SessionCommandPermit,
        source_capture_id: str,
        expected_row_version: int,
    ) -> ActionExecutionSnapshot:
        """复用已提交的 Before Capture，在当前命令租约下原子进入 executing。"""

    async def mark_executing(
        self,
        *,
        action_execution_id: str,
        permit: SessionCommandPermit,
        source_capture_id: str,
        expected_row_version: int,
    ) -> ActionExecutionSnapshot: ...

    async def record_interaction(
        self,
        *,
        interaction: InteractionResult,
        permit: SessionCommandPermit,
        expected_status: ActionExecutionStatus,
        expected_row_version: int,
    ) -> ActionExecutionSnapshot: ...

    async def complete_evidence(
        self,
        *,
        interaction: InteractionResult,
        permit: SessionCommandPermit,
        expected_row_version: int,
    ) -> ActionExecutionSnapshot: ...

    async def attach_terminal_evidence(
        self,
        *,
        interaction: InteractionResult,
        permit: SessionCommandPermit,
        expected_row_version: int,
    ) -> ActionExecutionSnapshot: ...

    async def fail_before_action(
        self,
        *,
        action_execution_id: str,
        permit: SessionCommandPermit,
        expected_row_version: int,
        error_code: str,
        error_message: str,
    ) -> ActionExecutionSnapshot: ...

    async def fail_evidence(
        self,
        *,
        interaction: InteractionResult,
        permit: SessionCommandPermit,
        expected_row_version: int,
    ) -> ActionExecutionSnapshot: ...

    async def fail_nonterminal_for_task(
        self,
        *,
        task_id: str,
        error_code: str,
        error_message: str,
        h5_only: bool = False,
    ) -> int: ...

    async def get(self, *, action_execution_id: str) -> ActionExecutionSnapshot | None: ...

    async def has_nonterminal(self, *, task_id: str, session_id: str | None = None) -> bool: ...

    async def has_pending_capture_commit(
        self,
        *,
        task_id: str,
        session_id: str | None = None,
    ) -> bool: ...
