"""H5 新会话补采保存页面图路线证明，不伪造 Journey 路线外键。"""

from alembic import op

revision = "20261008_0131"
down_revision = "20260930_0130"
branch_labels = None
depends_on = None

H5_GRAPH_REPLAY = """(
    strategy = 'new_session_replay' AND surface_kind = 'web' AND source_grade = 'C'
    AND replay_execution_plan_id IS NULL
    AND source_session_id ~ '^h5:[0-9a-f-]{36}(:recovery:[0-9a-f]{20})?$'
    AND target_session_id ~ '^h5:[0-9a-f-]{36}:recapture:[0-9a-f]{20}$'
    AND split_part(source_session_id, ':', 2) = split_part(target_session_id, ':', 2)
    AND COALESCE(plan_payload->>'replay_execution_plan_id', '')
        ~ '^h5-new-session-route:[0-9a-f]{64}$'
    AND COALESCE(plan_payload->>'source_session_id', '') = source_session_id
    AND COALESCE(plan_payload->>'target_session_id', '') = target_session_id
    AND COALESCE(plan_payload->>'strategy', '') = strategy
)"""

ORIGINAL_REPLAY = """(
    (strategy IN ('same_context_new_surface', 'same_app_session', 'new_session_replay')
        AND replay_execution_plan_id IS NOT NULL)
    OR (strategy NOT IN ('same_context_new_surface', 'same_app_session', 'new_session_replay')
        AND replay_execution_plan_id IS NULL)
)"""


def upgrade() -> None:
    op.execute(
        "ALTER TABLE qc_restoration_attempt DROP CONSTRAINT ck_qc_restoration_attempt_replay"
    )
    op.execute(
        "ALTER TABLE qc_restoration_attempt ADD CONSTRAINT ck_qc_restoration_attempt_replay CHECK ("
        + ORIGINAL_REPLAY
        + " OR "
        + H5_GRAPH_REPLAY
        + ")"
    )


def downgrade() -> None:
    # 存在页面图恢复事实时拒绝降级，不删除审计数据或把它伪装为 Journey。
    op.execute(
        "ALTER TABLE qc_restoration_attempt DROP CONSTRAINT ck_qc_restoration_attempt_replay"
    )
    op.execute(
        "ALTER TABLE qc_restoration_attempt ADD CONSTRAINT ck_qc_restoration_attempt_replay CHECK "
        + ORIGINAL_REPLAY
    )
