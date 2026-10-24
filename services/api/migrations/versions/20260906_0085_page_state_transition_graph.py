"""增加页面状态、实际转场和探索树投影事实。

Revision ID: 20260906_0085
Revises: 20260904_0084
Create Date: 2026-09-06

页面图是 ``qc_capture`` 与 ``qc_action_execution`` 的可重建投影：

* 状态行一一绑定 Capture，保存稳定身份、局部状态及其可信度；
* 转场行一一绑定 Action Execution（旧探索动作使用 ``legacy:`` 身份），
  即使失败、阻断或没有 After Capture 也保留终态；
* parent_transition_id / route_path 保存实际走过的路线，不把候选
  Frontier 路径误当成已经到达；
* 弹窗和外部落地页保留图证据，但由投影字段明确排除业务到达；
* finalization_epoch 允许同一任务的重跑隔离，未知 epoch 不得进入权威恢复路线。
"""

from __future__ import annotations

from alembic import op

revision = "20260906_0085"
down_revision = "20260904_0084"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE qc_page_graph_state (
            state_id uuid PRIMARY KEY
                REFERENCES qc_capture(id) ON DELETE CASCADE,
            task_id uuid NOT NULL REFERENCES qc_task(id) ON DELETE CASCADE,
            agent_run_id uuid REFERENCES qc_agent_run(id) ON DELETE SET NULL,
            surface_kind varchar(16) NOT NULL,
            surface_session_id varchar(128),
            finalization_epoch integer,
            command_sequence bigint,
            capture_role varchar(32) NOT NULL,
            logical_page_key varchar(768) NOT NULL,
            identity_status varchar(16) NOT NULL DEFAULT 'unknown',
            identity_source varchar(64) NOT NULL DEFAULT 'unconfirmed',
            page_type varchar(128),
            page_state varchar(256),
            state_fingerprint varchar(64) NOT NULL,
            is_overlay boolean NOT NULL DEFAULT false,
            is_external boolean NOT NULL DEFAULT false,
            counts_as_business_page boolean NOT NULL DEFAULT false,
            state_payload jsonb NOT NULL DEFAULT '{}'::jsonb,
            captured_at timestamptz,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT ck_qc_page_graph_state_surface CHECK (
                surface_kind IN ('web', 'android', 'ios')
            ),
            CONSTRAINT ck_qc_page_graph_state_identity_status CHECK (
                identity_status IN ('confirmed', 'inferred', 'unknown')
            ),
            CONSTRAINT ck_qc_page_graph_state_payload CHECK (
                jsonb_typeof(state_payload) = 'object'
            ),
            CONSTRAINT ck_qc_page_graph_state_numbers CHECK (
                (finalization_epoch IS NULL OR finalization_epoch >= 0)
                AND (command_sequence IS NULL OR command_sequence >= 0)
            ),
            CONSTRAINT ck_qc_page_graph_state_business CHECK (
                counts_as_business_page = (
                    identity_status = 'confirmed'
                    AND NOT is_overlay
                    AND NOT is_external
                )
            )
        )
        """
    )
    op.execute(
        """
        CREATE INDEX idx_qc_page_graph_state_run_order
        ON qc_page_graph_state (
            task_id, agent_run_id, finalization_epoch,
            surface_session_id, command_sequence, captured_at, state_id
        )
        """
    )
    op.execute(
        """
        CREATE INDEX idx_qc_page_graph_state_identity
        ON qc_page_graph_state (
            task_id, agent_run_id, finalization_epoch,
            logical_page_key, state_fingerprint
        )
        """
    )
    op.execute(
        """
        CREATE TABLE qc_page_graph_transition (
            transition_id varchar(128) PRIMARY KEY,
            task_id uuid NOT NULL REFERENCES qc_task(id) ON DELETE CASCADE,
            agent_run_id uuid REFERENCES qc_agent_run(id) ON DELETE SET NULL,
            surface_kind varchar(16) NOT NULL,
            surface_session_id varchar(128),
            finalization_epoch integer,
            command_sequence bigint,
            action_execution_id varchar(96)
                REFERENCES qc_action_execution(action_execution_id)
                ON DELETE CASCADE,
            source_capture_id uuid REFERENCES qc_capture(id) ON DELETE SET NULL,
            target_capture_id uuid REFERENCES qc_capture(id) ON DELETE SET NULL,
            source_state_id uuid
                REFERENCES qc_page_graph_state(state_id) ON DELETE SET NULL,
            target_state_id uuid
                REFERENCES qc_page_graph_state(state_id) ON DELETE SET NULL,
            action_kind varchar(64) NOT NULL DEFAULT 'unknown',
            semantic_target text,
            risk_level varchar(16),
            status varchar(32) NOT NULL,
            terminal boolean NOT NULL DEFAULT false,
            proof varchar(32) NOT NULL DEFAULT 'insufficient',
            counts_as_business_arrival boolean NOT NULL DEFAULT false,
            replay_eligible boolean NOT NULL DEFAULT false,
            reason_codes jsonb NOT NULL DEFAULT '[]'::jsonb,
            outcome_payload jsonb NOT NULL DEFAULT '{}'::jsonb,
            parent_transition_id varchar(128)
                REFERENCES qc_page_graph_transition(transition_id)
                ON DELETE SET NULL,
            route_path jsonb NOT NULL DEFAULT '[]'::jsonb,
            navigation_depth integer NOT NULL DEFAULT 0,
            started_at timestamptz,
            finished_at timestamptz,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT ck_qc_page_graph_transition_surface CHECK (
                surface_kind IN ('web', 'android', 'ios')
            ),
            CONSTRAINT ck_qc_page_graph_transition_status CHECK (
                status IN (
                    'planned', 'executing', 'evidence_pending', 'completed',
                    'evidence_failed', 'blocked', 'failed', 'timed_out',
                    'skipped', 'unknown'
                )
            ),
            CONSTRAINT ck_qc_page_graph_transition_proof CHECK (
                proof IN ('trusted', 'capture_pair', 'insufficient')
            ),
            CONSTRAINT ck_qc_page_graph_transition_risk CHECK (
                risk_level IS NULL OR risk_level IN ('low', 'medium', 'high', 'unknown')
            ),
            CONSTRAINT ck_qc_page_graph_transition_json CHECK (
                jsonb_typeof(reason_codes) = 'array'
                AND jsonb_typeof(outcome_payload) = 'object'
                AND jsonb_typeof(route_path) = 'array'
            ),
            CONSTRAINT ck_qc_page_graph_transition_numbers CHECK (
                (finalization_epoch IS NULL OR finalization_epoch >= 0)
                AND (command_sequence IS NULL OR command_sequence >= 0)
                AND navigation_depth >= 0
            ),
            CONSTRAINT ck_qc_page_graph_transition_terminal CHECK (
                terminal = (
                    status IN (
                        'completed', 'evidence_failed', 'blocked',
                        'failed', 'timed_out', 'skipped'
                    )
                )
            ),
            CONSTRAINT ck_qc_page_graph_transition_replay CHECK (
                NOT replay_eligible OR (
                    status = 'completed'
                    AND source_state_id IS NOT NULL
                    AND target_state_id IS NOT NULL
                    AND proof = 'trusted'
                    AND risk_level IS NOT NULL
                    AND risk_level = 'low'
                    AND finalization_epoch IS NOT NULL
                    AND surface_session_id IS NOT NULL
                )
            )
        )
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX uq_qc_page_graph_transition_action
        ON qc_page_graph_transition (task_id, action_execution_id)
        WHERE action_execution_id IS NOT NULL
        """
    )
    op.execute(
        """
        CREATE INDEX idx_qc_page_graph_transition_route
        ON qc_page_graph_transition (
            task_id, agent_run_id, finalization_epoch,
            surface_session_id, navigation_depth, command_sequence, created_at
        )
        """
    )
    op.execute(
        """
        CREATE INDEX idx_qc_page_graph_transition_source
        ON qc_page_graph_transition (source_state_id, status, replay_eligible)
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_qc_page_graph_transition_source")
    op.execute("DROP INDEX IF EXISTS idx_qc_page_graph_transition_route")
    op.execute("DROP INDEX IF EXISTS uq_qc_page_graph_transition_action")
    op.execute("DROP TABLE IF EXISTS qc_page_graph_transition")
    op.execute("DROP INDEX IF EXISTS idx_qc_page_graph_state_identity")
    op.execute("DROP INDEX IF EXISTS idx_qc_page_graph_state_run_order")
    op.execute("DROP TABLE IF EXISTS qc_page_graph_state")
