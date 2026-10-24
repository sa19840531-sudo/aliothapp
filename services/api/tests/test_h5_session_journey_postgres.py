"""独立数据库中的整段互斥、控制连接丢失和关闭竞争。"""

import asyncio
import os
from contextlib import suppress
from uuid import uuid4

import asyncpg
import pytest

from services.api.app.application.capture_coordinator import InProcessSessionCommandGate
from services.api.app.infrastructure.db.h5_session_journey_gate import PostgresH5SessionJourneyGate
from services.api.app.infrastructure.db.surface_session_repository import (
    PostgresSurfaceSessionRepository,
    SurfaceSessionDrainPendingError,
    SurfaceSessionPersistenceError,
)
from services.api.app.infrastructure.db.test_database_guard import (
    require_isolated_test_database_url,
)
from services.api.tests.test_exploration_scheduler_facts_postgres_integration import (
    _upgrade_isolated_postgres,  # noqa: F401 - 显式隔离库迁移夹具
)
from services.api.tests.test_h5_closed_session_recapture_postgres import _closed_request


@pytest.fixture
async def case():
    _url()
    async with _closed_request(check_constraint=False, isolation="read_committed") as (
        connection, pool, ids, *_
    ):
        yield connection, pool, ids["task"]


def _url():
    value = os.getenv("ALIOTH_TEST_DATABASE_URL")
    if not value:
        pytest.skip("需显式隔离数据库")
    return require_isolated_test_database_url(value, os.getenv("ALIOTH_DATABASE_URL"))


def _gate():
    return PostgresH5SessionJourneyGate(
        delegate=InProcessSessionCommandGate(), database_url=_url(),
    )


@pytest.mark.parametrize("isolation", ["read_committed", "repeatable_read"])
async def test_close_availability_requires_fresh_read_committed_snapshot(isolation):
    async with _closed_request(check_constraint=False, isolation=isolation) as (connection, *_):
        if isolation == "repeatable_read":
            with pytest.raises(asyncpg.SerializationError, match="关闭快照不是read committed"):
                await connection.fetchval("SELECT alioth_h5_journey_available($1)", f"h5:{uuid4()}")
        else:
            assert await connection.fetchval(
                "SELECT alioth_h5_journey_available($1)", f"h5:{uuid4()}",
            )


async def test_independent_executors_serialize_and_cancel_waiter_without_leaking_lock():
    first, second = _gate(), _gate()
    session = f"h5:journey-{uuid4()}"
    entered = asyncio.Event()

    async def waiting():
        async with second.hold_journey(task_id="task", session_id=session):
            entered.set()

    async with first.hold_journey(task_id="task", session_id=session):
        waiter = asyncio.create_task(waiting())
        await asyncio.sleep(0.05)
        assert not entered.is_set()
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
    await asyncio.wait_for(waiting(), 2)
    assert entered.is_set()


async def test_control_connection_loss_stops_owner_before_next_action():
    gate = _gate()
    session = f"h5:journey-{uuid4()}"
    entered, stopped = asyncio.Event(), asyncio.Event()
    actions = []

    async def owner():
        async with gate.hold_journey(task_id="task", session_id=session):
            entered.set()
            try:
                await asyncio.Event().wait()
                actions.append("unsafe-late-action")
            finally:
                stopped.set()

    task = asyncio.create_task(owner())
    connection = await asyncpg.connect(_url())
    try:
        await entered.wait()
        key = await connection.fetchval(
            "SELECT hashtextextended($1,0)", "h5-session-journey:" + session,
        )
        pid = await connection.fetchval(
            "SELECT pid FROM pg_locks WHERE locktype='advisory' AND granted "
            "AND classid=$1::oid AND objid=$2::oid AND objsubid=1",
            (key >> 32) & 0xffffffff, key & 0xffffffff,
        )
        assert pid is not None
        assert await connection.fetchval("SELECT pg_terminate_backend($1)", pid)
        with pytest.raises(RuntimeError, match="失效"):
            await asyncio.wait_for(task, 2)
        assert stopped.is_set()
        assert actions == []
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError, RuntimeError):
            await task
        await connection.close()


@pytest.mark.parametrize("exploration", [False, True])
async def test_close_cannot_seal_between_commands_in_active_journey(case, exploration):
    connection, pool, task = case
    session = f"h5:journey-{uuid4()}"
    await connection.execute(
        "INSERT INTO qc_surface_session(session_id,task_id,surface_kind,session_owner,status,"
        "finalization_epoch,activated_at,draining_at) "
        "VALUES($1,$2::uuid,'web','h5-workflow','draining',0,now(),now())",
        session, task,
    )
    repository = PostgresSurfaceSessionRepository(pool=pool)
    seal = repository.seal_exploration_for_close if exploration else repository.seal_for_close
    snapshot = await repository.get(session_id=session)
    async with _gate().hold_journey(task_id=task, session_id=session):
        with pytest.raises(SurfaceSessionDrainPendingError):
            await seal(task_id=task, session_id=session, expected_row_version=snapshot.row_version)
    result = await seal(task_id=task, session_id=session, expected_row_version=snapshot.row_version)
    assert result.status.value == "closing"


async def test_drain_cannot_start_halfway_through_journey(case):
    connection, pool, task = case
    session = f"h5:journey-{uuid4()}"
    await connection.execute(
        "INSERT INTO qc_surface_session(session_id,task_id,surface_kind,session_owner,status,"
        "finalization_epoch,activated_at) VALUES($1,$2::uuid,'web','h5-workflow','active',0,now())",
        session, task,
    )
    repository = PostgresSurfaceSessionRepository(pool=pool)
    snapshot = await repository.get(session_id=session)
    async with _gate().hold_journey(task_id=task, session_id=session):
        with pytest.raises(SurfaceSessionPersistenceError):
            await repository.begin_draining(
                task_id=task, session_id=session, expected_row_version=snapshot.row_version,
            )
    result = await repository.begin_draining(
        task_id=task, session_id=session, expected_row_version=snapshot.row_version,
    )
    assert result.status.value == "draining"
