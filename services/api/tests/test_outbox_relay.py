"""Outbox Relay 的回执顺序、指数重试与 Sweeper 恢复测试。"""

from datetime import UTC, datetime

import pytest

from services.api.app.application.app_metrics import (
    APP_OUTBOX_LAG_SECONDS,
    app_metric_observation,
)
from services.api.app.application.outbox import (
    DeliverySweeper,
    OutboxLease,
    OutboxRelay,
    OutboxRelayLeaseLostError,
    WakeupTransportReceipt,
)
from services.api.app.domain.protocol import build_protocol_envelope


class RecordingRepository:
    def __init__(self, leases=()) -> None:
        self.leases = list(leases)
        self.calls = []
        self.mark_result = True
        self.reschedule_result = True
        self.requeued_count = 0

    async def claim(self, **kwargs):
        self.calls.append(("claim", kwargs))
        return self.leases.pop(0) if self.leases else None

    async def mark_published(self, **kwargs):
        self.calls.append(("mark", kwargs))
        return self.mark_result

    async def reschedule(self, **kwargs):
        self.calls.append(("reschedule", kwargs))
        return self.reschedule_result

    async def has_unpublished(self, *, task_id):
        del task_id
        return bool(self.leases)

    async def requeue_stale_deliveries(self, **kwargs):
        self.calls.append(("sweep", kwargs))
        return self.requeued_count


class RecordingTransport:
    def __init__(self, *, error=None) -> None:
        self.error = error
        self.calls = []

    async def publish(self, lease):
        self.calls.append(lease)
        if self.error is not None:
            raise self.error
        return WakeupTransportReceipt(
            stream_name="alioth:v2:wakeup:exploration",
            stream_entry_id="1-0",
        )


def _lease(
    *,
    attempt_count: int = 1,
    surface_kind: str | None = None,
) -> OutboxLease:
    envelope = build_protocol_envelope(
        message_type="alioth.v2.exploration-work.available",
        payload_protocol_version="exploration-work.v1",
        idempotency_key="work-1",
        task_id="task-1",
        aggregate_type="explorer_work",
        aggregate_id="work-1",
        aggregate_version=0,
        occurred_at=datetime(2026, 8, 3, 12, 0, tzinfo=UTC),
        payload={
            "work_id": "work-1",
            **({"surface_kind": surface_kind} if surface_kind is not None else {}),
        },
    )
    return OutboxLease(
        aggregate_type="explorer_work",
        aggregate_id="work-1",
        aggregate_version=0,
        envelope=envelope,
        lease_token="lease-token-1",
        attempt_count=attempt_count,
    )


async def test_relay_marks_published_only_after_stream_receipt() -> None:
    repository = RecordingRepository((_lease(),))
    transport = RecordingTransport()
    relay = OutboxRelay(
        repository=repository,
        transport=transport,
        lease_owner="relay-1",
    )

    result = await relay.publish_one()

    assert result.receipt is not None
    assert [call[0] for call in repository.calls] == ["claim", "mark"]
    assert len(transport.calls) == 1
    mark = repository.calls[1][1]
    assert mark["stream_name"] == "alioth:v2:wakeup:exploration"
    assert mark["stream_entry_id"] == "1-0"


async def test_relay_observes_only_app_exploration_outbox_lag() -> None:
    app_before = app_metric_observation(APP_OUTBOX_LAG_SECONDS)
    repository = RecordingRepository(
        (
            _lease(surface_kind="android"),
            _lease(surface_kind="web"),
        )
    )
    relay = OutboxRelay(
        repository=repository,
        transport=RecordingTransport(),
        lease_owner="relay-1",
    )

    await relay.publish_one()
    after_app = app_metric_observation(APP_OUTBOX_LAG_SECONDS)
    await relay.publish_one()
    after_web = app_metric_observation(APP_OUTBOX_LAG_SECONDS)

    assert after_app.count == app_before.count + 1
    assert after_app.latest >= 0
    assert after_web == after_app


async def test_relay_reschedules_transport_failure_with_bounded_exponential_delay() -> None:
    repository = RecordingRepository((_lease(attempt_count=4),))
    relay = OutboxRelay(
        repository=repository,
        transport=RecordingTransport(error=ConnectionError("redis down")),
        lease_owner="relay-1",
        retry_base_seconds=2,
        retry_cap_seconds=10,
    )

    result = await relay.publish_one()

    assert result.rescheduled is True
    reschedule = repository.calls[1][1]
    assert reschedule["retry_delay_seconds"] == 10
    assert reschedule["error"] == "ConnectionError: redis down"
    assert all(call[0] != "mark" for call in repository.calls)


async def test_relay_does_not_claim_success_when_database_confirmation_is_lost() -> None:
    repository = RecordingRepository((_lease(),))
    repository.mark_result = False
    relay = OutboxRelay(
        repository=repository,
        transport=RecordingTransport(),
        lease_owner="relay-1",
    )

    with pytest.raises(OutboxRelayLeaseLostError, match="重复投递"):
        await relay.publish_one()


async def test_sweeper_requeues_database_facts_then_runs_relay() -> None:
    repository = RecordingRepository((_lease(),))
    repository.requeued_count = 3
    relay = OutboxRelay(
        repository=repository,
        transport=RecordingTransport(),
        lease_owner="relay-1",
    )
    sweeper = DeliverySweeper(
        repository=repository,
        relay=relay,
        message_types=("alioth.v2.exploration-work.available",),
        min_age_seconds=60,
    )

    result = await sweeper.sweep_once(limit=10)

    assert result.requeued_count == 3
    assert len(result.relay_results) == 1
    assert [call[0] for call in repository.calls] == ["sweep", "claim", "mark", "claim"]
