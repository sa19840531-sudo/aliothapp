"""新增独立的 EvidenceInputSet 聚合质检工作项。

Revision ID: 20260814_0061
Revises: 20260814_0060
Create Date: 2026-08-14

旧 qc_inspection_work_item 继续强制绑定非空 page_capture_id。本迁移不放宽旧表，
而是用一张独立工作项表绑定 EvidenceInputSet，确保任务级、旅程级与环境矩阵规则
不会借用任意页面作为锚点。一个 EvidenceInputSet 已绑定唯一规则，因此一个聚合
工作项只执行这一条规则；attempt 子表保存每次租约执行的判定或错误终态。
"""

from __future__ import annotations

from alembic import op

revision = "20260814_0061"
down_revision = "20260814_0060"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE qc_aggregate_inspection_work_item (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            task_id uuid NOT NULL REFERENCES qc_task(id) ON DELETE CASCADE,
            evidence_input_set_id varchar(96) NOT NULL,
            producer_agent_run_id uuid NOT NULL,
            consumer_agent_run_id uuid,
            rule_id uuid NOT NULL REFERENCES qc_rule(id) ON DELETE RESTRICT,
            rule_code varchar(128) NOT NULL,
            rule_contract_hash varchar(128) NOT NULL,
            scope varchar(32) NOT NULL,
            scope_key varchar(128) NOT NULL,
            protocol_version varchar(64) NOT NULL,
            input_hash varchar(128) NOT NULL,
            control_mode varchar(16) NOT NULL DEFAULT 'enforced',
            status varchar(32) NOT NULL DEFAULT 'pending',
            priority integer NOT NULL DEFAULT 100,
            max_attempts integer NOT NULL DEFAULT 3,
            attempt_count integer NOT NULL DEFAULT 0,
            next_attempt_at timestamptz NOT NULL DEFAULT now(),
            lease_owner varchar(255),
            lease_token uuid,
            lease_expires_at timestamptz,
            result_payload jsonb,
            result_hash varchar(128),
            last_error_code varchar(128),
            last_error_message text,
            finalization_epoch integer NOT NULL,
            row_version bigint NOT NULL DEFAULT 0,
            started_at timestamptz,
            finished_at timestamptz,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT fk_qc_aggregate_work_input_set
                FOREIGN KEY (task_id, evidence_input_set_id)
                REFERENCES qc_evidence_input_set(task_id, input_set_id)
                ON DELETE NO ACTION
                DEFERRABLE INITIALLY DEFERRED,
            CONSTRAINT fk_qc_aggregate_work_producer
                FOREIGN KEY (task_id, producer_agent_run_id)
                REFERENCES qc_agent_run(task_id, id)
                ON DELETE NO ACTION
                DEFERRABLE INITIALLY DEFERRED,
            CONSTRAINT fk_qc_aggregate_work_consumer
                FOREIGN KEY (task_id, consumer_agent_run_id)
                REFERENCES qc_agent_run(task_id, id)
                ON DELETE NO ACTION
                DEFERRABLE INITIALLY DEFERRED,
            CONSTRAINT uq_qc_aggregate_work_input_set
                UNIQUE (evidence_input_set_id),
            CONSTRAINT uq_qc_aggregate_work_task_identity
                UNIQUE (task_id, id),
            CONSTRAINT ck_qc_aggregate_work_scope CHECK (
                scope IN ('journey', 'task', 'environment_matrix')
                AND scope_key ~ '^[a-z][a-z0-9_.:-]{0,127}$'
            ),
            CONSTRAINT ck_qc_aggregate_work_protocol CHECK (
                protocol_version = 'aggregate-inspection-work-item.v1'
                AND input_hash ~ '^[0-9a-f]{64}$'
                AND (
                    result_hash IS NULL
                    OR result_hash ~ '^[0-9a-f]{64}$'
                )
            ),
            CONSTRAINT ck_qc_aggregate_work_status CHECK (
                status IN (
                    'pending', 'leased', 'running', 'retry_wait',
                    'succeeded', 'evidence_insufficient', 'failed',
                    'cancelled', 'superseded'
                )
            ),
            CONSTRAINT ck_qc_aggregate_work_control CHECK (
                control_mode IN ('shadow', 'enforced')
            ),
            CONSTRAINT ck_qc_aggregate_work_numbers CHECK (
                priority >= 0
                AND max_attempts > 0
                AND attempt_count >= 0
                AND attempt_count <= max_attempts
                AND finalization_epoch >= 0
                AND row_version >= 0
            ),
            CONSTRAINT ck_qc_aggregate_work_lease CHECK (
                (
                    status IN ('leased', 'running')
                    AND consumer_agent_run_id IS NOT NULL
                    AND lease_owner IS NOT NULL
                    AND lease_token IS NOT NULL
                    AND lease_expires_at IS NOT NULL
                )
                OR (
                    status NOT IN ('leased', 'running')
                    AND consumer_agent_run_id IS NULL
                    AND lease_owner IS NULL
                    AND lease_token IS NULL
                    AND lease_expires_at IS NULL
                )
            ),
            CONSTRAINT ck_qc_aggregate_work_result CHECK (
                (
                    status IN ('succeeded', 'evidence_insufficient')
                    AND result_payload IS NOT NULL
                    AND jsonb_typeof(result_payload) = 'object'
                    AND result_payload <> '{}'::jsonb
                    AND result_hash IS NOT NULL
                    AND finished_at IS NOT NULL
                )
                OR (
                    status IN ('failed', 'cancelled', 'superseded')
                    AND result_payload IS NULL
                    AND result_hash IS NULL
                    AND finished_at IS NOT NULL
                )
                OR (
                    status NOT IN (
                        'succeeded', 'evidence_insufficient', 'failed',
                        'cancelled', 'superseded'
                    )
                    AND result_payload IS NULL
                    AND result_hash IS NULL
                    AND finished_at IS NULL
                )
            )
        )
        """
    )
    op.execute(
        """
        CREATE TABLE qc_aggregate_inspection_attempt (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            task_id uuid NOT NULL,
            work_item_id uuid NOT NULL,
            consumer_agent_run_id uuid NOT NULL,
            attempt_no integer NOT NULL,
            lease_token uuid NOT NULL,
            input_hash varchar(128) NOT NULL,
            status varchar(32) NOT NULL DEFAULT 'running',
            result_payload jsonb,
            result_hash varchar(128),
            error_code varchar(128),
            error_message text,
            started_at timestamptz NOT NULL DEFAULT now(),
            finished_at timestamptz,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT fk_qc_aggregate_attempt_work
                FOREIGN KEY (task_id, work_item_id)
                REFERENCES qc_aggregate_inspection_work_item(task_id, id)
                ON DELETE CASCADE,
            CONSTRAINT fk_qc_aggregate_attempt_consumer
                FOREIGN KEY (task_id, consumer_agent_run_id)
                REFERENCES qc_agent_run(task_id, id)
                ON DELETE NO ACTION
                DEFERRABLE INITIALLY DEFERRED,
            CONSTRAINT uq_qc_aggregate_attempt_number
                UNIQUE (work_item_id, attempt_no),
            CONSTRAINT ck_qc_aggregate_attempt_status CHECK (
                status IN (
                    'running', 'succeeded', 'evidence_insufficient',
                    'retryable_error', 'terminal_error', 'abandoned'
                )
            ),
            CONSTRAINT ck_qc_aggregate_attempt_identity CHECK (
                attempt_no > 0
                AND input_hash ~ '^[0-9a-f]{64}$'
                AND (
                    result_hash IS NULL
                    OR result_hash ~ '^[0-9a-f]{64}$'
                )
            ),
            CONSTRAINT ck_qc_aggregate_attempt_result CHECK (
                (
                    status IN ('succeeded', 'evidence_insufficient')
                    AND result_payload IS NOT NULL
                    AND jsonb_typeof(result_payload) = 'object'
                    AND result_payload <> '{}'::jsonb
                    AND result_hash IS NOT NULL
                    AND finished_at IS NOT NULL
                )
                OR (
                    status IN (
                        'retryable_error', 'terminal_error', 'abandoned'
                    )
                    AND result_payload IS NULL
                    AND result_hash IS NULL
                    AND finished_at IS NOT NULL
                )
                OR (
                    status = 'running'
                    AND result_payload IS NULL
                    AND result_hash IS NULL
                    AND finished_at IS NULL
                )
            )
        )
        """
    )
    op.execute(
        """
        CREATE INDEX idx_qc_aggregate_work_claim
        ON qc_aggregate_inspection_work_item (
            task_id, status, priority, next_attempt_at, created_at
        )
        """
    )
    op.execute(
        """
        CREATE INDEX idx_qc_aggregate_work_task_epoch
        ON qc_aggregate_inspection_work_item (
            task_id, control_mode, finalization_epoch, status
        )
        """
    )
    op.execute(
        """
        CREATE INDEX idx_qc_aggregate_attempt_work_status
        ON qc_aggregate_inspection_attempt (work_item_id, status, attempt_no DESC)
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM qc_aggregate_inspection_work_item LIMIT 1)
               OR EXISTS (SELECT 1 FROM qc_aggregate_inspection_attempt LIMIT 1) THEN
                RAISE EXCEPTION
                    'cannot downgrade aggregate inspection work while audit rows exist';
            END IF;
        END;
        $$
        """
    )
    op.execute("DROP INDEX IF EXISTS idx_qc_aggregate_attempt_work_status")
    op.execute("DROP TABLE IF EXISTS qc_aggregate_inspection_attempt")
    op.execute("DROP INDEX IF EXISTS idx_qc_aggregate_work_task_epoch")
    op.execute("DROP INDEX IF EXISTS idx_qc_aggregate_work_claim")
    op.execute("DROP TABLE IF EXISTS qc_aggregate_inspection_work_item")
