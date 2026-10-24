"""工作台人工审核接口。"""

from functools import lru_cache
from typing import Annotated, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from services.api.app.api.v1.auth import require_access_scope
from services.api.app.api.v1.object_access import require_review_access
from services.api.app.application.workstation_reviews import (
    InvalidReviewActionError,
    ReviewItemNotFoundError,
    WorkstationReviewListFilter,
    WorkstationReviewService,
)
from services.api.app.core.errors import AliothError
from services.api.app.domain.iam.access_scope import AccessScope
from services.api.app.infrastructure.db.workstation_review_repository import (
    PostgresWorkstationReviewRepository,
)

router = APIRouter(prefix="/reviews")

ReviewStatus = Literal["pending", "approved", "rejected", "needs_recheck"]
ReviewTriggerReason = Literal[
    "low_confidence",
    "high_risk",
    "evidence_insufficient",
    "loop_budget_exceeded",
    "no_progress",
    "manual_flag",
]
ReviewAction = Literal["approve", "reject", "recheck"]


class WorkstationReviewListResponse(BaseModel):
    """人工审核队列响应。"""

    items: list[dict]
    total: int
    page: int
    page_size: int


class WorkstationReviewActionRequest(BaseModel):
    """人工审核动作请求。"""

    reason: str | None = None


class WorkstationReviewActionResponse(BaseModel):
    """人工审核动作响应。"""

    review_id: str
    status: str
    message: str


@lru_cache
def _get_workstation_review_service() -> WorkstationReviewService:
    return WorkstationReviewService(repository=PostgresWorkstationReviewRepository())


@router.get("", response_model=WorkstationReviewListResponse, summary="查询工作台人工审核队列")
async def list_workstation_reviews(
    service: Annotated[WorkstationReviewService, Depends(_get_workstation_review_service)],
    access_scope: Annotated[AccessScope, Depends(require_access_scope)],
    status: ReviewStatus | None = None,
    trigger_reason: ReviewTriggerReason | None = None,
    page: int = 1,
    page_size: int = 20,
) -> WorkstationReviewListResponse:
    safe_page = max(page, 1)
    safe_page_size = min(max(page_size, 1), 100)
    return WorkstationReviewListResponse(
        **await service.list_reviews(
            WorkstationReviewListFilter(
                status=status,
                trigger_reason=trigger_reason,
                page=safe_page,
                page_size=safe_page_size,
            ),
            access_scope=access_scope,
        )
    )


@router.post(
    "/{review_id}/{action}",
    response_model=WorkstationReviewActionResponse,
    summary="处理工作台人工审核项",
    dependencies=[Depends(require_review_access)],
)
async def perform_workstation_review_action(
    review_id: str,
    action: ReviewAction,
    payload: WorkstationReviewActionRequest,
    service: Annotated[WorkstationReviewService, Depends(_get_workstation_review_service)],
    access_scope: Annotated[AccessScope, Depends(require_access_scope)],
) -> WorkstationReviewActionResponse:
    try:
        result = await service.perform_action(
            review_id=review_id,
            action=action,
            reason=payload.reason,
            access_scope=access_scope,
        )
    except ReviewItemNotFoundError as exc:
        raise AliothError(message=str(exc), status_code=404) from exc
    except InvalidReviewActionError as exc:
        raise AliothError(message=str(exc), status_code=409) from exc
    return WorkstationReviewActionResponse(**result)
