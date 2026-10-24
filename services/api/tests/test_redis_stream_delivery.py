"""Redis Stream 唤醒路由、Consumer Group 与 Redis 清空恢复测试。"""

from datetime import UTC, datetime

import fakeredis
import pytest

from services.api.app.application.outbox import (
    DeliverySweeper,
    OutboxLease,
    OutboxRelay,
)
from services.api.app.domain.protocol import build_protocol_envelope
from services.api.app.infrastructure.queue.redis_stream import (
    RedisStreamWakeupConsumer,
    RedisStreamWakeupTransport,
    RedisWakeupDispatcher,
    WakeupRoutingError,
)


class RecoveryRepository:
    def __init__(self, lease: OutboxLease) -> None:
        self.lease = lease
        self.available = False
        self.marked = []

    async def claim(self, **kwargs):
        del kwargs
        if not self.available:
            return None
        self.available = False
        return self.lease

    async def mark_published(self, **kwargs):
        self.marked.append(kwargs)
        return True

    async def reschedule(self, **kwargs):
        del kwargs
        return True

    async def has_unpublished(self, *, task_id):
        del task_id
        return self.available

    async def requeue_stale_deliveries(self, **kwargs):
        del kwargs
        self.available = True
        return 1


class StaticDatabaseHandler:
    def __init__(self, *, confirmed: bool) -> None:
        self.confirmed = confirmed
        self.calls = []

    async def handle(self, wakeup):
        self.calls.append(wakeup)
        return self.confirmed


def _lease(*, message_type="alioth.v2.evidence-bundle.ready") -> OutboxLease:
    envelope = build_protocol_envelope(
        message_type=message_type,
        payload_protocol_version="evidence-bundle-ready.v1",
        idempotency_key="manifest-1",
        task_id="task-1",
        aggregate_type="evidence_manifest",
        aggregate_id="manifest-1",
        aggregate_version=1,
        occurred_at=datetime(2026, 8, 3, 12, 0, tzinfo=UTC),
        payload={"manifest_id": "manifest-1"},
    )
    return OutboxLease(
        aggregate_type="evidence_manifest",
        aggregate_id="manifest-1",
        aggregate_version=1,
        envelope=envelope,
        lease_token="lease-token-1",
        attempt_count=1,
    )


async def test_stream_wakeup_round_trip_preserves_aggregate_version() -> None:
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    transport = RedisStreamWakeupTransport(client)
    receipt = await transport.publish(_lease())
    consumer = RedisStreamWakeupConsumer(
        client,
        group_name="inspection-workers",
        consumer_name="worker-1",
        stream_names=(receipt.stream_name,),
    )
    await consumer.ensure_groups()

    wakeups = await consumer.receive(block_milliseconds=0)

    assert len(wakeups) == 1
    assert wakeups[0].message_id == _lease().envelope.message_id
    assert wakeups[0].message_type == "alioth.v2.evidence-bundle.ready"
    assert wakeups[0].aggregate_id == "manifest-1"
    assert wakeups[0].aggregate_version == 1
    assert await consumer.ack(wakeups[0]) is True


async def test_legacy_stream_wakeup_without_aggregate_version_defaults_to_zero() -> None:
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    stream_name = "alioth:v2:wakeup:inspection"
    await client.xadd(
        stream_name,
        {
            "message_id": "message:legacy-1",
            "message_type": "alioth.v2.evidence-bundle.ready",
            "task_id": "task-1",
            "aggregate_type": "evidence_manifest",
            "aggregate_id": "manifest-1",
            "idempotency_key": "manifest-1",
        },
    )
    consumer = RedisStreamWakeupConsumer(
        client,
        group_name="inspection-workers",
        consumer_name="worker-1",
        stream_names=(stream_name,),
    )
    await consumer.ensure_groups()

    wakeups = await consumer.receive(block_milliseconds=0)

    assert wakeups[0].aggregate_version == 0


