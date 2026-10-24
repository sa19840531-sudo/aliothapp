"""Web V2 证据包完整性与脱敏投影测试。"""

from copy import deepcopy

import pytest

from services.api.app.application.web_evidence_bundle import build_web_evidence_bundle
from services.api.tests.test_web_capture_readiness_metrics import _stable_capture_metrics


@pytest.mark.parametrize("value", ["WEB_NAVIGATION_CURRENT_UNTRUSTED", "私密异常原文", None])
def test_navigation_handoff_report_exposes_only_fixed_failure(value):
    snapshot = _valid_snapshot()
    snapshot["ledger"][0]["navigation_handoff_error"] = value
    entry = build_web_evidence_bundle(snapshot)["entries"][0]
    if value == "WEB_NAVIGATION_CURRENT_UNTRUSTED":
        assert entry["navigation_handoff_error"] == value
    else:
        assert "navigation_handoff_error" not in entry


def test_evidence_projects_runtime_menu_and_isolated_ad_without_sensitive_payload():
    from uuid import uuid4

    snapshot = _valid_snapshot()
    artifact_id = str(uuid4())
    snapshot["ledger"][0].update(
        source_kind="runtime_discovered", decision="runtime_observe_only", execution_slot=12,
        menu_path=["资讯"], candidate_url="https://reader:secret@example.test/news?token=hidden",
        ad_landing_evidence={
            "status": "return_failed", "source_url": "https://example.test/news?token=hidden",
            "landing_url": "https://ad.example.test/path?token=hidden", "fail_closed": True,
            "reason_code": "WEB_AD_LANDING_RETURN_FAILED", "candidate_kind": "banner",
            "artifact_refs": {"landing_dom": artifact_id, "token": "secret"},
            "artifact_ids": [artifact_id], "password": "secret", "raw_dom": "private",
        },
    )
    entry = build_web_evidence_bundle(snapshot)["entries"][0]
    assert entry["execution_slot"] == 12
    assert entry["menu_path"] == ["资讯"]
    assert entry["canonical_url"] == "https://example.test/news"
    assert entry["ad_landing_evidence"]["landing_url"] == "https://ad.example.test/path"
    assert entry["ad_landing_evidence"]["artifact_refs"] == {"landing_dom": artifact_id}
    assert entry["ad_landing_evidence"]["fail_closed"] is True
    assert "secret" not in str(entry) and "private" not in str(entry)


@pytest.mark.parametrize("limit", [None, 12])
def test_evidence_budget_accepts_unbounded_or_positive_frozen_limits(limit):
    snapshot = _valid_snapshot()
    snapshot["menu_limit"] = limit
    assert "budget_conflict" not in build_web_evidence_bundle(snapshot)["reason_codes"]


@pytest.mark.parametrize("slot", [True, 1.5, "1", 0, -1])
def test_unbounded_evidence_budget_still_requires_positive_integer_slots(slot):
    snapshot = _valid_snapshot()
    snapshot["menu_limit"] = None
    snapshot["ledger"][1]["execution_slot"] = slot
    assert "budget_conflict" in build_web_evidence_bundle(snapshot)["reason_codes"]


@pytest.mark.parametrize("overflow", (False, True))
def test_failed_ledger_terminal_is_not_reported_as_numeric_budget_conflict(overflow):
    snapshot = _valid_snapshot()
    snapshot["ledger"][1]["status"] = "navigation_failed"
    if overflow:
        snapshot["ledger"][1]["execution_slot"] = 11
    result = build_web_evidence_bundle(snapshot)
    assert result["status"] == "needs_review"
    assert "ledger_closure_invalid" in result["reason_codes"]
    assert ("budget_conflict" in result["reason_codes"]) is overflow


