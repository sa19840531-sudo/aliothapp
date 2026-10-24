"""增加 H5/App 规则投递事故的不可变运维审计。

Revision ID: 20260818_0071
Revises: 20260818_0070
Create Date: 2026-08-18
"""

from __future__ import annotations

from alembic import op

revision = "20260818_0071"
down_revision = "20260818_0070"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """保存带版本围栏、理由和前后状态的运维处置事实。"""

    op.execute(
        """
        CREATE TABLE qc_delivery_operation_audit (
            operation_id uuid PRIMARY KEY,
            task_id uuid NOT NULL REFERENCES qc_task(id) ON DELETE RESTRICT,
            platform varchar(16) NOT NULL,
            publication_id uuid NOT NULL,
            action varchar(32) NOT NULL,
            operator_user_id uuid NOT NULL REFERENCES qc_user(id) ON DELETE RESTRICT,
            reason varchar(500) NOT NULL,
            expected_row_version bigint NOT NULL,
            before_row_version bigint NOT NULL,
            after_row_version bigint NOT NULL,
            result varchar(16) NOT NULL,
            result_message text NOT NULL,
            affected_counts jsonb NOT NULL DEFAULT '{}'::jsonb,
            before_state jsonb NOT NULL,
            after_state jsonb NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT ck_qc_delivery_operation_platform CHECK (
                platform IN ('h5', 'app')
            ),
            CONSTRAINT ck_qc_delivery_operation_action CHECK (
                action IN ('reconcile', 'requeue', 'mark_needs_review')
            ),
            CONSTRAINT ck_qc_delivery_operation_result CHECK (
                result IN ('applied', 'rejected')
            ),
            CONSTRAINT ck_qc_delivery_operation_versions CHECK (
                expected_row_version > 0
                AND before_row_version > 0
                AND after_row_version > 0
            ),
            CONSTRAINT ck_qc_delivery_operation_reason CHECK (
                reason <> '' AND length(reason) <= 500
            ),
            CONSTRAINT ck_qc_delivery_operation_json CHECK (
                jsonb_typeof(affected_counts) = 'object'
                AND jsonb_typeof(before_state) = 'object'
                AND jsonb_typeof(after_state) = 'object'
            )
        )
        """
    )
    op.execute(
        """
        CREATE INDEX idx_qc_delivery_operation_publication
        ON qc_delivery_operation_audit (
            platform, publication_id, created_at DESC
        )
        """
    )
    op.execute(
        """
        CREATE INDEX idx_qc_delivery_operation_task
        ON qc_delivery_operation_audit (task_id, created_at DESC)
        """
    )
    op.execute(
        """
        CREATE FUNCTION qc_reject_delivery_operation_audit_mutation()
        RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'qc_delivery_operation_audit is immutable';
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_qc_delivery_operation_audit_immutable
        BEFORE UPDATE OR DELETE ON qc_delivery_operation_audit
        FOR EACH ROW EXECUTE FUNCTION qc_reject_delivery_operation_audit_mutation()
        """
    )


def downgrade() -> None:
    """存在运维处置审计时拒绝破坏性降级。"""

    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM qc_delivery_operation_audit LIMIT 1) THEN
                RAISE EXCEPTION
                    'cannot downgrade delivery operation audit while audit rows exist';
            END IF;
        END
        $$
        """
    )
    op.execute(
        "DROP TRIGGER IF EXISTS trg_qc_delivery_operation_audit_immutable "
        "ON qc_delivery_operation_audit"
    )
    op.execute("DROP FUNCTION IF EXISTS qc_reject_delivery_operation_audit_mutation()")
    op.execute("DROP INDEX IF EXISTS idx_qc_delivery_operation_task")
    op.execute("DROP INDEX IF EXISTS idx_qc_delivery_operation_publication")
    op.execute("DROP TABLE qc_delivery_operation_audit")