async def test_v2_stream_wakeup_without_aggregate_version_is_rejected() -> None:
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    stream_name = "alioth:v2:wakeup:inspection"
    await client.xadd(
        stream_name,
        {
            "message_id": "event:v2-missing-version",
            "message_type": "alioth.v2.evidence-bundle.ready",
            "task_id": "task-1",
            "aggregate_type": "evidence_manifest",
            "aggregate_id": "manifest-1",
            "idempotency_key": "manifest-1",
        },
    )
    consumer = RedisStreamWakeupConsumer(
        client,
        group_name="inspection-workers",
        consumer_name="worker-1",
        stream_names=(stream_name,),
    )
    await consumer.ensure_groups()

    with pytest.raises(ValueError, match="aggregate_version"):
        await consumer.receive(block_milliseconds=0)


async def test_unknown_message_type_is_rejected_instead_of_misrouted() -> None:
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    transport = RedisStreamWakeupTransport(client)

    with pytest.raises(WakeupRoutingError):
        await transport.publish(_lease(message_type="alioth.v2.unknown"))


async def test_pending_wakeup_can_be_claimed_after_consumer_crash() -> None:
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    transport = RedisStreamWakeupTransport(client)
    receipt = await transport.publish(_lease())
    crashed = RedisStreamWakeupConsumer(
        client,
        group_name="inspection-workers",
        consumer_name="worker-crashed",
        stream_names=(receipt.stream_name,),
    )
    await crashed.ensure_groups()
    assert len(await crashed.receive(block_milliseconds=0)) == 1
    recovered = RedisStreamWakeupConsumer(
        client,
        group_name="inspection-workers",
        consumer_name="worker-recovered",
        stream_names=(receipt.stream_name,),
    )

    batch = await recovered.claim_stale(
        stream_name=receipt.stream_name,
        min_idle_milliseconds=0,
    )

    assert len(batch.wakeups) == 1
    assert batch.wakeups[0].message_id == _lease().envelope.message_id
    assert await recovered.ack(batch.wakeups[0]) is True


async def test_consumer_reports_pending_count_for_operational_backlog() -> None:
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    transport = RedisStreamWakeupTransport(client)
    receipt = await transport.publish(_lease())
    consumer = RedisStreamWakeupConsumer(
        client,
        group_name="inspection-workers",
        consumer_name="worker-1",
        stream_names=(receipt.stream_name,),
    )
    await consumer.ensure_groups()
    assert len(await consumer.receive(block_milliseconds=0)) == 1

    assert await consumer.pending_count(stream_name=receipt.stream_name) == 1


async def test_dispatcher_does_not_ack_before_database_confirmation() -> None:
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    transport = RedisStreamWakeupTransport(client)
    receipt = await transport.publish(_lease())
    consumer = RedisStreamWakeupConsumer(
        client,
        group_name="inspection-workers",
        consumer_name="worker-1",
        stream_names=(receipt.stream_name,),
    )
    await consumer.ensure_groups()
    dispatcher = RedisWakeupDispatcher(
        consumer=consumer,
        handler=StaticDatabaseHandler(confirmed=False),
    )

    results = await dispatcher.receive_and_dispatch(block_milliseconds=0)

    assert len(results) == 1
    assert results[0].database_confirmed is False
    assert results[0].acknowledged is False
    pending = await client.xpending(receipt.stream_name, "inspection-workers")
    assert pending["pending"] == 1


async def test_redis_flush_is_recovered_from_database_sweeper() -> None:
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    transport = RedisStreamWakeupTransport(client)
    lease = _lease()
    await transport.publish(lease)
    await client.flushall()
    repository = RecoveryRepository(lease)
    relay = OutboxRelay(
        repository=repository,
        transport=transport,
        lease_owner="relay-1",
    )
    sweeper = DeliverySweeper(
        repository=repository,
        relay=relay,
        message_types=(lease.envelope.message_type,),
        min_age_seconds=60,
    )

    result = await sweeper.sweep_once()

    assert result.requeued_count == 1
    assert len(result.relay_results) == 1
    stream_name = result.relay_results[0].receipt.stream_name
    assert await client.xlen(stream_name) == 1
    assert repository.marked[0]["stream_entry_id"]
