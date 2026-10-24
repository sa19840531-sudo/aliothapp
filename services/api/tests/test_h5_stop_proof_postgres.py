"""只在新隔离库验证原旅程停止证明、写回围栏和持续容量。"""

import asyncio
import json
import os
import time
from contextlib import suppress
from uuid import uuid4

import asyncpg
import pytest

from services.api.app.application.h5_session_journey import (
    bind_h5_physical_resource,
    quarantine_h5_journeys,
)
from services.api.app.infrastructure.browser.h5_physical_stop import H5BrowserStopGuard
from services.api.tests.test_exploration_scheduler_facts_postgres_integration import (
    _upgrade_isolated_postgres,  # noqa: F401 - 显式隔离库迁移夹具
)
from services.api.tests.test_h5_session_journey_postgres import _gate, _url
from services.api.tests.test_h5_uncertain_journey_postgres import _disconnect


@pytest.mark.parametrize("fault", ["none", "wrong_token", "pending"])
async def test_real_stop_proof_only_releases_exact_old_guard(fault, tmp_path):
    from playwright.async_api import async_playwright

    gate = _gate()
    session, task_id = "h5:proven-" + uuid4().hex, "isolated-proof"
    connection = await asyncpg.connect(_url())
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        resource = await H5BrowserStopGuard.bind(browser, owned_local=True)
        try:
            with pytest.raises(RuntimeError):
                async with gate.hold_journey(task_id=task_id, session_id=session):
                    bind_h5_physical_resource(session, resource)
                    quarantine_h5_journeys("original-disconnect")
            original = await connection.fetchrow(
                "SELECT * FROM qc_h5_session_journey_guard WHERE session_id=$1", session,
            )
            assert original["status"] == "quarantined"
            if fault == "wrong_token":
                gate._quarantined[session].token = str(uuid4())
            if fault == "pending":
                finish = asyncio.Event()
                pending = asyncio.create_task(finish.wait())
                gate._quarantined[session].operations.add(pending)
            try:
                if fault == "none":
                    proof = await gate.resolve_quarantine(task_id=task_id, session_id=session)
                    (tmp_path / "stop-proof.json").write_text(json.dumps(proof), encoding="utf-8")
                    row = await connection.fetchrow(
                        "SELECT * FROM qc_h5_session_journey_guard WHERE token=$1",
                        original["token"],
                    )
                    assert row["status"] == "drained"
                    assert row["reason_code"] == "original-disconnect"
                    assert json.loads(row["physical_stop_proof"]) == proof
                    async with _gate().hold_journey(task_id=task_id, session_id=session):
                        pass
                else:
                    with pytest.raises(RuntimeError):
                        await gate.resolve_quarantine(task_id=task_id, session_id=session)
                    assert await connection.fetchval(
                        "SELECT status FROM qc_h5_session_journey_guard WHERE token=$1",
                        original["token"],
                    ) == "quarantined"
                    with pytest.raises(RuntimeError, match="未排空|未确认"):
                        async with _gate().hold_journey(task_id=task_id, session_id=session):
                            pytest.fail("错误旅程或在途操作不能解封")
            finally:
                if fault == "pending":
                    finish.set()
                    await pending
        finally:
            await browser.close()
            await connection.close()


async def test_control_disconnect_can_settle_only_after_owned_browser_stops(tmp_path):
    from playwright.async_api import Error, async_playwright

    gate, session = _gate(), "h5:disconnect-proof-" + uuid4().hex
    started = asyncio.Event()
    control = await asyncpg.connect(_url())
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        resource = await H5BrowserStopGuard.bind(browser, owned_local=True)
        page = await browser.new_page()
        await page.set_content("<main>真实在途，关闭后才停止</main>")

        async def owner():
            async with gate.hold_journey(task_id="proof", session_id=session):
                bind_h5_physical_resource(session, resource)
                started.set()
                await asyncio.Event().wait()

        running = asyncio.create_task(owner())
        try:
            await started.wait()
            await _disconnect(control, session)
            with pytest.raises(RuntimeError):
                await running
            assert browser.is_connected(), "数据库连接丢失本身不证明浏览器已停止"
            proof = await gate.resolve_quarantine(task_id="proof", session_id=session)
            assert not browser.is_connected()
            with pytest.raises(Error, match="closed"):
                await page.evaluate("1")
            (tmp_path / "disconnect-proof.json").write_text(json.dumps(proof), encoding="utf-8")
            assert await control.fetchval(
                "SELECT count(*) FROM qc_h5_session_journey_guard "
                "WHERE session_id=$1 AND status IN ('active','quarantined')", session,
            ) == 0
        finally:
            running.cancel()
            with suppress(RuntimeError, asyncio.CancelledError):
                await running
            await browser.close()
            await control.close()


