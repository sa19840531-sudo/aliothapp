"""App Session Command Gate 必须复用 Bootstrap 的稳定身份。"""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from services.api.app.application.surface_sessions import SessionCommandPermit
from services.api.app.domain.interaction import (
    SurfaceKind,
    SurfaceSessionSnapshot,
    SurfaceSessionStatus,
)
from services.api.app.infrastructure.db.session_command_gate import (
    PostgresSessionCommandGate,
)
from services.api.app.infrastructure.db.surface_session_repository import (
    SurfaceSessionPersistenceError,
)


class RecordingSessionRepository:
    def __init__(
        self,
        *,
        existing: SurfaceSessionSnapshot | None = None,
        acquired_current_capture_id: str | None = None,
    ) -> None:
        self.registrations: list[dict[str, object]] = []
        self.existing = existing
        self.get_calls: list[str] = []
        self.acquired_current_capture_id = acquired_current_capture_id
        self.released_current_capture_ids: list[str | None] = []

    async def get(self, *, session_id: str):
        self.get_calls.append(session_id)
        return self.existing

    async def register_active(self, **kwargs):
        self.registrations.append(kwargs)

    async def acquire_command(self, **kwargs) -> SessionCommandPermit:
        return SessionCommandPermit(
            task_id=kwargs["task_id"],
            session_id=kwargs["session_id"],
            surface_kind=kwargs["surface_kind"],
            lease_owner=kwargs["lease_owner"],
            lease_token=kwargs["lease_token"],
            command_sequence=1,
            finalization_epoch=0,
            lease_expires_at=datetime.now(UTC) + timedelta(seconds=120),
            current_capture_id=self.acquired_current_capture_id,
        )

    async def renew_command(self, *, permit, lease_seconds):
        del lease_seconds
        return permit

    async def release_command(self, *, permit, current_capture_id):
        del permit
        self.released_current_capture_ids.append(current_capture_id)
        return True


async def test_app_command_gate_reuses_bootstrap_session_identity() -> None:
    repository = RecordingSessionRepository()
    gate = PostgresSessionCommandGate(
        repository=repository,  # type: ignore[arg-type]
        lease_owner="worker-1:app-command",
        session_owner="app-workflow",
        platform_build_ref="app-build:build-1",
        device_ref="cloud_phone:pixel-8",
    )

    async with gate.hold(
        task_id="11111111-1111-1111-1111-111111111111",
        session_id="app:11111111-1111-1111-1111-111111111111",
        surface_kind=SurfaceKind.ANDROID,
    ):
        pass

    registration = repository.registrations[0]
    spec = registration["spec"]
    assert registration["session_owner"] == "app-workflow"
    assert registration["primary_surface_ref"] == (
        "app-surface:app:11111111-1111-1111-1111-111111111111"
    )
    assert spec.platform_build_ref == "app-build:build-1"
    assert spec.device_ref == "cloud_phone:pixel-8"


def _app_session_snapshot(**changes: object) -> SurfaceSessionSnapshot:
    now = datetime.now(UTC)
    snapshot = SurfaceSessionSnapshot(
        session_id="app:11111111-1111-1111-1111-111111111111",
        task_id="11111111-1111-1111-1111-111111111111",
        surface_kind=SurfaceKind.ANDROID,
        session_owner="app-workflow",
        status=SurfaceSessionStatus.ACTIVE,
        command_sequence=1,
        finalization_epoch=0,
        row_version=2,
        account_environment_ref=None,
        platform_build_ref="app-build:build-1",
        device_ref="cloud_phone:pixel-8",
        current_surface_ref=(
            "app-surface:app:11111111-1111-1111-1111-111111111111"
        ),
        current_capture_id=None,
        started_at=now,
        updated_at=now,
        device_lease_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        device_lease_fencing_token=7,
    )
    return replace(snapshot, **changes)


