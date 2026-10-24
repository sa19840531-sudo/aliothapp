"""128 晚到资源不是假通过；在途四项工作不能触发停止重试的复核投影。"""

import json
from dataclasses import replace
from datetime import datetime
from pathlib import Path

import pytest

from services.api.app.domain.task import TaskExecutionStatus
from services.api.app.domain.task.completion import CompletionGateFacts
from services.api.app.infrastructure.db.task_finalization_repository import (
    _defer_blocked_completion_gate,
    _should_mark_task_review,
)

CASES = json.loads(
    (Path(__file__).parent / "fixtures/h5_batch_20261011_232739.json").read_text()
)["jha128"]


def _facts():
    return CompletionGateFacts(
        exploration_terminal=True, pending_action_count=0, pending_capture_commit_count=0,
        active_session_request_count=0,
        background_execution_count=0, review_policy_satisfied=False,
        h5_coverage_satisfied=False, **CASES["first_gate"],
    )


def test_128_measured_main_terminal_is_not_a_settlement_or_acceptance_fact():
    timeline = CASES["timeline_utc"]
    times = [datetime.fromisoformat(timeline[key]) for key in (
        "main_finished", "publication", "request", "consumer_finished",
    )] + [datetime.fromisoformat(value) for value in timeline["gates"]]
    assert times == sorted(times)
    assert CASES["first_gate"]["nonterminal_enforced_work_item_count"] == 4
    assert CASES["last_gate"]["nonterminal_enforced_work_item_count"] == 0


def test_128_measured_late_work_must_not_be_parked_for_review():
    facts = _facts()
    assert not _should_mark_task_review(
        execution_status=TaskExecutionStatus.FAILED, facts=facts,
        publication_review_required_count=1, h5_rule_inventory_missing_count=56,
        allow_natural_evidence_review=True, require_execution_drained=True,
    )
    assert facts.gate_passed is False


@pytest.mark.parametrize("field", [
    "pending_action_count", "pending_capture_commit_count",
    "active_session_request_count", "background_execution_count",
])
def test_review_waits_for_each_inflight_resource(field):
    facts = replace(_facts(), nonterminal_enforced_work_item_count=0,
                    candidate_rule_count=1, terminal_candidate_rule_count=1, **{field: 1})
    assert not _should_mark_task_review(
        execution_status=TaskExecutionStatus.FAILED, facts=facts,
        publication_review_required_count=1, allow_natural_evidence_review=True,
        require_execution_drained=True,
    )


def test_128_drained_late_work_can_be_review_but_never_pass():
    facts = replace(_facts(), **CASES["last_gate"])
    assert _should_mark_task_review(
        execution_status=TaskExecutionStatus.FAILED, facts=facts,
        publication_review_required_count=1, h5_rule_inventory_missing_count=53,
        allow_natural_evidence_review=True, require_execution_drained=True,
    )
    assert facts.gate_passed is False


def test_existing_non_h5_review_projection_is_unchanged():
    facts = replace(_facts(), candidate_rule_count=1, terminal_candidate_rule_count=1)
    assert _should_mark_task_review(
        execution_status=TaskExecutionStatus.FAILED, facts=facts,
        publication_review_required_count=1,
    )


@pytest.mark.parametrize("pending", [0, 4])
async def test_unchanged_gate_parks_only_after_h5_execution_drains(pending):
    class Connection:
        async def execute(self, query, *args):
            self.query, self.args = query, args

    connection = Connection()
    await _defer_blocked_completion_gate(
        connection, task_id="task", finalization_epoch=0, gate_snapshot_id="gate",
        facts=replace(_facts(), nonterminal_enforced_work_item_count=pending),
    )
    assert connection.args[-1] is (pending == 0)
    assert "target_platform = 'h5' AND NOT $4::boolean" in connection.query


@pytest.mark.parametrize("inflight", [0, 4])
async def test_terminal_publication_review_is_not_inflight_execution(inflight):
    facts = replace(_facts(), candidate_rule_count=1, terminal_candidate_rule_count=1)
    assert _should_mark_task_review(
        execution_status=TaskExecutionStatus.FAILED, facts=facts,
        publication_review_required_count=4, require_execution_drained=True,
        execution_inflight_work_count=inflight,
    ) is (inflight == 0)
    assert not facts.gate_passed

    class Connection:
        async def execute(self, query, *args):
            self.args = args

    connection = Connection()
    await _defer_blocked_completion_gate(
        connection, task_id="task", finalization_epoch=0, gate_snapshot_id="gate",
        facts=facts, execution_inflight_work_count=inflight,
    )
    assert connection.args[-1] is (inflight == 0)


def test_drained_publication_review_keeps_missing_rule_visible_without_polling_forever():
    facts = replace(_facts(), candidate_rule_count=2, terminal_candidate_rule_count=1)
    assert _should_mark_task_review(
        execution_status=TaskExecutionStatus.FAILED, facts=facts,
        publication_review_required_count=1, h5_rule_inventory_missing_count=1,
        require_execution_drained=True, execution_inflight_work_count=0,
    )
    assert not facts.gate_passed
