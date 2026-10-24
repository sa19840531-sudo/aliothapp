"""隔离数据库复现同事实永久休眠及lost误当物理排空，不连接业务库。"""

import importlib
import os
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import asyncpg
import pytest

from services.api.app.infrastructure.db.h5_run_failure_repository import (
    PostgresH5RunFailureFactsRepository,
)
from services.api.app.infrastructure.db.task_finalization_repository import (
    PostgresTaskFinalizationRepository,
)
from services.api.tests.test_worker_stalled_finalization_postgres import (
    _command,
    _service,
)
from services.api.tests.test_worker_stalled_finalization_postgres import (
    pg_connection as pg_connection,
)
from services.api.tests.test_worker_stalled_finalization_postgres import (
    stalled_case as stalled_case,
)
from services.api.tests.test_workstation_task_list_postgres import _ConnectionPool

pytestmark = pytest.mark.skipif(
    not os.getenv("ALIOTH_TEST_DATABASE_URL"),
    reason="必须显式提供隔离数据库",
)


async def _lost(connection, ids):
    session = f"h5:{ids['run']}"
    await connection.execute(
        "INSERT INTO qc_surface_session(session_id,task_id,surface_kind,session_owner,status,"
        "finalization_epoch,activated_at,lost_at,failure_reason) "
        "VALUES($1,$2::uuid,'web','h5-workflow','lost',0,now(),now(),'ACTION_TIMED_OUT')",
        session,
        ids["task"],
    )
    return session


async def test_saved_review_with_infinity_still_gets_a_bounded_terminal_audit(
    pg_connection, stalled_case
):
    ids = stalled_case
    await pg_connection.execute(
        "UPDATE qc_task SET status='review',finalization_retry_at='infinity' WHERE id=$1::uuid",
        ids["task"],
    )
    repository = PostgresTaskFinalizationRepository(pool=_ConnectionPool(pg_connection))
    assert any(
        row.task_id == ids["task"] for row in await repository.list_recoverable_finalizations()
    )


async def test_lost_without_stop_proof_cannot_be_called_drained(pg_connection, stalled_case):
    ids = stalled_case
    await _lost(pg_connection, ids)
    assert (
        await pg_connection.fetchval(
            "SELECT status IN ('closed','lost','failed') "
            "FROM qc_surface_session WHERE task_id=$1::uuid",
            ids["task"],
        )
        is True
    )
    with pytest.raises(RuntimeError, match="NOT_DRAINED"):
        await PostgresH5RunFailureFactsRepository(
            pool=_ConnectionPool(pg_connection)
        ).assert_drained(task_id=ids["task"], agent_run_id=ids["run"])


async def test_stalled_lost_without_stop_proof_cannot_seal(pg_connection, stalled_case):
    ids = stalled_case
    await _lost(pg_connection, ids)
    before = await pg_connection.fetchrow("SELECT * FROM qc_task WHERE id=$1::uuid", ids["task"])
    result = await _service(pg_connection).terminate_stalled(
        _command(ids), minimum_terminal_age_seconds=600
    )
    assert result is None
    assert (
        await pg_connection.fetchrow("SELECT * FROM qc_task WHERE id=$1::uuid", ids["task"])
        == before
    )


def proof(session):
    return {
        "session_id": session,
        "resource_id": str(uuid4()),
        "browser_pid": 1234567,
        "method": "owned_browser_close_ack_and_pid_absent",
        "driver_stop_ack": True,
        "observed_at": datetime.now(UTC).isoformat(),
    }


async def test_bound_receipt_allows_review_technical_seal_not_quality_pass(
    pg_connection, stalled_case
):
    from services.api.app.infrastructure.db.surface_session_repository import (
        PostgresSurfaceSessionRepository,
    )

    ids = stalled_case
    session = await _lost(pg_connection, ids)
    repository = PostgresSurfaceSessionRepository(pool=_ConnectionPool(pg_connection))
    receipt = proof(session)
    before = await pg_connection.fetchrow(
        "SELECT * FROM qc_surface_session WHERE session_id=$1", session
    )
    await repository.record_browser_stop(session_id=session, proof=receipt)
    await repository.record_browser_stop(session_id=session, proof=receipt)
    assert await repository.read_browser_stop(session_id=session) == receipt
    assert (
        await pg_connection.fetchrow(
            "SELECT * FROM qc_surface_session WHERE session_id=$1", session
        )
        == before
    )
    await pg_connection.execute("UPDATE qc_task SET status='review' WHERE id=$1::uuid", ids["task"])
    result = await _service(pg_connection).terminate_stalled(
        _command(ids), minimum_terminal_age_seconds=600
    )
    assert result is not None and result.state.value == "sealed"
    assert result.execution_status.value == "failed"
    assert (
        await pg_connection.fetchval(
            "SELECT gate_passed FROM qc_completion_gate_snapshot WHERE task_id=$1::uuid",
            ids["task"],
        )
        is False
    )
    assert (
        await pg_connection.fetchval(
            "SELECT count(*) FROM qc_report_gate_binding WHERE task_id=$1::uuid", ids["task"]
        )
        == 0
    )


