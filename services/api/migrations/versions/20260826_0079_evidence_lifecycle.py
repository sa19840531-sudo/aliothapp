"""证据生命周期：制品状态、合法留存与审计事件。

Revision ID: 20260826_0079
Revises: 20260825_0078
Create Date: 2026-08-26

为对象存储保留／归档／GC 提供可审计骨架；默认不启用物理删除。
"""

from __future__ import annotations

from alembic import op

revision = "20260826_0079"
down_revision = "20260825_0078"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE qc_evidence_artifact
            ADD COLUMN IF NOT EXISTS lifecycle_status varchar(32) NOT NULL DEFAULT 'active',
            ADD COLUMN IF NOT EXISTS archived_at timestamptz,
            ADD COLUMN IF NOT EXISTS deleted_at timestamptz
        """
    )
    op.execute(
        """
        ALTER TABLE qc_evidence_artifact
            DROP CONSTRAINT IF EXISTS ck_qc_evidence_artifact_lifecycle
        """
    )
    op.execute(
        """
        ALTER TABLE qc_evidence_artifact
            ADD CONSTRAINT ck_qc_evidence_artifact_lifecycle CHECK (
                lifecycle_status IN (
                    'active', 'archived', 'pending_delete', 'deleted'
                )
            )
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_qc_evidence_artifact_lifecycle
            ON qc_evidence_artifact (lifecycle_status, created_at)
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS qc_evidence_legal_hold (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            task_id uuid NOT NULL REFERENCES qc_task(id) ON DELETE CASCADE,
            reason text NOT NULL,
            held_by varchar(128) NOT NULL,
            held_at timestamptz NOT NULL,
            released_at timestamptz,
            release_reason text,
            created_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT ck_qc_evidence_legal_hold_reason CHECK (
                length(btrim(reason)) > 0
            ),
            CONSTRAINT ck_qc_evidence_legal_hold_release CHECK (
                (released_at IS NULL AND release_reason IS NULL)
                OR (released_at IS NOT NULL AND release_reason IS NOT NULL)
            )
        )
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_qc_evidence_legal_hold_active
            ON qc_evidence_legal_hold (task_id)
            WHERE released_at IS NULL
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS qc_evidence_lifecycle_event (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            artifact_id uuid,
            task_id uuid NOT NULL REFERENCES qc_task(id) ON DELETE CASCADE,
            event_type varchar(64) NOT NULL,
            policy_version varchar(64) NOT NULL,
            actor varchar(128) NOT NULL,
            detail jsonb NOT NULL DEFAULT '{}'::jsonb,
            created_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT ck_qc_evidence_lifecycle_event_type CHECK (
                event_type IN (
                    'evaluate', 'archive', 'mark_delete', 'delete',
                    'restore', 'hold_apply', 'hold_release'
                )
            ),
            CONSTRAINT ck_qc_evidence_lifecycle_event_json CHECK (
                jsonb_typeof(detail) = 'object'
            )
        )
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_qc_evidence_lifecycle_event_task
            ON qc_evidence_lifecycle_event (task_id, created_at DESC)
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS qc_evidence_lifecycle_event")
    op.execute("DROP TABLE IF EXISTS qc_evidence_legal_hold")
    op.execute(
        "DROP INDEX IF EXISTS idx_qc_evidence_artifact_lifecycle"
    )
    op.execute(
        """
        ALTER TABLE qc_evidence_artifact
            DROP CONSTRAINT IF EXISTS ck_qc_evidence_artifact_lifecycle
        """
    )
    op.execute(
        """
        ALTER TABLE qc_evidence_artifact
            DROP COLUMN IF EXISTS deleted_at,
            DROP COLUMN IF EXISTS archived_at,
            DROP COLUMN IF EXISTS lifecycle_status
        """
    )
