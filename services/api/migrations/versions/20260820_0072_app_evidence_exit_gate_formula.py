"""使 App Evidence Exit Gate 的数据库公式与领域公式保持一致。"""

from __future__ import annotations

from alembic import op

revision = "20260820_0072"
down_revision = "20260818_0071"
branch_labels = None
depends_on = None


_APP_SURFACE = (
    "(snapshot_payload->>'protocol_version' = 'evidence-exit-gate-snapshot.v3' "
    "AND COALESCE(snapshot_payload->>'surface_kind', '') IN ('android', 'ios'))"
)
_APP_EMPTY_BASELINE = (
    "("
    f"{_APP_SURFACE} "
    "AND COALESCE(snapshot_payload->>'baseline_manifest_total_count', '0') = '0'"
    ")"
)


def upgrade() -> None:
    """允许 v3 App 无需求快照时按领域公式创建离场预留。"""

    op.execute(
        """
        ALTER TABLE qc_evidence_exit_gate_evaluation
        DROP CONSTRAINT IF EXISTS ck_qc_evidence_exit_gate_formula
        """
    )
    op.execute(
        f"""
        ALTER TABLE qc_evidence_exit_gate_evaluation
        ADD CONSTRAINT ck_qc_evidence_exit_gate_formula CHECK (
            allow_leave = (
                (
                    baseline_pass_manifest_count > 0
                    OR {_APP_EMPTY_BASELINE}
                )
                AND (
                    requirement_set_id IS NOT NULL
                    OR {_APP_SURFACE}
                )
                AND nonterminal_active_session_requirement_count = 0
                AND nonterminal_inline_evidence_request_count = 0
                AND evidence_commit_in_progress_count = 0
            )
            AND (
                (allow_leave AND jsonb_array_length(block_reasons) = 0)
                OR (
                    NOT allow_leave
                    AND jsonb_array_length(block_reasons) > 0
                )
            )
            AND (
                (
                    baseline_pass_manifest_count = 0
                    AND NOT {_APP_EMPTY_BASELINE}
                )
                = (block_reasons ? 'baseline_evidence_incomplete')
            )
            AND (
                (
                    nonterminal_active_session_requirement_count > 0
                    OR (
                        requirement_set_id IS NULL
                        AND NOT {_APP_SURFACE}
                    )
                )
                = (block_reasons ? 'active_session_requirements_nonterminal')
            )
            AND (
                (nonterminal_inline_evidence_request_count > 0)
                = (block_reasons ? 'inline_evidence_request_pending')
            )
            AND (
                (evidence_commit_in_progress_count > 0)
                = (block_reasons ? 'evidence_commit_in_progress')
            )
        )
        """
    )


def downgrade() -> None:
    """恢复旧版只支持 Web 的严格四项公式。"""

    op.execute(
        """
        ALTER TABLE qc_evidence_exit_gate_evaluation
        DROP CONSTRAINT IF EXISTS ck_qc_evidence_exit_gate_formula
        """
    )
    op.execute(
        """
        ALTER TABLE qc_evidence_exit_gate_evaluation
        ADD CONSTRAINT ck_qc_evidence_exit_gate_formula CHECK (
            allow_leave = (
                baseline_pass_manifest_count > 0
                AND requirement_set_id IS NOT NULL
                AND nonterminal_active_session_requirement_count = 0
                AND nonterminal_inline_evidence_request_count = 0
                AND evidence_commit_in_progress_count = 0
            )
            AND (
                (allow_leave AND jsonb_array_length(block_reasons) = 0)
                OR (
                    NOT allow_leave
                    AND jsonb_array_length(block_reasons) > 0
                )
            )
            AND (
                (baseline_pass_manifest_count = 0)
                = (block_reasons ? 'baseline_evidence_incomplete')
            )
            AND (
                (
                    requirement_set_id IS NULL
                    OR nonterminal_active_session_requirement_count > 0
                )
                = (block_reasons ? 'active_session_requirements_nonterminal')
            )
            AND (
                (nonterminal_inline_evidence_request_count > 0)
                = (block_reasons ? 'inline_evidence_request_pending')
            )
            AND (
                (evidence_commit_in_progress_count > 0)
                = (block_reasons ? 'evidence_commit_in_progress')
            )
        )
        """
    )
