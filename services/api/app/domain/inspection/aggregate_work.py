"""EvidenceInputSet 聚合质检工作的可靠唤醒协议。"""

from __future__ import annotations

from datetime import datetime

from services.api.app.domain.protocol import ProtocolEnvelope, build_protocol_envelope

AGGREGATE_INSPECTION_WORK_AVAILABLE_MESSAGE_TYPE = "alioth.v2.aggregate-inspection-work.available"
AGGREGATE_INSPECTION_WAKEUP_PROTOCOL_VERSION = "aggregate-inspection-wakeup.v1"


def build_aggregate_inspection_work_available(
    *,
    work_item_id: str,
    task_id: str,
    evidence_input_set_id: str,
    input_hash: str,
    finalization_epoch: int,
    row_version: int,
    occurred_at: datetime,
) -> ProtocolEnvelope:
    """只传稳定身份，Consumer 必须回 PostgreSQL 重新领取。"""

    if finalization_epoch < 0 or row_version < 0:
        raise ValueError("finalization_epoch/row_version 不能为负数")
    work_id = _required_text(work_item_id, "work_item_id")
    task = _required_text(task_id, "task_id")
    input_set = _required_text(evidence_input_set_id, "evidence_input_set_id")
    frozen_input = _required_text(input_hash, "input_hash")
    return build_protocol_envelope(
        message_type=AGGREGATE_INSPECTION_WORK_AVAILABLE_MESSAGE_TYPE,
        payload_protocol_version=AGGREGATE_INSPECTION_WAKEUP_PROTOCOL_VERSION,
        idempotency_key=f"{frozen_input}:{row_version}",
        task_id=task,
        aggregate_type="aggregate_inspection_work_item",
        aggregate_id=work_id,
        aggregate_version=row_version,
        occurred_at=occurred_at,
        payload={
            "protocol_version": AGGREGATE_INSPECTION_WAKEUP_PROTOCOL_VERSION,
            "work_item_id": work_id,
            "task_id": task,
            "evidence_input_set_id": input_set,
            "input_hash": frozen_input,
            "finalization_epoch": finalization_epoch,
            "message_version": row_version,
        },
        correlation_id=task,
    )


def _required_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError(f"{field_name} 必须为无首尾空白的非空字符串")
    return value
