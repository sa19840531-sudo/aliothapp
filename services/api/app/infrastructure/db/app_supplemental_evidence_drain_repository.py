"""App 补采排空的 PostgreSQL 权威状态读取。"""

from __future__ import annotations

from typing import Any

from services.api.app.application.app_supplemental_evidence_drain import (
    AppSupplementalEvidenceDrainState,
    AppSupplementalEvidenceExhaustionSettlement,
)
from services.api.app.infrastructure.db.pool import get_postgres_pool


class PostgresAppSupplementalEvidenceDrainStateReader:
    def __init__(self, *, database_url: str | None = None, pool: Any | None = None) -> None:
        self._database_url = database_url
        self._pool = pool

    async def load(
        self,
        *,
        task_id: str,
        agent_run_id: str,
        finalization_epoch: int,
    ) -> AppSupplementalEvidenceDrainState:
        _require_text(task_id, "task_id")
        _require_text(agent_run_id, "agent_run_id")
        if (
            isinstance(finalization_epoch, bool)
            or not isinstance(finalization_epoch, int)
            or finalization_epoch < 0
        ):
            raise ValueError("finalization_epoch 必须为非负整数")
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                WITH scoped_requests AS (
                    SELECT request.*
                    FROM qc_evidence_request AS request
                    INNER JOIN qc_capture AS capture
                        ON capture.id = request.target_capture_id
                       AND capture.task_id = request.task_id
                       AND capture.agent_run_id = $2::uuid
                    WHERE request.task_id = $1::uuid
                      AND request.finalization_epoch = $3
                      AND request.session_dependency = 'active_session'
                ), scoped_probes AS (
                    SELECT work.*
                    FROM qc_v2_work_item AS work
                    INNER JOIN scoped_requests AS request
                        ON request.request_id = work.evidence_request_id
                    WHERE work.work_type = 'probe'
                      AND work.work_payload->>'surface_kind' IN ('android', 'ios')
                      AND work.finalization_epoch = $3
                )
                SELECT
                    count(*) FILTER (
                        WHERE request.status IN ('pending', 'routed', 'running')
                    )::integer AS nonterminal_request_count,
                    (
                        SELECT count(DISTINCT probe.work_id)::integer
                        FROM scoped_probes AS probe
                        WHERE probe.status IN ('pending', 'leased', 'running', 'retry_wait')
                    ) AS nonterminal_probe_count,
                    (
                        SELECT count(*)::integer
                        FROM scoped_probes AS probe
                        INNER JOIN scoped_requests AS candidate
                            ON candidate.request_id = probe.evidence_request_id
                           AND candidate.status IN ('routed', 'running')
                        INNER JOIN qc_action_execution AS action
                            ON action.task_id = probe.task_id
                           AND action.session_id = probe.session_id
                           AND action.finalization_epoch = probe.finalization_epoch
                           AND action.status = 'completed'
                           AND action.result_capture_id IS NOT NULL
                           AND action.intent_payload
                                #>> '{parameters,exploration_work_id}' = probe.work_id
                        LEFT JOIN qc_evidence_supplement AS supplement
                            ON supplement.request_id = candidate.request_id
                           AND supplement.probe_work_id = probe.work_id
                        WHERE probe.status = 'succeeded'
                          AND supplement.supplement_id IS NULL
                    ) AS pending_completion_count,
                    LEAST(
                        min(request.next_attempt_at) FILTER (
                            WHERE request.status = 'pending'
                        ),
                        (
                            SELECT min(
                                CASE
                                    WHEN probe.status IN ('pending', 'retry_wait')
                                    THEN probe.next_attempt_at
                                    WHEN probe.status IN ('leased', 'running')
                                    THEN probe.lease_expires_at
                                END
                            )
                            FROM scoped_probes AS probe
                            WHERE probe.status IN (
                                'pending', 'retry_wait', 'leased', 'running'
                            )
                        )
                    ) AS next_attempt_at
                FROM scoped_requests AS request
                """,
                task_id,
                agent_run_id,
                finalization_epoch,
            )
        return AppSupplementalEvidenceDrainState(
            nonterminal_request_count=int(row["nonterminal_request_count"] or 0),
            nonterminal_probe_count=int(row["nonterminal_probe_count"] or 0),
            pending_completion_count=int(row["pending_completion_count"] or 0),
            next_attempt_at=row["next_attempt_at"],
        )

    async def _get_pool(self) -> Any:
        if self._pool is not None:
            return self._pool
        return await get_postgres_pool(self._database_url)


class PostgresAppSupplementalEvidenceDrainSettlementRepository:
    """在 App 补采耗尽时按 Task/Run/Epoch 保护性收口未执行事实。"""

    def __init__(self, *, database_url: str | None = None, pool: Any | None = None) -> None:
        self._database_url = database_url
        self._pool = pool

    async def settle_exhausted(
        self,
        *,
        task_id: str,
        agent_run_id: str,
        finalization_epoch: int,
        reason_code: str,
    ) -> AppSupplementalEvidenceExhaustionSettlement:
        _require_text(task_id, "task_id")
        _require_text(agent_run_id, "agent_run_id")
        _require_text(reason_code, "reason_code")
        if (
            isinstance(finalization_epoch, bool)
            or not isinstance(finalization_epoch, int)
            or finalization_epoch < 0
        ):
            raise ValueError("finalization_epoch 必须为非负整数")

        pool = await self._get_pool()
        async with pool.acquire() as connection, connection.transaction():
            # 与请求受理、Probe 发布共用 Task Finalization 锁，禁止结算后再发布新 Probe。
            await connection.execute(
                "SELECT pg_advisory_xact_lock(hashtext($1))",
                f"task-finalization:{task_id}",
            )
            candidate_query = """
                SELECT request.request_id
                FROM qc_evidence_request AS request
                INNER JOIN qc_capture AS target_capture
                    ON target_capture.id = request.target_capture_id
                   AND target_capture.task_id = request.task_id
                   AND target_capture.agent_run_id = $2::uuid
                INNER JOIN qc_task AS task
                    ON task.id = request.task_id
                   AND task.status = 'running'
                   AND task.finalization_state IN (
                       'open', 'exploration_draining', 'session_draining'
                   )
                   AND task.finalization_epoch = request.finalization_epoch
                WHERE request.task_id = $1::uuid
                  AND request.finalization_epoch = $3
                  AND (
                      $4::varchar[] IS NULL
                      OR request.request_id = ANY($4::varchar[])
                  )
                  AND request.session_dependency = 'active_session'
                  AND request.status IN ('pending', 'routed', 'running')
                  AND (
                      request.lease_owner IS NULL
                      OR request.lease_expires_at <= now()
                  )
                  AND NOT EXISTS (
                      SELECT 1
                      FROM qc_restoration_attempt AS restoration
                      WHERE restoration.request_id = request.request_id
                        AND restoration.task_id = request.task_id
                        AND restoration.finalization_epoch = $3
                        AND restoration.status = 'running'
                  )
                  AND NOT EXISTS (
                      SELECT 1
                      FROM qc_v2_work_item AS probe
                      INNER JOIN qc_capture AS source_capture
                          ON source_capture.id = probe.source_capture_id
                         AND source_capture.task_id = probe.task_id
                         AND source_capture.agent_run_id = $2::uuid
                      WHERE probe.task_id = request.task_id
                        AND probe.work_type = 'probe'
                        AND probe.evidence_request_id = request.request_id
                        AND probe.finalization_epoch = $3
                        AND (
                            probe.status IN ('leased', 'running', 'succeeded')
                            OR EXISTS (
                                SELECT 1
                                FROM qc_action_execution AS action
                                WHERE action.task_id = probe.task_id
                                  AND action.session_id = probe.session_id
                                  AND action.finalization_epoch = probe.finalization_epoch
                                  AND action.intent_payload
                                      #>> '{parameters,exploration_work_id}'
                                      = probe.work_id
                            )
                        )
                  )
                FOR UPDATE OF request
                """
            candidate_rows = await connection.fetch(
                candidate_query,
                task_id,
                agent_run_id,
                finalization_epoch,
                None,
            )
            request_ids = [str(row["request_id"]) for row in candidate_rows]
            if not request_ids:
                return AppSupplementalEvidenceExhaustionSettlement()

            # Probe claim 不共用本锁；先锁住仍可取消的 Work，等待已持有租约的
            # Claim 完成，再用新的语句快照重新判断状态和全部 Action ledger。
            await connection.fetch(
                """
                SELECT probe.work_id
                FROM qc_v2_work_item AS probe
                WHERE probe.task_id = $1::uuid
                  AND probe.work_type = 'probe'
                  AND probe.evidence_request_id = ANY($4::varchar[])
                  AND probe.finalization_epoch = $3
                  AND probe.status IN ('pending', 'retry_wait')
                  AND EXISTS (
                      SELECT 1
                      FROM qc_capture AS source_capture
                      WHERE source_capture.id = probe.source_capture_id
                        AND source_capture.task_id = probe.task_id
                        AND source_capture.agent_run_id = $2::uuid
                  )
                FOR UPDATE OF probe
                """,
                task_id,
                agent_run_id,
                finalization_epoch,
                request_ids,
            )
            candidate_rows = await connection.fetch(
                candidate_query,
                task_id,
                agent_run_id,
                finalization_epoch,
                request_ids,
            )
            request_ids = [str(row["request_id"]) for row in candidate_rows]
            if not request_ids:
                return AppSupplementalEvidenceExhaustionSettlement()

            cancelled_probe_count = await connection.fetchval(
                """
                WITH cancelled AS (
                    UPDATE qc_v2_work_item AS probe
                    SET
                        status = 'cancelled',
                        terminal_payload = COALESCE(probe.terminal_payload, '{}'::jsonb)
                            || jsonb_build_object('settlement_reason', $5::text),
                        lease_owner = NULL,
                        lease_token = NULL,
                        lease_expires_at = NULL,
                        last_error_code = $5::varchar,
                        last_error_message = 'App 补采耗尽，未执行 Probe 已取消',
                        finished_at = now(),
                        row_version = probe.row_version + 1,
                        updated_at = now()
                    FROM qc_evidence_request AS request
                    WHERE request.request_id = ANY($4::varchar[])
                      AND request.task_id = $1::uuid
                      AND request.finalization_epoch = $3
                      AND probe.task_id = request.task_id
                      AND probe.work_type = 'probe'
                      AND probe.evidence_request_id = request.request_id
                      AND probe.finalization_epoch = $3
                      AND probe.status IN ('pending', 'retry_wait')
                      AND EXISTS (
                          SELECT 1
                          FROM qc_capture AS target_capture
                          WHERE target_capture.id = request.target_capture_id
                            AND target_capture.task_id = request.task_id
                            AND target_capture.agent_run_id = $2::uuid
                      )
                      AND EXISTS (
                          SELECT 1
                          FROM qc_capture AS source_capture
                          WHERE source_capture.id = probe.source_capture_id
                            AND source_capture.task_id = probe.task_id
                            AND source_capture.agent_run_id = $2::uuid
                      )
                      AND NOT EXISTS (
                          SELECT 1
                          FROM qc_action_execution AS action
                          WHERE action.task_id = probe.task_id
                            AND action.session_id = probe.session_id
                            AND action.finalization_epoch = probe.finalization_epoch
                            AND action.intent_payload
                                #>> '{parameters,exploration_work_id}'
                                = probe.work_id
                      )
                    RETURNING probe.work_id
                )
                SELECT count(*) FROM cancelled
                """,
                task_id,
                agent_run_id,
                finalization_epoch,
                request_ids,
                reason_code,
            )

            settled_request_count = await connection.fetchval(
                """
                WITH settled AS (
                    UPDATE qc_evidence_request AS request
                    SET
                        status = CASE
                            WHEN request.expires_at <= now() THEN 'expired'
                            ELSE 'unfulfillable'
                        END,
                        resolution_reason = CASE
                            WHEN request.expires_at <= now()
                            THEN 'EVIDENCE_REQUEST_EXPIRED'
                            ELSE $5::text
                        END,
                        lease_owner = NULL,
                        lease_token = NULL,
                        lease_expires_at = NULL,
                        row_version = request.row_version + 1,
                        completed_at = now(),
                        updated_at = now()
                    FROM qc_task AS task
                    WHERE request.request_id = ANY($4::varchar[])
                      AND request.task_id = $1::uuid
                      AND request.finalization_epoch = $3
                      AND request.status IN ('pending', 'routed', 'running')
                      AND (
                          request.lease_owner IS NULL
                          OR request.lease_expires_at <= now()
                      )
                      AND task.id = request.task_id
                      AND task.status = 'running'
                      AND task.finalization_state IN (
                          'open', 'exploration_draining', 'session_draining'
                      )
                      AND task.finalization_epoch = request.finalization_epoch
                      AND EXISTS (
                          SELECT 1
                          FROM qc_capture AS target_capture
                          WHERE target_capture.id = request.target_capture_id
                            AND target_capture.task_id = request.task_id
                            AND target_capture.agent_run_id = $2::uuid
                      )
                      AND NOT EXISTS (
                          SELECT 1
                          FROM qc_restoration_attempt AS restoration
                          WHERE restoration.request_id = request.request_id
                            AND restoration.task_id = request.task_id
                            AND restoration.finalization_epoch = $3
                            AND restoration.status = 'running'
                      )
                      AND NOT EXISTS (
                          SELECT 1
                          FROM qc_v2_work_item AS probe
                          INNER JOIN qc_capture AS source_capture
                              ON source_capture.id = probe.source_capture_id
                             AND source_capture.task_id = probe.task_id
                             AND source_capture.agent_run_id = $2::uuid
                          WHERE probe.task_id = request.task_id
                            AND probe.work_type = 'probe'
                            AND probe.evidence_request_id = request.request_id
                            AND probe.finalization_epoch = $3
                            AND (
                                probe.status IN ('leased', 'running', 'succeeded')
                                OR EXISTS (
                                SELECT 1
                                FROM qc_action_execution AS action
                                WHERE action.task_id = probe.task_id
                                  AND action.session_id = probe.session_id
                                  AND action.finalization_epoch = probe.finalization_epoch
                                  AND action.intent_payload
                                      #>> '{parameters,exploration_work_id}'
                                      = probe.work_id
                                )
                            )
                      )
                    RETURNING request.request_id
                )
                SELECT count(*) FROM settled
                """,
                task_id,
                agent_run_id,
                finalization_epoch,
                request_ids,
                reason_code,
            )
        return AppSupplementalEvidenceExhaustionSettlement(
            settled_request_count=int(settled_request_count or 0),
            cancelled_probe_count=int(cancelled_probe_count or 0),
        )

    async def _get_pool(self) -> Any:
        if self._pool is not None:
            return self._pool
        return await get_postgres_pool(self._database_url)


def _require_text(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError(f"{field_name} 必须为无首尾空白的非空字符串")