def _valid_snapshot() -> dict[str, object]:
    captures: list[dict[str, object]] = []
    actions: list[dict[str, object]] = []
    artifacts: list[dict[str, object]] = []

    def capture(
        capture_id: str,
        *,
        role: str,
        parent_id: str | None,
        action_id: str | None,
        sequence: int | None,
        page_type: str,
    ) -> None:
        row = {
            "capture_id": capture_id,
            "task_id": "task-web",
            "agent_run_id": "run-web",
            "surface_kind": "web",
            "surface_session_id": "web:run-web",
            "parent_capture_id": parent_id,
            "action_execution_id": action_id,
            "capture_role": role,
            "session_command_sequence": sequence,
            "capture_status": "captured",
            "url": "https://reader:secret@example.test/watch?token=hidden#fragment",
            "logical_page_id": "web-logical:" + "d" * 64,
            "page_type": page_type,
            "page_task_id": "task-web",
            "page_agent_run_id": "run-web",
            "page_surface_session_id": "web:run-web",
            "page_parent_capture_id": parent_id,
            "page_action_execution_id": action_id,
            "page_capture_role": role,
            "page_session_command_sequence": sequence,
            "page_capture_status": "captured",
            "web_evidence_admission": {
                "kind": "business_page",
                "can_project_business_node": True,
                "can_enter_formal_bundle": True,
            },
            "pipeline_metrics": {"web_readiness": _stable_capture_metrics()},
            "web_product_capability_assessment": {
                "capabilities": ["content", "navigation"],
                "entry_status": "known",
                "visible_layer": "business",
            },
        }
        captures.append(row)
        required_types = [
            ("screenshot", "image/png"),
            ("dom", "text/html; charset=utf-8"),
        ]
        if role == "after_action":
            required_types.append(("action_trace", "application/json"))
        for artifact_type, content_type in required_types:
            artifacts.append(
                {
                    "artifact_id": f"artifact:{capture_id}:{artifact_type}",
                    "capture_id": capture_id,
                    "artifact_type": artifact_type,
                    "content_type": content_type,
                    "checksum": "sha256:" + "a" * 64,
                    "source": "baseline",
                    "lifecycle_status": "active",
                }
            )

    def action(
        action_id: str,
        *,
        intent_id: str,
        requested_id: str,
        source_id: str,
        result_id: str,
        sequence: int,
    ) -> None:
        actions.append(
            {
                "action_execution_id": action_id,
                "task_id": "task-web",
                "session_id": "web:run-web",
                "intent_id": intent_id,
                "command_sequence": sequence,
                "requested_source_capture_id": requested_id,
                "source_capture_id": source_id,
                "result_capture_id": result_id,
                "status": "completed",
                "error_code": None,
                "started_at": "2026-09-01T00:00:00Z",
                "action_finished_at": "2026-09-01T00:00:01Z",
                "evidence_finished_at": "2026-09-01T00:00:02Z",
                "transition_proof": "trusted",
                "counts_as_business_arrival": True,
            }
        )

    capture(
        "bootstrap",
        role="observation",
        parent_id=None,
        action_id=None,
        sequence=None,
        page_type="content_list",
    )
    previous = "bootstrap"
    for action_id, intent_id, page_type, sequence in (
        ("home-nav", "home-nav-intent", "home", 1),
        ("home-ready", "home-ready-intent", "home", 2),
        ("menu-nav", "menu-intent", "content_detail", 3),
        ("menu-ready", "menu-ready-intent", "content_detail", 4),
    ):
        before_id = f"{action_id}:before"
        after_id = f"{action_id}:after"
        capture(
            before_id,
            role="before_action",
            parent_id=previous,
            action_id=action_id,
            sequence=sequence,
            page_type=page_type,
        )
        capture(
            after_id,
            role="after_action",
            parent_id=before_id,
            action_id=action_id,
            sequence=sequence,
            page_type=page_type,
        )
        action(
            action_id,
            intent_id=intent_id,
            requested_id=previous,
            source_id=before_id,
            result_id=after_id,
            sequence=sequence,
        )
        previous = after_id

    for capture_row in captures:
        # 模拟已由同 Run 分类仓储认证的读模型输出，不信任网页 metadata。
        capture_row["logical_page_identity_status"] = "verified"
        capture_id = str(capture_row["capture_id"])
        if capture_id == "bootstrap" or capture_id.startswith("home-"):
            capture_row["logical_page_id"] = "web-logical:" + "a" * 64
            capture_row["url"] = (
                "https://reader:secret@example.test/?token=hidden#fragment"
            )

    return {
        "normalization_version": "web-page-type-assessment.v3",
        "task_id": "task-web",
        "agent_run_id": "run-web",
        "session_id": "web:run-web",
        "site_id": "example",
        "menu_limit": 5,
        "budget_state": "sealed",
        "finalization_epoch": 0,
        "ledger": [
            {
                "menu_entry_id": "home",
                "decision": "home_baseline",
                "execution_slot": None,
                "status": "home_baseline",
                "menu_action_count": 0,
                "menu_intent_id": None,
                "menu_source_capture_id": None,
                "menu_action_execution_id": None,
                "menu_probe_capture_id": "home-nav:after",
                "menu_ready_capture_id": "home-ready:after",
                "menu_page_type": "home",
                "detail_action_count": 0,
                "detail_intent_id": None,
                "detail_source_capture_id": None,
                "detail_action_execution_id": None,
                "detail_probe_capture_id": None,
                "detail_ready_capture_id": None,
                "detail_page_type": None,
            },
            {
                "menu_entry_id": "detail-menu",
                "decision": "allowlisted_observe_only",
                "execution_slot": 1,
                "status": "detail_captured",
                "menu_action_count": 1,
                "menu_intent_id": "menu-intent",
                "menu_source_capture_id": "home-ready:after",
                "menu_action_execution_id": "menu-nav",
                "menu_probe_capture_id": "menu-nav:after",
                "menu_ready_capture_id": "menu-ready:after",
                "menu_page_type": "content_detail",
                "detail_action_count": 0,
                "detail_intent_id": None,
                "detail_source_capture_id": None,
                "detail_action_execution_id": None,
                "detail_probe_capture_id": None,
                "detail_ready_capture_id": None,
                "detail_page_type": None,
            },
        ],
        "captures": captures,
        "actions": actions,
        "artifacts": artifacts,
    }


