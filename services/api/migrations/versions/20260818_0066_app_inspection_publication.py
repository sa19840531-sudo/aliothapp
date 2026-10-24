"""增加 App sealed Evidence Index 到规则工作的持久化 Publication。

Revision ID: 20260818_0066
Revises: 20260818_0065
Create Date: 2026-08-18
"""

from __future__ import annotations

from alembic import op

revision = "20260818_0066"
down_revision = "20260818_0065"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """建立 App Publication、Index Entry、冻结规则与工作项关联。"""

    op.execute(
        """
        CREATE TABLE qc_app_inspection_publication (
            id uuid PRIMARY KEY,
            task_id uuid NOT NULL REFERENCES qc_task(id) ON DELETE CASCADE,
            source_agent_run_id uuid NOT NULL
                REFERENCES qc_agent_run(id) ON DELETE RESTRICT,
            evidence_index_id varchar(96) NOT NULL
                REFERENCES qc_app_evidence_index(index_id) ON DELETE RESTRICT,
            evidence_index_hash varchar(128) NOT NULL,
            ruleset_id uuid NOT NULL REFERENCES qc_ruleset(id) ON DELETE RESTRICT,
            ruleset_version integer NOT NULL,
            ruleset_snapshot_hash varchar(128),
            finalization_epoch integer NOT NULL,
            input_hash varchar(128) NOT NULL,
            status varchar(32) NOT NULL DEFAULT 'pending',
            lease_owner varchar(255),
            lease_token uuid,
            lease_expires_at timestamptz,
            attempt_count integer NOT NULL DEFAULT 0,
            max_attempts integer NOT NULL DEFAULT 3,
            next_attempt_at timestamptz NOT NULL DEFAULT now(),
            row_version bigint NOT NULL DEFAULT 1,
            expected_entry_count integer NOT NULL,
            terminal_entry_count integer NOT NULL DEFAULT 0,
            expected_rule_count integer,
            linked_work_item_count integer NOT NULL DEFAULT 0,
            explicit_exclusion_count integer NOT NULL DEFAULT 0,
            failure_code varchar(128),
            failure_message text,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            finished_at timestamptz,
            CONSTRAINT uq_qc_app_publication_index_epoch
                UNIQUE (task_id, evidence_index_id, finalization_epoch),
            CONSTRAINT uq_qc_app_publication_input UNIQUE (input_hash),
            CONSTRAINT ck_qc_app_publication_status CHECK (
                status IN ('pending', 'leased', 'retry_wait', 'published',
                    'not_applicable', 'needs_review', 'failed', 'cancelled',
                    'superseded')
            ),
            CONSTRAINT ck_qc_app_publication_counts CHECK (
                finalization_epoch >= 0
                AND ruleset_version > 0
                AND attempt_count >= 0
                AND max_attempts > 0
                AND attempt_count <= max_attempts
                AND row_version > 0
                AND expected_entry_count >= 0
                AND terminal_entry_count >= 0
                AND terminal_entry_count <= expected_entry_count
                AND (expected_rule_count IS NULL OR expected_rule_count >= 0)
                AND linked_work_item_count >= 0
                AND explicit_exclusion_count >= 0
            ),
            CONSTRAINT ck_qc_app_publication_lease CHECK (
                (status = 'leased' AND lease_owner IS NOT NULL
                    AND lease_token IS NOT NULL AND lease_expires_at IS NOT NULL)
                OR
                (status <> 'leased' AND lease_owner IS NULL
                    AND lease_token IS NULL AND lease_expires_at IS NULL)
            ),
            CONSTRAINT ck_qc_app_publication_finished CHECK (
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
        CREATE TABLE qc_app_inspection_publication_entry (
            entry_id bigserial PRIMARY KEY,
            publication_id uuid NOT NULL
                REFERENCES qc_app_inspection_publication(id) ON DELETE CASCADE,
            sequence_no integer NOT NULL,
            capture_id uuid NOT NULL REFERENCES qc_capture(id) ON DELETE RESTRICT,
            evidence_manifest_id varchar(96) NOT NULL
                REFERENCES qc_evidence_manifest(manifest_id) ON DELETE RESTRICT,
            evidence_manifest_version integer NOT NULL,
            semantic_role varchar(32) NOT NULL,
            page_type varchar(128),
            page_state varchar(64) NOT NULL DEFAULT 'stable',
            status varchar(32) NOT NULL DEFAULT 'pending',
            candidate_rule_count integer,
            linked_work_item_count integer NOT NULL DEFAULT 0,
            explicit_exclusion_count integer NOT NULL DEFAULT 0,
            failure_code varchar(128),
            failure_message text,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            finished_at timestamptz,
            CONSTRAINT uq_qc_app_publication_entry_sequence
                UNIQUE (publication_id, sequence_no),
            CONSTRAINT uq_qc_app_publication_entry_capture
                UNIQUE (publication_id, capture_id),
            CONSTRAINT uq_qc_app_publication_entry_identity
                UNIQUE (publication_id, entry_id),
            CONSTRAINT ck_qc_app_publication_entry_values CHECK (
                sequence_no > 0
                AND evidence_manifest_version > 0
                AND page_state <> ''
                AND (candidate_rule_count IS NULL OR candidate_rule_count >= 0)
                AND linked_work_item_count >= 0
                AND explicit_exclusion_count >= 0
                AND status IN ('pending', 'linked', 'not_applicable', 'needs_review')
            ),
            CONSTRAINT ck_qc_app_publication_entry_finished CHECK (
                (status = 'pending' AND finished_at IS NULL)
                OR (status <> 'pending' AND finished_at IS NOT NULL)
            )
        )
        """
    )
    op.execute(
        """
        CREATE TABLE qc_app_inspection_publication_work_item (
            publication_id uuid NOT NULL,
            entry_id bigint NOT NULL,
            work_item_id uuid NOT NULL
                REFERENCES qc_inspection_work_item(id) ON DELETE RESTRICT,
            batch_key varchar(255) NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (publication_id, work_item_id),
            CONSTRAINT fk_qc_app_publication_work_entry
                FOREIGN KEY (publication_id, entry_id)
                REFERENCES qc_app_inspection_publication_entry(
                    publication_id, entry_id
                ) ON DELETE CASCADE,
            CONSTRAINT uq_qc_app_publication_work_identity
                UNIQUE (publication_id, entry_id, work_item_id),
            CONSTRAINT uq_qc_app_publication_entry_batch
                UNIQUE (entry_id, batch_key)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE qc_app_inspection_publication_rule (
            publication_id uuid NOT NULL,
            entry_id bigint NOT NULL,
            rule_id uuid NOT NULL REFERENCES qc_rule(id) ON DELETE RESTRICT,
            rule_code varchar(128) NOT NULL,
            ordinal integer NOT NULL,
            contract_hash varchar(128) NOT NULL,
            strategy varchar(32) NOT NULL,
            category varchar(32),
            session_dependency varchar(32) NOT NULL,
            disposition varchar(32) NOT NULL,
            work_item_id uuid REFERENCES qc_inspection_work_item(id) ON DELETE RESTRICT,
            execution_plan jsonb NOT NULL DEFAULT '{}'::jsonb,
            created_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (publication_id, entry_id, rule_id),
            CONSTRAINT fk_qc_app_publication_rule_entry
                FOREIGN KEY (publication_id, entry_id)
                REFERENCES qc_app_inspection_publication_entry(
                    publication_id, entry_id
                ) ON DELETE CASCADE,
            CONSTRAINT fk_qc_app_publication_rule_work
                FOREIGN KEY (publication_id, entry_id, work_item_id)
                REFERENCES qc_app_inspection_publication_work_item(
                    publication_id, entry_id, work_item_id
                ) ON DELETE CASCADE,
            CONSTRAINT uq_qc_app_publication_rule_ordinal
                UNIQUE (entry_id, ordinal),
            CONSTRAINT ck_qc_app_publication_rule_values CHECK (
                ordinal >= 0
                AND strategy IN ('rule_engine', 'llm', 'hybrid')
                AND session_dependency IN ('snapshot', 'active_session')
                AND disposition IN ('linked', 'not_applicable', 'needs_review')
                AND jsonb_typeof(execution_plan) = 'object'
                AND ((disposition = 'linked' AND work_item_id IS NOT NULL)
                    OR (disposition <> 'linked' AND work_item_id IS NULL))
            )
        )
        """
    )
    op.execute(
        """
        CREATE INDEX idx_qc_app_publication_claim
        ON qc_app_inspection_publication (
            status, next_attempt_at, lease_expires_at, created_at
        )
        """
    )
    op.execute(
        """
        CREATE INDEX idx_qc_app_publication_task_epoch
        ON qc_app_inspection_publication (
            task_id, finalization_epoch, status, created_at
        )
        """
    )
    op.execute(
        """
        CREATE INDEX idx_qc_app_publication_entry_status
        ON qc_app_inspection_publication_entry (
            publication_id, status, sequence_no
        )
        """
    )
    op.execute(
        """
        CREATE INDEX idx_qc_app_publication_rule_disposition
        ON qc_app_inspection_publication_rule (
            publication_id, disposition, entry_id
        )
        """
    )
    op.execute(
        """
        CREATE INDEX idx_qc_app_publication_work_item
        ON qc_app_inspection_publication_work_item (work_item_id)
        """
    )


def downgrade() -> None:
    """存在 App Publication 审计事实时拒绝破坏性降级。"""

    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM qc_app_inspection_publication LIMIT 1) THEN
                RAISE EXCEPTION
                    'cannot downgrade App inspection publication while audit rows exist';
            END IF;
        END
        $$
        """
    )
    op.execute("DROP INDEX IF EXISTS idx_qc_app_publication_work_item")
    op.execute("DROP INDEX IF EXISTS idx_qc_app_publication_rule_disposition")
    op.execute("DROP INDEX IF EXISTS idx_qc_app_publication_entry_status")
    op.execute("DROP INDEX IF EXISTS idx_qc_app_publication_task_epoch")
    op.execute("DROP INDEX IF EXISTS idx_qc_app_publication_claim")
    op.execute("DROP TABLE qc_app_inspection_publication_rule")
    op.execute("DROP TABLE qc_app_inspection_publication_work_item")
    op.execute("DROP TABLE qc_app_inspection_publication_entry")
    op.execute("DROP TABLE qc_app_inspection_publication")
