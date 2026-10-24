"""增加 V2 Journey、探索工作、证据请求、证据清单与事务 Outbox。

Revision ID: 20260803_0026
Revises: 20260801_0025
Create Date: 2026-08-03
"""

from __future__ import annotations

from alembic import op

revision = "20260803_0026"
down_revision = "20260801_0025"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE qc_journey_definition (
            definition_id varchar(128) NOT NULL,
            version integer NOT NULL,
            schema_version varchar(64) NOT NULL,
            name varchar(255) NOT NULL,
            description text,
            surface_kinds jsonb NOT NULL,
            content_hash varchar(128) NOT NULL,
            definition_payload jsonb NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (definition_id, version),
            CONSTRAINT uq_qc_journey_definition_content UNIQUE (
                definition_id, content_hash
            ),
            CONSTRAINT ck_qc_journey_definition_version CHECK (version > 0),
            CONSTRAINT ck_qc_journey_definition_payload CHECK (
                jsonb_typeof(definition_payload) = 'object'
            ),
            CONSTRAINT ck_qc_journey_definition_surfaces CHECK (
                jsonb_typeof(surface_kinds) = 'array'
                AND jsonb_array_length(surface_kinds) > 0
            )
        )
        """
    )
    op.execute(
        """
        CREATE TABLE qc_journey_execution_plan (
            execution_plan_id varchar(96) PRIMARY KEY,
            task_id uuid NOT NULL REFERENCES qc_task(id) ON DELETE CASCADE,
            definition_id varchar(128) NOT NULL,
            definition_version integer NOT NULL,
            definition_content_hash varchar(128) NOT NULL,
            schema_version varchar(64) NOT NULL,
            plan_payload jsonb NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT uq_qc_journey_execution_plan_task UNIQUE (task_id),
            CONSTRAINT fk_qc_journey_execution_plan_definition FOREIGN KEY (
                definition_id, definition_version
            ) REFERENCES qc_journey_definition (
                definition_id, version
            ) ON DELETE RESTRICT,
            CONSTRAINT ck_qc_journey_execution_plan_payload CHECK (
                jsonb_typeof(plan_payload) = 'object'
            )
        )
        """
    )
    op.execute(
        """
        CREATE TABLE qc_evidence_request (
            request_id varchar(96) PRIMARY KEY,
            requirement_hash varchar(128) NOT NULL,
            payload_hash varchar(128) NOT NULL,
            protocol_version varchar(64) NOT NULL,
            work_item_id uuid NOT NULL
                REFERENCES qc_inspection_work_item(id) ON DELETE CASCADE,
            attempt_id uuid NOT NULL
                REFERENCES qc_inspection_attempt(id) ON DELETE CASCADE,
            task_id uuid NOT NULL REFERENCES qc_task(id) ON DELETE CASCADE,
            rule_id uuid NOT NULL REFERENCES qc_rule(id) ON DELETE RESTRICT,
            rule_code varchar(128) NOT NULL,
            target_capture_id uuid NOT NULL
                REFERENCES qc_page_capture(id) ON DELETE RESTRICT,
            session_dependency varchar(32) NOT NULL,
            replay_policy varchar(64) NOT NULL,
            required_equivalence varchar(64) NOT NULL,
            side_effect_policy varchar(64) NOT NULL,
            minimum_source_grade varchar(8) NOT NULL,
            request_payload jsonb NOT NULL,
            status varchar(32) NOT NULL DEFAULT 'pending',
            resolution_manifest_id varchar(96),
            resolution_reason text,
            expires_at timestamptz NOT NULL,
            row_version bigint NOT NULL DEFAULT 0,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            completed_at timestamptz,
            CONSTRAINT uq_qc_evidence_request_requirement UNIQUE (
                target_capture_id, rule_id, requirement_hash
            ),
            CONSTRAINT ck_qc_evidence_request_status CHECK (
                status IN (
                    'pending', 'routed', 'running', 'satisfied',
                    'unfulfillable', 'expired', 'cancelled'
                )
            ),
            CONSTRAINT ck_qc_evidence_request_session CHECK (
                session_dependency IN ('snapshot', 'active_session')
            ),
            CONSTRAINT ck_qc_evidence_request_replay CHECK (
                replay_policy IN (
                    'same_surface_only', 'same_context_allowed',
                    'new_session_allowed', 'forbidden'
                )
            ),
            CONSTRAINT ck_qc_evidence_request_equivalence CHECK (
                required_equivalence IN (
                    'original_atomic', 'same_surface_continuation',
                    'logical_equivalent'
                )
            ),
            CONSTRAINT ck_qc_evidence_request_side_effect CHECK (
                side_effect_policy IN (
                    'read_only', 'isolated_test_data',
                    'approval_required', 'prohibited'
                )
            ),
            CONSTRAINT ck_qc_evidence_request_grade CHECK (
                minimum_source_grade IN ('A0', 'A1', 'B', 'C', 'D')
            ),
            CONSTRAINT ck_qc_evidence_request_payload CHECK (
                jsonb_typeof(request_payload) = 'object'
            ),
            CONSTRAINT ck_qc_evidence_request_row_version CHECK (
                row_version >= 0
            ),
            CONSTRAINT ck_qc_evidence_request_completed CHECK (
                (
                    status IN (
                        'satisfied', 'unfulfillable', 'expired', 'cancelled'
                    )
                    AND completed_at IS NOT NULL
                )
                OR (
                    status NOT IN (
                        'satisfied', 'unfulfillable', 'expired', 'cancelled'
                    )
                    AND completed_at IS NULL
                )
            )
        )
        """
    )
    op.execute(
        """
        CREATE TABLE qc_v2_work_item (
            work_id varchar(96) PRIMARY KEY,
            work_type varchar(32) NOT NULL,
            task_id uuid NOT NULL REFERENCES qc_task(id) ON DELETE CASCADE,
            session_id varchar(128) NOT NULL,
            branch_id varchar(128) NOT NULL,
            source_capture_id uuid NOT NULL
                REFERENCES qc_page_capture(id) ON DELETE RESTRICT,
            evidence_request_id varchar(96)
                REFERENCES qc_evidence_request(request_id) ON DELETE CASCADE,
            protocol_version varchar(64) NOT NULL,
            payload_hash varchar(128) NOT NULL,
            work_payload jsonb NOT NULL,
            status varchar(32) NOT NULL DEFAULT 'pending',
            priority integer NOT NULL DEFAULT 100,
            attempt_count integer NOT NULL DEFAULT 0,
            max_attempts integer NOT NULL DEFAULT 3,
            next_attempt_at timestamptz NOT NULL DEFAULT now(),
            deadline timestamptz NOT NULL,
            lease_owner varchar(255),
            lease_token uuid,
            lease_expires_at timestamptz,
            row_version bigint NOT NULL DEFAULT 0,
            terminal_payload jsonb,
            last_error_code varchar(128),
            last_error_message text,
            started_at timestamptz,
            finished_at timestamptz,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT ck_qc_v2_work_item_type CHECK (
                work_type IN ('exploration', 'probe')
            ),
            CONSTRAINT ck_qc_v2_work_item_probe_request CHECK (
                (work_type = 'probe' AND evidence_request_id IS NOT NULL)
                OR (work_type = 'exploration' AND evidence_request_id IS NULL)
            ),
            CONSTRAINT ck_qc_v2_work_item_status CHECK (
                status IN (
                    'pending', 'leased', 'running', 'retry_wait',
                    'succeeded', 'bounded', 'budget_exhausted', 'blocked',
                    'no_progress', 'failed', 'cancelled', 'superseded'
                )
            ),
            CONSTRAINT ck_qc_v2_work_item_attempts CHECK (
                attempt_count >= 0
                AND max_attempts > 0
                AND attempt_count <= max_attempts
            ),
            CONSTRAINT ck_qc_v2_work_item_priority CHECK (priority >= 0),
            CONSTRAINT ck_qc_v2_work_item_row_version CHECK (row_version >= 0),
            CONSTRAINT ck_qc_v2_work_item_payload CHECK (
                jsonb_typeof(work_payload) = 'object'
                AND (
                    terminal_payload IS NULL
                    OR jsonb_typeof(terminal_payload) = 'object'
                )
            ),
            CONSTRAINT ck_qc_v2_work_item_lease CHECK (
                (
                    status IN ('leased', 'running')
                    AND lease_owner IS NOT NULL
                    AND lease_token IS NOT NULL
                    AND lease_expires_at IS NOT NULL
                )
                OR (
                    status NOT IN ('leased', 'running')
                    AND lease_owner IS NULL
                    AND lease_token IS NULL
                    AND lease_expires_at IS NULL
                )
            ),
            CONSTRAINT ck_qc_v2_work_item_finished CHECK (
                (
                    status IN (
                        'succeeded', 'bounded', 'budget_exhausted', 'blocked',
                        'no_progress', 'failed', 'cancelled', 'superseded'
                    )
                    AND finished_at IS NOT NULL
                )
                OR (
                    status NOT IN (
                        'succeeded', 'bounded', 'budget_exhausted', 'blocked',
                        'no_progress', 'failed', 'cancelled', 'superseded'
                    )
                    AND finished_at IS NULL
                )
            )
        )
        """
    )
    op.execute(
        """
        CREATE TABLE qc_evidence_manifest (
            manifest_id varchar(96) PRIMARY KEY,
            manifest_version integer NOT NULL,
            task_id uuid NOT NULL REFERENCES qc_task(id) ON DELETE CASCADE,
            session_id varchar(128) NOT NULL,
            capture_id uuid NOT NULL
                REFERENCES qc_page_capture(id) ON DELETE CASCADE,
            protocol_version varchar(64) NOT NULL,
            manifest_hash varchar(128) NOT NULL,
            payload_hash varchar(128) NOT NULL,
            quality_status varchar(32) NOT NULL,
            quality_reason text,
            manifest_payload jsonb NOT NULL,
            created_at timestamptz NOT NULL,
            persisted_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT uq_qc_evidence_manifest_version UNIQUE (
                capture_id, manifest_version
            ),
            CONSTRAINT uq_qc_evidence_manifest_hash UNIQUE (
                capture_id, manifest_hash
            ),
            CONSTRAINT ck_qc_evidence_manifest_version CHECK (
                manifest_version > 0
            ),
            CONSTRAINT ck_qc_evidence_manifest_quality CHECK (
                quality_status IN ('pending', 'pass', 'fail')
            ),
            CONSTRAINT ck_qc_evidence_manifest_reason CHECK (
                quality_status <> 'fail' OR quality_reason IS NOT NULL
            ),
            CONSTRAINT ck_qc_evidence_manifest_payload CHECK (
                jsonb_typeof(manifest_payload) = 'object'
            )
        )
        """
    )
    op.execute(
        """
        ALTER TABLE qc_evidence_request
        ADD CONSTRAINT fk_qc_evidence_request_resolution_manifest
        FOREIGN KEY (resolution_manifest_id)
        REFERENCES qc_evidence_manifest(manifest_id)
        ON DELETE SET NULL
        """
    )
    op.execute(
        """
        CREATE TABLE qc_outbox_event (
            message_id varchar(96) PRIMARY KEY,
            task_id uuid NOT NULL REFERENCES qc_task(id) ON DELETE CASCADE,
            aggregate_type varchar(64) NOT NULL,
            aggregate_id varchar(128) NOT NULL,
            message_type varchar(128) NOT NULL,
            protocol_version varchar(64) NOT NULL,
            payload_protocol_version varchar(64) NOT NULL,
            idempotency_key varchar(255) NOT NULL,
            envelope_payload jsonb NOT NULL,
            occurred_at timestamptz NOT NULL,
            status varchar(32) NOT NULL DEFAULT 'pending',
            attempt_count integer NOT NULL DEFAULT 0,
            max_attempts integer NOT NULL DEFAULT 12,
            next_attempt_at timestamptz NOT NULL DEFAULT now(),
            lease_owner varchar(255),
            lease_token uuid,
            lease_expires_at timestamptz,
            last_error text,
            published_at timestamptz,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT uq_qc_outbox_event_idempotency UNIQUE (
                message_type, idempotency_key
            ),
            CONSTRAINT ck_qc_outbox_event_status CHECK (
                status IN (
                    'pending', 'publishing', 'retry_wait',
                    'published', 'dead_letter'
                )
            ),
            CONSTRAINT ck_qc_outbox_event_attempts CHECK (
                attempt_count >= 0
                AND max_attempts > 0
                AND attempt_count <= max_attempts
            ),
            CONSTRAINT ck_qc_outbox_event_payload CHECK (
                jsonb_typeof(envelope_payload) = 'object'
            ),
            CONSTRAINT ck_qc_outbox_event_lease CHECK (
                (
                    status = 'publishing'
                    AND lease_owner IS NOT NULL
                    AND lease_token IS NOT NULL
                    AND lease_expires_at IS NOT NULL
                )
                OR (
                    status <> 'publishing'
                    AND lease_owner IS NULL
                    AND lease_token IS NULL
                    AND lease_expires_at IS NULL
                )
            ),
            CONSTRAINT ck_qc_outbox_event_published CHECK (
                (status = 'published' AND published_at IS NOT NULL)
                OR (status <> 'published' AND published_at IS NULL)
            )
        )
        """
    )

    op.execute(
        """
        CREATE INDEX idx_qc_journey_definition_latest
        ON qc_journey_definition (definition_id, version DESC)
        """
    )
    op.execute(
        """
        CREATE INDEX idx_qc_evidence_request_task_status
        ON qc_evidence_request (task_id, status, created_at)
        """
    )
    op.execute(
        """
        CREATE INDEX idx_qc_evidence_request_expiry
        ON qc_evidence_request (expires_at)
        WHERE status IN ('pending', 'routed', 'running')
        """
    )
    op.execute(
        """
        CREATE INDEX idx_qc_v2_work_item_claim
        ON qc_v2_work_item (work_type, priority, next_attempt_at, created_at)
        WHERE status IN ('pending', 'retry_wait')
        """
    )
    op.execute(
        """
        CREATE INDEX idx_qc_v2_work_item_expired_lease
        ON qc_v2_work_item (lease_expires_at)
        WHERE status IN ('leased', 'running')
        """
    )
    op.execute(
        """
        CREATE INDEX idx_qc_v2_work_item_task_status
        ON qc_v2_work_item (task_id, status, created_at)
        """
    )
    op.execute(
        """
        CREATE INDEX idx_qc_evidence_manifest_capture
        ON qc_evidence_manifest (capture_id, manifest_version DESC)
        """
    )
    op.execute(
        """
        CREATE INDEX idx_qc_outbox_event_claim
        ON qc_outbox_event (next_attempt_at, occurred_at, message_id)
        WHERE status IN ('pending', 'retry_wait')
        """
    )
    op.execute(
        """
        CREATE INDEX idx_qc_outbox_event_expired_lease
        ON qc_outbox_event (lease_expires_at)
        WHERE status = 'publishing'
        """
    )
    op.execute(
        """
        CREATE INDEX idx_qc_outbox_event_task_status
        ON qc_outbox_event (task_id, status, created_at)
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS qc_outbox_event")
    op.execute("ALTER TABLE qc_evidence_request DROP CONSTRAINT IF EXISTS fk_qc_evidence_request_resolution_manifest")
    op.execute("DROP TABLE IF EXISTS qc_evidence_manifest")
    op.execute("DROP TABLE IF EXISTS qc_v2_work_item")
    op.execute("DROP TABLE IF EXISTS qc_evidence_request")
    op.execute("DROP TABLE IF EXISTS qc_journey_execution_plan")
    op.execute("DROP TABLE IF EXISTS qc_journey_definition")
