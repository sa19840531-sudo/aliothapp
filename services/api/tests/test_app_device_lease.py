"""App 设备租约 claim、续租、fencing 和隔离合同测试。"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from services.api.app.application.device_pool import (
    DeviceLease,
    DeviceLeaseGuard,
    DeviceLeaseLostError,
    DevicePoolError,
    InProcessDevicePool,
    maybe_power_off_cloud_phone_for_lease,
    release_device_lease,
)
from services.api.app.infrastructure.db.device_lease_repository import (
    PostgresDeviceLeaseRepository,
)


class _FakeTransaction:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        del exc_type, exc, traceback


class _FakeAcquire:
    def __init__(self, connection: _FakeConnection) -> None:
        self.connection = connection

    async def __aenter__(self) -> _FakeConnection:
        return self.connection

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        del exc_type, exc, traceback


class _FakePool:
    def __init__(self, connection: _FakeConnection) -> None:
        self.connection = connection

    def acquire(self) -> _FakeAcquire:
        return _FakeAcquire(self.connection)


class _FakeConnection:
    def __init__(self, *, lease_row: dict[str, Any]) -> None:
        self.lease_row = lease_row
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.available_resource: dict[str, Any] | None = {
            "device_ref": "cloud_phone:a",
            "backend": "cloud_phone",
            "profile_id": "a",
            "fencing_epoch": 0,
        }
        self.claim_select_results: list[dict[str, Any] | None] | None = None
        self.existing_resource: dict[str, Any] | None = None
        self.fetch_rows: list[dict[str, Any]] = []
        self.fetchval_result: Any = None

    def transaction(self) -> _FakeTransaction:
        return _FakeTransaction()

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        normalized = " ".join(query.split())
        self.calls.append((normalized, args))
        if "SELECT lease.*, resource.backend" in normalized:
            return None
        if "SELECT resource.device_ref" in normalized:
            if self.claim_select_results is not None:
                if not self.claim_select_results:
                    return None
                return self.claim_select_results.pop(0)
            return self.available_resource
        if "SELECT device_ref, enabled, health_state" in normalized:
            return self.existing_resource
        if (
            "UPDATE qc_device_resource" in normalized
            and "RETURNING device_ref, backend" in normalized
        ):
            resource = self.available_resource or {}
            return {
                "device_ref": resource.get("device_ref", "cloud_phone:a"),
                "backend": resource.get("backend", "cloud_phone"),
                "profile_id": resource.get("profile_id", "a"),
                "fencing_epoch": 9,
                "capabilities": resource.get("capabilities", {}),
            }
        if "INSERT INTO qc_device_lease" in normalized:
            return self.lease_row
        if "UPDATE qc_device_lease" in normalized:
            return self.lease_row
        if "SELECT backend, profile_id" in normalized:
            resource = self.available_resource or {}
            return {
                "backend": resource.get("backend", "cloud_phone"),
                "profile_id": resource.get("profile_id", "a"),
                "capabilities": resource.get("capabilities", {}),
            }
        raise AssertionError(f"未处理 SQL：{normalized}")

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        normalized = " ".join(query.split())
        self.calls.append((normalized, args))
        return list(self.fetch_rows)

    async def fetchval(self, query: str, *args: Any) -> Any:
        normalized = " ".join(query.split())
        self.calls.append((normalized, args))
        if self.fetchval_result is not None:
            return self.fetchval_result
        if "COUNT(DISTINCT task.id)" in normalized:
            return 0
        return self.lease_row["lease_id"]

    async def execute(self, query: str, *args: Any) -> str:
        self.calls.append((" ".join(query.split()), args))
        return "UPDATE 1"


def _lease_row() -> dict[str, Any]:
    now = datetime.now(UTC)
    return {
        "lease_id": "11111111-1111-1111-1111-111111111111",
        "device_ref": "cloud_phone:a",
        "task_id": "22222222-2222-2222-2222-222222222222",
        "session_id": "app:session-1",
        "lease_owner": "worker-a",
        "lease_token": "33333333-3333-3333-3333-333333333333",
        "fencing_token": 9,
        "state": "claimed",
        "claimed_at": now,
        "expires_at": now + timedelta(minutes=5),
        "last_renewed_at": None,
    }


def _parse_claimed_lease() -> DeviceLease:
    row = _lease_row()
    return DeviceLease(
        lease_id=row["lease_id"],
        device_ref=row["device_ref"],
        task_id=row["task_id"],
        session_id=row["session_id"],
        claimed_at=row["claimed_at"],
        expires_at=row["expires_at"],
        backend="cloud_phone",
        profile_id="a",
        lease_token=row["lease_token"],
        lease_owner=row["lease_owner"],
        fencing_token=row["fencing_token"],
        state="claimed",
    )


class _RecordingLeaseRepository:
    def __init__(self, *, lease: DeviceLease) -> None:
        self.lease = lease
        self.order: list[str] = []
        self.kwargs: list[dict[str, Any]] = []

    async def recover_expired(self, *, limit: int = 100) -> int:
        self.order.append("recover_expired")
        self.kwargs.append({"limit": limit})
        return 2

    async def reclaim_recovering(self, *, limit: int = 100) -> int:
        self.order.append("reclaim_recovering")
        self.kwargs.append({"limit": limit})
        return 1

    async def release_finished_task_devices(self, *, limit: int = 100) -> int:
        self.order.append("release_finished_task_devices")
        self.kwargs.append({"limit": limit})
        return 1

    async def quarantine_stale_recovering(
        self,
        *,
        grace_seconds: int = 60,
        limit: int = 100,
    ) -> int:
        self.order.append("quarantine_stale_recovering")
        self.kwargs.append({"grace_seconds": grace_seconds, "limit": limit})
        return 1

    async def claim(self, **kwargs: Any) -> DeviceLease:
        self.order.append("claim")
        self.kwargs.append(dict(kwargs))
        return self.lease


def test_in_process_pool_requires_explicit_identity_for_release_and_fencing() -> None:
    pool = InProcessDevicePool(slots=["cloud_phone:a"])
    first = pool.claim(
        task_id="task-1",
        session_id="session-1",
        preferred_device_ref="cloud_phone:a",
        lease_owner="worker-a",
    )

    assert first.fencing_token == 1
    assert first.lease_owner == "worker-a"
    assert pool.release(lease_id=first.lease_id, session_id="other-session") is False
    assert pool.release(lease=first) is True

    second = pool.claim(
        task_id="task-2",
        session_id="session-2",
        preferred_device_ref="cloud_phone:a",
        lease_owner="worker-b",
    )
    assert second.fencing_token == 2
    assert pool.release(lease=first) is False
    with pytest.raises(DeviceLeaseLostError):
        pool.renew(lease=first, lease_seconds=120)


@pytest.mark.asyncio
async def test_postgres_device_lease_repository_claim_and_renew_bind_owner_and_fencing() -> None:
    row = _lease_row()
    connection = _FakeConnection(lease_row=row)
    repository = PostgresDeviceLeaseRepository(pool=_FakePool(connection))

    claimed = await repository.claim(
        task_id=row["task_id"],
        session_id=row["session_id"],
        preferred_device_ref=row["device_ref"],
        lease_owner=row["lease_owner"],
        lease_seconds=120,
    )
    assert claimed.lease_owner == row["lease_owner"]
    assert claimed.fencing_token == row["fencing_token"]
    claim_sql, claim_args = next(
        item for item in connection.calls if "INSERT INTO qc_device_lease" in item[0]
    )
    assert "lease_owner" in claim_sql
    assert "fencing_token" in claim_sql
    assert claim_args[4] == row["lease_owner"]

    connection.calls.clear()
    renewed = await repository.renew(lease=claimed, lease_seconds=120)
    renew_sql, renew_args = next(
        item for item in connection.calls if "UPDATE qc_device_lease" in item[0]
    )
    assert renewed.state == "claimed"
    assert "lease.lease_owner = $5" in renew_sql
    assert "lease.fencing_token = $7::bigint" in renew_sql
    assert renew_args[4] == row["lease_owner"]


@pytest.mark.asyncio
async def test_postgres_claim_distinguishes_missing_and_busy_preferred() -> None:
    connection = _FakeConnection(lease_row=_lease_row())
    connection.available_resource = None
    repository = PostgresDeviceLeaseRepository(pool=_FakePool(connection))

    with pytest.raises(DevicePoolError) as missing:
        await repository.claim(
            task_id="22222222-2222-2222-2222-222222222222",
            session_id="app:session-missing",
            preferred_device_ref="cloud_phone:missing",
            lease_seconds=120,
        )
    assert missing.value.error_code == "DEVICE_RESOURCE_NOT_FOUND"
    assert missing.value.retryable is False

    connection.existing_resource = {
        "device_ref": "cloud_phone:a",
        "enabled": True,
        "health_state": "claimed",
    }
    with pytest.raises(DevicePoolError) as busy:
        await repository.claim(
            task_id="22222222-2222-2222-2222-222222222222",
            session_id="app:session-busy",
            preferred_device_ref="cloud_phone:a",
            lease_seconds=120,
        )
    assert busy.value.error_code == "DEVICE_BUSY"


@pytest.mark.asyncio
async def test_postgres_claim_falls_back_when_preferred_device_is_busy() -> None:
    connection = _FakeConnection(lease_row=_lease_row())
    connection.claim_select_results = [
        None,
        {
            "device_ref": "cloud_phone:b",
            "backend": "cloud_phone",
            "profile_id": "b",
            "fencing_epoch": 0,
        },
    ]
    connection.existing_resource = {
        "device_ref": "cloud_phone:a",
        "enabled": True,
        "health_state": "claimed",
    }
    repository = PostgresDeviceLeaseRepository(pool=_FakePool(connection))

    await repository.claim(
        task_id="22222222-2222-2222-2222-222222222222",
        session_id="app:session-fallback",
        preferred_device_ref="cloud_phone:a",
        lease_seconds=120,
    )
    update_args = next(
        args
        for sql, args in connection.calls
        if "UPDATE qc_device_resource" in sql and "health_state = 'claimed'" in sql
    )
    assert update_args[0] == "cloud_phone:b"


@pytest.mark.asyncio
async def test_postgres_recover_expired_and_reclaim_terminal_session_sql() -> None:
    row = _lease_row()
    connection = _FakeConnection(lease_row=row)
    connection.fetch_rows = [{"device_ref": "cloud_phone:a"}]
    repository = PostgresDeviceLeaseRepository(pool=_FakePool(connection))

    assert await repository.recover_expired(limit=10) == 1
    recover_sql = next(
        item[0]
        for item in connection.calls
        if "stale AS" in item[0] and "expires_at <= now()" in item[0]
    )
    assert "state = 'recovering'" in recover_sql
    assert "FOR UPDATE SKIP LOCKED" in recover_sql

    connection.calls.clear()
    assert await repository.reclaim_recovering(limit=10) == 1
    reclaim_sql = connection.calls[0][0]
    assert "lease.state = 'recovering'" in reclaim_sql
    assert "session.status IN ('closed', 'lost', 'failed')" in reclaim_sql
    assert "state = 'released'" in reclaim_sql

    connection.calls.clear()
    assert await repository.quarantine_stale_recovering(grace_seconds=60, limit=10) == 1
    quarantine_sql = connection.calls[0][0]
    assert "expired_lease_session_still_open" in quarantine_sql
    assert "session.status IN ('starting', 'active', 'draining', 'closing')" in quarantine_sql
    assert "task.status IN ('running', 'queued')" in quarantine_sql

    connection.calls.clear()
    await repository.renew(lease=_parse_claimed_lease(), lease_seconds=300)
    renew_sql = next(sql for sql, _args in connection.calls if "UPDATE qc_device_lease" in sql)
    assert "lease.state = 'recovering'" in renew_sql
    assert "expired_lease_session_still_open" in renew_sql
    assert "task.status IN ('running', 'queued')" in renew_sql


@pytest.mark.asyncio
async def test_release_finished_task_devices_clears_quarantine_for_review_or_canceled() -> None:
    connection = _FakeConnection(lease_row=_lease_row())
    connection.fetch_rows = [{"device_ref": "cloud_phone:qa-a"}]
    repository = PostgresDeviceLeaseRepository(pool=_FakePool(connection))

    released = await repository.release_finished_task_devices(limit=10)

    assert released == 1
    release_sql = connection.calls[0][0]
    assert "task.status IN ('review', 'canceled')" in release_sql
    assert "quarantine_reason = NULL" in release_sql
    assert "state = 'released'" in release_sql
    resource_sql = connection.calls[1][0]
    assert "health_state = 'available'" in resource_sql
    assert connection.calls[1][1] == (["cloud_phone:qa-a"],)


@pytest.mark.asyncio
async def test_postgres_device_pool_recovers_before_claim() -> None:
    from services.api.app.application.device_pool import PostgresDevicePool

    repository = _RecordingLeaseRepository(lease=_parse_claimed_lease())
    pool = PostgresDevicePool(repository)
    claimed = await pool.claim(
        task_id="22222222-2222-2222-2222-222222222222",
        session_id="app:session-1",
        preferred_device_ref="cloud_phone:a",
        lease_seconds=120,
        lease_owner="worker-a",
    )
    assert repository.order == [
        "recover_expired",
        "reclaim_recovering",
        "release_finished_task_devices",
        "claim",
    ]
    assert claimed.device_ref == "cloud_phone:a"


@pytest.mark.asyncio
async def test_device_lease_recovery_once_counts_optional_methods() -> None:
    from services.api.app.application.device_lease_recovery import recover_device_leases_once

    pool = _RecordingLeaseRepository(lease=_parse_claimed_lease())
    summary = await recover_device_leases_once(pool=pool, grace_seconds=30, limit=8)
    assert summary.expired_recovered == 2
    assert summary.terminal_reclaimed == 1
    assert summary.stale_quarantined == 1
    assert summary.finished_released == 1
    assert pool.order == [
        "recover_expired",
        "reclaim_recovering",
        "quarantine_stale_recovering",
        "release_finished_task_devices",
    ]
    assert pool.kwargs[-2]["grace_seconds"] == 30


def test_in_process_preferred_device_is_strict_and_claim_is_idempotent() -> None:
    pool = InProcessDevicePool(slots=["cloud_phone:a", "cloud_phone:b"])
    first = pool.claim(
        task_id="task-1",
        session_id="session-1",
        preferred_device_ref="cloud_phone:a",
        lease_owner="worker-a",
    )

    assert (
        pool.claim(
            task_id="task-1",
            session_id="session-1",
            preferred_device_ref="cloud_phone:a",
            lease_owner="worker-a",
        )
        == first
    )
    fallback = pool.claim(
        task_id="task-2",
        session_id="session-2",
        preferred_device_ref="cloud_phone:a",
    )
    assert fallback.device_ref == "cloud_phone:b"
    with pytest.raises(DevicePoolError) as busy:
        pool.claim(
            task_id="task-3",
            session_id="session-3",
            preferred_device_ref="cloud_phone:a",
        )
    assert busy.value.error_code == "DEVICE_BUSY"
    with pytest.raises(DevicePoolError) as unknown:
        pool.claim(
            task_id="task-4",
            session_id="session-4",
            preferred_device_ref="cloud_phone:missing",
        )
    assert unknown.value.error_code == "DEVICE_RESOURCE_NOT_FOUND"


def test_in_process_expired_lease_enters_recovery_and_cannot_be_reused() -> None:
    pool = InProcessDevicePool(slots=["cloud_phone:a"])
    lease = pool.claim(
        task_id="task-1",
        session_id="session-1",
        lease_seconds=60,
    )
    slot = pool._slots[lease.device_ref]
    slot.lease = lease.__class__(
        lease_id=lease.lease_id,
        device_ref=lease.device_ref,
        task_id=lease.task_id,
        session_id=lease.session_id,
        claimed_at=lease.claimed_at - timedelta(seconds=120),
        expires_at=datetime.now(UTC) - timedelta(seconds=1),
        backend=lease.backend,
        profile_id=lease.profile_id,
        lease_token=lease.lease_token,
        lease_owner=lease.lease_owner,
        fencing_token=lease.fencing_token,
        state=lease.state,
    )

    assert pool.recover_expired() == 1
    with pytest.raises(DevicePoolError, match="无可用设备"):
        pool.claim(task_id="task-2", session_id="session-2")
    recovered = pool.recover(lease=lease)
    assert recovered.state == "recovering"
    assert pool.quarantine(lease=recovered, reason="provider_close_unknown") is True
    assert pool.status()["available"] == 0


def test_in_process_recovering_lease_renews_for_the_same_owner() -> None:
    pool = InProcessDevicePool(slots=["cloud_phone:a"])
    lease = pool.claim(task_id="task-1", session_id="session-1", lease_seconds=60)
    slot = pool._slots[lease.device_ref]
    slot.lease = lease.__class__(
        lease_id=lease.lease_id,
        device_ref=lease.device_ref,
        task_id=lease.task_id,
        session_id=lease.session_id,
        claimed_at=lease.claimed_at - timedelta(seconds=120),
        expires_at=datetime.now(UTC) - timedelta(seconds=1),
        backend=lease.backend,
        profile_id=lease.profile_id,
        lease_token=lease.lease_token,
        lease_owner=lease.lease_owner,
        fencing_token=lease.fencing_token,
        state="recovering",
    )

    renewed = pool.renew(lease=lease, lease_seconds=300)

    assert renewed.state == "active"
    assert renewed.expires_at > datetime.now(UTC)
    with pytest.raises(DevicePoolError, match="无可用设备"):
        pool.claim(task_id="task-2", session_id="session-2")


def test_in_process_expired_lease_cannot_be_released_as_healthy() -> None:
    pool = InProcessDevicePool(slots=["cloud_phone:a"])
    lease = pool.claim(task_id="task-1", session_id="session-1", lease_seconds=60)
    slot = pool._slots[lease.device_ref]
    slot.lease = lease.__class__(
        lease_id=lease.lease_id,
        device_ref=lease.device_ref,
        task_id=lease.task_id,
        session_id=lease.session_id,
        claimed_at=lease.claimed_at - timedelta(seconds=120),
        expires_at=datetime.now(UTC) - timedelta(seconds=1),
        backend=lease.backend,
        profile_id=lease.profile_id,
        lease_token=lease.lease_token,
        lease_owner=lease.lease_owner,
        fencing_token=lease.fencing_token,
        state=lease.state,
    )

    assert pool.release(lease=lease) is False
    assert pool.status()["leased"][0]["state"] == "recovering"


@pytest.mark.asyncio
async def test_device_lease_guard_turns_renew_failure_into_fencing_error() -> None:
    pool = InProcessDevicePool(slots=["cloud_phone:a"])
    lease = pool.claim(task_id="task-1", session_id="session-1", lease_seconds=60)
    guard = DeviceLeaseGuard(
        pool=pool,
        lease=lease,
        lease_seconds=60,
        renew_interval_seconds=1,
    )

    await guard.start()
    assert guard.lease.state == "active"
    pool.quarantine(lease=guard.lease, reason="provider_failed")
    await asyncio.sleep(1.05)
    with pytest.raises(DeviceLeaseLostError):
        await guard.assert_healthy()
    with pytest.raises(DeviceLeaseLostError):
        await guard.stop()


def test_in_process_claim_binds_morelogin_env_id_from_slot_mapping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ALIOTH_DEVICE_POOL_MORELOGIN_ENV_IDS", "env-a,env-b")
    monkeypatch.delenv("ALIOTH_MORELOGIN_CLOUDPHONE_ID", raising=False)
    pool = InProcessDevicePool(slots=["cloud_phone:a", "cloud_phone:b"])
    first = pool.claim(task_id="task-1", session_id="session-1", lease_seconds=120)
    second = pool.claim(task_id="task-2", session_id="session-2", lease_seconds=120)
    assert {first.morelogin_env_id, second.morelogin_env_id} == {"env-a", "env-b"}
    assert first.morelogin_env_id != second.morelogin_env_id


@pytest.mark.asyncio
async def test_release_device_lease_triggers_auto_power_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ALIOTH_DEVICE_POOL_MORELOGIN_ENV_IDS", "env-a,env-b")
    monkeypatch.setenv(
        "ALIOTH_DEVICE_POOL_SLOTS",
        "cloud_phone:a,cloud_phone:b",
    )
    monkeypatch.delenv("ALIOTH_MORELOGIN_ADB_AUTO_POWER_OFF", raising=False)
    pool = InProcessDevicePool(slots=["cloud_phone:a", "cloud_phone:b"])
    lease = pool.claim(task_id="task-1", session_id="session-1", lease_seconds=120)
    assert lease.morelogin_env_id == "env-a"

    called_single: list[str] = []
    called_fleet: list[set[str] | None] = []

    def _fake_single(*, backend: str, morelogin_env_id: str, reason: str = "") -> list[str]:
        called_single.append(morelogin_env_id)
        return [f"morelogin powerOff envId={morelogin_env_id}"]

    def _fake_fleet(
        *,
        skip_env_ids: set[str] | frozenset[str] | None = None,
        reason: str = "",
    ) -> list[str]:
        called_fleet.append(set(skip_env_ids or set()))
        return ["morelogin fleet power-off start count=1"]

    async def _fake_get_pool() -> InProcessDevicePool:
        return pool

    monkeypatch.setattr(
        "services.api.app.application.device_pool.get_runtime_device_pool",
        _fake_get_pool,
    )
    monkeypatch.setattr(
        "services.api.app.infrastructure.device.morelogin_adb.maybe_power_off_morelogin_after_lease",
        _fake_single,
    )
    monkeypatch.setattr(
        "services.api.app.infrastructure.device.morelogin_adb.maybe_power_off_morelogin_fleet_idle",
        _fake_fleet,
    )
    assert await release_device_lease(lease=lease) is True
    assert called_single == ["env-a"]
    assert called_fleet and called_fleet[0] == set()


@pytest.mark.asyncio
async def test_release_device_lease_defers_power_off_when_queue_pending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ALIOTH_DEVICE_POOL_MORELOGIN_ENV_IDS", "env-a,env-b")
    monkeypatch.setenv(
        "ALIOTH_DEVICE_POOL_SLOTS",
        "cloud_phone:a,cloud_phone:b",
    )
    monkeypatch.delenv("ALIOTH_MORELOGIN_ADB_AUTO_POWER_OFF", raising=False)
    pool = InProcessDevicePool(slots=["cloud_phone:a", "cloud_phone:b"])
    lease = pool.claim(task_id="task-1", session_id="session-1", lease_seconds=120)

    called_single: list[str] = []
    called_fleet: list[set[str] | None] = []

    async def _fake_get_pool() -> InProcessDevicePool:
        return pool

    async def _fake_pending(*, exclude_task_id: str | None) -> int:
        assert exclude_task_id == "task-1"
        return 2

    monkeypatch.setattr(
        "services.api.app.application.device_pool.get_runtime_device_pool",
        _fake_get_pool,
    )
    monkeypatch.setattr(
        "services.api.app.application.device_pool._count_pending_app_device_demand",
        _fake_pending,
    )
    monkeypatch.setattr(
        "services.api.app.infrastructure.device.morelogin_adb.maybe_power_off_morelogin_after_lease",
        lambda **kwargs: called_single.append(kwargs["morelogin_env_id"]) or [],
    )
    monkeypatch.setattr(
        "services.api.app.infrastructure.device.morelogin_adb.maybe_power_off_morelogin_fleet_idle",
        lambda **kwargs: called_fleet.append(set(kwargs.get("skip_env_ids") or set())) or [],
    )
    assert await release_device_lease(lease=lease) is True
    assert called_single == []
    assert called_fleet == []


@pytest.mark.asyncio
async def test_postgres_count_pending_app_device_demand() -> None:
    connection = _FakeConnection(lease_row=_lease_row())
    connection.fetchval_result = 3
    repository = PostgresDeviceLeaseRepository(pool=_FakePool(connection))

    count = await repository.count_pending_app_device_demand(
        exclude_task_id="11111111-1111-1111-1111-111111111111",
    )
    assert count == 3
    sql, args = connection.calls[-1]
    assert "COUNT(DISTINCT task.id)" in sql
    assert "target_platform = 'app'" in sql
    assert args == ("11111111-1111-1111-1111-111111111111",)


@pytest.mark.asyncio
async def test_maybe_power_off_cloud_phone_for_lease_swallows_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lease = DeviceLease(
        lease_id="lease-1",
        device_ref="cloud_phone:a",
        task_id="task-1",
        session_id="session-1",
        claimed_at=datetime.now(UTC),
        expires_at=datetime.now(UTC) + timedelta(seconds=60),
        backend="cloud_phone",
        profile_id="a",
        morelogin_env_id="env-a",
    )

    def _boom(*, backend: str, morelogin_env_id: str, reason: str = "") -> list[str]:
        raise RuntimeError("boom")

    monkeypatch.setattr(
        "services.api.app.infrastructure.device.morelogin_adb.maybe_power_off_morelogin_after_lease",
        _boom,
    )
    await maybe_power_off_cloud_phone_for_lease(lease=lease, reason="unit")


@pytest.mark.asyncio
async def test_postgres_claim_returns_capabilities_env_id() -> None:
    row = _lease_row()
    connection = _FakeConnection(lease_row=row)
    connection.available_resource = {
        "device_ref": "cloud_phone:a",
        "backend": "cloud_phone",
        "profile_id": "a",
        "fencing_epoch": 0,
        "capabilities": {"vendor": "morelogin", "env_id": "1716966721435348"},
    }
    repository = PostgresDeviceLeaseRepository(pool=_FakePool(connection))

    claimed = await repository.claim(
        task_id=row["task_id"],
        session_id=row["session_id"],
        preferred_device_ref=row["device_ref"],
        lease_owner=row["lease_owner"],
        lease_seconds=120,
    )
    assert claimed.morelogin_env_id == "1716966721435348"
    select_sql = next(
        item[0] for item in connection.calls if "FROM qc_device_resource AS resource" in item[0]
    )
    assert "resource.capabilities" in select_sql


@pytest.mark.asyncio
async def test_postgres_list_inventory_projects_occupancy_and_queue() -> None:
    connection = _FakeConnection(lease_row=_lease_row())
    connection.fetch_rows = [
        {
            "device_ref": "cloud_phone:qa-a",
            "backend": "cloud_phone",
            "profile_id": "qa-a",
            "enabled": True,
            "health_state": "available",
            "capabilities": {"vendor": "morelogin", "env_id": "env-a"},
            "lease_task_id": "22222222-2222-2222-2222-222222222222",
            "lease_session_id": "session-a",
            "lease_state": "active",
            "lease_expires_at": datetime.now(UTC) + timedelta(hours=1),
            "queued_preferred_count": 2,
        },
        {
            "device_ref": "cloud_phone:qa-b",
            "backend": "cloud_phone",
            "profile_id": "qa-b",
            "enabled": True,
            "health_state": "available",
            "capabilities": {"vendor": "morelogin", "env_id": "env-b"},
            "lease_task_id": None,
            "lease_session_id": None,
            "lease_state": None,
            "lease_expires_at": None,
            "queued_preferred_count": 0,
        },
    ]
    repository = PostgresDeviceLeaseRepository(pool=_FakePool(connection))

    items = await repository.list_inventory()

    assert len(items) == 2
    assert items[0].profile_id == "qa-a"
    assert items[0].occupied is True
    assert items[0].occupancy_status == "occupied"
    assert items[0].queued_preferred_count == 2
    assert items[0].morelogin_env_id == "env-a"
    assert items[1].occupancy_status == "available"
    assert items[1].occupied is False
    assert any("preferred_waiting" in sql for sql, _ in connection.calls)


@pytest.mark.asyncio
async def test_postgres_configure_resources_writes_morelogin_capabilities(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ALIOTH_DEVICE_POOL_SLOTS", "cloud_phone:qa-a,cloud_phone:qa-b")
    monkeypatch.setenv("ALIOTH_DEVICE_POOL_MORELOGIN_ENV_IDS", "id-a,id-b")
    connection = _FakeConnection(lease_row=_lease_row())
    repository = PostgresDeviceLeaseRepository(pool=_FakePool(connection))

    count = await repository.configure_resources()
    assert count == 2
    inserts = [item for item in connection.calls if "INSERT INTO qc_device_resource" in item[0]]
    assert len(inserts) == 2
    assert '"env_id": "id-a"' in inserts[0][1][3] or '"env_id": "id-a"' in inserts[1][1][3]
    assert '"env_id": "id-b"' in inserts[0][1][3] or '"env_id": "id-b"' in inserts[1][1][3]
    assert "capabilities" in inserts[0][0]
