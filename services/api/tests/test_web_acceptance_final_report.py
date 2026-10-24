"""纯 Web 探索终稿事实投影不得混同正式质检封存。"""

import json

import pytest

from scripts.validation.web_acceptance_source import project_database_rows


def rows():
    return {
        "run": {
            "status": "failed",
            "target_platform": "web",
            "execution_mode": "exploration_only",
            "finalization_state": "open",
            "session_status": "closed",
            "payload": {"budget": {"menu_limit": 5}},
            "final_report_witness": {
                "agent_run_id": "run-1",
                "task_id": "task-1",
                "is_final": True,
                "report_stage": "final",
                "technical_execution_complete": True,
            },
        },
        "checkpoints": [],
        "captures": [],
        "artifacts": [],
        "actions": [],
        "frontier": [],
        "phases": [],
    }


def project(data):
    return project_database_rows(data, task_id="task-1", run_id="run-1")


@pytest.mark.parametrize("status", ["failed", "completed", "canceled"])
@pytest.mark.parametrize("serialized", [False, True])
def test_real_exploration_final_report_is_recognized_without_changing_run_status(
    status, serialized,
):
    data = rows()
    data["run"]["status"] = status
    if serialized:
        data["run"]["final_report_witness"] = json.dumps(data["run"]["final_report_witness"])

    state = project(data)

    assert state["report_finalized"] is True
    assert state["status"] == status
    assert "final_report_witness" not in state


@pytest.mark.parametrize(
    "field,value",
    [
        ("agent_run_id", "another-run"),
        ("task_id", "another-task"),
        ("is_final", False),
        ("is_final", "true"),
        ("report_stage", "preview"),
        ("technical_execution_complete", False),
        ("technical_execution_complete", "true"),
    ],
)
def test_incomplete_or_misattributed_final_report_is_not_accepted(field, value):
    data = rows()
    data["run"]["final_report_witness"][field] = value
    assert project(data)["report_finalized"] is False


@pytest.mark.parametrize(
    "field,value",
    [
        ("status", "running"),
        ("status", "pending"),
        ("session_status", "active"),
        ("session_status", "lost"),
        ("target_platform", "app"),
        ("target_platform", "h5"),
        ("execution_mode", "full_inspection"),
        ("execution_mode", None),
        ("final_report_witness", None),
        ("final_report_witness", {}),
        ("final_report_witness", []),
        ("final_report_witness", True),
        ("final_report_witness", "invalid-json"),
        ("final_report_witness", "[]"),
    ],
)
def test_exploration_finality_requires_web_scope_and_terminal_closed_session(field, value):
    data = rows()
    data["run"][field] = value
    assert project(data)["report_finalized"] is False


def test_sealed_formal_report_retains_existing_contract():
    data = rows()
    data["run"].update(
        execution_mode="full_inspection", finalization_state="sealed", final_report_witness=None,
    )
    assert project(data)["report_finalized"] is True
