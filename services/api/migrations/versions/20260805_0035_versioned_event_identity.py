"""为 Outbox 冻结版本化事件身份。

Revision ID: 20260805_0035
Revises: 20260805_0034
Create Date: 2026-08-05
"""

from __future__ import annotations

from alembic import op

revision = "20260805_0035"
down_revision = "20260805_0034"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE qc_outbox_event
        ADD COLUMN aggregate_version bigint NOT NULL DEFAULT 0,
        ADD CONSTRAINT ck_qc_outbox_event_aggregate_version CHECK (
            aggregate_version >= 0
        ),
        ADD CONSTRAINT ck_qc_outbox_event_protocol_version CHECK (
            protocol_version IN ('message-envelope.v1', 'message-envelope.v2')
        ),
        ADD CONSTRAINT ck_qc_outbox_event_v2_identity CHECK (
            protocol_version <> 'message-envelope.v2'
            OR (
                envelope_payload ?& ARRAY[
                    'protocol_version',
                    'message_id',
                    'message_type',
                    'task_id',
                    'aggregate_type',
                    'aggregate_id',
                    'aggregate_version',
                    'payload_protocol_version',
                    'idempotency_key',
                    'occurred_at',
                    'payload'
                ]
                AND envelope_payload->>'protocol_version' = protocol_version
                AND envelope_payload->>'message_id' = message_id
                AND envelope_payload->>'message_type' = message_type
                AND (envelope_payload->>'task_id')::uuid = task_id
                AND envelope_payload->>'aggregate_type' = aggregate_type
                AND envelope_payload->>'aggregate_id' = aggregate_id
                AND jsonb_typeof(envelope_payload->'aggregate_version') = 'number'
                AND (envelope_payload->>'aggregate_version')::bigint
                    = aggregate_version
                AND envelope_payload->>'payload_protocol_version'
                    = payload_protocol_version
                AND envelope_payload->>'idempotency_key' = idempotency_key
                AND (envelope_payload->>'occurred_at')::timestamptz = occurred_at
                AND jsonb_typeof(envelope_payload->'payload') = 'object'
            )
        ),
        ADD CONSTRAINT uq_qc_outbox_event_aggregate_version UNIQUE (
            message_type, aggregate_type, aggregate_id, aggregate_version
        )
        """
    )
    op.execute(
        """
        COMMENT ON COLUMN qc_outbox_event.aggregate_version IS
        '产生事件的 PostgreSQL 聚合事实版本；V1 历史事件回填为 0'
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE qc_outbox_event
        DROP CONSTRAINT IF EXISTS uq_qc_outbox_event_aggregate_version,
        DROP CONSTRAINT IF EXISTS ck_qc_outbox_event_v2_identity,
        DROP CONSTRAINT IF EXISTS ck_qc_outbox_event_protocol_version,
        DROP CONSTRAINT IF EXISTS ck_qc_outbox_event_aggregate_version,
        DROP COLUMN IF EXISTS aggregate_version
        """
    )
