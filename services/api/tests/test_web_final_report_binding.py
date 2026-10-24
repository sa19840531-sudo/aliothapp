"""Web V2 正式报告绑定的不可变 Coverage 合同测试。"""

from datetime import UTC, datetime

import pytest

from services.api.app.application.web_final_report_binding import (
    WebFinalReportBindingError,
    WebFinalReportBindingExpectation,
    validate_web_final_report_binding,
)
from services.api.app.domain.exploration.web_coverage_snapshot import (
    WebCoverageSnapshot,
)


def _coverage() -> WebCoverageSnapshot:
    return WebCoverageSnapshot.create(
        snapshot_id="coverage-1",
        task_id="task-1",
        agent_run_id="run-1",
        finalization_epoch=4,
        required_page_types=("home", "video_list", "video_detail"),
        optional_page_types=("activity", "content_list", "search_page"),
        not_applicable_page_types=("content_detail",),
        observed_page_types=("home", "video_list"),
        unrecognized_page_types=("landing",),
        applicability_mode="catalog_declared",
        catalog_version="catalog.v8",
        catalog_hash="c" * 64,
        observed_lifecycle_page_types=("age_gate",),
        unknown_page_capture_ids=("capture-unknown",),
        supplemental_evidence_summary={
            "captured": 2,
            "failed": 1,
            "not_attempted": 1,
        },
        created_at=datetime(2026, 9, 6, tzinfo=UTC),
    )


def _payload(snapshot: WebCoverageSnapshot) -> dict[str, object]:
    return {
        "report_contract": {
            "target_platform": "web",
            "workflow_version": "v2",
            "execution_mode": "full_inspection",
            "notification_mode": "enforced",
        },
        "report_stage": "final",
        "is_final": True,
        "report_id": "task:task-1:run:run-1",
        "report_binding_version": 2,
        "agent_run_id": "run-1",
        "task": {"task_id": "task-1"},
        "completion_gate": {
            "gate_passed": True,
            "gate_snapshot_id": "gate-1",
            "finalization_epoch": 4,
        },
        "page_exploration": {
            "web_coverage": {
                "coverage_snapshot_id": snapshot.snapshot_id,
                "finalization_epoch": snapshot.finalization_epoch,
                "required_page_types": list(snapshot.required_page_types),
                "optional_page_types": list(snapshot.optional_page_types),
                "not_applicable_page_types": list(
                    snapshot.not_applicable_page_types
                ),
                "observed_page_types": list(snapshot.observed_page_types),
                "missing_page_types": list(snapshot.missing_page_types),
                "unrecognized_page_types": list(
                    snapshot.unrecognized_page_types
                ),
                "observed_lifecycle_page_types": list(
                    snapshot.observed_lifecycle_page_types
                ),
                "unknown_page_capture_ids": list(
                    snapshot.unknown_page_capture_ids
                ),
                "supplemental_evidence_summary": dict(
                    snapshot.supplemental_evidence_summary
                ),
                "normalization_version": snapshot.normalization_version,
                "coverage_status": snapshot.coverage_status,
                "snapshot_hash": snapshot.snapshot_hash,
                "applicability_snapshot": {
                    "required_page_types": list(snapshot.required_page_types),
                    "optional_page_types": list(snapshot.optional_page_types),
                    "not_applicable_page_types": list(
                        snapshot.not_applicable_page_types
                    ),
                    "applicability_mode": snapshot.applicability_mode,
                    "catalog_version": snapshot.catalog_version,
                    "catalog_hash": snapshot.catalog_hash,
                    "snapshot_protocol_version": (
                        snapshot.snapshot_protocol_version
                    ),
                    "observed_page_types": list(snapshot.observed_page_types),
                    "missing_page_types": list(snapshot.missing_page_types),
                    "unrecognized_page_types": list(
                        snapshot.unrecognized_page_types
                    ),
                    "lifecycle_observations": list(
                        snapshot.observed_lifecycle_page_types
                    ),
                    "unknown_page_capture_ids": list(
                        snapshot.unknown_page_capture_ids
                    ),
                    "supplemental_evidence_summary": dict(
                        snapshot.supplemental_evidence_summary
                    ),
                },
            }
        },
    }


def _expectation(snapshot: WebCoverageSnapshot) -> WebFinalReportBindingExpectation:
    return WebFinalReportBindingExpectation(
        task_id="task-1",
        agent_run_id="run-1",
        report_id="task:task-1:run:run-1",
        report_version=2,
        gate_snapshot_id="gate-1",
        finalization_epoch=4,
        coverage_snapshot_id=snapshot.snapshot_id,
        coverage_snapshot_hash=snapshot.snapshot_hash,
        coverage_snapshot=snapshot,
    )


def test_web_final_binding_accepts_full_immutable_coverage_material() -> None:
    snapshot = _coverage()

    validate_web_final_report_binding(
        _payload(snapshot),
        expectation=_expectation(snapshot),
    )


@pytest.mark.parametrize("field,value", [("task_id", "other-task"), ("agent_run_id", "other-run"), ("finalization_epoch", 99)])
def test_new_web_explanation_must_match_frozen_report_identity(field, value):
    snapshot = _coverage()
    payload = _payload(snapshot)
    payload["page_exploration"]["web_report_explanation"] = {
        "schema_version": "web-report-explanation.v1", "task_id": "task-1", "agent_run_id": "run-1", "finalization_epoch": 4,
        field: value,
    }
    with pytest.raises(WebFinalReportBindingError, match="解释"):
        validate_web_final_report_binding(payload, expectation=_expectation(snapshot))


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("required_page_types",), ["video_list"]),
        (("observed_page_types",), ["video_list", "video_detail"]),
        (("missing_page_types",), []),
        (("applicability_snapshot", "catalog_version"), "forged.v8"),
        (("applicability_snapshot", "catalog_hash"), "f" * 64),
        (("applicability_snapshot", "applicability_mode"), "strict_four"),
        (("observed_lifecycle_page_types",), []),
        (("unknown_page_capture_ids",), []),
        (("supplemental_evidence_summary",), {"captured": 99}),
        (("applicability_snapshot", "lifecycle_observations"), []),
        (("applicability_snapshot", "unknown_page_capture_ids"), []),
        (
            ("applicability_snapshot", "supplemental_evidence_summary"),
            {"captured": 99},
        ),
    ],
)
def test_web_final_binding_rejects_forged_coverage_material(
    path: tuple[str, ...],
    value: object,
) -> None:
    snapshot = _coverage()
    payload = _payload(snapshot)
    coverage = payload["page_exploration"]["web_coverage"]  # type: ignore[index]
    target = coverage
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value

    with pytest.raises(WebFinalReportBindingError, match="Coverage|coverage"):
        validate_web_final_report_binding(
            payload,
            expectation=_expectation(snapshot),
        )


def test_web_final_binding_rejects_null_database_coverage_material() -> None:
    snapshot = _coverage()
    expectation = _expectation(snapshot)
    object.__setattr__(snapshot, "catalog_hash", None)

    with pytest.raises(WebFinalReportBindingError, match="Coverage Snapshot"):
        validate_web_final_report_binding(
            _payload(_coverage()),
            expectation=expectation,
        )
