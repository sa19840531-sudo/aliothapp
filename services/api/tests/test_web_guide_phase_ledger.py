"""引导阶段使用独立 phase，失败不进入 Web 主阶段 gap。"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from services.api.app.application.web_guide_candidate import WebGuideCandidate
from services.api.app.application.web_guide_page import WebGuideOutcome
from services.api.app.infrastructure.db.web_guide_phase_ledger import WebGuidePhaseLedger


@pytest.mark.asyncio
async def test_guide_phase_records_failure_without_main_checkpoint() -> None:
    calls = []
    phase = SimpleNamespace(phase_run_id="phase-1", row_version=0)
    class Gate:
        @asynccontextmanager
        async def hold(self, **kwargs):
            calls.append("gate")
            yield object()
    class Phases:
        async def get_run(self, **kwargs):
            calls.append("get")
            return phase if "start" in calls else None
        async def start_run(self, **kwargs):
            calls.append("start")
            assert kwargs["phase_key"] == "web_guide_page"
            return phase
        async def record_step(self, **kwargs):
            calls.append(("step", kwargs["decision_status"], kwargs["result_capture_id"]))
        async def finish_run(self, **kwargs):
            calls.append(("finish", kwargs["status"].value, kwargs["terminal_reason_code"]))
    ledger = WebGuidePhaseLedger(gate=Gate(), phases=Phases())
    candidate = WebGuideCandidate(
        candidate_id="candidate-1", source_capture_id="source-1",
        source_url="https://site.example/", destination_url="https://site.example/app",
        label="APP", kind="page_candidate",
    )
    claimed = await ledger.claim(candidate=candidate, task_id="task-1", session_id="web:run-1")
    await ledger.complete(
        phase=claimed,
        outcome=WebGuideOutcome(
            "incomplete", "WEB_GUIDE_OPEN_FAILED", "candidate-1", "source-1",
        ),
    )
    assert calls == [
        "gate", "get", "start", ("step", "incomplete", None), "get",
        ("finish", "failed", "WEB_GUIDE_OPEN_FAILED"),
    ]


@pytest.mark.asyncio
async def test_guide_phase_claim_is_idempotent_inside_session_gate() -> None:
    lock = asyncio.Lock()
    phase = SimpleNamespace(phase_run_id="phase-1")
    class Gate:
        @asynccontextmanager
        async def hold(self, **kwargs):
            async with lock:
                yield object()
    class Phases:
        created = False
        async def get_run(self, **kwargs):
            await asyncio.sleep(0)
            return phase if self.created else None
        async def start_run(self, **kwargs):
            self.created = True
            return phase
    ledger = WebGuidePhaseLedger(gate=Gate(), phases=Phases())
    candidate = WebGuideCandidate(
        candidate_id="candidate-1", source_capture_id="source-1",
        source_url="https://site.example/", destination_url="https://site.example/app",
        label="APP", kind="page_candidate",
    )
    first, second = await asyncio.gather(*(
        ledger.claim(candidate=candidate, task_id="task-1", session_id="web:run-1")
        for _ in range(2)
    ))
    assert (first is phase) != (second is phase)
    assert (first is None) != (second is None)


@pytest.mark.asyncio
async def test_guide_phase_records_result_capture_in_standard_field() -> None:
    captured = []
    class Phases:
        async def record_step(self, **kwargs):
            captured.append(kwargs)
        async def get_run(self, **kwargs):
            return SimpleNamespace(row_version=1)
        async def finish_run(self, **kwargs):
            return None
    ledger = WebGuidePhaseLedger(gate=object(), phases=Phases())
    await ledger.complete(
        phase=SimpleNamespace(phase_run_id="phase-1"),
        outcome=WebGuideOutcome(
            "confirmed", "WEB_GUIDE_MODEL_CONFIRMED", "candidate-1", "source-1",
            capture_id="capture-2", page_type="guide_page", artifact_ids=("shot-1",),
        ),
    )
    assert captured[0]["result_capture_id"] == "capture-2"