def test_saved_admission_cannot_override_untrusted_readiness():
    snapshot = _valid_snapshot()
    for capture in snapshot["captures"]:
        capture["pipeline_metrics"]["web_readiness"]["settle_complete"] = False
    result = build_web_evidence_bundle(snapshot)
    assert result["status"] == "needs_review"
    assert "evidence_incomplete" in result["reason_codes"]


@pytest.mark.parametrize(
    ("normalization_version", "page_type", "expected_status"),
    [
        ("web-page-type-normalization.v1", "content_list", "evidence_ready"),
        ("web-page-type-assessment.v2", "video_detail", "evidence_ready"),
        ("web-page-type-assessment.v3", "home", "evidence_ready"),
        ("web-page-type-assessment.v3", "content_list", "needs_review"),
    ],
)
def test_evidence_bundle_home_baseline_uses_frozen_page_type_version(
    normalization_version: str,
    page_type: str,
    expected_status: str,
) -> None:
    snapshot = _valid_snapshot()
    snapshot["normalization_version"] = normalization_version
    snapshot["ledger"][0]["menu_page_type"] = page_type
    for capture in snapshot["captures"]:
        if str(capture["capture_id"]).startswith("home-"):
            capture["page_type"] = page_type

    result = build_web_evidence_bundle(snapshot)

    assert result["status"] == expected_status, result["reason_codes"]


@pytest.mark.parametrize(
    "normalization_version",
    [None, "", "web-page-type-assessment.v999"],
)
def test_evidence_bundle_rejects_untrusted_home_normalization_version(
    normalization_version: str | None,
) -> None:
    snapshot = _valid_snapshot()
    if normalization_version is None:
        snapshot.pop("normalization_version")
    else:
        snapshot["normalization_version"] = normalization_version

    result = build_web_evidence_bundle(snapshot)

    assert result["status"] == "needs_review"
    assert "ledger_closure_invalid" in result["reason_codes"]
    assert "budget_conflict" not in result["reason_codes"]


def test_evidence_bundle_rejects_conflicting_top_level_and_coverage_versions() -> None:
    snapshot = _valid_snapshot()
    snapshot["normalization_version"] = "web-page-type-assessment.v2"
    snapshot["ledger"][0]["menu_page_type"] = "content_list"
    for capture in snapshot["captures"]:
        if str(capture["capture_id"]).startswith("home-"):
            capture["page_type"] = "content_list"
    snapshot["coverage_snapshot"] = {
        "normalization_version": "web-page-type-assessment.v3",
    }

    result = build_web_evidence_bundle(snapshot)

    assert result["status"] == "needs_review"
    assert "ledger_closure_invalid" in result["reason_codes"]
    assert "budget_conflict" not in result["reason_codes"]


def _append_action_pair(
    snapshot: dict[str, object],
    *,
    action_id: str,
    intent_id: str,
    requested_id: str,
    page_type: str,
    sequence: int,
    result_url: str | None = None,
) -> str:
    before_id = f"{action_id}:before"
    after_id = f"{action_id}:after"
    template = next(item for item in snapshot["captures"] if item["capture_id"] == requested_id)
    requested_url = next(
        item["url"]
        for item in snapshot["captures"]
        if item["capture_id"] == requested_id
    )
    for capture_id, role, parent_id in (
        (before_id, "before_action", requested_id),
        (after_id, "after_action", before_id),
    ):
        row = deepcopy(template)
        row.update(
            {
                "capture_id": capture_id,
                "parent_capture_id": parent_id,
                "action_execution_id": action_id,
                "capture_role": role,
                "session_command_sequence": sequence,
                "page_type": page_type,
                "page_parent_capture_id": parent_id,
                "page_action_execution_id": action_id,
                "page_capture_role": role,
                "page_session_command_sequence": sequence,
                "url": result_url if role == "after_action" and result_url else requested_url,
                "logical_page_id": "web-logical:" + "e" * 64 if role == "after_action" and result_url else template["logical_page_id"],
            }
        )
        snapshot["captures"].append(row)
        types = [("screenshot", "image/png"), ("dom", "text/html")]
        if role == "after_action":
            types.append(("action_trace", "application/json"))
        for artifact_type, content_type in types:
            snapshot["artifacts"].append(
                {
                    "artifact_id": f"artifact:{capture_id}:{artifact_type}",
                    "capture_id": capture_id,
                    "artifact_type": artifact_type,
                    "content_type": content_type,
                    "checksum": "sha256:" + "b" * 64,
                    "source": "baseline",
                    "lifecycle_status": "active",
                }
            )
    action = deepcopy(snapshot["actions"][0])
    action.update(
        {
            "action_execution_id": action_id,
            "intent_id": intent_id,
            "command_sequence": sequence,
            "requested_source_capture_id": requested_id,
            "source_capture_id": before_id,
            "result_capture_id": after_id,
        }
    )
    snapshot["actions"].append(action)
    return after_id


