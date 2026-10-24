"""关闭快照必须同时表达状态、命令租约及每类排空阻塞。"""

from dataclasses import replace

import pytest

from services.api.app.application.surface_sessions import (
    SessionCloseReadiness,
    SessionDrainPendingError,
)
from services.api.app.domain.interaction import SurfaceSessionStatus
from services.api.app.infrastructure.db.surface_session_repository import (
    PostgresSurfaceSessionRepository,
    SurfaceSessionDrainPendingError,
    SurfaceSessionPersistenceError,
)
from services.api.tests.test_surface_session_repository import (
    ExplorationCloseConnection,
    FakePool,
)


def ready_snapshot(**changes):
    return replace(
        SessionCloseReadiness(
            session_id="web:run-1", ready=True, pending_actions=0,
            pending_capture_commits=0, active_evidence_requests=0,
            running_restorations=0, nonterminal_active_work_items=0, pinned_surfaces=0,
        ),
        **changes,
    )


@pytest.mark.parametrize("status", list(SurfaceSessionStatus))
@pytest.mark.parametrize("leased", [False, True])
def test_readiness_requires_draining_and_no_command_lease(status, leased):
    expected = status is SurfaceSessionStatus.DRAINING and not leased
    snapshot = ready_snapshot(ready=expected, session_status=status, has_command_lease=leased)
    assert snapshot.ready is expected
    with pytest.raises(ValueError, match="ready 与会话状态"):
        replace(snapshot, ready=not expected)


@pytest.mark.parametrize("counter", [
    "pending_actions", "pending_capture_commits", "active_evidence_requests",
    "running_restorations", "nonterminal_active_work_items", "pinned_surfaces",
    "nonterminal_app_frontier_items", "pending_manifests", "building_evidence_indexes",
])
@pytest.mark.parametrize("method", ["seal_for_close", "seal_exploration_for_close"])
async def test_seal_preserves_each_blocker_and_never_writes_closing(counter, method):
    class BlockedConnection(ExplorationCloseConnection):
        async def fetchrow(self, query, *args):
            row = await super().fetchrow(query, *args)
            if "AS ready" in query:
                row.update(ready=False, **{counter: 2})
            return row

    connection = BlockedConnection()
    repository = PostgresSurfaceSessionRepository(pool=FakePool(connection))
    with pytest.raises(SurfaceSessionDrainPendingError) as caught:
        await getattr(repository, method)(
            task_id="task-1", session_id="app-session-1", expected_row_version=1,
        )
    assert isinstance(caught.value, SessionDrainPendingError)
    assert isinstance(caught.value, SurfaceSessionPersistenceError)
    assert caught.value.diagnostics["task_id"] == "task-1"
    assert caught.value.diagnostics["session_id"] == "app-session-1"
    assert caught.value.diagnostics[counter] == 2
    assert f'"{counter}": 2' in str(caught.value)
    assert len(connection.calls) == 1
    assert "FOR UPDATE OF session" in connection.calls[0][1]


@pytest.mark.parametrize("method", ["seal_for_close", "seal_exploration_for_close"])
async def test_stale_version_is_not_hidden_by_pending_blockers(method):
    class BlockedConnection(ExplorationCloseConnection):
        async def fetchrow(self, query, *args):
            row = await super().fetchrow(query, *args)
            row.update(ready=False, pending_actions=1)
            return row

    connection = BlockedConnection()
    repository = PostgresSurfaceSessionRepository(pool=FakePool(connection))
    with pytest.raises(SurfaceSessionPersistenceError, match="版本已变化") as caught:
        await getattr(repository, method)(
            task_id="task-1", session_id="app-session-1", expected_row_version=0,
        )
    assert not isinstance(caught.value, SessionDrainPendingError)
    assert len(connection.calls) == 1
    assert "FOR UPDATE OF session" in connection.calls[0][1]


@pytest.mark.parametrize("method", ["seal_for_close", "seal_exploration_for_close"])
async def test_missing_session_is_identity_error_not_pending(method):
    class MissingConnection(ExplorationCloseConnection):
        async def fetchrow(self, query, *args):
            await super().fetchrow(query, *args)
            return None

    connection = MissingConnection()
    repository = PostgresSurfaceSessionRepository(pool=FakePool(connection))
    with pytest.raises(SurfaceSessionPersistenceError, match="不存在或不属于") as caught:
        await getattr(repository, method)(
            task_id="task-1", session_id="other-session", expected_row_version=1,
        )
    assert not isinstance(caught.value, SessionDrainPendingError)
    assert len(connection.calls) == 1
