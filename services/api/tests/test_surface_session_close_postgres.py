"""Surface Session 正式关闭排空门禁的真实 PostgreSQL 回归。"""

from __future__ import annotations

from uuid import uuid4

import pytest

from services.api.app.application.surface_sessions import SessionDrainPendingError
from services.api.app.application.web_formal_session_closure import (
    DurableWebFormalSessionCloser,
)
from services.api.app.domain.interaction import (
    SurfaceKind,
    SurfaceSessionStatus,
)
from services.api.app.infrastructure.db.surface_session_repository import (
    PostgresSurfaceSessionRepository,
    SurfaceSessionDrainPendingError,
    SurfaceSessionPersistenceError,
)
from services.api.tests.test_web_lifecycle_recovery_pg import (
    _PassPendingManifestFinalizer,
    _runtime_with_postgres_session,
    _seed_review_capture,
    _start_formal_task,
)
from services.api.tests.test_web_v2_formal_report_e2e import (
    _PostgresCase,
)
from services.api.tests.test_web_v2_formal_report_e2e import (
    postgres_case as postgres_case,
)

_READINESS_COUNTERS = (
    "pending_actions",
    "pending_capture_commits",
    "active_evidence_requests",
    "running_restorations",
    "nonterminal_active_work_items",
    "nonterminal_app_frontier_items",
    "pinned_surfaces",
    "pending_manifests",
    "building_evidence_indexes",
)


def _assert_zero_business_blockers(readiness) -> None:
    assert all(getattr(readiness, field_name) == 0 for field_name in _READINESS_COUNTERS)


async def test_close_readiness_reports_active_closed_and_lost_without_value_error(
    postgres_case: _PostgresCase,
) -> None:
    """业务计数全零时，非 draining 状态也必须返回明确的未就绪快照。"""

    repository = PostgresSurfaceSessionRepository(pool=postgres_case.pool)
    active_task_id, _active_run_id, active_session_id = await _start_formal_task(
        postgres_case,
        with_session=True,
    )
    closed_task_id, _closed_run_id, closed_session_id = await _start_formal_task(
        postgres_case,
        with_session=True,
    )
    lost_task_id, _lost_run_id, lost_session_id = await _start_formal_task(
        postgres_case,
        with_session=True,
    )

    closed_snapshot = await repository.get(session_id=closed_session_id)
    assert closed_snapshot is not None
    draining_snapshot = await repository.begin_draining(
        task_id=closed_task_id,
        session_id=closed_session_id,
        expected_row_version=closed_snapshot.row_version,
    )
    closing_snapshot = await repository.seal_for_close(
        task_id=closed_task_id,
        session_id=closed_session_id,
        expected_row_version=draining_snapshot.row_version,
    )
    await repository.mark_closed(
        task_id=closed_task_id,
        session_id=closed_session_id,
        expected_row_version=closing_snapshot.row_version,
    )

    lost_snapshot = await repository.get(session_id=lost_session_id)
    assert lost_snapshot is not None
    await repository.mark_lost(
        task_id=lost_task_id,
        session_id=lost_session_id,
        expected_row_version=lost_snapshot.row_version,
        reason="test_driver_lost",
    )

    active_readiness = await repository.close_readiness(
        task_id=active_task_id,
        session_id=active_session_id,
    )
    closed_readiness = await repository.close_readiness(
        task_id=closed_task_id,
        session_id=closed_session_id,
    )
    lost_readiness = await repository.close_readiness(
        task_id=lost_task_id,
        session_id=lost_session_id,
    )

    assert [
        active_readiness.session_status,
        closed_readiness.session_status,
        lost_readiness.session_status,
    ] == [
        SurfaceSessionStatus.ACTIVE,
        SurfaceSessionStatus.CLOSED,
        SurfaceSessionStatus.LOST,
    ]
    for readiness in (active_readiness, closed_readiness, lost_readiness):
        assert readiness.ready is False
        assert readiness.has_command_lease is False
        _assert_zero_business_blockers(readiness)


async def test_draining_command_lease_blocks_close_without_leaking_token(
    postgres_case: _PostgresCase,
) -> None:
    """排空中的命令租约只能报告安全计数，不能把租约 token 带出异常。"""

    task_id, _run_id, session_id = await _start_formal_task(
        postgres_case,
        with_session=True,
    )
    repository = PostgresSurfaceSessionRepository(pool=postgres_case.pool)
    active_snapshot = await repository.get(session_id=session_id)
    assert active_snapshot is not None
    await repository.begin_draining(
        task_id=task_id,
        session_id=session_id,
        expected_row_version=active_snapshot.row_version,
    )
    lease_token = str(uuid4())
    permit = await repository.acquire_command(
        task_id=task_id,
        session_id=session_id,
        surface_kind=SurfaceKind.WEB,
        lease_owner="test-close-drain",
        lease_token=lease_token,
        lease_seconds=60,
        allow_draining=True,
    )
    assert permit.lease_token == lease_token
    snapshot = await repository.get(session_id=session_id)
    assert snapshot is not None

    readiness = await repository.close_readiness(
        task_id=task_id,
        session_id=session_id,
    )
    assert readiness.session_status is SurfaceSessionStatus.DRAINING
    assert readiness.has_command_lease is True
    assert readiness.ready is False
    _assert_zero_business_blockers(readiness)

    for seal_method in (repository.seal_for_close, repository.seal_exploration_for_close):
        with pytest.raises(SurfaceSessionPersistenceError, match="版本已变化") as stale:
            await seal_method(
                task_id=task_id,
                session_id=session_id,
                expected_row_version=snapshot.row_version - 1,
            )
        assert not isinstance(stale.value, SessionDrainPendingError)

    with pytest.raises(SurfaceSessionDrainPendingError) as raised:
        await repository.seal_for_close(
            task_id=task_id,
            session_id=session_id,
            expected_row_version=snapshot.row_version,
        )

    error = raised.value
    assert isinstance(error, SessionDrainPendingError)
    assert isinstance(error, SurfaceSessionPersistenceError)
    assert error.task_id == task_id
    assert error.readiness.has_command_lease is True
    assert lease_token not in str(error)
    stored = await postgres_case.connection.fetchrow(
        """
        SELECT status, lease_token::text AS lease_token
        FROM qc_surface_session
        WHERE task_id = $1::uuid AND session_id = $2
        """,
        task_id,
        session_id,
    )
    assert dict(stored) == {
        "status": "draining",
        "lease_token": lease_token,
    }


