"""空 checkpoint 诊断须通过真实动作身份归因，不能按错误文本合并。"""

import json
from copy import deepcopy
from datetime import UTC, datetime

import pytest

from scripts.validation.web_acceptance_reporter import SiteMinimumWitnessPolicy
from scripts.validation.web_acceptance_source import project_database_rows
from services.api.app.application.web_exploration_workflow import WebSurfaceBootstrapper
from services.api.app.domain.exploration.web_run_stage_checkpoint import WebStageCursor
from services.api.app.domain.interaction import (
    ActionIntent,
    InteractionOperation,
    SurfaceKind,
    build_action_execution_id,
)
from services.api.app.infrastructure.db.web_run_stage_checkpoint_row import (
    web_run_stage_checkpoint_values,
)
from services.api.tests.test_web_v2_live_acceptance_review import database_rows

RUN = "00000000-0000-0000-0000-000000000010"
TASK = "00000000-0000-0000-0000-000000000011"
ERROR = "WEB_GOTO_TLS_FAILED"


def failed_navigation_rows(*, root=False, terminal=True):
    rows = database_rows()
    key = "home" if root else "candidate"
    payload = {"session_id": f"web:{RUN}", "finalization_epoch": 2}
    if not root:
        payload.update(menu_ledger_id=key, capture_id="home", parent_capture_id="home")
    cursor = WebStageCursor.start(run_id=RUN, logical_page_key=key, cursor_payload=payload)
    for _ in range(3):
        cursor = cursor.claim(worker_id="test", now=cursor.next_attempt_at or datetime.now(UTC))
        cursor = cursor.schedule_retry(error_code=ERROR, now=datetime.now(UTC))
    if terminal:
        while not cursor.is_terminal:
            cursor = cursor.claim(worker_id="test", now=datetime.now(UTC)).mark_succeeded()
    checkpoint = web_run_stage_checkpoint_values(cursor)
    rows["checkpoints"] = [checkpoint] if root else [rows["checkpoints"][0], checkpoint]
    intent_id = f"web-bootstrap:{RUN}" if root else f"web-discovery-menu:{RUN}:stable-hash"
    intent = ActionIntent(
        task_id=TASK,
        session_id=f"web:{RUN}",
        intent_id=intent_id,
        idempotency_key=intent_id,
        surface_kind=SurfaceKind.WEB,
        source_capture_id=f"web-entry:{RUN}" if root else "home",
        action_type=InteractionOperation.NAVIGATE,
        semantic_target="测试导航",
        purpose="验收失败关联",
        capture_profile=WebSurfaceBootstrapper.capture_profile(),
        deadline=datetime.now(UTC),
        parameters={},
    )
    rows["actions"] = [
        {
            "action_execution_id": build_action_execution_id(intent),
            "intent_id": intent_id,
            "idempotency_key": intent_id,
            "command_sequence": 1 if root else 5,
            "finalization_epoch": 2,
            "intent_source_capture_id": intent.source_capture_id,
            "initial_surface_action": root,
            "web_capture_stage": "web_entry_probe" if root else "web_page_probe",
            "web_menu_entry_id": None if root else key,
            "requested_source_capture_id": None if root else "home",
            "result_capture_id": None,
            "action": "navigate",
            "status": "failed",
            "error_code": ERROR,
            "state_summary": {"reachable": False, "status_code": None},
        }
    ]
    if root:
        rows["captures"] = []
    else:
        rows["frontier"] = [
            {"menu_entry_id": key, "menu_intent_id": intent_id, "menu_action_count": 1}
        ]
        rows["artifacts"] = [
            {"artifact_id": "home-png", "capture_id": "home", "artifact_type": "screenshot"}
        ]
    return rows


def project(rows):
    return project_database_rows(rows, task_id=TASK, run_id=RUN)


