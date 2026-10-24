"""Consumer Inbox 仓储与 Redis 唤醒守卫测试。"""

import logging
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from services.api.app.application.event_inbox import (
    ClaimEventInboxCommand,
    CompleteEventInboxCommand,
    EventInboxClaim,
    EventInboxClaimDisposition,
    EventInboxIdentity,
    EventInboxRetryResult,
    RetryEventInboxCommand,
)
from services.api.app.infrastructure.db.event_inbox_repository import (
    PostgresEventInboxRepository,
)
from services.api.app.infrastructure.queue.inbox_wakeup import InboxGuardedWakeupHandler
from services.api.app.infrastructure.queue.redis_stream import (
    RedisStreamWakeup,
    RedisWakeupDispatcher,
)

_TASK_ID = "00000000-0000-0000-0000-000000000001"
_LEASE_TOKEN = "00000000-0000-0000-0000-000000000002"
_MIGRATION_PATH = (
    Path(__file__).parents[1]
    / "migrations"
    / "versions"
    / "20260805_0036_event_consumer_inbox.py"
)


class _Context:
    def __init__(self, value: Any) -> None:
        self.value = value

    async def __aenter__(self) -> Any:
        return self.value

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        return None


class _Connection:
    def __init__(
        self,
        *,
        fetchrow_results: list[Any] | None = None,
        fetchval_result: Any = None,
    ) -> None:
        self.fetchrow_results = list(fetchrow_results or [])
        self.fetchval_result = fetchval_result
        self.calls: list[tuple[str, str, tuple[Any, ...]]] = []
        self.transaction_entered = False

    def transaction(self) -> _Context:
        self.transaction_entered = True
        return _Context(None)

    async def fetchrow(self, query: str, *args: Any) -> Any:
        self.calls.append(("fetchrow", " ".join(query.split()), args))
        return self.fetchrow_results.pop(0) if self.fetchrow_results else None

    async def fetchval(self, query: str, *args: Any) -> Any:
        self.calls.append(("fetchval", " ".join(query.split()), args))
        return self.fetchval_result

    async def execute(self, query: str, *args: Any) -> str:
        self.calls.append(("execute", " ".join(query.split()), args))
        return "INSERT 0 1"


class _Pool:
    def __init__(self, connection: _Connection) -> None:
        self.connection = connection

    def acquire(self) -> _Context:
        return _Context(self.connection)


def _identity() -> EventInboxIdentity:
    return EventInboxIdentity(
        message_id="event:1",
        message_type="alioth.v2.inspection-work.available",
        task_id=_TASK_ID,
        aggregate_type="inspection_work_item",
        aggregate_id="work-1",
        aggregate_version=0,
        idempotency_key="work-1:create",
    )


def _claim_command() -> ClaimEventInboxCommand:
    return ClaimEventInboxCommand(
        consumer_name="alioth-v2-inspection-consumer",
        lease_owner="worker-1:inspection-inbox",
        lease_token=_LEASE_TOKEN,
        lease_seconds=180,
        max_attempts=3,
        identity=_identity(),
    )


async def test_inbox_claim_returns_completed_for_duplicate_event() -> None:
    connection = _Connection(
        fetchrow_results=[
            {"message_id": "event:1"},
            {
                "status": "completed",
                "row_version": 2,
                "attempt_count": 1,
                "max_attempts": 3,
                "lease_active": False,
                "retry_due": True,
            },
        ]
    )
    repository = PostgresEventInboxRepository(pool=_Pool(connection))

    result = await repository.claim(_claim_command())

    assert result.disposition is EventInboxClaimDisposition.COMPLETED
    assert result.row_version == 2
    assert connection.transaction_entered is True
    queries = [query for _kind, query, _args in connection.calls]
    assert "FROM qc_outbox_event" in queries[0]
    assert "aggregate_version = $6" in queries[0]
    assert "ON CONFLICT (consumer_name, message_id) DO NOTHING" in queries[1]
    assert "FOR UPDATE" in queries[2]
    assert not any(query.startswith("UPDATE qc_event_inbox") for query in queries)


