from dataclasses import dataclass
from typing import Any

import pytest

from services.api.app.domain.iam.access_scope import AccessScope
from services.api.app.infrastructure.db.object_access_repository import (
    ObjectAccessAction,
    PostgresObjectAccessRepository,
)

_TASK_ID = "11111111-1111-1111-1111-111111111111"
_OWNER_ID = "22222222-2222-2222-2222-222222222222"
_TENANT_ID = "33333333-3333-3333-3333-333333333333"


class FakeConnection:
    def __init__(self, row: dict[str, Any] | None) -> None:
        self.row = row
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        self.calls.append((query, args))
        return self.row


@dataclass
class _Acquire:
    connection: FakeConnection

    async def __aenter__(self) -> FakeConnection:
        return self.connection

    async def __aexit__(self, *_args: Any) -> None:
        return None


class FakePool:
    def __init__(self, row: dict[str, Any] | None) -> None:
        self.connection = FakeConnection(row)

    def acquire(self) -> _Acquire:
        return _Acquire(self.connection)


def _scope(*permissions: str, actor: str = _OWNER_ID, tenant: str = _TENANT_ID) -> AccessScope:
    return AccessScope(
        actor_user_id=actor,
        tenant_id=tenant,
        permissions=frozenset(permissions),
    )


@pytest.mark.asyncio
async def test_owner_can_read_and_write_task() -> None:
    repository = PostgresObjectAccessRepository(
        pool=FakePool({"owner_user_id": _OWNER_ID, "tenant_id": _TENANT_ID})
    )

    assert await repository.can_access_task(task_id=_TASK_ID, scope=_scope())
    assert await repository.can_access_task(
        task_id=_TASK_ID,
        scope=_scope(),
        action=ObjectAccessAction.WRITE,
    )


@pytest.mark.asyncio
async def test_same_tenant_reader_cannot_write() -> None:
    repository = PostgresObjectAccessRepository(
        pool=FakePool({"owner_user_id": _OWNER_ID, "tenant_id": _TENANT_ID})
    )
    reader = _scope(
        "taskObjects:read:tenant",
        actor="44444444-4444-4444-4444-444444444444",
    )

    assert await repository.can_access_task(task_id=_TASK_ID, scope=reader)
    assert not await repository.can_access_task(
        task_id=_TASK_ID,
        scope=reader,
        action=ObjectAccessAction.WRITE,
    )


@pytest.mark.asyncio
async def test_cross_tenant_is_hidden_without_any_permission() -> None:
    repository = PostgresObjectAccessRepository(
        pool=FakePool({"owner_user_id": _OWNER_ID, "tenant_id": _TENANT_ID})
    )
    other_tenant = _scope(
        "taskObjects:read:tenant",
        actor="44444444-4444-4444-4444-444444444444",
        tenant="55555555-5555-5555-5555-555555555555",
    )

    assert not await repository.can_access_task(task_id=_TASK_ID, scope=other_tenant)


@pytest.mark.asyncio
async def test_admin_any_permission_crosses_tenant() -> None:
    repository = PostgresObjectAccessRepository(
        pool=FakePool({"owner_user_id": _OWNER_ID, "tenant_id": _TENANT_ID})
    )
    admin = _scope(
        "taskObjects:read:any",
        "taskObjects:write:any",
        actor="44444444-4444-4444-4444-444444444444",
        tenant="55555555-5555-5555-5555-555555555555",
    )

    assert await repository.can_access_task(task_id=_TASK_ID, scope=admin)
    assert await repository.can_access_task(
        task_id=_TASK_ID,
        scope=admin,
        action=ObjectAccessAction.WRITE,
    )


@pytest.mark.asyncio
async def test_invalid_or_missing_identity_is_not_accessible() -> None:
    pool = FakePool(None)
    repository = PostgresObjectAccessRepository(pool=pool)

    assert not await repository.can_access_task(task_id="not-a-uuid", scope=_scope())
    assert not await repository.can_access_task(task_id=_TASK_ID, scope=_scope())
    assert len(pool.connection.calls) == 1
