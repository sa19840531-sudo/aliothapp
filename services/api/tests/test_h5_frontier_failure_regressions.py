"""空白页恢复与已排空 H5 任务的失败收口回归。"""

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from services.api.app.application.h5_exploration_recovery import restore_h5_frontier_source
from services.api.app.application.h5_exploration_workflow import _with_task_frontier
from services.api.app.application.h5_runtime_composition import _CoordinatorRecoveryBrowser
from services.api.app.domain.exploration.h5_frontier import H5TaskFrontierSnapshot
from services.api.app.domain.interaction import (
    build_action_execution_id,
    build_action_intent_hash,
)
from services.api.app.domain.task import (
    CompletionGateFacts,
    TaskExecutionStatus,
    build_completion_gate_snapshot,
)
from services.api.app.infrastructure.db.task_finalization_repository import (
    _requires_h5_review_status_transition,
    _should_mark_task_review,
)
from services.api.tests.test_h5_exploration_turn import (
    _DriftingBrowser,
    _dynamic_restore_entry,
    _work,
)
from services.api.tests.test_task_finalization_persistence import (
    NOW,
    RUN_ID,
    TASK_ID,
    FakeConnection,
    FakePool,
    _blocked_gate_update,
    _facts_row,
    _task_row,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["navigate", "back"])
async def test_frontier_restore_identity_separates_siblings_and_fresh_sources(operation) -> None:
    work = _work(capture_id="capture-current")
    source_state = "state:home"
    first_candidate = work.frontier.candidates[0]
    second_candidate = replace(
        first_candidate, candidate_id="candidate-other-sibling", semantic_target="另一个业务入口",
    )
    snapshot = H5TaskFrontierSnapshot.create(max_pages=None).merge(
        source_capture_id="capture-home",
        source_state_version=source_state,
        source_location=work.frontier.observation.location,
        navigation_depth=1,
        candidates=(first_candidate, second_candidate),
    )
    first_entry, second_entry = snapshot.pending_entries
    coordinator = SimpleNamespace(coordinate=AsyncMock())

    async def restore(entry_id, source_capture_id):
        selected = snapshot.select_entry(entry_id)
        frontier = _with_task_frontier(
            frontier=replace(
                work.frontier,
                observation=replace(work.frontier.observation, capture_id=source_capture_id),
            ),
            snapshot=selected,
        )
        browser = _CoordinatorRecoveryBrowser(
            work=replace(
                work, source_capture_id=source_capture_id,
                frontier=frontier, frontier_snapshot_id=frontier.snapshot_id,
            ),
            agent_run_id="run-h5", capture_coordinator=coordinator,
            detector=SimpleNamespace(), assessment_repository=SimpleNamespace(),
            cancellation=SimpleNamespace(raise_if_requested=AsyncMock()),
            now=lambda: work.frontier.observation.observed_at,
            source_state_version=source_state,
        )
        if operation == "navigate":
            await browser.navigate(work.frontier.observation.location)
        else:
            await browser.back()
        return coordinator.coordinate.call_args.args[0].intent

    first = await restore(first_entry.entry_id, "capture-current")
    repeated = await restore(first_entry.entry_id, "capture-current")
    sibling = await restore(second_entry.entry_id, "capture-current")
    fresh_source = await restore(first_entry.entry_id, "capture-fresh")

    assert build_action_intent_hash(first) == build_action_intent_hash(repeated)
    assert build_action_execution_id(first) == build_action_execution_id(repeated)
    assert len({
        build_action_execution_id(intent) for intent in (first, sibling, fresh_source)
    }) == 3
    assert all(len(intent.intent_id) <= 128 for intent in (first, sibling, fresh_source))


