"""发现只读审计与已执行发现必须按不同规则参与 Web 收口。"""

from collections import Counter
from types import SimpleNamespace

import pytest

from services.api.app.application.web_evidence_bundle import build_web_evidence_bundle
from services.api.app.application.web_runtime_composition import _web_exploration_is_partial
from services.api.tests.test_web_evidence_bundle import _valid_snapshot
from services.api.tests.test_web_runtime_composition import _successful_web_summary


def _audit(reason):
    identity = "e" * 64
    return {
        "entry_id": "discovery:" + identity,
        "menu_entry_id": "discovery:" + identity,
        "menu_path": ["审计候选"],
        "source_kind": "runtime_discovered",
        "decision": "runtime_record_only",
        "source_capture_id": "source",
        "discovery_source_capture_id": "source",
        "status": "runtime_discovered_unreviewed",
        "error_code": reason,
        "execution_slot": None,
        "menu_action_count": 0,
        "detail_action_count": 0,
        "parent_menu_entry_id": None,
        "parent_logical_target_identity": None,
        "menu_depth": 1,
        "exploration_path": ["审计候选"],
        "logical_target_identity": identity,
    }


@pytest.mark.parametrize(
    "reason",
    [
        "WEB_DISCOVERY_DISABLED",
        "WEB_DISCOVERY_PAYMENT_BLOCKED",
        "WEB_DISCOVERY_BUDGET_EXHAUSTED",
        "WEB_DISCOVERY_CLOSEOUT_RESERVED",
    ],
)
def test_discovery_audit_only_is_not_execution_failure_in_summary_or_bundle(reason):
    row = _audit(reason)
    summary = _successful_web_summary()
    summary["runtime_discovered_ledger"] = [row]
    summary["runtime_discovered_count"] = 1
    summary["status_counts"]["runtime_discovered_unreviewed"] = 1
    assert not _web_exploration_is_partial(SimpleNamespace(failed_urls=()), summary=summary)
    snapshot = _valid_snapshot()
    snapshot["ledger"].append(row)
    bundle = build_web_evidence_bundle(snapshot)
    assert bundle["status"] == "evidence_ready"
    entry = bundle["entries"][-1]
    assert entry["source_kind"] == "runtime_discovered"
    assert entry["discovery_reason"] == reason


@pytest.mark.parametrize("failed", [False, True])
def test_discovery_executed_entries_participate_in_same_success_accounting(failed):
    summary = _successful_web_summary(executed=2, menu_limit=10)
    discovered = summary["menu_ledger"].pop(2)
    identity = "f" * 64
    discovered.update(
        entry_id="discovery:" + identity,
        menu_entry_id="discovery:" + identity,
        menu_path=["已执行候选"],
        source_kind="runtime_discovered",
        decision="runtime_observe_only",
        parent_menu_entry_id=None,
        parent_logical_target_identity=None,
        menu_depth=1,
        exploration_path=["已执行候选"],
        logical_target_identity=identity,
    )
    if failed:
        discovered["status"] = "navigation_failed"
    summary["menu_total"] -= 1
    summary["runtime_discovered_ledger"] = [discovered]
    summary["runtime_discovered_count"] = 1
    summary["status_counts"] = dict(
        Counter(row["status"] for row in summary["menu_ledger"] + [discovered])
    )
    assert _web_exploration_is_partial(SimpleNamespace(failed_urls=()), summary=summary) is failed
    snapshot = _valid_snapshot()
    snapshot["ledger"][1].update(
        menu_entry_id="discovery:" + identity,
        menu_path=["已执行候选"],
        source_kind="runtime_discovered",
        decision="runtime_observe_only",
        parent_menu_entry_id=None,
        parent_logical_target_identity=None,
        menu_depth=1,
        exploration_path=["已执行候选"],
        logical_target_identity=identity,
    )
    if failed:
        snapshot["ledger"][1]["status"] = "navigation_failed"
    assert (build_web_evidence_bundle(snapshot)["status"] != "evidence_ready") is failed


