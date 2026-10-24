"""真实浏览器、整段互斥、停止回执和最终收口的隔离端到端反例。"""

import asyncio
import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import asyncpg
import pytest
from playwright.async_api import async_playwright

from services.api.app.application.capture_coordinator import InProcessSessionCommandGate
from services.api.app.application.h5_session_journey import (
    bind_h5_physical_resource,
    track_h5_physical_operation,
)
from services.api.app.infrastructure.browser.capture_runtime import WebCaptureRuntime
from services.api.app.infrastructure.browser.h5_physical_stop import H5BrowserStopGuard
from services.api.app.infrastructure.browser.page_capture import (
    PlaywrightPageCaptureBrowser,
    _PlaywrightSession,
)
from services.api.app.infrastructure.db.h5_llm_budget_repository import (
    PostgresH5LLMBudgetRepository,
)
from services.api.app.infrastructure.db.h5_session_journey_gate import PostgresH5SessionJourneyGate
from services.api.app.infrastructure.db.surface_session_repository import (
    PostgresSurfaceSessionRepository,
)
from services.api.app.infrastructure.db.test_database_guard import (
    require_isolated_test_database_url,
)
from services.api.tests.test_h5_uncertain_journey_postgres import _disconnect
from services.api.tests.test_inspection_closure_settlement_postgres import _seed_task
from services.api.tests.test_worker_stalled_finalization_postgres import (
    _candidate,
    _command,
    _service,
)
from services.api.tests.test_workstation_task_list_postgres import _ConnectionPool

pytestmark = pytest.mark.skipif(
    not os.getenv("ALIOTH_TEST_DATABASE_URL"), reason="必须显式提供隔离数据库"
)


@pytest.fixture(scope="module")
def stop_database(tmp_path_factory):
    shared = require_isolated_test_database_url(
        os.environ["ALIOTH_TEST_DATABASE_URL"], os.getenv("ALIOTH_DATABASE_URL")
    )
    name = f"alioth_stop_receipt_test_{uuid4().hex}"

    async def create():
        connection = await asyncpg.connect(shared)
        try:
            before = await connection.fetchrow(
                "SELECT (SELECT count(*) FROM qc_task) AS tasks,"
                "(SELECT count(*) FROM qc_h5_browser_stop_receipt) AS receipts"
            )
            await connection.execute(f'CREATE DATABASE "{name}" TEMPLATE template0')
            return dict(before)
        finally:
            await connection.close()

    before = asyncio.run(create())
    url = urlunsplit(urlsplit(shared)._replace(path="/" + name))
    root = Path(__file__).resolve().parents[3]
    directory = tmp_path_factory.mktemp("stop-receipt-database")
    with (directory / "migration.log").open("w") as log:
        result = subprocess.run(
            [sys.executable, "-m", "alembic", "-c", "services/api/migrations/alembic.ini",
             "upgrade", "head"], cwd=root, env={**os.environ, "ALIOTH_DATABASE_URL": url},
            stdout=log, stderr=subprocess.STDOUT, timeout=90,
        )
    assert result.returncode == 0, (directory / "migration.log").read_text()
    (directory / "identity.json").write_text(json.dumps({"database": name, "shared": before}))
    # 封存记录留在本模块专库，不删除，也不污染后续全局统计或迁移降级回归。
    return {"url": url, "shared": shared, "before": before}


