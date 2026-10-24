"""数据库行经真实 DTO 投影后不得丢失终态校验事实。"""

from types import SimpleNamespace

import pytest

from services.api.app.application.web_discovery_audit import web_discovery_is_audit_only
from services.api.app.application.web_home_baseline_validation import (
    persisted_home_baseline_has_valid_shape,
)
from services.api.app.application.web_runtime_composition import (
    ProductionWebV2RuntimeExecutor,
    _web_exploration_is_partial,
    _web_success_ledger_entry_is_valid,
)
from services.api.app.core.config import Settings
from services.api.app.infrastructure.db.web_exploration_budget_ledger_repository import (
    _build_summary,
)
from services.api.tests.test_web_runtime_composition import _web_record

VERSION = "web-page-type-assessment.v4"


def home_row(**changes):
    row = {
        "menu_entry_id": "home",
        "menu_path": ["首页"],
        "canonical_url_sha256": "a" * 64,
        "source_kind": "catalog",
        "status": "home_baseline",
        "decision": "home_baseline",
        "execution_slot": None,
        "menu_action_count": 0,
        "detail_action_count": 0,
        "menu_probe_capture_id": "home-probe",
        "menu_ready_capture_id": "home-ready",
        "menu_page_type": "home",
        "detail_selection": {},
        "diagnostic": {},
    }
    row.update(changes)
    return row


def discovery_row(**changes):
    row = {
        "menu_entry_id": "discovery-audit",
        "menu_path": ["受限入口"],
        "canonical_url_sha256": "b" * 64,
        "source_kind": "runtime_discovered",
        "status": "runtime_discovered_unreviewed",
        "decision": "runtime_record_only",
        "execution_slot": None,
        "menu_action_count": 0,
        "detail_action_count": 0,
        "discovery_source_capture_id": "home-ready",
        "error_code": "WEB_DISCOVERY_UNAUTHORIZED_ACTION",
        "diagnostic": {"error_code": "WEB_DISCOVERY_UNAUTHORIZED_ACTION"},
        "discovery_manifest": {},
    }
    row.update(changes)
    return row


def build_summary(*rows):
    return _build_summary(
        {
            "site_id": "site",
            "normalization_version": VERSION,
            "catalog_version": "catalog",
            "catalog_sha256": "c" * 64,
            "menu_total": sum(row["source_kind"] == "catalog" for row in rows),
            "menu_limit": 5,
        },
        list(rows),
    )


def test_valid_home_stays_valid_after_database_to_dto_projection():
    row = home_row()
    assert persisted_home_baseline_has_valid_shape(row, normalization_version=VERSION)
    projected = build_summary(row).to_dict()["menu_ledger"][0]
    assert projected["status"] == "home_baseline"
    assert persisted_home_baseline_has_valid_shape(projected, normalization_version=VERSION)


def test_zero_action_audit_stays_audit_only_after_database_to_dto_projection():
    row = discovery_row()
    assert web_discovery_is_audit_only(row)
    summary = build_summary(home_row(), row)
    assert summary.runtime_discovered_ledger[0].execution is None
    projected = summary.to_dict()["runtime_discovered_ledger"][0]
    assert projected["status"] == "runtime_discovered_unreviewed"
    assert web_discovery_is_audit_only(projected)


def test_closed_valid_dto_ledger_without_navigation_failures_is_not_partial():
    summary = build_summary(home_row(), discovery_row())
    assert summary.failed_urls == ()
    assert summary.frontier_remaining == 0
    assert _web_exploration_is_partial(summary, summary=summary.to_dict()) is False


@pytest.mark.parametrize(
    "change",
    [
        {"menu_page_type": "unknown"},
        {"menu_ready_capture_id": "home-probe"},
        {"decision": "same_origin_collect"},
        {"menu_action_count": 1},
    ],
)
def test_invalid_persisted_home_must_not_be_repaired_by_projection(change):
    summary = build_summary(home_row(**change))
    assert summary.menu_ledger[0].status.value == "navigation_failed"
    assert _web_exploration_is_partial(summary, summary=summary.to_dict()) is True


@pytest.mark.parametrize(
    "change",
    [
        {"error_code": "WEB_UNKNOWN_FAILURE"},
        {"menu_action_count": 1},
        {"detail_action_count": 1},
        {"execution_slot": 1},
        {"menu_action_execution_id": "unexpected-action"},
        {"detail_ready_capture_id": "unexpected-detail"},
    ],
)
def test_audit_exemption_does_not_hide_unknown_failure_or_executed_action(change):
    row = discovery_row(**change)
    row["diagnostic"] = {"error_code": row["error_code"]}
    assert web_discovery_is_audit_only(row) is False
    summary = build_summary(home_row(), row)
    projected = summary.to_dict()["runtime_discovered_ledger"][0]
    assert web_discovery_is_audit_only(projected) is False
    assert _web_exploration_is_partial(summary, summary=summary.to_dict()) is True


def test_catalog_projection_preserves_persisted_decision_and_execution_slot():
    row = home_row(
        menu_entry_id="blocked-menu", status="cross_origin_blocked",
        decision="cross_origin_block", execution_slot=3,
    )
    projected = build_summary(row).to_dict()["menu_ledger"][0]
    assert projected.get("decision") == "cross_origin_block"
    assert projected.get("execution_slot") == 3


@pytest.mark.parametrize("action_count,valid", [(0, True), (1, False)])
def test_cross_origin_block_remains_a_block_and_never_claims_an_action_success(action_count, valid):
    row = discovery_row(
        status="cross_origin_blocked",
        decision="cross_origin_block",
        menu_action_count=action_count,
        diagnostic={"error_code": "WEB_DISCOVERY_CROSS_ORIGIN_BLOCKED"},
    )
    projected = build_summary(home_row(), row).to_dict()["runtime_discovered_ledger"][0]
    assert projected["status"] == "cross_origin_blocked"
    assert projected["error_code"] == "WEB_DISCOVERY_CROSS_ORIGIN_BLOCKED"
    assert _web_success_ledger_entry_is_valid(projected, normalization_version=VERSION) is valid


@pytest.mark.asyncio
@pytest.mark.parametrize("gap", [None, "WEB_AD_OUTBOUND_DNS_FAILED", "WEB_READINESS_NOT_STABLE"])
async def test_executor_keeps_real_stage_gaps_separate_from_projection_completion(gap):
    summary = build_summary(home_row(), discovery_row())

    class FinishedStages:
        async def initialize(self, command, *, adult_self_attestation):
            pass

        async def next_due(self, run_id):
            return None

        async def final_result(self, run_id):
            return SimpleNamespace(
                preparation=None, exploration=summary, gap_codes=(gap,) if gap else (),
            )

    result = await ProductionWebV2RuntimeExecutor(worker_instance_id="projection-test").run(
        SimpleNamespace(stage_orchestrator=FinishedStages()),
        record=_web_record(), settings=Settings(),
    )
    assert result.termination_reason.value == ("incomplete" if gap else "completed")
    assert result.exploration_status == ("partial_evidence" if gap else "evidence_ready")
    assert result.execution_outcome.value == ("needs_review" if gap else "success")
    assert (gap in result.evidence_gap) if gap else result.evidence_gap == ()
