"""持久化动态页面探索动作与页面质检执行终态。

页面由运行时 Frontier 动态发现，不能在任务启动前物化为确定页面清单。本迁移新增
探索动作审计表，并在真实页面采集记录上补充质检生命周期、终态和失败补偿字段。

Revision ID: 20260717_0011
Revises: 20260717_0010
Create Date: 2026-07-17
"""

from __future__ import annotations

from alembic import op

revision = "20260717_0011"
down_revision = "20260717_0010"
branch_labels = None
depends_on = None

_UPGRADE_STATEMENTS = [
    """
    CREATE TABLE qc_page_exploration_action (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        task_id uuid NOT NULL REFERENCES qc_task(id) ON DELETE CASCADE,
        agent_run_id uuid NOT NULL REFERENCES qc_agent_run(id) ON DELETE CASCADE,
        exploration_action_id varchar(128) NOT NULL,
        source_page_capture_id uuid REFERENCES qc_page_capture(id) ON DELETE SET NULL,
        source_page_type varchar(64),
        action varchar(64) NOT NULL,
        origin varchar(32) NOT NULL DEFAULT 'discovered',
        target text,
        target_keywords jsonb NOT NULL DEFAULT '[]'::jsonb,
        risk_level varchar(32),
        purpose text,
        expected_next_page_type varchar(64),
        navigation_depth integer,
        status varchar(32) NOT NULL,
        result_page_capture_id uuid REFERENCES qc_page_capture(id) ON DELETE SET NULL,
        failure_reason varchar(128),
        failure_code varchar(128),
        failure_message text,
        failure_retryable boolean,
        attempt_count integer NOT NULL DEFAULT 0,
        created_at timestamptz NOT NULL DEFAULT now(),
        updated_at timestamptz NOT NULL DEFAULT now(),
        CONSTRAINT uq_qc_page_exploration_action_run_key UNIQUE (
            agent_run_id, exploration_action_id
        ),
        CONSTRAINT ck_qc_page_exploration_action_origin CHECK (
            origin IN ('entry', 'discovered')
        ),
        CONSTRAINT ck_qc_page_exploration_action_status CHECK (
            status IN ('candidate', 'selected', 'explored', 'blocked', 'skipped', 'failed')
        ),
        CONSTRAINT ck_qc_page_exploration_action_depth CHECK (
            navigation_depth IS NULL OR navigation_depth >= 0
        ),
        CONSTRAINT ck_qc_page_exploration_action_attempts CHECK (attempt_count >= 0)
    )
    """,
    """
    COMMENT ON TABLE qc_page_exploration_action IS
    '运行时动态页面 Frontier 动作；动作可失败或跳过，不要求每条动作都产生页面采集'
    """,
    """
    COMMENT ON COLUMN qc_page_exploration_action.exploration_action_id IS
    '单次 Agent Run 内稳定动作键（如 exploration-2），与运行时 WorkingMemory 对齐'
    """,
    """
    COMMENT ON COLUMN qc_page_exploration_action.result_page_capture_id IS
    '动作成功探索后产生的页面采集；失败、阻断或未选择时为空'
    """,
    """
    CREATE INDEX idx_qc_page_exploration_action_run_status
    ON qc_page_exploration_action (agent_run_id, status, updated_at)
    """,
    """
    CREATE INDEX idx_qc_page_exploration_action_source
    ON qc_page_exploration_action (source_page_capture_id, status)
    """,
    """
    ALTER TABLE qc_page_capture
        ADD COLUMN inspection_lifecycle_status varchar(32),
        ADD COLUMN inspection_status varchar(32),
        ADD COLUMN inspection_failure_stage varchar(64),
        ADD COLUMN inspection_failure_code varchar(128),
        ADD COLUMN inspection_failure_message text,
        ADD COLUMN inspection_failure_retryable boolean,
        ADD COLUMN inspection_abandon_reason varchar(128)
    """,
    """
    ALTER TABLE qc_page_capture
        ALTER COLUMN inspection_lifecycle_status SET DEFAULT 'active',
        ALTER COLUMN inspection_status SET DEFAULT 'captured'
    """,
    """
    ALTER TABLE qc_page_capture
        ADD CONSTRAINT ck_qc_page_capture_inspection_lifecycle CHECK (
            inspection_lifecycle_status IS NULL
            OR inspection_lifecycle_status IN ('active', 'superseded', 'abandoned')
        ),
        ADD CONSTRAINT ck_qc_page_capture_inspection_status CHECK (
            inspection_status IS NULL
            OR inspection_status IN (
                'captured', 'detecting', 'matching', 'evaluating',
                'completed', 'degraded', 'failed'
            )
        )
    """,
    """
    COMMENT ON COLUMN qc_page_capture.inspection_status IS
    '页面级质检执行状态；completed/degraded/failed 均为不再阻塞其他页面的终态'
    """,
    """
    COMMENT ON COLUMN qc_page_capture.inspection_lifecycle_status IS
    '页面采集在当前 Run 的生命周期：active/superseded/abandoned；历史行升级后可为空'
    """,
]

_DOWNGRADE_STATEMENTS = [
    """
    ALTER TABLE qc_page_capture
        DROP CONSTRAINT IF EXISTS ck_qc_page_capture_inspection_status,
        DROP CONSTRAINT IF EXISTS ck_qc_page_capture_inspection_lifecycle
    """,
    "ALTER TABLE qc_page_capture DROP COLUMN IF EXISTS inspection_abandon_reason",
    "ALTER TABLE qc_page_capture DROP COLUMN IF EXISTS inspection_failure_retryable",
    "ALTER TABLE qc_page_capture DROP COLUMN IF EXISTS inspection_failure_message",
    "ALTER TABLE qc_page_capture DROP COLUMN IF EXISTS inspection_failure_code",
    "ALTER TABLE qc_page_capture DROP COLUMN IF EXISTS inspection_failure_stage",
    "ALTER TABLE qc_page_capture DROP COLUMN IF EXISTS inspection_status",
    "ALTER TABLE qc_page_capture DROP COLUMN IF EXISTS inspection_lifecycle_status",
    "DROP TABLE IF EXISTS qc_page_exploration_action",
]


def upgrade() -> None:
    for statement in _UPGRADE_STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    for statement in _DOWNGRADE_STATEMENTS:
        op.execute(statement)
