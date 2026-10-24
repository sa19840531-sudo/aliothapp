"""通用 Action Execution 账本合同。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

from services.api.app.domain.interaction.action_intent import ActionIntent
from services.api.app.domain.interaction.validation import (
    RUNTIME_HANDLE_KEYS,
    InteractionValidationError,
    freeze_mapping,
    optional_text,
    require_text,
    thaw_mapping,
)
from services.api.app.domain.protocol import (
    ProtocolContractError,
    parse_aware_datetime,
    read_known_fields,
    require_mapping,
    stable_protocol_hash,
)

ACTION_EXECUTION_PROTOCOL_VERSION = "action-execution.v1"


class ActionExecutionStatus(StrEnum):
    """动作与基础证据提交必须分别可观察。"""

    PLANNED = "planned"
    EXECUTING = "executing"
    EVIDENCE_PENDING = "evidence_pending"
    COMPLETED = "completed"
    EVIDENCE_FAILED = "evidence_failed"
    BLOCKED = "blocked"
    FAILED = "failed"
    TIMED_OUT = "timed_out"

    @property
    def is_terminal(self) -> bool:
        return self in {
            ActionExecutionStatus.COMPLETED,
            ActionExecutionStatus.EVIDENCE_FAILED,
            ActionExecutionStatus.BLOCKED,
            ActionExecutionStatus.FAILED,
            ActionExecutionStatus.TIMED_OUT,
        }


@dataclass(frozen=True, kw_only=True)
class ActionExecutionSnapshot:
    """可持久化、可恢复且不含 Driver 句柄的动作账本投影。"""

    action_execution_id: str
    task_id: str
    session_id: str
    intent_id: str
    intent_hash: str
    command_sequence: int
    finalization_epoch: int
    # 入口动作发生在任何采集之前，没有可引用的父采集，此时为 None
    requested_source_capture_id: str | None
    status: ActionExecutionStatus
    row_version: int
    source_capture_id: str | None = None
    result_capture_id: str | None = None
    result_payload: Mapping[str, Any] = field(default_factory=dict)
    error_code: str | None = None
    error_message: str | None = None
    created_at: datetime | None = None
    started_at: datetime | None = None
    action_finished_at: datetime | None = None
    evidence_finished_at: datetime | None = None
    protocol_version: str = ACTION_EXECUTION_PROTOCOL_VERSION

    def __post_init__(self) -> None:
        for field_name in (
            "action_execution_id",
            "task_id",
            "session_id",
            "intent_id",
            "intent_hash",
        ):
            require_text(getattr(self, field_name), field_name)
        for field_name in ("command_sequence", "finalization_epoch", "row_version"):
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise InteractionValidationError(f"{field_name} 必须为非负整数")
        if self.command_sequence <= 0:
            raise InteractionValidationError("command_sequence 必须为正整数")
        if not isinstance(self.status, ActionExecutionStatus):
            raise InteractionValidationError("status 不受支持")
        for field_name in (
            "requested_source_capture_id",
            "source_capture_id",
            "result_capture_id",
            "error_code",
            "error_message",
        ):
            optional_text(getattr(self, field_name), field_name)
        object.__setattr__(
            self,
            "result_payload",
            freeze_mapping(
                self.result_payload,
                "result_payload",
                forbidden_keys=RUNTIME_HANDLE_KEYS,
            ),
        )
        if self.status is ActionExecutionStatus.PLANNED:
            if any(
                value is not None
                for value in (
                    self.source_capture_id,
                    self.result_capture_id,
                    self.error_code,
                    self.started_at,
                    self.action_finished_at,
                    self.evidence_finished_at,
                )
            ):
                raise InteractionValidationError("planned 动作不能提前携带执行字段")
        elif (
            self.status is ActionExecutionStatus.FAILED
            and self.source_capture_id is None
            and self.started_at is None
        ):
            if self.action_finished_at is None:
                raise InteractionValidationError("动作前失败必须记录 action_finished_at")
        elif self.source_capture_id is None or self.started_at is None:
            raise InteractionValidationError("已开始动作必须绑定动作前 Capture 与 started_at")
        if self.status is ActionExecutionStatus.EVIDENCE_PENDING:
            if self.action_finished_at is None or self.result_capture_id is not None:
                raise InteractionValidationError(
                    "evidence_pending 必须已完成动作且尚无结果 Capture"
                )
        if self.status is ActionExecutionStatus.COMPLETED:
            if self.result_capture_id is None or self.evidence_finished_at is None:
                raise InteractionValidationError("completed 动作必须绑定结果 Capture")
            if self.error_code is not None:
                raise InteractionValidationError("completed 动作不能携带错误")
        if (
            self.status
            in {
                ActionExecutionStatus.EVIDENCE_FAILED,
                ActionExecutionStatus.BLOCKED,
                ActionExecutionStatus.FAILED,
                ActionExecutionStatus.TIMED_OUT,
            }
            and self.error_code is None
        ):
            raise InteractionValidationError("失败或阻断终态必须携带 error_code")
        if self.status is ActionExecutionStatus.EVIDENCE_FAILED:
            if self.action_finished_at is None or self.evidence_finished_at is None:
                raise InteractionValidationError("evidence_failed 必须记录动作和证据终止时间")
        if self.protocol_version != ACTION_EXECUTION_PROTOCOL_VERSION:
            raise InteractionValidationError("ActionExecutionSnapshot 版本不受支持")
        for field_name in (
            "created_at",
            "started_at",
            "action_finished_at",
            "evidence_finished_at",
        ):
            value = getattr(self, field_name)
            if value is not None and value.utcoffset() is None:
                raise InteractionValidationError(f"{field_name} 必须带时区")

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol_version": self.protocol_version,
            "action_execution_id": self.action_execution_id,
            "task_id": self.task_id,
            "session_id": self.session_id,
            "intent_id": self.intent_id,
            "intent_hash": self.intent_hash,
            "command_sequence": self.command_sequence,
            "finalization_epoch": self.finalization_epoch,
            "requested_source_capture_id": self.requested_source_capture_id,
            "source_capture_id": self.source_capture_id,
            "result_capture_id": self.result_capture_id,
            "status": self.status.value,
            "result_payload": thaw_mapping(self.result_payload),
            "error_code": self.error_code,
            "error_message": self.error_message,
            "state_version": self.row_version,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "action_finished_at": (
                self.action_finished_at.isoformat() if self.action_finished_at else None
            ),
            "evidence_finished_at": (
                self.evidence_finished_at.isoformat() if self.evidence_finished_at else None
            ),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> ActionExecutionSnapshot:
        known = {
            "protocol_version",
            "action_execution_id",
            "task_id",
            "session_id",
            "intent_id",
            "intent_hash",
            "command_sequence",
            "finalization_epoch",
            "requested_source_capture_id",
            "source_capture_id",
            "result_capture_id",
            "status",
            "result_payload",
            "error_code",
            "error_message",
            "state_version",
            "created_at",
            "started_at",
            "action_finished_at",
            "evidence_finished_at",
        }
        try:
            values = read_known_fields(
                payload,
                known_fields=frozenset(known),
                required_fields=frozenset(known),
                field_name="ActionExecutionSnapshot",
            )
            return cls(
                action_execution_id=values["action_execution_id"],
                task_id=values["task_id"],
                session_id=values["session_id"],
                intent_id=values["intent_id"],
                intent_hash=values["intent_hash"],
                command_sequence=values["command_sequence"],
                finalization_epoch=values["finalization_epoch"],
                requested_source_capture_id=values["requested_source_capture_id"],
                source_capture_id=values["source_capture_id"],
                result_capture_id=values["result_capture_id"],
                status=ActionExecutionStatus(values["status"]),
                result_payload=require_mapping(values["result_payload"], "result_payload"),
                error_code=values["error_code"],
                error_message=values["error_message"],
                row_version=values["state_version"],
                created_at=_parse_optional_datetime(values["created_at"], "created_at"),
                started_at=_parse_optional_datetime(values["started_at"], "started_at"),
                action_finished_at=_parse_optional_datetime(
                    values["action_finished_at"],
                    "action_finished_at",
                ),
                evidence_finished_at=_parse_optional_datetime(
                    values["evidence_finished_at"],
                    "evidence_finished_at",
                ),
                protocol_version=values["protocol_version"],
            )
        except (ProtocolContractError, ValueError) as exc:
            raise InteractionValidationError(str(exc)) from exc


def build_action_execution_id(intent: ActionIntent) -> str:
    """以任务、会话和幂等键生成跨重试稳定的动作身份。"""
    if not isinstance(intent, ActionIntent):
        raise TypeError("intent 必须为 ActionIntent")
    return "action-execution:" + stable_protocol_hash(
        {
            "task_id": intent.task_id,
            "session_id": intent.session_id,
            "idempotency_key": intent.idempotency_key,
        }
    )


def build_action_intent_hash(intent: ActionIntent) -> str:
    """锁定动作语义，防止相同幂等键被另一份内容复用。"""
    if not isinstance(intent, ActionIntent):
        raise TypeError("intent 必须为 ActionIntent")
    return stable_protocol_hash(intent.to_dict())


def _parse_optional_datetime(value: Any, field_name: str) -> datetime | None:
    if value is None:
        return None
    return parse_aware_datetime(value, field_name)