@pytest.mark.parametrize(
    "mutation",
    [
        {"menu_action_count": 1},
        {"decision": "runtime_observe_only"},
        {"execution_slot": 1},
        {"error_code": "private-url:https://secret.test/"},
        {"menu_action_execution_id": "hidden-action"},
    ],
)
def test_discovery_audit_shape_cannot_hide_action_or_arbitrary_reason(mutation):
    row = {**_audit("WEB_DISCOVERY_DISABLED"), **mutation}
    snapshot = _valid_snapshot()
    snapshot["ledger"].append(row)
    assert build_web_evidence_bundle(snapshot)["status"] == "needs_review"


@pytest.mark.parametrize(
    "mutation",
    [
        "missing_menu_count",
        "missing_detail_count",
        "claimed",
        "detail_claimed",
        "missing_reason",
        "other_failure",
        "extra_reason",
        "wrong_status",
        "slot",
        "position",
        "identity",
        "disabled",
    ],
)
def test_closeout_catalog_restore_rejects_missing_counts_reason_and_identity_drift(mutation):
    from copy import deepcopy

    from services.api.app.infrastructure.db.web_exploration_budget_ledger_repository import (
        _same_materialized_catalog,
    )

    expected = [
        {
            "menu_entry_id": "catalog-news",
            "catalog_position": 1,
            "decision": "allowlisted_observe_only",
            "menu_path": ["资讯"],
            "canonical_url_sha256": "a" * 64,
            "execution_slot": 1,
            "status": "pending",
            "diagnostic": {},
        }
    ]
    actual = deepcopy(expected)
    actual[0].update(
        status="navigation_failed",
        menu_action_count=0,
        detail_action_count=0,
        diagnostic={"error_code": "WEB_MENU_CLOSEOUT_RESERVED"},
    )
    assert _same_materialized_catalog(expected, actual, allow_closeout=True)
    if mutation.startswith("missing_") and mutation.endswith("count"):
        actual[0].pop(
            "menu_action_count" if mutation == "missing_menu_count" else "detail_action_count"
        )
    elif mutation == "claimed":
        actual[0]["menu_action_count"] = 1
    elif mutation == "detail_claimed":
        actual[0]["detail_action_count"] = 1
    elif mutation == "missing_reason":
        actual[0]["diagnostic"] = None
    elif mutation == "other_failure":
        actual[0]["diagnostic"] = {"error_code": "WEB_MENU_NAVIGATION_FAILED"}
    elif mutation == "extra_reason":
        actual[0]["diagnostic"]["untrusted"] = True
    elif mutation == "wrong_status":
        actual[0]["status"] = "pending"
    elif mutation == "slot":
        actual[0]["execution_slot"] = 2
    elif mutation == "position":
        actual[0]["catalog_position"] = 2
    elif mutation == "identity":
        actual[0]["canonical_url_sha256"] = "b" * 64
    assert not _same_materialized_catalog(expected, actual, allow_closeout=mutation != "disabled")


def test_catalog_restore_keeps_actual_navigation_failure_but_rejects_forged_closeout():
    from services.api.app.infrastructure.db.web_exploration_budget_ledger_repository import (
        _same_materialized_catalog,
    )

    expected = [{"menu_entry_id": "catalog-news", "catalog_position": 1,
                 "decision": "allowlisted_observe_only", "menu_path": ["资讯"],
                 "canonical_url_sha256": "a" * 64, "execution_slot": 1,
                 "status": "pending", "diagnostic": {}}]
    actual = [{**expected[0], "status": "navigation_failed", "menu_action_count": 1,
               "detail_action_count": 0,
               "diagnostic": {"error_code": "WEB_MENU_NAVIGATION_FAILED"}}]
    assert _same_materialized_catalog(expected, actual)
    for allow_closeout in (True, False):
        actual[0]["diagnostic"] = {"error_code": "WEB_MENU_CLOSEOUT_RESERVED"}
        assert not _same_materialized_catalog(expected, actual, allow_closeout=allow_closeout)
