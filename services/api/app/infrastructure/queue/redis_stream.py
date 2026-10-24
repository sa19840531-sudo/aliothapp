"""V2 Redis Stream 唤醒传输；业务待办与租约始终以 PostgreSQL 为准。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from redis.exceptions import ResponseError

from services.api.app.application.outbox import (
    SUPPORTED_WAKEUP_MESSAGE_TYPES,
    OutboxLease,
    WakeupTransportReceipt,
)

DEFAULT_WAKEUP_STREAM_ROUTES = {
    "alioth.v2.task-submission.committed": "task-submission",
    "alioth.v2.exploration-work.available": "exploration",
    "alioth.v2.probe-work.available": "probe",
    "alioth.v2.inspection-work.available": "inspection",
    "alioth.v2.capture.committed": "inspection",
    "alioth.v2.evidence-bundle.ready": "inspection",
    "alioth.v2.evidence-supplement.attached": "inspection",
    "alioth.v2.h5-inspection-publication.available": "h5-inspection-publication",
    "alioth.v2.app-inspection-publication.available": "app-inspection-publication",
    "alioth.v2.aggregate-inspection-work.available": "aggregate-inspection",
}
DEFAULT_WAKEUP_STREAM_NAMES = {
    message_type: f"alioth:v2:wakeup:{suffix}"
    for message_type, suffix in DEFAULT_WAKEUP_STREAM_ROUTES.items()
}

if set(DEFAULT_WAKEUP_STREAM_ROUTES) != set(SUPPORTED_WAKEUP_MESSAGE_TYPES):
    raise RuntimeError("Outbox 支持的唤醒消息与 Redis Stream 路由不一致")


class WakeupRoutingError(ValueError):
    """消息类型没有冻结的 Stream 路由，禁止静默投到错误消费者。"""


@dataclass(frozen=True, kw_only=True)
class RedisStreamWakeup:
    """Consumer Group 读取的一条短期唤醒信号。"""

    stream_name: str
    stream_entry_id: str
    message_id: str
    message_type: str
    task_id: str
    aggregate_type: str
    aggregate_id: str
    aggregate_version: int
    idempotency_key: str

    def __post_init__(self) -> None:
        for field_name in (
            "stream_name",
            "stream_entry_id",
            "message_id",
            "message_type",
            "task_id",
            "aggregate_type",
            "aggregate_id",
            "idempotency_key",
        ):
            _require_text(getattr(self, field_name), field_name)
        if (
            isinstance(self.aggregate_version, bool)
            or not isinstance(self.aggregate_version, int)
            or self.aggregate_version < 0
        ):
            raise ValueError("aggregate_version 必须为非负整数")


@dataclass(frozen=True, kw_only=True)
class RedisPendingClaimBatch:
    """XAUTOCLAIM 的分页游标与重新领取结果。"""

    stream_name: str
    next_start_id: str
    wakeups: tuple[RedisStreamWakeup, ...]

    def __post_init__(self) -> None:
        _require_text(self.stream_name, "stream_name")
        _require_text(self.next_start_id, "next_start_id")
        if any(not isinstance(item, RedisStreamWakeup) for item in self.wakeups):
            raise ValueError("wakeups 类型不正确")


class DatabaseWakeupHandler(Protocol):
    """必须通过 PostgreSQL 领取或幂等确认工作，不能直接信任 Redis 字段。"""

    async def handle(self, wakeup: RedisStreamWakeup) -> bool: ...


@dataclass(frozen=True, kw_only=True)
class WakeupDispatchResult:
    """一次唤醒处理是否已获得数据库确认并完成 ACK。"""

    wakeup: RedisStreamWakeup
    database_confirmed: bool
    acknowledged: bool

    def __post_init__(self) -> None:
        if not isinstance(self.wakeup, RedisStreamWakeup):
            raise ValueError("wakeup 类型不正确")
        if not isinstance(self.database_confirmed, bool) or not isinstance(
            self.acknowledged,
            bool,
        ):
            raise ValueError("database_confirmed/acknowledged 必须为布尔值")
        if self.acknowledged and not self.database_confirmed:
            raise ValueError("数据库未确认时禁止 ACK")


class RedisStreamWakeupTransport:
    """把 Outbox 信封写入按工作类型分片的 Redis Stream。"""

    def __init__(
        self,
        client: Any,
        *,
        stream_prefix: str = "alioth:v2:wakeup",
        maxlen: int = 100_000,
        routes: Mapping[str, str] | None = None,
    ) -> None:
        self._client = client
        self._stream_prefix = _require_text(stream_prefix, "stream_prefix")
        if maxlen <= 0:
            raise ValueError("maxlen 必须大于 0")
        self._maxlen = maxlen
        source = dict(routes or DEFAULT_WAKEUP_STREAM_ROUTES)
        if not source:
            raise ValueError("routes 不能为空")
        self._routes = {
            _require_text(message_type, "message_type"): _require_text(suffix, "stream_suffix")
            for message_type, suffix in source.items()
        }

    @property
    def stream_names(self) -> tuple[str, ...]:
        return tuple(
            sorted({f"{self._stream_prefix}:{suffix}" for suffix in self._routes.values()})
        )

    def route(self, message_type: str) -> str:
        suffix = self._routes.get(message_type)
        if suffix is None:
            raise WakeupRoutingError(f"消息类型 {message_type} 没有 Redis Stream 路由")
        return f"{self._stream_prefix}:{suffix}"

    async def publish(self, lease: OutboxLease) -> WakeupTransportReceipt:
        if not isinstance(lease, OutboxLease):
            raise TypeError("lease 必须为 OutboxLease")
        stream_name = self.route(lease.envelope.message_type)
        entry_id = await self._client.xadd(
            stream_name,
            {
                "message_id": lease.envelope.message_id,
                "message_type": lease.envelope.message_type,
                "task_id": lease.envelope.task_id,
                "aggregate_type": lease.aggregate_type,
                "aggregate_id": lease.aggregate_id,
                "aggregate_version": str(lease.aggregate_version),
                "idempotency_key": lease.envelope.idempotency_key,
            },
            maxlen=self._maxlen,
            approximate=True,
        )
        return WakeupTransportReceipt(
            stream_name=stream_name,
            stream_entry_id=_decode_text(entry_id, "stream_entry_id"),
        )


class RedisStreamWakeupConsumer:
    """读取和 ACK 唤醒；调用方必须先完成数据库领取或幂等确认。"""

    def __init__(
        self,
        client: Any,
        *,
        group_name: str,
        consumer_name: str,
        stream_names: Sequence[str],
        message_stream_routes: Mapping[str, str] | None = None,
    ) -> None:
        self._client = client
        self._group_name = _require_text(group_name, "group_name")
        self._consumer_name = _require_text(consumer_name, "consumer_name")
        names = tuple(_require_text(item, "stream_names") for item in stream_names)
        if not names or len(names) != len(set(names)):
            raise ValueError("stream_names 必须为非空且不重复")
        self._stream_names = names
        source_routes = dict(message_stream_routes or DEFAULT_WAKEUP_STREAM_NAMES)
        self._message_stream_routes = {
            _require_text(message_type, "message_type"): _require_text(
                stream_name,
                "stream_name",
            )
            for message_type, stream_name in source_routes.items()
        }

    async def ensure_groups(self) -> None:
        for stream_name in self._stream_names:
            try:
                await self._client.xgroup_create(
                    stream_name,
                    self._group_name,
                    id="0-0",
                    mkstream=True,
                )
            except ResponseError as exc:
                if "BUSYGROUP" not in str(exc):
                    raise

    async def receive(
        self,
        *,
        count: int = 10,
        block_milliseconds: int = 1000,
    ) -> tuple[RedisStreamWakeup, ...]:
        if count <= 0 or block_milliseconds < 0:
            raise ValueError("count 必须大于 0，block_milliseconds 不能小于 0")
        response = await self._client.xreadgroup(
            self._group_name,
            self._consumer_name,
            streams={stream_name: ">" for stream_name in self._stream_names},
            count=count,
            block=block_milliseconds,
        )
        return _parse_stream_response(
            response,
            message_stream_routes=self._message_stream_routes,
        )

    async def claim_stale(
        self,
        *,
        stream_name: str,
        min_idle_milliseconds: int,
        start_id: str = "0-0",
        count: int = 10,
    ) -> RedisPendingClaimBatch:
        """重新领取崩溃 Consumer 留下的 Pending Entry，不创建新的业务工作。"""
        if stream_name not in self._stream_names:
            raise ValueError("stream_name 不属于当前 Consumer")
        if min_idle_milliseconds < 0 or count <= 0:
            raise ValueError("min_idle_milliseconds 不能小于 0，count 必须大于 0")
        start = _require_text(start_id, "start_id")
        response = await self._client.xautoclaim(
            stream_name,
            self._group_name,
            self._consumer_name,
            min_idle_milliseconds,
            start,
            count=count,
        )
        if not isinstance(response, Sequence) or len(response) < 2:
            raise ValueError("Redis XAUTOCLAIM 返回格式不受支持")
        next_start_id = _decode_text(response[0], "next_start_id")
        wakeups = _parse_stream_response(
            [(stream_name, response[1])],
            message_stream_routes=self._message_stream_routes,
        )
        return RedisPendingClaimBatch(
            stream_name=stream_name,
            next_start_id=next_start_id,
            wakeups=wakeups,
        )

    async def ack(self, wakeup: RedisStreamWakeup) -> bool:
        if not isinstance(wakeup, RedisStreamWakeup):
            raise TypeError("wakeup 必须为 RedisStreamWakeup")
        if wakeup.stream_name not in self._stream_names:
            raise ValueError("唤醒消息不属于当前 Consumer 的 Stream")
        acknowledged = await self._client.xack(
            wakeup.stream_name,
            self._group_name,
            wakeup.stream_entry_id,
        )
        return int(acknowledged) == 1

    async def pending_count(self, *, stream_name: str) -> int:
        """读取 Consumer Group pending 数量，供现有 worker 结构化日志观察积压。"""
        if stream_name not in self._stream_names:
            raise ValueError("stream_name 不属于当前 Consumer")
        summary = await self._client.xpending(stream_name, self._group_name)
        if isinstance(summary, Mapping):
            raw_count = summary.get("pending", summary.get(b"pending", 0))
        elif isinstance(summary, Sequence) and summary:
            raw_count = summary[0]
        else:
            raise ValueError("Redis XPENDING 返回格式不受支持")
        count = int(raw_count)
        if count < 0:
            raise ValueError("Redis XPENDING pending 数量不能为负数")
        return count


class RedisWakeupDispatcher:
    """先调用数据库权威处理器，只有明确确认后才 ACK Redis Entry。"""

    def __init__(
        self,
        *,
        consumer: RedisStreamWakeupConsumer,
        handler: DatabaseWakeupHandler,
    ) -> None:
        self._consumer = consumer
        self._handler = handler

    async def dispatch(self, wakeup: RedisStreamWakeup) -> WakeupDispatchResult:
        confirmed = await self._handler.handle(wakeup)
        if not isinstance(confirmed, bool):
            raise TypeError("DatabaseWakeupHandler 必须返回布尔值")
        acknowledged = await self._consumer.ack(wakeup) if confirmed else False
        return WakeupDispatchResult(
            wakeup=wakeup,
            database_confirmed=confirmed,
            acknowledged=acknowledged,
        )

    async def receive_and_dispatch(
        self,
        *,
        count: int = 10,
        block_milliseconds: int = 1000,
    ) -> tuple[WakeupDispatchResult, ...]:
        wakeups = await self._consumer.receive(
            count=count,
            block_milliseconds=block_milliseconds,
        )
        results: list[WakeupDispatchResult] = []
        for wakeup in wakeups:
            results.append(await self.dispatch(wakeup))
        return tuple(results)


def _parse_stream_response(
    response: Any,
    *,
    message_stream_routes: Mapping[str, str],
) -> tuple[RedisStreamWakeup, ...]:
    if not response:
        return ()
    wakeups: list[RedisStreamWakeup] = []
    for raw_stream_name, entries in response:
        stream_name = _decode_text(raw_stream_name, "stream_name")
        for raw_entry_id, raw_fields in entries:
            fields = {
                _decode_text(key, "field_name"): _decode_text(value, "field_value")
                for key, value in raw_fields.items()
            }
            message_type = _require_text(fields.get("message_type"), "message_type")
            expected_stream = message_stream_routes.get(message_type)
            if expected_stream is None or expected_stream != stream_name:
                raise WakeupRoutingError("Redis 唤醒消息位于错误的 Stream")
            message_id = _require_text(fields.get("message_id"), "message_id")
            wakeups.append(
                RedisStreamWakeup(
                    stream_name=stream_name,
                    stream_entry_id=_decode_text(raw_entry_id, "stream_entry_id"),
                    message_id=message_id,
                    message_type=message_type,
                    task_id=_require_text(fields.get("task_id"), "task_id"),
                    aggregate_type=_require_text(
                        fields.get("aggregate_type"),
                        "aggregate_type",
                    ),
                    aggregate_id=_require_text(fields.get("aggregate_id"), "aggregate_id"),
                    aggregate_version=_parse_aggregate_version(
                        fields.get("aggregate_version"),
                        message_id=message_id,
                    ),
                    idempotency_key=_require_text(
                        fields.get("idempotency_key"),
                        "idempotency_key",
                    ),
                )
            )
    return tuple(wakeups)


def _decode_text(value: Any, field_name: str) -> str:
    if isinstance(value, bytes):
        try:
            value = value.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError(f"{field_name} 不是 UTF-8") from exc
    return _require_text(value, field_name)


def _parse_non_negative_int(value: Any, field_name: str) -> int:
    text = _decode_text(value, field_name)
    if not text.isdecimal():
        raise ValueError(f"{field_name} 必须为非负整数字符串")
    return int(text)


def _parse_aggregate_version(value: Any, *, message_id: str) -> int:
    if value is None and message_id.startswith("message:"):
        # 升级前 V1 Stream Entry 没有该字段；0035 已将对应 Outbox 版本回填为 0。
        return 0
    return _parse_non_negative_int(value, "aggregate_version")


def _require_text(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError(f"{field_name} 必须为无首尾空白的非空字符串")
    return value
