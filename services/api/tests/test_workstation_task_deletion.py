"""管理端删除任务的服务和 API 权限、冲突及不存在合同。"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest
from fastapi.testclient import TestClient

from services.api.app.api.v1.auth import require_access_scope
from services.api.app.api.v1.workstation import tasks as tasks_api
from services.api.app.application.workstation_task_deletion import (
    TaskDeletionForbiddenError,
    WorkstationTaskDeletionService,
)
from services.api.app.application.workstation_tasks import (
    InvalidTaskOperationError,
    TaskNotFoundError,
    WorkstationTaskOperationResult,
)
from services.api.app.domain.iam.access_scope import AccessScope
from services.api.app.main import create_app

ADMIN_SCOPE = AccessScope(
    actor_user_id="00000000-0000-0000-0000-000000000001",
    tenant_id="00000000-0000-0000-0000-000000000002",
    permissions=frozenset({"taskObjects:read:any", "taskObjects:write:any"}),
)


@dataclass
class DeletionRepository:
    result: WorkstationTaskOperationResult | None = None
    error: Exception | None = None
    calls: list[tuple[str, AccessScope]] = field(default_factory=list)

    async def delete_task(self, task_id: str, *, access_scope: AccessScope):
        self.calls.append((task_id, access_scope))
        if self.error is not None:
            raise self.error
        return self.result


def _result(status: str) -> WorkstationTaskOperationResult:
    return WorkstationTaskOperationResult(
        task_id="task-1", status=status, run_id=None, message="任务已删除"
    )


@pytest.mark.parametrize("status", ["done", "review", "failed", "canceled"])
async def test_platform_writer_can_delete_terminal_task(status):
    repository = DeletionRepository(result=_result(status))
    result = await WorkstationTaskDeletionService(repository).delete_task(
        "task-1", access_scope=ADMIN_SCOPE
    )
    assert result.status == status
    assert repository.calls == [("task-1", ADMIN_SCOPE)]


@pytest.mark.parametrize(
    "permissions",
    [
        {"taskObjects:read:tenant"},
        {"taskObjects:read:department"},
        {"taskObjects:read:any"},
        set(),
    ],
)
async def test_non_platform_writer_is_rejected_before_repository_access(permissions):
    scope = AccessScope(
        actor_user_id=ADMIN_SCOPE.actor_user_id,
        tenant_id=ADMIN_SCOPE.tenant_id,
        permissions=frozenset(permissions),
    )
    repository = DeletionRepository(result=_result("done"))
    with pytest.raises(TaskDeletionForbiddenError):
        await WorkstationTaskDeletionService(repository).delete_task("task-1", access_scope=scope)
    assert repository.calls == []


@pytest.mark.parametrize("status", ["queued", "running", "canceling"])
async def test_service_refuses_nonterminal_repository_result(status):
    with pytest.raises(InvalidTaskOperationError):
        await WorkstationTaskDeletionService(
            DeletionRepository(result=_result(status))
        ).delete_task("task-1", access_scope=ADMIN_SCOPE)


async def test_service_uses_same_not_found_contract_for_missing_and_deleted_tasks():
    service = WorkstationTaskDeletionService(DeletionRepository())
    for task_id in ("missing", "already-deleted"):
        with pytest.raises(TaskNotFoundError, match="任务不存在或已删除"):
            await service.delete_task(task_id, access_scope=ADMIN_SCOPE)


def _client(repository: DeletionRepository, scope: AccessScope = ADMIN_SCOPE) -> TestClient:
    app = create_app()
    app.dependency_overrides[require_access_scope] = lambda: scope
    app.dependency_overrides[tasks_api._get_workstation_task_deletion_service] = lambda: (
        WorkstationTaskDeletionService(repository)
    )
    return TestClient(app)


def test_delete_api_returns_operation_result():
    response = _client(DeletionRepository(result=_result("done"))).delete(
        "/api/v1/workstation/tasks/task-1"
    )
    assert response.status_code == 200, response.text
    assert response.json() == {
        "task_id": "task-1",
        "status": "done",
        "run_id": None,
        "message": "任务已删除",
    }


def test_delete_api_non_platform_role_returns_403_without_repository_access():
    scope = AccessScope(
        actor_user_id=ADMIN_SCOPE.actor_user_id,
        tenant_id=ADMIN_SCOPE.tenant_id,
        permissions=frozenset({"taskObjects:read:tenant"}),
    )
    repository = DeletionRepository(result=_result("done"))
    response = _client(repository, scope).delete("/api/v1/workstation/tasks/task-1")
    assert response.status_code == 403
    assert response.json()["error"]["message"] == "仅平台管理员可删除任务"
    assert repository.calls == []


def test_delete_api_missing_and_repeated_delete_are_both_404():
    repository = DeletionRepository()
    client = _client(repository)
    for task_id in ("missing", "already-deleted"):
        response = client.delete(f"/api/v1/workstation/tasks/{task_id}")
        assert response.status_code == 404
        assert response.json()["error"]["message"] == "任务不存在或已删除"


@pytest.mark.parametrize(
    "message",
    ["排队、执行或取消中的任务不能删除", "任务仍有执行中的运行记录，暂不能删除"],
)
def test_delete_api_reports_task_state_conflicts(message):
    repository = DeletionRepository(error=InvalidTaskOperationError(message))
    response = _client(repository).delete("/api/v1/workstation/tasks/task-1")
    assert response.status_code == 409
    assert response.json()["error"]["message"] == message