async def test_app_command_gate_reuses_existing_session_without_reregistering() -> None:
    repository = RecordingSessionRepository(existing=_app_session_snapshot())
    gate = PostgresSessionCommandGate(
        repository=repository,  # type: ignore[arg-type]
        lease_owner="worker-1:app-command",
        session_owner="app-workflow",
        platform_build_ref="app-build:build-1",
        device_ref="cloud_phone:pixel-8",
    )

    async with gate.hold(
        task_id="11111111-1111-1111-1111-111111111111",
        session_id="app:11111111-1111-1111-1111-111111111111",
        surface_kind=SurfaceKind.ANDROID,
    ):
        pass

    assert repository.get_calls == [
        "app:11111111-1111-1111-1111-111111111111"
    ]
    assert repository.registrations == []


async def test_command_gate_does_not_republish_acquired_cursor_for_noop_command() -> None:
    repository = RecordingSessionRepository(
        existing=_app_session_snapshot(
            current_capture_id="33333333-3333-3333-3333-333333333333"
        ),
        acquired_current_capture_id="33333333-3333-3333-3333-333333333333",
    )
    gate = PostgresSessionCommandGate(
        repository=repository,  # type: ignore[arg-type]
        lease_owner="worker-1:read-only-command",
        session_owner="app-workflow",
        platform_build_ref="app-build:build-1",
        device_ref="cloud_phone:pixel-8",
    )

    async with gate.hold(
        task_id="11111111-1111-1111-1111-111111111111",
        session_id="app:11111111-1111-1111-1111-111111111111",
        surface_kind=SurfaceKind.ANDROID,
    ) as permit:
        assert permit.current_capture_id == "33333333-3333-3333-3333-333333333333"

    assert repository.released_current_capture_ids == [None]


async def test_command_gate_releases_new_cursor_recorded_by_command() -> None:
    repository = RecordingSessionRepository(
        existing=_app_session_snapshot(
            current_capture_id="33333333-3333-3333-3333-333333333333"
        ),
        acquired_current_capture_id="33333333-3333-3333-3333-333333333333",
    )
    gate = PostgresSessionCommandGate(
        repository=repository,  # type: ignore[arg-type]
        lease_owner="worker-1:capture-command",
        session_owner="app-workflow",
        platform_build_ref="app-build:build-1",
        device_ref="cloud_phone:pixel-8",
    )

    async with gate.hold(
        task_id="11111111-1111-1111-1111-111111111111",
        session_id="app:11111111-1111-1111-1111-111111111111",
        surface_kind=SurfaceKind.ANDROID,
    ) as permit:
        permit.record_current_capture("44444444-4444-4444-4444-444444444444")

    assert repository.released_current_capture_ids == [
        "44444444-4444-4444-4444-444444444444"
    ]


@pytest.mark.parametrize(
    ("field_name", "drifted_value"),
    [
        ("task_id", "22222222-2222-2222-2222-222222222222"),
        ("surface_kind", SurfaceKind.WEB),
        ("session_owner", "other-workflow"),
        ("platform_build_ref", "app-build:other"),
        ("device_ref", "cloud_phone:other"),
    ],
)
async def test_app_command_gate_rejects_existing_identity_drift(
    field_name: str,
    drifted_value: object,
) -> None:
    repository = RecordingSessionRepository(
        existing=_app_session_snapshot(**{field_name: drifted_value})
    )
    gate = PostgresSessionCommandGate(
        repository=repository,  # type: ignore[arg-type]
        lease_owner="worker-1:app-command",
        session_owner="app-workflow",
        platform_build_ref="app-build:build-1",
        device_ref="cloud_phone:pixel-8",
    )

    with pytest.raises(SurfaceSessionPersistenceError, match="Session"):
        async with gate.hold(
            task_id="11111111-1111-1111-1111-111111111111",
            session_id="app:11111111-1111-1111-1111-111111111111",
            surface_kind=SurfaceKind.ANDROID,
        ):
            pass

    assert repository.registrations == []