@pytest.mark.parametrize("drift", ["epoch", "owner", "command"])
async def test_stop_receipt_cannot_cross_epoch_owner_or_command(pg_connection, stalled_case, drift):
    from services.api.app.infrastructure.db.surface_session_repository import (
        PostgresSurfaceSessionRepository,
    )

    ids = stalled_case
    session = await _lost(pg_connection, ids)
    repository = PostgresSurfaceSessionRepository(pool=_ConnectionPool(pg_connection))
    receipt = proof(session)
    await repository.record_browser_stop(session_id=session, proof=receipt)
    if drift == "epoch":
        await pg_connection.execute(
            "UPDATE qc_task SET finalization_epoch=1 WHERE id=$1::uuid", ids["task"]
        )
    elif drift == "owner":
        other = str(uuid4())
        await pg_connection.execute(
            "INSERT INTO qc_agent_run(id,task_id,agent_type,agent_name,status) "
            "VALUES($1::uuid,$2::uuid,'main','other','failed')",
            other,
            ids["task"],
        )
        await pg_connection.execute(
            "UPDATE qc_task SET finalization_agent_run_id=$2::uuid WHERE id=$1::uuid",
            ids["task"],
            other,
        )
    else:
        await pg_connection.execute(
            "UPDATE qc_surface_session SET command_sequence=command_sequence+1 WHERE session_id=$1",
            session,
        )
    assert await repository.read_browser_stop(session_id=session) is None
    with pytest.raises(RuntimeError):
        await repository.record_browser_stop(session_id=session, proof=receipt)


async def test_original_browser_proof_survives_runtime_to_database(pg_connection, stalled_case):
    from playwright.async_api import async_playwright

    from services.api.app.infrastructure.browser.capture_runtime import WebCaptureRuntime
    from services.api.app.infrastructure.browser.h5_physical_stop import H5BrowserStopGuard
    from services.api.app.infrastructure.browser.page_capture import (
        PlaywrightPageCaptureBrowser,
        _PlaywrightSession,
    )
    from services.api.app.infrastructure.db.surface_session_repository import (
        PostgresSurfaceSessionRepository,
    )

    ids = stalled_case
    session_id = await _lost(pg_connection, ids)
    repository = PostgresSurfaceSessionRepository(pool=_ConnectionPool(pg_connection))
    browser = PlaywrightPageCaptureBrowser()
    async with async_playwright() as playwright:
        original = await playwright.chromium.launch(headless=True)
        guard = await H5BrowserStopGuard.bind(original, owned_local=True, playwright=playwright)
        browser._sessions[session_id] = _PlaywrightSession(
            playwright=playwright, browser=original, context=None, page=None, stop_guard=guard
        )
        runtime = WebCaptureRuntime(
            coordinator=None,
            session_command_gate=None,
            _browser=browser,
            _executor=None,
            _evidence_buffer=None,
            surface_sessions=repository,
        )
        try:
            await runtime.close_browser_session(session_id)
            await runtime.close_browser_session(session_id)
            stored = await repository.read_browser_stop(session_id=session_id)
            assert stored["browser_pid"] == guard.browser_pid
            assert stored["resource_id"] == guard.resource_id
            assert not original.is_connected()
            assert runtime._pending_browser_stop_receipts == {}
        finally:
            await original.close()


async def test_recovery_stop_before_finalization_owner_is_bound(pg_connection, stalled_case):
    from services.api.app.infrastructure.db.surface_session_repository import (
        PostgresSurfaceSessionRepository,
    )

    ids = stalled_case
    session = await _lost(pg_connection, ids)
    await pg_connection.execute(
        "UPDATE qc_task SET finalization_agent_run_id=NULL WHERE id=$1::uuid", ids["task"]
    )
    repository = PostgresSurfaceSessionRepository(pool=_ConnectionPool(pg_connection))
    receipt = proof(session)
    await repository.record_browser_stop(session_id=session, proof=receipt)
    assert await repository.read_browser_stop(session_id=session) == receipt
    assert (
        await pg_connection.fetchval(
            "SELECT owner_agent_run_id::text FROM qc_h5_browser_stop_receipt WHERE session_id=$1",
            session,
        )
        == ids["run"]
    )


