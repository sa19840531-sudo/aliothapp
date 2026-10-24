"""闭合聚合规则候选、Observation Packet 与 RuleExecution 身份。

Revision ID: 20260818_0067
Revises: 20260818_0066
Create Date: 2026-08-18
"""

from __future__ import annotations

from alembic import op

revision = "20260818_0067"
down_revision = "20260818_0066"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """新增期望候选事实，并让聚合结果不再借用页面锚点。"""

    op.execute(
        """
        CREATE TABLE qc_aggregate_inspection_candidate (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            task_id uuid NOT NULL REFERENCES qc_task(id) ON DELETE CASCADE,
            source_agent_run_id uuid NOT NULL,
            rule_id uuid NOT NULL REFERENCES qc_rule(id) ON DELETE RESTRICT,
            rule_code varchar(128) NOT NULL,
            rule_contract_hash varchar(128) NOT NULL,
            scope varchar(32) NOT NULL,
            scope_key varchar(128) NOT NULL,
            finalization_epoch integer NOT NULL,
            status varchar(32) NOT NULL DEFAULT 'expected',
            expected_member_count integer NOT NULL DEFAULT 0,
            evidence_input_set_id varchar(96),
            work_item_id uuid,
            disposition_code varchar(128),
            disposition_message text,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            finished_at timestamptz,
            CONSTRAINT fk_qc_aggregate_candidate_run
                FOREIGN KEY (task_id, source_agent_run_id)
                REFERENCES qc_agent_run(task_id, id) ON DELETE NO ACTION
                DEFERRABLE INITIALLY DEFERRED,
            CONSTRAINT fk_qc_aggregate_candidate_input_set
                FOREIGN KEY (task_id, evidence_input_set_id)
                REFERENCES qc_evidence_input_set(task_id, input_set_id)
                ON DELETE NO ACTION DEFERRABLE INITIALLY DEFERRED,
            CONSTRAINT fk_qc_aggregate_candidate_work
                FOREIGN KEY (task_id, work_item_id)
                REFERENCES qc_aggregate_inspection_work_item(task_id, id)
                ON DELETE NO ACTION DEFERRABLE INITIALLY DEFERRED,
            CONSTRAINT uq_qc_aggregate_candidate_rule_epoch
                UNIQUE (task_id, rule_id, scope_key, finalization_epoch),
            CONSTRAINT ck_qc_aggregate_candidate_scope CHECK (
                scope IN ('journey', 'task', 'environment_matrix')
                AND scope_key ~ '^[a-z][a-z0-9_.:-]{0,127}$'
            ),
            CONSTRAINT ck_qc_aggregate_candidate_status CHECK (
                status IN (
                    'expected', 'published', 'completed', 'not_applicable',
                    'needs_review', 'failed'
                )
            ),
            CONSTRAINT ck_qc_aggregate_candidate_counts CHECK (
                finalization_epoch >= 0 AND expected_member_count >= 0
            ),
            CONSTRAINT ck_qc_aggregate_candidate_links CHECK (
                (status = 'published'
                    AND evidence_input_set_id IS NOT NULL
                    AND work_item_id IS NOT NULL
                    AND finished_at IS NULL)
                OR (status = 'completed'
                    AND evidence_input_set_id IS NOT NULL
                    AND work_item_id IS NOT NULL
                    AND finished_at IS NOT NULL)
                OR (status IN ('not_applicable', 'needs_review', 'failed')
                    AND finished_at IS NOT NULL)
                OR (status = 'expected' AND finished_at IS NULL)
            )
        )
        """
    )
    op.execute(
        """
        CREATE INDEX idx_qc_aggregate_candidate_gate
        ON qc_aggregate_inspection_candidate (
            task_id, finalization_epoch, status, rule_id
        )
        """
    )
    op.execute(
        """
        ALTER TABLE qc_observation_packet
        ADD COLUMN evidence_input_set_id varchar(96),
        ADD COLUMN aggregate_work_item_id uuid,
        ADD CONSTRAINT fk_qc_observation_packet_aggregate_input
            FOREIGN KEY (task_id, evidence_input_set_id)
            REFERENCES qc_evidence_input_set(task_id, input_set_id)
            ON DELETE NO ACTION DEFERRABLE INITIALLY DEFERRED,
        ADD CONSTRAINT fk_qc_observation_packet_aggregate_work
            FOREIGN KEY (task_id, aggregate_work_item_id)
            REFERENCES qc_aggregate_inspection_work_item(task_id, id)
            ON DELETE NO ACTION DEFERRABLE INITIALLY DEFERRED,
        ADD CONSTRAINT ck_qc_observation_packet_aggregate_identity CHECK (
            aggregate_work_item_id IS NULL
            OR (
                page_capture_id IS NULL
                AND evidence_input_set_id IS NOT NULL
            )
        )
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX uq_qc_observation_packet_aggregate_work
        ON qc_observation_packet (aggregate_work_item_id)
        WHERE aggregate_work_item_id IS NOT NULL
        """
    )
    op.execute(
        """
        ALTER TABLE qc_rule_execution
        ADD COLUMN evidence_input_set_id varchar(96),
        ADD COLUMN aggregate_work_item_id uuid,
        ADD CONSTRAINT fk_qc_rule_execution_aggregate_input
            FOREIGN KEY (task_id, evidence_input_set_id)
            REFERENCES qc_evidence_input_set(task_id, input_set_id)
            ON DELETE NO ACTION DEFERRABLE INITIALLY DEFERRED,
        ADD CONSTRAINT fk_qc_rule_execution_aggregate_work
            FOREIGN KEY (task_id, aggregate_work_item_id)
            REFERENCES qc_aggregate_inspection_work_item(task_id, id)
            ON DELETE NO ACTION DEFERRABLE INITIALLY DEFERRED,
        ADD CONSTRAINT ck_qc_rule_execution_aggregate_identity CHECK (
            aggregate_work_item_id IS NULL
            OR (
                page_capture_id IS NULL
                AND evidence_input_set_id IS NOT NULL
            )
        )
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX uq_qc_rule_execution_aggregate_work
        ON qc_rule_execution (aggregate_work_item_id)
        WHERE aggregate_work_item_id IS NOT NULL
        """
    )