@pytest.mark.asyncio
async def test_web_command_gate_preserves_existing_h5_recovery_owner() -> None:
    from services.api.app.domain.interaction.surface_sessions import (
        SurfaceSessionSnapshot,
        SurfaceSessionStatus,
    )

    class Repository(RecordingSessionRepository):
        async def get(self, *, session_id):
            return SurfaceSessionSnapshot(
                session_id=session_id,
                task_id="11111111-1111-1111-1111-111111111111",
                surface_kind=SurfaceKind.WEB,
                session_owner=(
                    "h5-browser-recovery:"
                    "66666666-6666-6666-6666-666666666666"
                ),
                status=SurfaceSessionStatus.ACTIVE,
                command_sequence=1,
                finalization_epoch=0,
                row_version=2,
                account_environment_ref=None,
                platform_build_ref=None,
                device_ref=None,
                current_surface_ref="web-surface:recovery",
                current_capture_id=None,
                started_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )

    repository = Repository()
    gate = PostgresSessionCommandGate(
        repository=repository,  # type: ignore[arg-type]
        lease_owner="worker-1:web-command",
        session_owner="interaction-runtime",
    )

    async with gate.hold(
        task_id="11111111-1111-1111-1111-111111111111",
        session_id=(
            "h5:66666666-6666-6666-6666-666666666666:recovery:"
            "0123456789abcdefabcd"
        ),
        surface_kind=SurfaceKind.WEB,
    ):
        pass

    assert repository.registrations == []


@pytest.mark.asyncio
async def test_inprocess_cancel_session_blocks_subsequent_hold() -> None:
    from services.api.app.application.capture_coordinator import (
        InProcessSessionCommandGate,
        SessionCommandCanceledError,
    )

    gate = InProcessSessionCommandGate()
    task_id = "task-1"
    session_id = "session-1"

    async with gate.hold(
        task_id=task_id,
        session_id=session_id,
        surface_kind=SurfaceKind.ANDROID,
    ) as permit:
        assert permit.lease_owner == "in-process"

    await gate.cancel_session(
        task_id=task_id,
        session_id=session_id,
        reason="user_canceled",
    )

    with pytest.raises(SessionCommandCanceledError, match="user_canceled"):
        async with gate.hold(
            task_id=task_id,
            session_id=session_id,
            surface_kind=SurfaceKind.ANDROID,
        ):
            pass


@pytest.mark.asyncio
async def test_inprocess_hold_does_not_swallow_command_exception() -> None:
    from services.api.app.application.capture_coordinator import InProcessSessionCommandGate

    gate = InProcessSessionCommandGate()

    with pytest.raises(RuntimeError, match="动作失败"):
        async with gate.hold(task_id="task-1", session_id="session-1"):
            raise RuntimeError("动作失败")

    assert gate._users == {}
    await gate.release_session(task_id="task-1", session_id="session-1")