async def test_browser_stopped_but_journey_inflight_cannot_seal(pg_connection, stalled_case):
    from services.api.app.infrastructure.db.surface_session_repository import (
        PostgresSurfaceSessionRepository,
    )

    ids = stalled_case
    session = await _lost(pg_connection, ids)
    await PostgresSurfaceSessionRepository(pool=_ConnectionPool(pg_connection)).record_browser_stop(
        session_id=session, proof=proof(session)
    )
    await pg_connection.execute(
        "INSERT INTO qc_h5_session_journey_guard(token,task_id,session_id,status) "
        "VALUES($1::uuid,$2,$3,'active')",
        str(uuid4()),
        ids["task"],
        session,
    )
    with pytest.raises(RuntimeError, match="RESOURCES_NOT_DRAINED"):
        await _service(pg_connection).terminate(_command(ids))
    assert (
        await _service(pg_connection).terminate_stalled(
            _command(ids), minimum_terminal_age_seconds=600
        )
        is None
    )
    assert (
        await pg_connection.fetchval(
            "SELECT count(*) FROM qc_completion_gate_snapshot WHERE task_id=$1::uuid", ids["task"]
        )
        == 0
    )


@pytest.mark.parametrize("invalid", ["recent", "active", "canceled", "quarantined"])
async def test_infinity_audit_does_not_steal_live_or_revoked_owner(
    pg_connection, stalled_case, invalid
):
    ids = stalled_case
    await pg_connection.execute(
        "UPDATE qc_task SET status='review',finalization_retry_at='infinity' WHERE id=$1::uuid",
        ids["task"],
    )
    if invalid == "recent":
        await pg_connection.execute(
            "UPDATE qc_agent_run SET finished_at=now() WHERE id=$1::uuid", ids["run"]
        )
    elif invalid == "active":
        await pg_connection.execute(
            "UPDATE qc_agent_run SET status='running' WHERE id=$1::uuid", ids["run"]
        )
    elif invalid == "canceled":
        await pg_connection.execute(
            "UPDATE qc_agent_run SET output_context=jsonb_build_object('cancel_requested',true) "
            "WHERE id=$1::uuid",
            ids["run"],
        )
    else:
        await pg_connection.execute(
            "UPDATE qc_task SET finalization_recovery_state='quarantined',"
            "finalization_error_signature='fixture-error',finalization_same_error_count=3,"
            "finalization_quarantined_at=now() WHERE id=$1::uuid",
            ids["task"],
        )
    found = await PostgresTaskFinalizationRepository(
        pool=_ConnectionPool(pg_connection)
    ).list_recoverable_finalizations()
    assert all(row.task_id != ids["task"] for row in found)


@pytest.mark.parametrize("operation", ["overwrite", "downgrade", "empty_proof"])
async def test_stop_receipt_database_never_overwrites_or_erases_proof(
    pg_connection,
    stalled_case,
    monkeypatch,
    operation,
):
    from services.api.app.infrastructure.db.surface_session_repository import (
        PostgresSurfaceSessionRepository,
    )

    ids = stalled_case
    session = await _lost(pg_connection, ids)
    repository = PostgresSurfaceSessionRepository(pool=_ConnectionPool(pg_connection))
    await repository.record_browser_stop(session_id=session, proof=proof(session))
    before = await pg_connection.fetchrow(
        "SELECT * FROM qc_h5_browser_stop_receipt WHERE session_id=$1", session
    )
    statements = []
    if operation == "downgrade":
        module = importlib.import_module(
            "services.api.migrations.versions.20261019_0142_h5_browser_stop_receipt"
        )
        monkeypatch.setattr(module, "op", SimpleNamespace(execute=statements.append))
        module.downgrade()
    with pytest.raises(asyncpg.CheckViolationError):
        async with pg_connection.transaction():
            if operation == "overwrite":
                await pg_connection.execute(
                    "UPDATE qc_h5_browser_stop_receipt SET proof=proof WHERE session_id=$1", session
                )
            elif operation == "downgrade":
                await pg_connection.execute(statements[0])
            else:
                await pg_connection.execute(
                    "INSERT INTO qc_h5_browser_stop_receipt(session_id,task_id,owner_agent_run_id,"
                    "finalization_epoch,command_sequence,resource_id,proof) "
                    "VALUES($1,$2::uuid,$3::uuid,0,0,$4::uuid,'{}'::jsonb)",
                    session,
                    ids["task"],
                    ids["run"],
                    str(uuid4()),
                )
    assert (
        await pg_connection.fetchrow(
            "SELECT * FROM qc_h5_browser_stop_receipt WHERE session_id=$1", session
        )
        == before
    )


async def test_driver_jsonb_decoding_does_not_change_saved_reason_semantics(pg_connection):
    import json

    from services.api.app.infrastructure.db.page_graph_repository import _row_dict
    from services.api.tests.test_h5_draining_viewer_case import case

    saved = case()["reason_codes"]
    raw = await pg_connection.fetchrow("SELECT $1::jsonb AS reason_codes", saved)
    assert _row_dict(raw)["reason_codes"] == json.loads(saved)