def test_build_web_evidence_bundle_accepts_complete_direct_detail_and_redacts_url() -> None:
    result = build_web_evidence_bundle(_valid_snapshot())

    assert result["status"] == "evidence_ready"
    assert result["reason_codes"] == []
    assert result["summary"] == {
        "home_count": 1,
        "menu_count": 1,
        "detail_count": 1,
        "duplicate_formal_url_count": 0,
    }
    assert result["entries"][1]["menu_entry_id"] == "detail-menu"
    assert result["entries"][1]["menu_action_execution_id"] == "menu-nav"
    assert result["entries"][1]["detail_ready_capture_id"] == "menu-ready:after"
    assert result["entries"][1]["detail_page_type"] == "content_detail"
    assert result["captures"]
    assert len(result["formal_pages"]) == 2
    detail_page = next(
        page
        for page in result["formal_pages"]
        if page["logical_page_url"] == "https://example.test/watch"
    )
    assert detail_page["observation_capture_ids"] == ["menu-ready:after"]
    assert detail_page["product_capabilities"] == [
        "content",
        "navigation",
    ]
    assert {item["url"] for item in result["captures"]} == {
        "https://example.test/",
        "https://example.test/watch",
    }
    text = repr(result).casefold()
    for forbidden in (
        "reader",
        "secret",
        "token",
        "storage_path",
        "intent_payload",
        "result_payload",
        "lease_token",
        "metadata",
    ):
        assert forbidden not in text


def test_evidence_bundle_projects_runtime_lineage_without_resetting_depth() -> None:
    snapshot = _valid_snapshot()
    snapshot["ledger"][1].update(
        {
            "source_kind": "runtime_discovered",
            "parent_menu_entry_id": "discovery:parent",
            "menu_depth": 25,
            "exploration_path": [f"层级{index}" for index in range(1, 26)],
            "logical_target_identity": "logical-child",
            "parent_logical_target_identity": "logical-parent",
        }
    )

    result = build_web_evidence_bundle(snapshot)

    assert result["entries"][1]["parent_menu_entry_id"] == "discovery:parent"
    assert result["entries"][1]["menu_depth"] == 25
    assert result["entries"][1]["exploration_path"][-1] == "层级25"
    assert result["entries"][1]["logical_target_identity"] == "logical-child"
    assert result["entries"][1]["parent_logical_target_identity"] == "logical-parent"
    assert "lineage_invalid" in result["reason_codes"]


def test_evidence_bundle_rejects_child_path_equal_to_parent_path() -> None:
    snapshot = _valid_snapshot()
    parent = snapshot["ledger"][1]
    parent.update(
        menu_entry_id="discovery:" + "a" * 64,
        source_kind="runtime_discovered",
        parent_menu_entry_id=None,
        parent_logical_target_identity=None,
        menu_depth=1,
        exploration_path=["父菜单"],
        logical_target_identity="a" * 64,
    )
    child = {
        **parent,
        "menu_entry_id": "discovery:" + "b" * 64,
        "parent_menu_entry_id": parent["menu_entry_id"],
        "parent_logical_target_identity": parent["logical_target_identity"],
        "menu_depth": 2,
        "logical_target_identity": "b" * 64,
        "execution_slot": 2,
    }
    snapshot["ledger"].append(child)

    result = build_web_evidence_bundle(snapshot)

    assert result["status"] == "needs_review"
    assert "lineage_invalid" in result["reason_codes"]


def test_evidence_bundle_rejects_runtime_path_ending_with_wrong_menu_label() -> None:
    snapshot = _valid_snapshot()
    row = snapshot["ledger"][1]
    row.update(
        menu_entry_id="discovery:" + "a" * 64,
        source_kind="runtime_discovered",
        parent_menu_entry_id=None,
        parent_logical_target_identity=None,
        menu_depth=1,
        exploration_path=["错误标签"],
        logical_target_identity="a" * 64,
    )

    result = build_web_evidence_bundle(snapshot)

    assert result["status"] == "needs_review"
    assert "lineage_invalid" in result["reason_codes"]