def test_optional_primary_diagnostic_is_reported_without_stage_gap():
    rows = database_rows()
    rows["phases"].append(
        {
            "phase_key": "web_stage_classification",
            "status": "completed",
            "source_capture_id": "home",
            "optional_primary_error_code": "WEB_PRIMARY_CONTENT_CAPTURE_FAILED",
        }
    )
    state = project(rows)
    assert state["gap_codes"] == []
    assert any(
        item["error_code"] == "WEB_PRIMARY_CONTENT_CAPTURE_FAILED"
        and item["category"] == "optional_primary"
        and item["blocks_code_acceptance"] is False
        for item in state["failures"]
    )


def test_full_page_gap_is_reported_as_nonblocking_supplemental_evidence():
    rows = database_rows()
    checkpoint = rows["checkpoints"][0]
    checkpoint.update(
        last_error_code="WEB_FULL_PAGE_SCREENSHOT_LIMIT_EXCEEDED",
        gap_codes=[
            "WEB_FULL_PAGE_SCREENSHOT_LIMIT_EXCEEDED",
            "WEB_FULL_PAGE_SCREENSHOT_FAILED",
        ],
    )

    state = project(rows)

    assert state["root_gap"] is False
    assert state["failures"] == [
        {
            "error_code": "WEB_FULL_PAGE_SCREENSHOT_LIMIT_EXCEEDED",
            "category": "supplemental_evidence",
            "blocks_code_acceptance": False,
            "logical_page_key": "home",
            "response_received": None,
            "capture_id": None,
        }
    ]


def test_optional_auxiliary_failure_is_reported_without_blocking_code_acceptance():
    from scripts.validation.web_acceptance_reporter import classify_validation_failure

    failure = classify_validation_failure(
        "WEB_AUXILIARY_CANDIDATE_SOURCE_MISMATCH", response_received=None
    )

    assert failure.category == "optional_auxiliary"
    assert failure.blocks_code_acceptance is False


@pytest.mark.parametrize("terminal", [False, True])
def test_empty_checkpoint_detail_deduplicates_its_persisted_navigation_action(terminal):
    rows = failed_navigation_rows(terminal=terminal)
    state = project(rows)
    assert len(state["failures"]) == 1
    failure = state["failures"][0]
    assert failure["category"] == "external_transport"
    assert failure["logical_page_key"] == "candidate"
    assert failure["action_execution_id"] == rows["actions"][0]["action_execution_id"]
    assert state["actions"][0]["action_execution_id"] == failure["action_execution_id"]
    assert state["captures"][0]["artifacts"][0]["artifact_id"] == "home-png"
    assert state["root_failure"] is None


def test_root_without_capture_has_explanatory_external_failure_from_bootstrap_action():
    state = project(failed_navigation_rows(root=True))
    assert len(state["failures"]) == 1
    assert state["root_failure"]["logical_page_key"] == "home"
    assert state["root_failure"]["response_received"] is False
    assert state["root_failure"]["blocks_code_acceptance"] is False
    assert SiteMinimumWitnessPolicy().evaluate(state).complete


@pytest.mark.parametrize(
    "field,value",
    [
        ("command_sequence", 2),
        ("initial_surface_action", False),
        ("intent_source_capture_id", "other-entry"),
        ("finalization_epoch", 1),
        ("intent_id", "other-intent"),
        ("web_capture_stage", "web_page_probe"),
    ],
)
def test_root_does_not_infer_bootstrap_from_unrelated_action(field, value):
    rows = failed_navigation_rows(root=True)
    rows["actions"][0][field] = value
    state = project(rows)
    assert state["root_failure"] is None
    assert any(row["blocks_code_acceptance"] for row in state["failures"])


