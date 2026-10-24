"""增加 Evidence Exit Requirement Snapshot 与离场预留审计。

Revision ID: 20260803_0030
Revises: 20260803_0029
Create Date: 2026-08-03
"""

from __future__ import annotations

from alembic import op

revision = "20260803_0030"
down_revision = "20260803_0029"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE qc_evidence_exit_requirement_set (
            requirement_set_id varchar(96) PRIMARY KEY,
            revision integer NOT NULL,
            task_id uuid NOT NULL REFERENCES qc_task(id) ON DELETE CASCADE,
            session_id varchar(128) NOT NULL,
            capture_id uuid NOT NULL
                REFERENCES qc_page_capture(id) ON DELETE RESTRICT,
            matcher_snapshot_hash varchar(128) NOT NULL,
            protocol_version varchar(64) NOT NULL,
            payload_hash varchar(128) NOT NULL,
            requirement_count integer NOT NULL,
            nonterminal_count integer NOT NULL,
            requirement_payload jsonb NOT NULL,
            created_at timestamptz NOT NULL,
            persisted_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT fk_qc_evidence_exit_requirement_session FOREIGN KEY (
                task_id, session_id
            ) REFERENCES qc_surface_session (task_id, session_id)
                ON DELETE CASCADE,
            CONSTRAINT uq_qc_evidence_exit_requirement_revision UNIQUE (
                capture_id, revision
            ),
            CONSTRAINT ck_qc_evidence_exit_requirement_counts CHECK (
                revision > 0
                AND requirement_count >= 0
                AND nonterminal_count >= 0
                AND nonterminal_count <= requirement_count
            ),
            CONSTRAINT ck_qc_evidence_exit_requirement_payload CHECK (
                jsonb_typeof(requirement_payload) = 'object'
                AND jsonb_typeof(requirement_payload -> 'requirements') = 'array'
                AND requirement_count = jsonb_array_length(
                    requirement_payload -> 'requirements'
                )
                AND nonterminal_count = jsonb_array_length(
                    jsonb_path_query_array(
                        requirement_payload,
                        '$.requirements[*] ? (@.status == "pending")'
                    )
                )
            )
        )
        """
    )
    op.execute(
        """
        CREATE TABLE qc_evidence_exit_gate_evaluation (
            evaluation_id varchar(96) PRIMARY KEY,
            task_id uuid NOT NULL REFERENCES qc_task(id) ON DELETE CASCADE,
            session_id varchar(128) NOT NULL,
            source_capture_id uuid NOT NULL
                REFERENCES qc_page_capture(id) ON DELETE RESTRICT,
            action_intent_id varchar(128) NOT NULL,
            requirement_set_id varchar(96)
                REFERENCES qc_evidence_exit_requirement_set(requirement_set_id)
                ON DELETE RESTRICT,
            session_command_sequence bigint NOT NULL,
            finalization_epoch integer NOT NULL,
            baseline_pass_manifest_count integer NOT NULL,
            nonterminal_active_session_requirement_count integer NOT NULL,
            nonterminal_inline_evidence_request_count integer NOT NULL,
            evidence_commit_in_progress_count integer NOT NULL,
            allow_leave boolean NOT NULL,
            block_reasons jsonb NOT NULL,
            snapshot_payload jsonb NOT NULL,
            decision_payload jsonb NOT NULL,
            reservation_status varchar(32) NOT NULL,
            expires_at timestamptz,
            evaluated_at timestamptz NOT NULL,
            finalized_at timestamptz,
            row_version bigint NOT NULL DEFAULT 0,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT fk_qc_evidence_exit_gate_session FOREIGN KEY (
                task_id, session_id
            ) REFERENCES qc_surface_session (task_id, session_id)
                ON DELETE CASCADE,
            CONSTRAINT ck_qc_evidence_exit_gate_numbers CHECK (
                session_command_sequence > 0
                AND finalization_epoch >= 0
                AND baseline_pass_manifest_count >= 0
                AND nonterminal_active_session_requirement_count >= 0
                AND nonterminal_inline_evidence_request_count >= 0
                AND evidence_commit_in_progress_count >= 0
                AND row_version >= 0
            ),
            CONSTRAINT ck_qc_evidence_exit_gate_payload CHECK (
                jsonb_typeof(block_reasons) = 'array'
                AND jsonb_typeof(snapshot_payload) = 'object'
                AND jsonb_typeof(decision_payload) = 'object'
                AND block_reasons <@ '[
                    "baseline_evidence_incomplete",
                    "active_session_requirements_nonterminal",
                    "inline_evidence_request_pending",
                    "evidence_commit_in_progress"
                ]'::jsonb
                AND jsonb_array_length(
                    jsonb_path_query_array(
                        block_reasons,
                        '$[*] ? (@ == "baseline_evidence_incomplete")'
                    )
                ) <= 1
                AND jsonb_array_length(
                    jsonb_path_query_array(
                        block_reasons,
                        '$[*] ? (@ == "active_session_requirements_nonterminal")'
                    )
                ) <= 1
                AND jsonb_array_length(
                    jsonb_path_query_array(
                        block_reasons,
                        '$[*] ? (@ == "inline_evidence_request_pending")'
                    )
                ) <= 1
                AND jsonb_array_length(
                    jsonb_path_query_array(
                        block_reasons,
                        '$[*] ? (@ == "evidence_commit_in_progress")'
                    )
                ) <= 1
            ),
            CONSTRAINT ck_qc_evidence_exit_gate_formula CHECK (
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
            ),
            CONSTRAINT ck_qc_evidence_exit_gate_status CHECK (
                reservation_status IN (
                    'blocked', 'reserved', 'consumed', 'cancelled', 'expired'
                )
            ),
            CONSTRAINT ck_qc_evidence_exit_gate_lifecycle CHECK (
                (
                    reservation_status = 'blocked'
                    AND NOT allow_leave
                    AND expires_at IS NULL
                    AND finalized_at IS NOT NULL
                )
                OR (
                    reservation_status = 'reserved'
                    AND allow_leave
                    AND expires_at IS NOT NULL
                    AND finalized_at IS NULL
                )
                OR (
                    reservation_status IN ('consumed', 'cancelled', 'expired')
                    AND allow_leave
                    AND expires_at IS NOT NULL
                    AND finalized_at IS NOT NULL
                )
            )
        )
        """
    )
    op.execute(
        """
        CREATE INDEX idx_qc_evidence_exit_requirement_latest
        ON qc_evidence_exit_requirement_set (capture_id, revision DESC)
        """
    )
    op.execute(
        """
        CREATE INDEX idx_qc_evidence_exit_gate_action_audit
        ON qc_evidence_exit_gate_evaluation (
            session_id, action_intent_id, evaluated_at DESC
        )
        """
    )
    op.execute(
        """
        CREATE INDEX idx_qc_evidence_exit_gate_reservation
        ON qc_evidence_exit_gate_evaluation (
            source_capture_id, reservation_status, expires_at
        )
        WHERE reservation_status = 'reserved'
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS qc_evidence_exit_gate_evaluation")
    op.execute("DROP TABLE IF EXISTS qc_evidence_exit_requirement_set")