def test_evidence_bundle_accepts_verified_submenu_expansion_without_page_type() -> None:
    snapshot = _valid_snapshot()
    row = snapshot["ledger"][1]
    row.update(
        {
            "menu_entry_id": "discovery:" + "c" * 64,
            "menu_path": ["父菜单"],
            "source_kind": "runtime_discovered",
            "parent_menu_entry_id": None,
            "parent_logical_target_identity": None,
            "menu_depth": 1,
            "exploration_path": ["父菜单"],
            "logical_target_identity": "c" * 64,
            "status": "menu_page_captured",
            "menu_probe_capture_id": "menu-nav:after",
            "menu_ready_capture_id": "menu-nav:after",
            "menu_page_type": None,
            "detail_action_count": 0,
            "diagnostic_type": "WEB_SUBMENU_EXPANDED",
            "menu_interaction_outcome": {
                "command_accepted": True,
                "effect_observed": True,
                "postcondition_satisfied": True,
                "transition_arrived": True,
                "after_capture_id": "menu-nav:after",
                "failure_reason": None,
                "evidence_mode": "natural",
            },
        }
    )
    action = next(
        item for item in snapshot["actions"] if item["action_execution_id"] == "menu-nav"
    )
    action["transition_proof"] = "insufficient"
    action["counts_as_business_arrival"] = False

    result = build_web_evidence_bundle(snapshot)

    assert result["status"] == "evidence_ready", result["reason_codes"]
    assert result["entries"][1]["diagnostic_type"] == "WEB_SUBMENU_EXPANDED"
    assert len(result["formal_pages"]) == 1


@pytest.mark.parametrize(
    "mutation",
    [
        "missing_type",
        "missing_outcome",
        "missing_failure_reason",
        "failed_transition",
        "page_type_present",
        "probe_mismatch",
        "after_capture_mismatch",
        "detail_intent_residual",
        "detail_source_residual",
        "detail_candidate_residual",
        "detail_destination_residual",
        "detail_action_residual",
        "detail_probe_residual",
        "detail_ready_residual",
        "detail_page_type_residual",
        "detail_selection_residual",
    ],
)
def test_evidence_bundle_rejects_incomplete_submenu_expansion(mutation: str) -> None:
    snapshot = _valid_snapshot()
    row = snapshot["ledger"][1]
    outcome = {
        "command_accepted": True,
        "effect_observed": True,
        "postcondition_satisfied": True,
        "transition_arrived": True,
        "after_capture_id": "menu-nav:after",
        "failure_reason": None,
        "evidence_mode": "natural",
    }
    row.update(
        menu_entry_id="discovery:" + "c" * 64,
        source_kind="runtime_discovered",
        parent_menu_entry_id=None,
        parent_logical_target_identity=None,
        menu_depth=1,
        exploration_path=["父菜单"],
        logical_target_identity="c" * 64,
        status="menu_page_captured",
        menu_probe_capture_id="menu-nav:after",
        menu_ready_capture_id="menu-nav:after",
        menu_page_type=None,
        detail_action_count=0,
        diagnostic_type="WEB_SUBMENU_EXPANDED",
        menu_interaction_outcome=outcome,
    )
    if mutation == "missing_type":
        row.pop("diagnostic_type")
    elif mutation == "missing_outcome":
        row.pop("menu_interaction_outcome")
    elif mutation == "missing_failure_reason":
        outcome.pop("failure_reason")
    elif mutation == "failed_transition":
        outcome["transition_arrived"] = False
    elif mutation == "page_type_present":
        row["menu_page_type"] = "content_list"
    elif mutation == "probe_mismatch":
        row["menu_probe_capture_id"] = "home-nav:after"
    elif mutation == "after_capture_mismatch":
        outcome["after_capture_id"] = "home-nav:after"
    elif mutation == "detail_intent_residual":
        row["detail_intent_id"] = "unexpected-detail-intent"
    elif mutation == "detail_source_residual":
        row["detail_source_capture_id"] = "menu-nav:after"
    elif mutation == "detail_candidate_residual":
        row["detail_candidate_id"] = "unexpected-detail-candidate"
    elif mutation == "detail_destination_residual":
        row["detail_destination_sha256"] = "a" * 64
    elif mutation == "detail_action_residual":
        row["detail_action_execution_id"] = "unexpected-detail-action"
    elif mutation == "detail_probe_residual":
        row["detail_probe_capture_id"] = "menu-nav:after"
    elif mutation == "detail_ready_residual":
        row["detail_ready_capture_id"] = "menu-ready:after"
    elif mutation == "detail_page_type_residual":
        row["detail_page_type"] = "content_detail"
    else:
        row["detail_selection"] = {"candidate_id": "unexpected-detail-candidate"}

    result = build_web_evidence_bundle(snapshot)

    assert result["status"] == "needs_review"
    assert "ledger_closure_invalid" in result["reason_codes"]
    assert "budget_conflict" not in result["reason_codes"]


