"""App Workflow 的进程内计数与结构化日志出口。"""

from __future__ import annotations

import logging
import math
from collections import Counter
from dataclasses import dataclass
from threading import Lock

APP_CAPTURE_ORPHAN_TOTAL = "app_capture_orphan_total"
APP_BOOTSTRAP_LEASE_LOST_TOTAL = "app_bootstrap_lease_lost_total"
APP_EXPLORATION_VISUAL_FACT_MISSING_TOTAL = "app_exploration_visual_fact_missing_total"
APP_EXPLORATION_CLOSURE_FAILURE_TOTAL = "app_exploration_closure_failure_total"
APP_EXPLORATION_STALE_SESSION_TOTAL = "app_exploration_stale_session_total"
APP_EXPLORATION_EVIDENCE_GAP_TOTAL = "app_exploration_evidence_gap_total"
APP_RUN_RECOVERY_OUTCOME_TOTAL = "app_run_recovery_outcome_total"
APP_TECHNICAL_FAILURE_TOTAL = "app_technical_failure_total"
APP_MANIFEST_WRITE_FAILURE_TOTAL = "app_manifest_write_failure_total"
APP_MANIFEST_PENDING_AGE_SECONDS = "app_manifest_pending_age_seconds"
APP_FRONTIER_CLAIM_AGE_SECONDS = "app_frontier_claim_age_seconds"
APP_OUTBOX_LAG_SECONDS = "app_outbox_lag_seconds"

_COUNTER_METRICS = frozenset(
    {
        APP_CAPTURE_ORPHAN_TOTAL,
        APP_BOOTSTRAP_LEASE_LOST_TOTAL,
        APP_EXPLORATION_VISUAL_FACT_MISSING_TOTAL,
        APP_EXPLORATION_CLOSURE_FAILURE_TOTAL,
        APP_EXPLORATION_STALE_SESSION_TOTAL,
        APP_EXPLORATION_EVIDENCE_GAP_TOTAL,
        APP_RUN_RECOVERY_OUTCOME_TOTAL,
        APP_TECHNICAL_FAILURE_TOTAL,
        APP_MANIFEST_WRITE_FAILURE_TOTAL,
    }
)
_OBSERVATION_METRICS = frozenset(
    {
        APP_FRONTIER_CLAIM_AGE_SECONDS,
        APP_OUTBOX_LAG_SECONDS,
        APP_MANIFEST_PENDING_AGE_SECONDS,
    }
)
_COUNTERS: Counter[str] = Counter()
_LOCK = Lock()
_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, kw_only=True)
class AppMetricObservation:
    """单进程时长观测聚合；结构化日志供外部采集器跨进程汇总。"""

    count: int
    total: float
    maximum: float
    latest: float


_OBSERVATIONS: dict[str, AppMetricObservation] = {}
_METRIC_CONTEXT_FIELDS = {
    APP_CAPTURE_ORPHAN_TOTAL: frozenset(),
    APP_BOOTSTRAP_LEASE_LOST_TOTAL: frozenset({"phase"}),
    APP_EXPLORATION_VISUAL_FACT_MISSING_TOTAL: frozenset(
        {"semantic_role", "reason_code", "error_code"}
    ),
    APP_EXPLORATION_CLOSURE_FAILURE_TOTAL: frozenset({"phase", "error_type", "recovered"}),
    APP_EXPLORATION_STALE_SESSION_TOTAL: frozenset({"reason_code", "surface_kind", "outcome"}),
    APP_EXPLORATION_EVIDENCE_GAP_TOTAL: frozenset({"semantic_role", "reason_code"}),
    APP_RUN_RECOVERY_OUTCOME_TOTAL: frozenset({"reason_code", "outcome"}),
    APP_TECHNICAL_FAILURE_TOTAL: frozenset({"phase", "reason_code", "recovered"}),
    APP_MANIFEST_WRITE_FAILURE_TOTAL: frozenset({"phase", "error_type"}),
    APP_FRONTIER_CLAIM_AGE_SECONDS: frozenset({"surface_kind"}),
    APP_OUTBOX_LAG_SECONDS: frozenset({"surface_kind"}),
    APP_MANIFEST_PENDING_AGE_SECONDS: frozenset({"phase"}),
}


