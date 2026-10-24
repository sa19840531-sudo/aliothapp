"""任务提交事实的可靠唤醒协议。"""

from __future__ import annotations

from datetime import datetime

from services.api.app.domain.protocol import ProtocolEnvelope, build_protocol_envelope

TASK_SUBMISSION_COMMITTED_MESSAGE_TYPE = "alioth.v2.task-submission.committed"
TASK_SUBMISSION_WAKEUP_PROTOCOL_VERSION = "task-submission-wakeup.v1"


def build_task_submission_committed(
    *,
    submission_id: str,
    task_id: str,
    run_id: str,
    request_hash: str,
    occurred_at: datetime,
) -> ProtocolEnvelope:
    """发布稳定身份；Consumer 必须回 PostgreSQL 读取初始 Run。"""

    submission = _required_text(submission_id, "submission_id")
    task = _required_text(task_id, "task_id")
    run = _required_text(run_id, "run_id")
    frozen_hash = _required_text(request_hash, "request_hash")
    return build_protocol_envelope(
        message_type=TASK_SUBMISSION_COMMITTED_MESSAGE_TYPE,
        payload_protocol_version=TASK_SUBMISSION_WAKEUP_PROTOCOL_VERSION,
        idempotency_key=f"task-submission:{submission}:0",
        task_id=task,
        aggregate_type="task_submission",
        aggregate_id=submission,
        aggregate_version=0,
        occurred_at=occurred_at,
        payload={
            "protocol_version": TASK_SUBMISSION_WAKEUP_PROTOCOL_VERSION,
            "submission_id": submission,
            "task_id": task,
            "run_id": run,
            "request_hash": frozen_hash,
        },
        correlation_id=task,
    )


def _required_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError(f"{field_name} 必须为无首尾空白的非空字符串")
    return value
