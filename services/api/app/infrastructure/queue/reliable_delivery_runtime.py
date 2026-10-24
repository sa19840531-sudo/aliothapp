"""V2 Outbox Relay 与 Sweeper 的生产组合入口。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from services.api.app.application.outbox import (
    SUPPORTED_WAKEUP_MESSAGE_TYPES,
    DeliverySweeper,
    DeliverySweepResult,
    OutboxRelay,
    OutboxRelayResult,
)
from services.api.app.core.config import get_settings
from services.api.app.infrastructure.db.outbox_repository import (
    PostgresOutboxRepository,
)
from services.api.app.infrastructure.queue.redis_queue import build_redis_client
from services.api.app.infrastructure.queue.redis_stream import (
    RedisStreamWakeupTransport,
)


@dataclass(kw_only=True)
class ReliableDeliveryRuntime:
    """可由现有 Worker 或独立进程周期驱动的可靠派发运行时。"""

    relay: OutboxRelay
    sweeper: DeliverySweeper
    redis_client: Any
    owns_redis_client: bool

    async def relay_once(self, *, limit: int = 100) -> tuple[OutboxRelayResult, ...]:
        return await self.relay.publish_batch(limit=limit)

    async def sweep_once(self, *, limit: int = 100) -> DeliverySweepResult:
        return await self.sweeper.sweep_once(limit=limit)

    async def close(self) -> None:
        if self.owns_redis_client:
            await self.redis_client.aclose()


def build_default_reliable_delivery_runtime(
    *,
    redis_client: Any | None = None,
    lease_owner: str | None = None,
    stream_prefix: str = "alioth:v2:wakeup",
    stream_maxlen: int = 100_000,
    sweep_min_age_seconds: int = 60,
) -> ReliableDeliveryRuntime:
    """按平台配置组合 PostgreSQL 事实源与 Redis Stream 唤醒层。"""
    client = redis_client
    owns_client = client is None
    if client is None:
        client = build_redis_client(get_settings().queue)
    repository = PostgresOutboxRepository()
    transport = RedisStreamWakeupTransport(
        client,
        stream_prefix=stream_prefix,
        maxlen=stream_maxlen,
    )
    relay = OutboxRelay(
        repository=repository,
        transport=transport,
        lease_owner=lease_owner or f"v2-outbox-relay:{uuid4()}",
    )
    sweeper = DeliverySweeper(
        repository=repository,
        relay=relay,
        message_types=SUPPORTED_WAKEUP_MESSAGE_TYPES,
        min_age_seconds=sweep_min_age_seconds,
    )
    return ReliableDeliveryRuntime(
        relay=relay,
        sweeper=sweeper,
        redis_client=client,
        owns_redis_client=owns_client,
    )