def _validate_metric_context(name: str, context: dict[str, str]) -> None:
    """指标标签只允许短文本，禁止把高基数身份或敏感配置带入聚合出口。"""

    for field_name, value in context.items():
        if not isinstance(field_name, str) or not field_name or len(field_name) > 64:
            raise ValueError("App Workflow 指标标签名称不合法")
        if not isinstance(value, str) or not value.strip() or len(value) > 128:
            raise ValueError("App Workflow 指标标签值必须为短非空文本")
    allowed_fields = _METRIC_CONTEXT_FIELDS[name]
    unexpected = set(context) - allowed_fields
    if unexpected:
        raise ValueError("App Workflow 指标包含不受支持的标签")


def record_app_metric(name: str, **context: str) -> int:
    """递增受控指标并写结构化日志，便于外部采集器汇总多进程值。"""
    if name not in _COUNTER_METRICS:
        raise ValueError("App Workflow 计数指标名称不受支持")
    _validate_metric_context(name, context)
    with _LOCK:
        _COUNTERS[name] += 1
        value = _COUNTERS[name]
    _LOGGER.warning(
        "app_workflow_metric_incremented",
        extra={"metric_name": name, "metric_value": value, "metric_context": context},
    )
    return value


def record_app_metric_safely(name: str, **context: str) -> int | None:
    """尽力记录计数；观测出口异常不得改变 App 业务终态。"""

    try:
        return record_app_metric(name, **context)
    except Exception as exc:  # noqa: BLE001 - 指标失败不得反向打断业务主链
        _LOGGER.exception(
            "app_workflow_metric_record_failed",
            extra={"metric_name": name, "metric_error_type": type(exc).__name__},
        )
        return None


def app_metric_value(name: str) -> int:
    """返回当前进程计数，供健康检查和回归测试读取。"""
    if name not in _COUNTER_METRICS:
        raise ValueError("App Workflow 计数指标名称不受支持")
    with _LOCK:
        return _COUNTERS[name]


def observe_app_metric(name: str, value: float, **context: str) -> AppMetricObservation:
    """记录非负时长并返回 count/total/max/latest 聚合。"""

    if name not in _OBSERVATION_METRICS:
        raise ValueError("App Workflow 观测指标名称不受支持")
    _validate_metric_context(name, context)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("App Workflow 观测值必须为非负有限数")
    numeric_value = float(value)
    if not math.isfinite(numeric_value) or numeric_value < 0:
        raise ValueError("App Workflow 观测值必须为非负有限数")
    with _LOCK:
        previous = _OBSERVATIONS.get(name)
        observation = AppMetricObservation(
            count=1 if previous is None else previous.count + 1,
            total=numeric_value if previous is None else previous.total + numeric_value,
            maximum=numeric_value if previous is None else max(previous.maximum, numeric_value),
            latest=numeric_value,
        )
        _OBSERVATIONS[name] = observation
    _LOGGER.info(
        "app_workflow_metric_observed",
        extra={
            "metric_name": name,
            "metric_value": numeric_value,
            "metric_count": observation.count,
            "metric_total": observation.total,
            "metric_maximum": observation.maximum,
            "metric_context": context,
        },
    )
    return observation


def observe_app_metric_safely(
    name: str,
    value: float,
    **context: str,
) -> AppMetricObservation | None:
    """尽力记录时长；无效标签或采集故障只写日志，不影响业务。"""

    try:
        return observe_app_metric(name, value, **context)
    except Exception as exc:  # noqa: BLE001 - 指标失败不得反向打断业务主链
        _LOGGER.exception(
            "app_workflow_metric_observation_failed",
            extra={"metric_name": name, "metric_error_type": type(exc).__name__},
        )
        return None


def app_metric_observation(name: str) -> AppMetricObservation:
    """返回当前进程时长聚合；尚未观测时返回全零快照。"""

    if name not in _OBSERVATION_METRICS:
        raise ValueError("App Workflow 观测指标名称不受支持")
    with _LOCK:
        return _OBSERVATIONS.get(
            name,
            AppMetricObservation(count=0, total=0.0, maximum=0.0, latest=0.0),
        )
