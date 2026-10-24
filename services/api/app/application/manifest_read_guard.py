"""E21：无清单或校验失败时的拒判通路与审计告警。

冻结依据：
- E21 验收：无清单、证据被改动或 checksum 不一致时拒判；校验不一致产生告警
- E02 条款 1.1：判定侧只读清单结论——拒判不是"判定为不通过"，
  而是拒绝产出任何 pass/fail 结论

拒判口径（E02 条款 2.1：技术故障不得伪装成证据不足）：

| 读取结果 | execution_status | 原因码 | 告警 |
|---|---|---|---|
| `evidence_insufficient`（清单裁决 fail） | `evidence_insufficient` | 同名 | 否 |
| `evidence_pending`（清单裁决 pending，证据在途） | `error` | `manifest_pending` | 否 |
| `control_plane_incomplete`（无清单／归属不符） | `error` | `manifest_absent` | 否 |
| `evidence_integrity_failed`（缺失／不可读／校验不一致） | `error` | `evidence_tampered` | **是** |
| `payload_budget_exceeded`（E02 条款 1.5） | `error` | `payload_budget_exceeded` | 否 |

**只有清单裁决为 fail 才报「证据不足」**：这是读清单结论，不是判定侧自己数证据
（条款 1.1）。其余四类都是技术性故障或资源约束，一律不占用
`evidence_insufficient`；细粒度差异由业务结果与原因码承载，
`execution_status` 只是受 `ck_qc_rule_execution_status` 约束的粗粒度落库桶
（pass/fail/warning/manual_review/evidence_insufficient/skipped/error）。

告警只描述证据身份与失败原因，不携带证据原文（E26 敏感字段边界）。
生产判定路径的接线随 E22 移除附件直查、E24 切主读取路径落地；
本模块提供 `load_verified_source` 作为拒判通路的统一入口。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from services.api.app.application.manifest_backed_observation import (
    ManifestBackedObservationRepository,
    ManifestReadDecision,
    ManifestReadOutcome,
)
from services.api.app.application.manifest_backed_packet import ManifestBackedReadError
from services.api.app.application.observation_packet import ObservationSource

logger = logging.getLogger(__name__)

MANIFEST_READ_REJECTION_SCHEMA_VERSION = "manifest-read-rejection.v1"

# judge_input 审计投影中承载拒判结论的键
MANIFEST_READ_REJECTION_AUDIT_KEY = "manifest_read_rejection"

ALERT_CODE_EVIDENCE_INTEGRITY_FAILED = "evidence_manifest_integrity_failed"

# E02 条款 2.1：只有清单裁决 fail 才占用 evidence_insufficient
REJECTION_STATUS_BY_OUTCOME: dict[ManifestReadOutcome, str] = {
    ManifestReadOutcome.EVIDENCE_INSUFFICIENT: "evidence_insufficient",
    ManifestReadOutcome.EVIDENCE_PENDING: "error",
    ManifestReadOutcome.CONTROL_PLANE_INCOMPLETE: "error",
    ManifestReadOutcome.EVIDENCE_INTEGRITY_FAILED: "error",
    ManifestReadOutcome.PAYLOAD_BUDGET_EXCEEDED: "error",
}

# 独立原因码：粗粒度 execution_status 之外的可区分表达（E02 条款 1.5/2.1）
REJECTION_REASON_CODE_BY_OUTCOME: dict[ManifestReadOutcome, str] = {
    ManifestReadOutcome.EVIDENCE_INSUFFICIENT: "manifest_quality_failed",
    ManifestReadOutcome.EVIDENCE_PENDING: "manifest_pending",
    ManifestReadOutcome.CONTROL_PLANE_INCOMPLETE: "manifest_absent",
    ManifestReadOutcome.EVIDENCE_INTEGRITY_FAILED: "evidence_tampered",
    ManifestReadOutcome.PAYLOAD_BUDGET_EXCEEDED: "payload_budget_exceeded",
}

_REJECTION_SUMMARY_BY_OUTCOME: dict[ManifestReadOutcome, str] = {
    ManifestReadOutcome.EVIDENCE_INSUFFICIENT: "证据清单裁决未通过，拒绝判定",
    ManifestReadOutcome.EVIDENCE_PENDING: "证据提交在途，拒绝判定",
    ManifestReadOutcome.CONTROL_PLANE_INCOMPLETE: "证据清单不可用，拒绝判定",
    ManifestReadOutcome.EVIDENCE_INTEGRITY_FAILED: "证据与清单不一致，拒绝判定",
    ManifestReadOutcome.PAYLOAD_BUDGET_EXCEEDED: "判定载荷超出预算，拒绝判定",
}


@dataclass(frozen=True, kw_only=True)
class ManifestReadAlert:
    """证据不可信的审计告警；只含身份与原因，不含证据原文。"""

    alert_code: str
    task_id: str
    capture_id: str
    outcome: ManifestReadOutcome
    detected_at: datetime
    manifest_id: str | None = None
    manifest_version: int | None = None
    reason: str | None = None

    def to_audit_payload(self) -> dict[str, Any]:
        return {
            "alert_code": self.alert_code,
            "task_id": self.task_id,
            "capture_id": self.capture_id,
            "outcome": self.outcome.value,
            "manifest_id": self.manifest_id,
            "manifest_version": self.manifest_version,
            "detected_at": self.detected_at.isoformat(),
            "reason": self.reason,
        }


class ManifestReadAlertSink(Protocol):
    """告警写入目的地契约，可为日志态、内存态或后续监控实现。"""

    def record(self, alert: ManifestReadAlert) -> None: ...


class LoggingManifestReadAlertSink:
    """默认告警实现：按可检索的稳定事件名写错误日志。"""

    def record(self, alert: ManifestReadAlert) -> None:
        logger.error(
            "%s task_id=%s capture_id=%s manifest_id=%s manifest_version=%s reason=%s",
            alert.alert_code,
            alert.task_id,
            alert.capture_id,
            alert.manifest_id,
            alert.manifest_version,
            alert.reason,
        )


class InMemoryManifestReadAlertSink:
    """内存态告警，供测试与单次 Run 内自查使用。"""

    def __init__(self) -> None:
        self.alerts: list[ManifestReadAlert] = []

    def record(self, alert: ManifestReadAlert) -> None:
        self.alerts.append(alert)


@dataclass(frozen=True, kw_only=True)
class ManifestReadRejection:
    """拒判结论：说明为何拒绝判定，不产出任何 pass/fail。"""

    execution_status: str
    reason_code: str
    result_summary: str
    reason: str
    decision: ManifestReadDecision
    alert: ManifestReadAlert | None = None

    @property
    def alerted(self) -> bool:
        return self.alert is not None

    @property
    def business_outcome(self) -> ManifestReadOutcome:
        """细粒度业务结果；execution_status 只是粗粒度落库桶。"""
        return self.decision.outcome

    def to_judge_output(self) -> dict[str, Any]:
        """判定输出：结论位固定为拒判状态，不得被读成通过或不通过。"""
        return {
            "schema_version": MANIFEST_READ_REJECTION_SCHEMA_VERSION,
            "conclusion": self.execution_status,
            "reason_code": self.reason_code,
            "business_outcome": self.business_outcome.value,
            "reasoning_summary": self.reason,
            "evidence_trusted": False,
        }

    def to_audit_payload(self) -> dict[str, Any]:
        """审计投影：拒判结论 + 读取裁决 +（如有）告警。"""
        payload: dict[str, Any] = {
            "schema_version": MANIFEST_READ_REJECTION_SCHEMA_VERSION,
            "execution_status": self.execution_status,
            "reason_code": self.reason_code,
            "business_outcome": self.business_outcome.value,
            "reason": self.reason,
            "decision": self.decision.to_audit_payload(),
        }
        if self.alert is not None:
            payload["alert"] = self.alert.to_audit_payload()
        return payload


class ManifestReadGuard:
    """把清单读取裁决转成拒判，并对校验不一致发出告警。"""

    def __init__(
        self,
        *,
        alert_sink: ManifestReadAlertSink | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._alert_sink = alert_sink or LoggingManifestReadAlertSink()
        self._now = now or (lambda: datetime.now(UTC))

    def evaluate(self, decision: ManifestReadDecision) -> ManifestReadRejection | None:
        """校验通过返回 None；否则产出拒判，并按需告警。"""
        if decision.outcome is ManifestReadOutcome.VERIFIED:
            return None
        status = REJECTION_STATUS_BY_OUTCOME.get(decision.outcome)
        if status is None:  # pragma: no cover - 枚举已穷举
            raise ValueError(f"未定义的读取裁决处置：{decision.outcome}")
        alert: ManifestReadAlert | None = None
        if decision.outcome is ManifestReadOutcome.EVIDENCE_INTEGRITY_FAILED:
            alert = ManifestReadAlert(
                alert_code=ALERT_CODE_EVIDENCE_INTEGRITY_FAILED,
                task_id=decision.task_id,
                capture_id=decision.capture_id,
                outcome=decision.outcome,
                detected_at=self._now(),
                manifest_id=decision.manifest_id,
                manifest_version=decision.manifest_version,
                reason=decision.reason,
            )
            self._alert_sink.record(alert)
        return ManifestReadRejection(
            execution_status=status,
            reason_code=REJECTION_REASON_CODE_BY_OUTCOME[decision.outcome],
            result_summary=_REJECTION_SUMMARY_BY_OUTCOME[decision.outcome],
            reason=decision.reason or _REJECTION_SUMMARY_BY_OUTCOME[decision.outcome],
            decision=decision,
            alert=alert,
        )


async def load_verified_source(
    repository: ManifestBackedObservationRepository,
    *,
    task_id: str,
    capture_id: str,
    guard: ManifestReadGuard,
) -> tuple[ObservationSource | None, ManifestReadRejection | None]:
    """拒判通路统一入口：要么拿到可信证据，要么拿到拒判结论。

    返回 `(source, rejection)`：
    - 证据可信：`(source, None)`；
    - 无清单或校验失败：`(None, rejection)`，调用方不得继续判定；
    - Capture 上下文不存在：`(None, None)`，交由既有页面缺失路径处理。

    读取失败一律不再向上抛异常，避免判定链把证据不可信当成运行异常吞掉。
    """
    try:
        source = await repository.load_source(
            task_id=task_id,
            page_capture_id=capture_id,
        )
    except ManifestBackedReadError:
        decision = repository.decision_for(task_id=task_id, capture_id=capture_id)
        if decision is None:  # pragma: no cover - E20 抛出前必留裁决
            raise
        rejection = guard.evaluate(decision)
        if rejection is None:  # pragma: no cover - 失败裁决必定产出拒判
            raise
        return None, rejection
    if source is None:
        return None, None
    decision = repository.decision_for(task_id=task_id, capture_id=capture_id)
    if decision is not None:
        rejection = guard.evaluate(decision)
        if rejection is not None:
            return None, rejection
    return source, None
