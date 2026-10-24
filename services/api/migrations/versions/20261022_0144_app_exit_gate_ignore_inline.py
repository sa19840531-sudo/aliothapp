"""App Evidence Exit Gate：pending inline 补证不阻断课纲离场。"""

from __future__ import annotations

from alembic import op

revision = "20261022_0144"
down_revision = "20261022_0143"
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
    """App 探索与规则 inline 补证并行；未结清不得拦后续导航。"""

    op.execute(
        """
        ALTER TABLE qc_evidence_exit_gate_evaluation
        DROP CONSTRAINT IF EXISTS ck_qc_evidence_exit_gate_formula
        """
    )
    # 生产库可能已有 App 行按旧公式因 inline 补证被阻断。
    # 新公式允许离场时，不能把 blocked 行直接改成 allow_leave=true（会撞 lifecycle）。
    _NEW_ALLOW_LEAVE = f"""(
        (
            baseline_pass_manifest_count > 0
            OR {_APP_EMPTY_BASELINE}
        )
        AND (
            requirement_set_id IS NOT NULL
            OR {_APP_SURFACE}
        )
        AND nonterminal_active_session_requirement_count = 0
        AND (
            nonterminal_inline_evidence_request_count = 0
            OR {_APP_SURFACE}
        )
        AND evidence_commit_in_progress_count = 0
    )"""
    _REBUILT_REASONS = f"""COALESCE(
        (
            SELECT jsonb_agg(to_jsonb(reason.reason) ORDER BY reason.ordinality)
            FROM (
                SELECT reason.reason, reason.ordinality
                FROM unnest(
                    ARRAY[
                        CASE
                            WHEN baseline_pass_manifest_count = 0
                                 AND NOT {_APP_EMPTY_BASELINE}
                            THEN 'baseline_evidence_incomplete'
                        END,
                        CASE
                            WHEN nonterminal_active_session_requirement_count > 0
                                 OR (
                                     requirement_set_id IS NULL
                                     AND NOT {_APP_SURFACE}
                                 )
                            THEN 'active_session_requirements_nonterminal'
                        END,
                        CASE
                            WHEN evidence_commit_in_progress_count > 0
                            THEN 'evidence_commit_in_progress'
                        END
                    ]
                ) WITH ORDINALITY AS reason(reason, ordinality)
                WHERE reason.reason IS NOT NULL
            ) AS reason
        ),
        '[]'::jsonb
    )"""
    op.execute(
        f"""
        UPDATE qc_evidence_exit_gate_evaluation
        SET
            allow_leave = true,
            block_reasons = '[]'::jsonb,
            reservation_status = 'cancelled',
            expires_at = COALESCE(expires_at, finalized_at, evaluated_at),
            business_outcome = NULL,
            outcome_payload = NULL
        WHERE {_APP_SURFACE}
          AND reservation_status = 'blocked'
          AND {_NEW_ALLOW_LEAVE}
        """
    )
    op.execute(
        f"""
        UPDATE qc_evidence_exit_gate_evaluation
        SET
            allow_leave = false,
            block_reasons = {_REBUILT_REASONS}
        WHERE {_APP_SURFACE}
          AND reservation_status = 'blocked'
          AND NOT {_NEW_ALLOW_LEAVE}
        """
    )
    op.execute(
        f"""
        UPDATE qc_evidence_exit_gate_evaluation
        SET block_reasons = block_reasons - 'inline_evidence_request_pending'
        WHERE {_APP_SURFACE}
          AND reservation_status <> 'blocked'
          AND block_reasons ? 'inline_evidence_request_pending'
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
                AND (
                    nonterminal_inline_evidence_request_count = 0
                    OR {_APP_SURFACE}
                )
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
                (
                    nonterminal_inline_evidence_request_count > 0
                    AND NOT {_APP_SURFACE}
                )
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
    """恢复 0072：App 仍要求 inline 补证结清才能离场。"""

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