@pytest.mark.parametrize(
    "defect", ["other-stage", "no-ledger", "wrong-intent", "wrong-menu", "ambiguous"]
)
def test_candidate_matching_requires_stage_and_unique_persisted_identity(defect):
    rows = failed_navigation_rows(terminal=False)
    if defect == "other-stage":
        rows["checkpoints"][1].update(current_stage="readiness", gaps="[]")
    elif defect == "no-ledger":
        rows["frontier"] = []
    elif defect == "wrong-intent":
        rows["actions"][0]["intent_id"] = "unrelated"
    elif defect == "wrong-menu":
        rows["actions"][0]["web_menu_entry_id"] = "unrelated"
    else:
        other = deepcopy(rows["actions"][0])
        other["action_execution_id"] = "another-action"
        other["command_sequence"] = 6
        rows["actions"].append(other)
    state = project(rows)
    assert any(row["blocks_code_acceptance"] for row in state["failures"])
    assert not any(
        row.get("action_execution_id") for row in state["failures"] if row.get("logical_page_key")
    )


def test_same_error_on_other_action_is_not_consumed_by_checkpoint_merge():
    rows = failed_navigation_rows()
    other = deepcopy(rows["actions"][0])
    other.update(action_execution_id="unrelated-action", intent_id="unrelated", command_sequence=9)
    rows["actions"].insert(0, other)
    state = project(rows)
    assert len(state["failures"]) == 2
    independent = next(
        row for row in state["failures"] if row["action_execution_id"] == "unrelated-action"
    )
    assert "logical_page_key" not in independent
    assert len(state["actions"]) == 2


@pytest.mark.parametrize(
    "summary", [{}, {"reachable": False}, {"reachable": False, "status_code": 200}]
)
def test_matched_failure_still_blocks_without_own_no_response_evidence(summary):
    rows = failed_navigation_rows()
    rows["actions"][0]["state_summary"] = summary
    state = project(rows)
    assert len(state["failures"]) == 1
    assert state["failures"][0]["blocks_code_acceptance"] is True


def test_matched_action_can_use_only_its_formal_result_capture_response():
    rows = failed_navigation_rows()
    action = rows["actions"][0]
    action.update(state_summary={}, result_capture_id="failed-result")
    result = deepcopy(rows["captures"][0])
    result.update(
        capture_id="failed-result",
        role="after_action",
        action_execution_id=action["action_execution_id"],
        command_sequence=5,
        status_code=None,
        reachable=False,
    )
    rows["captures"].append(result)
    state = project(rows)
    assert len(state["failures"]) == 1
    assert state["failures"][0]["category"] == "external_transport"
    assert state["failures"][0]["capture_id"] == "failed-result"
    result["action_execution_id"] = "unrelated"
    assert project(rows)["failures"][0]["blocks_code_acceptance"] is True


@pytest.mark.parametrize(
    "defect", ["missing-gap", "old-generation", "later-failure", "other-error"]
)
def test_later_or_unproven_checkpoint_failure_does_not_reuse_prior_navigation(defect):
    rows = failed_navigation_rows()
    checkpoint = rows["checkpoints"][1]
    gaps = json.loads(checkpoint["gaps"])
    if defect == "missing-gap":
        gaps = []
    elif defect == "old-generation":
        checkpoint["stage_generation"] += 1
    elif defect == "later-failure":
        # 后续清障阶段即使出现同码，也不能继承先前导航动作的响应。
        gaps.append({**gaps[-1], "stage": "obstruction_clear"})
    else:
        checkpoint["last_error_code"] = "WEB_PRIMARY_CAPTURE_FAILED"
    checkpoint["gaps"] = gaps
    state = project(rows)
    assert len(state["failures"]) == 2
    assert state["failures"][0]["blocks_code_acceptance"] is True
    assert "action_execution_id" not in state["failures"][0]


def test_own_result_response_overrides_conflicting_no_response_summary():
    rows = failed_navigation_rows()
    action = rows["actions"][0]
    action["result_capture_id"] = "responded"
    result = deepcopy(rows["captures"][0])
    result.update(
        capture_id="responded",
        role="after_action",
        action_execution_id=action["action_execution_id"],
        command_sequence=5,
    )
    rows["captures"].append(result)
    state = project(rows)
    assert len(state["failures"]) == 1
    assert state["failures"][0]["response_received"] is True
    assert state["failures"][0]["blocks_code_acceptance"] is True
