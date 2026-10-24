"""Transactional Outbox 的 PostgreSQL 租约仓储。"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from services.api.app.application.outbox import (
    SUPPORTED_WAKEUP_MESSAGE_TYPES,
    TASK_FINALIZATION_PRIORITY_WAKEUP_MESSAGE_TYPES,
    OutboxLease,
)
from services.api.app.domain.protocol import ProtocolEnvelope
from services.api.app.infrastructure.db.pool import get_postgres_pool


class PostgresOutboxRepository:
    """使用短租约、SKIP LOCKED 与最大尝试次数支撑可靠 Relay。"""

    def __init__(self, *, database_url: str | None = None, pool: Any | None = None) -> None:
        self._database_url = database_url
        self._pool = pool

    async def claim(
        self,
        *,
        lease_owner: str,
        lease_token: str,
        lease_seconds: int = 60,
    ) -> OutboxLease | None:
        if not lease_owner.strip():
            raise ValueError("lease_owner 不能为空")
        if lease_seconds <= 0:
            raise ValueError("lease_seconds 必须大于 0")
        pool = await self._get_pool()
        async with pool.acquire() as connection, connection.transaction():
            await connection.execute(
                """
                UPDATE qc_outbox_event
                SET
                    status = 'dead_letter',
                    lease_owner = NULL,
                    lease_token = NULL,
                    lease_expires_at = NULL,
                    last_error = COALESCE(last_error, 'outbox_attempts_exhausted'),
                    updated_at = now()
                WHERE status = 'publishing'
                  AND lease_expires_at <= now()
                  AND attempt_count >= max_attempts
                """
            )
            row = await connection.fetchrow(
                """
                WITH candidate AS (
                    SELECT message_id
                    FROM qc_outbox_event
                    WHERE attempt_count < max_attempts
                      -- Relay 只投递可路由唤醒消息；非唤醒类型（如 app-workflow.ready）
                      -- 不得进入 Redis Stream 发布循环。
                      AND message_type = ANY($4::varchar[])
                      AND (
                        (
                            status IN ('pending', 'retry_wait')
                            AND next_attempt_at <= now()
                        )
                        OR (
                            status = 'publishing'
                            AND lease_expires_at <= now()
                        )
                      )
                    -- 终态 Publication 是任务封存门禁，不能被持续产生的普通唤醒饿死。
                    ORDER BY
                        (message_type = ANY($5::varchar[])) DESC,
                        next_attempt_at,
                        occurred_at,
                        message_id
                    FOR UPDATE SKIP LOCKED
                    LIMIT 1
                )
                UPDATE qc_outbox_event AS event
                SET
                    status = 'publishing',
                    attempt_count = event.attempt_count + 1,
                    lease_owner = $1,
                    lease_token = $2::uuid,
                    lease_expires_at = now() + make_interval(secs => $3),
                    updated_at = now()
                FROM candidate
                WHERE event.message_id = candidate.message_id
                RETURNING
                    event.aggregate_type,
                    event.aggregate_id,
                    event.aggregate_version,
                    event.envelope_payload,
                    event.lease_token::text AS lease_token,
                    event.attempt_count
                """,
                lease_owner,
                lease_token,
                lease_seconds,
                list(SUPPORTED_WAKEUP_MESSAGE_TYPES),
                list(TASK_FINALIZATION_PRIORITY_WAKEUP_MESSAGE_TYPES),
            )
        if row is None:
            return None
        return OutboxLease(
            aggregate_type=str(row["aggregate_type"]),
            aggregate_id=str(row["aggregate_id"]),
            aggregate_version=int(row["aggregate_version"]),
            envelope=ProtocolEnvelope.from_dict(
                _as_mapping(row["envelope_payload"], "envelope_payload")
            ),
            lease_token=str(row["lease_token"]),
            attempt_count=int(row["attempt_count"]),
        )

    async def mark_published(
        self,
        *,
        message_id: str,
        lease_token: str,
        stream_name: str,
        stream_entry_id: str,
    ) -> bool:
        if not stream_name.strip() or not stream_entry_id.strip():
            raise ValueError("Stream 回执不能为空")
        pool = await self._get_pool()
        async with pool.acquire() as connection, connection.transaction():
            task_id = await connection.fetchval(
                """
                UPDATE qc_outbox_event
                SET
                    status = 'published',
                    published_at = now(),
                    lease_owner = NULL,
                    lease_token = NULL,
                    lease_expires_at = NULL,
                    last_error = NULL,
                    last_stream_name = $3,
                    last_stream_entry_id = $4,
                    updated_at = now()
                WHERE message_id = $1
                  AND status = 'publishing'
                  AND lease_token = $2::uuid
                  AND lease_expires_at > now()
                RETURNING task_id::text
                """,
                message_id,
                lease_token,
                stream_name,
                stream_entry_id,
            )
            if task_id is None:
                return False
            await connection.execute(
                """
                UPDATE qc_task
                SET finalization_retry_at = now()
                WHERE id = $1::uuid
                  AND status IN ('running', 'review')
                  AND finalization_state = 'session_draining'
                  AND completion_gate_snapshot_id IS NOT NULL
                  AND finalization_recovery_state = 'active'
                """,
                task_id,
            )
        return True

    async def reschedule(
        self,
        *,
        message_id: str,
        lease_token: str,
        error: str,
        retry_delay_seconds: int,
    ) -> bool:
        if not error.strip():
            raise ValueError("error 不能为空")
        if retry_delay_seconds < 0:
            raise ValueError("retry_delay_seconds 不能小于 0")
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            updated_id = await connection.fetchval(
                """
                UPDATE qc_outbox_event
                SET
                    status = CASE
                        WHEN attempt_count >= max_attempts
                        THEN 'dead_letter'
                        ELSE 'retry_wait'
                    END,
                    next_attempt_at = CASE
                        WHEN attempt_count >= max_attempts
                        THEN next_attempt_at
                        ELSE now() + make_interval(secs => $4)
                    END,
                    lease_owner = NULL,
                    lease_token = NULL,
                    lease_expires_at = NULL,
                    last_error = $3,
                    updated_at = now()
                WHERE message_id = $1
                  AND status = 'publishing'
                  AND lease_token = $2::uuid
                  AND lease_expires_at > now()
                RETURNING message_id
                """,
                message_id,
                lease_token,
                error,
                retry_delay_seconds,
            )
        return updated_id is not None

    async def has_unpublished(self, *, task_id: str) -> bool:
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            return bool(
                await connection.fetchval(
                    """
                    SELECT EXISTS (
                        SELECT 1
                        FROM qc_outbox_event
                        WHERE task_id = $1::uuid
                          AND status IN (
                              'pending', 'publishing', 'retry_wait', 'dead_letter'
                          )
                          AND message_type = ANY($2::varchar[])
                    )
                    """,
                    task_id,
                    list(SUPPORTED_WAKEUP_MESSAGE_TYPES),
                )
            )

    async def requeue_stale_deliveries(
        self,
        *,
        message_types: tuple[str, ...],
        min_age_seconds: int,
        limit: int,
    ) -> int:
        if not message_types or any(not item.strip() for item in message_types):
            raise ValueError("message_types 必须为非空文本数组")
        if len(message_types) != len(set(message_types)):
            raise ValueError("message_types 不能重复")
        if min_age_seconds <= 0 or limit <= 0:
            raise ValueError("min_age_seconds 和 limit 必须大于 0")
        pool = await self._get_pool()
        async with pool.acquire() as connection, connection.transaction():
            count = await connection.fetchval(
                """
                WITH candidate AS (
                    SELECT event.message_id
                    FROM qc_outbox_event AS event
                    INNER JOIN qc_task AS task ON task.id = event.task_id
                    WHERE event.message_type = ANY($1::varchar[])
                      AND task.status IN ('queued', 'running')
                      AND task.finalization_state IN (
                          'open', 'exploration_draining', 'session_draining'
                      )
                      AND (
                          event.aggregate_type <> 'task_submission'
                          OR EXISTS (
                              SELECT 1
                              FROM qc_task_submission AS submission
                              INNER JOIN qc_agent_run AS initial_run
                                  ON initial_run.task_id = submission.task_id
                                 AND initial_run.id = submission.initial_agent_run_id
                              WHERE submission.id::text = event.aggregate_id
                                AND submission.task_id = event.task_id
                                AND initial_run.status = 'pending'
                          )
                      )
                      AND (
                          event.aggregate_type <> 'inspection_work_item'
                          OR EXISTS (
                              SELECT 1
                              FROM qc_inspection_work_item AS work_item
                              WHERE work_item.id::text = event.aggregate_id
                                AND work_item.task_id = event.task_id
                                AND work_item.status NOT IN (
                                    'succeeded', 'partial', 'failed',
                                    'cancelled', 'superseded'
                                )
                          )
                      )
                      AND (
                          event.aggregate_type <>
                              'aggregate_inspection_work_item'
                          OR EXISTS (
                              SELECT 1
                              FROM qc_aggregate_inspection_work_item AS aggregate_work
                              WHERE aggregate_work.id::text = event.aggregate_id
                                AND aggregate_work.task_id = event.task_id
                                AND aggregate_work.status NOT IN (
                                    'succeeded', 'evidence_insufficient', 'failed',
                                    'cancelled', 'superseded'
                                )
                          )
                      )
                      AND (
                          event.aggregate_type <> 'explorer_work'
                          OR EXISTS (
                              SELECT 1
                              FROM qc_v2_work_item AS work_item
                              WHERE work_item.work_id = event.aggregate_id
                                AND work_item.task_id = event.task_id
                                AND work_item.status NOT IN (
                                    'succeeded', 'bounded', 'budget_exhausted',
                                    'blocked', 'no_progress', 'failed',
                                    'cancelled', 'superseded'
                                )
                          )
                      )
                      AND (
                        (
                            event.status = 'published'
                            AND event.published_at <= now() - make_interval(secs => $2)
                        )
                        OR (
                            event.status = 'dead_letter'
                            AND event.updated_at <= now() - make_interval(secs => $2)
                        )
                      )
                    ORDER BY
                        COALESCE(event.published_at, event.updated_at),
                        event.message_id
                    FOR UPDATE OF event SKIP LOCKED
                    LIMIT $3
                ), reset_dead_inbox AS (
                    UPDATE qc_event_inbox AS inbox
                    SET
                        status = 'pending',
                        attempt_count = 0,
                        next_attempt_at = now(),
                        lease_owner = NULL,
                        lease_token = NULL,
                        lease_expires_at = NULL,
                        last_error = NULL,
                        completed_at = NULL,
                        row_version = inbox.row_version + 1,
                        updated_at = now()
                    FROM candidate
                    WHERE inbox.message_id = candidate.message_id
                      AND inbox.status = 'dead_letter'
                    RETURNING inbox.message_id
                ), requeued AS (
                    UPDATE qc_outbox_event AS event
                    SET
                        status = 'pending',
                        attempt_count = 0,
                        next_attempt_at = now(),
                        lease_owner = NULL,
                        lease_token = NULL,
                        lease_expires_at = NULL,
                        last_error = CASE
                            WHEN event.status = 'dead_letter'
                            THEN 'sweeper_requeued_dead_letter'
                            ELSE NULL
                        END,
                        published_at = NULL,
                        replay_count = event.replay_count + 1,
                        last_requeued_at = now(),
                        updated_at = now()
                    FROM candidate
                    WHERE event.message_id = candidate.message_id
                    RETURNING 1
                )
                SELECT count(*) FROM requeued
                """,
                list(message_types),
                min_age_seconds,
                limit,
            )
        return int(count or 0)

    async def _get_pool(self) -> Any:
        if self._pool is not None:
            return self._pool
        return await get_postgres_pool(self._database_url)


def _as_mapping(value: Any, field_name: str) -> Mapping[str, Any]:
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, Mapping):
        raise ValueError(f"{field_name} 必须为对象")
    return value
