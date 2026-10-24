"""H5 报告必须保存实际覆盖身份，拒绝缺失、改写和跨周期输入。"""

from copy import deepcopy
from types import SimpleNamespace

import pytest

from services.api.app.application.h5_report_coverage import (
    bind_h5_report_coverage,
    h5_page_exploration_for_report,
)
from services.api.app.domain.exploration.h5_coverage import H5CoverageSnapshot
from services.api.app.domain.exploration.h5_frontier import H5TaskFrontierSnapshot


def coverage_payload(*, task_id="task-1", agent_run_id="run-1", epoch=3):
    snapshot = H5CoverageSnapshot.advance(
        previous=None,
        frontier=H5TaskFrontierSnapshot.create(
            max_pages=3, sampling_policy_summary={"include_page_types": ["home"]},
        ),
        location="https://example.test/home", capture_id="capture-1",
        page_type="home", page_state="stable",
        assessment={
            "execution_status": "accepted",
            "page_type_assessment": {"page_type": "home", "status": "covered"},
        },
        requires_auth=False, overlay_present=False,
    )
    return {
        **snapshot.to_dict(), "coverage_snapshot_id": "coverage-1",
        "task_id": task_id, "agent_run_id": agent_run_id, "finalization_epoch": epoch,
    }


def _report(coverage):
    return {
        "task": {"task_id": "task-1"}, "agent_run_id": "run-1",
        "is_final": True, "report_stage": "final",
        "completion_gate": {
            "gate_passed": True, "gate_snapshot_id": "gate-1", "finalization_epoch": 4,
        },
        "page_exploration": h5_page_exploration_for_report(
            {"page_results": [{"page_capture_id": "capture-1"}]},
            coverage=coverage, task_id="task-1", agent_run_id="run-1",
        ),
    }


def _bind(report, coverage):
    bind_h5_report_coverage(
        report, coverage=coverage, task_id="task-1", agent_run_id="run-1",
        gate_snapshot_id="gate-1", gate_finalization_epoch=4,
    )


def test_h5_report_binds_coverage_source_epoch_and_gate_seal_epoch_separately():
    coverage = coverage_payload()
    original = deepcopy(coverage)
    report = _report(coverage)

    _bind(report, coverage)

    frozen = report["page_exploration"]["h5_coverage"]
    assert frozen["coverage_snapshot_id"] == "coverage-1"
    assert frozen["payload_hash"] == coverage["payload_hash"]
    assert frozen["finalization_epoch"] == 3
    assert frozen["gate_finalization_epoch"] == 4
    assert frozen["gate_snapshot_id"] == "gate-1"
    assert report["page_exploration"]["page_results"] == [{"page_capture_id": "capture-1"}]
    assert coverage == original
    _bind(report, coverage)
    assert report["page_exploration"]["h5_coverage"] == frozen


def test_h5_report_preserves_timeline_review_terminal_when_coverage_is_complete():
    page_exploration = h5_page_exploration_for_report(
        {
            "complete": False,
            "quality_status": "needs_review",
            "requires_review": True,
            "terminal_reason": "review_policy_unsatisfied",
        },
        coverage=coverage_payload(),
        task_id="task-1",
        agent_run_id="run-1",
    )

    assert page_exploration["complete"] is False
    assert page_exploration["quality_status"] == "needs_review"
    assert page_exploration["requires_review"] is True
    assert page_exploration["terminal_reason"] == "review_policy_unsatisfied"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("task_id", "old-task"), ("agent_run_id", "old-run"),
        ("finalization_epoch", 2), ("coverage_snapshot_id", "coverage-old"),
        ("payload_hash", "tampered"), ("covered_page_types", []),
    ],
)
def test_h5_binding_rejects_report_identity_or_content_drift(field, value):
    coverage = coverage_payload()
    report = _report(coverage)
    report["page_exploration"]["h5_coverage"][field] = value

    with pytest.raises(ValueError):
        _bind(report, coverage)


def test_h5_binding_rejects_summary_without_authority():
    report = _report(coverage_payload())
    report["page_exploration"] = {"complete": True}
    with pytest.raises(ValueError, match="缺少 Coverage"):
        _bind(report, coverage_payload())


@pytest.mark.parametrize("field", ["task", "agent_run_id", "completion_gate", "is_final"])
def test_h5_binding_rejects_mismatching_report_body(field):
    coverage = coverage_payload()
    report = _report(coverage)
    report[field] = None
    with pytest.raises(ValueError, match="报告正文"):
        _bind(report, coverage)


def test_h5_binding_rejects_inconsistent_completion():
    coverage = coverage_payload()
    report = _report(coverage)
    report["page_exploration"]["complete"] = False
    with pytest.raises(ValueError, match="探索结论"):
        _bind(report, coverage)


@pytest.mark.parametrize(
    ("missing", "timeline_run"), [(True, "run-1"), (False, "other-run"), (False, None)],
)
async def test_worker_rejects_missing_snapshot_or_different_timeline_run(missing, timeline_run):
    from services.api.app import worker

    class Reader:
        async def fetch_h5_coverage_snapshot(self, **kwargs):
            return None if missing else coverage_payload()

        async def page_timeline(self, task_id):
            assert not missing
            return {"run_id": timeline_run, "exploration_summary": {"complete": True}}

    record = SimpleNamespace(
        task_id="task-1", run_id="run-1",
        task_config={"workflow_version": "v2", "_task_target": {"platform": "h5"}},
    )
    with pytest.raises(RuntimeError, match="Coverage Snapshot|时间线与 Run"):
        await worker._page_exploration_for_report(record, reader=Reader())


def test_blocked_preview_does_not_claim_gate_accepted_its_coverage():
    from services.api.app import worker

    record = SimpleNamespace(final_report=_report(coverage_payload()))
    worker._attach_completion_gate(
        record,
        SimpleNamespace(
            gate_passed=False, gate_snapshot_id="blocked-gate", finalization_epoch=3,
            block_reasons=("pending_action",),
        ),
    )
    assert record.final_report["page_exploration"]["h5_coverage"]["gate_snapshot_id"] is None
    assert record.final_report["completion_gate"]["gate_snapshot_id"] == "blocked-gate"
