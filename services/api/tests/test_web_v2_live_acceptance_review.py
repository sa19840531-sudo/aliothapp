"""验收审查回归：硬退出门和真实持久证据关系。"""

import json
from copy import deepcopy

import pytest

from scripts.validation import web_acceptance_source as source
from scripts.validation import web_v2_live_acceptance as cli
from services.api.tests.test_web_v2_live_acceptance_reporter import snapshot


def database_rows():
    return {
        "run": {
            "status": "done",
            "session_status": "closed",
            "finalization_state": "sealed",
            "payload": {"budget": {"menu_limit": 10}},
        },
        "checkpoints": [
            {
                "logical_page_key": "home",
                "current_stage": "session_close",
                "stage_status": "succeeded",
                "gap_codes": [],
                "cursor_payload": {"capture_id": "home"},
            }
        ],
        "captures": [
            {
                "capture_id": "home",
                "role": "primary",
                "capture_purpose": "web_page_ready_baseline",
                "final_url": "https://site.test/",
                "page_type": "home",
                "assessment": {},
                "navigation": [],
                "status_code": 200,
                "reachable": True,
            }
        ],
        "artifacts": [],
        "phases": [],
        "actions": [],
        "frontier": [],
    }


def full_page_rows():
    rows = database_rows()
    rows["captures"].append(
        {
            "capture_id": "full",
            "parent_capture_id": "home",
            "page_parent_capture_id": "home",
            "role": "supplemental",
            "page_role": "supplemental",
            "capture_purpose": "web_full_page_supplemental",
            "page_capture_purpose": "web_full_page_supplemental",
            "finalization_epoch": "4",
            "page_finalization_epoch": "4",
            "command_sequence": 8,
            "final_url": "https://site.test/",
            "page_type": "home",
            "assessment": {},
            "navigation": [],
        }
    )
    rows["artifacts"].append(
        {
            "artifact_id": "full-png",
            "artifact_type": "screenshot",
            "content_type": "image/png",
            "capture_id": "full",
        }
    )
    rows["phases"].append(
        {
            "phase_key": "web_full_page_supplemental",
            "status": "completed",
            "phase_protocol_version": "web-full-page-supplemental.v1",
            "source_capture_id": "home",
            "evidence_artifact_ids": ["full-png"],
            "finalization_epoch": 4,
            "command_sequence": 8,
        }
    )
    return rows


@pytest.fixture
def configure_cli(tmp_path, monkeypatch):
    monkeypatch.setenv("ALIOTH_LIVE_TOKEN", "private-token")
    monkeypatch.setenv("ALIOTH_LIVE_DATABASE_URL", "private-dsn")
    monkeypatch.setenv("ALIOTH_LIVE_REDIS_URL", "private-redis")

    def configure(current, *, second_site=False):
        for capture in current.get("captures", []):
            for artifact in capture.get("artifacts", []):
                artifact_path = tmp_path / artifact["artifact_id"]
                artifact_path.write_bytes(b"validation-evidence")
                artifact["local_path"] = str(artifact_path)

        async def read(*_args):
            return deepcopy(current)

        async def workers(*_args):
            return 1

        monkeypatch.setattr(cli, "read_database_state", read)
        monkeypatch.setattr(cli, "read_web_worker_count", workers)
        state = {
            "worker_count": 1,
            "site_timeout_seconds": 1800,
            "sites": [
                {
                    "task_id": "task-1",
                    "run_id": "run-1",
                    "requested_url": "https://site.test/",
                    "directory": "site",
                    "deadline": 9999999999,
                    "extension_count": 0,
                }
            ],
        }
        if second_site:
            state["sites"].append(
                {
                    "requested_url": "https://next.test/",
                    "directory": "next",
                    "idempotency_key": "next",
                }
            )
        cli.write_json(tmp_path / "validation-state.json", state)
        return tmp_path

    return configure


@pytest.mark.parametrize(
    "defect,flag",
    [(None, False), ("minimum", False), ("artifact", False), ("minimum", True), ("artifact", True)],
)
def test_real_cli_report_always_enforces_site_and_artifact_gates(configure_cli, defect, flag):
    current = snapshot()
    current.update(
        menu_limit=10,
        capabilities={
            "list_structure_clusters": ["l1", "l2"],
            "detail_structure_clusters": ["d1", "d2", "d3", "d4"],
            "age_gate": True,
            "popup": True,
            "cross_origin_recovery": True,
            "ad_isolation": True,
            "long_page": True,
        },
    )
    if defect == "minimum":
        current["checkpoints"][0]["stage_status"] = "running"
    if defect == "artifact":
        current["checkpoints"][0]["artifact_ids"] = ["missing-png"]
    output = configure_cli(current)
    arguments = ["report", "--output", str(output)] + (["--verify-artifacts"] if flag else [])
    assert cli.main(arguments) == (2 if defect else 0)
    report = json.loads((output / "final-report.json").read_text(encoding="utf-8"))
    assert report["sites"][0]["task_id"] == "task-1"


