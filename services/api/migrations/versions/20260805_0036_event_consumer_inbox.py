"""新增 PostgreSQL Consumer Inbox。

Revision ID: 20260805_0036
Revises: 20260805_0035
Create Date: 2026-08-05
"""

from __future__ import annotations

from alembic import op

revision = "20260805_0036"
down_revision = "20260805_0035"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE qc_event_inbox (
            consumer_name varchar(128) NOT NULL,
            message_id varchar(96) NOT NULL
                REFERENCES qc_outbox_event(message_id),
            task_id uuid NOT NULL REFERENCES qc_task(id) ON DELETE CASCADE,
            message_type varchar(128) NOT NULL,
            aggregate_type varchar(64) NOT NULL,
            aggregate_id varchar(128) NOT NULL,
            aggregate_version bigint NOT NULL,
            idempotency_key varchar(255) NOT NULL,
            status varchar(32) NOT NULL DEFAULT 'pending',
            attempt_count integer NOT NULL DEFAULT 0,
            max_attempts integer NOT NULL DEFAULT 12,
            next_attempt_at timestamptz NOT NULL DEFAULT now(),
            lease_owner varchar(255),
            lease_token uuid,
            lease_expires_at timestamptz,
            last_error text,
            completed_at timestamptz,
            row_version bigint NOT NULL DEFAULT 0,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (consumer_name, message_id),
            CONSTRAINT ck_qc_event_inbox_status CHECK (
                status IN (
                    'pending', 'processing', 'retry_wait',
                    'completed', 'dead_letter'
                )
            ),
            CONSTRAINT ck_qc_event_inbox_versions CHECK (
                aggregate_version >= 0 AND row_version >= 0
            ),
            CONSTRAINT ck_qc_event_inbox_attempts CHECK (
                attempt_count >= 0
                AND max_attempts > 0
                AND attempt_count <= max_attempts
            ),
            CONSTRAINT ck_qc_event_inbox_lease CHECK (
                (
                    status = 'processing'
                    AND lease_owner IS NOT NULL
                    AND lease_token IS NOT NULL
                    AND lease_expires_at IS NOT NULL
                )
                OR (
                    status <> 'processing'
                    AND lease_owner IS NULL
                    AND lease_token IS NULL
                    AND lease_expires_at IS NULL
                )
            ),
            CONSTRAINT ck_qc_event_inbox_completion CHECK (
                (status = 'completed' AND completed_at IS NOT NULL)
                OR (status <> 'completed' AND completed_at IS NULL)
            ),
            CONSTRAINT ck_qc_event_inbox_dead_letter CHECK (
                status <> 'dead_letter' OR last_error IS NOT NULL
            )
        )
        """
    )
    op.execute(
        """
        CREATE INDEX idx_qc_event_inbox_claim
        ON qc_event_inbox (
            consumer_name, status, next_attempt_at, lease_expires_at
        )
        WHERE status IN ('pending', 'processing', 'retry_wait')
        """
    )
    op.execute(
        """
        CREATE INDEX idx_qc_event_inbox_task
        ON qc_event_inbox (task_id, consumer_name, created_at)
        """
    )
    op.execute(
        """
        COMMENT ON TABLE qc_event_inbox IS
        '逻辑消费者的事件领取、重试、完成和死信审计；PostgreSQL 为权威事实源'
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS qc_event_inbox")
