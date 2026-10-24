"""增加 V2 恢复尝试、状态等价证明与重放补采挂接。

Revision ID: 20260803_0028
Revises: 20260803_0027
Create Date: 2026-08-03
"""

from __future__ import annotations

from alembic import op

revision = "20260803_0028"
down_revision = "20260803_0027"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE qc_restoration_attempt (
            attempt_id varchar(96) PRIMARY KEY,
            plan_id varchar(96) NOT NULL,
            plan_hash varchar(128) NOT NULL,
            plan_protocol_version varchar(64) NOT NULL,
            plan_payload jsonb NOT NULL,
            request_id varchar(96) NOT NULL
                REFERENCES qc_evidence_request(request_id) ON DELETE CASCADE,
            task_id uuid NOT NULL REFERENCES qc_task(id) ON DELETE CASCADE,
            original_capture_id uuid NOT NULL
                REFERENCES qc_page_capture(id) ON DELETE RESTRICT,
            source_session_id varchar(128) NOT NULL,
            target_session_id varchar(128) NOT NULL,
            surface_kind varchar(32) NOT NULL,
            strategy varchar(64) NOT NULL,
            replay_execution_plan_id varchar(96)
                REFERENCES qc_journey_execution_plan(execution_plan_id)
                ON DELETE RESTRICT,
            attempt_no integer NOT NULL,
            status varchar(32) NOT NULL DEFAULT 'running',
            source_grade varchar(8) NOT NULL,
            restored_capture_id uuid
                REFERENCES qc_page_capture(id) ON DELETE RESTRICT,
            equivalence_assessment_id varchar(96),
            assessment_hash varchar(128),
            assessment_payload jsonb,
            action_execution_ids jsonb NOT NULL DEFAULT '[]'::jsonb,
            attempt_protocol_version varchar(64) NOT NULL,
            row_version bigint NOT NULL DEFAULT 0,
            error_code varchar(128),
            error_message text,
            started_at timestamptz NOT NULL,
            finished_at timestamptz,
            updated_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT uq_qc_restoration_attempt_order UNIQUE (
                plan_id, attempt_no
            ),
            CONSTRAINT uq_qc_restoration_attempt_assessment UNIQUE (
                equivalence_assessment_id
            ),
            CONSTRAINT uq_qc_restoration_attempt_assessment_owner UNIQUE (
                attempt_id, equivalence_assessment_id
            ),
            CONSTRAINT ck_qc_restoration_attempt_surface CHECK (
                surface_kind IN ('web', 'android', 'ios')
            ),
            CONSTRAINT ck_qc_restoration_attempt_strategy CHECK (
                strategy IN (
                    'original_surface_pin', 'same_context_new_surface',
                    'same_app_session', 'new_session_replay', 'unfulfillable'
                )
            ),
            CONSTRAINT ck_qc_restoration_attempt_platform_strategy CHECK (
                (
                    strategy <> 'same_context_new_surface'
                    OR surface_kind = 'web'
                )
                AND (
                    strategy <> 'same_app_session'
                    OR surface_kind IN ('android', 'ios')
                )
            ),
            CONSTRAINT ck_qc_restoration_attempt_status CHECK (
                status IN (
                    'running', 'equivalent', 'divergent',
                    'unfulfillable', 'failed'
                )
            ),
            CONSTRAINT ck_qc_restoration_attempt_grade CHECK (
                (strategy = 'original_surface_pin' AND source_grade = 'A1')
                OR (
                    strategy IN (
                        'same_context_new_surface', 'same_app_session'
                    )
                    AND source_grade = 'B'
                )
                OR (strategy = 'new_session_replay' AND source_grade = 'C')
                OR (strategy = 'unfulfillable' AND source_grade = 'D')
            ),
            CONSTRAINT ck_qc_restoration_attempt_numbers CHECK (
                attempt_no > 0 AND row_version >= 0
            ),
            CONSTRAINT ck_qc_restoration_attempt_payload CHECK (
                jsonb_typeof(plan_payload) = 'object'
                AND jsonb_typeof(action_execution_ids) = 'array'
                AND (
                    assessment_payload IS NULL
                    OR jsonb_typeof(assessment_payload) = 'object'
                )
            ),
            CONSTRAINT ck_qc_restoration_attempt_replay CHECK (
                (
                    strategy IN (
                        'same_context_new_surface', 'same_app_session',
                        'new_session_replay'
                    )
                    AND replay_execution_plan_id IS NOT NULL
                )
                OR (
                    strategy NOT IN (
                        'same_context_new_surface', 'same_app_session',
                        'new_session_replay'
                    )
                    AND replay_execution_plan_id IS NULL
                )
            ),
            CONSTRAINT ck_qc_restoration_attempt_session CHECK (
                (
                    strategy = 'new_session_replay'
                    AND source_session_id <> target_session_id
                )
                OR (
                    strategy <> 'new_session_replay'
                    AND source_session_id = target_session_id
                )
            ),
            CONSTRAINT ck_qc_restoration_attempt_terminal CHECK (
                (
                    status = 'running'
                    AND finished_at IS NULL
                    AND restored_capture_id IS NULL
                    AND equivalence_assessment_id IS NULL
                    AND assessment_hash IS NULL
                    AND assessment_payload IS NULL
                    AND error_code IS NULL
                    AND error_message IS NULL
                )
                OR (
                    status IN ('equivalent', 'divergent')
                    AND finished_at IS NOT NULL
                    AND restored_capture_id IS NOT NULL
                    AND equivalence_assessment_id IS NOT NULL
                    AND assessment_hash IS NOT NULL
                    AND assessment_payload IS NOT NULL
                    AND error_code IS NULL
                    AND error_message IS NULL
                    AND assessment_payload ? 'passed'
                    AND jsonb_typeof(assessment_payload->'passed') = 'boolean'
                    AND (assessment_payload->>'passed')::boolean
                        = (status = 'equivalent')
                )
                OR (
                    status IN ('unfulfillable', 'failed')
                    AND finished_at IS NOT NULL
                    AND equivalence_assessment_id IS NULL
                    AND assessment_hash IS NULL
                    AND assessment_payload IS NULL
                    AND error_code IS NOT NULL
                )
            ),
            CONSTRAINT ck_qc_restoration_attempt_time CHECK (
                finished_at IS NULL OR finished_at >= started_at
            )
        )
        """
    )
    op.execute(
        """
        ALTER TABLE qc_v2_work_item
        ADD COLUMN restoration_attempt_id varchar(96),
        ADD COLUMN equivalence_assessment_id varchar(96),
        ADD COLUMN source_grade varchar(8),
        ADD CONSTRAINT fk_qc_v2_work_item_restoration_assessment
            FOREIGN KEY (restoration_attempt_id, equivalence_assessment_id)
            REFERENCES qc_restoration_attempt (
                attempt_id, equivalence_assessment_id
            ) ON DELETE RESTRICT,
        ADD CONSTRAINT ck_qc_v2_work_item_source_grade CHECK (
            source_grade IS NULL OR source_grade IN ('A0', 'A1', 'B', 'C', 'D')
        ),
        ADD CONSTRAINT ck_qc_v2_work_item_probe_proof CHECK (
            (
                work_type = 'probe'
                AND restoration_attempt_id IS NOT NULL
                AND equivalence_assessment_id IS NOT NULL
                AND source_grade IS NOT NULL
            )
            OR (
                work_type = 'exploration'
                AND restoration_attempt_id IS NULL
                AND equivalence_assessment_id IS NULL
                AND source_grade IS NULL
            )
        ) NOT VALID
        """
    )
    op.execute(
        """
        CREATE TABLE qc_evidence_supplement (
            supplement_id varchar(96) PRIMARY KEY,
            task_id uuid NOT NULL REFERENCES qc_task(id) ON DELETE CASCADE,
            request_id varchar(96) NOT NULL
                REFERENCES qc_evidence_request(request_id) ON DELETE CASCADE,
            manifest_id varchar(96) NOT NULL
                REFERENCES qc_evidence_manifest(manifest_id) ON DELETE RESTRICT,
            probe_work_id varchar(96) NOT NULL
                REFERENCES qc_v2_work_item(work_id) ON DELETE RESTRICT,
            restoration_attempt_id varchar(96) NOT NULL,
            equivalence_assessment_id varchar(96) NOT NULL,
            source_grade varchar(8) NOT NULL,
            origin varchar(32) NOT NULL DEFAULT 'replay_probe',
            protocol_version varchar(64) NOT NULL,
            payload_hash varchar(128) NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT uq_qc_evidence_supplement_manifest UNIQUE (
                request_id, manifest_id
            ),
            CONSTRAINT uq_qc_evidence_supplement_probe UNIQUE (
                request_id, probe_work_id
            ),
            CONSTRAINT fk_qc_evidence_supplement_assessment
                FOREIGN KEY (
                    restoration_attempt_id, equivalence_assessment_id
                ) REFERENCES qc_restoration_attempt (
                    attempt_id, equivalence_assessment_id
                ) ON DELETE RESTRICT,
            CONSTRAINT ck_qc_evidence_supplement_grade CHECK (
                source_grade IN ('A1', 'B', 'C')
            ),
            CONSTRAINT ck_qc_evidence_supplement_origin CHECK (
                origin = 'replay_probe'
            )
        )
        """
    )
    op.execute(
        """
        CREATE INDEX idx_qc_restoration_attempt_request_status
        ON qc_restoration_attempt (request_id, status, started_at)
        """
    )
    op.execute(
        """
        CREATE INDEX idx_qc_restoration_attempt_task_status
        ON qc_restoration_attempt (task_id, status, started_at)
        """
    )
    op.execute(
        """
        CREATE INDEX idx_qc_v2_work_item_restoration
        ON qc_v2_work_item (restoration_attempt_id)
        WHERE restoration_attempt_id IS NOT NULL
        """
    )
    op.execute(
        """
        CREATE INDEX idx_qc_evidence_supplement_attempt
        ON qc_evidence_supplement (restoration_attempt_id, created_at)
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS qc_evidence_supplement")
    op.execute("DROP INDEX IF EXISTS idx_qc_v2_work_item_restoration")
    op.execute(
        """
        ALTER TABLE qc_v2_work_item
        DROP CONSTRAINT IF EXISTS ck_qc_v2_work_item_probe_proof,
        DROP CONSTRAINT IF EXISTS ck_qc_v2_work_item_source_grade,
        DROP CONSTRAINT IF EXISTS fk_qc_v2_work_item_restoration_assessment,
        DROP COLUMN IF EXISTS source_grade,
        DROP COLUMN IF EXISTS equivalence_assessment_id,
        DROP COLUMN IF EXISTS restoration_attempt_id
        """
    )
    op.execute("DROP TABLE IF EXISTS qc_restoration_attempt")
