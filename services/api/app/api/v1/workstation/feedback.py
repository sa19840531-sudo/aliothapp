"""工作台反馈工单接口（我的反馈 / 反馈中心）。

可见范围跟任务对象一致：总部看全部公司，公司管理员/审计员看本公司，其他账号
只看自己提交的；处理（改状态、回复）需要本公司或跨公司的审核权限。
"""

from __future__ import annotations

from datetime import datetime
from functools import lru_cache
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from services.api.app.api.v1.auth import AuthUserResponse, require_access_scope, require_auth_user
from services.api.app.core.errors import AliothError
from services.api.app.domain.iam.access_scope import AccessScope
from services.api.app.infrastructure.db.feedback_repository import PostgresFeedbackRepository

router = APIRouter(prefix="/feedback")

FeedbackKind = Literal["false_positive", "missed", "rule_suggestion", "platform_issue", "other"]
FeedbackPriority = Literal["urgent", "high", "medium", "low"]
FeedbackStatus = Literal["pending", "processing", "resolved", "closed"]


class FeedbackCreatePayload(BaseModel):
    kind: FeedbackKind
    priority: FeedbackPriority = "medium"
    title: str = Field(..., min_length=1, max_length=200)
    description: str = Field(..., min_length=1, max_length=4000)
    task_id: str | None = Field(default=None, max_length=64)
    rule_code: str | None = Field(default=None, max_length=64)
    contact: str | None = Field(default=None, max_length=128)


class FeedbackHandlePayload(BaseModel):
    status: FeedbackStatus
    reply: str | None = Field(default=None, max_length=4000)


class FeedbackTicketResponse(BaseModel):
    id: str
    ticket_no: str
    tenant_id: str
    tenant_name: str | None = None
    submitter_id: str
    submitter_name: str
    kind: str
    priority: str
    title: str
    description: str
    task_id: str | None = None
    rule_code: str | None = None
    contact: str | None = None
    status: str
    reply: str | None = None
    handled_by: str | None = None
    handled_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class FeedbackListResponse(BaseModel):
    items: list[FeedbackTicketResponse]
    total: int
    page: int
    page_size: int
    """当前账号能否处理工单（改状态/回复）。"""
    can_handle: bool
    """当前账号的可见范围：all / tenant / own。"""
    visibility: str


@lru_cache
def _get_feedback_repository() -> PostgresFeedbackRepository:
    return PostgresFeedbackRepository()


def _visibility(scope: AccessScope) -> str:
    if scope.can_read_any:
        return "all"
    if scope.can_read_tenant:
        return "tenant"
    return "own"


def _can_handle(scope: AccessScope, *, tenant_id: str | None = None) -> bool:
    if "taskObjects:review:any" in scope.permissions:
        return True
    if not scope.can_review_tenant:
        return False
    return tenant_id is None or tenant_id == scope.tenant_id


def _to_response(row: dict[str, object]) -> FeedbackTicketResponse:
    return FeedbackTicketResponse(
        id=str(row["id"]),
        ticket_no=str(row["ticket_no"]),
        tenant_id=str(row["tenant_id"]),
        tenant_name=(str(row["tenant_name"]) if row.get("tenant_name") else None),
        submitter_id=str(row["submitter_id"]),
        submitter_name=str(row["submitter_name"]),
        kind=str(row["kind"]),
        priority=str(row["priority"]),
        title=str(row["title"]),
        description=str(row["description"]),
        task_id=(str(row["task_id"]) if row.get("task_id") else None),
        rule_code=(str(row["rule_code"]) if row.get("rule_code") else None),
        contact=(str(row["contact"]) if row.get("contact") else None),
        status=str(row["status"]),
        reply=(str(row["reply"]) if row.get("reply") else None),
        handled_by=(str(row["handled_by"]) if row.get("handled_by") else None),
        handled_at=row.get("handled_at"),  # type: ignore[arg-type]
        created_at=row["created_at"],  # type: ignore[arg-type]
        updated_at=row["updated_at"],  # type: ignore[arg-type]
    )


