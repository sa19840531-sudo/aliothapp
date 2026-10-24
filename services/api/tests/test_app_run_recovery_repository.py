"""App Run 执行租约与 PostgreSQL-first 恢复快照仓储测试。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from services.api.app.application.app_run_recovery import (
    AcquireAppRunExecutionLeaseCommand,
    AppRunRecoveryPersistenceError,
    ReleaseAppRunExecutionLeaseCommand,
    RenewAppRunExecutionLeaseCommand,
)
from services.api.app.infrastructure.db.app_run_recovery_repository import (
    PostgresAppRunRecoveryRepository,
)
from services.api.app.infrastructure.queue.redis_queue import AgentRunQueueStatus


class FakeAcquire:
    def __init__(self, connection: FakeConnection) -> None:
        self.connection = connection

    async def __aenter__(self) -> FakeConnection:
        return self.connection

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        del exc_type, exc, traceback


class FakePool:
    def __init__(self, connection: FakeConnection) -> None:
        self.connection = connection

    def acquire(self) -> FakeAcquire:
        return FakeAcquire(self.connection)


class FakeConnection:
    """按调用顺序返回数据库结果，并保留压缩后的 SQL 供断言。"""

    def __init__(
        self,
        *,
        fetchrow_results: list[dict[str, Any] | None] | None = None,
        fetch_results: list[list[dict[str, Any]]] | None = None,
        fetchval_results: list[Any] | None = None,
    ) -> None:
        self.fetchrow_results = list(fetchrow_results or [])
        self.fetch_results = list(fetch_results or [])
        self.fetchval_results = list(fetchval_results or [])
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        self.calls.append((" ".join(query.split()), args))
        if not self.fetchrow_results:
            raise AssertionError("未配置 fetchrow 返回值")
        return self.fetchrow_results.pop(0)

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        self.calls.append((" ".join(query.split()), args))
        if not self.fetch_results:
            raise AssertionError("未配置 fetch 返回值")
        return self.fetch_results.pop(0)

    async def fetchval(self, query: str, *args: Any) -> Any:
        self.calls.append((" ".join(query.split()), args))
        if not self.fetchval_results:
            raise AssertionError("未配置 fetchval 返回值")
        return self.fetchval_results.pop(0)


def _lease_row(
    *,
    task_id: str = "11111111-1111-1111-1111-111111111111",
    agent_run_id: str = "22222222-2222-2222-2222-222222222222",
    lease_owner: str = "app-worker-1",
    lease_token: str = "33333333-3333-3333-3333-333333333333",
    row_version: int = 2,
    attempt_count: int = 3,
    lease_expires_at: datetime | None = None,
) -> dict[str, Any]:
    return {
        "task_id": task_id,
        "agent_run_id": agent_run_id,
        "lease_owner": lease_owner,
        "lease_token": lease_token,
        "lease_expires_at": lease_expires_at or datetime(2026, 8, 13, 10, 0, tzinfo=UTC),
        "row_version": row_version,
        "attempt_count": attempt_count,
    }


def _recovery_row(
    *,
    run_status: str = "running",
    ready: bool,
    reason: str,
    next_recovery_at: datetime | None,
    execution_lease_active: bool,
) -> dict[str, Any]:
    started_at = datetime(2026, 8, 13, 9, 0, tzinfo=UTC)
    return {
        "run_id": "22222222-2222-2222-2222-222222222222",
        "task_id": "11111111-1111-1111-1111-111111111111",
        "status": run_status,
        "input_context": {
            "message": "恢复 App Run",
            "task_config": {"kind": "app"},
            "page_sampling_policy": {"mode": "first_n"},
        },
        "output_context": {},
        "working_memory": {"current_page": {"url": "https://example.test"}},
        "loop_guard": {"max_steps": 10},
        "stop_reason": None,
        "error_message": None,
        "total_steps": 2,
        "started_at": started_at,
        "finished_at": None,
        "created_at": started_at - timedelta(minutes=1),
        "recovery_ready": ready,
        "recovery_reason": reason,
        "next_recovery_at": next_recovery_at,
        "execution_lease_active": execution_lease_active,
    }


def _acquire_command() -> AcquireAppRunExecutionLeaseCommand:
    return AcquireAppRunExecutionLeaseCommand(
        task_id="11111111-1111-1111-1111-111111111111",
        agent_run_id="22222222-2222-2222-2222-222222222222",
        lease_owner="app-worker-1",
        lease_token="33333333-3333-3333-3333-333333333333",
        lease_seconds=120,
    )


async def test_acquire_allows_expired_lease_takeover_and_binds_identity() -> None:
    connection = FakeConnection(fetchrow_results=[_lease_row()])
    repository = PostgresAppRunRecoveryRepository(pool=FakePool(connection))

    lease = await repository.acquire(_acquire_command())

    assert lease is not None
    assert lease.task_id == "11111111-1111-1111-1111-111111111111"
    assert lease.agent_run_id == "22222222-2222-2222-2222-222222222222"
    assert lease.lease_owner == "app-worker-1"
    assert lease.lease_token == "33333333-3333-3333-3333-333333333333"
    query, args = connection.calls[0]
    assert "INSERT INTO qc_app_run_execution_lease AS existing" in query
    assert "run.id = $1::uuid" in query
    assert "run.task_id = $2::uuid" in query
    assert "existing.task_id = EXCLUDED.task_id" in query
    assert "existing.lease_expires_at <= now()" in query
    assert "existing.lease_owner = EXCLUDED.lease_owner" in query
    assert "existing.lease_token = EXCLUDED.lease_token" in query
    assert "THEN existing.attempt_count" in query
    assert "ELSE existing.attempt_count + 1" in query
    assert args == (
        "22222222-2222-2222-2222-222222222222",
        "11111111-1111-1111-1111-111111111111",
        "app-worker-1",
        "33333333-3333-3333-3333-333333333333",
        120,
    )


async def test_acquire_returns_none_when_database_rejects_identity_or_active_lease() -> None:
    connection = FakeConnection(fetchrow_results=[None])
    repository = PostgresAppRunRecoveryRepository(pool=FakePool(connection))

    assert await repository.acquire(_acquire_command()) is None
    query, _args = connection.calls[0]
    assert "task.target_platform = 'app'" in query
    assert "task.status IN ('queued', 'running')" in query
    assert "task.finalization_state <> 'sealed'" in query


async def test_renew_requires_live_owner_token_and_task_identity() -> None:
    connection = FakeConnection(fetchrow_results=[_lease_row(row_version=4)])
    repository = PostgresAppRunRecoveryRepository(pool=FakePool(connection))
    command = RenewAppRunExecutionLeaseCommand(
        task_id="11111111-1111-1111-1111-111111111111",
        agent_run_id="22222222-2222-2222-2222-222222222222",
        lease_owner="app-worker-1",
        lease_token="33333333-3333-3333-3333-333333333333",
        lease_seconds=90,
    )

    lease = await repository.renew(command)

    assert lease.row_version == 4
    query, args = connection.calls[0]
    assert "UPDATE qc_app_run_execution_lease AS lease" in query
    assert "lease.agent_run_id = $1::uuid" in query
    assert "lease.task_id = $2::uuid" in query
    assert "lease.lease_owner = $3::varchar" in query
    assert "lease.lease_token = $4::uuid" in query
    assert "lease.lease_expires_at > now()" in query
    assert "run.id = lease.agent_run_id" in query
    assert "run.task_id = lease.task_id" in query
    assert "'done', 'failed', 'aborted', 'canceled'" in query
    assert "task.finalization_state <> 'sealed'" not in query
    assert args == (
        "22222222-2222-2222-2222-222222222222",
        "11111111-1111-1111-1111-111111111111",
        "app-worker-1",
        "33333333-3333-3333-3333-333333333333",
        90,
    )


async def test_renew_rejects_missing_or_expired_lease() -> None:
    connection = FakeConnection(fetchrow_results=[None])
    repository = PostgresAppRunRecoveryRepository(pool=FakePool(connection))

    with pytest.raises(AppRunRecoveryPersistenceError, match="租约已失效"):
        await repository.renew(
            RenewAppRunExecutionLeaseCommand(
                task_id="11111111-1111-1111-1111-111111111111",
                agent_run_id="22222222-2222-2222-2222-222222222222",
                lease_owner="app-worker-1",
                lease_token="33333333-3333-3333-3333-333333333333",
                lease_seconds=90,
            )
        )


async def test_release_deletes_only_matching_lease_identity() -> None:
    connection = FakeConnection(fetchval_results=[True])
    repository = PostgresAppRunRecoveryRepository(pool=FakePool(connection))
    command = ReleaseAppRunExecutionLeaseCommand(
        task_id="11111111-1111-1111-1111-111111111111",
        agent_run_id="22222222-2222-2222-2222-222222222222",
        lease_owner="app-worker-1",
        lease_token="33333333-3333-3333-3333-333333333333",
    )

    assert await repository.release(command) is True
    query, args = connection.calls[0]
    assert "DELETE FROM qc_app_run_execution_lease AS lease" in query
    assert "lease.agent_run_id = $1::uuid" in query
    assert "lease.task_id = $2::uuid" in query
    assert "lease.lease_owner = $3::varchar" in query
    assert "lease.lease_token = $4::uuid" in query
    assert "RETURNING true" in query
    assert args == (
        "22222222-2222-2222-2222-222222222222",
        "11111111-1111-1111-1111-111111111111",
        "app-worker-1",
        "33333333-3333-3333-3333-333333333333",
    )


async def test_recovery_snapshots_parse_waiting_and_active_lease_states() -> None:
    now = datetime(2026, 8, 13, 10, 0, tzinfo=UTC)
    due = _recovery_row(
        ready=True,
        reason="exploration_work_due_waiting_runtime_recovery",
        next_recovery_at=now - timedelta(seconds=1),
        execution_lease_active=False,
    )
    future = _recovery_row(
        ready=False,
        reason="exploration_work_waiting",
        next_recovery_at=now + timedelta(minutes=5),
        execution_lease_active=False,
    )
    active = _recovery_row(
        ready=False,
        reason="run_execution_lease_active",
        next_recovery_at=now + timedelta(minutes=2),
        execution_lease_active=True,
    )
    connection = FakeConnection(fetch_results=[[due, future, active]])
    repository = PostgresAppRunRecoveryRepository(pool=FakePool(connection))

    snapshots = await repository.list_recovery_snapshots(limit=3)

    assert [snapshot.ready for snapshot in snapshots] == [True, False, False]
    assert [snapshot.reason for snapshot in snapshots] == [
        "exploration_work_due_waiting_runtime_recovery",
        "exploration_work_waiting",
        "run_execution_lease_active",
    ]
    assert snapshots[0].record.status is AgentRunQueueStatus.QUEUED
    assert snapshots[0].next_recovery_at == now - timedelta(seconds=1)
    assert snapshots[1].next_recovery_at == now + timedelta(minutes=5)
    assert snapshots[2].execution_lease_active is True
    assert snapshots[2].next_recovery_at == now + timedelta(minutes=2)
    query, args = connection.calls[0]
    assert "execution_lease.lease_expires_at > now()" in query
    assert args == (None, 3)


async def test_recovery_snapshots_wake_persisted_result_session_and_unclaimable_work() -> None:
    now = datetime(2026, 8, 13, 10, 0, tzinfo=UTC)
    rows = [
        _recovery_row(
            ready=True,
            reason="exploration_result_waiting_runtime_recovery",
            next_recovery_at=now,
            execution_lease_active=False,
        ),
        _recovery_row(
            ready=True,
            reason="app_ready_session_unavailable_waiting_runtime_recovery",
            next_recovery_at=now,
            execution_lease_active=False,
        ),
        _recovery_row(
            ready=True,
            reason="exploration_work_unclaimable_waiting_runtime_recovery",
            next_recovery_at=now,
            execution_lease_active=False,
        ),
        _recovery_row(
            ready=True,
            reason="terminal_work_waiting_runtime_recovery",
            next_recovery_at=now,
            execution_lease_active=False,
        ),
        _recovery_row(
            ready=False,
            reason="exploration_work_waiting",
            next_recovery_at=now + timedelta(minutes=5),
            execution_lease_active=False,
        ),
        _recovery_row(
            ready=False,
            reason="run_execution_lease_active",
            next_recovery_at=now + timedelta(minutes=2),
            execution_lease_active=True,
        ),
    ]
    connection = FakeConnection(fetch_results=[rows])
    repository = PostgresAppRunRecoveryRepository(pool=FakePool(connection))

    snapshots = await repository.list_recovery_snapshots(limit=len(rows))

    assert [(snapshot.ready, snapshot.reason) for snapshot in snapshots] == [
        (True, "exploration_result_waiting_runtime_recovery"),
        (True, "app_ready_session_unavailable_waiting_runtime_recovery"),
        (True, "exploration_work_unclaimable_waiting_runtime_recovery"),
        (True, "terminal_work_waiting_runtime_recovery"),
        (False, "exploration_work_waiting"),
        (False, "run_execution_lease_active"),
    ]
    assert [snapshot.next_recovery_at for snapshot in snapshots[:4]] == [now] * 4
    assert snapshots[-1].execution_lease_active is True


async def test_recovery_query_holds_persisted_work_for_checkpoint_aware_runtime() -> None:
    connection = FakeConnection(fetch_results=[[]])
    repository = PostgresAppRunRecoveryRepository(pool=FakePool(connection))

    await repository.list_recovery_snapshots(limit=10)

    query, _args = connection.calls[0]
    assert "AS has_nonterminal_work" in query
    assert "work.has_terminal_work AND NOT work.has_nonterminal_work" in query
    assert "exploration_result_waiting_runtime_recovery" in query
    assert "terminal_work_waiting_runtime_recovery" in query
    assert "exploration_work_due_waiting_runtime_recovery" in query
    assert "exploration_work_unclaimable_waiting_runtime_recovery" in query
    assert "AS has_unclaimable_work" in query
    assert "item.status IN ('pending', 'retry_wait', 'leased', 'running')" in query
    assert "item.deadline <= now() OR item.attempt_count >= item.max_attempts" in query
    assert "AS has_terminal_work" in query
    assert "item.terminal_payload IS NOT NULL" not in query
    assert "WHEN result.agent_run_id IS NOT NULL THEN true" in query
    assert "WHEN work.has_due_work THEN true" in query
    assert "WHEN work.has_unclaimable_work THEN true" in query
    assert "WHEN work.has_any_work THEN false" in query
    assert "WHEN result.agent_run_id IS NOT NULL THEN now()" in query
    assert "WHEN work.has_due_work OR work.has_unclaimable_work" in query
    assert "LEAST(item.next_attempt_at, item.deadline)" in query
    assert "LEAST(item.lease_expires_at, item.deadline)" in query
    assert "surface_session.status <> 'active'" in query
    assert "app_ready_session_unavailable_waiting_runtime_recovery" in query
    assert "ORDER BY recovery_ready DESC" in query
    assert "WITH recovery_candidates AS" in query
    assert "run.agent_type = 'main'" in query
    assert "COALESCE(next_recovery_at, created_at)" in query
    assert query.index("WHEN execution_lease.lease_expires_at > now() THEN false") < query.index(
        "WHEN result.agent_run_id IS NOT NULL THEN true"
    )


async def test_get_recovery_snapshot_returns_none_when_run_is_not_recoverable() -> None:
    connection = FakeConnection(fetch_results=[[]])
    repository = PostgresAppRunRecoveryRepository(pool=FakePool(connection))

    snapshot = await repository.get_recovery_snapshot(
        agent_run_id="22222222-2222-2222-2222-222222222222"
    )

    assert snapshot is None
    query, args = connection.calls[0]
    assert args == ("22222222-2222-2222-2222-222222222222", 1)
    assert "$1::uuid IS NOT NULL" in query
