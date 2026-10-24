"""增加任务 Completion Gate、Finalization Epoch 与报告快照绑定。

Revision ID: 20260803_0032
Revises: 20260803_0031
Create Date: 2026-08-03
"""

from __future__ import annotations

from alembic import op

revision = "20260803_0032"
down_revision = "20260803_0031"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE qc_task
        ADD COLUMN finalization_state varchar(32) NOT NULL DEFAULT 'open',
        ADD COLUMN finalization_epoch integer NOT NULL DEFAULT 0,
        ADD COLUMN finalization_row_version bigint NOT NULL DEFAULT 0,
        ADD COLUMN completion_gate_snapshot_id varchar(96),
        ADD COLUMN execution_status varchar(32),
        ADD COLUMN quality_outcome varchar(32),
        ADD COLUMN review_generation bigint NOT NULL DEFAULT 0,
        ADD COLUMN review_policy_decision_id varchar(128),
        ADD COLUMN exploration_draining_at timestamptz,
        ADD COLUMN session_draining_at timestamptz,
        ADD COLUMN sealed_at timestamptz,
        ADD CONSTRAINT ck_qc_task_finalization_state CHECK (
            finalization_state IN (
                'open', 'exploration_draining', 'session_draining',
                'finalizing', 'sealed'
            )
        ),
        ADD CONSTRAINT ck_qc_task_finalization_numbers CHECK (
            finalization_epoch >= 0
            AND finalization_row_version >= 0
            AND review_generation >= 0
        ),
        ADD CONSTRAINT ck_qc_task_execution_status CHECK (
            execution_status IS NULL OR execution_status IN (
                'completed', 'completed_with_gaps', 'needs_review', 'failed'
            )
        ),
        ADD CONSTRAINT ck_qc_task_quality_outcome CHECK (
            quality_outcome IS NULL OR quality_outcome IN (
                'pass', 'fail', 'warning', 'mixed', 'unknown'
            )
        ),
        ADD CONSTRAINT ck_qc_task_finalization_lifecycle CHECK (
            (
                finalization_state = 'open'
                AND exploration_draining_at IS NULL
                AND session_draining_at IS NULL
                AND sealed_at IS NULL
            )
            OR (
                finalization_state = 'exploration_draining'
                AND exploration_draining_at IS NOT NULL
                AND session_draining_at IS NULL
                AND sealed_at IS NULL
            )
            OR (
                finalization_state IN ('session_draining', 'finalizing')
                AND exploration_draining_at IS NOT NULL
                AND session_draining_at IS NOT NULL
                AND sealed_at IS NULL
            )
            OR (
                finalization_state = 'sealed'
                AND exploration_draining_at IS NOT NULL
                AND session_draining_at IS NOT NULL
                AND sealed_at IS NOT NULL
            )
        ),
        ADD CONSTRAINT ck_qc_task_finalization_outcome CHECK (
            (
                finalization_state = 'sealed'
                AND completion_gate_snapshot_id IS NOT NULL
                AND execution_status IS NOT NULL
                AND quality_outcome IS NOT NULL
            )
            OR (
                finalization_state <> 'sealed'
                AND execution_status IS NULL
                AND quality_outcome IS NULL
            )
        ),
        ADD CONSTRAINT ck_qc_task_finalization_legacy_status CHECK (
            finalization_state <> 'sealed'
            OR (execution_status IN ('completed', 'completed_with_gaps') AND status = 'done')
            OR (execution_status = 'needs_review' AND status = 'review')
            OR (execution_status = 'failed' AND status = 'failed')
        )
        """
    )
    op.execute(
        """
        UPDATE qc_task AS task
        SET review_generation = review_counts.item_count
        FROM (
            SELECT task_id, count(*)::bigint AS item_count
            FROM qc_review_item
            GROUP BY task_id
        ) AS review_counts
        WHERE task.id = review_counts.task_id
        """
    )
    for table_name in (
        "qc_v2_work_item",
        "qc_inspection_work_item",
        "qc_evidence_request",
        "qc_restoration_attempt",
        "qc_journey_execution",
    ):
        op.execute(
            f"""
            ALTER TABLE {table_name}
                ADD COLUMN finalization_epoch integer NOT NULL DEFAULT 0,
                ADD CONSTRAINT ck_{table_name}_finalization_epoch CHECK (
                    finalization_epoch >= 0
                )
            """
        )
    op.execute(
        """
        CREATE TABLE qc_review_policy_gate_decision (
            decision_id varchar(128) PRIMARY KEY,
            task_id uuid NOT NULL REFERENCES qc_task(id) ON DELETE CASCADE,
            review_generation bigint NOT NULL,
            policy_hash varchar(128) NOT NULL,
            open_review_count integer NOT NULL,
            satisfied boolean NOT NULL,
            rationale text NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT uq_qc_review_policy_gate_generation UNIQUE (
                task_id, review_generation, decision_id
            ),
            CONSTRAINT ck_qc_review_policy_gate_numbers CHECK (
                review_generation >= 0 AND open_review_count >= 0
            ),
            CONSTRAINT ck_qc_review_policy_gate_text CHECK (
                btrim(policy_hash) <> '' AND btrim(rationale) <> ''
            )
        )
        """
    )
    op.execute(
        """
        CREATE TABLE qc_completion_gate_snapshot (
            gate_snapshot_id varchar(96) PRIMARY KEY,
            task_id uuid NOT NULL REFERENCES qc_task(id) ON DELETE CASCADE,
            finalization_epoch integer NOT NULL,
            evaluation_sequence integer NOT NULL,
            protocol_version varchar(64) NOT NULL,
            facts_hash varchar(128) NOT NULL,
            exploration_terminal boolean NOT NULL,
            no_pending_action boolean NOT NULL,
            no_pending_capture_commit boolean NOT NULL,
            no_nonterminal_enforced_work_item boolean NOT NULL,
            no_active_session_request boolean NOT NULL,
            candidate_rule_coverage_terminal boolean NOT NULL,
            no_background_execution boolean NOT NULL,
            review_policy_satisfied boolean NOT NULL,
            pending_action_count integer NOT NULL,
            pending_capture_commit_count integer NOT NULL,
            nonterminal_enforced_work_item_count integer NOT NULL,
            active_session_request_count integer NOT NULL,
            candidate_rule_count integer NOT NULL,
            terminal_candidate_rule_count integer NOT NULL,
            background_execution_count integer NOT NULL,
            open_review_count integer NOT NULL,
            review_generation bigint NOT NULL,
            review_policy_decision_id varchar(128),
            block_reasons jsonb NOT NULL,
            gate_passed boolean NOT NULL,
            evaluated_at timestamptz NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT uq_qc_completion_gate_sequence UNIQUE (
                task_id, evaluation_sequence
            ),
            CONSTRAINT uq_qc_completion_gate_owner UNIQUE (
                task_id, gate_snapshot_id, finalization_epoch
            ),
            CONSTRAINT fk_qc_completion_gate_review_policy FOREIGN KEY (
                task_id, review_generation, review_policy_decision_id
            ) REFERENCES qc_review_policy_gate_decision (
                task_id, review_generation, decision_id
            ) ON DELETE RESTRICT,
            CONSTRAINT ck_qc_completion_gate_numbers CHECK (
                finalization_epoch >= 0
                AND evaluation_sequence > 0
                AND pending_action_count >= 0
                AND pending_capture_commit_count >= 0
                AND nonterminal_enforced_work_item_count >= 0
                AND active_session_request_count >= 0
                AND candidate_rule_count >= 0
                AND terminal_candidate_rule_count >= 0
                AND terminal_candidate_rule_count <= candidate_rule_count
                AND background_execution_count >= 0
                AND open_review_count >= 0
                AND review_generation >= 0
            ),
            CONSTRAINT ck_qc_completion_gate_protocol CHECK (
                protocol_version = 'task-completion-gate.v1'
            ),
            CONSTRAINT ck_qc_completion_gate_block_reasons CHECK (
                jsonb_typeof(block_reasons) = 'array'
            ),
            CONSTRAINT ck_qc_completion_gate_derived_facts CHECK (
                no_pending_action = (pending_action_count = 0)
                AND no_pending_capture_commit = (pending_capture_commit_count = 0)
                AND no_nonterminal_enforced_work_item
                    = (nonterminal_enforced_work_item_count = 0)
                AND no_active_session_request = (active_session_request_count = 0)
                AND candidate_rule_coverage_terminal
                    = (terminal_candidate_rule_count = candidate_rule_count)
                AND no_background_execution = (background_execution_count = 0)
            ),
            CONSTRAINT ck_qc_completion_gate_formula CHECK (
                gate_passed = (
                    exploration_terminal
                    AND no_pending_action
                    AND no_pending_capture_commit
                    AND no_nonterminal_enforced_work_item
                    AND no_active_session_request
                    AND candidate_rule_coverage_terminal
                    AND no_background_execution
                    AND review_policy_satisfied
                )
            )
        )
        """
    )
    op.execute(
        """
        CREATE TABLE qc_report_gate_binding (
            report_id varchar(128) NOT NULL,
            report_version integer NOT NULL,
            report_payload_hash varchar(128) NOT NULL,
            protocol_version varchar(64) NOT NULL,
            task_id uuid NOT NULL REFERENCES qc_task(id) ON DELETE CASCADE,
            gate_snapshot_id varchar(96) NOT NULL,
            finalization_epoch integer NOT NULL,
            execution_status varchar(32) NOT NULL,
            quality_outcome varchar(32) NOT NULL,
            bound_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (report_id, report_version),
            CONSTRAINT fk_qc_report_gate_snapshot FOREIGN KEY (
                task_id, gate_snapshot_id, finalization_epoch
            ) REFERENCES qc_completion_gate_snapshot (
                task_id, gate_snapshot_id, finalization_epoch
            ) ON DELETE RESTRICT,
            CONSTRAINT ck_qc_report_gate_version CHECK (report_version > 0),
            CONSTRAINT ck_qc_report_gate_epoch CHECK (finalization_epoch >= 0),
            CONSTRAINT ck_qc_report_gate_protocol CHECK (
                protocol_version = 'report-gate-binding.v1'
            ),
            CONSTRAINT ck_qc_report_gate_execution CHECK (
                execution_status IN (
                    'completed', 'completed_with_gaps', 'needs_review', 'failed'
                )
            ),
            CONSTRAINT ck_qc_report_gate_quality CHECK (
                quality_outcome IN ('pass', 'fail', 'warning', 'mixed', 'unknown')
            )
        )
        """
    )
    op.execute(
        """
        ALTER TABLE qc_task
        ADD CONSTRAINT fk_qc_task_completion_gate_snapshot
            FOREIGN KEY (
                id, completion_gate_snapshot_id, finalization_epoch
            ) REFERENCES qc_completion_gate_snapshot (
                task_id, gate_snapshot_id, finalization_epoch
            ) ON DELETE SET NULL (completion_gate_snapshot_id),
        ADD CONSTRAINT fk_qc_task_review_policy_decision
            FOREIGN KEY (
                id, review_generation, review_policy_decision_id
            ) REFERENCES qc_review_policy_gate_decision (
                task_id, review_generation, decision_id
            ) ON DELETE SET NULL (review_policy_decision_id)
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION qc_guard_v2_finalization_write()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        DECLARE
            task_state varchar(32);
            task_epoch integer;
        BEGIN
            SELECT finalization_state, finalization_epoch
            INTO task_state, task_epoch
            FROM qc_task
            WHERE id = NEW.task_id
            FOR KEY SHARE;

            IF task_state IS NULL THEN
                RAISE EXCEPTION 'V2 写入关联任务不存在';
            END IF;
            IF task_state IN ('finalizing', 'sealed') THEN
                RAISE EXCEPTION '任务已进入最终封存阶段，拒绝晚到 V2 写入';
            END IF;
            IF NEW.finalization_epoch <> task_epoch THEN
                RAISE EXCEPTION 'V2 写入 finalization_epoch 已过期';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    for table_name in (
        "qc_v2_work_item",
        "qc_inspection_work_item",
        "qc_evidence_request",
        "qc_restoration_attempt",
        "qc_journey_execution",
        "qc_action_execution",
    ):
        op.execute(
            f"""
            CREATE TRIGGER trg_{table_name}_finalization_guard
            BEFORE INSERT OR UPDATE ON {table_name}
            FOR EACH ROW EXECUTE FUNCTION qc_guard_v2_finalization_write()
            """
        )
    op.execute(
        """
        CREATE TRIGGER trg_qc_surface_session_finalization_guard
        BEFORE INSERT ON qc_surface_session
        FOR EACH ROW EXECUTE FUNCTION qc_guard_v2_finalization_write()
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION qc_guard_agent_run_finalization()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        DECLARE
            task_state varchar(32);
        BEGIN
            IF TG_OP = 'UPDATE' AND NEW.task_id IS DISTINCT FROM OLD.task_id THEN
                RAISE EXCEPTION 'Agent Run task_id 不可变更';
            END IF;

            SELECT finalization_state
            INTO task_state
            FROM qc_task
            WHERE id = NEW.task_id
            FOR KEY SHARE;

            IF task_state IS NULL THEN
                RAISE EXCEPTION 'Agent Run 关联任务不存在';
            END IF;
            IF task_state IN ('finalizing', 'sealed') THEN
                RAISE EXCEPTION '任务已封存，拒绝新增或改写 Agent Run';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_qc_agent_run_finalization_guard
        BEFORE INSERT OR UPDATE ON qc_agent_run
        FOR EACH ROW EXECUTE FUNCTION qc_guard_agent_run_finalization()
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION qc_guard_rule_execution_finalization()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        DECLARE
            task_state varchar(32);
        BEGIN
            SELECT finalization_state
            INTO task_state
            FROM qc_task
            WHERE id = NEW.task_id
            FOR KEY SHARE;

            IF task_state NOT IN ('finalizing', 'sealed') THEN
                RETURN NEW;
            END IF;
            IF TG_OP = 'INSERT' THEN
                RAISE EXCEPTION '任务已封存，拒绝新增规则执行';
            END IF;
            IF NEW.rule_id IS DISTINCT FROM OLD.rule_id
                OR NEW.page_capture_id IS DISTINCT FROM OLD.page_capture_id
                OR NEW.observation_packet_id IS DISTINCT FROM OLD.observation_packet_id
                OR NEW.agent_run_id IS DISTINCT FROM OLD.agent_run_id
                OR NEW.execution_status IS DISTINCT FROM OLD.execution_status
                OR NEW.judge_strategy IS DISTINCT FROM OLD.judge_strategy
                OR NEW.confidence IS DISTINCT FROM OLD.confidence
                OR NEW.severity IS DISTINCT FROM OLD.severity
                OR NEW.result_summary IS DISTINCT FROM OLD.result_summary
                OR NEW.problem_desc IS DISTINCT FROM OLD.problem_desc
                OR NEW.evidence_artifact_ids IS DISTINCT FROM OLD.evidence_artifact_ids
                OR NEW.judge_input IS DISTINCT FROM OLD.judge_input
                OR NEW.judge_output IS DISTINCT FROM OLD.judge_output
                OR NEW.error_code IS DISTINCT FROM OLD.error_code
                OR NEW.error_message IS DISTINCT FROM OLD.error_message
                OR NEW.started_at IS DISTINCT FROM OLD.started_at
                OR NEW.finished_at IS DISTINCT FROM OLD.finished_at
            THEN
                RAISE EXCEPTION '任务已封存，拒绝改写规则执行事实';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_qc_rule_execution_finalization_guard
        BEFORE INSERT OR UPDATE ON qc_rule_execution
        FOR EACH ROW EXECUTE FUNCTION qc_guard_rule_execution_finalization()
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION qc_track_review_generation()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        DECLARE
            target_task_id uuid;
            task_state varchar(32);
        BEGIN
            target_task_id := CASE WHEN TG_OP = 'DELETE' THEN OLD.task_id ELSE NEW.task_id END;
            IF TG_OP = 'UPDATE' AND NEW.task_id IS DISTINCT FROM OLD.task_id THEN
                RAISE EXCEPTION '复核项 task_id 不可变更';
            END IF;
            SELECT finalization_state
            INTO task_state
            FROM qc_task
            WHERE id = target_task_id
            FOR KEY SHARE;

            IF task_state IN ('finalizing', 'sealed') AND (
                TG_OP = 'INSERT'
                OR (TG_OP = 'UPDATE' AND NEW.status IN ('pending', 'needs_recheck'))
            ) THEN
                RAISE EXCEPTION '任务已封存，拒绝新增或重新打开复核项';
            END IF;

            UPDATE qc_task
            SET
                review_generation = review_generation + 1,
                review_policy_decision_id = NULL,
                updated_at = now()
            WHERE id = target_task_id;
            IF TG_OP = 'DELETE' THEN
                RETURN OLD;
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_qc_review_item_generation
        AFTER INSERT OR UPDATE OR DELETE ON qc_review_item
        FOR EACH ROW EXECUTE FUNCTION qc_track_review_generation()
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION qc_guard_immutable_audit_record()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION '任务完成审计记录不可删除';
            END IF;
            IF NEW IS DISTINCT FROM OLD THEN
                RAISE EXCEPTION '任务完成审计记录不可改写';
            END IF;
            RETURN OLD;
        END;
        $$
        """
    )
    for table_name in (
        "qc_review_policy_gate_decision",
        "qc_completion_gate_snapshot",
        "qc_report_gate_binding",
    ):
        op.execute(
            f"""
            CREATE TRIGGER trg_{table_name}_immutable
            BEFORE UPDATE OR DELETE ON {table_name}
            FOR EACH ROW EXECUTE FUNCTION qc_guard_immutable_audit_record()
            """
        )
    op.execute(
        """
        CREATE INDEX idx_qc_task_finalization_state
        ON qc_task (finalization_state, finalization_epoch, updated_at)
        """
    )
    op.execute(
        """
        CREATE INDEX idx_qc_completion_gate_task
        ON qc_completion_gate_snapshot (task_id, evaluation_sequence DESC)
        """
    )
    op.execute(
        """
        CREATE INDEX idx_qc_report_gate_task
        ON qc_report_gate_binding (task_id, report_id, report_version DESC)
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_qc_report_gate_task")
    op.execute("DROP INDEX IF EXISTS idx_qc_completion_gate_task")
    op.execute("DROP INDEX IF EXISTS idx_qc_task_finalization_state")
    for table_name in (
        "qc_report_gate_binding",
        "qc_completion_gate_snapshot",
        "qc_review_policy_gate_decision",
    ):
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table_name}_immutable ON {table_name}")
    op.execute("DROP FUNCTION IF EXISTS qc_guard_immutable_audit_record()")
    op.execute("DROP TRIGGER IF EXISTS trg_qc_review_item_generation ON qc_review_item")
    op.execute("DROP FUNCTION IF EXISTS qc_track_review_generation()")
    op.execute(
        "DROP TRIGGER IF EXISTS trg_qc_rule_execution_finalization_guard ON qc_rule_execution"
    )
    op.execute("DROP FUNCTION IF EXISTS qc_guard_rule_execution_finalization()")
    op.execute("DROP TRIGGER IF EXISTS trg_qc_agent_run_finalization_guard ON qc_agent_run")
    op.execute("DROP FUNCTION IF EXISTS qc_guard_agent_run_finalization()")
    op.execute(
        "DROP TRIGGER IF EXISTS trg_qc_surface_session_finalization_guard ON qc_surface_session"
    )
    for table_name in (
        "qc_action_execution",
        "qc_journey_execution",
        "qc_restoration_attempt",
        "qc_evidence_request",
        "qc_inspection_work_item",
        "qc_v2_work_item",
    ):
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table_name}_finalization_guard ON {table_name}")
    op.execute("DROP FUNCTION IF EXISTS qc_guard_v2_finalization_write()")
    op.execute(
        """
        ALTER TABLE qc_task
        DROP CONSTRAINT IF EXISTS fk_qc_task_review_policy_decision,
        DROP CONSTRAINT IF EXISTS fk_qc_task_completion_gate_snapshot
        """
    )
    op.execute("DROP TABLE IF EXISTS qc_report_gate_binding")
    op.execute("DROP TABLE IF EXISTS qc_completion_gate_snapshot")
    op.execute("DROP TABLE IF EXISTS qc_review_policy_gate_decision")
    for table_name in (
        "qc_journey_execution",
        "qc_restoration_attempt",
        "qc_evidence_request",
        "qc_inspection_work_item",
        "qc_v2_work_item",
    ):
        op.execute(
            f"""
            ALTER TABLE {table_name}
                DROP CONSTRAINT IF EXISTS ck_{table_name}_finalization_epoch,
                DROP COLUMN IF EXISTS finalization_epoch
            """
        )
    op.execute(
        """
        ALTER TABLE qc_task
        DROP CONSTRAINT IF EXISTS ck_qc_task_finalization_legacy_status,
        DROP CONSTRAINT IF EXISTS ck_qc_task_finalization_outcome,
        DROP CONSTRAINT IF EXISTS ck_qc_task_finalization_lifecycle,
        DROP CONSTRAINT IF EXISTS ck_qc_task_quality_outcome,
        DROP CONSTRAINT IF EXISTS ck_qc_task_execution_status,
        DROP CONSTRAINT IF EXISTS ck_qc_task_finalization_numbers,
        DROP CONSTRAINT IF EXISTS ck_qc_task_finalization_state,
        DROP COLUMN IF EXISTS sealed_at,
        DROP COLUMN IF EXISTS session_draining_at,
        DROP COLUMN IF EXISTS exploration_draining_at,
        DROP COLUMN IF EXISTS review_policy_decision_id,
        DROP COLUMN IF EXISTS review_generation,
        DROP COLUMN IF EXISTS quality_outcome,
        DROP COLUMN IF EXISTS execution_status,
        DROP COLUMN IF EXISTS completion_gate_snapshot_id,
        DROP COLUMN IF EXISTS finalization_row_version,
        DROP COLUMN IF EXISTS finalization_epoch,
        DROP COLUMN IF EXISTS finalization_state
        """
    )