async def test_cancelled_python_await_does_not_prove_cdp_side_effect_stopped():
    from playwright.async_api import async_playwright

    gate, session = _gate(), "h5:cancel-cdp-" + uuid4().hex
    control = await asyncpg.connect(_url())
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        resource = await H5BrowserStopGuard.bind(browser, owned_local=True)
        page = await browser.new_page()
        await page.set_content("<script>window.effects=0</script>")

        async def owner():
            async with gate.hold_journey(task_id="cancel-proof", session_id=session):
                bind_h5_physical_resource(session, resource)
                await page.evaluate("""() => new Promise(() => {
                  window.pendingStarted=true;setTimeout(() => {window.effects=1},100);
                })""")

        running = asyncio.create_task(owner())
        try:
            await page.wait_for_function("window.pendingStarted===true")
            running.cancel()
            with pytest.raises(asyncio.CancelledError):
                await running
            await page.wait_for_function("window.effects===1")
            assert await page.evaluate("window.effects") == 1
            assert await control.fetchval(
                "SELECT status FROM qc_h5_session_journey_guard WHERE session_id=$1", session,
            ) == "quarantined", "Python取消返回时，CDP执行仍能产生效果，不能冒充排空"
            await gate.resolve_quarantine(task_id="cancel-proof", session_id=session)
        finally:
            running.cancel()
            with suppress(RuntimeError, asyncio.CancelledError):
                await running
            await browser.close()
            await control.close()


async def test_sustained_capacity_bounds_control_connections_and_retains_history(tmp_path):
    gate, run = _gate(), uuid4().hex
    control = await asyncpg.connect(_url())
    duration = float(os.getenv("H5_CAPACITY_SOAK_SECONDS", "2"))
    started = time.monotonic()
    active = peak = completed = 0
    db_peak = total_backend_peak = 0
    waits = []

    async def actor(index):
        nonlocal active, peak, completed
        cycle = 0
        while cycle < 3 or time.monotonic() - started < duration:
            session = f"h5:capacity-{run}-{index}-{cycle}"
            waiting = time.monotonic()
            async with gate.hold_journey(task_id=run, session_id=session):
                waits.append(time.monotonic() - waiting)
                active += 1
                peak = max(peak, active)
                try:
                    await asyncio.sleep(0.04)
                finally:
                    active -= 1
            completed += 1
            cycle += 1

    actors = [asyncio.create_task(actor(index)) for index in range(64)]
    try:
        while any(not task.done() for task in actors):
            activity = await control.fetch(
                "SELECT pid,backend_type,state,query,backend_start,state_change "
                "FROM pg_stat_activity "
                "WHERE datname=current_database() "
                "AND pid <> pg_backend_pid()",
            )
            clients = [row for row in activity if row["backend_type"] == "client backend"]
            db_peak = max(db_peak, len(clients))
            total_backend_peak = max(total_backend_peak, len(activity))
            if len(clients) > 16:
                (tmp_path / "connection-overflow.json").write_text(
                    json.dumps([dict(row) for row in activity], default=str, indent=2),
                    encoding="utf-8",
                )
            await asyncio.sleep(0.03)
        await asyncio.gather(*actors)
        assert peak <= 16 and db_peak <= 16
        assert gate._locks == gate._active == gate._lock_users == {}
        assert not gate._quarantined
        pending = await control.fetchval(
            "SELECT count(*) FROM qc_h5_session_journey_guard "
            "WHERE task_id=$1 AND status <> 'drained'", run,
        )
        retained = await control.fetchval(
            "SELECT count(*) FROM qc_h5_session_journey_guard WHERE task_id=$1", run,
        )
        assert pending == 0 and retained == completed
        assert await control.fetchval(
            "SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() "
            "AND pid <> pg_backend_pid() AND backend_type='client backend'",
        ) == 0
        waits.sort()
        report = {
            "logical_concurrency": 64, "control_connection_limit": 16,
            "elapsed_seconds": time.monotonic() - started, "completed": completed,
            "retained_guard_rows": retained, "pending": pending,
            "active_peak": peak, "database_connection_peak": db_peak,
            "total_backend_peak_including_autovacuum": total_backend_peak,
            "queue_p95_seconds": waits[int(len(waits) * .95)], "queue_max_seconds": max(waits),
            "real_site_acceptance": False, "full_180_minute_soak": False,
        }
        (tmp_path / "capacity.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    finally:
        for task in actors:
            if not task.done():
                task.cancel()
        await asyncio.gather(*actors, return_exceptions=True)
        await control.close()
