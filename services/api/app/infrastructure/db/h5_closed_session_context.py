"""从原 H5 主 Run 的已关闭会话读取新会话补采起点和执行围栏。"""

import hashlib
from datetime import datetime
from typing import Any

from services.api.app.application.evidence_recovery_replay import EvidenceRecoveryReplayError
from services.api.app.application.h5_targeted_recapture import H5TargetedRecaptureContext
from services.api.app.domain.inspection import EvidenceRequest
from services.api.app.infrastructure.db.h5_snapshot_recovery_scope import (
    h5_snapshot_task_permission_sql,
)
from services.api.app.infrastructure.db.h5_targeted_recapture_repository import (
    PostgresH5TargetedRecaptureRepository,
)


class PostgresH5ClosedSessionContextReader(PostgresH5TargetedRecaptureRepository):
    """活会话沿用旧读取器；仅关闭后的 Snapshot 进入新会话规划。"""

    async def read(self, *, request: EvidenceRequest) -> H5TargetedRecaptureContext | None:
        active = await super().read(request=request)
        if active is not None:
            return active
        return await self._read_replay_context(request=request, prospective=False)

    async def read_snapshot_replay_context(
        self, *, request: EvidenceRequest,
    ) -> H5TargetedRecaptureContext | None:
        """预检活会话的入口恢复路线；不能据此领取或执行补采。"""
        return await self._read_replay_context(request=request, prospective=True)

    async def read_isolated_display_context(
        self, *, request: EvidenceRequest,
    ) -> H5TargetedRecaptureContext | None:
        """活会话隔离补证也必须持有有效租约；预检不能转成执行许可。"""
        return await self._read_replay_context(
            request=request, prospective=True, require_lease=True,
        )

    async def _read_replay_context(
        self, *, request: EvidenceRequest, prospective: bool, require_lease: bool = False,
    ) -> H5TargetedRecaptureContext | None:
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                SELECT task.id::text AS task_id, capture.agent_run_id::text AS agent_run_id,
                    task.finalization_epoch, task.target_url AS entry_url,
                    session.session_id, request.attempt_count,
                    target.state_id::text AS target_state_id,
                    target.logical_page_key AS target_identity, target_page.url AS target_url,
                    root.state_id::text AS entry_capture_id,
                    root.logical_page_key AS entry_identity, root.url AS entry_capture_url,
                    root.counts_as_business_page AS entry_normalization_required
                FROM qc_evidence_request AS request
                INNER JOIN qc_task AS task ON task.id = request.task_id
                    AND task.finalization_epoch = request.finalization_epoch
                INNER JOIN qc_capture AS capture ON capture.id = request.target_capture_id
                    AND capture.task_id = task.id AND capture.surface_kind = 'web'
                    AND capture.capture_status = 'captured'
                INNER JOIN qc_surface_session AS session
                    ON session.session_id = capture.surface_session_id
                    AND session.task_id = task.id AND session.surface_kind = 'web'
                    AND session.finalization_epoch = task.finalization_epoch
                    AND (($3 AND session.status IN ('active', 'draining'))
                         OR (NOT $3 AND session.status IN ('closed', 'lost')))
                    AND ((session.session_owner = 'h5-workflow'
                          AND session.session_id = 'h5:' || capture.agent_run_id::text)
                         OR (session.session_owner =
                             'h5-browser-recovery:' || capture.agent_run_id::text
                             AND session.session_id ~ ('^h5:' || capture.agent_run_id::text
                                 || ':recovery:[0-9a-f]{20}$')))
                INNER JOIN qc_page_graph_state AS target ON target.state_id = capture.id
                    AND target.task_id = task.id AND target.agent_run_id = capture.agent_run_id
                    AND target.finalization_epoch = task.finalization_epoch
                    AND target.surface_session_id = session.session_id
                    AND target.identity_status = 'confirmed'
                    AND target.identity_source = 'logical_surface'
                INNER JOIN qc_page_capture AS target_page ON target_page.id = capture.id
                    AND target_page.task_id = task.id
                INNER JOIN LATERAL (
                    SELECT state.state_id, state.logical_page_key, page.url,
                        state.counts_as_business_page
                    FROM qc_page_graph_state AS state
                    INNER JOIN qc_capture AS entry ON entry.id = state.state_id
                        AND entry.task_id = task.id AND entry.agent_run_id = capture.agent_run_id
                        AND entry.surface_session_id = session.session_id
                        AND entry.capture_status = 'captured'
                        AND entry.surface_kind = 'web'
                        AND entry.capture_role IN ('bootstrap', 'after_action')
                    INNER JOIN qc_page_capture AS page ON page.id = entry.id
                        AND page.task_id = task.id
                    WHERE state.task_id = task.id AND state.agent_run_id = capture.agent_run_id
                      AND state.finalization_epoch = task.finalization_epoch
                      AND state.surface_session_id = session.session_id
                      AND state.identity_status = 'confirmed'
                      AND state.identity_source = 'logical_surface'
                      AND NOT state.is_external
                      AND (NOT state.counts_as_business_page OR NOT EXISTS (
                            SELECT 1 FROM qc_action_execution AS prior
                            WHERE prior.task_id = task.id AND prior.session_id = session.session_id
                              AND prior.finalization_epoch = task.finalization_epoch
                              AND prior.started_at <= entry.captured_at
                              AND NOT COALESCE((
                                  prior.status = 'completed'
                                  AND prior.intent_payload->>'risk_level' = 'low'
                                  AND (
                                      prior.intent_payload->>'action_type' = 'observe'
                                      OR (prior.intent_payload->>'action_type' = 'navigate'
                                          AND prior.intent_payload->'parameters'
                                              ->>'initial_surface_action' = 'true'
                                          AND prior.intent_payload->'parameters'
                                              ->>'destination_url' = task.target_url)
                                      OR (prior.intent_payload->>'action_type' = 'click'
                                          AND prior.intent_payload->'parameters'->>'target_mode'
                                              IN ('startup_ad_dismissal', 'popup_dismissal'))
                                  )
                              ), false)
                        ))
                    ORDER BY CASE WHEN state.counts_as_business_page THEN 0 ELSE 1 END,
                        entry.captured_at, entry.id LIMIT 1
                ) AS root ON true
                WHERE request.request_id = $1 AND request.task_id = $2::uuid
                  AND request.status = 'pending' AND request.expires_at > now()
                  AND (($3 AND NOT $4) OR (request.lease_token IS NOT NULL
                                           AND request.lease_expires_at > now()))
                  AND __H5_SNAPSHOT_TASK_PERMISSION__
                """.replace("__H5_SNAPSHOT_TASK_PERMISSION__", h5_snapshot_task_permission_sql()),
                request.request_id,
                request.task_id,
                prospective,
                require_lease,
            )
        if row is None:
            return None
        suffix = hashlib.sha256(
            f"{request.request_id}:{row['finalization_epoch']}:{row['attempt_count']}".encode()
        ).hexdigest()[:20]
        return H5TargetedRecaptureContext(
            task_id=row["task_id"],
            agent_run_id=row["agent_run_id"],
            finalization_epoch=int(row["finalization_epoch"]),
            session_id=row["session_id"],
            current_capture_id=row["entry_capture_id"],
            current_identity=row["entry_identity"],
            current_url=row["entry_capture_url"],
            target_state_id=row["target_state_id"],
            target_identity=row["target_identity"],
            target_url=row["target_url"],
            source_session_closed=not prospective,
            entry_url=row["entry_url"],
            entry_normalization_required=bool(row.get("entry_normalization_required")),
            replay_session_id=f"h5:{row['agent_run_id']}:recapture:{suffix}",
        )

    async def read_h5_epoch(self, *, task_id: str, agent_run_id: str) -> int | None:
        active_epoch = await super().read_h5_epoch(task_id=task_id, agent_run_id=agent_run_id)
        if active_epoch is not None:
            return active_epoch
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            epoch = await connection.fetchval(
                """
                SELECT task.finalization_epoch FROM qc_task AS task
                INNER JOIN qc_capture AS capture ON capture.task_id = task.id
                    AND capture.agent_run_id = $2::uuid
                INNER JOIN qc_surface_session AS session
                    ON session.session_id = capture.surface_session_id
                    AND session.task_id = task.id
                    AND session.finalization_epoch = task.finalization_epoch
                    AND session.status IN ('closed', 'lost')
                    AND session.surface_kind = 'web'
                    AND ((session.session_owner = 'h5-workflow'
                          AND session.session_id = 'h5:' || $2::text)
                         OR (session.session_owner = 'h5-browser-recovery:' || $2::text
                             AND session.session_id ~
                                ('^h5:' || $2::text || ':recovery:[0-9a-f]{20}$')))
                WHERE task.id = $1::uuid AND __H5_SNAPSHOT_TASK_PERMISSION__ LIMIT 1
                """.replace("__H5_SNAPSHOT_TASK_PERMISSION__", h5_snapshot_task_permission_sql()),
                task_id,
                agent_run_id,
            )
        return int(epoch) if epoch is not None else None

    async def check_budget(
        self,
        *,
        task_id: str,
        agent_run_id: str,
        finalization_epoch: int,
        max_actions: int,
        deadline: datetime,
    ) -> None:
        """每个动作前读取持久事实；新会话不获得新的动作或墙钟预算。"""
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                SELECT task.finalization_epoch,
                    (SELECT count(*) FROM qc_action_execution AS action
                        INNER JOIN qc_surface_session AS session
                            ON session.session_id = action.session_id
                            AND session.task_id = task.id
                        WHERE action.task_id = task.id
                          AND (session.session_id = 'h5:' || $2::text
                               OR session.session_owner IN ('h5-browser-recovery:' || $2::text,
                                   'h5-snapshot-recapture:' || $2::text))) AS actions
                FROM qc_task AS task INNER JOIN qc_agent_run AS main_run
                    ON main_run.id = $2::uuid AND main_run.task_id = task.id
                    AND main_run.agent_type = 'main'
                    AND main_run.status IN ('running', 'done', 'failed')
                WHERE task.id = $1::uuid AND task.target_platform = 'h5'
                  AND task.status IN ('running', 'review')
                  AND task.finalization_state IN (
                      'open', 'exploration_draining', 'session_draining')
                  AND task.finalization_epoch = $3
                  AND (task.finalization_agent_run_id = main_run.id
                       OR (task.status = 'running' AND task.finalization_state = 'open'
                           AND task.finalization_agent_run_id IS NULL))
                  AND now() < $4
                """,
                task_id,
                agent_run_id,
                finalization_epoch,
                deadline,
            )
        if row is None or int(row["actions"]) >= max_actions:
            error = EvidenceRecoveryReplayError(
                reason_code="H5_NEW_SESSION_SCOPE_OR_BUDGET_EXHAUSTED",
                message="H5 新会话补采已失去当前任务执行许可或原预算已耗尽",
            )
            error.retryable = False
            raise error

    async def frozen_frontier(self, *, task_id: str, agent_run_id: str) -> Any | None:
        """恢复器只能沿用已冻结 Work 的截止时间与预算，不能重算一轮预算。"""
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            return await connection.fetchrow(
                """
                SELECT work.work_payload, work.finalization_epoch FROM qc_v2_work_item AS work
                INNER JOIN qc_capture AS capture ON capture.id = work.source_capture_id
                    AND capture.task_id = work.task_id AND capture.agent_run_id = $2::uuid
                WHERE work.task_id = $1::uuid AND work.work_type = 'exploration'
                ORDER BY work.updated_at DESC, work.work_id DESC LIMIT 1
                """,
                task_id,
                agent_run_id,
            )

    async def settle_interrupted_restorations(self, *, task_id: str, session_id: str) -> None:
        """回收本执行器新会话时终结被取消打断的恢复，不能遗留 running 屏障。"""
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            await connection.execute(
                """
                UPDATE qc_restoration_attempt AS attempt
                SET status = 'failed', error_code = 'H5_NEW_SESSION_INTERRUPTED',
                    error_message = 'H5 新会话补采退出，恢复未完成',
                    finished_at = GREATEST(clock_timestamp(), attempt.started_at),
                    updated_at = now(), row_version = attempt.row_version + 1
                FROM qc_capture AS source, qc_task AS task
                WHERE attempt.task_id = $1::uuid AND attempt.target_session_id = $2
                  AND attempt.status = 'running' AND attempt.strategy = 'new_session_replay'
                  AND attempt.source_grade = 'C'
                  AND source.id = attempt.original_capture_id AND source.task_id = attempt.task_id
                  AND task.id = attempt.task_id AND task.target_platform = 'h5'
                  AND attempt.target_session_id ~
                      ('^h5:' || source.agent_run_id::text || ':recapture:[0-9a-f]{20}$')
                  AND NOT EXISTS (SELECT 1 FROM qc_surface_session AS session
                      WHERE session.session_id = attempt.target_session_id
                        AND (session.task_id <> attempt.task_id
                             OR session.finalization_epoch <> attempt.finalization_epoch
                             OR session.session_owner <>
                                 'h5-snapshot-recapture:' || source.agent_run_id::text))
                """,
                task_id,
                session_id,
            )

    async def has_closed_snapshot_requests(self, *, task_id: str, agent_run_id: str) -> bool:
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            return bool(
                await connection.fetchval(
                    """
                SELECT EXISTS (SELECT 1 FROM qc_evidence_request AS request
                    INNER JOIN qc_task AS task ON task.id = request.task_id
                        AND task.finalization_epoch = request.finalization_epoch
                    INNER JOIN qc_capture AS capture ON capture.id = request.target_capture_id
                        AND capture.task_id = task.id AND capture.agent_run_id = $2::uuid
                    INNER JOIN qc_surface_session AS source
                        ON source.session_id = capture.surface_session_id
                        AND source.task_id = task.id
                        AND source.finalization_epoch = task.finalization_epoch
                        AND source.status IN ('closed', 'lost')
                        AND ((source.session_owner = 'h5-workflow'
                              AND source.session_id = 'h5:' || $2::text)
                             OR (source.session_owner = 'h5-browser-recovery:' || $2::text
                                 AND source.session_id ~
                                    ('^h5:' || $2::text || ':recovery:[0-9a-f]{20}$')))
                    WHERE task.id = $1::uuid AND __H5_TASK_PERMISSION__
                      AND request.session_dependency = 'snapshot'
                      AND request.status IN ('pending', 'routed', 'running')
                      AND request.expires_at > now())
                """.replace("__H5_TASK_PERMISSION__", h5_snapshot_task_permission_sql()),
                    task_id,
                    agent_run_id,
                )
            )
