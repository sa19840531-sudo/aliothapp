"""将 App 目标覆盖与视觉质量纳入 Completion Gate。

Revision ID: 20260818_0070
Revises: 20260818_0069
Create Date: 2026-08-18
"""

from __future__ import annotations

from alembic import op

revision = "20260818_0070"
down_revision = "20260818_0069"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """历史 Gate 保持原值；新 Gate 必须写入真实 App 覆盖事实。"""
    op.execute(
        """
        ALTER TABLE qc_completion_gate_snapshot
        DROP CONSTRAINT IF EXISTS ck_qc_completion_gate_formula
        """
    )
    op.execute(
        """
        ALTER TABLE qc_completion_gate_snapshot
        ADD COLUMN app_coverage_satisfied boolean NOT NULL DEFAULT true
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
                AND app_coverage_satisfied
                AND h5_coverage_satisfied
            )
        )
        """
    )


def downgrade() -> None:
    """仅移除 Gate 投影列，不删除已经封存的目标或视觉事实。"""
    op.execute(
        """
        ALTER TABLE qc_completion_gate_snapshot
        DROP CONSTRAINT IF EXISTS ck_qc_completion_gate_formula
        """
    )
    op.execute(
        """
        ALTER TABLE qc_completion_gate_snapshot
        DROP COLUMN app_coverage_satisfied
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
