"""取消关闭期间的版本竞争只重试状态 CAS，不重复关闭真实设备。"""

from contextlib import asynccontextmanager
from dataclasses import replace

import pytest

from services.api.app.application import app_exploration_closure
from services.api.app.domain.interaction import SurfaceKind, SurfaceSessionStatus
from services.api.app.infrastructure.db.surface_session_repository import (
    PostgresSurfaceSessionRepository,
    SurfaceSessionPersistenceError,
)
from services.api.tests.test_app_exploration_closure import _service, _snapshot


class _MissingRowConnection:
    @asynccontextmanager
    async def transaction(self):
        yield self

    async def fetchrow(self, *args):
        return None


class _MissingRowPool:
    @asynccontextmanager
    async def acquire(self):
        yield _MissingRowConnection()


class _RacingSessions:
    def __init__(self, *, conflicts=0, conflict_change=None, failure=None):
        self.snapshot = _snapshot(SurfaceSessionStatus.ACTIVE, 1)
        self.conflicts = conflicts
        self.conflict_change = conflict_change
        self.failure = failure
        self.attempt_versions = []

    async def get(self, *, session_id):
        return self.snapshot

    async def mark_lost(self, **kwargs):
        self.attempt_versions.append(kwargs["expected_row_version"])
        if self.failure:
            raise self.failure
        stale = kwargs["expected_row_version"] != self.snapshot.row_version
        if self.conflicts:
            self.conflicts -= 1
            self.snapshot = replace(self.snapshot, row_version=self.snapshot.row_version + 1)
            if self.conflict_change:
                self.snapshot = self.conflict_change(self.snapshot)
            stale = True
        if stale:
            # 使用真实仓储的 CAS miss 分支，避免伪造错误类型掩盖层间契约。
            repository = PostgresSurfaceSessionRepository(pool=_MissingRowPool())
            return await repository.mark_lost(**kwargs)
        self.snapshot = replace(
            self.snapshot,
            status=SurfaceSessionStatus.LOST,
            current_surface_ref=None,
            row_version=self.snapshot.row_version + 1,
        )
        return self.snapshot


def _loss_service(sessions, *, during_close=None):
    events = []
    service, _ = _service(events=events)
    service._sessions = sessions

    class Driver:
        async def close_session(self, **kwargs):
            events.append("close")
            if during_close:
                sessions.snapshot = during_close(sessions.snapshot)

    service._driver = Driver()
    return service, events


async def _mark_lost(service):
    return await service.mark_lost(
        task_id="task-1",
        session_id="session-1",
        expected_surface_kind=SurfaceKind.ANDROID,
        expected_build_id="build-1",
        reason="runtime_cancelled",
    )


@pytest.mark.asyncio
async def test_loss_reads_fresh_version_after_driver_close():
    sessions = _RacingSessions()
    service, events = _loss_service(
        sessions, during_close=lambda item: replace(item, row_version=2)
    )

    result = await _mark_lost(service)

    assert result.status is SurfaceSessionStatus.LOST
    assert sessions.attempt_versions == [2]
    assert events == ["close"]


@pytest.mark.asyncio
async def test_loss_retries_two_real_cas_conflicts_without_reclosing_driver():
    sessions = _RacingSessions(conflicts=2)
    service, events = _loss_service(sessions)

    result = await _mark_lost(service)

    assert result.status is SurfaceSessionStatus.LOST
    assert sessions.attempt_versions == [1, 2, 3]
    assert events == ["close"]


@pytest.mark.asyncio
async def test_loss_stops_after_three_cas_attempts():
    sessions = _RacingSessions(conflicts=10)
    service, events = _loss_service(sessions)

    with pytest.raises(SurfaceSessionPersistenceError):
        await _mark_lost(service)

    assert sessions.attempt_versions == [1, 2, 3]
    assert events == ["close"]


@pytest.mark.asyncio
async def test_loss_returns_terminal_committed_during_last_cas_conflict():
    sessions = _RacingSessions(
        conflicts=3,
        conflict_change=lambda item: (
            replace(item, status=SurfaceSessionStatus.CLOSED, current_surface_ref=None)
            if item.row_version == 4
            else item
        ),
    )
    service, events = _loss_service(sessions)

    result = await _mark_lost(service)

    assert result.status is SurfaceSessionStatus.CLOSED
    assert sessions.attempt_versions == [1, 2, 3]
    assert events == ["close"]


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [SurfaceSessionStatus.CLOSED, SurfaceSessionStatus.LOST])
async def test_loss_returns_terminal_state_committed_during_close(status):
    sessions = _RacingSessions()
    service, events = _loss_service(
        sessions,
        during_close=lambda item: replace(
            item, status=status, row_version=2, current_surface_ref=None
        ),
    )

    result = await _mark_lost(service)

    assert result.status is status
    assert sessions.attempt_versions == []
    assert events == ["close"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        {"task_id": "task-other"},
        {"session_id": "session-other"},
        {"platform_build_ref": "build-other"},
        {"surface_kind": SurfaceKind.IOS},
        {"finalization_epoch": 1},
        {"current_surface_ref": "app-surface:other"},
    ],
)
async def test_loss_does_not_mark_rebound_identity_after_close(change):
    sessions = _RacingSessions()
    service, events = _loss_service(
        sessions, during_close=lambda item: replace(item, row_version=2, **change)
    )

    result = await _mark_lost(service)

    assert result is sessions.snapshot
    assert sessions.attempt_versions == []
    assert events == ["close"]


@pytest.mark.asyncio
async def test_loss_rechecks_epoch_on_cas_retry():
    sessions = _RacingSessions(
        conflicts=1, conflict_change=lambda item: replace(item, finalization_epoch=1)
    )
    service, events = _loss_service(sessions)

    result = await _mark_lost(service)

    assert result.finalization_epoch == 1
    assert sessions.attempt_versions == [1]
    assert events == ["close"]


@pytest.mark.asyncio
@pytest.mark.parametrize("during_conflict", [False, True])
@pytest.mark.parametrize(
    "change",
    [
        {"device_ref": "cloud_phone:replacement"},
        {"device_lease_id": "lease-replacement"},
        {"device_lease_fencing_token": 8},
    ],
)
async def test_loss_does_not_settle_rebound_device_lease(monkeypatch, change, during_conflict):
    settlements = []

    async def settle(**kwargs):
        settlements.append(kwargs)

    monkeypatch.setattr(app_exploration_closure, "settle_device_lease_after_session_loss", settle)

    def rebind(item):
        return replace(item, **change)

    sessions = _RacingSessions(
        conflicts=1 if during_conflict else 0,
        conflict_change=rebind if during_conflict else None,
    )
    sessions.snapshot = replace(
        sessions.snapshot, device_lease_id="lease-original", device_lease_fencing_token=7
    )
    service, events = _loss_service(sessions, during_close=None if during_conflict else rebind)

    result = await _mark_lost(service)

    assert result.status is SurfaceSessionStatus.ACTIVE
    assert sessions.attempt_versions == ([1] if during_conflict else [])
    assert settlements == []
    assert events == ["close"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [RuntimeError("database unavailable"), SurfaceSessionPersistenceError("database unavailable")],
)
async def test_loss_does_not_retry_unrelated_repository_failure(failure):
    sessions = _RacingSessions(failure=failure)
    service, events = _loss_service(sessions)

    with pytest.raises(RuntimeError, match="database unavailable"):
        await _mark_lost(service)

    assert sessions.attempt_versions == [1]
    assert events == ["close"]
