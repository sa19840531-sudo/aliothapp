"""App Completion Gate：禁止零规则库存空集默过。

Revision ID: 20260806_0041
Revises: 20260806_0040
"""

from alembic import op

revision = "20260806_0041"
down_revision = "20260806_0040"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE qc_completion_gate_snapshot
            ADD COLUMN require_nonempty_candidate_inventory boolean NOT NULL DEFAULT false,
            ADD COLUMN no_rules_applicable boolean NOT NULL DEFAULT false,
            ADD COLUMN candidate_inventory_satisfied boolean NOT NULL DEFAULT true
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
            ADD CONSTRAINT ck_qc_completion_gate_inventory_derived CHECK (
                candidate_inventory_satisfied = (
                    (NOT require_nonempty_candidate_inventory)
                    OR (candidate_rule_count > 0)
                    OR no_rules_applicable
                )
            )
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


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE qc_completion_gate_snapshot
            DROP CONSTRAINT IF EXISTS ck_qc_completion_gate_formula
        """
    )
    op.execute(
        """
        ALTER TABLE qc_completion_gate_snapshot
            DROP CONSTRAINT IF EXISTS ck_qc_completion_gate_inventory_derived
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
                )
            )
        """
    )
    op.execute(
        """
        ALTER TABLE qc_completion_gate_snapshot
            DROP COLUMN IF EXISTS candidate_inventory_satisfied,
            DROP COLUMN IF EXISTS no_rules_applicable,
            DROP COLUMN IF EXISTS require_nonempty_candidate_inventory
        """
    )
