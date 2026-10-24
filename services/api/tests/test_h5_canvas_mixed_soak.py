"""独占新库与原生浏览器的混合长测；短测、失败和中断均不能称180分钟通过。"""

import asyncio
import base64
import importlib
import json
import os
import re
import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from urllib.parse import quote
from uuid import uuid4

import asyncpg
import pytest

from services.api.app.application.capture_coordinator import InProcessSessionCommandGate
from services.api.app.infrastructure.browser.page_capture import (
    PageCaptureMode,
    PageCaptureRequest,
    PlaywrightPageCaptureBrowser,
)
from services.api.app.infrastructure.db.h5_llm_budget_repository import (
    PostgresH5LLMBudgetRepository,
)
from services.api.app.infrastructure.db.h5_session_journey_gate import PostgresH5SessionJourneyGate
from services.api.app.infrastructure.db.test_database_guard import (
    require_isolated_test_database_url,
)

_HTML = """<meta name=viewport content="width=device-width,initial-scale=1">
<style>body{margin:0}canvas{display:block}#panel[hidden]{display:none}</style>
<nav><button id=next>Next</button><button id=pay>Payment display</button></nav>
<main data-view=0><canvas id=two width=240 height=200></canvas>
<canvas id=gl width=32 height=32></canvas><video muted autoplay playsinline></video>
<div id=panel hidden>Display only: 20 CNY. No order endpoint.</div></main>
<script>
window.two=document.querySelector('#two').getContext('2d');
two.fillStyle='#227766';two.fillRect(0,0,240,200);
window.gl=document.querySelector('#gl').getContext('webgl');
gl.clearColor(.2,.3,.4,1);gl.clear(gl.COLOR_BUFFER_BIT);
window.stream=document.querySelector('#two').captureStream(2);
document.querySelector('video').srcObject=stream;
window.ticks=0;setInterval(()=>{ticks++;two.fillRect(0,0,240,200)},500);
next.onclick=()=>{document.querySelector('main').dataset.view=String(
  Number(document.querySelector('main').dataset.view)+1);history.pushState({},'', '#view='+
  document.querySelector('main').dataset.view)};
pay.onclick=()=>document.querySelector('#panel').hidden=false;
</script>"""


def _isolated_url():
    url = os.environ.get("H5_CANVAS_SOAK_DATABASE_URL", "")
    return require_isolated_test_database_url(
        url, os.getenv("ALIOTH_DATABASE_URL"),
        allowed_database_names=frozenset({"alioth_test_h5_soak", "alioth_test_h5_soak_smoke"}),
        allowed_roles=frozenset({"alioth_test"}),
    )


def _full_soak(*, duration, elapsed, completed, expected, failure, drained):
    return (duration == 10800 and elapsed >= 10800 and completed == expected
            and failure is None and drained)


@pytest.mark.parametrize("change", ["short", "interrupted", "failed", "pending", "full"])
def test_soak_completion_never_uses_green_short_or_unconfirmed_stop(change):
    values = dict(duration=10800, elapsed=10800, completed=160, expected=160,
                  failure=None, drained=True)
    if change == "short":
        values.update(duration=3, elapsed=3)
    elif change == "interrupted":
        values["elapsed"] = 10799
    elif change == "failed":
        values["failure"] = "contextlost"
    elif change == "pending":
        values["drained"] = False
    assert _full_soak(**values) is (change == "full")


@pytest.mark.parametrize("url", [
    "postgresql://alioth_test@127.0.0.1:5432/alioth",
    "postgresql://tianshu@127.0.0.1:56445/alioth_test_h5_soak",
])
def test_soak_never_accepts_business_database_or_role(monkeypatch, url):
    monkeypatch.setenv("H5_CANVAS_SOAK_DATABASE_URL", url)
    with pytest.raises(ValueError):
        _isolated_url()


async def _process_output(*args):
    process = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE)
    async with asyncio.timeout(5):
        output, _ = await process.communicate()
    assert process.returncode == 0
    return output.decode()


async def _new_schema(control, monkeypatch):
    assert await control.fetchval("SELECT to_regclass('qc_task')") is None, "只接受全新空隔离库"
    await control.execute("""CREATE TABLE qc_task(id uuid PRIMARY KEY,target_platform text,
        status text,finalization_epoch int);
        CREATE TABLE qc_agent_run(id uuid PRIMARY KEY,task_id uuid,status text);
        CREATE TABLE qc_llm_usage_event(task_id uuid,ai_calls int);""")
    budget = importlib.import_module(
        "services.api.migrations.versions.20261010_0132_h5_llm_call_budget",
    )
    await control.execute(budget.CREATE_BUDGET_TABLE)
    for revision in ("20261014_0133_h5_journey_guard", "20261014_0134_h5_stop_proof"):
        module = importlib.import_module("services.api.migrations.versions." + revision)
        statements = []
        monkeypatch.setattr(module, "op", SimpleNamespace(execute=statements.append))
        module.upgrade()
        for statement in statements:
            await control.execute(statement)