@pytest.mark.parametrize(
    ("field_name", "residual"),
    [
        ("detail_intent_id", "detail-intent"),
        ("detail_source_capture_id", "detail-source"),
        ("detail_candidate_id", "detail-candidate"),
        ("detail_destination_sha256", "a" * 64),
        ("detail_action_execution_id", "detail-action"),
        ("detail_probe_capture_id", "detail-probe"),
        ("detail_ready_capture_id", "detail-ready"),
        ("detail_page_type", "content_detail"),
        ("detail_selection", {"candidate_id": "detail-candidate"}),
    ],
)
def test_submenu_shared_shape_rejects_every_detail_stage_residual(
    field_name: str,
    residual: object,
) -> None:
    from services.api.app.application.web_submenu_expansion import (
        persisted_submenu_expansion_has_valid_shape,
    )

    row = {
        "status": "menu_page_captured",
        "execution_slot": 1,
        "menu_action_count": 1,
        "menu_intent_id": "menu-intent",
        "menu_source_capture_id": "menu-source",
        "menu_action_execution_id": "menu-action",
        "menu_probe_capture_id": "submenu-after",
        "menu_ready_capture_id": "submenu-after",
        "menu_page_type": None,
        "detail_action_count": 0,
        "detail_page_type": None,
        "diagnostic_type": "WEB_SUBMENU_EXPANDED",
        "menu_interaction_outcome": {
            "command_accepted": True,
            "effect_observed": True,
            "postcondition_satisfied": True,
            "transition_arrived": True,
            "after_capture_id": "submenu-after",
            "failure_reason": None,
            "evidence_mode": "natural",
        },
    }
    row[field_name] = residual

    assert not persisted_submenu_expansion_has_valid_shape(row)


def test_evidence_bundle_accepts_ordinary_menu_list_without_submenu_diagnostic() -> None:
    snapshot = _valid_snapshot()
    row = snapshot["ledger"][1]
    row.update(
        status="menu_page_captured",
        menu_page_type="content_list",
        detail_action_count=0,
        diagnostic_type=None,
        diagnostic={},
    )
    for capture in snapshot["captures"]:
        if str(capture["capture_id"]).startswith("menu-"):
            capture["page_type"] = "content_list"

    result = build_web_evidence_bundle(snapshot)

    assert result["status"] == "evidence_ready", result["reason_codes"]
    assert result["entries"][1]["menu_page_type"] == "content_list"


@pytest.mark.parametrize(
    ("diagnostic_type", "nested_type"),
    [
        ("OTHER_DIAGNOSTIC", "WEB_SUBMENU_EXPANDED"),
        ("WEB_SUBMENU_EXPANDED", "OTHER_DIAGNOSTIC"),
        ("OTHER_DIAGNOSTIC", "CONFLICTING_DIAGNOSTIC"),
    ],
)
def test_evidence_bundle_rejects_conflicting_menu_page_diagnostic_types(
    diagnostic_type: str,
    nested_type: str,
) -> None:
    snapshot = _valid_snapshot()
    row = snapshot["ledger"][1]
    row.update(
        status="menu_page_captured",
        menu_page_type="content_list",
        detail_action_count=0,
        diagnostic_type=diagnostic_type,
        diagnostic={"type": nested_type},
    )
    for capture in snapshot["captures"]:
        if str(capture["capture_id"]).startswith("menu-"):
            capture["page_type"] = "content_list"

    result = build_web_evidence_bundle(snapshot)

    assert result["status"] == "needs_review"
    assert "ledger_closure_invalid" in result["reason_codes"]
    assert "budget_conflict" not in result["reason_codes"]


