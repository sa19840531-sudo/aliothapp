"""断连释放连接锁不等于浏览器停止；只在独立测试库注入故障。"""

import asyncio
from contextlib import suppress
from uuid import uuid4

import asyncpg
import pytest

from services.api.app.infrastructure.db.surface_session_repository import (
    PostgresSurfaceSessionRepository,
    SurfaceSessionDrainPendingError,
)
from services.api.tests.test_exploration_scheduler_facts_postgres_integration import (
    _upgrade_isolated_postgres,  # noqa: F401 - 显式隔离库迁移夹具
)
from services.api.tests.test_h5_closed_session_recapture_postgres import _closed_request
from services.api.tests.test_h5_session_journey_postgres import _gate, _url


async def _disconnect(connection, session):
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


@pytest.mark.parametrize("check", ["new_owner", "seal", "exploration_seal"])
@pytest.mark.parametrize("isolation", ["read_committed", "repeatable_read"])
async def test_disconnect_cannot_admit_or_seal_while_old_physical_operation_continues(
    check, isolation,
):
    async with _closed_request(check_constraint=False, isolation=isolation) as (
        connection, pool, ids, *_
    ):
        task_id, session = ids["task"], f"h5:uncertain-{uuid4()}"
        await connection.execute(
            "INSERT INTO qc_surface_session(session_id,task_id,surface_kind,session_owner,"
            "status,finalization_epoch,activated_at,draining_at) "
            "VALUES($1,$2::uuid,'web','h5-workflow','draining',0,now(),now())",
            session, task_id,
        )
        started, cancelled, finish = (asyncio.Event() for _ in range(3))

        async def physical_owner():
            async with _gate().hold_journey(task_id=task_id, session_id=session):
                started.set()
                try:
                    await finish.wait()
                except asyncio.CancelledError:
                    cancelled.set()
                    await finish.wait()

        owner = asyncio.create_task(physical_owner())
        control = await asyncpg.connect(_url())
        try:
            await asyncio.wait_for(started.wait(), 2)
            await _disconnect(control, session)
            await asyncio.wait_for(cancelled.wait(), 2)
            assert not owner.done()
            if check == "new_owner":
                with pytest.raises(RuntimeError, match="未排空|未确认"):
                    async with _gate().hold_journey(task_id=task_id, session_id=session):
                        pytest.fail("连接锁释放不能授权另一执行器切页")
            else:
                repository = PostgresSurfaceSessionRepository(pool=pool)
                snapshot = await repository.get(session_id=session)
                seal = (repository.seal_for_close if check == "seal"
                        else repository.seal_exploration_for_close)
                with pytest.raises((SurfaceSessionDrainPendingError, asyncpg.SerializationError)):
                    await seal(task_id=task_id, session_id=session,
                               expected_row_version=snapshot.row_version)
        finally:
            finish.set()
            with suppress(RuntimeError, asyncio.CancelledError):
                await owner
            await control.close()


async def test_normal_completed_journey_does_not_block_close():
    async with _closed_request(check_constraint=False, isolation="read_committed") as (
        connection, pool, ids, *_
    ):
        session = f"h5:normal-{uuid4()}"
        await connection.execute(
            "INSERT INTO qc_surface_session(session_id,task_id,surface_kind,session_owner,"
            "status,finalization_epoch,activated_at,draining_at) "
            "VALUES($1,$2::uuid,'web','h5-workflow','draining',0,now(),now())",
            session, ids["task"],
        )
        async with _gate().hold_journey(task_id=ids["task"], session_id=session):
            pass
        repository = PostgresSurfaceSessionRepository(pool=pool)
        snapshot = await repository.get(session_id=session)
        result = await repository.seal_for_close(
            task_id=ids["task"], session_id=session, expected_row_version=snapshot.row_version,
        )
        assert result.status.value == "closing"