async def test_inbox_returns_terminal_rejection_for_identity_not_backed_by_outbox() -> None:
    connection = _Connection(fetchrow_results=[None])
    repository = PostgresEventInboxRepository(pool=_Pool(connection))

    result = await repository.claim(_claim_command())

    assert result.disposition.value == "rejected"
    assert result.message_id == "event:1"
    assert result.row_version == 0
    assert result.attempt_count == 0
    assert len(connection.calls) == 1


async def test_inbox_claim_leases_pending_event_and_increments_version() -> None:
    connection = _Connection(
        fetchrow_results=[
            {"message_id": "event:1"},
            {
                "status": "pending",
                "row_version": 0,
                "attempt_count": 0,
                "max_attempts": 3,
                "lease_active": False,
                "retry_due": True,
            },
            {
                "status": "processing",
                "row_version": 1,
                "attempt_count": 1,
                "lease_token": _LEASE_TOKEN,
            },
        ]
    )
    repository = PostgresEventInboxRepository(pool=_Pool(connection))

    result = await repository.claim(_claim_command())

    assert result.disposition is EventInboxClaimDisposition.CLAIMED
    assert result.row_version == 1
    assert result.attempt_count == 1
    claim_query = connection.calls[-1][1]
    assert "attempt_count = attempt_count + 1" in claim_query
    assert "row_version = row_version + 1" in claim_query
    assert "lease_token = $5::uuid" in claim_query


async def test_inbox_rejects_stale_lease_completion() -> None:
    connection = _Connection(fetchval_result=None)
    repository = PostgresEventInboxRepository(pool=_Pool(connection))

    completed = await repository.complete(
        CompleteEventInboxCommand(
            consumer_name="alioth-v2-inspection-consumer",
            message_id="event:1",
            lease_token=_LEASE_TOKEN,
            expected_row_version=1,
        )
    )

    assert completed is False
    query = connection.calls[0][1]
    assert "status = 'processing'" in query
    assert "lease_expires_at > now()" in query
    assert "row_version = $4" in query


async def test_inbox_retry_releases_lease_and_increments_version() -> None:
    connection = _Connection(
        fetchrow_results=[{"status": "retry_wait", "row_version": 2}]
    )
    repository = PostgresEventInboxRepository(pool=_Pool(connection))

    result = await repository.retry(
        RetryEventInboxCommand(
            consumer_name="alioth-v2-inspection-consumer",
            message_id="event:1",
            lease_token=_LEASE_TOKEN,
            expected_row_version=1,
            error="temporary_database_error",
            retry_delay_seconds=2,
        )
    )

    assert result == EventInboxRetryResult(status="retry_wait", row_version=2)
    query = connection.calls[0][1]
    assert "lease_token = NULL" in query
    assert "attempt_count >= max_attempts" in query
    assert "row_version = row_version + 1" in query


class _InboxRepository:
    def __init__(
        self,
        *,
        disposition: EventInboxClaimDisposition,
        complete_result: bool = True,
    ) -> None:
        self.disposition = disposition
        self.complete_result = complete_result
        self.claims: list[ClaimEventInboxCommand] = []
        self.completions: list[CompleteEventInboxCommand] = []
        self.retries: list[RetryEventInboxCommand] = []

    async def claim(self, command: ClaimEventInboxCommand) -> EventInboxClaim:
        self.claims.append(command)
        return EventInboxClaim(
            disposition=self.disposition,
            message_id=command.identity.message_id,
            row_version=1,
            attempt_count=1,
            lease_token=(
                command.lease_token
                if self.disposition is EventInboxClaimDisposition.CLAIMED
                else None
            ),
        )

    async def complete(self, command: CompleteEventInboxCommand) -> bool:
        self.completions.append(command)
        return self.complete_result

    async def retry(
        self,
        command: RetryEventInboxCommand,
    ) -> EventInboxRetryResult:
        self.retries.append(command)
        return EventInboxRetryResult(status="retry_wait", row_version=2)


