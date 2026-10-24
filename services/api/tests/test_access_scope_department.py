"""部门级数据范围：AccessScope 判定、对象级裁决与 SQL 参数。"""

from types import SimpleNamespace

import pytest

from services.api.app.domain.iam.access_scope import AccessScope, access_scope_from_user
from services.api.app.infrastructure.db.object_access_repository import (
    ObjectAccessAction,
    PostgresObjectAccessRepository,
)
from services.api.tests.test_object_access_repository import FakePool

DEPARTMENT_PERMISSIONS = frozenset(
    {
        "taskObjects:read:owner",
        "taskObjects:write:owner",
        "taskObjects:read:department",
        "taskObjects:review:department",
    }
)
TENANT = "33333333-3333-3333-3333-333333333333"
ME = "22222222-2222-2222-2222-222222222222"
COLLEAGUE = "44444444-4444-4444-4444-444444444444"


def _department_scope(department: str = "一部", **overrides) -> AccessScope:
    return AccessScope(
        actor_user_id=ME,
        tenant_id=TENANT,
        permissions=DEPARTMENT_PERMISSIONS,
        department_name=department,
        **overrides,
    )


def test_department_account_reads_and_reviews_colleague_objects_in_own_department() -> None:
    scope = _department_scope()

    assert scope.department_scope == "一部"
    assert scope.review_department_scope == "一部"
    assert scope.can_read_resource(owner_id=COLLEAGUE, tenant_id=TENANT, department_name="一部") is True
    assert scope.can_review_resource(owner_id=COLLEAGUE, tenant_id=TENANT, department_name="一部") is True
    # 同部门也不能改别人的任务
    assert scope.can_write_resource(owner_id=COLLEAGUE, tenant_id=TENANT, department_name="一部") is False
    # 其它部门、没填部门、其它公司都看不到
    assert scope.can_read_resource(owner_id=COLLEAGUE, tenant_id=TENANT, department_name="二部") is False
    assert scope.can_read_resource(owner_id=COLLEAGUE, tenant_id=TENANT, department_name=None) is False
    assert scope.can_read_resource(owner_id=COLLEAGUE, tenant_id="other", department_name="一部") is False
    # 自己发起的照旧
    assert scope.can_read_resource(owner_id=ME, tenant_id=TENANT, department_name="二部") is True


def test_account_without_department_falls_back_to_owner_scope() -> None:
    for department in ("", "  ", "-"):
        scope = _department_scope(department)
        assert scope.department_scope is None
        assert scope.review_department_scope is None
        assert scope.can_read_resource(owner_id=COLLEAGUE, tenant_id=TENANT, department_name="一部") is False
        assert scope.can_read_resource(owner_id=ME, tenant_id=TENANT, department_name="一部") is True


def test_company_admin_ignores_department_and_scope_comes_from_user_row() -> None:
    scope = access_scope_from_user(
        SimpleNamespace(
            id=ME,
            tenant_id=TENANT,
            permissions=["taskObjects:read:tenant", "taskObjects:review:tenant"],
            department_name=" 二部 ",
        )
    )
    assert scope.department_name == "二部"
    # 公司级权限不需要部门条件，SQL 参数保持 None
    assert scope.department_scope is None
    assert scope.can_read_resource(owner_id=COLLEAGUE, tenant_id=TENANT, department_name="一部") is True

    department_user = access_scope_from_user(
        SimpleNamespace(
            id=ME,
            tenant_id=TENANT,
            permissions=list(DEPARTMENT_PERMISSIONS),
            department_name="-",
        )
    )
    assert department_user.department_name == ""
    assert department_user.department_scope is None


@pytest.mark.asyncio
async def test_object_access_uses_task_department() -> None:
    same_department = PostgresObjectAccessRepository(
        pool=FakePool({"owner_user_id": COLLEAGUE, "tenant_id": TENANT, "department_name": "一部"})
    )
    other_department = PostgresObjectAccessRepository(
        pool=FakePool({"owner_user_id": COLLEAGUE, "tenant_id": TENANT, "department_name": "二部"})
    )
    legacy_row = PostgresObjectAccessRepository(
        pool=FakePool({"owner_user_id": COLLEAGUE, "tenant_id": TENANT})
    )
    scope = _department_scope()
    task_id = "11111111-1111-1111-1111-111111111111"

    assert await same_department.can_access_task(task_id=task_id, scope=scope) is True
    assert await same_department.can_access_task(task_id=task_id, scope=scope, action=ObjectAccessAction.REVIEW) is True
    assert await same_department.can_access_task(task_id=task_id, scope=scope, action=ObjectAccessAction.WRITE) is False
    assert await other_department.can_access_task(task_id=task_id, scope=scope) is False
    assert await legacy_row.can_access_task(task_id=task_id, scope=scope) is False
    query = same_department._pool.connection.calls[0][0]  # type: ignore[attr-defined]
    assert "department_name" in query
