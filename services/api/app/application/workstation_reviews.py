"""工作台人工审核用例。"""

from dataclasses import dataclass
from typing import Protocol

from services.api.app.domain.iam.access_scope import AccessScope


@dataclass(frozen=True)
class WorkstationReviewListFilter:
    """人工审核队列筛选条件。"""

    status: str | None = None
    trigger_reason: str | None = None
    page: int = 1
    page_size: int = 20


class WorkstationReviewRepository(Protocol):
    """人工审核仓储契约。"""

    async def list_reviews(
        self, filters: WorkstationReviewListFilter, *, access_scope: AccessScope
    ) -> dict:
        """查询人工审核队列。"""

    async def update_review_status(
        self,
        *,
        review_id: str,
        status: str,
        reason: str | None,
        access_scope: AccessScope,
    ) -> dict | None:
        """更新审核状态。"""


class WorkstationReviewService:
    """工作台人工审核服务。"""

    def __init__(self, *, repository: WorkstationReviewRepository) -> None:
        self._repository = repository

    async def list_reviews(
        self, filters: WorkstationReviewListFilter, *, access_scope: AccessScope
    ) -> dict:
        return await self._repository.list_reviews(filters, access_scope=access_scope)

    async def perform_action(
        self,
        *,
        review_id: str,
        action: str,
        reason: str | None,
        access_scope: AccessScope,
    ) -> dict:
        status = {
            "approve": "approved",
            "reject": "rejected",
            "recheck": "needs_recheck",
        }.get(action)
        if status is None:
            raise InvalidReviewActionError(f"不支持的审核操作：{action}")
        result = await self._repository.update_review_status(
            review_id=review_id,
            status=status,
            reason=reason,
            access_scope=access_scope,
        )
        if result is None:
            raise ReviewItemNotFoundError(f"未找到审核项：{review_id}")
        return result


class ReviewItemNotFoundError(ValueError):
    """审核项不存在。"""


class InvalidReviewActionError(ValueError):
    """审核动作非法。"""
