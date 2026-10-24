"""App 定向补采计划所需执行上下文的 PostgreSQL 读取器。"""

from __future__ import annotations

from typing import Any

from services.api.app.application.app_targeted_recapture_planning import (
    AppTargetedRecaptureContext,
)
from services.api.app.domain.inspection import EvidenceRequest
from services.api.app.domain.interaction import SurfaceKind
from services.api.app.infrastructure.db.pool import get_postgres_pool


class PostgresAppTargetedRecaptureContextReader:
    """只接受仍在同一 Task/Run/Epoch 活会话中的 pending 请求。"""

    def __init__(self, *, database_url: str | None = None, pool: Any | None = None) -> None:
        self._database_url = database_url
        self._pool = pool

    async def read(
        self,
        *,
        request: EvidenceRequest,
    ) -> AppTargetedRecaptureContext | None:
        if not isinstance(request, EvidenceRequest):
            raise TypeError("request 必须为 EvidenceRequest")
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                SELECT
                    capture.task_id::text AS task_id,
                    capture.agent_run_id::text AS agent_run_id,
                    session.finalization_epoch,
                    session.session_id AS source_session_id,
                    session.surface_kind,
                    session.current_capture_id::text AS current_capture_id,
                    target_state.state_id::text AS target_state_id,
                    target_state.logical_page_key AS target_identity
                FROM qc_evidence_request AS evidence_request
                INNER JOIN qc_capture AS capture
                    ON capture.id = evidence_request.target_capture_id
                   AND capture.task_id = evidence_request.task_id
                   AND capture.capture_status = 'captured'
                INNER JOIN qc_surface_session AS session
                    ON session.task_id = capture.task_id
                   AND session.session_id = capture.surface_session_id
                   AND session.surface_kind = capture.surface_kind
                   AND session.status IN ('active', 'draining')
                   AND session.current_capture_id IS NOT NULL
                   AND session.lease_owner IS NOT NULL
                   AND session.lease_token IS NOT NULL
                   AND session.lease_expires_at > now()
                INNER JOIN qc_task AS task
                    ON task.id = capture.task_id
                   AND task.status = 'running'
                   AND task.finalization_state IN (
                       'open', 'exploration_draining', 'session_draining'
                   )
                   AND task.finalization_epoch = session.finalization_epoch
                LEFT JOIN qc_page_graph_state AS target_state
                    ON target_state.state_id = capture.id
                   AND target_state.task_id = capture.task_id
                   AND target_state.agent_run_id = capture.agent_run_id
                   AND target_state.surface_session_id = capture.surface_session_id
                   AND target_state.finalization_epoch = session.finalization_epoch
                WHERE evidence_request.request_id = $1
                  AND evidence_request.task_id = $2::uuid
                  AND evidence_request.status = 'pending'
                  AND evidence_request.expires_at > now()
                  AND evidence_request.finalization_epoch = session.finalization_epoch
                """,
                request.request_id,
                request.task_id,
            )
        if row is None:
            return None
        try:
            surface_kind = SurfaceKind(str(row["surface_kind"]))
        except ValueError:
            return None
        if surface_kind is SurfaceKind.WEB or row["agent_run_id"] is None:
            return None
        return AppTargetedRecaptureContext(
            task_id=str(row["task_id"]),
            agent_run_id=str(row["agent_run_id"]),
            finalization_epoch=int(row["finalization_epoch"]),
            source_session_id=str(row["source_session_id"]),
            surface_kind=surface_kind,
            current_capture_id=str(row["current_capture_id"]),
            target_state_id=(
                str(row["target_state_id"])
                if row["target_state_id"] is not None
                else None
            ),
            target_identity=row["target_identity"],
        )

    async def _get_pool(self) -> Any:
        if self._pool is None:
            self._pool = await get_postgres_pool(self._database_url)
        return self._pool