@pytest.mark.parametrize(
    "mutation", ["no-artifact", "viewport", "wrong-parent", "wrong-epoch", "non-png", "no-phase"]
)
def test_long_page_rejects_phase_only_and_invalid_supplemental_relationship(mutation):
    rows = full_page_rows()
    if mutation == "no-artifact":
        rows["artifacts"] = []
    elif mutation == "viewport":
        rows["captures"][1]["capture_purpose"] = "web_page_ready_baseline"
    elif mutation == "wrong-parent":
        rows["captures"][1]["parent_capture_id"] = "unrelated"
    elif mutation == "wrong-epoch":
        rows["captures"][1]["finalization_epoch"] = "3"
    elif mutation == "non-png":
        rows["artifacts"][0]["content_type"] = "image/jpeg"
    else:
        rows["phases"] = []
    state = source.project_database_rows(rows, task_id="task", run_id="run")
    assert state["capabilities"]["long_page"] is False


@pytest.mark.parametrize(
    ("purpose", "expected"),
    [
        ("web_direct_full_page", True),
        ("ready_primary", False),
        (None, True),
    ],
)
def test_single_png_long_page_requires_direct_purpose_or_historical_absence(purpose, expected):
    rows = full_page_rows()
    rows["artifacts"][0]["screenshot_purpose"] = purpose
    state = source.project_database_rows(rows, task_id="task", run_id="run")
    assert state["capabilities"]["long_page"] is expected


def test_long_page_exports_formally_linked_supplemental_png():
    state = source.project_database_rows(full_page_rows(), task_id="task", run_id="run")
    assert state["capabilities"]["long_page"] is True
    full = next(row for row in state["captures"] if row["capture_id"] == "full")
    assert full["full_page_screenshot_artifact_id"] == "full-png"
    assert full["parent_capture_id"] == "home"
    assert full["capture_purpose"] == "web_full_page_supplemental"


@pytest.mark.parametrize(
    "defect",
    [None, "missing-manifest", "wrong-capture", "wrong-reference", "wrong-mode", "extra-id"],
)
def test_scrolled_viewport_report_requires_linked_png_and_manifest(defect):
    rows = full_page_rows()
    manifest = {
        "artifact_id": "scroll-manifest",
        "artifact_type": "page_coverage",
        "content_type": "application/json",
        "capture_id": "full",
        "coverage_metadata": {
            "protocol_version": "web-scrolled-viewport-manifest.v1",
            "capture_mode": "scroll_offset_viewport",
            "associated_page_capture_id": "full",
            "associated_screenshot_artifact_ids": ["full-png"],
        },
    }
    rows["artifacts"].append(manifest)
    rows["phases"][0]["evidence_artifact_ids"] = ["full-png", "scroll-manifest"]
    if defect == "missing-manifest":
        rows["artifacts"].pop()
    elif defect == "wrong-capture":
        manifest["capture_id"] = "home"
    elif defect == "wrong-reference":
        manifest["coverage_metadata"]["associated_screenshot_artifact_ids"] = ["unrelated"]
    elif defect == "wrong-mode":
        manifest["coverage_metadata"]["capture_mode"] = "viewport"
    elif defect == "extra-id":
        rows["phases"][0]["evidence_artifact_ids"].append("unrelated")

    state = source.project_database_rows(rows, task_id="task", run_id="run")
    assert state["capabilities"]["long_page"] is (defect is None)
    full = next(row for row in state["captures"] if row["capture_id"] == "full")
    assert full.get("full_page_screenshot_artifact_id") == ("full-png" if defect is None else None)


@pytest.mark.parametrize(
    "dismissal_count,remaining,expected", [(2, False, True), (0, False, False), (2, True, False)]
)
def test_popup_uses_persisted_obstruction_chain_without_invented_page_type(
    dismissal_count, remaining, expected
):
    rows = database_rows()
    rows["captures"][0]["obstruction_chain"] = {
        "enabled": True,
        "detected": True,
        "dismissal_count": dismissal_count,
        "outcome": "stable",
        "remaining_obstruction": remaining,
        "secret": "private-body",
    }
    state = source.project_database_rows(rows, task_id="task", run_id="run")
    assert state["capabilities"]["popup"] is expected
    assert "private-body" not in json.dumps(state)