@pytest.mark.parametrize("refined", [False, True])
@pytest.mark.parametrize("tamper", [None, "operation", "epoch", "before_parent", "task"])
def test_formal_page_prefers_primary_content_supplement_and_exposes_safe_selection(refined, tamper) -> None:
    """恢复按 Capture ID 选主图时，本测试应捕获广告前态覆盖主体补图。"""

    snapshot = _valid_snapshot()
    selected_capture_id = _append_action_pair(
        snapshot,
        action_id="primary-scroll",
        intent_id="primary-scroll-intent",
        requested_id="menu-ready:after",
        page_type="content_detail",
        sequence=5,
    )
    selected = next(
        item for item in snapshot["captures"] if item["capture_id"] == selected_capture_id
    )
    selected["capture_purpose"] = "web_primary_content_evidence"
    if refined:
        snapshot["ledger"][1]["menu_ready_capture_id"] = selected_capture_id
        next(c for c in snapshot["captures"] if c["capture_id"] == "menu-ready:after")["page_type"] = None
    else:
        selected["page_type"] = None
    selected["primary_content_capture"] = {
        "parent_capture_id": "menu-ready:after",
        "direct_parent_capture_id": "primary-scroll:before",
        "target_candidate_id": "web-primary:candidate",
        "scroll_count": 1,
        "before_ad_coverage": 0.72,
        "after_ad_coverage": 0.08,
        "unsafe_extra": "secret",
    }
    snapshot["finalization_epoch"] = 0
    for row in snapshot["captures"]:
        row["finalization_epoch"] = row["page_finalization_epoch"] = 0
    for row in snapshot["actions"]:
        row["finalization_epoch"] = 0
    action = next(a for a in snapshot["actions"] if a["action_execution_id"] == "primary-scroll")
    action.update(action_type="scroll", web_capture_stage="web_primary_content_evidence", target_mode="web_primary_content_candidate")
    if tamper == "operation":
        action["action_type"] = "goto"
    elif tamper == "epoch":
        action["finalization_epoch"] = 1
    elif tamper == "before_parent":
        next(c for c in snapshot["captures"] if c["capture_id"] == "primary-scroll:before")["parent_capture_id"] = "home-ready:after"
    elif tamper == "task":
        selected["task_id"] = "other-task"

    result = build_web_evidence_bundle(snapshot)
    if tamper:
        assert result["status"] == "needs_review"
        return

    detail_page = next(
        page
        for page in result["formal_pages"]
        if page["logical_page_url"] == "https://example.test/watch"
    )
    assert detail_page["primary_capture_id"] == selected_capture_id
    assert detail_page["page_type"] == "content_detail"
    assert "menu-ready:after" in detail_page["observation_capture_ids"]
    assert result["status"] == "evidence_ready", result["reason_codes"]
    public_capture = next(
        item for item in result["captures"] if item["capture_id"] == selected_capture_id
    )
    assert public_capture["capture_purpose"] == "web_primary_content_evidence"
    assert public_capture["primary_content_capture"] == {
        "parent_capture_id": "menu-ready:after",
        "direct_parent_capture_id": "primary-scroll:before",
        "scroll_count": 1,
        "before_ad_coverage": 0.72,
        "after_ad_coverage": 0.08,
    }


def test_duplicate_formal_url_is_counted_before_logical_page_deduplication() -> None:
    """同一 URL 的重复正式账本条目必须留痕并阻断证据就绪。"""

    snapshot = _valid_snapshot()
    duplicate_entry = deepcopy(snapshot["ledger"][1])
    duplicate_entry["menu_entry_id"] = "detail-menu-alias"
    duplicate_entry["execution_slot"] = 2
    snapshot["ledger"].append(duplicate_entry)

    result = build_web_evidence_bundle(snapshot)

    assert result["status"] == "needs_review"
    assert "duplicate_formal_url" in result["reason_codes"]
    assert result["summary"]["duplicate_formal_url_count"] == 1
    assert len(result["formal_pages"]) == 2


def test_formal_menu_action_requires_trusted_business_arrival() -> None:
    snapshot = _valid_snapshot()
    menu_action = next(
        row for row in snapshot["actions"] if row["action_execution_id"] == "menu-nav"
    )
    menu_action["transition_proof"] = "insufficient"
    menu_action["counts_as_business_arrival"] = False

    result = build_web_evidence_bundle(snapshot)

    assert result["status"] == "needs_review"
    assert "untrusted_arrival" in result["reason_codes"]


def test_build_web_evidence_bundle_keeps_legacy_ten_menu_budget_reportable() -> None:
    snapshot = _valid_snapshot()
    snapshot["menu_limit"] = 10

    result = build_web_evidence_bundle(snapshot)

    assert result["status"] == "evidence_ready"
    assert result["reason_codes"] == []


def test_build_web_evidence_bundle_accepts_one_detail_action_for_list_menu() -> None:
    snapshot = _valid_snapshot()
    for capture in snapshot["captures"]:
        if str(capture["capture_id"]).startswith(("menu-nav:", "menu-ready:")):
            capture["page_type"] = "video_list"
    detail_probe = _append_action_pair(
        snapshot,
        action_id="detail-nav",
        intent_id="detail-intent",
        requested_id="menu-ready:after",
        page_type="video_detail",
        sequence=5,
        result_url="https://example.test/video/1",
    )
    detail_ready = _append_action_pair(
        snapshot,
        action_id="detail-ready",
        intent_id="detail-ready-intent",
        requested_id=detail_probe,
        page_type="video_detail",
        sequence=6,
    )
    snapshot["ledger"][1].update(
        {
            "menu_page_type": "video_list",
            "detail_action_count": 1,
            "detail_intent_id": "detail-intent",
            "detail_source_capture_id": "menu-ready:after",
            "detail_action_execution_id": "detail-nav",
            "detail_probe_capture_id": detail_probe,
            "detail_ready_capture_id": detail_ready,
            "detail_page_type": "video_detail",
        }
    )

    result = build_web_evidence_bundle(snapshot)

    assert result["status"] == "evidence_ready"
    assert result["summary"]["menu_count"] == 1
    assert result["summary"]["detail_count"] == 1


