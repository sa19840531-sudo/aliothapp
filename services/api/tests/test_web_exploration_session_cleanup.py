"""Web V2 遗留 evidence_pending 动作的专用收敛合同。"""

from types import SimpleNamespace

import pytest

from services.api.app.application.web_exploration_session_cleanup import (
    WebExplorationSessionCleanup,
)
from services.api.app.domain.interaction import SurfaceSessionStatus
from services.api.app.infrastructure.browser.capture_runtime import (
    WebCaptureRuntime,
    _web_exploration_cleanup_enabled,
)
from services.api.app.infrastructure.db.web_exploration_session_cleanup_repository import (
    PostgresWebExplorationSessionCleanupRepository,
)


class RecordingCleanupRepository:
    def __init__(self, *, updated: int = 1) -> None:
        self.updated = updated
        self.calls = []

    async def fail_abandoned_evidence_pending(self, *, task_id, session_id):
        self.calls.append((task_id, session_id))
        return self.updated


@pytest.mark.parametrize(
    ("session_owner", "include_h5", "desktop_web", "expected"),
    (
        ("web-v2-workflow", False, True, True),
        ("web-v2-workflow", True, True, False),
        ("h5-workflow", True, False, False),
        ("app-workflow", False, True, False),
    ),
)
def test_cleanup_injection_is_web_v2_only(
    session_owner: str,
    include_h5: bool,
    desktop_web: bool,
    expected: bool,
) -> None:
    assert _web_exploration_cleanup_enabled(
        session_owner=session_owner,
        include_h5_exploration=include_h5,
        web_desktop_policy_enabled=desktop_web,
    ) is expected


@pytest.mark.asyncio
async def test_web_exploration_cleanup_delegates_only_web_session() -> None:
    repository = RecordingCleanupRepository(updated=2)
    cleanup = WebExplorationSessionCleanup(repository=repository)

    updated = await cleanup.cleanup(
        task_id="11111111-1111-1111-1111-111111111111",
        session_id="web:run-1",
    )

    assert updated == 2
    assert repository.calls == [
        ("11111111-1111-1111-1111-111111111111", "web:run-1")
    ]


@pytest.mark.parametrize("session_id", ("h5:run-1", "session-1", ""))
@pytest.mark.asyncio
async def test_web_exploration_cleanup_rejects_non_web_session(session_id: str) -> None:
    repository = RecordingCleanupRepository()

    with pytest.raises(ValueError, match="web:"):
        await WebExplorationSessionCleanup(repository=repository).cleanup(
            task_id="11111111-1111-1111-1111-111111111111",
            session_id=session_id,
        )

    assert repository.calls == []


class CleanupConnection:
    def __init__(self) -> None:
        self.query = ""
        self.args = ()

    async def fetchval(self, query, *args):
        self.query = query
        self.args = args
        return 1


class AcquireContext:
    def __init__(self, connection) -> None:
        self.connection = connection

    async def __aenter__(self):
        return self.connection

    async def __aexit__(self, exc_type, exc, traceback):
        del exc_type, exc, traceback


class FakePool:
    def __init__(self, connection) -> None:
        self.connection = connection

    def acquire(self):
        return AcquireContext(self.connection)


@pytest.mark.asyncio
async def test_cleanup_sql_is_strictly_scoped_to_draining_web_v2_owner() -> None:
    connection = CleanupConnection()
    repository = PostgresWebExplorationSessionCleanupRepository(
        pool=FakePool(connection)
    )

    updated = await repository.fail_abandoned_evidence_pending(
        task_id="11111111-1111-1111-1111-111111111111",
        session_id="web:run-1",
    )

    assert updated == 1
    assert "session.session_owner = 'web-v2-workflow'" in connection.query
    assert "session.surface_kind = 'web'" in connection.query
    assert "session.status = 'draining'" in connection.query
    assert "session.lease_token IS NULL" in connection.query
    assert "session.session_id LIKE 'web:%'" in connection.query
    assert "action.status = 'evidence_pending'" in connection.query
    assert "status = 'evidence_failed'" in connection.query
    assert "error_code = $3::varchar(128)" in connection.query
    assert "error_message = $4::text" in connection.query
    assert "WEB_EXPLORATION_ABANDONED_EVIDENCE" in connection.args


@pytest.mark.asyncio
async def test_web_runtime_cleans_pending_action_between_draining_and_seal() -> None:
    events: list[str] = []

    class Sessions:
        async def get(self, *, session_id):
            assert session_id == "web:run-1"
            events.append("get")
            return SimpleNamespace(
                task_id="11111111-1111-1111-1111-111111111111",
                status=SurfaceSessionStatus.ACTIVE,
                row_version=1,
            )

        async def begin_draining(self, **kwargs):
            assert kwargs["expected_row_version"] == 1
            events.append("begin_draining")
            return SimpleNamespace(
                task_id=kwargs["task_id"],
                status=SurfaceSessionStatus.DRAINING,
                row_version=2,
            )

        async def seal_exploration_for_close(self, **kwargs):
            assert kwargs["expected_row_version"] == 2
            events.append("seal")
            return SimpleNamespace(
                task_id=kwargs["task_id"],
                status=SurfaceSessionStatus.CLOSING,
                row_version=3,
            )

        async def mark_closed(self, **kwargs):
            assert kwargs["expected_row_version"] == 3
            events.append("mark_closed")

    class Cleanup:
        async def cleanup(self, *, task_id, session_id):
            assert task_id == "11111111-1111-1111-1111-111111111111"
            assert session_id == "web:run-1"
            events.append("cleanup")
            return 1

    class Browser:
        async def close_session(self, session_id):
            assert session_id == "web:run-1"
            events.append("browser_close")

    class Gate:
        async def release_session(self, **kwargs):
            del kwargs
            events.append("release")

    runtime = WebCaptureRuntime(
        coordinator=SimpleNamespace(),
        session_command_gate=Gate(),
        _browser=Browser(),
        _executor=SimpleNamespace(),
        _evidence_buffer=SimpleNamespace(),
        surface_sessions=Sessions(),
        _web_exploration_session_cleanup=Cleanup(),
    )

    await runtime.close_exploration_session(
        task_id="11111111-1111-1111-1111-111111111111",
        session_id="web:run-1",
    )

    assert events == [
        "get",
        "begin_draining",
        "cleanup",
        "seal",
        "browser_close",
        "mark_closed",
        "release",
    ]
