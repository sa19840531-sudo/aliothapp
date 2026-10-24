"""下载票据兑现时的撤权复验：票据未过期不等于此刻仍有权取走证据。"""

from typing import Any

import pytest
from fastapi.testclient import TestClient

from services.api.app.api.v1 import evidence as evidence_module
from services.api.app.api.v1 import object_access as object_access_module
from services.api.app.main import create_app
from services.api.tests.conftest import (
    TEST_AUTH_USER,
    pop_test_object_access,
    pop_test_ticket_subject,
)

ARTIFACT_ID = "art-1"


class FakeInsightService:
    async def evidence_file(self, artifact_id: str) -> dict[str, Any]:
        return {
            "artifact_id": artifact_id,
            "artifact_type": "screenshot",
            "content_type": "image/png",
            "content": b"png-bytes",
        }


class StubTicketSubjectRepository:
    """可改写当前用户事实的主体仓储桩。"""

    def __init__(self, **overrides: Any) -> None:
        self.row: dict[str, Any] | None = {
            "id": TEST_AUTH_USER.id,
            "username": TEST_AUTH_USER.username,
            "full_name": TEST_AUTH_USER.full_name,
            "role": TEST_AUTH_USER.role,
            "status": TEST_AUTH_USER.status,
            "tenant_id": TEST_AUTH_USER.tenant_id,
            **overrides,
        }

    async def find_by_id(self, user_id: str) -> dict[str, Any] | None:
        return self.row


class RecordingDownloadAuditRepository:
    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []

    async def record(self, **kwargs: Any) -> None:
        self.records.append(kwargs)


class DenyingObjectAccessRepository:
    async def can_access_artifact(self, **_kwargs: Any) -> bool:
        return False

    async def can_access_task(self, **_kwargs: Any) -> bool:
        return True


@pytest.fixture()
def app_client() -> TestClient:
    app = create_app()
    app.dependency_overrides[evidence_module._get_task_insight_service] = FakeInsightService
    return TestClient(app)


@pytest.fixture()
def audit(app_client: TestClient) -> RecordingDownloadAuditRepository:
    recorder = RecordingDownloadAuditRepository()
    app_client.app.dependency_overrides[evidence_module._get_download_audit_repository] = (
        lambda: recorder
    )
    return recorder


def _issue_and_fetch(test_client: TestClient) -> int:
    issued = test_client.post(f"/api/v1/evidence/{ARTIFACT_ID}/ticket")
    assert issued.status_code == 200
    return test_client.get(f"/api/v1{issued.json()['url']}").status_code


def test_valid_ticket_still_serves_file_for_current_subject(app_client: TestClient) -> None:
    assert _issue_and_fetch(app_client) == 200


def test_disabled_account_cannot_redeem_unexpired_ticket(app_client: TestClient) -> None:
    """签发后账号被停用，未过期的旧票据必须立即失效。"""
    pop_test_ticket_subject(app_client.app)
    app_client.app.dependency_overrides[evidence_module._get_ticket_subject_repository] = (
        lambda: StubTicketSubjectRepository(status="disabled")
    )

    assert _issue_and_fetch(app_client) == 404


def test_deleted_account_cannot_redeem_unexpired_ticket(app_client: TestClient) -> None:
    repository = StubTicketSubjectRepository()
    repository.row = None
    pop_test_ticket_subject(app_client.app)
    app_client.app.dependency_overrides[evidence_module._get_ticket_subject_repository] = (
        lambda: repository
    )

    assert _issue_and_fetch(app_client) == 404


def test_tenant_change_invalidates_unexpired_ticket(app_client: TestClient) -> None:
    """票据签发时绑定的租户与当前用户租户不一致，一律视为已撤权。"""
    pop_test_ticket_subject(app_client.app)
    app_client.app.dependency_overrides[evidence_module._get_ticket_subject_repository] = (
        lambda: StubTicketSubjectRepository(tenant_id="tenant-other")
    )

    assert _issue_and_fetch(app_client) == 404


def test_revoked_object_access_invalidates_unexpired_ticket(app_client: TestClient) -> None:
    """对象权限在签发后被收回时，兑现必须按当前事实重判而不是复用票据。"""
    issued = app_client.post(f"/api/v1/evidence/{ARTIFACT_ID}/ticket")
    assert issued.status_code == 200
    ticket_url = f"/api/v1{issued.json()['url']}"

    pop_test_object_access(app_client.app)
    app_client.app.dependency_overrides[
        object_access_module._get_object_access_repository
    ] = DenyingObjectAccessRepository

    assert app_client.get(ticket_url).status_code == 404


def test_allowed_download_is_audited(
    app_client: TestClient, audit: RecordingDownloadAuditRepository
) -> None:
    assert _issue_and_fetch(app_client) == 200

    assert len(audit.records) == 1
    record = audit.records[0]
    assert record["artifact_id"] == ARTIFACT_ID
    assert record["actor_user_id"] == TEST_AUTH_USER.id
    assert record["tenant_id"] == TEST_AUTH_USER.tenant_id
    assert record["result"] == "allowed"
    assert record["denied_reason"] is None
    assert record["ticket_id"]


def test_subject_revoked_download_is_audited(
    app_client: TestClient, audit: RecordingDownloadAuditRepository
) -> None:
    pop_test_ticket_subject(app_client.app)
    app_client.app.dependency_overrides[evidence_module._get_ticket_subject_repository] = (
        lambda: StubTicketSubjectRepository(status="disabled")
    )

    assert _issue_and_fetch(app_client) == 404
    assert [item["result"] for item in audit.records] == ["denied"]
    assert audit.records[0]["denied_reason"] == "subject_revoked"


def test_object_access_revoked_download_is_audited(
    app_client: TestClient, audit: RecordingDownloadAuditRepository
) -> None:
    """权限必须在签发后才收回，否则票据在签发这一步就已经拿不到。"""
    issued = app_client.post(f"/api/v1/evidence/{ARTIFACT_ID}/ticket")
    assert issued.status_code == 200
    ticket_url = f"/api/v1{issued.json()['url']}"

    pop_test_object_access(app_client.app)
    app_client.app.dependency_overrides[
        object_access_module._get_object_access_repository
    ] = DenyingObjectAccessRepository

    assert app_client.get(ticket_url).status_code == 404
    assert [item["result"] for item in audit.records] == ["denied"]
    assert audit.records[0]["denied_reason"] == "object_access_revoked"
