"""任务详情过程可视化：领域日志表、旅程页面节点表与页面识别指标列。

新版任务详情页需要"过程态"数据：执行日志（INFO/WARN/ERROR/EXEC）、按旅程排列的
页面节点（含待执行/已跳过）、页面识别指标（识别状态/置信度/耗时/元素数）与规则
展示元数据（所属规则包/触发条件文案）。数据由 Worker 侧旁路观察者写入，Agent /
Skill / Tool 的实现与编排不感知这些表。

Revision ID: 20260717_0009
Revises: 20260716_0008
Create Date: 2026-07-17
"""

from __future__ import annotations

from alembic import op

revision = "20260717_0009"
down_revision = "20260716_0008"
branch_labels = None
depends_on = None

_UPGRADE_STATEMENTS = [
    """
    CREATE TABLE qc_task_log (
        id bigserial PRIMARY KEY,
        task_id uuid NOT NULL REFERENCES qc_task(id) ON DELETE CASCADE,
        agent_run_id uuid REFERENCES qc_agent_run(id) ON DELETE SET NULL,
        ts timestamptz NOT NULL DEFAULT now(),
        level varchar(16) NOT NULL,
        source varchar(64) NOT NULL DEFAULT 'observer',
        message text NOT NULL,
        page_capture_id uuid REFERENCES qc_page_capture(id) ON DELETE SET NULL,
        rule_execution_id uuid REFERENCES qc_rule_execution(id) ON DELETE SET NULL,
        context jsonb NOT NULL DEFAULT '{}'::jsonb,
        created_at timestamptz NOT NULL DEFAULT now(),
        CONSTRAINT ck_qc_task_log_level CHECK (
            level IN ('INFO', 'WARN', 'ERROR', 'EXEC')
        )
    )
    """,
    """
    COMMENT ON TABLE qc_task_log IS
    '任务执行领域日志：由 Worker 旁路观察者按关键节点写入，供任务详情页展示与导出'
    """,
    "CREATE INDEX idx_qc_task_log_task ON qc_task_log (task_id, id)",
    "CREATE INDEX idx_qc_task_log_task_level ON qc_task_log (task_id, level, id)",
    """
    CREATE TABLE qc_task_page_node (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        task_id uuid NOT NULL REFERENCES qc_task(id) ON DELETE CASCADE,
        agent_run_id uuid NOT NULL REFERENCES qc_agent_run(id) ON DELETE CASCADE,
        node_seq integer NOT NULL,
        source varchar(16) NOT NULL DEFAULT 'journey',
        step_name varchar(256) NOT NULL,
        page_type varchar(64),
        status varchar(32) NOT NULL DEFAULT 'pending',
        page_capture_id uuid REFERENCES qc_page_capture(id) ON DELETE SET NULL,
        candidate_rule_total integer,
        current_rule_code varchar(128),
        first_visited_at timestamptz,
        finished_at timestamptz,
        created_at timestamptz NOT NULL DEFAULT now(),
        updated_at timestamptz NOT NULL DEFAULT now(),
        CONSTRAINT uq_qc_task_page_node_seq UNIQUE (agent_run_id, node_seq),
        CONSTRAINT ck_qc_task_page_node_source CHECK (
            source IN ('journey', 'discovered')
        ),
        CONSTRAINT ck_qc_task_page_node_status CHECK (
            status IN ('pending', 'visiting', 'recognized', 'failed', 'skipped')
        )
    )
    """,
    """
    COMMENT ON TABLE qc_task_page_node IS
    '任务旅程页面节点：任务启动时按 journey_steps 物化计划节点，运行中由观察者'
    '关联实际采集与识别结果；计划外访问的页面以 source=discovered 追加'
    """,
    """
    COMMENT ON COLUMN qc_task_page_node.candidate_rule_total IS
    '该页面候选规则总数（candidate_rules_tool 返回的精确值），未查询前为 NULL'
    """,
    """
    COMMENT ON COLUMN qc_task_page_node.current_rule_code IS
    '当前正在执行的规则编码（观察 rule_judge/record 调用推断），页面完成后清空'
    """,
    "CREATE INDEX idx_qc_task_page_node_task ON qc_task_page_node (task_id, node_seq)",
    """
    ALTER TABLE qc_page_capture
        ADD COLUMN detection_status varchar(32),
        ADD COLUMN detection_confidence numeric(5,4),
        ADD COLUMN detection_duration_ms integer,
        ADD COLUMN element_count integer
    """,
    """
    ALTER TABLE qc_page_capture
        ADD CONSTRAINT ck_qc_page_capture_detection_status CHECK (
            detection_status IS NULL
            OR detection_status IN ('recognized', 'ambiguous', 'insufficient', 'error')
        )
    """,
    """
    COMMENT ON COLUMN qc_page_capture.detection_status IS
    '页面识别结论（page_detection_tool 返回，观察者回写）：recognized/ambiguous/insufficient/error'
    """,
    """
    COMMENT ON COLUMN qc_page_capture.detection_confidence IS '页面识别置信度 0-1（观察者回写）'
    """,
    """
    COMMENT ON COLUMN qc_page_capture.detection_duration_ms IS '页面识别耗时毫秒（观察者回写）'
    """,
    """
    COMMENT ON COLUMN qc_page_capture.element_count IS
    '页面可交互元素计数（a/button/input/select/textarea/img/video，观察者从已存 DOM 统计）'
    """,
    """
    ALTER TABLE qc_rule
        ADD COLUMN rule_package varchar(128),
        ADD COLUMN trigger_display varchar(512)
    """,
    """
    COMMENT ON COLUMN qc_rule.rule_package IS
    '所属规则包展示名（如"广告质检"）；NULL 时读侧按 category 兜底'
    """,
    """
    COMMENT ON COLUMN qc_rule.trigger_display IS
    '触发条件展示文案（如"识别到支付/价格页时触发"）；NULL 时读侧按适用页面类型兜底'
    """,
]

_DOWNGRADE_STATEMENTS = [
    "ALTER TABLE qc_rule DROP COLUMN IF EXISTS trigger_display",
    "ALTER TABLE qc_rule DROP COLUMN IF EXISTS rule_package",
    """
    ALTER TABLE qc_page_capture
        DROP CONSTRAINT IF EXISTS ck_qc_page_capture_detection_status
    """,
    "ALTER TABLE qc_page_capture DROP COLUMN IF EXISTS element_count",
    "ALTER TABLE qc_page_capture DROP COLUMN IF EXISTS detection_duration_ms",
    "ALTER TABLE qc_page_capture DROP COLUMN IF EXISTS detection_confidence",
    "ALTER TABLE qc_page_capture DROP COLUMN IF EXISTS detection_status",
    "DROP TABLE IF EXISTS qc_task_page_node",
    "DROP TABLE IF EXISTS qc_task_log",
]


def upgrade() -> None:
    for statement in _UPGRADE_STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    for statement in _DOWNGRADE_STATEMENTS:
        op.execute(statement)