async def test_formal_close_waits_for_pending_capture_then_retries_to_closed(
    postgres_case: _PostgresCase,
) -> None:
    """正式 Capture 未提交时保留浏览器，清理真实阻塞后同一关闭请求可重试。"""

    task_id, run_id, session_id = await _start_formal_task(
        postgres_case,
        with_session=True,
    )
    capture = await _seed_review_capture(
        postgres_case,
        task_id=task_id,
        run_id=run_id,
        session_id=session_id,
    )
    parent_capture_id = await postgres_case.connection.fetchval(
        "SELECT parent_page_capture_id::text FROM qc_page_capture WHERE id = $1::uuid",
        capture.capture_id,
    )
    assert isinstance(parent_capture_id, str)
    manifest_finalizer = _PassPendingManifestFinalizer(postgres_case)
    await manifest_finalizer.finalize_capture(
        task_id=task_id,
        capture_id=parent_capture_id,
    )
    await manifest_finalizer.finalize_capture(
        task_id=task_id,
        capture_id=capture.capture_id,
    )
    await postgres_case.connection.execute(
        """
        UPDATE qc_page_capture
        SET capture_status = 'pending', updated_at = now()
        WHERE id = $1::uuid
        """,
        capture.capture_id,
    )
    common_capture_status = await postgres_case.connection.fetchval(
        "SELECT capture_status FROM qc_capture WHERE id = $1::uuid",
        capture.capture_id,
    )
    assert common_capture_status == "pending"

    runtime, browser, gate = _runtime_with_postgres_session(postgres_case)
    closer = DurableWebFormalSessionCloser(runtime_factory=lambda: runtime)
    repository = PostgresSurfaceSessionRepository(pool=postgres_case.pool)

    with pytest.raises(SurfaceSessionDrainPendingError) as raised:
        await closer.close(task_id=task_id, session_id=session_id)

    assert raised.value.readiness.pending_capture_commits == 1
    pending_snapshot = await repository.get(session_id=session_id)
    assert pending_snapshot is not None
    assert pending_snapshot.status is SurfaceSessionStatus.DRAINING
    assert browser.closed_sessions == []
    assert gate.released_sessions == []

    await postgres_case.connection.execute(
        """
        UPDATE qc_page_capture
        SET capture_status = 'captured', captured_at = now(), updated_at = now()
        WHERE id = $1::uuid
        """,
        capture.capture_id,
    )
    ready = await repository.close_readiness(
        task_id=task_id,
        session_id=session_id,
    )
    assert ready.ready is True
    assert ready.pending_capture_commits == 0

    await closer.close(task_id=task_id, session_id=session_id)

    closed_snapshot = await repository.get(session_id=session_id)
    assert closed_snapshot is not None
    assert closed_snapshot.status is SurfaceSessionStatus.CLOSED
    assert browser.closed_sessions == [session_id]
    assert gate.released_sessions == [(task_id, session_id)]


async def test_missing_or_wrong_task_is_persistence_error_not_drain_pending(
    postgres_case: _PostgresCase,
) -> None:
    """会话缺失或任务归属错误必须明确报归属错误，不能伪装成排空阻塞。"""

    task_id, _run_id, session_id = await _start_formal_task(
        postgres_case,
        with_session=True,
    )
    repository = PostgresSurfaceSessionRepository(pool=postgres_case.pool)
    missing_session_id = f"web:{uuid4()}"
    wrong_task_id = str(uuid4())

    with pytest.raises(SurfaceSessionPersistenceError) as missing_readiness:
        await repository.close_readiness(
            task_id=task_id,
            session_id=missing_session_id,
        )
    assert not isinstance(missing_readiness.value, SessionDrainPendingError)
    assert "不存在或不属于当前任务" in str(missing_readiness.value)

    with pytest.raises(SurfaceSessionPersistenceError) as wrong_readiness:
        await repository.close_readiness(
            task_id=wrong_task_id,
            session_id=session_id,
        )
    assert not isinstance(wrong_readiness.value, SessionDrainPendingError)
    assert "不存在或不属于当前任务" in str(wrong_readiness.value)

    with pytest.raises(SurfaceSessionPersistenceError) as missing_seal:
        await repository.seal_for_close(
            task_id=task_id,
            session_id=missing_session_id,
            expected_row_version=0,
        )
    assert not isinstance(missing_seal.value, SessionDrainPendingError)
    assert "不存在或不属于当前任务" in str(missing_seal.value)

    with pytest.raises(SurfaceSessionPersistenceError) as wrong_seal:
        await repository.seal_for_close(
            task_id=wrong_task_id,
            session_id=session_id,
            expected_row_version=0,
        )
    assert not isinstance(wrong_seal.value, SessionDrainPendingError)
    assert "不存在或不属于当前任务" in str(wrong_seal.value)
