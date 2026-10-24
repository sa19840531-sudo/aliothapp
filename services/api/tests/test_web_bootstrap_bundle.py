"""Web 无父入口只在实际首跳意图和双采链同时成立时形成证据闭包。"""

import pytest

from services.api.app.application.web_evidence_bundle import build_web_evidence_bundle
from services.api.tests.test_web_evidence_bundle import _valid_snapshot


def bootstrap_snapshot():
    snapshot = _valid_snapshot()
    snapshot.update(
        target_platform="web",
        workflow_version="v2",
        session_owner="web-v2-workflow",
        finalization_epoch=0,
    )
    snapshot["captures"] = [row for row in snapshot["captures"] if row["capture_id"] != "bootstrap"]
    for capture in snapshot["captures"]:
        capture.update(finalization_epoch=0, page_finalization_epoch=0)
        if capture["capture_id"] == "home-nav:before":
            capture.update(
                url="about:blank",
                parent_capture_id=None,
                page_parent_capture_id=None,
                page_type=None,
                logical_page_id=None,
            )
    action = snapshot["actions"][0]
    action.update(
        intent_id="web-bootstrap:run-web",
        requested_source_capture_id=None,
        action_type="navigate",
        web_capture_stage="web_entry_probe",
        initial_surface_action=True,
        intent_source_capture_id="web-entry:run-web",
        intent_surface_kind="web",
        intent_task_id="task-web",
        intent_session_id="web:run-web",
        finalization_epoch=0,
    )
    return snapshot


def test_strict_web_bootstrap_blank_before_is_included_without_fabricated_parent():
    bundle = build_web_evidence_bundle(bootstrap_snapshot())
    assert bundle["status"] == "evidence_ready", bundle["reason_codes"]
    assert any(row["capture_id"] == "home-nav:before" for row in bundle["captures"])


@pytest.mark.parametrize(
    "mutation", ["ordinary", "h5", "run", "sequence", "parent", "epoch", "platform"]
)
def test_null_requested_source_alone_never_authorizes_a_web_bootstrap(mutation):
    snapshot = bootstrap_snapshot()
    action = snapshot["actions"][0]
    before = next(row for row in snapshot["captures"] if row["capture_id"] == "home-nav:before")
    if mutation == "ordinary":
        action["initial_surface_action"] = False
    elif mutation == "h5":
        action["intent_source_capture_id"] = "h5-entry:run-web"
    elif mutation == "run":
        action["intent_source_capture_id"] = "web-entry:other-run"
    elif mutation == "sequence":
        action["command_sequence"] = 2
        for row in snapshot["captures"]:
            if row["action_execution_id"] == action["action_execution_id"]:
                row.update(session_command_sequence=2, page_session_command_sequence=2)
    elif mutation == "parent":
        before["parent_capture_id"] = "hidden-parent"
    elif mutation == "epoch":
        before["finalization_epoch"] = 1
        before["page_finalization_epoch"] = 1
    else:
        snapshot["target_platform"] = "h5"
    assert build_web_evidence_bundle(snapshot)["status"] == "needs_review"