@pytest.mark.parametrize("fault", ["cancel", "disconnect", "supplement_disconnect"])
async def test_original_stop_receipt_drains_before_finalization(fault, tmp_path, stop_database):
    url = stop_database["url"]
    connection = await asyncpg.connect(url)
    ids = {key: str(uuid4()) for key in ("tenant", "user", "ruleset", "rule", "task", "run")}
    session = f"h5:{ids['run']}"
    owner_gate, competitor_gate = (
        PostgresH5SessionJourneyGate(delegate=InProcessSessionCommandGate(), database_url=url)
        for _ in range(2)
    )
    owner = physical = None
    timeline = []

    def record(kind, **data):
        timeline.append({"at": datetime.now(UTC).isoformat(), "kind": kind, **data})

    try:
        async with connection.transaction():
            await _seed_task(connection, ids)
            await connection.execute(
                "UPDATE qc_tenant SET display_name=$2 WHERE id=$1::uuid",
                ids["tenant"], f"Stop E2E {ids['task']}",
            )
        await connection.execute(
            "UPDATE qc_task SET target_platform='h5',finalization_agent_run_id=$2::uuid,"
            "task_config=$3::jsonb WHERE id=$1::uuid",
            ids["task"], ids["run"], json.dumps({"workflow_version": "v2",
                "execution_mode": "full_inspection", "max_actions": 80,
                "h5_inspection_notification_mode": "enforced"}),
        )
        deadline = datetime.now(UTC) + timedelta(minutes=180)
        budget = PostgresH5LLMBudgetRepository(pool=_ConnectionPool(connection))
        await budget.initialize(
            task_id=ids["task"], agent_run_id=ids["run"], max_calls=200, deadline=deadline
        )
        consumer = ids["run"]
        if fault == "supplement_disconnect":
            consumer = str(uuid4())
            await connection.execute(
                "INSERT INTO qc_agent_run(id,task_id,agent_type,agent_name,status) "
                "VALUES($1::uuid,$2::uuid,'rule_judge','isolated-supplement','running')",
                consumer, ids["task"],
            )
        await budget.reserve(task_id=ids["task"], consumer_agent_run_id=consumer)
        await connection.execute(
            "INSERT INTO qc_surface_session(session_id,task_id,surface_kind,session_owner,"
            "status,finalization_epoch,activated_at) "
            "VALUES($1,$2::uuid,'web','h5-workflow','active',0,now())", session, ids["task"]
        )
        await _candidate(connection, ids, "evidence_insufficient")
        surface = PostgresSurfaceSessionRepository(pool=_ConnectionPool(connection))
        adapter = PlaywrightPageCaptureBrowser()
        runtime = WebCaptureRuntime(coordinator=None, session_command_gate=owner_gate,
            _browser=adapter, _executor=None, _evidence_buffer=None, surface_sessions=surface)
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            context = await browser.new_context()
            page = await context.new_page()
            await page.route("**/*", lambda route: route.abort())
            await page.set_content("<main data-view='price'>受控价格与补证页面</main>"
                                   "<script>window.effects=0</script>")
            guard = await H5BrowserStopGuard.bind(
                browser, owned_local=True, playwright=playwright
            )
            adapter._sessions[session] = _PlaywrightSession(
                playwright=playwright, browser=browser, context=context, page=page, stop_guard=guard
            )
            entered = asyncio.Event()

            async def inflight():
                nonlocal physical
                async with owner_gate.hold_journey(task_id=ids["task"], session_id=session):
                    bind_h5_physical_resource(session, guard)
                    physical = asyncio.create_task(page.evaluate("""() => new Promise(() => {
                      window.pendingStarted=true;
                      window.applyLateEffect=()=>{window.effects++;};
                    })"""))
                    track_h5_physical_operation(physical)
                    await page.wait_for_function("window.pendingStarted===true")
                    entered.set()
                    await asyncio.shield(physical)

            owner = asyncio.create_task(inflight())
            try:
                await asyncio.wait_for(entered.wait(), 5)
                record("physical_inflight", browser_pid=guard.browser_pid,
                       resource_id=guard.resource_id,
                       role="supplement" if fault == "supplement_disconnect" else "main")
                if fault == "cancel":
                    owner.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await owner
                else:
                    await _disconnect(connection, session)
                    with pytest.raises(RuntimeError, match="失效"):
                        await owner
                assert browser.is_connected() and not physical.done()
                await page.evaluate("window.applyLateEffect()")
                assert await page.evaluate("window.effects") == 1
                record("late_effect_after_logical_stop")
                (tmp_path / "inflight.html").write_text(await page.content(), encoding="utf-8")
                await page.screenshot(path=str(tmp_path / "inflight.png"))
                # 独立执行器不能因连接锁已释放而抢占，失败准入不应消耗模型预算。
                with pytest.raises(RuntimeError, match="未排空|未确认"):
                    async with competitor_gate.hold_journey(
                        task_id=ids["task"], session_id=session
                    ):
                        await budget.reserve(
                            task_id=ids["task"], consumer_agent_run_id=ids["run"]
                        )
                        pytest.fail("原浏览器仍在途时不得进入补证或主探索")
                await connection.execute(
                    "UPDATE qc_agent_run SET status='failed',finished_at=now(),"
                    "stop_reason='incomplete' WHERE task_id=$1::uuid", ids["task"]
                )
                await connection.execute(
                    "UPDATE qc_task SET status='review' WHERE id=$1::uuid", ids["task"]
                )
                await connection.execute(
                    "UPDATE qc_surface_session SET status='lost',lost_at=now(),"
                    "failure_reason='ISOLATED_INFLIGHT' WHERE session_id=$1", session
                )
                command = _command(ids)
                before = await connection.fetchrow(
                    "SELECT * FROM qc_task WHERE id=$1::uuid", ids["task"]
                )
                with pytest.raises(RuntimeError, match="UNCONFIRMED|NOT_DRAINED"):
                    await _service(connection).terminate(command)
                assert await connection.fetchrow(
                    "SELECT * FROM qc_task WHERE id=$1::uuid", ids["task"]
                ) == before
                assert await surface.read_browser_stop(session_id=session) is None
                record("finalization_blocked_without_proof")
                await runtime.close_browser_session(session)
                stored = await surface.read_browser_stop(session_id=session)
                assert stored["browser_pid"] == guard.browser_pid
                assert stored["resource_id"] == guard.resource_id
                assert stored["driver_stop_ack"] is True
                assert not browser.is_connected()
                with pytest.raises(ProcessLookupError):
                    os.kill(guard.browser_pid, 0)
                await asyncio.gather(physical, return_exceptions=True)
                record("original_stop_receipt_persisted", proof=stored)
                # 浏览器回执不代替原旅程排空写回，不能提前封存。
                with pytest.raises(RuntimeError, match="RESOURCES_NOT_DRAINED"):
                    await _service(connection).terminate(command)
                proofs = await owner_gate.resolve_quarantine(
                    task_id=ids["task"], session_id=session
                )
                assert proofs[0]["resource_id"] == stored["resource_id"]
                record("original_journey_drained", proofs=proofs)
                result = await _service(connection).terminate(command)
                assert result.state.value == "sealed" and result.execution_status.value == "failed"
                gate = await connection.fetchrow(
                    "SELECT * FROM qc_completion_gate_snapshot WHERE task_id=$1::uuid", ids["task"]
                )
                assert gate["gate_passed"] is False
                assert await connection.fetchval(
                    "SELECT count(*) FROM qc_report_gate_binding WHERE task_id=$1::uuid",
                    ids["task"]
                ) == 0
                frozen = dict(await connection.fetchrow(
                    "SELECT * FROM qc_h5_llm_call_budget WHERE task_id=$1::uuid", ids["task"]
                ))
                assert frozen["deadline"] == deadline and frozen["max_calls"] == 200
                assert frozen["admitted_calls"] == 1
                assert not adapter._sessions and not runtime._pending_browser_stop_receipts
                assert not owner_gate._quarantined and not owner_gate._active
                record("technical_sealed_not_quality_passed", gate=dict(gate), budget=frozen)
            finally:
                if owner is not None:
                    owner.cancel()
                    await asyncio.gather(owner, return_exceptions=True)
                await browser.close()
                if physical is not None:
                    await asyncio.gather(physical, return_exceptions=True)
    finally:
        (tmp_path / "timeline.json").write_text(
                json.dumps({"fault": fault, "ids": ids, "database": urlsplit(url).path,
                            "events": timeline},
                       default=str, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        await connection.close()


async def test_sealed_stop_cases_do_not_pollute_shared_database(stop_database):
    connection = await asyncpg.connect(stop_database["shared"])
    try:
        after = await connection.fetchrow(
            "SELECT (SELECT count(*) FROM qc_task) AS tasks,"
            "(SELECT count(*) FROM qc_h5_browser_stop_receipt) AS receipts"
        )
        assert dict(after) == stop_database["before"]
    finally:
        await connection.close()
