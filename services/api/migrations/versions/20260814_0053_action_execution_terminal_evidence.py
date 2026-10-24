"""允许动作失败终态在不改变终态的前提下绑定 After Capture。"""

from __future__ import annotations

from alembic import op

revision = "20260814_0053"
down_revision = "20260814_0052"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 先检查历史数据，避免替换约束时把不完整的终态证据事实放进新合同。
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1
                FROM qc_action_execution AS action
                LEFT JOIN qc_capture AS result
                    ON result.id = action.result_capture_id
                WHERE action.status IN ('blocked', 'failed', 'timed_out')
                  AND (
                    (action.result_capture_id IS NULL
                        AND action.evidence_finished_at IS NOT NULL)
                    OR (action.result_capture_id IS NOT NULL
                        AND action.evidence_finished_at IS NULL)
                    OR action.result_capture_id IS NOT NULL AND (
                        action.source_capture_id IS NULL
                        OR action.started_at IS NULL
                        OR action.action_finished_at IS NULL
                        OR action.error_code IS NULL
                        OR result.id IS NULL
                        OR result.task_id IS DISTINCT FROM action.task_id
                        OR result.surface_session_id IS DISTINCT FROM action.session_id
                        OR result.surface_kind IS DISTINCT FROM (
                            SELECT session.surface_kind
                            FROM qc_surface_session AS session
                            WHERE session.task_id = action.task_id
                              AND session.session_id = action.session_id
                        )
                        OR result.parent_capture_id IS DISTINCT FROM action.source_capture_id
                        OR result.action_execution_id
                            IS DISTINCT FROM action.action_execution_id
                        OR result.capture_role IS DISTINCT FROM 'after_action'
                        OR result.capture_status IS DISTINCT FROM 'captured'
                        OR result.session_command_sequence
                            IS DISTINCT FROM action.command_sequence
                    )
                  )
            ) THEN
                RAISE EXCEPTION
                    '存在不符合 0052 合同的动作终态证据，拒绝替换状态约束';
            END IF;
        END
        $$;
        """
    )
    op.execute(
        """
        ALTER TABLE qc_action_execution
        DROP CONSTRAINT IF EXISTS ck_qc_action_execution_state
        """
    )
    op.execute(
        """
        ALTER TABLE qc_action_execution
        ADD CONSTRAINT ck_qc_action_execution_state CHECK (
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
                AND result_capture_id IS NULL
                AND evidence_finished_at IS NULL
                AND error_code IS NOT NULL
            )
            OR (
                status = 'failed'
                AND action_finished_at IS NOT NULL
                AND result_capture_id IS NULL
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
            OR (
                status IN ('blocked', 'failed', 'timed_out')
                AND source_capture_id IS NOT NULL
                AND started_at IS NOT NULL
                AND action_finished_at IS NOT NULL
                AND result_capture_id IS NOT NULL
                AND evidence_finished_at IS NOT NULL
                AND error_code IS NOT NULL
            )
        )
        """
    )


def downgrade() -> None:
    # 旧约束无法表达失败终态的 After 证据，存在此类事实时必须停止回滚。
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1
                FROM qc_action_execution
                WHERE status IN ('blocked', 'failed', 'timed_out')
                  AND (
                    result_capture_id IS NOT NULL
                    OR evidence_finished_at IS NOT NULL
                  )
            ) THEN
                RAISE EXCEPTION
                    '存在 terminal-with-evidence 动作事实，不能降级 0052';
            END IF;
        END
        $$;
        """
    )
    op.execute(
        """
        ALTER TABLE qc_action_execution
        DROP CONSTRAINT IF EXISTS ck_qc_action_execution_state
        """
    )
    op.execute(
        """
        ALTER TABLE qc_action_execution
        ADD CONSTRAINT ck_qc_action_execution_state CHECK (
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
                AND result_capture_id IS NULL
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
        """
    )
