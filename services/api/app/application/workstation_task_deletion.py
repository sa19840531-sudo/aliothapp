"""管理端任务删除用例：隐藏已结束任务并保留质检材料。"""

from typing import Protocol

from services.api.app.application.workstation_tasks import (
    InvalidTaskOperationError,
    TaskNotFoundError,
    WorkstationTaskOperationResult,
)
from services.api.app.domain.iam.access_scope import AccessScope

DELETABLE_TASK_STATUSES = frozenset({"done", "review", "failed", "canceled"})


class TaskDeletionForbiddenError(Exception):
    """当前主体无管理端任务删除权限。"""


class WorkstationTaskDeletionRepository(Protocol):
    """删除操作必须在同一事务内检查状态、隐藏任务并写审计。"""

    async def delete_task(
        self, task_id: str, *, access_scope: AccessScope
    ) -> WorkstationTaskOperationResult | None:
        """隐藏已结束且没有活跃运行的任务。"""


class WorkstationTaskDeletionService:
    """复用平台对象写权限，只提供管理端删除能力。"""

    def __init__(self, repository: WorkstationTaskDeletionRepository) -> None:
        self._repository = repository

    async def delete_task(
        self, task_id: str, *, access_scope: AccessScope
    ) -> WorkstationTaskOperationResult:
        if not access_scope.can_write_any:
            raise TaskDeletionForbiddenError("仅平台管理员可删除任务")
        result = await self._repository.delete_task(task_id, access_scope=access_scope)
        if result is None:
            raise TaskNotFoundError("任务不存在或已删除")
        if result.status not in DELETABLE_TASK_STATUSES:
            raise InvalidTaskOperationError("仅可删除已结束的任务")
        return result
