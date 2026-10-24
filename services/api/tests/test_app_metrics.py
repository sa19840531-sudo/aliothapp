"""App Workflow 进程内指标计数、观测聚合与结构化日志测试。"""

from __future__ import annotations

import logging
import math
from collections.abc import Iterator

import pytest

from services.api.app.application import app_metrics as metrics


@pytest.fixture(autouse=True)
def reset_app_metrics() -> Iterator[None]:
    """隔离进程内指标状态，避免测试顺序影响聚合断言。"""
    with metrics._LOCK:
        metrics._COUNTERS.clear()
        metrics._OBSERVATIONS.clear()
    yield
    with metrics._LOCK:
        metrics._COUNTERS.clear()
        metrics._OBSERVATIONS.clear()


@pytest.mark.parametrize(
    "name",
    [
        metrics.APP_CAPTURE_ORPHAN_TOTAL,
        metrics.APP_BOOTSTRAP_LEASE_LOST_TOTAL,
        metrics.APP_EXPLORATION_VISUAL_FACT_MISSING_TOTAL,
        metrics.APP_EXPLORATION_CLOSURE_FAILURE_TOTAL,
        metrics.APP_EXPLORATION_STALE_SESSION_TOTAL,
        metrics.APP_EXPLORATION_EVIDENCE_GAP_TOTAL,
        metrics.APP_RUN_RECOVERY_OUTCOME_TOTAL,
    ],
)
def test_counter_metric_names_are_allowed(name: str) -> None:
    assert metrics.record_app_metric(name) == 1
    assert metrics.app_metric_value(name) == 1


@pytest.mark.parametrize("name", ["", "app_unknown_total", metrics.APP_FRONTIER_CLAIM_AGE_SECONDS])
def test_counter_metric_names_are_rejected_when_unknown(name: str) -> None:
    with pytest.raises(ValueError, match="计数指标名称"):
        metrics.record_app_metric(name)
    with pytest.raises(ValueError, match="计数指标名称"):
        metrics.app_metric_value(name)


def test_technical_failure_metric_requires_controlled_recovery_labels() -> None:
    assert (
        metrics.record_app_metric(
            metrics.APP_TECHNICAL_FAILURE_TOTAL,
            phase="runtime",
            reason_code="TimeoutError",
            recovered="false",
        )
        == 1
    )


def test_manifest_backlog_metrics_reject_high_cardinality_context() -> None:
    assert (
        metrics.record_app_metric(
            metrics.APP_MANIFEST_WRITE_FAILURE_TOTAL,
            phase="backlog_finalize",
            error_type="TimeoutError",
        )
        == 1
    )
    observation = metrics.observe_app_metric(
        metrics.APP_MANIFEST_PENDING_AGE_SECONDS,
        180.0,
        phase="backlog",
    )
    assert observation.latest == 180.0
    with pytest.raises(ValueError, match="不受支持的标签"):
        metrics.observe_app_metric(
            metrics.APP_MANIFEST_PENDING_AGE_SECONDS,
            1.0,
            capture_id="high-cardinality",
        )


@pytest.mark.parametrize(
    "name",
    [metrics.APP_FRONTIER_CLAIM_AGE_SECONDS, metrics.APP_OUTBOX_LAG_SECONDS],
)
def test_observation_metric_names_are_allowed(name: str) -> None:
    observed = metrics.observe_app_metric(name, 0)

    assert observed == metrics.AppMetricObservation(count=1, total=0.0, maximum=0.0, latest=0.0)
    assert metrics.app_metric_observation(name) == observed


@pytest.mark.parametrize("name", ["", "app_unknown_seconds", metrics.APP_CAPTURE_ORPHAN_TOTAL])
def test_observation_metric_names_are_rejected_when_unknown(name: str) -> None:
    with pytest.raises(ValueError, match="观测指标名称"):
        metrics.observe_app_metric(name, 1.0)
    with pytest.raises(ValueError, match="观测指标名称"):
        metrics.app_metric_observation(name)


@pytest.mark.parametrize(
    "value",
    [True, False, "1", float("nan"), float("inf"), float("-inf"), -0.1],
)
def test_observation_values_must_be_non_negative_finite_numbers(value: object) -> None:
    with pytest.raises(ValueError, match="非负有限数"):
        metrics.observe_app_metric(metrics.APP_OUTBOX_LAG_SECONDS, value)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "context",
    [
        {"label": ""},
        {"label": "x" * 129},
        {"label": 1},
        {"": "value"},
        {"x" * 65: "value"},
    ],
)
def test_metric_context_requires_bounded_nonempty_text(context: dict[str, object]) -> None:
    with pytest.raises(ValueError, match="指标标签"):
        metrics.record_app_metric(
            metrics.APP_RUN_RECOVERY_OUTCOME_TOTAL,
            **context,  # type: ignore[arg-type]
        )
    with pytest.raises(ValueError, match="指标标签"):
        metrics.observe_app_metric(
            metrics.APP_OUTBOX_LAG_SECONDS,
            1.0,
            **context,  # type: ignore[arg-type]
        )


