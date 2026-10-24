"""以 PostgreSQL Inbox 守卫 Redis 唤醒业务处理。"""

from __future__ import annotations

import logging
from collections.abc import Callable
from uuid import uuid4

from services.api.app.application.event_inbox import (
    ClaimEventInboxCommand,
    CompleteEventInboxCommand,
    EventInboxClaimDisposition,
    EventInboxIdentity,
    EventInboxRepository,
    RetryEventInboxCommand,
)
from services.api.app.infrastructure.queue.redis_stream import (
    DatabaseWakeupHandler,
    RedisStreamWakeup,
)

logger = logging.getLogger(__name__)


class InboxGuardedWakeupHandler:
    """领取 Inbox 后执行业务，Inbox 完成成功才向 Dispatcher 返回确认。"""

    def __init__(
        self,
        *,
        repository: EventInboxRepository,
        handler: DatabaseWakeupHandler,
        consumer_name: str,
        lease_owner: str,
        lease_seconds: int,
        retry_delay_seconds: int,
        max_attempts: int = 12,
        lease_token_factory: Callable[[], str] | None = None,
    ) -> None:
        if (
            not isinstance(consumer_name, str)
            or not consumer_name.strip()
            or consumer_name != consumer_name.strip()
            or not isinstance(lease_owner, str)
            or not lease_owner.strip()
            or lease_owner != lease_owner.strip()
        ):
            raise ValueError("consumer_name/lease_owner 不能为空")
        _require_int(lease_seconds, "lease_seconds", minimum=1)
        _require_int(retry_delay_seconds, "retry_delay_seconds", minimum=0)
        _require_int(max_attempts, "max_attempts", minimum=1)
        self._repository = repository
        self._handler = handler
        self._consumer_name = consumer_name
        self._lease_owner = lease_owner
        self._lease_seconds = lease_seconds
        self._retry_delay_seconds = retry_delay_seconds
        self._max_attempts = max_attempts
        self._lease_token_factory = lease_token_factory or (lambda: str(uuid4()))

    async def handle(self, wakeup: RedisStreamWakeup) -> bool:
        if not isinstance(wakeup, RedisStreamWakeup):
            raise TypeError("wakeup 必须为 RedisStreamWakeup")
        lease_token = self._lease_token_factory()
        claim = await self._repository.claim(
            ClaimEventInboxCommand(
                consumer_name=self._consumer_name,
                lease_owner=self._lease_owner,
                lease_token=lease_token,
                lease_seconds=self._lease_seconds,
                max_attempts=self._max_attempts,
                identity=EventInboxIdentity(
                    message_id=wakeup.message_id,
                    message_type=wakeup.message_type,
                    task_id=wakeup.task_id,
                    aggregate_type=wakeup.aggregate_type,
                    aggregate_id=wakeup.aggregate_id,
                    aggregate_version=wakeup.aggregate_version,
                    idempotency_key=wakeup.idempotency_key,
                ),
            )
        )
        if claim.disposition in {
            EventInboxClaimDisposition.COMPLETED,
            EventInboxClaimDisposition.DEAD_LETTER,
        }:
            return True
        if claim.disposition is EventInboxClaimDisposition.REJECTED:
            logger.warning(
                "event_inbox_wakeup_rejected reason_code=outbox_authority_missing "
                "consumer_name=%s stream_name=%s stream_entry_id=%s message_id=%s "
                "message_type=%s task_id=%s aggregate_type=%s aggregate_id=%s "
                "aggregate_version=%s idempotency_key=%s",
                self._consumer_name,
                wakeup.stream_name,
                wakeup.stream_entry_id,
                wakeup.message_id,
                wakeup.message_type,
                wakeup.task_id,
                wakeup.aggregate_type,
                wakeup.aggregate_id,
                wakeup.aggregate_version,
                wakeup.idempotency_key,
            )
            return True
        if claim.disposition is EventInboxClaimDisposition.BUSY:
            return False
        if claim.disposition is not EventInboxClaimDisposition.CLAIMED:
            raise RuntimeError("Inbox 返回了不受支持的领取结论")
        try:
            confirmed = await self._handler.handle(wakeup)
        except Exception as exc:
            await self._retry_claim(
                message_id=wakeup.message_id,
                lease_token=lease_token,
                row_version=claim.row_version,
                error=_bounded_error(exc),
            )
            raise
        if not isinstance(confirmed, bool):
            await self._retry_claim(
                message_id=wakeup.message_id,
                lease_token=lease_token,
                row_version=claim.row_version,
                error="database_handler_return_type_invalid",
            )
            raise TypeError("DatabaseWakeupHandler 必须返回布尔值")
        if not confirmed:
            await self._retry_claim(
                message_id=wakeup.message_id,
                lease_token=lease_token,
                row_version=claim.row_version,
                error="database_handler_not_confirmed",
            )
            return False
        return await self._repository.complete(
            CompleteEventInboxCommand(
                consumer_name=self._consumer_name,
                message_id=wakeup.message_id,
                lease_token=lease_token,
                expected_row_version=claim.row_version,
            )
        )

    async def _retry_claim(
        self,
        *,
        message_id: str,
        lease_token: str,
        row_version: int,
        error: str,
    ) -> None:
        await self._repository.retry(
            RetryEventInboxCommand(
                consumer_name=self._consumer_name,
                message_id=message_id,
                lease_token=lease_token,
                expected_row_version=row_version,
                error=error,
                retry_delay_seconds=self._retry_delay_seconds,
            )
        )


def _bounded_error(exc: Exception) -> str:
    text = f"{type(exc).__name__}: {exc}".strip()
    return text[:4000] or type(exc).__name__


def _require_int(value: object, field_name: str, *, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{field_name} 必须为不小于 {minimum} 的整数")
    return value
