from types import SimpleNamespace

import pytest

from services.api.app.api.v1.auth import require_access_scope
from services.api.app.domain.iam.access_scope import AccessScope, access_scope_from_user


def test_platform_admin_can_cross_tenant_read_write_and_review() -> None:
    scope = access_scope_from_user(
        SimpleNamespace(
            id="admin-1",
            tenant_id="tenant-a",
            permissions=[
                "taskObjects:read:any",
                "taskObjects:write:any",
                "taskObjects:review:any",
            ],
        )
    )

    assert scope.can_read_any is True
    assert scope.can_write_any is True
    assert scope.can_read_tenant is True
    assert scope.can_review_tenant is True
    assert scope.can_read_resource(owner_id="user-b", tenant_id="tenant-b") is True
    assert scope.can_write_resource(owner_id="user-b", tenant_id="tenant-b") is True
    assert scope.can_review_resource(owner_id="user-b", tenant_id="tenant-b") is True


def test_reviewer_reads_and_reviews_same_tenant_but_not_other_tenant() -> None:
    scope = AccessScope(
        actor_user_id="reviewer-1",
        tenant_id="tenant-a",
        permissions=frozenset(
            {"taskObjects:read:tenant", "taskObjects:review:tenant"}
        ),
    )

    assert scope.can_read_resource(owner_id="user-b", tenant_id="tenant-a") is True
    assert scope.can_review_resource(owner_id="user-b", tenant_id="tenant-a") is True
    assert scope.can_write_resource(owner_id="user-b", tenant_id="tenant-a") is False
    assert scope.can_read_resource(owner_id="user-b", tenant_id="tenant-b") is False
    assert scope.can_review_resource(owner_id="user-b", tenant_id="tenant-b") is False


def test_owner_can_read_and_write_only_own_tenant_object() -> None:
    scope = AccessScope(
        actor_user_id="owner-1",
        tenant_id="tenant-a",
        permissions=frozenset({"taskObjects:read:owner", "taskObjects:write:owner"}),
    )

    assert scope.can_read_resource(owner_id="owner-1", tenant_id="tenant-a") is True
    assert scope.can_write_resource(owner_id="owner-1", tenant_id="tenant-a") is True
    assert scope.can_read_resource(owner_id="owner-2", tenant_id="tenant-a") is False
    assert scope.can_read_resource(owner_id="owner-1", tenant_id="tenant-b") is False


def test_scope_rejects_missing_authenticated_tenant() -> None:
    with pytest.raises(ValueError, match="tenant_id"):
        access_scope_from_user(SimpleNamespace(id="owner-1", permissions=[]))


def test_fastapi_scope_dependency_is_callable_with_current_user() -> None:
    assert callable(require_access_scope)
    assert "AccessScope" in str(require_access_scope.__annotations__["return"])