async def test_real_browser_late_cdp_result_cannot_publish_after_control_disconnect(tmp_path):
    from services.api.app.application.page_capture_persistence import PageCapturePersistenceService
    from services.api.tests.test_page_capture_persistence import (
        FakeEvidenceArtifactStore,
        FakePageCaptureRepository,
        _capture_result,
    )

    playwright_api = pytest.importorskip("playwright.async_api")
    session = f"h5:cdp-unknown-{uuid4()}"
    started, cancelled = asyncio.Event(), asyncio.Event()
    repository, store = FakePageCaptureRepository(), FakeEvidenceArtifactStore()
    persistence = PageCapturePersistenceService(repository=repository, artifact_store=store)
    async with playwright_api.async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.route("**/*", lambda route: route.abort())
        await page.set_content("<main>受控物理在途</main><script>window.effects=0</script>")

        async def owner():
            async with _gate().hold_journey(task_id="cdp-test", session_id=session):
                physical = asyncio.create_task(page.evaluate("""() => new Promise(resolve => {
                    window.releasePhysical = () => {window.effects += 1;resolve(window.effects)};
                })"""))
                await page.wait_for_function("typeof window.releasePhysical === 'function'")
                started.set()
                try:
                    await asyncio.shield(physical)
                except asyncio.CancelledError:
                    cancelled.set()
                    await physical
                await persistence.persist(
                    task_id="cdp-test", agent_run_id="run", result=_capture_result(),
                    surface_session_id=session, session_command_sequence=1, finalization_epoch=0,
                )

        owner_task = asyncio.create_task(owner())
        control = await asyncpg.connect(_url())
        try:
            await asyncio.wait_for(started.wait(), 2)
            await _disconnect(control, session)
            await asyncio.wait_for(cancelled.wait(), 2)
            with pytest.raises(RuntimeError, match="未排空|未确认"):
                async with _gate().hold_journey(task_id="cdp-test", session_id=session):
                    pytest.fail("CDP仍在途，不得进入新旅程")
            await page.evaluate("window.releasePhysical()")
            with pytest.raises(RuntimeError, match="未确认"):
                await asyncio.wait_for(owner_task, 2)
            assert await page.evaluate("window.effects") == 1
            assert repository.calls == [] and store.calls == []
            guard = await control.fetchrow(
                "SELECT status,reason_code FROM qc_h5_session_journey_guard WHERE session_id=$1",
                session,
            )
            assert guard["status"] == "quarantined"
            # 回执或TTL不能自行重置未知在途，保留原诊断及逐次token。
            await control.execute(
                "UPDATE qc_h5_session_journey_guard SET started_at=now()-interval '1 day' "
                "WHERE session_id=$1", session,
            )
            with pytest.raises(RuntimeError, match="未排空|未确认"):
                async with _gate().hold_journey(task_id="cdp-test", session_id=session):
                    pass
            async with _gate().hold_journey(task_id="other", session_id=f"h5:other-{uuid4()}"):
                pass
            (tmp_path / "late-cdp-dom.html").write_text(await page.content(), encoding="utf-8")
            (tmp_path / "late-cdp.png").write_bytes(await page.screenshot(type="png"))
        finally:
            if not owner_task.done():
                await page.evaluate("window.releasePhysical?.()")
            with suppress(RuntimeError, asyncio.CancelledError):
                await owner_task
            await control.close()
            await browser.close()


async def test_unknown_guard_counts_as_gate_resource_without_a_live_session():
    from services.api.app.infrastructure.db.task_finalization_repository import _GATE_FACTS_SQL

    async with _closed_request(check_constraint=False, isolation="read_committed") as (
        connection, _, ids, *_
    ):
        before = await connection.fetchrow(_GATE_FACTS_SQL, ids["task"], "failed")
        await connection.execute(
            "INSERT INTO qc_h5_session_journey_guard(token,task_id,session_id,status,reason_code) "
            "VALUES($1,$2,$3,'quarantined','H5_PHYSICAL_OPERATION_NOT_DRAINED')",
            uuid4(), ids["task"], f"h5:orphan-{uuid4()}",
        )
        after = await connection.fetchrow(_GATE_FACTS_SQL, ids["task"], "failed")
        assert after["background_execution_count"] == before["background_execution_count"] + 1
