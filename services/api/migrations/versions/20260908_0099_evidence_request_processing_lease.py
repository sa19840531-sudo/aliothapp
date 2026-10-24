"""增加 Evidence Request 补采消费者短租约。

Revision ID: 20260908_0099
Revises: 20260908_0098
Create Date: 2026-09-08
"""

from __future__ import annotations

from alembic import op

revision = "20260908_0099"
down_revision = "20260908_0098"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """为补采请求增加可回收的单消费者处理许可。"""

    op.execute(
        """
        ALTER TABLE qc_evidence_request
        ADD COLUMN lease_owner varchar(255),
        ADD COLUMN lease_token uuid,
        ADD COLUMN lease_expires_at timestamptz,
        ADD COLUMN attempt_count integer NOT NULL DEFAULT 0,
        ADD COLUMN max_attempts integer NOT NULL DEFAULT 3,
        ADD COLUMN next_attempt_at timestamptz NOT NULL DEFAULT now(),
        ADD CONSTRAINT ck_qc_evidence_request_processing_lease CHECK (
            (
                lease_owner IS NOT NULL
                AND lease_token IS NOT NULL
                AND lease_expires_at IS NOT NULL
                AND status = 'pending'
            )
            OR
            (
                lease_owner IS NULL
                AND lease_token IS NULL
                AND lease_expires_at IS NULL
            )
        ),
        ADD CONSTRAINT ck_qc_evidence_request_attempts CHECK (
            attempt_count >= 0
            AND max_attempts > 0
            AND attempt_count <= max_attempts
        )
        """
    )
    op.execute(
        """
        CREATE INDEX idx_qc_evidence_request_processing_claim
        ON qc_evidence_request (
            task_id, finalization_epoch, next_attempt_at,
            lease_expires_at, created_at
        )
        WHERE status = 'pending'
        """
    )


def downgrade() -> None:
    """撤销消费者租约；在途请求安全回到 pending。"""

    op.execute(
        """
        UPDATE qc_evidence_request
        SET lease_owner = NULL,
            lease_token = NULL,
            lease_expires_at = NULL,
            updated_at = now()
        WHERE lease_owner IS NOT NULL
        """
    )
    op.execute("DROP INDEX IF EXISTS idx_qc_evidence_request_processing_claim")
    op.execute(
        """
        ALTER TABLE qc_evidence_request
        DROP CONSTRAINT ck_qc_evidence_request_attempts,
        DROP CONSTRAINT ck_qc_evidence_request_processing_lease,
        DROP COLUMN next_attempt_at,
        DROP COLUMN max_attempts,
        DROP COLUMN attempt_count,
        DROP COLUMN lease_expires_at,
        DROP COLUMN lease_token,
        DROP COLUMN lease_owner
        """
    )
