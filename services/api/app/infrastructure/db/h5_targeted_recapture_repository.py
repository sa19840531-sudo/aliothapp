"""H5 定向补采的 PostgreSQL 执行域与状态快照读取。"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from services.api.app.application.h5_targeted_recapture import (
    H5SnapshotCheckpointRequest,
    H5TargetedRecaptureContext,
)
from services.api.app.domain.inspection import EvidenceRequest, StateInvariantSnapshot
from services.api.app.domain.interaction import CaptureRef, SurfaceKind
from services.api.app.infrastructure.db.pool import get_postgres_pool


class H5ClaimedEvidenceRequestReference:
    """H5 已持租约请求无需再次执行首次受理，路由事务仍校验完整租约围栏。"""

    async def upsert_in_transaction(
        self,
        connection: Any,
        *,
        request: EvidenceRequest,
    ) -> None:
        del connection
        if not isinstance(request, EvidenceRequest):
            raise TypeError("request 必须为 EvidenceRequest")


class PostgresH5TargetedRecaptureRepository:
    """只读取 `target_platform=h5` 的当前活会话，拒绝桌面 Web 串入。"""

    def __init__(self, *, database_url: str | None = None, pool: Any | None = None) -> None:
        self._database_url = database_url
        self._pool = pool

    async def list_compatible_observation_requests(
        self, *, request: EvidenceRequest, agent_run_id: str, finalization_epoch: int,
    ) -> tuple[EvidenceRequest, ...]:
        from services.api.app.application.h5_recapture_coalescing import compatible_observation_key

        key = compatible_observation_key(request)
        if key is None:
            return ()
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            rows = await connection.fetch(
                """
                SELECT candidate.request_payload
                FROM qc_evidence_request AS candidate
                JOIN qc_task AS task ON task.id = candidate.task_id
                    AND task.target_platform = 'h5' AND task.status = 'running'
                    AND task.finalization_epoch = candidate.finalization_epoch
                JOIN qc_capture AS capture ON capture.id = candidate.target_capture_id
                    AND capture.task_id = task.id AND capture.agent_run_id = $2::uuid
                    AND capture.surface_kind = 'web' AND capture.capture_status = 'captured'
                WHERE candidate.task_id = $1::uuid AND candidate.finalization_epoch = $3
                  AND candidate.target_capture_id = $4::uuid AND candidate.request_id <> $5
                  AND candidate.status = 'pending' AND candidate.lease_token IS NULL
                  AND candidate.expires_at > now() AND candidate.next_attempt_at <= now()
                  AND candidate.attempt_count < candidate.max_attempts
                ORDER BY candidate.created_at, candidate.request_id LIMIT 32
                """, request.task_id, agent_run_id, finalization_epoch,
                request.target_capture_id, request.request_id,
            )
        candidates = tuple(
            EvidenceRequest.from_dict(_mapping(row["request_payload"])) for row in rows
        )
        return tuple(item for item in candidates if compatible_observation_key(item) == key)

    async def list_snapshot_checkpoint_requests(
        self, *, task_id: str, agent_run_id: str,
    ) -> tuple[H5SnapshotCheckpointRequest, ...]:
        """完整读取检查点前沿，不复用排除已领取请求的消费清单。"""
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            rows = await connection.fetch(
                """
                SELECT request.request_payload, request.finalization_epoch,
                    (request.lease_token IS NOT NULL
                     OR request.status IN ('routed', 'running')
                     OR EXISTS (
                         SELECT 1 FROM qc_v2_work_item AS probe
                         WHERE probe.task_id = task.id
                           AND probe.finalization_epoch = task.finalization_epoch
                           AND probe.evidence_request_id = request.request_id
                           AND probe.work_type = 'probe'
                           AND (probe.status IN ('pending', 'retry_wait', 'leased', 'running')
                                OR (probe.status = 'succeeded' AND NOT EXISTS (
                                    SELECT 1 FROM qc_evidence_supplement AS supplement
                                    WHERE supplement.probe_work_id = probe.work_id
                                )))
                     )) AS blocks_current_surface
                FROM qc_evidence_request AS request
                JOIN qc_task AS task ON task.id = request.task_id
                JOIN qc_capture AS capture ON capture.id = request.target_capture_id
                    AND capture.task_id = task.id AND capture.agent_run_id = $2::uuid
                WHERE task.id = $1::uuid AND lower(task.target_platform) = 'h5'
                  AND task.status = 'running' AND task.finalization_state = 'open'
                  AND request.finalization_epoch = task.finalization_epoch
                  AND request.session_dependency = 'snapshot'
                  AND request.status IN ('pending', 'routed', 'running')
                ORDER BY request.request_id LIMIT 51
                """, task_id, agent_run_id,
            )
        return tuple(H5SnapshotCheckpointRequest(
            request=EvidenceRequest.from_dict(_mapping(row["request_payload"])),
            finalization_epoch=int(row["finalization_epoch"]),
            blocks_current_surface=bool(row["blocks_current_surface"]),
        ) for row in rows)

    async def list_deferred_snapshot_requests(
        self, *, task_id: str, agent_run_id: str, include_ready: bool = False,
    ) -> tuple[EvidenceRequest, ...]:
        """只读当前执行域中尚未到重试时间、未被占用的快照请求。"""
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            rows = await connection.fetch(
                """
                SELECT request.request_payload
                FROM qc_evidence_request AS request
                INNER JOIN qc_task AS task ON task.id = request.task_id
                INNER JOIN qc_capture AS capture
                    ON capture.id = request.target_capture_id
                   AND capture.task_id = task.id
                   AND capture.agent_run_id = $2::uuid
                WHERE task.id = $1::uuid
                  AND lower(task.target_platform) = 'h5'
                  AND task.status = 'running'
                  AND task.finalization_state = 'open'
                  AND request.finalization_epoch = task.finalization_epoch
                  AND request.session_dependency = 'snapshot'
                  AND request.status = 'pending'
                  AND request.lease_token IS NULL
                  AND ($3::boolean OR request.next_attempt_at > now())
                  AND request.expires_at > now()
                ORDER BY request.request_id
                LIMIT 51
                """, task_id, agent_run_id, include_ready,
            )
        return tuple(EvidenceRequest.from_dict(
            json.loads(row["request_payload"])
            if isinstance(row["request_payload"], str) else row["request_payload"]
        ) for row in rows)

    async def read(
        self,
        *,
        request: EvidenceRequest,
    ) -> H5TargetedRecaptureContext | None:
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                SELECT
                    task.id::text AS task_id,
                    capture.agent_run_id::text AS agent_run_id,
                    task.finalization_epoch,
                    session.session_id,
                    session.current_capture_id::text AS current_capture_id,
                    target_state.state_id::text AS target_state_id,
                    CASE
                        WHEN target_state.identity_status = 'confirmed'
                         AND target_state.identity_source = 'logical_surface'
                        THEN target_state.logical_page_key
                        ELSE NULL
                    END AS target_identity,
                    CASE
                        WHEN current_state.identity_status = 'confirmed'
                         AND current_state.identity_source = 'logical_surface'
                        THEN current_state.logical_page_key
                        ELSE NULL
                    END AS current_identity,
                    target_page.url AS target_url,
                    current_page.url AS current_url,
                    target_state.state_payload->'logical_surface'->>'document_identity'
                        AS target_document_identity,
                    current_state.state_payload->'logical_surface'->>'document_identity'
                        AS current_document_identity,
                    target_state.state_payload->'logical_surface'->>'state_fingerprint'
                        AS target_state_fingerprint,
                    current_state.state_payload->'logical_surface'->>'state_fingerprint'
                        AS current_state_fingerprint
                FROM qc_evidence_request AS evidence_request
                INNER JOIN qc_task AS task
                    ON task.id = evidence_request.task_id
                   AND lower(task.target_platform) = 'h5'
                   AND task.status = 'running'
                   AND task.finalization_state IN (
                       'open', 'exploration_draining', 'session_draining'
                   )
                   AND task.finalization_epoch = evidence_request.finalization_epoch
                INNER JOIN qc_capture AS capture
                    ON capture.id = evidence_request.target_capture_id
                   AND capture.task_id = task.id
                   AND capture.surface_kind = 'web'
                   AND capture.capture_status = 'captured'
                INNER JOIN qc_surface_session AS session
                    ON session.task_id = task.id
                   AND session.session_id = capture.surface_session_id
                   AND session.surface_kind = 'web'
                   AND (
                       (
                           session.session_owner = 'h5-workflow'
                           AND session.session_id = 'h5:' || capture.agent_run_id::text
                       )
                       OR (
                           session.session_owner =
                               'h5-browser-recovery:' || capture.agent_run_id::text
                           AND session.session_id ~ (
                               '^h5:' || capture.agent_run_id::text || ':recovery:[0-9a-f]{20}$'
                           )
                       )
                   )
                   AND session.finalization_epoch = task.finalization_epoch
                   AND session.status IN ('active', 'draining')
                   AND session.current_capture_id IS NOT NULL
                LEFT JOIN qc_page_graph_state AS target_state
                    ON target_state.state_id = capture.id
                   AND target_state.task_id = task.id
                   AND target_state.agent_run_id = capture.agent_run_id
                   AND target_state.surface_session_id = session.session_id
                   AND target_state.finalization_epoch = task.finalization_epoch
                LEFT JOIN qc_page_graph_state AS current_state
                    ON current_state.state_id = session.current_capture_id
                   AND current_state.task_id = task.id
                   AND current_state.agent_run_id = capture.agent_run_id
                   AND current_state.surface_session_id = session.session_id
                   AND current_state.finalization_epoch = task.finalization_epoch
                LEFT JOIN qc_page_capture AS target_page
                    ON target_page.id = capture.id
                   AND target_page.task_id = task.id
                LEFT JOIN qc_page_capture AS current_page
                    ON current_page.id = session.current_capture_id
                   AND current_page.task_id = task.id
                WHERE evidence_request.request_id = $1
                  AND evidence_request.task_id = $2::uuid
                  AND evidence_request.status = 'pending'
                  AND evidence_request.session_dependency IN (
                      'active_session', 'snapshot'
                  )
                  AND evidence_request.expires_at > now()
                """,
                request.request_id,
                request.task_id,
            )
        if row is None:
            return None
        return H5TargetedRecaptureContext(
            task_id=str(row["task_id"]),
            agent_run_id=str(row["agent_run_id"]),
            finalization_epoch=int(row["finalization_epoch"]),
            session_id=str(row["session_id"]),
            current_capture_id=str(row["current_capture_id"]),
            target_state_id=(
                str(row["target_state_id"])
                if row["target_state_id"] is not None
                else None
            ),
            target_identity=row["target_identity"],
            current_identity=row["current_identity"],
            target_url=row["target_url"],
            current_url=row["current_url"],
            target_document_identity=row.get("target_document_identity"),
            current_document_identity=row.get("current_document_identity"),
            target_state_fingerprint=row.get("target_state_fingerprint"),
            current_state_fingerprint=row.get("current_state_fingerprint"),
        )

    async def read_h5_epoch(
        self,
        *,
        task_id: str,
        agent_run_id: str,
    ) -> int | None:
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            value = await connection.fetchval(
                """
                SELECT task.finalization_epoch
                FROM qc_task AS task
                WHERE task.id = $1::uuid
                  AND lower(task.target_platform) = 'h5'
                  AND task.status = 'running'
                  AND task.finalization_state IN (
                      'open', 'exploration_draining', 'session_draining'
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM qc_v2_work_item AS probe
                      INNER JOIN qc_capture AS source_capture
                          ON source_capture.id = probe.source_capture_id
                         AND source_capture.task_id = task.id
                         AND source_capture.agent_run_id = $2::uuid
                      WHERE probe.task_id = task.id
                        AND probe.finalization_epoch = task.finalization_epoch
                        AND probe.work_type = 'probe'
                        AND probe.work_payload->>'surface_kind' = 'web'
                        AND (
                            probe.status IN ('pending', 'retry_wait', 'leased', 'running')
                            OR (
                                probe.status = 'succeeded'
                                AND NOT EXISTS (
                                    SELECT 1 FROM qc_evidence_supplement AS supplement
                                    WHERE supplement.probe_work_id = probe.work_id
                                )
                                AND NOT EXISTS (
                                    SELECT 1 FROM qc_evidence_request AS request
                                    WHERE request.request_id = probe.evidence_request_id
                                      AND request.task_id = task.id
                                      AND request.finalization_epoch = task.finalization_epoch
                                      AND request.status IN (
                                          'unfulfillable', 'expired', 'cancelled'
                                      )
                                )
                            )
                        )
                  )
                  AND EXISTS (
                      SELECT 1
                      FROM qc_surface_session AS session
                      WHERE session.task_id = task.id
                        AND session.surface_kind = 'web'
                        AND (
                            (
                                session.session_owner = 'h5-workflow'
                                AND session.session_id = 'h5:' || $2::text
                            )
                            OR (
                                session.session_owner = 'h5-browser-recovery:' || $2::text
                                AND session.session_id ~ (
                                    '^h5:' || $2::text || ':recovery:[0-9a-f]{20}$'
                                )
                                AND EXISTS (
                                    SELECT 1 FROM qc_capture AS capture
                                    WHERE capture.id = session.current_capture_id
                                      AND capture.task_id = task.id
                                      AND capture.agent_run_id = $2::uuid
                                      AND capture.surface_session_id = session.session_id
                                      AND capture.surface_kind = 'web'
                                      AND capture.capture_status = 'captured'
                                )
                            )
                        )
                        AND session.finalization_epoch = task.finalization_epoch
                        AND session.status IN ('active', 'draining')
                  )
                """,
                task_id,
                agent_run_id,
            )
        return int(value) if value is not None else None

    async def _get_pool(self) -> Any:
        if self._pool is None:
            self._pool = await get_postgres_pool(self._database_url)
        return self._pool


