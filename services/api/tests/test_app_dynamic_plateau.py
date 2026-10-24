"""冻结规则与真实截图变化不得被粗页型停滞截断；预算和无效果仍有界。"""

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from services.api.app.application.app_exploration_turn import AppExplorationTurnRunner
from services.api.app.application.app_global_frontier import AppGlobalFrontierService
from services.api.app.application.app_hjll_script import HJLL_SCRIPT_REASON_CODE
from services.api.app.application.app_hlapp_script import (
    HLAPP_SCRIPT_BLOCKED_CODE,
    HLAPP_SCRIPT_REASON_CODE,
)
from services.api.app.application.app_rule_coverage_facts import project_app_rule_coverage_facts
from services.api.app.application.app_screenshot_exploration import _next_coverage_metadata
from services.api.app.domain.exploration import (
    AppExplorationGoal,
    AppExplorationOutcome,
    AppExplorationTerminalEvaluator,
    AppFrontierStatus,
    CandidateAction,
    ExplorationTerminal,
    ExplorationTerminalStatus,
)
from services.api.app.domain.interaction import InteractionOperation, InteractionRiskLevel
from services.api.tests.test_app_exploration_terminal import (
    NOW,
    _candidate,
    _dynamic_covered_metadata,
    _goal,
    _work,
)
from services.api.tests.test_app_exploration_turn import (
    MemoryAppFrontierQueue,
    MemoryWorkQueue,
    RecordingContinuation,
    StaticTurnExecutor,
    _global_frontier_work,
    _turn_result,
)
from services.api.tests.test_app_screenshot_exploration import (
    _coverage_analysis,
    _coverage_goal,
)


def _scroll() -> CandidateAction:
    return CandidateAction(
        candidate_id="trusted-scroll",
        semantic_target="向下浏览当前业务列表",
        action_type=InteractionOperation.SCROLL,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=50,
        navigation_depth=0,
    )


def _scroll_metadata(*, goal, changed: bool, package_name="com.example.target"):
    metadata = None
    counts = []
    for index in range(4):
        analysis = _coverage_analysis(
            capture_id=f"business-{index}",
            artifact_id=f"png-{index}",
            image_signature="sha256:" + (str(index) if changed else "0") * 64,
            visual_page_type="video_list",
            package_name=package_name,
        )
        metadata = _next_coverage_metadata(
            goal=goal,
            analysis=analysis,
            prior_metadata=metadata,
            business_page_scope="video_list",
            completed_action=_scroll() if index else None,
            action_succeeded=True if index else None,
            arrival_proven=True if index else None,
        )
        counts.append(metadata["dynamic_repeated_business_page_action_count"])
    return metadata, counts


@pytest.mark.parametrize("changed", [False, True])
def test_same_page_scroll_counts_evidence_without_inflating_business_pages(changed):
    metadata, counts = _scroll_metadata(
        goal=_coverage_goal(required_page_types=("video_list",), minimum_business_pages=1),
        changed=changed,
    )
    assert counts == ([0, 0, 0, 0] if changed else [0, 1, 2, 3])
    assert metadata["covered_business_page_count"] == 1
    assert metadata["covered_page_types"] == ["video_list"]
    assert len(metadata["covered_business_pages"]) == 1


