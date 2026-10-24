from dataclasses import dataclass

import pytest
from fastapi.testclient import TestClient

from services.api.app.api.v1 import object_access as object_access_module
from services.api.app.api.v1.workstation import reviews as workstation_reviews_module
from services.api.app.application.workstation_reviews import WorkstationReviewListFilter
from services.api.app.domain.iam.access_scope import AccessScope
from services.api.app.main import create_app


@dataclass
class FakeWorkstationReviewService:
    list_filter: WorkstationReviewListFilter | None = None
    list_scope: AccessScope | None = None
    action_calls: list[tuple[str, str, str | None, AccessScope]] | None = None

    async def list_reviews(
        self, filters: WorkstationReviewListFilter, *, access_scope: AccessScope
    ) -> dict:
        self.list_filter = filters
        self.list_scope = access_scope
        return {
            "items": [
                {
                    "review_id": "review-1",
                    "execution_id": "execution-1",
                    "task_id": "task-1",
                    "task_name": "首页改版回归测试",
                    "rule_name": "核心文案对比度检查",
                    "trigger_reason": "low_confidence",
                    "status": "pending",
                    "reviewer": None,
                    "review_reason": None,
                    "created_at": "2026-07-10T09:30:00+00:00",
                }
            ],
            "total": 1,
            "page": filters.page,
            "page_size": filters.page_size,
        }

    async def perform_action(
        self,
        *,
        review_id: str,
        action: str,
        reason: str | None,
        access_scope: AccessScope,
    ) -> dict:
        if self.action_calls is None:
            self.action_calls = []
        self.action_calls.append((review_id, action, reason, access_scope))
        return {"review_id": review_id, "status": "approved", "message": "审核已更新"}


@pytest.fixture
def client() -> tuple[TestClient, FakeWorkstationReviewService]:
    service = FakeWorkstationReviewService()
    app = create_app()
    app.dependency_overrides[workstation_reviews_module._get_workstation_review_service] = (
        lambda: service
    )
    return TestClient(app), service


def test_list_workstation_reviews_uses_filters(
    client: tuple[TestClient, FakeWorkstationReviewService],
) -> None:
    test_client, service = client

    response = test_client.get(
        "/api/v1/workstation/reviews?status=pending&trigger_reason=low_confidence&page=2&page_size=10"
    )

    assert response.status_code == 200
    body = response.json()
    assert body["items"][0]["review_id"] == "review-1"
    assert body["total"] == 1
    assert service.list_filter == WorkstationReviewListFilter(
        status="pending",
        trigger_reason="low_confidence",
        page=2,
        page_size=10,
    )
    assert service.list_scope is not None
    assert service.list_scope.actor_user_id == "test-user"
    assert service.list_scope.tenant_id == "tenant-1"


def test_review_action_endpoint_calls_service(
    client: tuple[TestClient, FakeWorkstationReviewService],
) -> None:
    test_client, service = client

    response = test_client.post(
        "/api/v1/workstation/reviews/review-1/approve",
        json={"reason": "确认原结论"},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "approved"
    assert service.action_calls is not None
    assert service.action_calls[0][:3] == ("review-1", "approve", "确认原结论")
    assert service.action_calls[0][3].actor_user_id == "test-user"


def test_review_action_returns_404_when_object_access_denied(
    client: tuple[TestClient, FakeWorkstationReviewService],
) -> None:
    test_client, service = client

    class _DenyReviewAccess:
        async def can_access_review(self, **_kwargs) -> bool:
            return False

    app = test_client.app
    app.dependency_overrides[object_access_module._get_object_access_repository] = (
        _DenyReviewAccess
    )

    response = test_client.post(
        "/api/v1/workstation/reviews/review-foreign/approve",
        json={"reason": "不应被调用"},
    )

    assert response.status_code == 404
    assert service.action_calls is None
