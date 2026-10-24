"""H5 兼容补证只引用同一派发批内已验证的真实只读取证。"""

import json
from typing import Any

from services.api.app.application.h5_probe_work import H5SharedProbeEvidence
from services.api.app.application.h5_recapture_coalescing import (
    compatible_observation_key,
    compatible_probe_pair,
)
from services.api.app.domain.exploration import ProbeWork
from services.api.app.infrastructure.db.pool import get_postgres_pool


class PostgresH5SharedProbeEvidenceReader:
    def __init__(self, *, pool: Any | None = None):
        self._pool = pool

    async def find(self, *, work: ProbeWork, agent_run_id: str):
        row = await self.read_pair(work=work, agent_run_id=agent_run_id)
        return (
            H5SharedProbeEvidence(
                origin_work_id=row["origin_work_id"],
                result_capture_id=row["result_capture_id"],
            )
            if row
            else None
        )

    async def release_stale_unexecuted(
        self,
        *,
        work: ProbeWork,
        agent_run_id: str,
        lease_token: str,
        row_version: int,
    ) -> bool:
        """首条观察未形成可共享证据时，旧来源兄弟项回队独立恢复，不判无法补证。"""
        if compatible_observation_key(work.evidence_request) is None:
            return False
        pool = self._pool or await get_postgres_pool()
        async with pool.acquire() as connection, connection.transaction():
            await connection.execute(
                "SELECT pg_advisory_xact_lock(hashtext($1))", f"task-finalization:{work.task_id}"
            )
            return bool(
                await connection.fetchval(
                    """
                WITH stale AS (
                    UPDATE qc_v2_work_item AS work
                    SET status = 'superseded', last_error_code = 'H5_SHARED_PROBE_SOURCE_REPLACED',
                        last_error_message = '共享现场已推进，尚未执行的请求回队独立恢复',
                        lease_owner = NULL, lease_token = NULL, lease_expires_at = NULL,
                        row_version = work.row_version + 1, finished_at = now(), updated_at = now()
                    FROM qc_task AS task, qc_surface_session AS session, qc_capture AS source,
                        qc_evidence_request AS request
                    WHERE work.task_id = $1::uuid AND work.work_id = $2 AND work.status = 'running'
                        AND work.lease_token = $4::uuid AND work.row_version = $5
                        AND work.lease_expires_at > now() AND work.work_type = 'probe'
                        AND work.work_payload->>'surface_kind' = 'web'
                        AND task.id = work.task_id AND task.target_platform = 'h5'
                        AND task.status IN ('running', 'review')
                        AND task.finalization_epoch = work.finalization_epoch
                        AND task.finalization_state IN (
                            'open', 'exploration_draining', 'session_draining')
                        AND session.session_id = work.session_id AND session.task_id = task.id
                        AND session.status IN ('active', 'draining')
                        AND session.surface_kind = 'web'
                        AND session.finalization_epoch = work.finalization_epoch
                        AND source.id = work.source_capture_id AND source.task_id = task.id
                        AND source.agent_run_id = $3::uuid
                        AND source.surface_session_id = work.session_id
                        AND session.current_capture_id <> source.id
                        AND request.request_id = work.evidence_request_id
                        AND request.task_id = task.id
                        AND request.finalization_epoch = work.finalization_epoch
                        AND request.status IN ('routed', 'running') AND request.lease_token IS NULL
                        AND NOT EXISTS (SELECT 1 FROM qc_action_execution AS action
                            WHERE action.task_id = task.id
                                AND action.intent_payload
                                    #>> '{parameters,exploration_work_id}' = work.work_id)
                    RETURNING work.evidence_request_id
                ), reopened AS (
                    UPDATE qc_evidence_request AS request
                    SET status = CASE
                            WHEN request.expires_at <= now() THEN 'expired'
                            WHEN request.attempt_count >= request.max_attempts THEN 'unfulfillable'
                            ELSE 'pending' END,
                        resolution_reason = CASE
                            WHEN request.expires_at <= now() THEN 'H5_SHARED_PROBE_REQUEST_EXPIRED'
                            WHEN request.attempt_count >= request.max_attempts
                                THEN 'H5_SHARED_PROBE_ATTEMPTS_EXHAUSTED'
                            ELSE 'H5_SHARED_PROBE_SOURCE_REPLACED' END,
                        completed_at = CASE WHEN request.expires_at <= now()
                            OR request.attempt_count >= request.max_attempts THEN now()
                            ELSE NULL END,
                        row_version = request.row_version + 1,
                        next_attempt_at = now(), updated_at = now()
                    FROM stale WHERE request.request_id = stale.evidence_request_id RETURNING 1
                ) SELECT EXISTS (SELECT 1 FROM reopened)
                """,
                    work.task_id,
                    work.work_id,
                    agent_run_id,
                    lease_token,
                    row_version,
                )
            )

    async def read_pair(
        self,
        *,
        work: ProbeWork,
        agent_run_id: str,
        origin_work_id: str | None = None,
        result_capture_id: str | None = None,
    ):
        if compatible_observation_key(work.evidence_request) is None:
            return None
        pool = self._pool or await get_postgres_pool()
        async with pool.acquire() as connection:
            rows = await connection.fetch(
                """
                SELECT leader.work_id AS origin_work_id, leader.work_payload,
                    result.id::text AS result_capture_id,
                    before_frame.id::text AS before_capture_id,
                    result.agent_run_id::text AS agent_run_id,
                    result.surface_session_id AS session_id
                FROM qc_v2_work_item AS follower
                JOIN qc_task AS task ON task.id = follower.task_id
                    AND task.target_platform = 'h5' AND task.status IN ('running', 'review')
                    AND task.finalization_epoch = follower.finalization_epoch
                    AND task.finalization_state IN (
                        'open', 'exploration_draining', 'session_draining')
                JOIN qc_evidence_request AS request
                    ON request.request_id = follower.evidence_request_id
                    AND request.task_id = task.id
                    AND request.finalization_epoch = task.finalization_epoch
                    AND request.status IN ('running', 'satisfied')
                JOIN qc_restoration_attempt AS restoration
                    ON restoration.attempt_id = follower.work_payload->>'restoration_attempt_id'
                    AND restoration.request_id = request.request_id
                    AND restoration.task_id = task.id
                    AND restoration.finalization_epoch = task.finalization_epoch
                    AND restoration.status = 'equivalent'
                    AND restoration.assessment_payload->>'passed' = 'true'
                    AND restoration.restored_capture_id = follower.source_capture_id
                    AND restoration.assessment_payload->>'assessment_id'
                        = follower.work_payload->>'equivalence_assessment_id'
                JOIN qc_capture AS source ON source.id = follower.source_capture_id
                    AND source.task_id = task.id AND source.agent_run_id = $3::uuid
                    AND source.surface_session_id = follower.session_id
                    AND source.capture_status = 'captured'
                JOIN qc_v2_work_item AS leader ON leader.task_id = task.id
                    AND leader.session_id = follower.session_id AND leader.work_type = 'probe'
                    AND leader.status = 'succeeded' AND leader.work_id <> follower.work_id
                    AND leader.finalization_epoch = follower.finalization_epoch
                    AND leader.source_capture_id = follower.source_capture_id
                    AND leader.work_payload->>'surface_kind' = 'web'
                JOIN qc_action_execution AS action ON action.task_id = task.id
                    AND action.session_id = follower.session_id AND action.status = 'completed'
                    AND action.finalization_epoch = follower.finalization_epoch
                    AND action.intent_payload->>'action_type' = 'observe'
                    AND action.intent_payload
                        #>> '{parameters,exploration_work_id}' = leader.work_id
                    AND action.intent_payload->>'source_capture_id' = source.id::text
                    AND follower.created_at <= action.created_at
                JOIN qc_capture AS result ON result.id = action.result_capture_id
                    AND result.task_id = task.id AND result.agent_run_id = $3::uuid
                    AND result.surface_session_id = follower.session_id
                    AND result.action_execution_id = action.action_execution_id
                    AND result.session_command_sequence = action.command_sequence
                    AND result.capture_role = 'probe_result_replay'
                    AND result.capture_status = 'captured'
                JOIN qc_capture AS before_frame ON before_frame.id = result.parent_capture_id
                    AND before_frame.task_id = task.id AND before_frame.agent_run_id = $3::uuid
                    AND before_frame.surface_session_id = follower.session_id
                    AND before_frame.action_execution_id = action.action_execution_id
                    AND before_frame.capture_role = 'probe_before'
                    AND before_frame.capture_status = 'captured'
                JOIN qc_surface_session AS session ON session.session_id = follower.session_id
                    AND session.task_id = task.id AND session.surface_kind = 'web'
                    AND session.finalization_epoch = follower.finalization_epoch
                WHERE follower.task_id = $1::uuid AND follower.work_id = $2
                    AND follower.work_payload->>'surface_kind' = 'web'
                    AND follower.work_type = 'probe'
                    AND (($4::text IS NULL AND follower.status = 'running'
                        AND session.status IN ('active', 'draining')
                        AND session.current_capture_id = result.id
                        AND session.command_sequence = action.command_sequence)
                        OR ($4::text = leader.work_id AND follower.status = 'succeeded'
                            AND result.id = $5::uuid))
                    AND NOT EXISTS (
                        SELECT 1 FROM qc_capture AS frame
                        LEFT JOIN qc_page_capture AS page
                            ON page.id = frame.id AND page.task_id = task.id
                        LEFT JOIN qc_page_graph_state AS state ON state.state_id = frame.id
                            AND state.task_id = task.id AND state.agent_run_id = $3::uuid
                            AND state.finalization_epoch = follower.finalization_epoch
                        WHERE frame.id IN (source.id, before_frame.id, result.id)
                        AND (page.page_state IS DISTINCT FROM 'stable'
                            OR state.identity_status IS DISTINCT FROM 'confirmed'
                            OR state.identity_source IS DISTINCT FROM 'logical_surface'
                            OR state.is_overlay IS DISTINCT FROM false
                            OR state.is_external IS DISTINCT FROM false
                            OR state.logical_page_key IS DISTINCT FROM (
                                SELECT logical_page_key FROM qc_page_graph_state
                                WHERE state_id = source.id AND task_id = task.id))
                    )
                    AND EXISTS (SELECT 1 FROM qc_evidence_manifest
                        WHERE task_id = task.id AND capture_id = before_frame.id
                            AND quality_status = 'pass')
                    AND EXISTS (SELECT 1 FROM qc_evidence_manifest
                        WHERE task_id = task.id AND capture_id = result.id
                            AND quality_status = 'pass')
                ORDER BY leader.finished_at DESC LIMIT 32
                """,
                work.task_id,
                work.work_id,
                agent_run_id,
                origin_work_id,
                result_capture_id,
            )
        for row in rows:
            payload = row["work_payload"]
            leader = ProbeWork.from_dict(
                json.loads(payload) if isinstance(payload, str) else payload
            )
            if compatible_probe_pair(leader, work):
                return row
        return None