def _ensure_visible(row: dict[str, object], scope: AccessScope) -> None:
    if not scope.can_read_resource(
        owner_id=str(row["submitter_id"]),
        tenant_id=str(row["tenant_id"]),
    ):
        raise AliothError(message="反馈不存在或无权查看", status_code=404)


@router.post("", response_model=FeedbackTicketResponse, summary="提交反馈", status_code=201)
async def create_feedback(
    payload: FeedbackCreatePayload,
    current_user: Annotated[AuthUserResponse, Depends(require_auth_user)],
    scope: Annotated[AccessScope, Depends(require_access_scope)],
    repository: Annotated[PostgresFeedbackRepository, Depends(_get_feedback_repository)],
) -> FeedbackTicketResponse:
    row = await repository.create_ticket(
        {
            "tenant_id": scope.tenant_id,
            "submitter_id": scope.actor_user_id,
            "submitter_name": current_user.full_name or current_user.username,
            "kind": payload.kind,
            "priority": payload.priority,
            "title": payload.title.strip(),
            "description": payload.description.strip(),
            "task_id": (payload.task_id or "").strip() or None,
            "rule_code": (payload.rule_code or "").strip() or None,
            "contact": (payload.contact or "").strip() or None,
        }
    )
    return _to_response(row)


@router.get("", response_model=FeedbackListResponse, summary="查询反馈列表")
async def list_feedback(
    scope: Annotated[AccessScope, Depends(require_access_scope)],
    repository: Annotated[PostgresFeedbackRepository, Depends(_get_feedback_repository)],
    status: Annotated[FeedbackStatus | None, Query()] = None,
    kind: Annotated[FeedbackKind | None, Query()] = None,
    keyword: Annotated[str | None, Query(max_length=200)] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
) -> FeedbackListResponse:
    visibility = _visibility(scope)
    rows, total = await repository.list_tickets(
        tenant_id=None if visibility == "all" else scope.tenant_id,
        submitter_id=scope.actor_user_id if visibility == "own" else None,
        status=status,
        kind=kind,
        keyword=keyword,
        page=page,
        page_size=page_size,
    )
    return FeedbackListResponse(
        items=[_to_response(row) for row in rows],
        total=total,
        page=page,
        page_size=page_size,
        can_handle=_can_handle(scope),
        visibility=visibility,
    )


@router.get("/{ticket_id}", response_model=FeedbackTicketResponse, summary="查看反馈")
async def get_feedback(
    ticket_id: str,
    scope: Annotated[AccessScope, Depends(require_access_scope)],
    repository: Annotated[PostgresFeedbackRepository, Depends(_get_feedback_repository)],
) -> FeedbackTicketResponse:
    row = await repository.find_by_id(ticket_id)
    if row is None:
        raise AliothError(message="反馈不存在或无权查看", status_code=404)
    _ensure_visible(row, scope)
    return _to_response(row)


@router.patch("/{ticket_id}", response_model=FeedbackTicketResponse, summary="处理反馈")
async def handle_feedback(
    ticket_id: str,
    payload: FeedbackHandlePayload,
    current_user: Annotated[AuthUserResponse, Depends(require_auth_user)],
    scope: Annotated[AccessScope, Depends(require_access_scope)],
    repository: Annotated[PostgresFeedbackRepository, Depends(_get_feedback_repository)],
) -> FeedbackTicketResponse:
    row = await repository.find_by_id(ticket_id)
    if row is None:
        raise AliothError(message="反馈不存在或无权查看", status_code=404)
    _ensure_visible(row, scope)
    if not _can_handle(scope, tenant_id=str(row["tenant_id"])):
        raise AliothError(message="无权处理该反馈", status_code=403)
    updated = await repository.update_ticket(
        ticket_id,
        status=payload.status,
        reply=(payload.reply or "").strip() or None,
        handled_by=current_user.full_name or current_user.username,
    )
    if updated is None:
        raise AliothError(message="反馈不存在或无权查看", status_code=404)
    return _to_response(updated)
