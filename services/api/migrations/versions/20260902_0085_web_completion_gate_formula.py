"""将 Web Coverage 阻断事实纳入 Completion Gate 数据库公式。

0084 可能已经在环境中被标记为已应用，因此通过后续 revision 演进约束。降级使用
NOT VALID 恢复旧公式：保留新版本产生的历史审计快照，同时仍约束降级后的新增写入。
"""

from __future__ import annotations

from alembic import op

revision = "20260902_0085"
down_revision = "20260902_0084"
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
            )
        ) NOT VALID
        """
    )
