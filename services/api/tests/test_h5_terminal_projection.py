"""H5 探索终态投影合同测试。"""

from __future__ import annotations

import pytest

from services.api.app.application.h5_terminal_projection import (
    h5_target_coverage_ready,
    project_h5_terminal,
)


def _project(
    *,
    work_status: str = "succeeded",
    assessment_scope_ready: bool = True,
    coverage_ready: bool = True,
    assessments: list[dict[str, object]] | None = None,
    inventory: list[dict[str, object]] | None = None,
    closure: dict[str, object] | None = None,
) -> dict[str, object]:
    return project_h5_terminal(
        work_items=[{"status": work_status}],
        frontier={"pending_count": 0, "blocked_count": 0, "failed_count": 0},
        assessments=assessments or [],
        inventory=inventory or [],
        full_inspection_closure=closure,
        assessment_scope_ready=assessment_scope_ready,
        coverage_ready=coverage_ready,
    )


@pytest.mark.parametrize(
    ("work_status", "terminal_reason", "quality_status"),
    [
        ("pending", "exploration_incomplete", "in_progress"),
        ("leased", "exploration_incomplete", "in_progress"),
        ("running", "exploration_incomplete", "in_progress"),
        ("retry_wait", "exploration_incomplete", "in_progress"),
        ("cancelled", "work_cancelled", "needs_review"),
        ("failed", "work_failed", "technical_failed"),
        ("bounded", "work_bounded", "needs_review"),
        ("budget_exhausted", "work_budget_exhausted", "needs_review"),
        ("blocked", "work_blocked", "needs_review"),
        ("no_progress", "work_no_progress", "needs_review"),
    ],
)
def test_h5_work_status_has_stable_terminal_projection(
    work_status: str,
    terminal_reason: str,
    quality_status: str,
) -> None:
    result = _project(work_status=work_status)

    assert result == {
        "complete": False,
        "quality_status": quality_status,
        "requires_review": quality_status == "needs_review",
        "terminal_reason": terminal_reason,
    }


@pytest.mark.parametrize(
    ("changes", "terminal_reason", "quality_status"),
    [
        (
            {"assessment_scope_ready": False},
            "assessment_scope_incomplete",
            "needs_review",
        ),
        ({"coverage_ready": False}, "coverage_scope_incomplete", "needs_review"),
        (
            {"assessments": [{"status": "requires_auth"}]},
            "authentication_required",
            "needs_review",
        ),
        (
            {"assessments": [{"status": "blocked_by_safety"}]},
            "blocked_by_safety",
            "needs_review",
        ),
        (
            {"assessments": [{"status": "evidence_insufficient"}]},
            "evidence_insufficient",
            "needs_review",
        ),
        (
            {"inventory": [{"status": "failed"}]},
            "evidence_failed",
            "technical_failed",
        ),
        (
            {"closure": {"failed_inspection_count": 1}},
            "inspection_failed",
            "technical_failed",
        ),
        (
            {"closure": {"nonterminal_inspection_count": 1}},
            "inspection_pending",
            "needs_review",
        ),
        (
            {
                "closure": {
                    "gate_snapshot_id": "gate-1",
                    "gate_passed": False,
                    "gate_block_reasons": ["review_pending"],
                }
            },
            "review_pending",
            "needs_review",
        ),
    ],
)
def test_h5_terminal_fact_gaps_have_stable_projection(
    changes: dict[str, object],
    terminal_reason: str,
    quality_status: str,
) -> None:
    result = _project(**changes)

    assert result["complete"] is False
    assert result["quality_status"] == quality_status
    assert result["terminal_reason"] == terminal_reason


def test_h5_terminal_projection_completes_only_with_all_required_facts() -> None:
    result = _project()

    assert result == {
        "complete": True,
        "quality_status": "complete",
        "requires_review": False,
        "terminal_reason": None,
    }


def test_h5_terminal_projection_ignores_closure_counts_before_gate_exists() -> None:
    result = _project(
        closure={
            "inspection_count": 0,
            "nonterminal_inspection_count": 0,
            "failed_inspection_count": 0,
            "gate_snapshot_id": None,
            "gate_passed": None,
            "gate_block_reasons": [],
        }
    )

    assert result == {
        "complete": True,
        "quality_status": "complete",
        "requires_review": False,
        "terminal_reason": None,
    }


def test_h5_target_coverage_requires_nonempty_closed_scope() -> None:
    complete = {
        "completion_ready": True,
        "assessment_scope_ready": True,
        "required_page_types": ["content_list"],
        "missing_page_types": [],
    }

    assert h5_target_coverage_ready(complete) is True
    assert h5_target_coverage_ready({**complete, "required_page_types": []}) is False
    assert h5_target_coverage_ready({**complete, "missing_page_types": ["content_detail"]}) is False
    assert h5_target_coverage_ready({**complete, "completion_ready": False}) is False
    assert h5_target_coverage_ready(None) is False