def test_real_frozen_73_rule_inventory_continues_after_same_page_new_png_scrolls():
    frozen = json.loads(
        Path(__file__)
        .with_name("fixtures")
        .joinpath("app_cycle6_rule_coverage.json")
        .read_text(encoding="utf-8")
    )
    target = frozen["target"]
    goal = AppExplorationGoal(
        required_page_types=tuple(target["required_page_types"]),
        required_milestones=tuple(target["required_milestones"]),
        minimum_business_pages=target["minimum_business_pages"],
        allowed_packages=("com.hj.ttthree",),
    )
    metadata, counts = _scroll_metadata(goal=goal, changed=True, package_name="com.hj.ttthree")
    coverage = project_app_rule_coverage_facts(
        frozen["inventory"],
        scope={
            "task_id": "cycle6",
            "agent_run_id": "run",
            "session_id": "session",
            "finalization_epoch": 0,
        },
        completed_rule_ids=set(),
        captures=[
            {
                "capture_id": "business-3",
                "page_type": "video_list",
                "page_state": "stable",
                "page_url": "app://com.hj.ttthree/.MainActivity",
                "session_auth": "guest",
            }
        ],
        pending_inspection_work_count=1,
    )
    metadata.update(
        {
            "pending_action_count": 0,
            "pending_evidence_count": 0,
            "pending_facts_available": True,
            "app_rule_inventory_coverage": coverage,
            "app_runtime_policy": {"app_target_snapshot": target},
            "app_global_frontier": {"exhausted": False, "nonterminal_count": 4, "pending": 4},
        }
    )
    work = _work(app_goal=goal, candidates=(_scroll(),), metadata=metadata)
    evaluation = AppExplorationTerminalEvaluator(now=lambda: NOW).evaluate(
        work, eligible_candidate_count=1
    )
    assert len(frozen["inventory"]["rules"]) == 73
    assert coverage["pending_rule_count"] == 48
    assert len(coverage["deferred_rule_ids"]) == 23
    assert counts == [0, 0, 0, 0]
    assert evaluation.terminal is None
    assert evaluation.coverage.rule_coverage_ready is False
    assert evaluation.coverage.covered_business_page_count == 1


@pytest.mark.parametrize("repeated_state_count", [0, 3])
@pytest.mark.parametrize("has_siblings", [False, True])
def test_pending_judge_waits_before_local_plateau_with_remaining_budget(
    repeated_state_count, has_siblings
):
    metadata = _dynamic_covered_metadata(global_frontier_exhausted=not has_siblings)
    metadata.update(
        {
            "dynamic_repeated_business_page_action_count": 3,
            "app_rule_inventory_coverage": {
                "pending_rule_count": 1,
                "pending_inspection_work_count": 1,
            },
        }
    )
    evaluation = AppExplorationTerminalEvaluator(now=lambda: NOW).evaluate(
        _work(app_goal=_goal(), metadata=metadata, repeated_state_count=repeated_state_count),
        eligible_candidate_count=0,
    )
    assert evaluation.terminal is None


@pytest.mark.parametrize("budget", ["actions", "duration", "deadline"])
def test_local_plateau_and_pending_judge_cannot_extend_original_budget(budget):
    metadata = _dynamic_covered_metadata(global_frontier_exhausted=False)
    metadata.update(
        {
            "dynamic_repeated_business_page_action_count": 3,
            "app_rule_inventory_coverage": {
                "pending_rule_count": 1,
                "pending_inspection_work_count": 1,
            },
        }
    )
    work = _work(app_goal=_goal(), metadata=metadata, repeated_state_count=3)
    if budget == "deadline":
        work = replace(work, deadline=NOW)
    else:
        field = "remaining_actions" if budget == "actions" else "remaining_duration_ms"
        changes = {field: 0}
        if budget == "actions":
            changes.update(remaining_business_actions=0, remaining_obstruction_actions=0)
        work = replace(work, budget=replace(work.budget, **changes))
    evaluation = AppExplorationTerminalEvaluator(now=lambda: NOW).evaluate(
        work, eligible_candidate_count=0
    )
    assert evaluation.terminal is not None
    assert evaluation.terminal.status is ExplorationTerminalStatus.BUDGET_EXHAUSTED


def test_unchanged_evidence_without_safe_actions_still_stops_with_real_rule_gap():
    metadata = _dynamic_covered_metadata(global_frontier_exhausted=True)
    metadata.update(
        {
            "dynamic_repeated_business_page_action_count": 3,
            "app_rule_inventory_coverage": {"pending_rule_count": 1},
        }
    )
    evaluation = AppExplorationTerminalEvaluator(now=lambda: NOW).evaluate(
        _work(app_goal=_goal(), metadata=metadata, repeated_state_count=3),
        eligible_candidate_count=0,
    )
    assert evaluation.terminal is not None
    assert evaluation.terminal.reason_code == "loop_detected"
    assert evaluation.outcome is AppExplorationOutcome.EVIDENCE_INSUFFICIENT


