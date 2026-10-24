"""App Bootstrap PostgreSQL 检查点与乐观锁测试。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from services.api.app.application.app_bootstrap_state import (
    CancelAppBootstrapCommand,
    FailAppBootstrapCommand,
    RenewAppBootstrapLeaseCommand,
    TransitionAppBootstrapCommand,
)
from services.api.app.domain.interaction import AppBootstrapStage
from services.api.app.infrastructure.db.app_bootstrap_repository import (
    AppBootstrapPersistenceError,
    PostgresAppBootstrapRepository,
)


class FakeTransaction:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        return None


class FakeAcquire:
    def __init__(self, connection: BootstrapConnection) -> None:
        self.connection = connection

    async def __aenter__(self) -> BootstrapConnection:
        return self.connection

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        return None


class FakePool:
    def __init__(self, connection: BootstrapConnection) -> None:
        self.connection = connection

    def acquire(self) -> FakeAcquire:
        return FakeAcquire(self.connection)


class BootstrapConnection:
    def __init__(self, *, row: dict[str, Any] | None) -> None:
        self.row = row
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    def transaction(self) -> FakeTransaction:
        return FakeTransaction()

    async def fetchrow(self, query: str, *args: Any):
        self.calls.append((" ".join(query.split()), args))
        return self.row


def _bootstrap_row(
    *,
    stage: str = "resolving_build",
    row_version: int = 2,
) -> dict[str, Any]:
    now = datetime.now(UTC)
    return {
        "task_id": "11111111-1111-1111-1111-111111111111",
        "session_id": "app-session-1",
        "platform_build_ref": "build-1",
        "stage": stage,
        "install_request_id": None,
        "install_attempt": 0,
        "installed_package": None,
        "open_request_id": None,
        "open_attempt": 0,
        "bootstrap_capture_id": None,
        "error_code": None,
        "error_message": None,
        "row_version": row_version,
        "started_at": now,
        "ready_at": None,
        "failed_at": None,
        "updated_at": now,
    }


async def test_get_or_create_claims_only_owned_android_starting_session() -> None:
    connection = BootstrapConnection(row=_bootstrap_row(stage="validating", row_version=0))
    repository = PostgresAppBootstrapRepository(pool=FakePool(connection))

    snapshot = await repository.get_or_create(
        task_id="11111111-1111-1111-1111-111111111111",
        session_id="app-session-1",
        platform_build_ref="build-1",
        lease_owner="app-worker-1",
        lease_token="22222222-2222-2222-2222-222222222222",
        lease_seconds=120,
    )

    query, args = connection.calls[0]
    assert snapshot.stage is AppBootstrapStage.VALIDATING
    assert "INSERT INTO qc_app_bootstrap AS existing" in query
    assert "session.surface_kind = 'android'" in query
    assert "session.status IN ('starting', 'active')" in query
    assert "task.finalization_state = 'open'" in query
    assert "workflow_lease_token = $5::uuid" in query
    assert "workflow_lease_expires_at <= now()" in query
    assert args[3:6] == (
        "app-worker-1",
        "22222222-2222-2222-2222-222222222222",
        120,
    )


async def test_transition_uses_stage_version_and_lease_identity() -> None:
    row = _bootstrap_row(stage="installing", row_version=3)
    row.update(
        install_request_id="install:build-1",
        install_attempt=1,
    )
    connection = BootstrapConnection(row=row)
    repository = PostgresAppBootstrapRepository(pool=FakePool(connection))

    snapshot = await repository.transition(
        TransitionAppBootstrapCommand(
            task_id=row["task_id"],
            session_id=row["session_id"],
            platform_build_ref=row["platform_build_ref"],
            expected_stage=AppBootstrapStage.RESOLVING_BUILD,
            target_stage=AppBootstrapStage.INSTALLING,
            expected_row_version=2,
            lease_owner="app-worker-1",
            lease_token="22222222-2222-2222-2222-222222222222",
            install_request_id="install:build-1",
            install_attempt=1,
        )
    )

    query, args = connection.calls[0]
    assert snapshot.stage is AppBootstrapStage.INSTALLING
    assert "bootstrap.stage = $4" in query
    assert "bootstrap.row_version = $6" in query
    assert "bootstrap.workflow_lease_owner = $7" in query
    assert "bootstrap.workflow_lease_token = $8::uuid" in query
    assert "bootstrap.workflow_lease_expires_at > now()" in query
    assert "row_version = bootstrap.row_version + 1" in query
    assert "bootstrap.stage = 'installing' AND $5::varchar = 'installed'" in query
    assert "bootstrap.stage = 'opening' AND $5::varchar = 'opened'" in query
    assert args[3:8] == (
        AppBootstrapStage.RESOLVING_BUILD.value,
        AppBootstrapStage.INSTALLING.value,
        2,
        "app-worker-1",
        "22222222-2222-2222-2222-222222222222",
    )


async def test_renew_keeps_row_version_and_requires_live_lease_owner_token() -> None:
    row = _bootstrap_row(stage="installing", row_version=3)
    connection = BootstrapConnection(row=row)
    repository = PostgresAppBootstrapRepository(pool=FakePool(connection))

    snapshot = await repository.renew(
        RenewAppBootstrapLeaseCommand(
            task_id=row["task_id"],
            session_id=row["session_id"],
            platform_build_ref=row["platform_build_ref"],
            lease_owner="app-worker-1",
            lease_token="22222222-2222-2222-2222-222222222222",
            lease_seconds=120,
        )
    )

    query, args = connection.calls[0]
    assert snapshot.row_version == 3
    assert "workflow_lease_expires_at = now() + make_interval(secs => $6::integer)" in query
    assert "bootstrap.workflow_lease_owner = $4" in query
    assert "bootstrap.workflow_lease_token = $5::uuid" in query
    assert "bootstrap.workflow_lease_expires_at > now()" in query
    assert "bootstrap.stage NOT IN ('app_ready', 'failed', 'canceled')" in query
    assert "row_version = bootstrap.row_version + 1" not in query
    assert args[3:] == (
        "app-worker-1",
        "22222222-2222-2222-2222-222222222222",
        120,
    )


async def test_renew_rejects_expired_or_stale_worker_lease() -> None:
    repository = PostgresAppBootstrapRepository(
        pool=FakePool(BootstrapConnection(row=None))
    )

    with pytest.raises(AppBootstrapPersistenceError, match="App Bootstrap Workflow 租约已失效"):
        await repository.renew(
            RenewAppBootstrapLeaseCommand(
                task_id="11111111-1111-1111-1111-111111111111",
                session_id="app-session-1",
                platform_build_ref="build-1",
                lease_owner="stale-worker",
                lease_token="33333333-3333-3333-3333-333333333333",
                lease_seconds=120,
            )
        )


async def test_transition_rejects_stale_worker_without_silent_success() -> None:
    repository = PostgresAppBootstrapRepository(
        pool=FakePool(BootstrapConnection(row=None))
    )

    with pytest.raises(AppBootstrapPersistenceError, match="阶段、版本或 Workflow 租约"):
        await repository.transition(
            TransitionAppBootstrapCommand(
                task_id="11111111-1111-1111-1111-111111111111",
                session_id="app-session-1",
                platform_build_ref="build-1",
                expected_stage=AppBootstrapStage.OPENING,
                target_stage=AppBootstrapStage.OPENED,
                expected_row_version=6,
                lease_owner="stale-worker",
                lease_token="33333333-3333-3333-3333-333333333333",
                open_request_id="open-result",
            )
        )


@pytest.mark.parametrize(
    "command",
    [
        TransitionAppBootstrapCommand(
            task_id="11111111-1111-1111-1111-111111111111",
            session_id="app-session-1",
            platform_build_ref="build-1",
            expected_stage=AppBootstrapStage.INSTALLING,
            target_stage=AppBootstrapStage.INSTALLED,
            expected_row_version=2,
            lease_owner="worker-b",
            lease_token="33333333-3333-3333-3333-333333333333",
            install_request_id="install:build-1",
            installed_package="com.example.target",
        ),
        FailAppBootstrapCommand(
            task_id="11111111-1111-1111-1111-111111111111",
            session_id="app-session-1",
            platform_build_ref="build-1",
            expected_stage=AppBootstrapStage.INSTALLING,
            expected_row_version=2,
            lease_owner="worker-b",
            lease_token="33333333-3333-3333-3333-333333333333",
            error_code="INSTALL_FAILED",
        ),
        CancelAppBootstrapCommand(
            task_id="11111111-1111-1111-1111-111111111111",
            session_id="app-session-1",
            platform_build_ref="build-1",
            expected_stage=AppBootstrapStage.OPENING,
            expected_row_version=2,
            lease_owner="worker-b",
            lease_token="33333333-3333-3333-3333-333333333333",
        ),
    ],
)
async def test_second_worker_stale_token_cannot_transition_fail_or_cancel(command: Any) -> None:
    """Worker A 已持有租约时，Worker B 的旧 token 不能覆盖任意终态。"""
    repository = PostgresAppBootstrapRepository(
        pool=FakePool(BootstrapConnection(row=None))
    )

    if isinstance(command, TransitionAppBootstrapCommand):
        operation = repository.transition(command)
    elif isinstance(command, FailAppBootstrapCommand):
        operation = repository.fail(command)
    else:
        operation = repository.cancel(command)

    with pytest.raises(AppBootstrapPersistenceError):
        await operation


async def test_fail_is_terminal_and_clears_workflow_lease() -> None:
    row = _bootstrap_row(stage="failed", row_version=5)
    failed_at = datetime.now(UTC)
    row.update(
        error_code="INSTALL_FAILED",
        error_message="安装失败",
        failed_at=failed_at,
    )
    connection = BootstrapConnection(row=row)
    repository = PostgresAppBootstrapRepository(pool=FakePool(connection))

    snapshot = await repository.fail(
        FailAppBootstrapCommand(
            task_id=row["task_id"],
            session_id=row["session_id"],
            platform_build_ref=row["platform_build_ref"],
            expected_stage=AppBootstrapStage.INSTALLING,
            expected_row_version=4,
            lease_owner="app-worker-1",
            lease_token="22222222-2222-2222-2222-222222222222",
            error_code="INSTALL_FAILED",
            error_message="安装失败",
        )
    )

    query, _ = connection.calls[0]
    assert snapshot.stage is AppBootstrapStage.FAILED
    assert "stage = 'failed'" in query
    assert "workflow_lease_owner = NULL" in query
    assert "workflow_lease_token = NULL" in query
    assert "workflow_lease_expires_at = NULL" in query
    assert "failed_at = now()" in query


async def test_cancel_is_terminal_and_clears_workflow_lease() -> None:
    row = _bootstrap_row(stage="canceled", row_version=5)
    connection = BootstrapConnection(row=row)
    repository = PostgresAppBootstrapRepository(pool=FakePool(connection))

    snapshot = await repository.cancel(
        CancelAppBootstrapCommand(
            task_id=row["task_id"],
            session_id=row["session_id"],
            platform_build_ref=row["platform_build_ref"],
            expected_stage=AppBootstrapStage.OPENING,
            expected_row_version=4,
            lease_owner="app-worker-1",
            lease_token="22222222-2222-2222-2222-222222222222",
        )
    )

    query, _ = connection.calls[0]
    assert snapshot.stage is AppBootstrapStage.CANCELED
    assert "stage = 'canceled'" in query
    assert "workflow_lease_owner = NULL" in query
    assert "workflow_lease_token = NULL" in query
    assert "workflow_lease_expires_at = NULL" in query


def test_transition_command_rejects_skipped_stage_and_invalid_attempt() -> None:
    common = {
        "task_id": "11111111-1111-1111-1111-111111111111",
        "session_id": "app-session-1",
        "platform_build_ref": "build-1",
        "expected_row_version": 0,
        "lease_owner": "app-worker-1",
        "lease_token": "22222222-2222-2222-2222-222222222222",
    }
    with pytest.raises(ValueError, match="非法 App Bootstrap 阶段跃迁"):
        TransitionAppBootstrapCommand(
            **common,
            expected_stage=AppBootstrapStage.VALIDATING,
            target_stage=AppBootstrapStage.INSTALLING,
        )
    with pytest.raises(ValueError, match="install_attempt 必须大于 0"):
        TransitionAppBootstrapCommand(
            **common,
            expected_stage=AppBootstrapStage.RESOLVING_BUILD,
            target_stage=AppBootstrapStage.INSTALLING,
            install_request_id="install:build-1",
            install_attempt=0,
        )


async def test_bootstrap_lease_expiry_must_be_positive() -> None:
    connection = BootstrapConnection(row=_bootstrap_row())
    repository = PostgresAppBootstrapRepository(pool=FakePool(connection))

    with pytest.raises(ValueError, match="lease_seconds 必须大于 0"):
        await repository.get_or_create(
            task_id="11111111-1111-1111-1111-111111111111",
            session_id="app-session-1",
            platform_build_ref="build-1",
            lease_owner="app-worker-1",
            lease_token="22222222-2222-2222-2222-222222222222",
            lease_seconds=0,
        )