class _Handler:
    def __init__(self, *, confirmed: bool = True, error: Exception | None = None) -> None:
        self.confirmed = confirmed
        self.error = error
        self.calls: list[RedisStreamWakeup] = []

    async def handle(self, wakeup: RedisStreamWakeup) -> bool:
        self.calls.append(wakeup)
        if self.error is not None:
            raise self.error
        return self.confirmed


def _wakeup() -> RedisStreamWakeup:
    return RedisStreamWakeup(
        stream_name="alioth:v2:wakeup:inspection",
        stream_entry_id="1-0",
        message_id="event:1",
        message_type="alioth.v2.inspection-work.available",
        task_id=_TASK_ID,
        aggregate_type="inspection_work_item",
        aggregate_id="work-1",
        aggregate_version=0,
        idempotency_key="work-1:create",
    )


def _guard(
    repository: _InboxRepository,
    handler: _Handler,
    *,
    lease_owner: str = "worker-1:inspection-inbox",
) -> InboxGuardedWakeupHandler:
    return InboxGuardedWakeupHandler(
        repository=repository,
        handler=handler,
        consumer_name="alioth-v2-inspection-consumer",
        lease_owner=lease_owner,
        lease_seconds=180,
        retry_delay_seconds=2,
        max_attempts=3,
        lease_token_factory=lambda: _LEASE_TOKEN,
    )


async def test_idempotent_handler_skips_completed_duplicate() -> None:
    repository = _InboxRepository(disposition=EventInboxClaimDisposition.COMPLETED)
    handler = _Handler()

    confirmed = await _guard(repository, handler).handle(_wakeup())

    assert confirmed is True
    assert handler.calls == []
    assert repository.completions == []


async def test_idempotent_handler_keeps_busy_event_unconfirmed() -> None:
    repository = _InboxRepository(disposition=EventInboxClaimDisposition.BUSY)
    handler = _Handler()

    confirmed = await _guard(repository, handler).handle(_wakeup())

    assert confirmed is False
    assert handler.calls == []
    assert repository.completions == []


async def test_idempotent_handler_does_not_confirm_when_inbox_completion_is_lost() -> None:
    repository = _InboxRepository(
        disposition=EventInboxClaimDisposition.CLAIMED,
        complete_result=False,
    )
    handler = _Handler()

    confirmed = await _guard(repository, handler).handle(_wakeup())

    assert confirmed is False
    assert len(handler.calls) == 1
    assert len(repository.completions) == 1


class _DeduplicatingInboxRepository(_InboxRepository):
    def __init__(self) -> None:
        super().__init__(disposition=EventInboxClaimDisposition.CLAIMED)
        self.completed = False

    async def claim(self, command: ClaimEventInboxCommand) -> EventInboxClaim:
        self.disposition = (
            EventInboxClaimDisposition.COMPLETED
            if self.completed
            else EventInboxClaimDisposition.CLAIMED
        )
        return await super().claim(command)

    async def complete(self, command: CompleteEventInboxCommand) -> bool:
        self.completed = True
        return await super().complete(command)


async def test_duplicate_redis_entries_execute_business_once() -> None:
    repository = _DeduplicatingInboxRepository()
    handler = _Handler()
    guard = _guard(repository, handler)

    assert await guard.handle(_wakeup()) is True
    assert await guard.handle(_wakeup()) is True

    assert len(handler.calls) == 1
    assert len(repository.claims) == 2
    assert len(repository.completions) == 1


async def test_idempotent_handler_retries_business_failure_without_confirmation() -> None:
    repository = _InboxRepository(disposition=EventInboxClaimDisposition.CLAIMED)
    handler = _Handler(error=RuntimeError("database unavailable"))

    with pytest.raises(RuntimeError, match="database unavailable"):
        await _guard(repository, handler).handle(_wakeup())

    assert len(repository.retries) == 1
    assert repository.completions == []


