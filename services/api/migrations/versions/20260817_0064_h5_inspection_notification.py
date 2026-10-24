"""增加 H5 证据到规则质检的持久化 Publication。

Revision ID: 20260817_0064
Revises: 20260814_0063
Create Date: 2026-08-17
"""

from __future__ import annotations

from alembic import op

revision = "20260817_0064"
down_revision = "20260814_0063"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """建立 H5 Publication、冻结规则和共享工作项关联事实。"""

    op.execute(
        """
        CREATE TABLE qc_h5_inspection_publication (
            id uuid PRIMARY KEY,
            task_id uuid NOT NULL REFERENCES qc_task(id) ON DELETE CASCADE,
            source_agent_run_id uuid NOT NULL
                REFERENCES qc_agent_run(id) ON DELETE RESTRICT,
            page_capture_id uuid NOT NULL
                REFERENCES qc_page_capture(id) ON DELETE RESTRICT,
            page_type_assessment_id varchar(160) NOT NULL
                REFERENCES qc_h5_page_type_assessment(assessment_record_id)
                ON DELETE RESTRICT,
            assessment_hash varchar(128) NOT NULL,
            ruleset_id uuid NOT NULL REFERENCES qc_ruleset(id) ON DELETE RESTRICT,
            ruleset_version integer NOT NULL,
            ruleset_snapshot_hash varchar(128),
            finalization_epoch integer NOT NULL,
            input_hash varchar(128) NOT NULL,
            mode varchar(16) NOT NULL,
            status varchar(32) NOT NULL DEFAULT 'pending',
            lease_owner varchar(255),
            lease_token uuid,
            lease_expires_at timestamptz,
            attempt_count integer NOT NULL DEFAULT 0,
            max_attempts integer NOT NULL DEFAULT 3,
            next_attempt_at timestamptz NOT NULL DEFAULT now(),
            row_version bigint NOT NULL DEFAULT 1,
            candidate_rule_count integer,
            expected_batch_count integer,
            published_work_item_count integer NOT NULL DEFAULT 0,
            excluded_active_session_rule_count integer NOT NULL DEFAULT 0,
            consumer_agent_run_id uuid REFERENCES qc_agent_run(id) ON DELETE SET NULL,
            failure_code varchar(128),
            failure_message text,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            finished_at timestamptz,
            CONSTRAINT uq_qc_h5_publication_capture_epoch
                UNIQUE (task_id, page_capture_id, finalization_epoch),
            CONSTRAINT uq_qc_h5_publication_input UNIQUE (input_hash),
            CONSTRAINT ck_qc_h5_publication_mode CHECK (
                mode IN ('shadow', 'enforced')
            ),
            CONSTRAINT ck_qc_h5_publication_status CHECK (
                status IN ('pending', 'leased', 'retry_wait', 'published',
                    'not_applicable', 'needs_review', 'failed', 'cancelled',
                    'superseded')
            ),
            CONSTRAINT ck_qc_h5_publication_counts CHECK (
                finalization_epoch >= 0
                AND ruleset_version > 0
                AND attempt_count >= 0
                AND max_attempts > 0
                AND attempt_count <= max_attempts
                AND row_version > 0
                AND published_work_item_count >= 0
                AND excluded_active_session_rule_count >= 0
                AND (candidate_rule_count IS NULL OR candidate_rule_count >= 0)
                AND (expected_batch_count IS NULL OR expected_batch_count >= 0)
            ),
            CONSTRAINT ck_qc_h5_publication_lease CHECK (
                (status = 'leased' AND lease_owner IS NOT NULL
                    AND lease_token IS NOT NULL AND lease_expires_at IS NOT NULL)
                OR
                (status <> 'leased' AND lease_owner IS NULL
                    AND lease_token IS NULL AND lease_expires_at IS NULL)
            ),
            CONSTRAINT ck_qc_h5_publication_finished CHECK (
                (status IN ('published', 'not_applicable', 'needs_review',
                    'failed', 'cancelled', 'superseded') AND finished_at IS NOT NULL)
                OR
                (status IN ('pending', 'leased', 'retry_wait') AND finished_at IS NULL)
            )
        )
        """
    )
    op.execute(
        """
        CREATE TABLE qc_h5_inspection_publication_rule (
            publication_id uuid NOT NULL
                REFERENCES qc_h5_inspection_publication(id) ON DELETE CASCADE,
            rule_id uuid NOT NULL REFERENCES qc_rule(id) ON DELETE RESTRICT,
            rule_code varchar(128) NOT NULL,
            ordinal integer NOT NULL,
            contract_hash varchar(128) NOT NULL,
            strategy varchar(32) NOT NULL,
            category varchar(32),
            session_dependency varchar(32) NOT NULL,
            execution_plan jsonb NOT NULL DEFAULT '{}'::jsonb,
            created_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (publication_id, rule_id),
            CONSTRAINT uq_qc_h5_publication_rule_ordinal
                UNIQUE (publication_id, ordinal),
            CONSTRAINT ck_qc_h5_publication_rule_values CHECK (
                ordinal >= 0
                AND strategy IN ('rule_engine', 'llm', 'hybrid')
                AND session_dependency IN ('snapshot', 'active_session')
                AND jsonb_typeof(execution_plan) = 'object'
            )
        )
        """
    )
    op.execute(
        """
        CREATE TABLE qc_h5_inspection_publication_work_item (
            publication_id uuid NOT NULL
                REFERENCES qc_h5_inspection_publication(id) ON DELETE CASCADE,
            work_item_id uuid NOT NULL
                REFERENCES qc_inspection_work_item(id) ON DELETE RESTRICT,
            batch_key varchar(255) NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (publication_id, work_item_id),
            CONSTRAINT uq_qc_h5_publication_batch
                UNIQUE (publication_id, batch_key)
        )
        """
    )
    op.execute(
        """
        CREATE INDEX idx_qc_h5_publication_claim
        ON qc_h5_inspection_publication (
            status, next_attempt_at, lease_expires_at, created_at
        )
        """
    )
    op.execute(
        """
        CREATE INDEX idx_qc_h5_publication_task_epoch
        ON qc_h5_inspection_publication (
            task_id, finalization_epoch, status, created_at
        )
        """
    )
    op.execute(
        """
        CREATE INDEX idx_qc_h5_publication_work_item
        ON qc_h5_inspection_publication_work_item (work_item_id)
        """
    )


def downgrade() -> None:
    """存在通知审计事实时拒绝破坏性降级。"""

    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM qc_h5_inspection_publication LIMIT 1) THEN
                RAISE EXCEPTION
                    'cannot downgrade H5 inspection notification while audit rows exist';
            END IF;
        END
        $$
        """
    )
    op.execute("DROP INDEX IF EXISTS idx_qc_h5_publication_work_item")
    op.execute("DROP INDEX IF EXISTS idx_qc_h5_publication_task_epoch")
    op.execute("DROP INDEX IF EXISTS idx_qc_h5_publication_claim")
    op.execute("DROP TABLE qc_h5_inspection_publication_work_item")
    op.execute("DROP TABLE qc_h5_inspection_publication_rule")
    op.execute("DROP TABLE qc_h5_inspection_publication")