def test_build_web_evidence_bundle_marks_missing_dom_as_evidence_incomplete() -> None:
    snapshot = _valid_snapshot()
    snapshot["artifacts"] = [
        item
        for item in snapshot["artifacts"]
        if not (item["capture_id"] == "menu-ready:after" and item["artifact_type"] == "dom")
    ]

    result = build_web_evidence_bundle(snapshot)

    assert result["status"] == "needs_review"
    assert "evidence_incomplete" in result["reason_codes"]


def test_rule_supplement_or_deleted_dom_cannot_replace_baseline_dom() -> None:
    snapshot = _valid_snapshot()
    dom = next(
        item
        for item in snapshot["artifacts"]
        if item["capture_id"] == "menu-ready:after" and item["artifact_type"] == "dom"
    )
    dom["source"] = "rule_supplement"
    dom["lifecycle_status"] = "deleted"

    result = build_web_evidence_bundle(snapshot)

    assert result["status"] == "needs_review"
    assert "evidence_incomplete" in result["reason_codes"]


def test_build_web_evidence_bundle_rejects_ledger_source_mapped_to_action_before() -> None:
    snapshot = _valid_snapshot()
    snapshot["ledger"][1]["menu_source_capture_id"] = "menu-nav:before"

    result = build_web_evidence_bundle(snapshot)

    assert result["status"] == "needs_review"
    assert "evidence_incomplete" in result["reason_codes"]


def test_build_web_evidence_bundle_rejects_page_common_identity_drift() -> None:
    snapshot = deepcopy(_valid_snapshot())
    snapshot["captures"][0]["page_agent_run_id"] = "foreign-run"

    result = build_web_evidence_bundle(snapshot)

    assert result["status"] == "needs_review"
    assert "evidence_incomplete" in result["reason_codes"]


def test_ready_capture_without_action_identity_is_evidence_incomplete() -> None:
    snapshot = _valid_snapshot()
    ready = next(
        item for item in snapshot["captures"] if item["capture_id"] == "menu-ready:after"
    )
    ready["action_execution_id"] = None
    ready["page_action_execution_id"] = None

    result = build_web_evidence_bundle(snapshot)

    assert result["status"] == "needs_review"
    assert "evidence_incomplete" in result["reason_codes"]


def test_ready_capture_cannot_alias_navigation_probe() -> None:
    snapshot = _valid_snapshot()
    snapshot["ledger"][1]["menu_ready_capture_id"] = "menu-nav:after"

    result = build_web_evidence_bundle(snapshot)

    assert result["status"] == "needs_review"
    assert "evidence_incomplete" in result["reason_codes"]


def test_build_web_evidence_bundle_rejects_budget_overflow() -> None:
    snapshot = _valid_snapshot()
    template = deepcopy(snapshot["ledger"][1])
    snapshot["ledger"] = [snapshot["ledger"][0]] + [
        {
            **deepcopy(template),
            "menu_entry_id": f"menu-{index}",
            "execution_slot": index + 1,
        }
        for index in range(11)
    ]

    result = build_web_evidence_bundle(snapshot)

    assert result["status"] == "needs_review"
    assert "budget_conflict" in result["reason_codes"]


def test_bundle_projects_only_whitelisted_web_coverage_and_gap_summary() -> None:
    snapshot = _valid_snapshot()
    snapshot["coverage_snapshot"] = {
        "snapshot_id": "snapshot-web-1",
        "snapshot_hash": "a" * 64,
        "finalization_epoch": 4,
        "normalization_version": "web-page-type-normalization.v1",
        "coverage_status": "completed_with_gaps",
        "required_page_types": ["video_list", "content_list", "content_detail"],
        "observed_page_types": ["video_list", "content_list"],
        "missing_page_types": ["content_detail"],
        "unrecognized_page_types": ["raw_unknown"],
        "raw_dom": "<html>secret</html>",
        "source_url": "https://reader:password@example.test/?token=secret",
    }
    snapshot["coverage_gap_summary"] = {
        "candidate_rule_count": 2,
        "terminal_rule_count": 2,
        "evidence_insufficient_count": 2,
        "raw_result": "secret",
    }

    result = build_web_evidence_bundle(snapshot)

    assert result["coverage"] == {
        "snapshot_id": "snapshot-web-1",
        "snapshot_hash": "a" * 64,
        "finalization_epoch": 4,
        "normalization_version": "web-page-type-normalization.v1",
        "coverage_status": "completed_with_gaps",
        "required_page_types": ["video_list", "content_list", "content_detail"],
        "observed_page_types": ["video_list", "content_list"],
        "missing_page_types": ["content_detail"],
        "unrecognized_page_types": ["raw_unknown"],
        "gap_rule_count": 2,
        "gap_terminal_rule_count": 2,
        "evidence_insufficient_count": 2,
    }
    assert "raw_dom" not in repr(result)
    assert "raw_result" not in repr(result)
    assert "password" not in repr(result)
