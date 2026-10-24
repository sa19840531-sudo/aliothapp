"""增加 V2 Surface Session、Surface Registry 与 Action Execution 账本。

Revision ID: 20260803_0029
Revises: 20260803_0028
Create Date: 2026-08-03
"""

from __future__ import annotations

from alembic import op

revision = "20260803_0029"
down_revision = "20260803_0028"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE qc_surface_session (
            session_id varchar(128) PRIMARY KEY,
            task_id uuid NOT NULL REFERENCES qc_task(id) ON DELETE CASCADE,
            surface_kind varchar(32) NOT NULL,
            session_owner varchar(255) NOT NULL,
            status varchar(32) NOT NULL DEFAULT 'starting',
            command_sequence bigint NOT NULL DEFAULT 0,
            finalization_epoch integer NOT NULL DEFAULT 0,
            account_environment_ref varchar(255),
            platform_build_ref varchar(255),
            device_ref varchar(255),
            current_surface_ref varchar(128),
            current_capture_id uuid
                REFERENCES qc_page_capture(id) ON DELETE RESTRICT,
            lease_owner varchar(255),
            lease_token uuid,
            lease_expires_at timestamptz,
            row_version bigint NOT NULL DEFAULT 0,
            failure_reason text,
            started_at timestamptz NOT NULL DEFAULT now(),
            activated_at timestamptz,
            draining_at timestamptz,
            closed_at timestamptz,
            lost_at timestamptz,
            updated_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT uq_qc_surface_session_task UNIQUE (task_id, session_id),
            CONSTRAINT ck_qc_surface_session_kind CHECK (
                surface_kind IN ('web', 'android', 'ios')
            ),
            CONSTRAINT ck_qc_surface_session_status CHECK (
                status IN (
                    'starting', 'active', 'draining', 'closing',
                    'closed', 'lost', 'failed'
                )
            ),
            CONSTRAINT ck_qc_surface_session_numbers CHECK (
                command_sequence >= 0
                AND finalization_epoch >= 0
                AND row_version >= 0
            ),
            CONSTRAINT ck_qc_surface_session_lease CHECK (
                (
                    lease_owner IS NULL
                    AND lease_token IS NULL
                    AND lease_expires_at IS NULL
                )
                OR (
                    status IN ('active', 'draining')
                    AND lease_owner IS NOT NULL
                    AND lease_token IS NOT NULL
                    AND lease_expires_at IS NOT NULL
                )
            ),
            CONSTRAINT ck_qc_surface_session_lifecycle CHECK (
                (
                    status = 'starting'
                    AND activated_at IS NULL
                    AND draining_at IS NULL
                    AND closed_at IS NULL
                    AND lost_at IS NULL
                )
                OR (
                    status = 'active'
                    AND activated_at IS NOT NULL
                    AND draining_at IS NULL
                    AND closed_at IS NULL
                    AND lost_at IS NULL
                )
                OR (
                    status = 'draining'
                    AND activated_at IS NOT NULL
                    AND draining_at IS NOT NULL
                    AND closed_at IS NULL
                    AND lost_at IS NULL
                )
                OR (
                    status = 'closing'
                    AND activated_at IS NOT NULL
                    AND draining_at IS NOT NULL
                    AND closed_at IS NULL
                    AND lost_at IS NULL
                )
                OR (
                    status = 'closed'
                    AND activated_at IS NOT NULL
                    AND draining_at IS NOT NULL
                    AND closed_at IS NOT NULL
                    AND lost_at IS NULL
                )
                OR (
                    status = 'lost'
                    AND lost_at IS NOT NULL
                    AND closed_at IS NULL
                    AND failure_reason IS NOT NULL
                )
                OR (
                    status = 'failed'
                    AND closed_at IS NOT NULL
                    AND failure_reason IS NOT NULL
                )
            )
        )
        """
    )
    op.execute(
        """
        CREATE TABLE qc_surface_ref (
            surface_ref_id varchar(128) PRIMARY KEY,
            session_id varchar(128) NOT NULL,
            task_id uuid NOT NULL,
            surface_kind varchar(32) NOT NULL,
            surface_key varchar(255) NOT NULL,
            status varchar(32) NOT NULL DEFAULT 'active',
            current_capture_id uuid
                REFERENCES qc_page_capture(id) ON DELETE RESTRICT,
            pin_reason text,
            row_version bigint NOT NULL DEFAULT 0,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            released_at timestamptz,
            CONSTRAINT fk_qc_surface_ref_session FOREIGN KEY (task_id, session_id)
                REFERENCES qc_surface_session (task_id, session_id)
                ON DELETE CASCADE,
            CONSTRAINT uq_qc_surface_ref_key UNIQUE (session_id, surface_key),
            CONSTRAINT ck_qc_surface_ref_kind CHECK (
                surface_kind IN ('web', 'android', 'ios')
            ),
            CONSTRAINT ck_qc_surface_ref_status CHECK (
                status IN ('active', 'pinned', 'draining', 'released', 'lost')
            ),
            CONSTRAINT ck_qc_surface_ref_pin CHECK (
                (status = 'pinned' AND pin_reason IS NOT NULL)
                OR (status <> 'pinned' AND pin_reason IS NULL)
            ),
            CONSTRAINT ck_qc_surface_ref_version CHECK (row_version >= 0),
            CONSTRAINT ck_qc_surface_ref_terminal CHECK (
                (status IN ('released', 'lost') AND released_at IS NOT NULL)
                OR (status NOT IN ('released', 'lost') AND released_at IS NULL)
            )
        )
        """
    )
    op.execute(
        """
        ALTER TABLE qc_surface_session
        ADD CONSTRAINT fk_qc_surface_session_current_surface
        FOREIGN KEY (current_surface_ref)
        REFERENCES qc_surface_ref(surface_ref_id)
        ON DELETE SET NULL
        """
    )
    op.execute(
        """
        CREATE TABLE qc_action_execution (
            action_execution_id varchar(96) PRIMARY KEY,
            task_id uuid NOT NULL,
            session_id varchar(128) NOT NULL,
            intent_id varchar(128) NOT NULL,
            idempotency_key varchar(255) NOT NULL,
            intent_hash varchar(128) NOT NULL,
            intent_protocol_version varchar(64) NOT NULL,
            intent_payload jsonb NOT NULL,
            command_sequence bigint NOT NULL,
            finalization_epoch integer NOT NULL,
            requested_source_capture_id uuid NOT NULL
                REFERENCES qc_page_capture(id) ON DELETE RESTRICT,
            source_capture_id uuid
                REFERENCES qc_page_capture(id) ON DELETE RESTRICT,
            result_capture_id uuid
                REFERENCES qc_page_capture(id) ON DELETE RESTRICT,
            status varchar(32) NOT NULL DEFAULT 'planned',
            result_payload jsonb NOT NULL DEFAULT '{}'::jsonb,
            error_code varchar(128),
            error_message text,
            row_version bigint NOT NULL DEFAULT 0,
            created_at timestamptz NOT NULL DEFAULT now(),
            started_at timestamptz,
            action_finished_at timestamptz,
            evidence_finished_at timestamptz,
            updated_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT fk_qc_action_execution_session FOREIGN KEY (
                task_id, session_id
            ) REFERENCES qc_surface_session (task_id, session_id)
                ON DELETE CASCADE,
            CONSTRAINT uq_qc_action_execution_intent UNIQUE (
                session_id, intent_id
            ),
            CONSTRAINT uq_qc_action_execution_idempotency UNIQUE (
                session_id, idempotency_key
            ),
            CONSTRAINT uq_qc_action_execution_sequence UNIQUE (
                session_id, command_sequence
            ),
            CONSTRAINT ck_qc_action_execution_status CHECK (
                status IN (
                    'planned', 'executing', 'evidence_pending', 'completed',
                    'evidence_failed', 'blocked', 'failed', 'timed_out'
                )
            ),
            CONSTRAINT ck_qc_action_execution_numbers CHECK (
                command_sequence > 0
                AND finalization_epoch >= 0
                AND row_version >= 0
            ),
            CONSTRAINT ck_qc_action_execution_payload CHECK (
                jsonb_typeof(intent_payload) = 'object'
                AND jsonb_typeof(result_payload) = 'object'
            ),
            CONSTRAINT ck_qc_action_execution_state CHECK (
                (
                    status = 'planned'
                    AND source_capture_id IS NULL
                    AND result_capture_id IS NULL
                    AND started_at IS NULL
                    AND action_finished_at IS NULL
                    AND evidence_finished_at IS NULL
                    AND error_code IS NULL
                )
                OR (
                    status = 'executing'
                    AND source_capture_id IS NOT NULL
                    AND started_at IS NOT NULL
                    AND result_capture_id IS NULL
                    AND action_finished_at IS NULL
                    AND evidence_finished_at IS NULL
                    AND error_code IS NULL
                )
                OR (
                    status = 'evidence_pending'
                    AND source_capture_id IS NOT NULL
                    AND started_at IS NOT NULL
                    AND action_finished_at IS NOT NULL
                    AND result_capture_id IS NULL
                    AND evidence_finished_at IS NULL
                    AND error_code IS NULL
                )
                OR (
                    status = 'completed'
                    AND source_capture_id IS NOT NULL
                    AND result_capture_id IS NOT NULL
                    AND started_at IS NOT NULL
                    AND action_finished_at IS NOT NULL
                    AND evidence_finished_at IS NOT NULL
                    AND error_code IS NULL
                )
                OR (
                    status = 'evidence_failed'
                    AND source_capture_id IS NOT NULL
                    AND started_at IS NOT NULL
                    AND action_finished_at IS NOT NULL
                    AND evidence_finished_at IS NOT NULL
                    AND error_code IS NOT NULL
                )
                OR (
                    status IN ('blocked', 'timed_out')
                    AND source_capture_id IS NOT NULL
                    AND started_at IS NOT NULL
                    AND action_finished_at IS NOT NULL
                    AND evidence_finished_at IS NULL
                    AND error_code IS NOT NULL
                )
                OR (
                    status = 'failed'
                    AND action_finished_at IS NOT NULL
                    AND evidence_finished_at IS NULL
                    AND error_code IS NOT NULL
                    AND (
                        (
                            source_capture_id IS NULL
                            AND started_at IS NULL
                        )
                        OR (
                            source_capture_id IS NOT NULL
                            AND started_at IS NOT NULL
                        )
                    )
                )
            )
        )
        """
    )
    op.execute(
        """
        ALTER TABLE qc_page_capture
        ADD COLUMN surface_session_id varchar(128),
        ADD COLUMN capture_role varchar(128),
        ADD COLUMN action_execution_id varchar(96),
        ADD COLUMN session_command_sequence bigint,
        ADD CONSTRAINT fk_qc_page_capture_surface_session
            FOREIGN KEY (task_id, surface_session_id)
            REFERENCES qc_surface_session (task_id, session_id)
            ON DELETE RESTRICT,
        ADD CONSTRAINT fk_qc_page_capture_action_execution
            FOREIGN KEY (action_execution_id)
            REFERENCES qc_action_execution(action_execution_id)
            ON DELETE RESTRICT,
        ADD CONSTRAINT ck_qc_page_capture_v2_session CHECK (
            (
                surface_session_id IS NULL
                AND capture_role IS NULL
                AND action_execution_id IS NULL
                AND session_command_sequence IS NULL
            )
            OR (
                surface_session_id IS NOT NULL
                AND capture_role IS NOT NULL
                AND btrim(capture_role) <> ''
                AND session_command_sequence IS NOT NULL
                AND session_command_sequence > 0
            )
        ) NOT VALID
        """
    )
    op.execute(
        """
        CREATE INDEX idx_qc_surface_session_task_status
        ON qc_surface_session (task_id, status, updated_at)
        """
    )
    op.execute(
        """
        CREATE INDEX idx_qc_surface_session_expired_lease
        ON qc_surface_session (lease_expires_at)
        WHERE lease_token IS NOT NULL
        """
    )
    op.execute(
        """
        CREATE INDEX idx_qc_surface_ref_session_status
        ON qc_surface_ref (session_id, status, updated_at)
        """
    )
    op.execute(
        """
        CREATE INDEX idx_qc_action_execution_task_status
        ON qc_action_execution (task_id, status, created_at)
        """
    )
    op.execute(
        """
        CREATE INDEX idx_qc_page_capture_surface_session
        ON qc_page_capture (surface_session_id, session_command_sequence)
        WHERE surface_session_id IS NOT NULL
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_qc_page_capture_surface_session")
    op.execute(
        """
        ALTER TABLE qc_page_capture
        DROP CONSTRAINT IF EXISTS ck_qc_page_capture_v2_session,
        DROP CONSTRAINT IF EXISTS fk_qc_page_capture_action_execution,
        DROP CONSTRAINT IF EXISTS fk_qc_page_capture_surface_session,
        DROP COLUMN IF EXISTS session_command_sequence,
        DROP COLUMN IF EXISTS action_execution_id,
        DROP COLUMN IF EXISTS capture_role,
        DROP COLUMN IF EXISTS surface_session_id
        """
    )
    op.execute("DROP TABLE IF EXISTS qc_action_execution")
    op.execute(
        """
        ALTER TABLE qc_surface_session
        DROP CONSTRAINT IF EXISTS fk_qc_surface_session_current_surface
        """
    )
    op.execute("DROP TABLE IF EXISTS qc_surface_ref")
    op.execute("DROP TABLE IF EXISTS qc_surface_session")
