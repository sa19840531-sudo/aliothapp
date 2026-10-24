"""PostgreSQL Consumer Inbox 的租约与幂等完成实现。"""

from __future__ import annotations

from typing import Any

from services.api.app.application.event_inbox import (
    ClaimEventInboxCommand,
    CompleteEventInboxCommand,
    EventInboxClaim,
    EventInboxClaimDisposition,
    EventInboxConflictError,
    EventInboxRetryResult,
    RetryEventInboxCommand,
)
from services.api.app.infrastructure.db.pool import get_postgres_pool


class PostgresEventInboxRepository:
    """先核验 Outbox 权威身份，再领取逻辑消费者的事件事实。"""

    def __init__(self, *, database_url: str | None = None, pool: Any | None = None) -> None:
        self._database_url = database_url
        self._pool = pool

    async def claim(self, command: ClaimEventInboxCommand) -> EventInboxClaim:
        if not isinstance(command, ClaimEventInboxCommand):
            raise TypeError("command 必须为 ClaimEventInboxCommand")
        identity = command.identity
        pool = await self._get_pool()
        async with pool.acquire() as connection, connection.transaction():
            authoritative = await connection.fetchrow(
                """
                SELECT message_id
                FROM qc_outbox_event
                WHERE message_id = $1
                  AND message_type = $2
                  AND task_id = $3::uuid
                  AND aggregate_type = $4
                  AND aggregate_id = $5
                  AND aggregate_version = $6
                  AND idempotency_key = $7
                """,
                identity.message_id,
                identity.message_type,
                identity.task_id,
                identity.aggregate_type,
                identity.aggregate_id,
                identity.aggregate_version,
                identity.idempotency_key,
            )
            if authoritative is None:
                return EventInboxClaim(
                    disposition=EventInboxClaimDisposition.REJECTED,
                    message_id=identity.message_id,
                    row_version=0,
                    attempt_count=0,
                )
            await connection.execute(
                """
                INSERT INTO qc_event_inbox (
                    consumer_name, message_id, task_id, message_type,
                    aggregate_type, aggregate_id, aggregate_version,
                    idempotency_key, max_attempts
                )
                VALUES ($1, $2, $3::uuid, $4, $5, $6, $7, $8, $9)
                ON CONFLICT (consumer_name, message_id) DO NOTHING
                """,
                command.consumer_name,
                identity.message_id,
                identity.task_id,
                identity.message_type,
                identity.aggregate_type,
                identity.aggregate_id,
                identity.aggregate_version,
                identity.idempotency_key,
                command.max_attempts,
            )
            current = await connection.fetchrow(
                """
                SELECT
                    status,
                    row_version,
                    attempt_count,
                    max_attempts,
                    lease_expires_at > now() AS lease_active,
                    next_attempt_at <= now() AS retry_due
                FROM qc_event_inbox
                WHERE consumer_name = $1
                  AND message_id = $2
                  AND task_id = $3::uuid
                  AND message_type = $4
                  AND aggregate_type = $5
                  AND aggregate_id = $6
                  AND aggregate_version = $7
                  AND idempotency_key = $8
                FOR UPDATE
                """,
                command.consumer_name,
                identity.message_id,
                identity.task_id,
                identity.message_type,
                identity.aggregate_type,
                identity.aggregate_id,
                identity.aggregate_version,
                identity.idempotency_key,
            )
            if current is None:
                raise EventInboxConflictError("Inbox 已存事件身份发生漂移")
            status = str(current["status"])
            if status == "completed":
                return _unleased_claim(
                    disposition=EventInboxClaimDisposition.COMPLETED,
                    message_id=identity.message_id,
                    row=current,
                )
            if status == "dead_letter":
                return _unleased_claim(
                    disposition=EventInboxClaimDisposition.DEAD_LETTER,
                    message_id=identity.message_id,
                    row=current,
                )
            if (
                status == "processing" and bool(current["lease_active"])
            ) or (
                status == "retry_wait" and not bool(current["retry_due"])
            ):
                return _unleased_claim(
                    disposition=EventInboxClaimDisposition.BUSY,
                    message_id=identity.message_id,
                    row=current,
                )
            if int(current["attempt_count"]) >= int(current["max_attempts"]):
                dead = await connection.fetchrow(
                    """
                    UPDATE qc_event_inbox
                    SET
                        status = 'dead_letter',
                        lease_owner = NULL,
                        lease_token = NULL,
                        lease_expires_at = NULL,
                        last_error = COALESCE(last_error, 'inbox_attempts_exhausted'),
                        row_version = row_version + 1,
                        updated_at = now()
                    WHERE consumer_name = $1
                      AND message_id = $2
                      AND row_version = $3
                    RETURNING status, row_version, attempt_count
                    """,
                    command.consumer_name,
                    identity.message_id,
                    current["row_version"],
                )
                if dead is None:
                    raise EventInboxConflictError("Inbox 终态已被并发推进")
                return _unleased_claim(
                    disposition=EventInboxClaimDisposition.DEAD_LETTER,
                    message_id=identity.message_id,
                    row=dead,
                )
            claimed = await connection.fetchrow(
                """
                UPDATE qc_event_inbox
                SET
                    status = 'processing',
                    attempt_count = attempt_count + 1,
                    lease_owner = $4,
                    lease_token = $5::uuid,
                    lease_expires_at = now() + make_interval(secs => $6),
                    last_error = NULL,
                    row_version = row_version + 1,
                    updated_at = now()
                WHERE consumer_name = $1
                  AND message_id = $2
                  AND row_version = $3
                RETURNING
                    status,
                    row_version,
                    attempt_count,
                    lease_token::text AS lease_token
                """,
                command.consumer_name,
                identity.message_id,
                current["row_version"],
                command.lease_owner,
                command.lease_token,
                command.lease_seconds,
            )
            if claimed is None:
                raise EventInboxConflictError("Inbox 领取版本已被并发推进")
            return EventInboxClaim(
                disposition=EventInboxClaimDisposition.CLAIMED,
                message_id=identity.message_id,
                row_version=int(claimed["row_version"]),
                attempt_count=int(claimed["attempt_count"]),
                lease_token=str(claimed["lease_token"]),
            )

    async def complete(self, command: CompleteEventInboxCommand) -> bool:
        if not isinstance(command, CompleteEventInboxCommand):
            raise TypeError("command 必须为 CompleteEventInboxCommand")
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            message_id = await connection.fetchval(
                """
                UPDATE qc_event_inbox
                SET
                    status = 'completed',
                    completed_at = now(),
                    lease_owner = NULL,
                    lease_token = NULL,
                    lease_expires_at = NULL,
                    last_error = NULL,
                    row_version = row_version + 1,
                    updated_at = now()
                WHERE consumer_name = $1
                  AND message_id = $2
                  AND status = 'processing'
                  AND lease_token = $3::uuid
                  AND lease_expires_at > now()
                  AND row_version = $4
                RETURNING message_id
                """,
                command.consumer_name,
                command.message_id,
                command.lease_token,
                command.expected_row_version,
            )
        return message_id is not None

    async def retry(
        self,
        command: RetryEventInboxCommand,
    ) -> EventInboxRetryResult | None:
        if not isinstance(command, RetryEventInboxCommand):
            raise TypeError("command 必须为 RetryEventInboxCommand")
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                UPDATE qc_event_inbox
                SET
                    status = CASE
                        WHEN attempt_count >= max_attempts THEN 'dead_letter'
                        ELSE 'retry_wait'
                    END,
                    next_attempt_at = CASE
                        WHEN attempt_count >= max_attempts THEN next_attempt_at
                        ELSE now() + make_interval(secs => $6)
                    END,
                    lease_owner = NULL,
                    lease_token = NULL,
                    lease_expires_at = NULL,
                    last_error = $5,
                    row_version = row_version + 1,
                    updated_at = now()
                WHERE consumer_name = $1
                  AND message_id = $2
                  AND status = 'processing'
                  AND lease_token = $3::uuid
                  AND lease_expires_at > now()
                  AND row_version = $4
                RETURNING status, row_version
                """,
                command.consumer_name,
                command.message_id,
                command.lease_token,
                command.expected_row_version,
                command.error,
                command.retry_delay_seconds,
            )
        if row is None:
            return None
        return EventInboxRetryResult(
            status=str(row["status"]),
            row_version=int(row["row_version"]),
        )

    async def _get_pool(self) -> Any:
        if self._pool is not None:
            return self._pool
        return await get_postgres_pool(self._database_url)


def _unleased_claim(
    *,
    disposition: EventInboxClaimDisposition,
    message_id: str,
    row: Any,
) -> EventInboxClaim:
    return EventInboxClaim(
        disposition=disposition,
        message_id=message_id,
        row_version=int(row["row_version"]),
        attempt_count=int(row["attempt_count"]),
    )
