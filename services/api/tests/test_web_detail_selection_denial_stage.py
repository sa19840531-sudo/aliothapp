"""详情选择模型拒绝只结算本菜单，并保留后续菜单调度。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from services.api.app.application.web_desktop_navigation import WebNavigationCandidate
from services.api.app.application.web_exploration_workflow import RunWebWorkflowCommand
from services.api.app.application.web_list_detail_navigation import WebListDetailResult
from services.api.app.application.web_runtime_stage_frontier import WebRuntimeStageFrontier
from services.api.app.application.web_runtime_stage_primary import WebRuntimeStagePrimary
from services.api.app.domain.exploration import WebMenuTerminalStatus
from services.api.app.domain.exploration.web_run_stage_checkpoint import WebRunStage


def _candidate(entry_id: str, label: str) -> WebNavigationCandidate:
    return WebNavigationCandidate(
        entry_id=entry_id,
        label=label,
        url=f"https://example.test/{entry_id}",
        menu_path=(label,),
        dom_fingerprint=f"fingerprint-{entry_id}",
        source_signal="trusted_navigation",
        page_type_key="video_list",
        source_capture_id="capture-home",
        source_page_url="https://example.test",
    )


@dataclass
class _Row:
    entry_id: str
    candidate: WebNavigationCandidate
    execution: object | None = None
    persisted_status: str = "menu_reserved"

    def restored_navigation_candidate(self) -> WebNavigationCandidate:
        return self.candidate


@dataclass(frozen=True)
class _Cursor:
    logical_page_key: str
    cursor_payload: dict[str, object]
    current_stage: WebRunStage
    gaps: tuple[()] = ()


@pytest.mark.asyncio
async def test_detail_selection_denial_finalizes_menu_and_schedules_next() -> None:
    first = _Row("menu-first", _candidate("menu-first", "列表一"))
    second = _Row("menu-second", _candidate("menu-second", "列表二"), persisted_status="pending")
    rows = (first, second)

    class Ledger:
        records = []

        async def read_summary(self, **kwargs):
            del kwargs
            return SimpleNamespace(
                runtime_discovered_ledger=rows,
                menu_limit=10,
                executed_same_origin_count=len(self.records),
            )

        async def finalize(self, record):
            self.records.append(record)
            first.execution = record
            first.persisted_status = record.status.value

    class Evidence:
        saved = []

        async def find_completed(self, cursor, *, operation):
            del cursor, operation
            return None

        async def capture_outcome(self, *, capture_id):
            return (
                {"page_type": "video_list"}
                if capture_id == "capture-ready"
                else {"action_execution_id": "menu-action-first"}
            )

        async def current_capture_reference(self):
            return {"capture_id": "capture-ready"}

        async def save_completed(self, cursor, outcome, *, operation):
            self.saved.append((cursor, outcome, operation))

    class DetailNavigator:
        calls = 0

        async def capture_one(self, evidence, **kwargs):
            del evidence, kwargs
            self.calls += 1
            return WebListDetailResult(
                status=WebMenuTerminalStatus.DETAIL_SELECTION_UNTRUSTED,
                detail_capture_id=None,
                page_type=None,
                error_code="WEB_DETAIL_SELECTION_MODEL_PERMISSION_DENIED",
                detail_selection_reason="WEB_DETAIL_SELECTION_MODEL_PERMISSION_DENIED",
                detail_candidate_ids=("web-candidate:one",),
                detail_action_count=0,
            )

    class DetailEvidence:
        async def read_list(self, *, capture_id):
            assert capture_id == "capture-ready"
            return object()

    command = RunWebWorkflowCommand(
        task_id="task-1",
        agent_run_id="run-1",
        session_id="web:run-1",
        session_owner="web-v2-workflow",
        target_url="https://example.test",
        browser_profile_id="desktop_fhd_1920x1080_v1",
        deadline=datetime.now(UTC) + timedelta(minutes=2),
    )
    cursor = _Cursor(
        logical_page_key="menu:first",
        cursor_payload={
            "menu_ledger_id": "menu-first",
            "capture_id": "capture-ready",
            "menu_navigation_capture_id": "capture-probe",
        },
        current_stage=WebRunStage.CANDIDATE_NAVIGATION,
        gaps=(),
    )
    evidence = Evidence()
    ledger = Ledger()
    navigator = DetailNavigator()
    frontier = WebRuntimeStageFrontier(
        ledger=ledger,
        candidate_source=object(),
        navigator=object(),
        site=SimpleNamespace(site_id="site-1", origin="https://example.test", entries=()),
        evidence=evidence,
    )
    primary = WebRuntimeStagePrimary(
        runtime=SimpleNamespace(
            stage_detail_navigator=navigator,
            stage_detail_evidence_source=DetailEvidence(),
        ),
        evidence=evidence,
        command=command,
    )

    await primary.advance_detail(cursor, frontier=frontier)
    await frontier.reconcile(command, cursor)
    next_menu = await frontier.next_candidate(command, excluded_entry_ids={"menu-first"})

    assert navigator.calls == 1
    assert len(ledger.records) == 1
    assert first.persisted_status == WebMenuTerminalStatus.DETAIL_SELECTION_UNTRUSTED.value
    assert ledger.records[0].diagnostic["error_code"] == (
        "WEB_DETAIL_SELECTION_MODEL_PERMISSION_DENIED"
    )
    assert ledger.records[0].menu_ready_capture_id == "capture-ready"
    assert ledger.records[0].menu_probe_capture_id == "capture-probe"
    assert ledger.records[0].detail_action_execution_id is None
    assert next_menu[0] == "menu-second"
    assert len(evidence.saved) == 1
