"""App sealed Evidence Index 到规则工作的持久化发布协议。"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid5

from services.api.app.domain.protocol import ProtocolEnvelope, build_protocol_envelope

APP_INSPECTION_PUBLICATION_PROTOCOL_VERSION = "app-inspection-publication.v1"
APP_INSPECTION_PUBLICATION_AVAILABLE_MESSAGE_TYPE = (
    "alioth.v2.app-inspection-publication.available"
)
_PUBLICATION_NAMESPACE = UUID("145228df-e4b4-47a8-97cb-9ae66657d4f1")


def build_app_inspection_publication_id(
    *,
    task_id: str,
    finalization_epoch: int,
    evidence_index_id: str,
    evidence_index_hash: str,
    ruleset_snapshot_hash: str | None,
) -> str:
    """由封存索引、任务 epoch 与锁定规则集派生跨进程稳定 UUID。"""

    if finalization_epoch < 0:
        raise ValueError("finalization_epoch 不能为负数")
    parts = (
        _required_text(task_id, "task_id"),
        str(finalization_epoch),
        _required_text(evidence_index_id, "evidence_index_id"),
        _required_text(evidence_index_hash, "evidence_index_hash"),
        str(ruleset_snapshot_hash or ""),
    )
    return str(uuid5(_PUBLICATION_NAMESPACE, ":".join(parts)))


def build_app_inspection_publication_available(
    *,
    publication_id: str,
    task_id: str,
    evidence_index_id: str,
    evidence_index_hash: str,
    finalization_epoch: int,
    row_version: int,
    input_hash: str,
    occurred_at: datetime,
) -> ProtocolEnvelope:
    """构造只携带稳定身份的 Publication 可用事件。"""

    if row_version <= 0:
        raise ValueError("row_version 必须为正整数")
    publication = _required_text(publication_id, "publication_id")
    task = _required_text(task_id, "task_id")
    index_id = _required_text(evidence_index_id, "evidence_index_id")
    index_hash = _required_text(evidence_index_hash, "evidence_index_hash")
    frozen_input = _required_text(input_hash, "input_hash")
    return build_protocol_envelope(
        message_type=APP_INSPECTION_PUBLICATION_AVAILABLE_MESSAGE_TYPE,
        payload_protocol_version=APP_INSPECTION_PUBLICATION_PROTOCOL_VERSION,
        idempotency_key=f"{frozen_input}:{row_version}",
        task_id=task,
        aggregate_type="app_inspection_publication",
        aggregate_id=publication,
        aggregate_version=row_version,
        occurred_at=occurred_at,
        payload={
            "protocol_version": APP_INSPECTION_PUBLICATION_PROTOCOL_VERSION,
            "publication_id": publication,
            "task_id": task,
            "evidence_index_id": index_id,
            "evidence_index_hash": index_hash,
            "finalization_epoch": finalization_epoch,
            "message_version": row_version,
            "input_hash": frozen_input,
        },
        correlation_id=task,
    )


def _required_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError(f"{field_name} 必须为无首尾空白的非空字符串")
    return value