def test_explicit_targets_ignore_dynamic_page_plateau_counter():
    evaluation = AppExplorationTerminalEvaluator(now=lambda: NOW).evaluate(
        _work(
            app_goal=_goal(),
            candidates=(_candidate(),),
            metadata={
                "dynamic_repeated_business_page_action_count": 3,
            },
        ),
        eligible_candidate_count=1,
    )
    assert evaluation.terminal is None


@pytest.mark.parametrize("budget", ["actions", "duration", "deadline"])
async def test_restore_does_not_reopen_siblings_after_original_budget(budget):
    work = _global_frontier_work()
    if budget == "deadline":
        work = replace(work, deadline=datetime.now(UTC) - timedelta(seconds=1))
    else:
        field = "remaining_actions" if budget == "actions" else "remaining_duration_ms"
        changes = {field: 0}
        if budget == "actions":
            changes.update(remaining_business_actions=0, remaining_obstruction_actions=0)
        work = replace(work, budget=replace(work.budget, **changes))
    frontier_queue = MemoryAppFrontierQueue()
    global_frontier = AppGlobalFrontierService(repository=frontier_queue)
    await global_frontier.register_work_candidates(work=work, agent_run_id="run-1")
    runner = AppExplorationTurnRunner(
        queue=MemoryWorkQueue(work),
        turn_executor=StaticTurnExecutor(_turn_result(work)),
        continuation=RecordingContinuation(),
        lease_owner="worker",
        global_frontier=global_frontier,
        frontier_queue=frontier_queue,
    )
    decision, assigned = await runner._restore_pending_frontier(
        work=work,
        agent_run_id="run-1",
        proposed_terminal=ExplorationTerminal(
            work_id=work.work_id,
            task_id=work.task_id,
            status=ExplorationTerminalStatus.NO_PROGRESS,
            reason="当前分支同图无效果",
            reason_code="app_repeated_business_page_plateau",
            outcome=AppExplorationOutcome.EVIDENCE_INSUFFICIENT.value,
        ),
    )
    assert isinstance(decision, ExplorationTerminal)
    assert decision.status is ExplorationTerminalStatus.BUDGET_EXHAUSTED
    assert assigned is None
    assert set(frontier_queue.statuses.values()) == {AppFrontierStatus.PENDING}
    assert frontier_queue.transitions == []


@pytest.mark.parametrize(
    "reason",
    [
        HJLL_SCRIPT_REASON_CODE,
        HLAPP_SCRIPT_REASON_CODE,
        HLAPP_SCRIPT_BLOCKED_CODE,
    ],
)
async def test_explicit_playbook_terminal_keeps_its_original_exit(reason):
    work = _global_frontier_work()
    frontier_queue = MemoryAppFrontierQueue()
    global_frontier = AppGlobalFrontierService(repository=frontier_queue)
    await global_frontier.register_work_candidates(work=work, agent_run_id="run-1")
    terminal = ExplorationTerminal(
        work_id=work.work_id,
        task_id=work.task_id,
        status=(
            ExplorationTerminalStatus.BLOCKED
            if reason == HLAPP_SCRIPT_BLOCKED_CODE
            else ExplorationTerminalStatus.COMPLETED
        ),
        reason="显式脚本按既定合同退出",
        reason_code=reason,
        outcome=(
            AppExplorationOutcome.EVIDENCE_INSUFFICIENT.value
            if reason == HLAPP_SCRIPT_BLOCKED_CODE
            else AppExplorationOutcome.EXPLORATION_COMPLETED.value
        ),
    )
    runner = AppExplorationTurnRunner(
        queue=MemoryWorkQueue(work),
        turn_executor=StaticTurnExecutor(_turn_result(work)),
        continuation=RecordingContinuation(),
        lease_owner="worker",
        global_frontier=global_frontier,
        frontier_queue=frontier_queue,
    )
    decision, assigned = await runner._restore_pending_frontier(
        work=work,
        agent_run_id="run-1",
        proposed_terminal=terminal,
    )
    assert decision is terminal
    assert assigned is None
    assert set(frontier_queue.statuses.values()) == {AppFrontierStatus.PENDING}
    assert frontier_queue.transitions == []