@pytest.mark.asyncio
async def test_same_url_observation_parent_is_not_browser_history() -> None:
    work = _work(capture_id="capture-scrolled")
    source_state = "state:before-scroll"
    source_location = work.frontier.observation.location
    snapshot = H5TaskFrontierSnapshot.create(max_pages=None).merge(
        source_capture_id="capture-before-scroll",
        source_state_version=source_state,
        source_location=source_location,
        navigation_depth=1,
        candidates=work.frontier.candidates,
    )
    snapshot = snapshot.select_entry(snapshot.pending_entries[0].entry_id)
    frontier = _with_task_frontier(
        frontier=replace(
            work.frontier,
            observation=replace(
                work.frontier.observation,
                state_version="state:after-scroll",
                metadata={"parent_state_version": source_state},
            ),
        ),
        snapshot=snapshot,
    )
    browser = _CoordinatorRecoveryBrowser(
        work=replace(work, frontier=frontier, frontier_snapshot_id=frontier.snapshot_id),
        agent_run_id="run-h5",
        capture_coordinator=SimpleNamespace(),
        detector=SimpleNamespace(),
        assessment_repository=SimpleNamespace(),
        cancellation=SimpleNamespace(),
        now=lambda: work.frontier.observation.observed_at,
        source_state_version=source_state,
    )

    assert not await browser.is_history_ancestor(
        source_location=source_location,
        current_location=source_location,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("attempt", [0, 1])
async def test_blank_page_restores_authorized_source_instead_of_failing_run(attempt) -> None:
    entry, detection = _dynamic_restore_entry()
    browser = _DriftingBrowser(detection)

    result = await restore_h5_frontier_source(
        entry=entry,
        current_location="about:blank",
        browser=browser,
        attempt_count=attempt,
    )

    assert result.restored
    assert result.route == "navigate"
    assert browser.navigated
    assert result.capture is not None
    assert result.assessment is not None


@pytest.mark.asyncio
async def test_blank_page_does_not_reset_restore_attempt_budget() -> None:
    entry, detection = _dynamic_restore_entry()
    browser = _DriftingBrowser(detection)
    result = await restore_h5_frontier_source(
        entry=entry,
        current_location="about:blank",
        browser=browser,
        attempt_count=2,
    )
    assert not result.restored
    assert result.reason_code == "h5_frontier_restore_exhausted"
    assert result.restoration_attempt_count == 2
    assert not browser.navigated


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "location", ["data:text/html,blank", "javascript:void(0)", "", " https://m.example.test/movie"]
)
async def test_invalid_landing_is_bounded_restore_failure(location: str) -> None:
    entry, detection = _dynamic_restore_entry()
    browser = _DriftingBrowser(detection)

    result = await restore_h5_frontier_source(
        entry=entry,
        current_location=location,
        browser=browser,
    )

    assert not result.restored
    assert result.reason_code == "h5_frontier_restore_location_unavailable"
    assert result.restoration_attempt_count == 1
    assert not browser.navigated


@pytest.mark.asyncio
async def test_blank_recovery_does_not_allow_external_origin_restore() -> None:
    entry, detection = _dynamic_restore_entry()
    browser = _DriftingBrowser(detection)

    result = await restore_h5_frontier_source(
        entry=entry,
        current_location="https://external.example.test/",
        browser=browser,
    )

    assert not result.restored
    assert result.reason_code == "h5_frontier_restore_cross_origin"
    assert not browser.navigated


def _drained_gap_facts(**overrides) -> CompletionGateFacts:
    return CompletionGateFacts(
        **{
            "exploration_terminal": True,
            "pending_action_count": 0,
            "pending_capture_commit_count": 0,
            "nonterminal_enforced_work_item_count": 0,
            "active_session_request_count": 0,
            "background_execution_count": 0,
            "candidate_rule_count": 150,
            "terminal_candidate_rule_count": 91,
            "review_policy_satisfied": False,
            "h5_coverage_satisfied": False,
            **overrides,
        }
    )


@pytest.mark.parametrize(
    "status",
    [
        TaskExecutionStatus.FAILED,
        TaskExecutionStatus.NEEDS_REVIEW,
        TaskExecutionStatus.COMPLETED_WITH_GAPS,
    ],
)
def test_drained_inventory_gap_stops_running_without_passing_gate(status) -> None:
    facts = _drained_gap_facts()

    assert _should_mark_task_review(
        execution_status=status,
        facts=facts,
        h5_rule_inventory_missing_count=59,
    )
    assert not facts.gate_passed
    assert not _should_mark_task_review(execution_status=status, facts=facts)


@pytest.mark.parametrize(
    "field",
    [
        "pending_action_count",
        "pending_capture_commit_count",
        "nonterminal_enforced_work_item_count",
        "active_session_request_count",
        "background_execution_count",
    ],
)
def test_inventory_gap_cannot_hide_live_work(field: str) -> None:
    assert not _should_mark_task_review(
        execution_status=TaskExecutionStatus.FAILED,
        facts=_drained_gap_facts(**{field: 1}),
        h5_rule_inventory_missing_count=59,
    )


def test_inventory_gap_does_not_hide_pending_candidate_results() -> None:
    assert not _should_mark_task_review(
        execution_status=TaskExecutionStatus.FAILED,
        facts=_drained_gap_facts(),
        h5_rule_inventory_missing_count=58,
    )


@pytest.mark.parametrize("platform", ["h5", "web", "android", "ios"])
def test_old_gate_does_not_prevent_h5_failed_status_projection(platform: str) -> None:
    assert _requires_h5_review_status_transition(
        task={"task_status": "running", "target_platform": platform, "workflow_version": "v2"},
        execution_status=TaskExecutionStatus.FAILED,
    ) is (platform == "h5")