def test_observation_values_aggregate_count_total_maximum_and_latest() -> None:
    values = [1, 3.5, 2]

    observations = [
        metrics.observe_app_metric(metrics.APP_FRONTIER_CLAIM_AGE_SECONDS, value)
        for value in values
    ]

    assert observations[-1].count == 3
    assert observations[-1].total == pytest.approx(6.5)
    assert observations[-1].maximum == pytest.approx(3.5)
    assert observations[-1].latest == pytest.approx(2.0)
    assert (
        metrics.app_metric_observation(metrics.APP_FRONTIER_CLAIM_AGE_SECONDS)
        == observations[-1]
    )


def test_unobserved_metric_returns_zero_snapshot() -> None:
    assert metrics.app_metric_observation(
        metrics.APP_OUTBOX_LAG_SECONDS
    ) == metrics.AppMetricObservation(
        count=0, total=0.0, maximum=0.0, latest=0.0
    )


def test_structured_logs_include_counter_fields(caplog: pytest.LogCaptureFixture) -> None:
    name = metrics.APP_RUN_RECOVERY_OUTCOME_TOTAL

    with caplog.at_level(logging.WARNING, logger=metrics.__name__):
        value = metrics.record_app_metric(
            name,
            outcome="reclaimed",
            reason_code="work_due",
        )

    records = [
        record
        for record in caplog.records
        if record.message == "app_workflow_metric_incremented"
    ]
    assert len(records) == 1
    record = records[0]
    assert record.metric_name == name
    assert record.metric_value == value
    assert record.metric_context == {
        "outcome": "reclaimed",
        "reason_code": "work_due",
    }


def test_structured_logs_include_observation_aggregation_fields(
    caplog: pytest.LogCaptureFixture,
) -> None:
    name = metrics.APP_OUTBOX_LAG_SECONDS

    with caplog.at_level(logging.INFO, logger=metrics.__name__):
        observed = metrics.observe_app_metric(name, 2.5, surface_kind="android")

    records = [
        record
        for record in caplog.records
        if record.message == "app_workflow_metric_observed"
    ]
    assert len(records) == 1
    record = records[0]
    assert record.metric_name == name
    assert record.metric_value == pytest.approx(2.5)
    assert record.metric_count == observed.count == 1
    assert record.metric_total == pytest.approx(observed.total)
    assert record.metric_maximum == pytest.approx(observed.maximum)
    assert record.metric_context == {"surface_kind": "android"}


def test_metric_rejects_high_cardinality_identity_labels() -> None:
    with pytest.raises(ValueError, match="不受支持的标签"):
        metrics.record_app_metric(
            metrics.APP_RUN_RECOVERY_OUTCOME_TOTAL,
            run_id="run-1",
        )
    with pytest.raises(ValueError, match="不受支持的标签"):
        metrics.observe_app_metric(
            metrics.APP_OUTBOX_LAG_SECONDS,
            1.0,
            message_id="message-1",
        )


def test_safe_metric_helpers_never_change_business_flow(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.ERROR, logger=metrics.__name__):
        counter = metrics.record_app_metric_safely(
            metrics.APP_EXPLORATION_EVIDENCE_GAP_TOTAL,
            semantic_role="final_observation",
            reason_code="x" * 129,
        )
        observation = metrics.observe_app_metric_safely(
            metrics.APP_OUTBOX_LAG_SECONDS,
            float("nan"),
            surface_kind="android",
        )

    assert counter is None
    assert observation is None
    assert metrics.app_metric_value(metrics.APP_EXPLORATION_EVIDENCE_GAP_TOTAL) == 0
    assert metrics.app_metric_observation(
        metrics.APP_OUTBOX_LAG_SECONDS
    ).count == 0
    assert {record.message for record in caplog.records} == {
        "app_workflow_metric_record_failed",
        "app_workflow_metric_observation_failed",
    }


def test_non_negative_finite_boundary_is_accepted() -> None:
    for value in (0, 2, 2.5, math.nextafter(0.0, math.inf)):
        observed = metrics.observe_app_metric(metrics.APP_OUTBOX_LAG_SECONDS, value)

    assert observed.count == 4
    assert observed.latest == pytest.approx(math.nextafter(0.0, math.inf))