def downgrade() -> None:
    """有聚合结果时拒绝丢失新身份列。"""

    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM qc_rule_execution
                WHERE aggregate_work_item_id IS NOT NULL
            ) OR EXISTS (
                SELECT 1 FROM qc_observation_packet
                WHERE aggregate_work_item_id IS NOT NULL
            ) OR EXISTS (
                SELECT 1 FROM qc_aggregate_inspection_candidate
            ) THEN
                RAISE EXCEPTION
                    'cannot downgrade aggregate runtime while aggregate facts exist';
            END IF;
        END;
        $$
        """
    )
    op.execute("DROP INDEX IF EXISTS uq_qc_rule_execution_aggregate_work")
    op.execute(
        "ALTER TABLE qc_rule_execution "
        "DROP CONSTRAINT IF EXISTS ck_qc_rule_execution_aggregate_identity, "
        "DROP CONSTRAINT IF EXISTS fk_qc_rule_execution_aggregate_work, "
        "DROP CONSTRAINT IF EXISTS fk_qc_rule_execution_aggregate_input, "
        "DROP COLUMN IF EXISTS aggregate_work_item_id, "
        "DROP COLUMN IF EXISTS evidence_input_set_id"
    )
    op.execute("DROP INDEX IF EXISTS uq_qc_observation_packet_aggregate_work")
    op.execute(
        "ALTER TABLE qc_observation_packet "
        "DROP CONSTRAINT IF EXISTS ck_qc_observation_packet_aggregate_identity, "
        "DROP CONSTRAINT IF EXISTS fk_qc_observation_packet_aggregate_work, "
        "DROP CONSTRAINT IF EXISTS fk_qc_observation_packet_aggregate_input, "
        "DROP COLUMN IF EXISTS aggregate_work_item_id, "
        "DROP COLUMN IF EXISTS evidence_input_set_id"
    )
    op.execute("DROP TABLE IF EXISTS qc_aggregate_inspection_candidate")
