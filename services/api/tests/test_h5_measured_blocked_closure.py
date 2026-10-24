"""真实开屏失败的复核停轮与在途资源分开，不得强制封存或通过。"""

import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from services.api.app.application.task_finalization import (
    EvaluateCompletionGateCommand,
    FinalizeTaskWithReportCommand,
)
from services.api.app.domain.task import (
    CompletionGateFacts,
    TaskExecutionStatus,
    TaskFinalizationState,
    TaskQualityOutcome,
    build_completion_gate_snapshot,
)
from services.api.app.infrastructure.db import task_finalization_repository as repository
from services.api.tests.test_task_finalization_persistence import (
    RUN_ID,
    TASK_ID,
    FakeConnection,
    FakePool,
    _task_row,
)

CASE = json.loads((Path(__file__).parent / "fixtures/h5_dys18_closure_20261012.json").read_text())


def _facts():
    gate = CASE["gate"]
    return CompletionGateFacts(
        **{name: gate[name] for name in CompletionGateFacts.__dataclass_fields__ if name in gate}
    )


@pytest.mark.parametrize("formal", [False, True])
@pytest.mark.parametrize(
    "pending",
    [
        None,
        "pending_action_count",
        "pending_capture_commit_count",
        "active_session_request_count",
        "background_execution_count",
        "inspection_work",
    ],
)
async def test_measured_failed_review_parks_only_after_drain(monkeypatch, formal, pending):
    facts = _facts()
    if pending is not None and pending != "inspection_work":
        facts = replace(facts, **{pending: 1})
    assert not facts.gate_passed
    task = _task_row(state="session_draining", epoch=0, row_version=0)
    task.update(target_platform="h5", task_status="running", workflow_version="v2")
    gate_id = build_completion_gate_snapshot(
        task_id=TASK_ID,
        finalization_epoch=0,
        evaluation_sequence=1,
        facts=facts,
        evaluated_at=datetime(2026, 10, 12, 14, 16, 3, tzinfo=UTC),
    ).gate_snapshot_id
    connection = FakeConnection(
        fetchrow_results=[task, _task_row(state="session_draining", epoch=0, row_version=1)],
        fetchval_results=[1, gate_id],
    )

    async def read_facts(*args):
        return {
            "evaluated_at": datetime(2026, 10, 12, 14, 16, 3, tzinfo=UTC),
            "h5_rule_inventory_missing_count": 69,
            "h5_execution_inflight_work_count": int(pending == "inspection_work"),
        }, facts

    monkeypatch.setattr(repository, "_read_completion_gate_facts", read_facts)
    command = dict(
        task_id=TASK_ID,
        agent_run_id=RUN_ID,
        expected_finalization_epoch=0,
        expected_row_version=0,
        execution_status=TaskExecutionStatus.FAILED,
        quality_outcome=TaskQualityOutcome.UNKNOWN,
    )
    service = repository.PostgresTaskFinalizationRepository(pool=FakePool(connection))
    if formal:
        decision = await service.finalize_task_with_report(
            command=FinalizeTaskWithReportCommand(
                **command,
                report_id="offline",
                report_version=1,
                final_report={},
            )
        )
    else:
        decision = await service.evaluate_and_seal(command=EvaluateCompletionGateCommand(**command))
    queries = [q for _, q, _ in connection.calls]
    blocked_updates = [
        args
        for _, q, args in connection.calls
        if "UPDATE qc_task" in q and "WHEN $5::boolean THEN 'infinity'" in q
    ]
    assert len(blocked_updates) == 1
    assert blocked_updates[0][-1] is (pending is None)
    assert decision.finalization.state is TaskFinalizationState.SESSION_DRAINING
    assert decision.finalization.execution_status is None
    assert decision.finalization.quality_outcome is None
    assert not any("execution_status =" in q or "quality_outcome =" in q for q in queries)
    assert not decision.snapshot.gate_passed
    assert not any(
        "finalization_state = 'sealed'" in q or "INSERT INTO qc_report_gate_binding" in q
        for q in queries
    )


def test_measured_review_has_a_concrete_missing_coverage_reason_not_live_execution():
    facts = _facts()
    assert CASE["task"]["finalization_agent_run_id"] == CASE["run_id"]
    assert facts.exploration_terminal
    assert repository._execution_resources_drained(facts, execution_inflight_work_count=0)
    assert not facts.h5_coverage_satisfied
    assert not facts.review_policy_satisfied
    assert facts.candidate_rule_count - facts.terminal_candidate_rule_count == 69
    assert CASE["task"]["status"] == "review"
    assert CASE["task"]["finalization_retry_at"].startswith("9999-")
    assert [a["intent_payload"]["action_type"] for a in CASE["actions"]] == [
        "navigate",
        "observe",
    ]
