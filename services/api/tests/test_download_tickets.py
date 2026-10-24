from datetime import UTC, datetime

import jwt
import pytest

from services.api.app.application.download_tickets import (
    DownloadTicketError,
    SignedDownloadTicketService,
)

_SECRET = "download-ticket-test-secret-at-least-32-bytes"


def test_ticket_is_resource_bound_and_short_lived() -> None:
    service = SignedDownloadTicketService(secret=_SECRET, ttl_seconds=60)
    now = datetime(2026, 8, 18, 8, 0, tzinfo=UTC)

    token, expires_at = service.issue(
        resource_kind="evidence_artifact",
        resource_id="artifact-1",
        actor_user_id="user-1",
        tenant_id="tenant-1",
        now=now,
    )
    payload = jwt.decode(
        token,
        _SECRET,
        algorithms=["HS256"],
        audience="alioth-resource-download",
        options={"verify_exp": False, "verify_iat": False},
    )

    assert expires_at.timestamp() - now.timestamp() == 60
    assert payload["typ"] == "resource_download_ticket"
    assert payload["resource_kind"] == "evidence_artifact"
    assert payload["resource_id"] == "artifact-1"
    assert "permissions" not in payload


def test_ticket_rejects_resource_identity_reuse() -> None:
    service = SignedDownloadTicketService(secret=_SECRET)
    token, _expires_at = service.issue(
        resource_kind="evidence_artifact",
        resource_id="artifact-1",
        actor_user_id="user-1",
        tenant_id="tenant-1",
    )

    with pytest.raises(DownloadTicketError, match="资源身份不匹配"):
        service.verify(
            token,
            resource_kind="evidence_artifact",
            resource_id="artifact-2",
        )


def test_ticket_service_rejects_long_lived_or_weak_configuration() -> None:
    with pytest.raises(ValueError, match="至少需要 32 字节"):
        SignedDownloadTicketService(secret="short")
    with pytest.raises(ValueError, match="1 到 300"):
        SignedDownloadTicketService(secret=_SECRET, ttl_seconds=301)
