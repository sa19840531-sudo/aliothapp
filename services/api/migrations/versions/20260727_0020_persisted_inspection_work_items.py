"""增加页面质检持久化工作项、规则成员与判定尝试。

Revision ID: 20260727_0020
Revises: 20260725_0019
Create Date: 2026-07-27
"""

from __future__ import annotations

from alembic import op

revision = "20260727_0020"
down_revision = "20260725_0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE qc_inspection_work_item (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            task_id uuid NOT NULL REFERENCES qc_task(id) ON DELETE CASCADE,
            page_capture_id uuid NOT NULL
                REFERENCES qc_page_capture(id) ON DELETE CASCADE,
            producer_agent_run_id uuid
                REFERENCES qc_agent_run(id) ON DELETE SET NULL,
            consumer_agent_run_id uuid
                REFERENCES qc_agent_run(id) ON DELETE SET NULL,
            session_owner_agent_run_id uuid
                REFERENCES qc_agent_run(id) ON DELETE SET NULL,
            ruleset_id uuid NOT NULL REFERENCES qc_ruleset(id) ON DELETE RESTRICT,
            ruleset_version integer NOT NULL,
            ruleset_snapshot_hash varchar(128),
            protocol_version varchar(64) NOT NULL
                DEFAULT 'inspection-work-item.v1',
            batch_key varchar(255) NOT NULL,
            strategy varchar(32) NOT NULL,
            category varchar(32),
            session_dependency varchar(32) NOT NULL,
            input_hash varchar(128) NOT NULL,
            status varchar(32) NOT NULL DEFAULT 'pending',
            priority integer NOT NULL DEFAULT 100,
            attempt_count integer NOT NULL DEFAULT 0,
            max_attempts integer NOT NULL DEFAULT 3,
            next_attempt_at timestamptz NOT NULL DEFAULT now(),
            lease_owner varchar(255),
            lease_token uuid,
            lease_expires_at timestamptz,
            row_version bigint NOT NULL DEFAULT 0,
            last_error_code varchar(128),
            last_error_message text,
            started_at timestamptz,
            finished_at timestamptz,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT uq_qc_inspection_work_item_identity UNIQUE (
                task_id, page_capture_id, batch_key, input_hash
            ),
            CONSTRAINT ck_qc_inspection_work_item_strategy CHECK (
                strategy IN ('rule_engine', 'llm', 'hybrid')
            ),
            CONSTRAINT ck_qc_inspection_work_item_session CHECK (
                session_dependency IN ('snapshot', 'active_session')
            ),
            CONSTRAINT ck_qc_inspection_work_item_session_owner CHECK (
                session_dependency = 'snapshot'
                OR session_owner_agent_run_id IS NOT NULL
            ),
            CONSTRAINT ck_qc_inspection_work_item_category CHECK (
                category IS NULL
                OR category IN (
                    'text', 'visual', 'interaction', 'ad', 'payment',
                    'input_risk', 'player', 'compliance'
                )
            ),
            CONSTRAINT ck_qc_inspection_work_item_status CHECK (
                status IN (
                    'pending', 'leased', 'running', 'retry_wait',
                    'waiting_evidence', 'succeeded', 'partial', 'failed',
                    'cancelled', 'superseded'
                )
            ),
            CONSTRAINT ck_qc_inspection_work_item_attempts CHECK (
                attempt_count >= 0
                AND max_attempts > 0
                AND attempt_count <= max_attempts
            ),
            CONSTRAINT ck_qc_inspection_work_item_priority CHECK (
                priority >= 0
            ),
            CONSTRAINT ck_qc_inspection_work_item_ruleset_version CHECK (
                ruleset_version > 0
            ),
            CONSTRAINT ck_qc_inspection_work_item_row_version CHECK (
                row_version >= 0
            ),
            CONSTRAINT ck_qc_inspection_work_item_lease CHECK (
                (
                    status IN ('leased', 'running')
                    AND lease_owner IS NOT NULL
                    AND lease_token IS NOT NULL
                    AND lease_expires_at IS NOT NULL
                    AND consumer_agent_run_id IS NOT NULL
                )
                OR (
                    status NOT IN ('leased', 'running')
                    AND lease_owner IS NULL
                    AND lease_token IS NULL
                    AND lease_expires_at IS NULL
                )
            ),
            CONSTRAINT ck_qc_inspection_work_item_finished CHECK (
                (
                    status IN (
                        'succeeded', 'partial', 'failed',
                        'cancelled', 'superseded'
                    )
                    AND finished_at IS NOT NULL
                )
                OR (
                    status NOT IN (
                        'succeeded', 'partial', 'failed',
                        'cancelled', 'superseded'
                    )
                    AND finished_at IS NULL
                )
            )
        )
        """
    )
    op.execute(
        """
        CREATE TABLE qc_inspection_work_item_rule (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            work_item_id uuid NOT NULL
                REFERENCES qc_inspection_work_item(id) ON DELETE CASCADE,
            rule_id uuid NOT NULL REFERENCES qc_rule(id) ON DELETE RESTRICT,
            rule_code varchar(128) NOT NULL,
            ordinal integer NOT NULL,
            contract_hash varchar(128) NOT NULL,
            status varchar(32) NOT NULL DEFAULT 'pending',
            observation_packet_id uuid
                REFERENCES qc_observation_packet(id) ON DELETE SET NULL,
            rule_execution_id uuid
                REFERENCES qc_rule_execution(id) ON DELETE SET NULL,
            last_error_code varchar(128),
            last_error_message text,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT uq_qc_inspection_work_item_rule UNIQUE (
                work_item_id, rule_id
            ),
            CONSTRAINT uq_qc_inspection_work_item_rule_ordinal UNIQUE (
                work_item_id, ordinal
            ),
            CONSTRAINT ck_qc_inspection_work_item_rule_ordinal CHECK (
                ordinal >= 0
            ),
            CONSTRAINT ck_qc_inspection_work_item_rule_status CHECK (
                status IN (
                    'pending', 'judged', 'written',
                    'evidence_insufficient', 'error', 'skipped'
                )
            )
        )
        """
    )
    op.execute(
        """
        CREATE TABLE qc_inspection_attempt (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            work_item_id uuid NOT NULL
                REFERENCES qc_inspection_work_item(id) ON DELETE CASCADE,
            consumer_agent_run_id uuid
                REFERENCES qc_agent_run(id) ON DELETE SET NULL,
            attempt_no integer NOT NULL,
            lease_token uuid NOT NULL,
            status varchar(32) NOT NULL DEFAULT 'running',
            input_hash varchar(128) NOT NULL,
            result_payload jsonb NOT NULL DEFAULT '{}'::jsonb,
            error_code varchar(128),
            error_message text,
            started_at timestamptz NOT NULL DEFAULT now(),
            judged_at timestamptz,
            finished_at timestamptz,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT uq_qc_inspection_attempt_no UNIQUE (
                work_item_id, attempt_no
            ),
            CONSTRAINT ck_qc_inspection_attempt_no CHECK (
                attempt_no > 0
            ),
            CONSTRAINT ck_qc_inspection_attempt_status CHECK (
                status IN (
                    'running', 'judged', 'written',
                    'retryable_error', 'terminal_error', 'abandoned'
                )
            ),
            CONSTRAINT ck_qc_inspection_attempt_finished CHECK (
                (
                    status IN (
                        'written', 'retryable_error',
                        'terminal_error', 'abandoned'
                    )
                    AND finished_at IS NOT NULL
                )
                OR (
                    status IN ('running', 'judged')
                    AND finished_at IS NULL
                )
            )
        )
        """
    )
    op.execute(
        """
        CREATE INDEX idx_qc_inspection_work_item_claim
        ON qc_inspection_work_item (
            priority, next_attempt_at, created_at
        )
        WHERE status IN ('pending', 'retry_wait')
        """
    )
    op.execute(
        """
        CREATE INDEX idx_qc_inspection_work_item_expired_lease
        ON qc_inspection_work_item (lease_expires_at)
        WHERE status IN ('leased', 'running')
        """
    )
    op.execute(
        """
        CREATE INDEX idx_qc_inspection_work_item_task_status
        ON qc_inspection_work_item (task_id, status, created_at)
        """
    )
    op.execute(
        """
        CREATE INDEX idx_qc_inspection_work_item_page
        ON qc_inspection_work_item (page_capture_id, created_at)
        """
    )
    op.execute(
        """
        CREATE INDEX idx_qc_inspection_work_item_rule_status
        ON qc_inspection_work_item_rule (work_item_id, status, ordinal)
        """
    )
    op.execute(
        """
        CREATE INDEX idx_qc_inspection_attempt_work_item
        ON qc_inspection_attempt (work_item_id, attempt_no DESC)
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS qc_inspection_attempt")
    op.execute("DROP TABLE IF EXISTS qc_inspection_work_item_rule")
    op.execute("DROP TABLE IF EXISTS qc_inspection_work_item")
