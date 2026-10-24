"""把展示层门禁违规纳入 Completion Gate 数据库公式。

Revision ID: 20260907_0096
Revises: 20260907_0095
Create Date: 2026-09-07

自然结论（pass/fail/warning）消费了被遮挡底页或诊断材料时，收口快照的
block_reasons 会带 natural_evidence_presentation_violation；数据库公式同步要求
gate_passed 为假。降级用 NOT VALID 恢复 0085 的公式，保留新版本产生的审计快照。
"""

from __future__ import annotations

from alembic import op

revision = "20260907_0096"
down_revision = "20260907_0095"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE qc_completion_gate_snapshot
        DROP CONSTRAINT IF EXISTS ck_qc_completion_gate_formula
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
                AND NOT (block_reasons ? 'web_coverage_snapshot_incomplete')
                AND NOT (block_reasons ? 'natural_evidence_presentation_violation')
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
                AND NOT (block_reasons ? 'web_coverage_snapshot_incomplete')
            )
        ) NOT VALID
        """
    )