class PostgresH5StateInvariantReader:
    """从 H5 Capture 与页面图事实构造公共状态等价输入。"""

    def __init__(self, *, database_url: str | None = None, pool: Any | None = None) -> None:
        self._database_url = database_url
        self._pool = pool

    async def read(self, *, capture: CaptureRef) -> StateInvariantSnapshot:
        if capture.surface_kind is not SurfaceKind.WEB:
            raise ValueError("H5 状态快照只接受 Web Surface Capture")
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                SELECT
                    capture.task_id::text AS task_id,
                    capture.agent_run_id::text AS agent_run_id,
                    capture.surface_session_id AS session_id,
                    capture.surface_kind,
                    capture.metadata,
                    capture.captured_at,
                    page.url,
                    page.page_type,
                    page.page_state,
                    state.logical_page_key,
                    state.state_fingerprint,
                    state.is_overlay,
                    state.is_external,
                    state.state_payload
                FROM qc_capture AS capture
                INNER JOIN qc_task AS task
                    ON task.id = capture.task_id
                   AND lower(task.target_platform) = 'h5'
                INNER JOIN qc_page_capture AS page ON page.id = capture.id
                INNER JOIN qc_page_graph_state AS state
                    ON state.state_id = capture.id
                   AND state.task_id = capture.task_id
                   AND state.agent_run_id = capture.agent_run_id
                WHERE capture.id = $1::uuid
                  AND capture.task_id = $2::uuid
                  AND capture.agent_run_id IS NOT NULL
                  AND capture.surface_session_id = $3
                  AND capture.surface_kind = 'web'
                  AND capture.capture_status = 'captured'
                """,
                capture.capture_id,
                capture.task_id,
                capture.session_id,
            )
        if row is None:
            raise ValueError("H5 Capture 缺少同 Run 页面图状态，不能证明状态等价")
        metadata = _mapping(row["metadata"])
        state_payload = _mapping(row["state_payload"])
        surface = _mapping(state_payload.get("logical_surface"))
        transition = _mapping(surface.get("transition"))
        signals = _mapping(surface.get("state_signals"))
        semantic = {
            "page_state": str(row["page_state"] or "stable"),
        }
        region = metadata.get("region_label") or state_payload.get("region_label")
        if isinstance(region, str) and region.strip():
            semantic["region"] = region.strip()
        return StateInvariantSnapshot(
            task_id=str(row["task_id"]),
            capture_id=capture.capture_id,
            session_id=str(row["session_id"]),
            surface_kind=SurfaceKind.WEB,
            environment={"surface_kind": SurfaceKind.WEB.value},
            business={
                key: value
                for key, value in {
                    "page_type": row["page_type"],
                    "url_pattern": row["url"],
                    "logical_page_key": row["logical_page_key"],
                }.items()
                if value is not None and str(value).strip()
            },
            semantic=semantic,
            structure={
                "layer_target": "overlay" if bool(row["is_overlay"]) else "base_page",
                "state_fingerprint": str(row["state_fingerprint"]),
                "is_external": bool(row["is_external"]),
                "identity_status": state_payload.get("identity_status"),
                "document_identity": surface.get("document_identity"),
                "logical_state_fingerprint": surface.get("state_fingerprint"),
                "logical_transition_proof": transition.get("logical_transition_proof"),
                "transition_reason": transition.get("reason_code"),
                "transition_before_identity": transition.get("before_identity_fingerprint"),
                "transition_after_identity": transition.get("after_identity_fingerprint"),
                "transition_before_document": transition.get("before_document_identity"),
                "transition_after_document": transition.get("after_document_identity"),
                "obstruction_count": signals.get("obstruction_count"),
                "loading": signals.get("loading"),
                "evidence_quality": surface.get("evidence_quality"),
            },
            captured_at=row["captured_at"],
        )

    async def _get_pool(self) -> Any:
        if self._pool is None:
            self._pool = await get_postgres_pool(self._database_url)
        return self._pool


def _mapping(value: object) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return dict(decoded) if isinstance(decoded, Mapping) else {}
    return {}