@pytest.mark.asyncio
async def test_postgres_cancel_session_marks_lost_and_clears_local_binding() -> None:
    from services.api.app.application.capture_coordinator import (
        SessionCommandCanceledError,
    )
    from services.api.app.domain.interaction.surface_sessions import (
        SurfaceSessionSnapshot,
        SurfaceSessionStatus,
    )

    class _Repo(RecordingSessionRepository):
        def __init__(self) -> None:
            super().__init__()
            self.lost: list[dict[str, object]] = []
            self._snapshot = SurfaceSessionSnapshot(
                session_id="app:session-1",
                task_id="11111111-1111-1111-1111-111111111111",
                surface_kind=SurfaceKind.ANDROID,
                session_owner="app-workflow",
                status=SurfaceSessionStatus.ACTIVE,
                command_sequence=2,
                finalization_epoch=0,
                row_version=3,
                account_environment_ref=None,
                platform_build_ref="app-build:build-1",
                device_ref="cloud_phone:pixel-8",
                current_surface_ref="app-surface:app:session-1",
                current_capture_id=None,
                started_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )

        async def get(self, *, session_id: str):
            assert session_id == self._snapshot.session_id
            return self._snapshot

        async def mark_lost(self, **kwargs):
            self.lost.append(kwargs)
            self._snapshot = SurfaceSessionSnapshot(
                session_id=self._snapshot.session_id,
                task_id=self._snapshot.task_id,
                surface_kind=self._snapshot.surface_kind,
                session_owner=self._snapshot.session_owner,
                status=SurfaceSessionStatus.LOST,
                command_sequence=self._snapshot.command_sequence,
                finalization_epoch=self._snapshot.finalization_epoch,
                row_version=self._snapshot.row_version + 1,
                account_environment_ref=None,
                platform_build_ref=self._snapshot.platform_build_ref,
                device_ref=self._snapshot.device_ref,
                current_surface_ref=None,
                current_capture_id=None,
                started_at=self._snapshot.started_at,
                updated_at=datetime.now(UTC),
            )
            return self._snapshot

    repository = _Repo()
    gate = PostgresSessionCommandGate(
        repository=repository,  # type: ignore[arg-type]
        lease_owner="worker-1:app-command",
        session_owner="app-workflow",
        platform_build_ref="app-build:build-1",
        device_ref="cloud_phone:pixel-8",
    )

    await gate.cancel_session(
        task_id="11111111-1111-1111-1111-111111111111",
        session_id="app:session-1",
        reason="run_canceled",
    )

    assert repository.lost[0]["reason"] == "run_canceled"
    assert repository.lost[0]["expected_row_version"] == 3
    with pytest.raises(SessionCommandCanceledError, match="run_canceled"):
        async with gate.hold(
            task_id="11111111-1111-1111-1111-111111111111",
            session_id="app:session-1",
            surface_kind=SurfaceKind.ANDROID,
        ):
            pass


@pytest.mark.asyncio
async def test_mobile_demo_observe_holds_session_command_gate(
    tmp_path: Path,
) -> None:
    """排期第 13 列：observe 必须经 SessionCommandGate，与动作串行。"""
    from services.api.app.application.app_screenshot_artifacts import (
        AppScreenshotArtifactService,
    )
    from services.api.app.application.capture_coordinator import (
        InProcessSessionCommandGate,
        SessionCommandCanceledError,
    )
    from services.api.app.infrastructure.device.adb_ops import AdbDeviceOps
    from services.api.app.infrastructure.device.android_capture_runtime import (
        build_mobile_demo_android_runtime,
        start_mobile_demo_android_runtime,
    )
    from services.api.app.infrastructure.storage.local_evidence_store import (
        LocalEvidenceArtifactStore,
    )

    class _FakeOps(AdbDeviceOps):
        def __init__(self) -> None:
            super().__init__(
                backend="fake",
                width=1080,
                height=1920,
                _log=lambda _m: None,
                _screencap_fn=self._screencap,
                _shell_fn=lambda *_a, **_k: None,
            )

        def _screencap(self, dest: Path) -> None:
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 120)

    ops = _FakeOps()
    svc = AppScreenshotArtifactService(
        store=LocalEvidenceArtifactStore(root=tmp_path / "ev"),
        index_root=tmp_path / "idx",
    )
    runtime = build_mobile_demo_android_runtime(
        task_id="task-demo",
        session_id="session-demo",
        backend="fake",
        shots_dir=tmp_path / "shots",
        ops=ops,
        artifact_service=svc,
    )
    await start_mobile_demo_android_runtime(runtime)
    assert isinstance(runtime.session_command_gate, InProcessSessionCommandGate)

    out = tmp_path / "observe.png"
    capture_id = await runtime.observe_to_file(out)
    assert capture_id
    assert out.is_file()

    await runtime.cancel(reason="demo_stop")
    with pytest.raises(SessionCommandCanceledError, match="demo_stop"):
        await runtime.observe_to_file(tmp_path / "after_cancel.png")
