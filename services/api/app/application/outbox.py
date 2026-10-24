"""Transactional Outbox 的应用层消费端口。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol
from uuid import uuid4

from services.api.app.application.app_metrics import (
    APP_OUTBOX_LAG_SECONDS,
    observe_app_metric_safely,
)
from services.api.app.domain.protocol import ProtocolEnvelope

INSPECTION_WORK_AVAILABLE_MESSAGE_TYPE = "alioth.v2.inspection-work.available"

TASK_FINALIZATION_PRIORITY_WAKEUP_MESSAGE_TYPES = (
    INSPECTION_WORK_AVAILABLE_MESSAGE_TYPE,
    "alioth.v2.h5-inspection-publication.available",
    "alioth.v2.app-inspection-publication.available",
)

SUPPORTED_WAKEUP_MESSAGE_TYPES = (
    "alioth.v2.task-submission.committed",
    "alioth.v2.exploration-work.available",
    "alioth.v2.probe-work.available",
    INSPECTION_WORK_AVAILABLE_MESSAGE_TYPE,
    "alioth.v2.capture.committed",
    "alioth.v2.evidence-bundle.ready",
    "alioth.v2.evidence-supplement.attached",
    "alioth.v2.h5-inspection-publication.available",
    "alioth.v2.app-inspection-publication.available",
    "alioth.v2.aggregate-inspection-work.available",
)


@dataclass(frozen=True, kw_only=True)
class OutboxLease:
    """发布器领取的一条消息及其短期租约。"""

    aggregate_type: str
    aggregate_id: str
    aggregate_version: int
    envelope: ProtocolEnvelope
    lease_token: str
    attempt_count: int

    def __post_init__(self) -> None:
        for field_name in ("aggregate_type", "aggregate_id", "lease_token"):
            _require_text(getattr(self, field_name), field_name)
        if not isinstance(self.envelope, ProtocolEnvelope):
            raise ValueError("envelope 类型不正确")
        if (
            isinstance(self.aggregate_version, bool)
            or not isinstance(self.aggregate_version, int)
            or self.aggregate_version < 0
        ):
            raise ValueError("aggregate_version 必须为非负整数")
        if self.envelope.aggregate_type is not None and (
            self.envelope.aggregate_type != self.aggregate_type
            or self.envelope.aggregate_id != self.aggregate_id
            or self.envelope.aggregate_version != self.aggregate_version
        ):
            raise ValueError("Outbox 租约聚合身份与信封不一致")
        if (
            isinstance(self.attempt_count, bool)
            or not isinstance(self.attempt_count, int)
            or self.attempt_count <= 0
        ):
            raise ValueError("attempt_count 必须为正整数")


@dataclass(frozen=True, kw_only=True)
class WakeupTransportReceipt:
    """Redis Stream 已接收唤醒信号的最小回执。"""

    stream_name: str
    stream_entry_id: str

    def __post_init__(self) -> None:
        if len(_require_text(self.stream_name, "stream_name")) > 255:
            raise ValueError("stream_name 不能超过 255 字符")
        if len(_require_text(self.stream_entry_id, "stream_entry_id")) > 128:
            raise ValueError("stream_entry_id 不能超过 128 字符")


@dataclass(frozen=True, kw_only=True)
class OutboxRelayResult:
    """Relay 单次领取的可观测结果。"""

    claimed: bool
    message_id: str | None = None
    attempt_count: int | None = None
    receipt: WakeupTransportReceipt | None = None
    rescheduled: bool = False
    error: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.claimed, bool) or not isinstance(self.rescheduled, bool):
            raise ValueError("claimed/rescheduled 必须为布尔值")
        if not self.claimed:
            if (
                any(
                    value is not None
                    for value in (
                        self.message_id,
                        self.attempt_count,
                        self.receipt,
                        self.error,
                    )
                )
                or self.rescheduled
            ):
                raise ValueError("未领取结果不能携带消息状态")
            return
        _require_text(self.message_id, "message_id")
        if (
            isinstance(self.attempt_count, bool)
            or not isinstance(self.attempt_count, int)
            or self.attempt_count <= 0
        ):
            raise ValueError("attempt_count 必须为正整数")
        if self.rescheduled == (self.receipt is not None):
            raise ValueError("已领取消息必须且只能处于已发布或已重排之一")
        if self.rescheduled != (self.error is not None):
            raise ValueError("重排结果必须携带错误，成功结果不能携带错误")


@dataclass(frozen=True, kw_only=True)
class DeliverySweepResult:
    """一次数据库补偿扫描及随后 Relay 的结果。"""

    requeued_count: int
    relay_results: tuple[OutboxRelayResult, ...]

    def __post_init__(self) -> None:
        if (
            isinstance(self.requeued_count, bool)
            or not isinstance(self.requeued_count, int)
            or self.requeued_count < 0
        ):
            raise ValueError("requeued_count 必须为非负整数")
        if any(not isinstance(item, OutboxRelayResult) for item in self.relay_results):
            raise ValueError("relay_results 类型不正确")


class OutboxRepository(Protocol):
    """Outbox Relay 所需的最小持久化操作。"""

    async def claim(
        self,
        *,
        lease_owner: str,
        lease_token: str,
        lease_seconds: int = 60,
    ) -> OutboxLease | None: ...

    async def mark_published(
        self,
        *,
        message_id: str,
        lease_token: str,
        stream_name: str,
        stream_entry_id: str,
    ) -> bool: ...

    async def reschedule(
        self,
        *,
        message_id: str,
        lease_token: str,
        error: str,
        retry_delay_seconds: int,
    ) -> bool: ...

    async def has_unpublished(self, *, task_id: str) -> bool: ...

    async def requeue_stale_deliveries(
        self,
        *,
        message_types: tuple[str, ...],
        min_age_seconds: int,
        limit: int,
    ) -> int: ...


class WakeupTransport(Protocol):
    """Outbox Relay 唯一允许调用的短期唤醒传输。"""

    async def publish(self, lease: OutboxLease) -> WakeupTransportReceipt: ...


class OutboxRelayLeaseLostError(RuntimeError):
    """Redis 已发布但数据库租约无法确认，后续允许产生重复唤醒。"""


class OutboxRelay:
    """先写 Redis Stream，收到回执后才把 Outbox 标记为 published。"""

    def __init__(
        self,
        *,
        repository: OutboxRepository,
        transport: WakeupTransport,
        lease_owner: str,
        lease_seconds: int = 60,
        retry_base_seconds: int = 2,
        retry_cap_seconds: int = 300,
    ) -> None:
        self._repository = repository
        self._transport = transport
        self._lease_owner = _require_text(lease_owner, "lease_owner")
        if lease_seconds <= 0:
            raise ValueError("lease_seconds 必须大于 0")
        if retry_base_seconds <= 0:
            raise ValueError("retry_base_seconds 必须大于 0")
        if retry_cap_seconds < retry_base_seconds:
            raise ValueError("retry_cap_seconds 不能小于 retry_base_seconds")
        self._lease_seconds = lease_seconds
        self._retry_base_seconds = retry_base_seconds
        self._retry_cap_seconds = retry_cap_seconds

    async def publish_one(self) -> OutboxRelayResult:
        lease = await self._repository.claim(
            lease_owner=self._lease_owner,
            lease_token=str(uuid4()),
            lease_seconds=self._lease_seconds,
        )
        if lease is None:
            return OutboxRelayResult(claimed=False)
        message_id = lease.envelope.message_id
        try:
            receipt = await self._transport.publish(lease)
        except Exception as exc:  # noqa: BLE001 - 传输失败必须回写数据库重试态
            error = _bounded_error(exc)
            delay = _retry_delay_seconds(
                attempt_count=lease.attempt_count,
                base=self._retry_base_seconds,
                cap=self._retry_cap_seconds,
            )
            rescheduled = await self._repository.reschedule(
                message_id=message_id,
                lease_token=lease.lease_token,
                error=error,
                retry_delay_seconds=delay,
            )
            if not rescheduled:
                raise OutboxRelayLeaseLostError(
                    "Outbox 传输失败后租约已丢失，无法安全重排"
                ) from exc
            return OutboxRelayResult(
                claimed=True,
                message_id=message_id,
                attempt_count=lease.attempt_count,
                rescheduled=True,
                error=error,
            )
        marked = await self._repository.mark_published(
            message_id=message_id,
            lease_token=lease.lease_token,
            stream_name=receipt.stream_name,
            stream_entry_id=receipt.stream_entry_id,
        )
        if not marked:
            raise OutboxRelayLeaseLostError(
                "Redis Stream 已接收唤醒，但 Outbox 租约确认失败；后续重复投递是预期行为"
            )
        app_surface_kind = _app_exploration_surface_kind(lease)
        if app_surface_kind is not None:
            observe_app_metric_safely(
                APP_OUTBOX_LAG_SECONDS,
                max(0.0, (datetime.now(UTC) - lease.envelope.occurred_at).total_seconds()),
                surface_kind=app_surface_kind,
            )
        return OutboxRelayResult(
            claimed=True,
            message_id=message_id,
            attempt_count=lease.attempt_count,
            receipt=receipt,
        )

    async def publish_batch(self, *, limit: int = 100) -> tuple[OutboxRelayResult, ...]:
        if limit <= 0:
            raise ValueError("limit 必须大于 0")
        results: list[OutboxRelayResult] = []
        for _ in range(limit):
            result = await self.publish_one()
            if not result.claimed:
                break
            results.append(result)
        return tuple(results)


class DeliverySweeper:
    """从 PostgreSQL 恢复唤醒信号；Redis 内容不是待办事实。"""

    def __init__(
        self,
        *,
        repository: OutboxRepository,
        relay: OutboxRelay,
        message_types: tuple[str, ...],
        min_age_seconds: int = 60,
    ) -> None:
        self._repository = repository
        self._relay = relay
        normalized = tuple(_require_text(item, "message_types") for item in message_types)
        if not normalized or len(normalized) != len(set(normalized)):
            raise ValueError("message_types 必须为非空且不重复")
        if min_age_seconds <= 0:
            raise ValueError("min_age_seconds 必须大于 0")
        self._message_types = normalized
        self._min_age_seconds = min_age_seconds

    async def sweep_once(self, *, limit: int = 100) -> DeliverySweepResult:
        if limit <= 0:
            raise ValueError("limit 必须大于 0")
        requeued = await self._repository.requeue_stale_deliveries(
            message_types=self._message_types,
            min_age_seconds=self._min_age_seconds,
            limit=limit,
        )
        relayed = await self._relay.publish_batch(limit=limit)
        return DeliverySweepResult(
            requeued_count=requeued,
            relay_results=relayed,
        )


def _retry_delay_seconds(*, attempt_count: int, base: int, cap: int) -> int:
    exponent = min(max(attempt_count - 1, 0), 20)
    return min(cap, base * (2**exponent))


def _bounded_error(exc: Exception) -> str:
    value = f"{type(exc).__name__}: {exc}".strip()
    return value[:2000] or type(exc).__name__


def _app_exploration_surface_kind(lease: OutboxLease) -> str | None:
    """返回受控 App Surface 标签；Web/H5 与非探索消息不进入 App 指标。"""

    if not (
        lease.aggregate_type == "explorer_work"
        and lease.envelope.message_type == "alioth.v2.exploration-work.available"
    ):
        return None
    surface_kind = lease.envelope.payload.get("surface_kind")
    return surface_kind if surface_kind in {"android", "ios"} else None


def _require_text(value: str | None, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError(f"{field_name} 必须为无首尾空白的非空字符串")
    return value
