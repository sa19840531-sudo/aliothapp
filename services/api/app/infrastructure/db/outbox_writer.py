"""在既有 PostgreSQL 事务中追加不可变 Outbox 消息。"""

from __future__ import annotations

import json
from typing import Any

from services.api.app.domain.protocol import (
    MESSAGE_ENVELOPE_PROTOCOL_VERSION,
    ProtocolEnvelope,
)


class OutboxWriteConflictError(RuntimeError):
    """同一消息身份已经绑定了不同信封载荷。"""


async def insert_outbox_event(
    connection: Any,
    *,
    aggregate_type: str,
    aggregate_id: str,
    envelope: ProtocolEnvelope,
) -> None:
    """复用调用方事务写入消息，禁止提交后再直接发布队列。"""
    if envelope.protocol_version != MESSAGE_ENVELOPE_PROTOCOL_VERSION:
        raise ValueError("Outbox 新事件必须使用 message-envelope.v2")
    if (
        envelope.aggregate_type != aggregate_type
        or envelope.aggregate_id != aggregate_id
    ):
        raise ValueError("Outbox 聚合列必须与 V2 信封身份一致")
    row = await connection.fetchval(
        """
        INSERT INTO qc_outbox_event (
            message_id, task_id, aggregate_type, aggregate_id, aggregate_version,
            message_type, protocol_version, payload_protocol_version,
            idempotency_key, envelope_payload, occurred_at
        )
        VALUES (
            $1, $2::uuid, $3, $4, $5,
            $6, $7, $8, $9, $10::jsonb, $11
        )
        ON CONFLICT DO NOTHING
        RETURNING message_id
        """,
        envelope.message_id,
        envelope.task_id,
        aggregate_type,
        aggregate_id,
        envelope.aggregate_version,
        envelope.message_type,
        envelope.protocol_version,
        envelope.payload_protocol_version,
        envelope.idempotency_key,
        json.dumps(
            envelope.to_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
        envelope.occurred_at,
    )
    if row is not None:
        return
    replay = await connection.fetchrow(
        """
        WITH candidates AS (
            SELECT event.*
            FROM qc_outbox_event AS event
            WHERE event.message_id = $1
               OR (
                    event.message_type = $6
                    AND event.idempotency_key = $9
               )
               OR (
                    event.message_type = $6
                    AND event.aggregate_type = $3
                    AND event.aggregate_id = $4
                    AND event.aggregate_version = $5
               )
        )
        SELECT
            count(*)::int AS candidate_count,
            count(*) FILTER (
                WHERE message_id = $1
                  AND task_id = $2::uuid
                  AND aggregate_type = $3
                  AND aggregate_id = $4
                  AND aggregate_version = $5
                  AND message_type = $6
                  AND protocol_version = $7
                  AND payload_protocol_version = $8
                  AND idempotency_key = $9
                  AND (envelope_payload - 'occurred_at')
                      = ($10::jsonb - 'occurred_at')
            )::int AS exact_count
        FROM candidates
        """,
        envelope.message_id,
        envelope.task_id,
        aggregate_type,
        aggregate_id,
        envelope.aggregate_version,
        envelope.message_type,
        envelope.protocol_version,
        envelope.payload_protocol_version,
        envelope.idempotency_key,
        json.dumps(
            envelope.to_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
    )
    if (
        replay is None
        or int(replay["candidate_count"]) != 1
        or int(replay["exact_count"]) != 1
    ):
        raise OutboxWriteConflictError("Outbox 事件身份、幂等键或载荷发生漂移")
