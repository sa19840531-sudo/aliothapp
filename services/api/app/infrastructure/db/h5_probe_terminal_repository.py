"""H5 Probe 失败终态与 Evidence Request 的 PostgreSQL 收口。"""

from __future__ import annotations

from typing import Any

from services.api.app.application.exploration_work_queue import ExplorerWorkStatus
from services.api.app.application.h5_probe_work import H5ProbeWorkOutcome
from services.api.app.infrastructure.db.h5_snapshot_recovery_scope import (
    h5_snapshot_request_constraints_sql,
)
from services.api.app.infrastructure.db.pool import get_postgres_pool


class PostgresH5ProbeTerminalSettlementRepository:
    """只结算当前 H5 Task/Run/Epoch 内已无法继续的 Web Probe。"""

    def __init__(self, *, database_url: str | None = None, pool: Any | None = None,
                 allow_new_session_recovery: bool = False) -> None:
        self._database_url = database_url
        self._pool = pool
        self._allow_new_session_recovery = allow_new_session_recovery

    async def settle_completion_failure(
        self, *, task_id: str, agent_run_id: str,
        outcome: H5ProbeWorkOutcome, error_code: str,
    ) -> int:
        """动作成功但附件永久不可用时，只终结未挂接请求，不改写成功动作。"""
        for value, field in (
            (task_id, "task_id"), (agent_run_id, "agent_run_id"),
            (outcome.work_id, "work_id"), (outcome.evidence_request_id, "request_id"),
            (outcome.result_capture_id, "result_capture_id"), (error_code, "error_code"),
        ):
            _require_text(value, field)
        if outcome.final_status is not ExplorerWorkStatus.SUCCEEDED:
            raise ValueError("只允许结算已成功 Probe 的挂接失败")
        pool = self._pool or await get_postgres_pool(self._database_url)
        async with pool.acquire() as connection, connection.transaction():
            await connection.execute(
                "SELECT pg_advisory_xact_lock(hashtext($1))", f"h5-probe-terminal:{task_id}",
            )
            settled = await connection.fetchval(
                """
                WITH settled AS (
                    UPDATE qc_evidence_request AS request
                    SET status = 'unfulfillable',
                        resolution_reason = 'h5_probe_attachment_failed:' || $6,
                        lease_owner = NULL, lease_token = NULL, lease_expires_at = NULL,
                        completed_at = now(), updated_at = now(),
                        row_version = request.row_version + 1
                    FROM qc_task AS task, qc_v2_work_item AS work,
                         qc_action_execution AS action, qc_capture AS result_capture,
                         qc_capture AS source_capture
                    WHERE task.id = $1::uuid AND task.target_platform = 'h5'
                      AND task.status IN ('running', 'review')
                      AND task.finalization_state IN (
                          'open', 'exploration_draining', 'session_draining')
                      AND request.task_id = task.id
                      AND request.request_id = $4
                      AND request.finalization_epoch = task.finalization_epoch
                      AND request.session_dependency IN ('active_session', 'snapshot')
                      AND request.status IN ('routed', 'running')
                      AND work.task_id = task.id AND work.work_id = $3
                      AND work.evidence_request_id = request.request_id
                      AND work.finalization_epoch = task.finalization_epoch
                      AND work.work_type = 'probe' AND work.status = 'succeeded'
                      AND work.work_payload->>'surface_kind' = 'web'
                      AND source_capture.id = work.source_capture_id
                      AND source_capture.task_id = task.id
                      AND source_capture.agent_run_id = $2::uuid
                      AND action.task_id = task.id AND action.status = 'completed'
                      AND action.session_id = work.session_id
                      AND action.finalization_epoch = task.finalization_epoch
                      AND action.intent_payload
                            #>> '{parameters,exploration_work_id}' = work.work_id
                      AND action.result_capture_id = result_capture.id
                      AND result_capture.id = $5::uuid AND result_capture.task_id = task.id
                      AND result_capture.agent_run_id = $2::uuid
                      AND result_capture.surface_session_id = work.session_id
                      AND result_capture.action_execution_id = action.action_execution_id
                      AND result_capture.capture_role = 'probe_result_replay'
                      AND NOT EXISTS (
                          SELECT 1 FROM qc_evidence_supplement AS supplement
                          WHERE supplement.request_id = request.request_id
                      )
                    RETURNING request.request_id
                ) SELECT count(*) FROM settled
                """,
                task_id, agent_run_id, outcome.work_id, outcome.evidence_request_id,
                outcome.result_capture_id, error_code,
            )
        return int(settled or 0)

    async def settle_ready(self, *, task_id: str, agent_run_id: str) -> int:
        _require_text(task_id, "task_id")
        _require_text(agent_run_id, "agent_run_id")
        pool = self._pool or await get_postgres_pool(self._database_url)
        async with pool.acquire() as connection, connection.transaction():
            await connection.execute(
                "SELECT pg_advisory_xact_lock(hashtext($1))",
                f"h5-probe-terminal:{task_id}",
            )
            # 领取端会跳过过期请求；没有本地终结入口时它们会永久卡住关闭屏障。
            expired = await connection.fetchval(
                """
                WITH expired_request AS (
                    UPDATE qc_evidence_request AS request
                    SET status = 'expired', resolution_reason = 'EVIDENCE_REQUEST_EXPIRED',
                        lease_owner = NULL, lease_token = NULL, lease_expires_at = NULL,
                        row_version = request.row_version + 1,
                        completed_at = now(), updated_at = now()
                    FROM qc_task AS task, qc_capture AS target_capture
                    WHERE task.id = $1::uuid
                      AND task.target_platform = 'h5'
                      AND task.status IN ('running', 'review')
                      AND task.finalization_state IN (
                          'open', 'exploration_draining', 'session_draining'
                      )
                      AND request.task_id = task.id
                      AND request.finalization_epoch = task.finalization_epoch
                      AND request.session_dependency IN ('active_session', 'snapshot')
                      AND request.status IN ('pending', 'routed', 'running')
                      AND request.expires_at <= now()
                      AND target_capture.id = request.target_capture_id
                      AND target_capture.task_id = task.id
                      AND target_capture.agent_run_id = $2::uuid
                    RETURNING request.request_id
                ), cancelled_probe AS (
                    UPDATE qc_v2_work_item AS probe
                    SET status = 'cancelled', last_error_code = 'EVIDENCE_REQUEST_EXPIRED',
                        last_error_message = '关联 H5 补采请求已过期',
                        lease_owner = NULL, lease_token = NULL, lease_expires_at = NULL,
                        row_version = probe.row_version + 1,
                        finished_at = now(), updated_at = now()
                    FROM expired_request
                    WHERE probe.task_id = $1::uuid
                      AND probe.work_type = 'probe'
                      AND probe.work_payload->>'surface_kind' = 'web'
                      AND probe.evidence_request_id = expired_request.request_id
                      AND probe.finalization_epoch = (
                          SELECT finalization_epoch FROM qc_task WHERE id = $1::uuid
                      )
                      AND probe.status IN ('pending', 'leased', 'running', 'retry_wait')
                    RETURNING probe.work_id
                )
                SELECT count(*) FROM expired_request
                """,
                task_id,
                agent_run_id,
            )
            await connection.execute(
                """
                UPDATE qc_v2_work_item AS probe
                SET
                    status = 'failed',
                    last_error_code = CASE
                        WHEN probe.deadline <= now()
                        THEN 'H5_PROBE_DEADLINE_EXHAUSTED'
                        ELSE 'H5_PROBE_ATTEMPTS_EXHAUSTED'
                    END,
                    last_error_message = CASE
                        WHEN probe.deadline <= now()
                        THEN 'H5 Probe 已超过截止时间，不能继续执行'
                        ELSE 'H5 Probe 已耗尽工作项重试次数'
                    END,
                    lease_owner = NULL,
                    lease_token = NULL,
                    lease_expires_at = NULL,
                    finished_at = now(),
                    row_version = probe.row_version + 1,
                    updated_at = now()
                FROM qc_task AS task
                INNER JOIN qc_evidence_request AS request
                    ON request.task_id = task.id
                   AND request.finalization_epoch = task.finalization_epoch
                INNER JOIN qc_capture AS target_capture
                    ON target_capture.id = request.target_capture_id
                   AND target_capture.task_id = request.task_id
                   AND target_capture.agent_run_id = $2::uuid
                WHERE task.id = $1::uuid
                  AND task.target_platform = 'h5'
                  AND task.status IN ('running', 'review')
                  AND task.finalization_state IN (
                      'open', 'exploration_draining', 'session_draining'
                  )
                  AND probe.task_id = task.id
                  AND probe.finalization_epoch = task.finalization_epoch
                  AND probe.work_type = 'probe'
                  AND probe.evidence_request_id = request.request_id
                  AND probe.work_payload->>'surface_kind' = 'web'
                  AND probe.status IN (
                      'pending', 'retry_wait', 'leased', 'running'
                  )
                  AND (
                      (
                          probe.status IN ('pending', 'retry_wait')
                          AND (
                              probe.deadline <= now()
                              OR probe.attempt_count >= probe.max_attempts
                          )
                      )
                      OR (
                          probe.status IN ('leased', 'running')
                          AND probe.lease_expires_at <= now()
                          AND (
                              probe.deadline <= now()
                              OR probe.attempt_count >= probe.max_attempts
                          )
                      )
                  )
                """,
                task_id,
                agent_run_id,
            )
            # 进程中断后的兜底仅处理过期 Probe 的空 planned 账本，不结算未知点击。
            await connection.execute(
                """
                UPDATE qc_action_execution AS action
                SET status = 'failed', error_code = 'H5_ACTION_BEFORE_CAPTURE_ABORTED',
                    error_message = 'H5 Probe 截止且命令许可已释放，动作未开始',
                    action_finished_at = now(), evidence_finished_at = now(),
                    row_version = action.row_version + 1, updated_at = now()
                FROM qc_task AS task, qc_v2_work_item AS probe,
                    qc_surface_session AS session, qc_capture AS target
                WHERE task.id = $1::uuid AND task.target_platform = 'h5'
                    AND task.status IN ('running', 'review')
                    AND task.finalization_state IN (
                        'open', 'exploration_draining', 'session_draining')
                    AND action.task_id = task.id
                    AND action.finalization_epoch = task.finalization_epoch
                    AND action.status = 'planned' AND action.started_at IS NULL
                    AND action.source_capture_id IS NULL AND action.result_capture_id IS NULL
                    AND probe.task_id = task.id AND probe.work_type = 'probe'
                    AND probe.work_payload->>'surface_kind' = 'web'
                    AND probe.finalization_epoch = task.finalization_epoch
                    AND probe.work_id = action.intent_payload #>> '{parameters,exploration_work_id}'
                    AND probe.deadline <= now()
                    AND probe.status IN ('failed', 'cancelled', 'budget_exhausted')
                    AND (probe.lease_token IS NULL OR probe.lease_expires_at <= now())
                    AND target.id = probe.source_capture_id AND target.task_id = task.id
                    AND target.agent_run_id = $2::uuid
                    AND session.task_id = task.id AND session.session_id = action.session_id
                    AND session.session_id = probe.session_id AND session.surface_kind = 'web'
                    AND session.finalization_epoch = task.finalization_epoch
                    AND session.command_sequence >= action.command_sequence
                    AND (session.lease_token IS NULL OR session.lease_expires_at <= now())
                    AND NOT EXISTS (SELECT 1 FROM qc_capture AS frame
                        WHERE frame.task_id = task.id
                            AND frame.action_execution_id = action.action_execution_id)
                """, task_id, agent_run_id,
            )
            settled = await connection.fetchval(
                """
                WITH terminal_request AS (
                    UPDATE qc_evidence_request AS request
                    SET
                        status = 'unfulfillable',
                        resolution_reason = CONCAT(
                            'h5_probe_terminal:',
                            COALESCE(terminal_probe.last_error_code, terminal_probe.status)
                        ),
                        lease_owner = NULL,
                        lease_token = NULL,
                        lease_expires_at = NULL,
                        row_version = request.row_version + 1,
                        completed_at = now(),
                        updated_at = now()
                    FROM qc_task AS task
                    INNER JOIN qc_capture AS target_capture
                        ON target_capture.task_id = task.id
                       AND target_capture.agent_run_id = $2::uuid
                    INNER JOIN qc_v2_work_item AS terminal_probe
                        ON terminal_probe.task_id = task.id
                       AND terminal_probe.finalization_epoch = task.finalization_epoch
                       AND terminal_probe.work_type = 'probe'
                       AND terminal_probe.work_payload->>'surface_kind' = 'web'
                       AND terminal_probe.status IN (
                           'failed', 'blocked', 'bounded',
                           'budget_exhausted', 'no_progress'
                       )
                    WHERE task.id = $1::uuid
                      AND task.target_platform = 'h5'
                      AND task.status IN ('running', 'review')
                      AND task.finalization_state IN (
                          'open', 'exploration_draining', 'session_draining'
                      )
                      AND request.task_id = task.id
                      AND request.finalization_epoch = task.finalization_epoch
                      AND request.session_dependency IN ('active_session', 'snapshot')
                      AND request.status IN ('routed', 'running')
                      AND request.target_capture_id = target_capture.id
                      AND terminal_probe.evidence_request_id = request.request_id
                      AND NOT EXISTS (
                          SELECT 1
                          FROM qc_evidence_supplement AS supplement
                          WHERE supplement.request_id = request.request_id
                            AND supplement.probe_work_id = terminal_probe.work_id
                      )
                      AND NOT EXISTS (
                          SELECT 1
                          FROM qc_v2_work_item AS recoverable_probe
                          WHERE recoverable_probe.task_id = request.task_id
                            AND recoverable_probe.evidence_request_id = request.request_id
                            AND recoverable_probe.finalization_epoch = request.finalization_epoch
                            AND recoverable_probe.work_type = 'probe'
                            AND recoverable_probe.work_payload->>'surface_kind' = 'web'
                            AND recoverable_probe.status IN (
                                'pending', 'leased', 'running',
                                'retry_wait', 'succeeded'
                            )
                      )
                    RETURNING request.request_id
                )
                SELECT count(*) FROM terminal_request
                """,
                task_id,
                agent_run_id,
            )
        return int(settled or 0) + int(expired or 0)

    async def settle_requests_after_session_close(
        self,
        *,
        task_id: str,
        agent_run_id: str,
    ) -> int:
        """按真实关闭会话终结无法执行的 H5 请求，不伪造补采成功。"""

        _require_text(task_id, "task_id")
        _require_text(agent_run_id, "agent_run_id")
        pool = self._pool or await get_postgres_pool(self._database_url)
        async with pool.acquire() as connection, connection.transaction():
            await connection.execute(
                "SELECT pg_advisory_xact_lock(hashtext($1))",
                f"task-finalization:{task_id}",
            )
            await connection.execute(
                "SELECT pg_advisory_xact_lock(hashtext($1))",
                f"h5-snapshot-recapture-closure:{task_id}",
            )
            # 新会话执行器持有跨进程锁时，不能在并行 Judge 中提前终结其请求。
            locked = await connection.fetchval(
                "SELECT pg_try_advisory_xact_lock(hashtext($1))",
                f"h5-new-session-recapture:{task_id}",
            )
            if locked is False:
                return 0
            # 主 Run 已终态且执行租约失效时，不再等待不存在的现场。有效租约、
            # 其他执行域和非 H5 owner 均保留；遗留会话记 lost，不伪造正常关闭。
            await connection.execute(
                """
                WITH orphaned_session AS (
                    UPDATE qc_surface_session AS session
                    SET status = 'lost',
                        lease_owner = NULL, lease_token = NULL, lease_expires_at = NULL,
                        current_surface_ref = NULL,
                        failure_reason = 'h5_terminal_run_session_orphaned',
                        lost_at = now(), updated_at = now(),
                        row_version = session.row_version + 1
                    FROM qc_task AS task, qc_agent_run AS main_run
                    WHERE task.id = $1::uuid AND task.target_platform = 'h5'
                      AND task.status IN ('running', 'review')
                      AND task.finalization_state IN (
                          'open', 'exploration_draining', 'session_draining'
                      )
                      AND task.finalization_agent_run_id = $2::uuid
                      AND main_run.id = $2::uuid AND main_run.task_id = task.id
                      AND main_run.agent_type = 'main'
                      AND main_run.status IN ('done', 'failed', 'canceled')
                      AND session.task_id = task.id
                      AND session.finalization_epoch = task.finalization_epoch
                      AND session.surface_kind = 'web'
                      AND session.status IN ('starting', 'active', 'draining', 'closing')
                      AND (session.lease_expires_at IS NULL OR session.lease_expires_at <= now())
                      AND (
                          (session.session_owner = 'h5-workflow'
                           AND session.session_id = 'h5:' || $2::text)
                          OR (session.session_owner = 'h5-browser-recovery:' || $2::text
                              AND session.session_id ~ (
                                  '^h5:' || $2::text || ':recovery:[0-9a-f]{20}$'))
                      )
                    RETURNING session.session_id, session.task_id
                )
                UPDATE qc_surface_ref AS surface
                SET status = 'lost', pin_reason = NULL,
                    released_at = now(), updated_at = now(),
                    row_version = surface.row_version + 1
                FROM orphaned_session
                WHERE surface.task_id = orphaned_session.task_id
                  AND surface.session_id = orphaned_session.session_id
                  AND surface.status NOT IN ('released', 'lost')
                """,
                task_id,
                agent_run_id,
            )
            await connection.execute(
                """
                UPDATE qc_v2_work_item AS probe
                SET
                    status = 'failed',
                    last_error_code = CASE WHEN EXISTS (
                        SELECT 1 FROM qc_surface_session AS source_session
                        WHERE source_session.session_id = target_capture.surface_session_id
                          AND source_session.task_id = task.id
                          AND source_session.status = 'lost'
                    ) THEN 'H5_RECAPTURE_SESSION_LOST' ELSE 'H5_RECAPTURE_SESSION_CLOSED' END,
                    last_error_message = 'H5 来源会话已关闭或丢失，无法继续执行现场补采',
                    lease_owner = NULL,
                    lease_token = NULL,
                    lease_expires_at = NULL,
                    finished_at = now(),
                    row_version = probe.row_version + 1,
                    updated_at = now()
                FROM qc_task AS task
                INNER JOIN qc_evidence_request AS request
                    ON request.task_id = task.id
                   AND request.finalization_epoch = task.finalization_epoch
                INNER JOIN qc_capture AS target_capture
                    ON target_capture.id = request.target_capture_id
                   AND target_capture.task_id = request.task_id
                   AND target_capture.agent_run_id = $2::uuid
                INNER JOIN qc_inspection_work_item AS work_item
                    ON work_item.id = request.work_item_id
                   AND work_item.task_id = request.task_id
                   AND work_item.finalization_epoch = request.finalization_epoch
                   AND work_item.producer_agent_run_id = $2::uuid
                   AND work_item.control_mode = 'enforced'
                WHERE task.id = $1::uuid
                  AND task.target_platform = 'h5'
                  AND task.status IN ('running', 'review')
                  AND task.finalization_state IN (
                      'open', 'exploration_draining', 'session_draining'
                  )
                  AND request.session_dependency IN ('active_session', 'snapshot')
                  AND request.status IN ('pending', 'routed', 'running')
                  AND probe.task_id = task.id
                  AND probe.finalization_epoch = task.finalization_epoch
                  AND probe.work_type = 'probe'
                  AND probe.evidence_request_id = request.request_id
                  AND probe.work_payload->>'surface_kind' = 'web'
                  AND probe.status IN ('pending', 'leased', 'running', 'retry_wait')
                  AND EXISTS (
                      SELECT 1 FROM qc_surface_session AS replay_session
                      WHERE replay_session.session_id = COALESCE(
                            probe.work_payload->>'session_id', target_capture.surface_session_id)
                        AND replay_session.task_id = task.id
                        AND replay_session.finalization_epoch = task.finalization_epoch
                        AND (replay_session.status IN ('closed', 'lost')
                             OR replay_session.session_owner NOT LIKE 'h5-snapshot-recapture:%')
                  )
                  AND EXISTS (
                      SELECT 1 FROM qc_surface_session AS session
                      WHERE session.session_id = target_capture.surface_session_id
                        AND session.task_id = task.id
                        AND session.finalization_epoch = task.finalization_epoch
                        AND session.surface_kind = 'web'
                        AND session.status IN ('closed', 'lost')
                        AND (
                            (session.session_owner = 'h5-workflow'
                             AND session.session_id = 'h5:' || $2::text)
                            OR (session.session_owner = 'h5-browser-recovery:' || $2::text
                                AND session.session_id ~ (
                                    '^h5:' || $2::text || ':recovery:[0-9a-f]{20}$'))
                        )
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM qc_evidence_supplement AS supplement
                      WHERE supplement.request_id = request.request_id
                  )
                """,
                task_id,
                agent_run_id,
            )
            settled = await connection.fetchval(
                """
                WITH terminal_request AS (
                    UPDATE qc_evidence_request AS request
                    SET
                        status = 'unfulfillable',
                        resolution_reason = CASE WHEN EXISTS (
                            SELECT 1 FROM qc_surface_session AS source_session
                            WHERE source_session.session_id = target_capture.surface_session_id
                              AND source_session.task_id = task.id
                              AND source_session.status = 'lost'
                        ) THEN 'H5_RECAPTURE_SESSION_LOST' ELSE 'H5_RECAPTURE_SESSION_CLOSED' END,
                        lease_owner = NULL,
                        lease_token = NULL,
                        lease_expires_at = NULL,
                        row_version = request.row_version + 1,
                        completed_at = now(),
                        updated_at = now()
                    FROM qc_task AS task
                    INNER JOIN qc_capture AS target_capture
                        ON target_capture.task_id = task.id
                       AND target_capture.agent_run_id = $2::uuid
                    INNER JOIN qc_inspection_work_item AS work_item
                        ON work_item.task_id = task.id
                       AND work_item.producer_agent_run_id = $2::uuid
                       AND work_item.finalization_epoch = task.finalization_epoch
                       AND work_item.control_mode = 'enforced'
                    WHERE task.id = $1::uuid
                      AND task.target_platform = 'h5'
                      AND task.status IN ('running', 'review')
                      AND task.finalization_state IN (
                          'open', 'exploration_draining', 'session_draining'
                      )
                      AND request.task_id = task.id
                      AND request.work_item_id = work_item.id
                      AND request.target_capture_id = target_capture.id
                      AND request.finalization_epoch = task.finalization_epoch
                      AND request.session_dependency IN ('active_session', 'snapshot')
                      AND request.status IN ('pending', 'routed', 'running')
                      AND EXISTS (
                          SELECT 1 FROM qc_surface_session AS session
                          WHERE session.session_id = target_capture.surface_session_id
                            AND session.task_id = task.id
                            AND session.finalization_epoch = task.finalization_epoch
                            AND session.surface_kind = 'web'
                            AND session.status IN ('closed', 'lost')
                            AND (
                                (session.session_owner = 'h5-workflow'
                                 AND session.session_id = 'h5:' || $2::text)
                                OR (session.session_owner = 'h5-browser-recovery:' || $2::text
                                    AND session.session_id ~ (
                                        '^h5:' || $2::text || ':recovery:[0-9a-f]{20}$'))
                            )
                      )
                      AND NOT EXISTS (
                          SELECT 1
                          FROM qc_evidence_supplement AS supplement
                          WHERE supplement.request_id = request.request_id
                      )
                      AND NOT (__PRESERVE_NEW_SESSION_REQUEST__)
                    RETURNING request.request_id
                )
                SELECT count(*) FROM terminal_request
                """.replace("__PRESERVE_NEW_SESSION_REQUEST__", (
                    h5_snapshot_request_constraints_sql()
                    + """ AND request.expires_at > now() AND (
                        request.attempt_count < request.max_attempts
                        OR (request.lease_token IS NOT NULL AND request.lease_expires_at > now())
                        OR (request.status IN ('routed', 'running') AND EXISTS (
                            SELECT 1 FROM qc_v2_work_item AS recovery_probe
                            WHERE recovery_probe.evidence_request_id = request.request_id
                              AND recovery_probe.task_id = request.task_id
                              AND recovery_probe.finalization_epoch = request.finalization_epoch
                              AND recovery_probe.work_type = 'probe'
                              AND recovery_probe.status IN (
                                  'pending', 'retry_wait', 'leased', 'running', 'succeeded'
                              )
                        ))
                    )"""
                    if self._allow_new_session_recovery else "false"
                )),
                task_id,
                agent_run_id,
            )
        return int(settled or 0)


def _require_text(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError(f"{field_name} 必须为无首尾空白的非空字符串")


__all__ = ["PostgresH5ProbeTerminalSettlementRepository"]
