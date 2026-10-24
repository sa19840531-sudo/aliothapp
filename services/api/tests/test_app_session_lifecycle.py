"""App Session seal → close / lost，禁止残留 active。"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from services.api.app.application.app_session_lifecycle import close_app_runtime_session
from services.api.app.domain.interaction import (
    SurfaceKind,
    SurfaceSessionSnapshot,
    SurfaceSessionStatus,
)


def _snapshot(*, status: SurfaceSessionStatus, row_version: int = 1) -> SurfaceSessionSnapshot:
    now = datetime.now(UTC)
    return SurfaceSessionSnapshot(
        session_id="app:task-1",
        task_id="11111111-1111-1111-1111-111111111111",
        surface_kind=SurfaceKind.ANDROID,
        session_owner="app-workflow",
        status=status,
        command_sequence=1,
        finalization_epoch=0,
        row_version=row_version,
        account_environment_ref=None,
        platform_build_ref="app-build:build-1",
        device_ref="cloud_phone:pixel-8",
        current_surface_ref="app-surface:app:task-1",
        current_capture_id=None,
        started_at=now,
        updated_at=now,
    )


class _FakeSessions:
    def __init__(self, snapshot: SurfaceSessionSnapshot) -> None:
        self.snapshot = snapshot
        self.calls: list[str] = []

    async def get(self, *, session_id: str):
        assert session_id == self.snapshot.session_id
        return self.snapshot

    async def begin_draining(self, **kwargs):
        del kwargs
        self.calls.append("begin_draining")
        self.snapshot = _replace_status(self.snapshot, SurfaceSessionStatus.DRAINING)
        return self.snapshot

    async def seal_for_close(self, **kwargs):
        del kwargs
        self.calls.append("seal_for_close")
        self.snapshot = _replace_status(self.snapshot, SurfaceSessionStatus.CLOSING)
        return self.snapshot

    async def mark_closed(self, **kwargs):
        del kwargs
        self.calls.append("mark_closed")
        self.snapshot = _replace_status(self.snapshot, SurfaceSessionStatus.CLOSED)
        return self.snapshot

    async def mark_lost(self, **kwargs):
        self.calls.append("mark_lost")
        self.lost_reason = str(kwargs.get("reason") or "")
        self.snapshot = _replace_status(self.snapshot, SurfaceSessionStatus.LOST)
        return self.snapshot


class _FakeDriver:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.closed = 0

    async def close_session(self, *, session, reason):
        del session, reason
        self.closed += 1
        if self.fail:
            raise RuntimeError("device gone")


def _replace_status(
    snapshot: SurfaceSessionSnapshot, status: SurfaceSessionStatus
) -> SurfaceSessionSnapshot:
    return SurfaceSessionSnapshot(
        session_id=snapshot.session_id,
        task_id=snapshot.task_id,
        surface_kind=snapshot.surface_kind,
        session_owner=snapshot.session_owner,
        status=status,
        command_sequence=snapshot.command_sequence,
        finalization_epoch=snapshot.finalization_epoch,
        row_version=snapshot.row_version + 1,
        account_environment_ref=snapshot.account_environment_ref,
        platform_build_ref=snapshot.platform_build_ref,
        device_ref=snapshot.device_ref,
        current_surface_ref=snapshot.current_surface_ref,
        current_capture_id=snapshot.current_capture_id,
        started_at=snapshot.started_at,
        updated_at=datetime.now(UTC),
    )


@pytest.mark.asyncio
async def test_defer_close_keeps_draining_without_lost() -> None:
    sessions = _FakeSessions(_snapshot(status=SurfaceSessionStatus.ACTIVE))
    driver = _FakeDriver()
    status = await close_app_runtime_session(
        driver=driver,
        surface_sessions=sessions,
        task_id=sessions.snapshot.task_id,
        session_id=sessions.snapshot.session_id,
        reason="app_runtime_run_complete",
        defer_close=True,
    )
    assert status == "draining"
    assert sessions.calls == ["begin_draining"]
    assert driver.closed == 0
    assert sessions.snapshot.status is SurfaceSessionStatus.DRAINING


@pytest.mark.asyncio
async def test_close_seals_then_marks_closed() -> None:
    sessions = _FakeSessions(_snapshot(status=SurfaceSessionStatus.ACTIVE))
    driver = _FakeDriver()
    status = await close_app_runtime_session(
        driver=driver,
        surface_sessions=sessions,
        task_id=sessions.snapshot.task_id,
        session_id=sessions.snapshot.session_id,
        reason="app_runtime_run_complete",
    )
    assert status == "closed"
    assert sessions.calls == ["begin_draining", "seal_for_close", "mark_closed"]
    assert driver.closed == 1
    assert sessions.snapshot.status is SurfaceSessionStatus.CLOSED


@pytest.mark.asyncio
async def test_close_failure_marks_lost_not_active() -> None:
    sessions = _FakeSessions(_snapshot(status=SurfaceSessionStatus.ACTIVE))
    driver = _FakeDriver(fail=True)
    status = await close_app_runtime_session(
        driver=driver,
        surface_sessions=sessions,
        task_id="11111111-1111-1111-1111-111111111111",
        session_id="app:task-1",
        reason="app_runtime_run_complete",
    )
    assert status == "lost"
    assert "mark_lost" in sessions.calls
    assert sessions.snapshot.status is SurfaceSessionStatus.LOST
    assert sessions.snapshot.status is not SurfaceSessionStatus.ACTIVE


@pytest.mark.asyncio
async def test_seal_failure_marks_lost() -> None:
    sessions = _FakeSessions(_snapshot(status=SurfaceSessionStatus.ACTIVE))

    async def _boom(**kwargs):
        del kwargs
        sessions.calls.append("seal_for_close")
        raise RuntimeError("lease still held")

    sessions.seal_for_close = _boom  # type: ignore[method-assign]
    status = await close_app_runtime_session(
        driver=_FakeDriver(),
        surface_sessions=sessions,
        task_id="11111111-1111-1111-1111-111111111111",
        session_id="app:task-1",
        reason="app_runtime_run_complete",
    )
    assert status == "lost"
    assert sessions.snapshot.status is SurfaceSessionStatus.LOST
