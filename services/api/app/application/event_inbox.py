"""跨消费者事件 Inbox 的领取、完成与重试端口。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol
from uuid import UUID

EVENT_INBOX_PROTOCOL_VERSION = "event-inbox.v1"


class EventInboxConflictError(RuntimeError):
    """事件身份、消费租约或行版本已经发生变化。"""


class EventInboxClaimDisposition(StrEnum):
    """消费者收到事件后的数据库权威结论。"""

    CLAIMED = "claimed"
    COMPLETED = "completed"
    BUSY = "busy"
    DEAD_LETTER = "dead_letter"
    REJECTED = "rejected"


@dataclass(frozen=True, kw_only=True)
class EventInboxIdentity:
    """Redis 只传递、PostgreSQL 必须回查确认的事件身份。"""

    message_id: str
    message_type: str
    task_id: str
    aggregate_type: str
    aggregate_id: str
    aggregate_version: int
    idempotency_key: str

    def __post_init__(self) -> None:
        for field_name in (
            "message_id",
            "message_type",
            "task_id",
            "aggregate_type",
            "aggregate_id",
            "idempotency_key",
        ):
            _require_text(getattr(self, field_name), field_name)
        _require_non_negative_int(self.aggregate_version, "aggregate_version")


@dataclass(frozen=True, kw_only=True)
class ClaimEventInboxCommand:
    """以稳定逻辑消费者和实例租约领取事件。"""

    consumer_name: str
    lease_owner: str
    lease_token: str
    lease_seconds: int
    identity: EventInboxIdentity
    max_attempts: int = 12

    def __post_init__(self) -> None:
        _require_text(self.consumer_name, "consumer_name")
        _require_text(self.lease_owner, "lease_owner")
        _require_uuid_text(self.lease_token, "lease_token")
        _require_positive_int(self.lease_seconds, "lease_seconds")
        _require_positive_int(self.max_attempts, "max_attempts")
        if not isinstance(self.identity, EventInboxIdentity):
            raise ValueError("identity 类型不正确")


@dataclass(frozen=True, kw_only=True)
class EventInboxClaim:
    """一次 Inbox 领取的数据库结论。"""

    disposition: EventInboxClaimDisposition
    message_id: str
    row_version: int
    attempt_count: int
    lease_token: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.disposition, EventInboxClaimDisposition):
            raise ValueError("disposition 类型不正确")
        _require_text(self.message_id, "message_id")
        _require_non_negative_int(self.row_version, "row_version")
        _require_non_negative_int(self.attempt_count, "attempt_count")
        if self.disposition is EventInboxClaimDisposition.CLAIMED:
            _require_uuid_text(self.lease_token, "lease_token")
            if self.attempt_count <= 0:
                raise ValueError("已领取事件的 attempt_count 必须为正整数")
        elif self.lease_token is not None:
            raise ValueError("未领取结论不能携带 lease_token")


@dataclass(frozen=True, kw_only=True)
class CompleteEventInboxCommand:
    """以领取时版本完成事件。"""

    consumer_name: str
    message_id: str
    lease_token: str
    expected_row_version: int

    def __post_init__(self) -> None:
        _require_text(self.consumer_name, "consumer_name")
        _require_text(self.message_id, "message_id")
        _require_uuid_text(self.lease_token, "lease_token")
        _require_non_negative_int(self.expected_row_version, "expected_row_version")


@dataclass(frozen=True, kw_only=True)
class RetryEventInboxCommand:
    """释放失败事件租约并进入重试等待。"""

    consumer_name: str
    message_id: str
    lease_token: str
    expected_row_version: int
    error: str
    retry_delay_seconds: int

    def __post_init__(self) -> None:
        _require_text(self.consumer_name, "consumer_name")
        _require_text(self.message_id, "message_id")
        _require_uuid_text(self.lease_token, "lease_token")
        _require_non_negative_int(self.expected_row_version, "expected_row_version")
        _require_text(self.error, "error")
        _require_non_negative_int(self.retry_delay_seconds, "retry_delay_seconds")


@dataclass(frozen=True, kw_only=True)
class EventInboxRetryResult:
    """失败释放后的状态和新版本。"""

    status: str
    row_version: int

    def __post_init__(self) -> None:
        if self.status not in {"retry_wait", "dead_letter"}:
            raise ValueError("重试结果状态不正确")
        _require_non_negative_int(self.row_version, "row_version")


class EventInboxRepository(Protocol):
    """Inbox 守卫所需的最小 PostgreSQL 端口。"""

    async def claim(self, command: ClaimEventInboxCommand) -> EventInboxClaim: ...

    async def complete(self, command: CompleteEventInboxCommand) -> bool: ...

    async def retry(
        self,
        command: RetryEventInboxCommand,
    ) -> EventInboxRetryResult | None: ...


def _require_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError(f"{field_name} 必须为无首尾空白的非空字符串")
    return value


def _require_uuid_text(value: object, field_name: str) -> str:
    text = _require_text(value, field_name)
    try:
        UUID(text)
    except ValueError as exc:
        raise ValueError(f"{field_name} 必须为 UUID") from exc
    return text


def _require_non_negative_int(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field_name} 必须为非负整数")
    return value


def _require_positive_int(value: object, field_name: str) -> int:
    parsed = _require_non_negative_int(value, field_name)
    if parsed == 0:
        raise ValueError(f"{field_name} 必须为正整数")
    return parsed