def test_popup_uses_durable_completed_readiness_click_chain() -> None:
    rows = database_rows()
    rows["actions"] = [
        {
            "action_execution_id": "readiness-one",
            "status": "completed",
            "action": "click",
            "intent_id": f"web-readiness:click:{index}",
            "result_capture_id": f"after-{index}",
            "error_code": None,
        }
        for index in range(2)
    ]

    state = source.project_database_rows(rows, task_id="task", run_id="run")

    assert state["capabilities"]["popup"] is True


def test_popup_rejects_incomplete_or_non_readiness_clicks() -> None:
    rows = database_rows()
    rows["actions"] = [
        {
            "action_execution_id": "age-confirm",
            "status": "completed",
            "action": "click",
            "intent_id": "web-readiness:click:one",
            "result_capture_id": "after-one",
            "error_code": None,
        },
        {
            "status": "completed",
            "action": "click",
            "intent_id": "web-age-gate:confirm",
            "result_capture_id": "after-age",
            "error_code": None,
        },
        {
            "action_execution_id": "readiness-failed",
            "status": "failed",
            "action": "click",
            "intent_id": "web-readiness:click:failed",
            "result_capture_id": None,
            "error_code": "WEB_ACTION_FAILED",
        },
    ]

    state = source.project_database_rows(rows, task_id="task", run_id="run")

    assert state["capabilities"]["popup"] is False


def test_home_success_does_not_change_candidate_tls_failure_category():
    rows = database_rows()
    rows["captures"].append(
        {
            "capture_id": "failed-candidate",
            "role": "after_action",
            "final_url": "https://site.test/candidate",
            "page_type": "unknown",
            "assessment": {},
            "navigation": [],
            "status_code": None,
            "reachable": False,
        }
    )
    rows["checkpoints"].append(
        {
            "logical_page_key": "candidate",
            "current_stage": "candidate_navigation",
            "stage_status": "gapped",
            "gap_codes": ["WEB_NAVIGATION_RETRY_EXHAUSTED"],
            "cursor_payload": {},
            "last_error_code": "WEB_GOTO_TLS_FAILED",
            "last_error_detail": {"capture_id": "failed-candidate"},
        }
    )
    rows["actions"].append(
        {
            "action_execution_id": "tls-action",
            "error_code": "WEB_GOTO_TLS_FAILED",
            "state_summary": {"status_code": None, "reachable": False},
        }
    )
    state = source.project_database_rows(rows, task_id="task", run_id="run")
    assert len(state["failures"]) == 2
    assert all(row["category"] == "external_transport" for row in state["failures"])
    assert all(row["response_received"] is False for row in state["failures"])
    assert state["root_failure"] is None


@pytest.mark.parametrize(
    "summary,expected",
    [({}, None), ({"reachable": False}, None), ({"status_code": 200, "reachable": False}, True)],
)
def test_candidate_failure_needs_its_own_response_evidence(summary, expected):
    rows = database_rows()
    rows["actions"] = [
        {
            "action_execution_id": "tls",
            "error_code": "WEB_GOTO_TLS_FAILED",
            "state_summary": summary,
        }
    ]
    state = source.project_database_rows(rows, task_id="task", run_id="run")
    assert state["failures"][0]["response_received"] is expected
    assert state["failures"][0]["category"] == "code_or_evidence"


def test_missing_phase_and_capture_sequence_cannot_prove_formal_long_page():
    rows = full_page_rows()
    rows["captures"][1].pop("command_sequence")
    rows["phases"][0].pop("command_sequence")
    state = source.project_database_rows(rows, task_id="task", run_id="run")
    assert state["capabilities"]["long_page"] is False


@pytest.mark.parametrize("prior_observation_timeout", [False, True])
def test_aborted_is_terminal_and_cli_releases_slot(
    configure_cli, monkeypatch, prior_observation_timeout
):
    current = snapshot()
    current.update(status="aborted", menu_limit=10)
    assert cli.SiteMinimumWitnessPolicy().evaluate(current).complete
    output = configure_cli(current, second_site=True)
    manifest = json.loads((output / "validation-state.json").read_text())
    manifest["sites"][0]["observation_timeout"] = prior_observation_timeout
    cli.write_json(output / "validation-state.json", manifest)

    async def request(_self, method, path, **_kwargs):
        assert method == "POST" and path == "/api/v2/exploration-tasks"
        return {"task_id": "task-next", "run_id": "run-next"}

    monkeypatch.setattr(cli.AcceptanceClient, "request", request)
    monkeypatch.setattr(cli.asyncio, "sleep", lambda *_args: fail_sleep())
    assert cli.main(["monitor", "--output", str(output)]) == 0
    state = json.loads((output / "validation-state.json").read_text())
    assert state["sites"][0]["terminal"] is True
    assert state["sites"][1]["task_id"] == "task-next"


async def fail_sleep():
    """下一次轮询立即继续，测试不等待真实两分钟。"""
    return None
