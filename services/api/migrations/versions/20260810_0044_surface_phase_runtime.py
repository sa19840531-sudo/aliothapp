"""增加 Surface 阶段运行与阶段步骤账本。

Revision ID: 20260810_0044
Revises: 20260807_0043
Create Date: 2026-08-10
"""

from __future__ import annotations

from alembic import op

revision = "20260810_0044"
down_revision = "20260807_0043"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE qc_surface_phase_run (
            phase_run_id varchar(96) PRIMARY KEY,
            task_id uuid NOT NULL REFERENCES qc_task(id) ON DELETE CASCADE,
            session_id varchar(128) NOT NULL,
            surface_kind varchar(32) NOT NULL,
            phase_key varchar(64) NOT NULL,
            phase_protocol_version varchar(64) NOT NULL,
            idempotency_key varchar(255) NOT NULL,
            source_capture_id uuid NOT NULL
                REFERENCES qc_capture(id) ON DELETE RESTRICT,
            status varchar(32) NOT NULL DEFAULT 'running',
            command_sequence bigint NOT NULL,
            finalization_epoch integer NOT NULL,
            context_payload jsonb NOT NULL,
            context_payload_hash varchar(128) NOT NULL,
            evidence_artifact_ids jsonb NOT NULL DEFAULT '[]'::jsonb,
            step_count integer NOT NULL DEFAULT 0,
            terminal_reason_code varchar(128),
            row_version bigint NOT NULL DEFAULT 0,
            started_at timestamptz NOT NULL DEFAULT now(),
            finished_at timestamptz,
            updated_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT fk_qc_surface_phase_run_session FOREIGN KEY (
                task_id, session_id
            ) REFERENCES qc_surface_session (task_id, session_id)
                ON DELETE CASCADE,
            CONSTRAINT uq_qc_surface_phase_run_idempotency UNIQUE (
                task_id, session_id, phase_key, idempotency_key
            ),
            CONSTRAINT ck_qc_surface_phase_run_surface CHECK (
                surface_kind IN ('web', 'android', 'ios')
            ),
            CONSTRAINT ck_qc_surface_phase_run_status CHECK (
                status IN ('running', 'completed', 'blocked', 'failed', 'canceled')
            ),
            CONSTRAINT ck_qc_surface_phase_run_numbers CHECK (
                command_sequence > 0
                AND finalization_epoch >= 0
                AND step_count >= 0
                AND row_version >= 0
            ),
            CONSTRAINT ck_qc_surface_phase_run_payload CHECK (
                jsonb_typeof(context_payload) = 'object'
                AND jsonb_typeof(evidence_artifact_ids) = 'array'
            ),
            CONSTRAINT ck_qc_surface_phase_run_terminal CHECK (
                (
                    status = 'running'
                    AND terminal_reason_code IS NULL
                    AND finished_at IS NULL
                )
                OR (
                    status IN ('completed', 'blocked', 'failed', 'canceled')
                    AND terminal_reason_code IS NOT NULL
                    AND finished_at IS NOT NULL
                )
            )
        )
        """
    )
    op.execute(
        """
        CREATE TABLE qc_surface_phase_step (
            phase_step_id varchar(128) PRIMARY KEY,
            phase_run_id varchar(96) NOT NULL
                REFERENCES qc_surface_phase_run(phase_run_id)
                ON DELETE CASCADE,
            task_id uuid NOT NULL REFERENCES qc_task(id) ON DELETE CASCADE,
            session_id varchar(128) NOT NULL,
            step_key varchar(128) NOT NULL,
            step_index integer NOT NULL,
            status varchar(32) NOT NULL,
            decision_status varchar(64) NOT NULL,
            reason_code varchar(128) NOT NULL,
            source_capture_id uuid NOT NULL
                REFERENCES qc_capture(id) ON DELETE RESTRICT,
            result_capture_id uuid
                REFERENCES qc_capture(id) ON DELETE RESTRICT,
            action_execution_id varchar(128)
                REFERENCES qc_action_execution(action_execution_id)
                ON DELETE RESTRICT,
            decision_payload jsonb NOT NULL,
            decision_payload_hash varchar(128) NOT NULL,
            next_action_payload jsonb,
            evidence_artifact_ids jsonb NOT NULL DEFAULT '[]'::jsonb,
            created_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT uq_qc_surface_phase_step_key UNIQUE (
                phase_run_id, step_key
            ),
            CONSTRAINT uq_qc_surface_phase_step_index UNIQUE (
                phase_run_id, step_index
            ),
            CONSTRAINT ck_qc_surface_phase_step_status CHECK (
                status IN (
                    'decided', 'action_planned', 'action_verified',
                    'completed', 'blocked', 'failed'
                )
            ),
            CONSTRAINT ck_qc_surface_phase_step_numbers CHECK (step_index > 0),
            CONSTRAINT ck_qc_surface_phase_step_payload CHECK (
                jsonb_typeof(decision_payload) = 'object'
                AND (
                    next_action_payload IS NULL
                    OR jsonb_typeof(next_action_payload) = 'object'
                )
                AND jsonb_typeof(evidence_artifact_ids) = 'array'
            )
        )
        """
    )
    op.execute(
        """
        CREATE INDEX idx_qc_surface_phase_run_task_status
        ON qc_surface_phase_run (task_id, status, updated_at)
        """
    )
    op.execute(
        """
        CREATE INDEX idx_qc_surface_phase_run_session
        ON qc_surface_phase_run (session_id, phase_key, started_at)
        """
    )
    op.execute(
        """
        CREATE INDEX idx_qc_surface_phase_step_run
        ON qc_surface_phase_step (phase_run_id, step_index)
        """
    )
    op.execute(
        """
        CREATE INDEX idx_qc_surface_phase_step_capture
        ON qc_surface_phase_step (source_capture_id)
        """
    )
    op.execute(
        """
        COMMENT ON TABLE qc_surface_phase_run IS
        '记录一次界面阶段机运行的可恢复状态、幂等身份和终态结果'
        """
    )
    op.execute(
        """
        COMMENT ON TABLE qc_surface_phase_step IS
        '记录界面阶段机在一次运行中的每次判定、动作建议和证据引用'
        """
    )
    op.execute(
        """
        COMMENT ON COLUMN qc_surface_phase_run.phase_key IS
        '阶段机标识，例如 startup_ad_countdown、popup_chain、auth_gate 或 system_dialog'
        """
    )
    op.execute(
        """
        COMMENT ON COLUMN qc_surface_phase_run.source_capture_id IS
        '启动本次阶段运行的页面或界面采集记录'
        """
    )
    op.execute(
        """
        COMMENT ON COLUMN qc_surface_phase_run.context_payload IS
        '阶段机运行上下文的结构化快照'
        """
    )
    op.execute(
        """
        COMMENT ON COLUMN qc_surface_phase_step.decision_payload IS
        '阶段机输出的结构化判定结果'
        """
    )
    op.execute(
        """
        COMMENT ON COLUMN qc_surface_phase_step.next_action_payload IS
        '阶段机建议交互运行时执行的下一步动作'
        """
    )
    op.execute(
        """
        COMMENT ON COLUMN qc_surface_phase_step.evidence_artifact_ids IS
        '支撑本次阶段判定的证据附件 ID 列表'
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_qc_surface_phase_step_capture")
    op.execute("DROP INDEX IF EXISTS idx_qc_surface_phase_step_run")
    op.execute("DROP INDEX IF EXISTS idx_qc_surface_phase_run_session")
    op.execute("DROP INDEX IF EXISTS idx_qc_surface_phase_run_task_status")
    op.execute("DROP TABLE IF EXISTS qc_surface_phase_step")
    op.execute("DROP TABLE IF EXISTS qc_surface_phase_run")
