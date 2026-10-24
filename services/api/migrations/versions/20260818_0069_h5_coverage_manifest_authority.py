"""建立 H5 覆盖快照并绑定 PASS Manifest 权威。

Revision ID: 20260818_0069
Revises: 20260818_0068
Create Date: 2026-08-18
"""

from __future__ import annotations

from alembic import op

revision = "20260818_0069"
down_revision = "20260818_0068"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """历史 Inventory 显式标为 legacy；不伪造覆盖或 Manifest 通过事实。"""
    op.execute(
        """
        CREATE TABLE qc_h5_coverage_snapshot (
            coverage_snapshot_id varchar(160) PRIMARY KEY,
            task_id uuid NOT NULL REFERENCES qc_task(id) ON DELETE CASCADE,
            agent_run_id uuid NOT NULL,
            finalization_epoch integer NOT NULL,
            revision integer NOT NULL,
            authority_source varchar(64) NOT NULL,
            required_page_types jsonb NOT NULL,
            covered_page_types jsonb NOT NULL,
            missing_page_types jsonb NOT NULL,
            coverage_scopes jsonb NOT NULL,
            assessment_scope_ready boolean NOT NULL,
            frontier_terminal boolean NOT NULL,
            completion_ready boolean NOT NULL,
            schema_version varchar(64) NOT NULL,
            payload_hash varchar(128) NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT fk_qc_h5_coverage_agent_run
                FOREIGN KEY (task_id, agent_run_id)
                REFERENCES qc_agent_run(task_id, id) ON DELETE CASCADE,
            CONSTRAINT uq_qc_h5_coverage_revision
                UNIQUE (task_id, agent_run_id, finalization_epoch, revision),
            CONSTRAINT ck_qc_h5_coverage_revision CHECK (revision > 0),
            CONSTRAINT ck_qc_h5_coverage_epoch CHECK (finalization_epoch >= 0),
            CONSTRAINT ck_qc_h5_coverage_authority CHECK (
                authority_source = 'h5_page_type_assessment'
            ),
            CONSTRAINT ck_qc_h5_coverage_json CHECK (
                jsonb_typeof(required_page_types) = 'array'
                AND jsonb_typeof(covered_page_types) = 'array'
                AND jsonb_typeof(missing_page_types) = 'array'
                AND jsonb_typeof(coverage_scopes) = 'array'
            )
        )
        """
    )
    op.execute(
        """
        ALTER TABLE qc_completion_gate_snapshot
        DROP CONSTRAINT IF EXISTS ck_qc_completion_gate_formula
        """
    )
    op.execute(
        """
        ALTER TABLE qc_completion_gate_snapshot
        ADD COLUMN h5_coverage_satisfied boolean NOT NULL DEFAULT true
        """
    )
    op.execute(
        """
        ALTER TABLE qc_completion_gate_snapshot
        ADD CONSTRAINT ck_qc_completion_gate_formula CHECK (
            gate_passed = (
                exploration_terminal
                AND no_pending_action
                AND no_pending_capture_commit
                AND no_nonterminal_enforced_work_item
                AND no_active_session_request
                AND candidate_rule_coverage_terminal
                AND no_background_execution
                AND review_policy_satisfied
                AND candidate_inventory_satisfied
                AND h5_coverage_satisfied
            )
        )
        """
    )
    op.execute(
        """
        CREATE INDEX idx_qc_h5_coverage_current
        ON qc_h5_coverage_snapshot (
            task_id, agent_run_id, finalization_epoch, revision DESC
        )
        """
    )
    op.execute(
        """
        ALTER TABLE qc_h5_evidence_inventory_item
        ADD COLUMN before_manifest_id varchar(96),
        ADD COLUMN before_manifest_hash varchar(128),
        ADD COLUMN after_manifest_id varchar(96),
        ADD COLUMN after_manifest_hash varchar(128),
        ADD COLUMN authority_source varchar(64) NOT NULL
            DEFAULT 'legacy_object_verified',
        ADD CONSTRAINT fk_qc_h5_inventory_before_manifest
            FOREIGN KEY (before_manifest_id)
            REFERENCES qc_evidence_manifest(manifest_id) ON DELETE RESTRICT,
        ADD CONSTRAINT fk_qc_h5_inventory_after_manifest
            FOREIGN KEY (after_manifest_id)
            REFERENCES qc_evidence_manifest(manifest_id) ON DELETE RESTRICT,
        ADD CONSTRAINT ck_qc_h5_inventory_authority CHECK (
            authority_source IN (
                'pass_manifest', 'manifest_unverified', 'legacy_object_verified'
            )
        ),
        ADD CONSTRAINT ck_qc_h5_inventory_manifest_pair CHECK (
            authority_source <> 'pass_manifest'
            OR (
                before_manifest_id IS NOT NULL
                AND before_manifest_hash IS NOT NULL
                AND after_manifest_id IS NOT NULL
                AND after_manifest_hash IS NOT NULL
            )
        )
        """
    )


def downgrade() -> None:
    """仅在没有 coverage 和新权威 Inventory 时允许回退。"""
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM qc_h5_coverage_snapshot) THEN
                RAISE EXCEPTION
                    'qc_h5_coverage_snapshot contains rows; refusing destructive downgrade';
            END IF;
            IF EXISTS (
                SELECT 1 FROM qc_h5_evidence_inventory_item
                WHERE authority_source = 'pass_manifest'
            ) THEN
                RAISE EXCEPTION
                    'manifest-backed H5 inventory exists; refusing destructive downgrade';
            END IF;
        END
        $$
        """
    )
    op.execute(
        """
        ALTER TABLE qc_completion_gate_snapshot
        DROP CONSTRAINT IF EXISTS ck_qc_completion_gate_formula
        """
    )
    op.execute(
        """
        ALTER TABLE qc_completion_gate_snapshot
        DROP COLUMN h5_coverage_satisfied
        """
    )
    op.execute(
        """
        ALTER TABLE qc_completion_gate_snapshot
        ADD CONSTRAINT ck_qc_completion_gate_formula CHECK (
            gate_passed = (
                exploration_terminal
                AND no_pending_action
                AND no_pending_capture_commit
                AND no_nonterminal_enforced_work_item
                AND no_active_session_request
                AND candidate_rule_coverage_terminal
                AND no_background_execution
                AND review_policy_satisfied
                AND candidate_inventory_satisfied
            )
        )
        """
    )
    op.execute(
        """
        ALTER TABLE qc_h5_evidence_inventory_item
        DROP CONSTRAINT ck_qc_h5_inventory_manifest_pair,
        DROP CONSTRAINT ck_qc_h5_inventory_authority,
        DROP CONSTRAINT fk_qc_h5_inventory_after_manifest,
        DROP CONSTRAINT fk_qc_h5_inventory_before_manifest,
        DROP COLUMN authority_source,
        DROP COLUMN after_manifest_hash,
        DROP COLUMN after_manifest_id,
        DROP COLUMN before_manifest_hash,
        DROP COLUMN before_manifest_id
        """
    )
    op.execute("DROP INDEX idx_qc_h5_coverage_current")
    op.execute("DROP TABLE qc_h5_coverage_snapshot")
