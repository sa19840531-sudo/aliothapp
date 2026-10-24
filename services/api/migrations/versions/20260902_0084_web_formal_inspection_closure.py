"""增加 Web 正式质检覆盖快照与规则结果来源幂等键。

Revision ID: 20260902_0084
Revises: 20260901_0083
Create Date: 2026-09-02
"""

from __future__ import annotations

from alembic import op

revision = "20260902_0084"
down_revision = "20260901_0083"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE qc_web_coverage_snapshot (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            task_id uuid NOT NULL REFERENCES qc_task(id) ON DELETE CASCADE,
            agent_run_id uuid NOT NULL REFERENCES qc_agent_run(id) ON DELETE RESTRICT,
            finalization_epoch integer NOT NULL,
            required_page_types jsonb NOT NULL,
            observed_page_types jsonb NOT NULL,
            missing_page_types jsonb NOT NULL,
            unrecognized_page_types jsonb NOT NULL,
            normalization_version varchar(64) NOT NULL,
            coverage_status varchar(32) NOT NULL,
            snapshot_hash varchar(64) NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT uq_qc_web_coverage_snapshot_identity
                UNIQUE (task_id, agent_run_id, finalization_epoch),
            CONSTRAINT ck_qc_web_coverage_snapshot_epoch CHECK (
                finalization_epoch >= 0
            ),
            CONSTRAINT ck_qc_web_coverage_snapshot_collections CHECK (
                jsonb_typeof(required_page_types) = 'array'
                AND jsonb_typeof(observed_page_types) = 'array'
                AND jsonb_typeof(missing_page_types) = 'array'
                AND jsonb_typeof(unrecognized_page_types) = 'array'
            ),
            CONSTRAINT ck_qc_web_coverage_snapshot_normalization CHECK (
                normalization_version = 'web-page-type-normalization.v1'
            ),
            CONSTRAINT ck_qc_web_coverage_snapshot_status CHECK (
                coverage_status IN (
                    'complete', 'completed_with_gaps', 'failed'
                )
            )
        )
        """
    )
    op.execute(
        """
        CREATE FUNCTION qc_reject_web_coverage_snapshot_mutation()
        RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'qc_web_coverage_snapshot is immutable';
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_qc_web_coverage_snapshot_immutable
        BEFORE UPDATE OR DELETE ON qc_web_coverage_snapshot
        FOR EACH ROW EXECUTE FUNCTION qc_reject_web_coverage_snapshot_mutation()
        """
    )
    op.execute(
        """
        ALTER TABLE qc_rule_execution
        ADD COLUMN source_idempotency_key varchar(255)
        """
    )
    op.execute(
        """
        CREATE FUNCTION qc_guard_rule_execution_source_identity()
        RETURNS trigger AS $$
        BEGIN
            IF NEW.source_idempotency_key IS DISTINCT FROM OLD.source_idempotency_key THEN
                RAISE EXCEPTION 'qc_rule_execution source identity is immutable';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_qc_rule_execution_source_identity_guard
        BEFORE UPDATE OF source_idempotency_key ON qc_rule_execution
        FOR EACH ROW EXECUTE FUNCTION qc_guard_rule_execution_source_identity()
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX uq_qc_rule_execution_source_idempotency
        ON qc_rule_execution (task_id, rule_id, source_idempotency_key)
        WHERE source_idempotency_key IS NOT NULL
        """
    )
    op.execute(
        """
        COMMENT ON TABLE qc_web_coverage_snapshot IS
        'Web 探索终态后按 task、main run 和 finalization epoch 冻结的不可变覆盖事实'
        """
    )
    op.execute(
        """
        COMMENT ON COLUMN qc_rule_execution.source_idempotency_key IS
        '无页面 Capture 的正式结果来源幂等身份；为空时保持原页面结果语义'
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_qc_rule_execution_source_idempotency")
    op.execute(
        "DROP TRIGGER IF EXISTS trg_qc_rule_execution_source_identity_guard "
        "ON qc_rule_execution"
    )
    op.execute(
        "DROP FUNCTION IF EXISTS qc_guard_rule_execution_source_identity()"
    )
    op.execute(
        """
        ALTER TABLE qc_rule_execution
        DROP COLUMN IF EXISTS source_idempotency_key
        """
    )
    op.execute(
        "DROP TRIGGER IF EXISTS trg_qc_web_coverage_snapshot_immutable "
        "ON qc_web_coverage_snapshot"
    )
    op.execute("DROP FUNCTION IF EXISTS qc_reject_web_coverage_snapshot_mutation()")
    op.execute("DROP TABLE qc_web_coverage_snapshot")