async def soak_isolated_canvas_browser_and_database_mixed_soak(monkeypatch, tmp_path):
    url = _isolated_url()
    duration = float(os.getenv("H5_CANVAS_SOAK_SECONDS", "3"))
    assert 0 < duration <= 10800
    steps = 40 if duration == 10800 else 2
    control = await asyncpg.connect(url)
    pool = None
    adapter = PlaywrightPageCaptureBrowser(max_concurrent_captures=2)
    gate = PostgresH5SessionJourneyGate(delegate=InProcessSessionCommandGate(), database_url=url)
    report = {"requested_seconds": duration, "real_site_acceptance": False,
              "model_provider": "deterministic_local_stub", "usage_available": False,
              "pricing_configured": False, "full_180_minute_soak": False,
              "freeze": {"minutes": 180, "actions": 80, "physical_calls": 200},
              "completed_captures": 0, "failure": None, "proofs": [], "samples": 0}
    actors, sessions, baseline_gpu = [], [], {}
    started = None
    stop = asyncio.Event()
    waits = []

    def journal(kind, **data):
        value = {"at": datetime.now(UTC).isoformat(), "kind": kind, **data}
        with (tmp_path / "history.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(value, default=str) + "\n")

    try:
        await _new_schema(control, monkeypatch)
        pool = await asyncpg.create_pool(url, min_size=1, max_size=2)
        budget = PostgresH5LLMBudgetRepository(pool=pool)
        frozen = datetime.now(UTC) + timedelta(minutes=180)
        report["frozen_deadline"] = frozen.isoformat()
        identities = []
        for _index in range(2):
            task, main, supplemental = (str(uuid4()) for _ in range(3))
            session = f"h5:canvas-soak-{uuid4().hex}"
            sessions.append(session)
            await control.execute("INSERT INTO qc_task VALUES($1::uuid,'h5','running',0)", task)
            await control.execute("INSERT INTO qc_agent_run VALUES($1::uuid,$3::uuid,'running'),"
                                  "($2::uuid,$3::uuid,'running')", main, supplemental, task)
            await budget.initialize(task_id=task, agent_run_id=main, max_calls=200, deadline=frozen)
            identities.append((task, main, supplemental, session))
        report["identities"] = identities
        started = time.monotonic()
        report["started_at"] = datetime.now(UTC).isoformat()

        async def capture(task, run, session, cycle, role, mode, target=None):
            for source in ("planning", "quality"):
                await budget.reserve(task_id=task, consumer_agent_run_id=run)
                journal("stub_admission_completed", task_id=task, run_id=run, cycle=cycle,
                        source=source, role=role, tokens=None, cny=None)
            result = await adapter.capture_with_timeout(PageCaptureRequest(
                mode=mode, url=target, session_id=session, viewport_width=240, viewport_height=320,
                capture_screenshot=True, capture_dom=True, capture_visible_text=True,
            ), timeout_seconds=15)
            directory = tmp_path / f"{session}-{cycle}-{role}"
            directory.mkdir()
            (directory / "capture.json").write_text(json.dumps(result.to_dict()), encoding="utf-8")
            for number, artifact in enumerate(result.artifacts):
                if artifact.artifact_type == "screenshot":
                    encoded = artifact.content.get("data")
                    if encoded:
                        (directory / f"{number}.png").write_bytes(base64.b64decode(encoded))
            assert result.execution_status == "success", result.error_message
            assert result.metrics["h5_screenshot_origins"]
            assert all(item["frame_origin"]["status"] != "renderer_unavailable"
                       for item in result.metrics["h5_screenshot_origins"])
            page = adapter._sessions[session].page
            state = await page.evaluate("""() => ({two:two.isContextLost(),gl:gl.isContextLost(),
                ticks,view:document.querySelector('main').dataset.view,
                video:{time:document.querySelector('video').currentTime,
                ready:document.querySelector('video').readyState},
                payment_visible:!document.querySelector('#panel').hidden})""")
            journal("native_capture", session=session, cycle=cycle, role=role, state=state,
                    final_url=result.final_url)
            assert not state["two"] and not state["gl"]
            report["completed_captures"] += 1
            return state

        async def main_actor(identity, queue):
            task, main, _, session = identity
            for cycle in range(steps):
                await asyncio.sleep(max(0, started + duration * cycle / steps - time.monotonic()))
                async with gate.hold_journey(task_id=task, session_id=session):
                    page = adapter._sessions[session].page if session in adapter._sessions else None
                    if page is not None:
                        await page.locator("#next" if cycle % 2 else "#pay").click()
                    state = await capture(task, main, session, cycle, "main",
                        PageCaptureMode.GOTO if page is None else PageCaptureMode.OBSERVE,
                        "data:text/html," + quote(_HTML) if page is None else None)
                    await queue.put((cycle, state["view"], time.monotonic()))
                    await asyncio.sleep(0.04)
            await queue.put(None)

        async def supplement_actor(identity, queue):
            task, _, supplemental, session = identity
            while (job := await queue.get()) is not None:
                cycle, expected_view, waiting = job
                async with gate.hold_journey(task_id=task, session_id=session):
                    waits.append(time.monotonic() - waiting)
                    state = await capture(task, supplemental, session, cycle, "supplement",
                                          PageCaptureMode.OBSERVE)
                    assert state["view"] == expected_view, "同会话整段互斥失效，补证目标漂移"

        async def control_actor(index):
            cycle = 0
            while not stop.is_set():
                async with gate.hold_journey(task_id="isolated-control",
                    session_id=f"h5:soak-control-{index}"):
                    await asyncio.sleep(0.03)
                cycle += 1
                journal("control_completed", actor=index, cycle=cycle)
                try:
                    await asyncio.wait_for(stop.wait(), timeout=10)
                except TimeoutError:
                    pass

        for identity in identities:
            queue = asyncio.Queue(maxsize=1)
            actors.extend([asyncio.create_task(main_actor(identity, queue)),
                           asyncio.create_task(supplement_actor(identity, queue))])
        actors.extend(asyncio.create_task(control_actor(index)) for index in range(8))
        while time.monotonic() - started < duration:
            for actor in actors:
                if actor.done():
                    actor.result()
            pids = set()
            for session, owned in list(adapter._sessions.items()):
                cdp = await owned.browser.new_browser_cdp_session()
                try:
                    processes = (await cdp.send("SystemInfo.getProcessInfo"))["processInfo"]
                    gpu = {p["id"] for p in processes if p["type"] == "GPU"}
                    if session not in baseline_gpu:
                        baseline_gpu[session] = gpu
                        info = await cdp.send("SystemInfo.getInfo")
                        journal("gpu_baseline", session=session,
                                version=owned.browser.version, gpu=info)
                    journal("processes", session=session, processes=processes)
                    assert gpu == baseline_gpu[session], "未注入故障的GPU进程发生变化"
                    pids.update(p["id"] for p in processes)
                finally:
                    await cdp.detach()
            rss = (await _process_output("ps", "-p", ",".join(map(str, pids)), "-o", "pid=,rss=")
                   if pids else "")
            memory = await _process_output("memory_pressure", "-Q")
            percent = int(re.search(r"free percentage: (\d+)%", memory)[1])
            rss_kb = sum(int(line.split()[1]) for line in rss.splitlines())
            budgets = [dict(row) for row in await control.fetch(
                "SELECT * FROM qc_h5_llm_call_budget",
            )]
            assert all(row["deadline"] == frozen and row["max_calls"] == 200
                       and row["admitted_calls"] <= 200 for row in budgets)
            connections = await control.fetchval("SELECT count(*) FROM pg_stat_activity "
                "WHERE datname=current_database() AND backend_type='client backend'")
            journal("sample", rss=rss, host_memory=memory, budgets=budgets, connections=connections)
            assert percent >= 10 and rss_kb <= 786432, "达到新增资源安全边界，停止而不是制造整机OOM"
            assert connections <= 19
            report["samples"] += 1
            report["elapsed_seconds"] = time.monotonic() - started
            (tmp_path / "progress.json").write_text(json.dumps(report), encoding="utf-8")
            await asyncio.sleep(min(30, max(0, duration - (time.monotonic() - started))))
        stop.set()
        await asyncio.wait_for(asyncio.gather(*actors), timeout=45)
        for _, _, _, session in identities:
            resource = adapter._sessions[session].stop_guard
            await adapter.close_session(session)
            report["proofs"].append(await resource.stop_and_prove())
            await gate.release_session(
                task_id=next(i[0] for i in identities if i[3] == session), session_id=session,
            )
        assert report["completed_captures"] == 4 * steps
        assert not gate._active and not gate._quarantined and not gate._locks
        assert not adapter._sessions and not adapter._session_locks
        assert await control.fetchval("SELECT count(*) FROM qc_h5_session_journey_guard "
                                      "WHERE status <> 'drained'") == 0
        report["drained"] = True
        report["wait_max_seconds"] = max(waits)
    except BaseException as exc:
        report["failure"] = type(exc).__name__ + ":" + str(exc)
        journal("failure", error=report["failure"])
        raise
    finally:
        stop.set()
        for actor in actors:
            actor.cancel()
        await asyncio.gather(*actors, return_exceptions=True)
        for session in list(adapter._sessions):
            try:
                await adapter.close_session(session)
            except BaseException as exc:
                journal("stop_unconfirmed", session=session, error=type(exc).__name__)
        if pool is not None:
            await pool.close()
        await control.close()
        report["elapsed_seconds"] = time.monotonic() - started if started else 0
        report["full_180_minute_soak"] = _full_soak(
            duration=duration, elapsed=report["elapsed_seconds"],
            completed=report["completed_captures"], expected=4 * steps,
            failure=report["failure"], drained=report.get("drained", False))
        (tmp_path / "result.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
