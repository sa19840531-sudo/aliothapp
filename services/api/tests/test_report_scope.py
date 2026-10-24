"""报告读写链路的 owner / tenant SQL 边界测试。"""

from dataclasses import dataclass
from typing import Any

import pytest

from services.api.app.domain.iam.access_scope import AccessScope
from services.api.app.infrastructure.db.task_insight_repository import (
    PostgresTaskInsightRepository,
)

TASK_ID = "11111111-1111-1111-1111-111111111111"
EXECUTION_ID = "22222222-2222-2222-2222-222222222222"
OWNER_ID = "33333333-3333-3333-3333-333333333333"
TENANT_ID = "44444444-4444-4444-4444-444444444444"


class _Connection:
    def __init__(self, *, row: dict[str, Any] | None = None) -> None:
        self.row = row
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        self.calls.append((" ".join(query.split()), args))
        return self.row

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        self.calls.append((" ".join(query.split()), args))
        return []


@dataclass
class _Acquire:
    connection: _Connection

    async def __aenter__(self) -> _Connection:
        return self.connection

    async def __aexit__(self, *_args: Any) -> None:
        return None


class _Pool:
    def __init__(self, *, row: dict[str, Any] | None = None) -> None:
        self.connection = _Connection(row=row)

    def acquire(self) -> _Acquire:
        return _Acquire(self.connection)


def _scope(*permissions: str, actor: str = OWNER_ID, tenant: str = TENANT_ID) -> AccessScope:
    return AccessScope(
        actor_user_id=actor,
        tenant_id=tenant,
        permissions=frozenset(permissions),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("scope", "expected_any", "expected_tenant_read"),
    [
        (_scope("taskObjects:read:owner"), False, False),
        (
            _scope(
                "taskObjects:read:tenant",
                actor="55555555-5555-5555-5555-555555555555",
            ),
            False,
            True,
        ),
        (
            _scope(
                "taskObjects:read:tenant",
                actor="55555555-5555-5555-5555-555555555555",
                tenant="66666666-6666-6666-6666-666666666666",
            ),
            False,
            True,
        ),
        (
            _scope(
                "taskObjects:read:any",
                actor="55555555-5555-5555-5555-555555555555",
                tenant="66666666-6666-6666-6666-666666666666",
            ),
            True,
            True,
        ),
    ],
)
async def test_report_context_sql_carries_owner_tenant_matrix(
    scope: AccessScope,
    expected_any: bool,
    expected_tenant_read: bool,
) -> None:
    pool = _Pool()
    repository = PostgresTaskInsightRepository(pool=pool)

    assert await repository.fetch_task_context(TASK_ID, access_scope=scope) is None

    query, args = pool.connection.calls[0]
    assert "task.tenant_id = $3::uuid" in query
    assert "task.submitter_id = $5::uuid" in query
    assert args == (
        TASK_ID,
        expected_any,
        scope.tenant_id,
        expected_tenant_read,
        scope.actor_user_id,
        None,
    )


@pytest.mark.asyncio
async def test_report_confirmation_execution_query_binds_path_task_and_scope() -> None:
    pool = _Pool()
    repository = PostgresTaskInsightRepository(pool=pool)
    scope = _scope("taskObjects:review:tenant")

    assert (
        await repository.fetch_rule_execution(
            EXECUTION_ID,
            task_id=TASK_ID,
            access_scope=scope,
        )
        is None
    )

    query, args = pool.connection.calls[0]
    assert "execution.task_id = $2::uuid" in query
    assert "task.tenant_id = $4::uuid" in query
    assert "task.submitter_id = $6::uuid" in query
    assert args == (
        EXECUTION_ID,
        TASK_ID,
        False,
        scope.tenant_id,
        True,
        scope.actor_user_id,
        None,
    )


@pytest.mark.asyncio
async def test_confirmation_update_rejects_missing_path_task_before_database_write() -> None:
    pool = _Pool()
    repository = PostgresTaskInsightRepository(pool=pool)
    scope = _scope("taskObjects:review:tenant")

    assert (
        await repository.update_human_verdict(
            execution_id=EXECUTION_ID,
            verdict="pass",
            note=None,
            operator=scope.actor_user_id,
            access_scope=scope,
        )
        is None
    )
    assert pool.connection.calls == []