class _BatchConsumer:
    def __init__(self, wakeups: tuple[RedisStreamWakeup, ...]) -> None:
        self.wakeups = wakeups
        self.acknowledged: list[RedisStreamWakeup] = []

    async def receive(
        self,
        *,
        count: int,
        block_milliseconds: int,
    ) -> tuple[RedisStreamWakeup, ...]:
        del count, block_milliseconds
        return self.wakeups

    async def ack(self, wakeup: RedisStreamWakeup) -> bool:
        self.acknowledged.append(wakeup)
        return True


async def test_outbox_orphan_is_audited_and_does_not_block_next_wakeup(
    caplog: pytest.LogCaptureFixture,
) -> None:
    orphan = _wakeup()
    legitimate = replace(
        orphan,
        stream_entry_id="2-0",
        message_id="event:2",
        aggregate_id="work-2",
        idempotency_key="work-2:create",
    )
    connection = _Connection(
        fetchrow_results=[
            None,
            {"message_id": "event:2"},
            {
                "status": "pending",
                "row_version": 0,
                "attempt_count": 0,
                "max_attempts": 3,
                "lease_active": False,
                "retry_due": True,
            },
            {
                "status": "processing",
                "row_version": 1,
                "attempt_count": 1,
                "lease_token": _LEASE_TOKEN,
            },
        ],
        fetchval_result="event:2",
    )
    handler = _Handler()
    consumer = _BatchConsumer((orphan, legitimate))
    dispatcher = RedisWakeupDispatcher(
        consumer=consumer,
        handler=_guard(
            PostgresEventInboxRepository(pool=_Pool(connection)),
            handler,
        ),
    )

    with caplog.at_level(logging.WARNING):
        results = await dispatcher.receive_and_dispatch(block_milliseconds=0)

    assert [(item.database_confirmed, item.acknowledged) for item in results] == [
        (True, True),
        (True, True),
    ]
    assert handler.calls == [legitimate]
    assert consumer.acknowledged == [orphan, legitimate]
    assert "event_inbox_wakeup_rejected" in caplog.text
    assert "reason_code=outbox_authority_missing" in caplog.text
    assert "consumer_name=alioth-v2-inspection-consumer" in caplog.text
    assert "event:1" in caplog.text
    assert _TASK_ID in caplog.text
    assert "1-0" in caplog.text
    assert "inspection_work_item" in caplog.text
    assert "work-1:create" in caplog.text


async def test_inspection_consumer_uses_logical_group_for_inbox_deduplication() -> None:
    first_repository = _InboxRepository(disposition=EventInboxClaimDisposition.COMPLETED)
    second_repository = _InboxRepository(disposition=EventInboxClaimDisposition.COMPLETED)

    await _guard(first_repository, _Handler(), lease_owner="worker-a").handle(_wakeup())
    await _guard(second_repository, _Handler(), lease_owner="worker-b").handle(_wakeup())

    assert first_repository.claims[0].consumer_name == "alioth-v2-inspection-consumer"
    assert second_repository.claims[0].consumer_name == "alioth-v2-inspection-consumer"
    assert first_repository.claims[0].lease_owner != second_repository.claims[0].lease_owner


def test_event_inbox_migration_freezes_lease_and_audit_constraints() -> None:
    migration = _MIGRATION_PATH.read_text(encoding="utf-8")

    assert 'revision = "20260805_0036"' in migration
    assert 'down_revision = "20260805_0035"' in migration
    assert "CREATE TABLE qc_event_inbox" in migration
    assert "PRIMARY KEY (consumer_name, message_id)" in migration
    assert "ck_qc_event_inbox_lease" in migration
    assert "ck_qc_event_inbox_completion" in migration
    assert "ck_qc_event_inbox_dead_letter" in migration
