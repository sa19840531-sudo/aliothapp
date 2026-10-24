"""增加 Outbox Redis Stream 回执与 Sweeper 重放审计。

Revision ID: 20260803_0031
Revises: 20260803_0030
Create Date: 2026-08-03
"""

from __future__ import annotations

from alembic import op

revision = "20260803_0031"
down_revision = "20260803_0030"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE qc_outbox_event
        ADD COLUMN replay_count integer NOT NULL DEFAULT 0,
        ADD COLUMN last_requeued_at timestamptz,
        ADD COLUMN last_stream_name varchar(255),
        ADD COLUMN last_stream_entry_id varchar(128),
        ADD CONSTRAINT ck_qc_outbox_event_replay_count CHECK (
            replay_count >= 0
        ),
        ADD CONSTRAINT ck_qc_outbox_event_stream_receipt CHECK (
            (last_stream_name IS NULL) = (last_stream_entry_id IS NULL)
        )
        """
    )
    op.execute(
        """
        CREATE INDEX idx_qc_outbox_event_sweeper
        ON qc_outbox_event (
            message_type,
            status,
            COALESCE(published_at, updated_at)
        )
        WHERE status IN ('published', 'dead_letter')
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_qc_outbox_event_sweeper")
    op.execute(
        """
        ALTER TABLE qc_outbox_event
        DROP CONSTRAINT IF EXISTS ck_qc_outbox_event_stream_receipt,
        DROP CONSTRAINT IF EXISTS ck_qc_outbox_event_replay_count,
        DROP COLUMN IF EXISTS last_stream_entry_id,
        DROP COLUMN IF EXISTS last_stream_name,
        DROP COLUMN IF EXISTS last_requeued_at,
        DROP COLUMN IF EXISTS replay_count
        """
    )
