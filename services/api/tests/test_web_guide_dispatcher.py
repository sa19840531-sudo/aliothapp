"""主线仅登记引导候选，支线失败不会使阶段失败。"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from services.api.app.application.web_guide_dispatcher import WebGuideBranchDispatcher
from services.api.app.application.web_guide_page import WebGuideOutcome


@pytest.mark.asyncio
async def test_guide_dispatcher_runs_optional_branch_without_waiting_in_main() -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    completed = []
    class Branch:
        async def attempt(self, candidate, *, task_id, agent_run_id):
            started.set()
            await release.wait()
            return WebGuideOutcome(
                "incomplete", "WEB_GUIDE_OPEN_FAILED",
                candidate.candidate_id, candidate.source_capture_id,
            )
    class Ledger:
        async def claim(self, *, candidate, task_id, session_id):
            return "phase-1"
        async def complete(self, *, phase, outcome):
            completed.append(outcome)
    dispatcher = WebGuideBranchDispatcher(
        branch=Branch(), ledger=Ledger(), task_id="task-1", agent_run_id="run-1",
        session_id="web:run-1", deadline=datetime.now(UTC) + timedelta(minutes=1),
    )
    await dispatcher.schedule(
        source_capture_id="source-1", source_url="https://site.example/",
        snapshot={"elements": [{"tag": "a", "visible": True,
                                "text": "官方 APP", "href": "/app"}]},
    )
    await asyncio.wait_for(started.wait(), 1)
    assert completed == []
    release.set()
    await dispatcher.finish()
    assert completed[0].status == "incomplete"


@pytest.mark.asyncio
async def test_guide_claim_failure_does_not_escape_to_main() -> None:
    class Ledger:
        async def claim(self, **kwargs):
            raise RuntimeError("database unavailable")
    dispatcher = WebGuideBranchDispatcher(
        branch=object(), ledger=Ledger(), task_id="task-1", agent_run_id="run-1",
        session_id="web:run-1", deadline=datetime.now(UTC) + timedelta(minutes=1),
    )
    await dispatcher.schedule(
        source_capture_id="source-1", source_url="https://site.example/",
        snapshot={"elements": [{"tag": "a", "visible": True,
                                "text": "官方 APP", "href": "/app"}]},
    )
    await dispatcher.finish()