@pytest.mark.asyncio
async def test_inventory_read_separates_missing_results_from_pending_work(monkeypatch) -> None:
    from services.api.app.application.task_finalization import EvaluateCompletionGateCommand
    from services.api.app.domain.task import TaskQualityOutcome
    from services.api.app.infrastructure.db import (
        h5_rule_inventory_completion_gate,
        task_finalization_repository,
    )

    async def apply_inventory(connection, *, task_id, row, facts):
        return replace(facts, candidate_rule_count=facts.candidate_rule_count + 59)

    monkeypatch.setattr(
        h5_rule_inventory_completion_gate, "apply_h5_rule_inventory_gate", apply_inventory
    )
    connection = FakeConnection(
        fetchrow_results=[_facts_row(candidate_rule_count=91, terminal_candidate_rule_count=91)]
    )
    row, facts = await task_finalization_repository._read_completion_gate_facts(
        connection,
        EvaluateCompletionGateCommand(
            task_id=TASK_ID,
            agent_run_id=RUN_ID,
            expected_finalization_epoch=4,
            expected_row_version=7,
            execution_status=TaskExecutionStatus.FAILED,
            quality_outcome=TaskQualityOutcome.UNKNOWN,
        ),
    )
    assert row["h5_rule_inventory_missing_count"] == 59
    assert facts.candidate_rule_count == 150
    assert facts.terminal_candidate_rule_count == 91
    assert not facts.gate_passed


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["evaluate_and_seal", "finalize_task_with_report"])
async def test_both_finalization_paths_stop_drained_h5_without_binding_report(
    monkeypatch, method
) -> None:
    from services.api.app.application.task_finalization import (
        EvaluateCompletionGateCommand,
        FinalizeTaskWithReportCommand,
    )
    from services.api.app.domain.task import TaskQualityOutcome
    from services.api.app.infrastructure.db import task_finalization_repository as module

    async def read_facts(connection, command):
        return {"evaluated_at": NOW, "h5_rule_inventory_missing_count": 59}, _drained_gap_facts()

    monkeypatch.setattr(module, "_read_completion_gate_facts", read_facts)
    gate_id = build_completion_gate_snapshot(
        task_id=TASK_ID,
        finalization_epoch=4,
        evaluation_sequence=1,
        facts=_drained_gap_facts(),
        evaluated_at=NOW,
    ).gate_snapshot_id
    connection = FakeConnection(
        fetchrow_results=[
            {
                **_task_row(state="session_draining", epoch=4, row_version=7),
                "task_status": "running",
                "target_platform": "h5",
                "workflow_version": "v2",
            },
            _task_row(state="session_draining", epoch=4, row_version=8),
        ],
        fetchval_results=[1, gate_id],
    )
    repository = module.PostgresTaskFinalizationRepository(pool=FakePool(connection))
    values = {
        "task_id": TASK_ID,
        "agent_run_id": RUN_ID,
        "expected_finalization_epoch": 4,
        "expected_row_version": 7,
        "execution_status": TaskExecutionStatus.FAILED,
        "quality_outcome": TaskQualityOutcome.UNKNOWN,
    }
    command = (
        EvaluateCompletionGateCommand(**values)
        if method == "evaluate_and_seal"
        else FinalizeTaskWithReportCommand(
            **values,
            report_id="report-test",
            report_version=1,
            final_report={},
        )
    )
    decision = await getattr(repository, method)(command=command)

    assert not decision.snapshot.gate_passed
    assert _blocked_gate_update(connection)[2][-1] is True
    assert decision.finalization.finalization_epoch == 4
    assert not any("INSERT INTO qc_report" in call[1] for call in connection.calls)


@pytest.mark.asyncio
async def test_real_browser_blank_source_recovery_keeps_capture_assessment() -> None:
    from playwright.async_api import async_playwright

    entry, detection = _dynamic_restore_entry()
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            page = await browser.new_page()
            await page.route(
                "https://m.example.test/**",
                lambda route: route.fulfill(
                    content_type="text/html",
                    body="<main><h1>视频列表</h1><button>内容</button></main>",
                ),
            )

            class Browser(_DriftingBrowser):
                async def navigate(self, destination_url):
                    await page.goto(destination_url)
                    return await super().navigate(page.url)

                async def capture(self):
                    assert page.url == entry.source_location
                    assert await page.screenshot()
                    return await super().capture()

            runtime = Browser(detection)
            assert page.url == "about:blank"
            result = await restore_h5_frontier_source(
                entry=entry,
                current_location=page.url,
                browser=runtime,
            )
            assert result.restored
            assert result.capture is not None
            assert result.assessment.execution_status == "accepted"
            assert page.url == entry.source_location
        finally:
            await browser.close()
