"""创建任务草稿接口（原型「保存草稿」）：草稿只对提交它的账号可见。"""

from __future__ import annotations

from datetime import datetime
from functools import lru_cache
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from services.api.app.api.v1.auth import require_access_scope
from services.api.app.core.errors import AliothError
from services.api.app.domain.iam.access_scope import AccessScope
from services.api.app.infrastructure.db.task_draft_repository import PostgresTaskDraftRepository

router = APIRouter(prefix="/task-drafts")


class TaskDraftPayload(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    target_platform: Literal["h5", "web", "app"] = "h5"
    """创建任务表单原样存下来，恢复时由前端解释；服务端不校验业务字段。"""
    payload: dict[str, Any] = Field(default_factory=dict)


class TaskDraftResponse(BaseModel):
    id: str
    name: str
    target_platform: str
    payload: dict[str, Any]
    created_at: datetime
    updated_at: datetime


class TaskDraftListResponse(BaseModel):
    items: list[TaskDraftResponse]


@lru_cache
def _get_task_draft_repository() -> PostgresTaskDraftRepository:
    return PostgresTaskDraftRepository()


def _to_response(row: dict[str, Any]) -> TaskDraftResponse:
    return TaskDraftResponse(
        id=str(row["id"]),
        name=str(row["name"]),
        target_platform=str(row["target_platform"]),
        payload=dict(row.get("payload") or {}),
        created_at=row["created_at"],  # type: ignore[arg-type]
        updated_at=row["updated_at"],  # type: ignore[arg-type]
    )


@router.get("", response_model=TaskDraftListResponse, summary="我的任务草稿")
async def list_task_drafts(
    scope: Annotated[AccessScope, Depends(require_access_scope)],
    repository: Annotated[PostgresTaskDraftRepository, Depends(_get_task_draft_repository)],
) -> TaskDraftListResponse:
    rows = await repository.list_drafts(owner_id=scope.actor_user_id)
    return TaskDraftListResponse(items=[_to_response(row) for row in rows])


@router.post("", response_model=TaskDraftResponse, status_code=201, summary="保存任务草稿")
async def create_task_draft(
    payload: TaskDraftPayload,
    scope: Annotated[AccessScope, Depends(require_access_scope)],
    repository: Annotated[PostgresTaskDraftRepository, Depends(_get_task_draft_repository)],
) -> TaskDraftResponse:
    row = await repository.create_draft(
        {
            "tenant_id": scope.tenant_id,
            "owner_id": scope.actor_user_id,
            "name": payload.name.strip(),
            "target_platform": payload.target_platform,
            "payload": payload.payload,
        }
    )
    return _to_response(row)


@router.put("/{draft_id}", response_model=TaskDraftResponse, summary="更新任务草稿")
async def update_task_draft(
    draft_id: str,
    payload: TaskDraftPayload,
    scope: Annotated[AccessScope, Depends(require_access_scope)],
    repository: Annotated[PostgresTaskDraftRepository, Depends(_get_task_draft_repository)],
) -> TaskDraftResponse:
    row = await repository.update_draft(
        draft_id,
        owner_id=scope.actor_user_id,
        payload={
            "name": payload.name.strip(),
            "target_platform": payload.target_platform,
            "payload": payload.payload,
        },
    )
    if row is None:
        raise AliothError(message="草稿不存在或无权修改", status_code=404)
    return _to_response(row)


@router.delete("/{draft_id}", status_code=204, response_model=None, summary="删除任务草稿")
async def delete_task_draft(
    draft_id: str,
    scope: Annotated[AccessScope, Depends(require_access_scope)],
    repository: Annotated[PostgresTaskDraftRepository, Depends(_get_task_draft_repository)],
) -> None:
    deleted = await repository.delete_draft(draft_id, owner_id=scope.actor_user_id)
    if not deleted:
        raise AliothError(message="草稿不存在或无权删除", status_code=404)
