"""H5 页面覆盖权威恢复的 PostgreSQL 实现。"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from services.api.app.application.h5_coverage_authority import (
    H5CoverageAuthorityCandidate,
)
from services.api.app.domain.exploration.h5_coverage import H5CoverageSnapshot
from services.api.app.domain.exploration.h5_frontier import (
    H5TaskFrontierSnapshot,
    h5_frontier_completion_blockers_are_explainable,
)
from services.api.app.domain.protocol import stable_protocol_hash
from services.api.app.infrastructure.db.pool import get_postgres_pool


class PostgresH5CoverageAuthorityRepository:
    """查找未晋升的 H5 Capture，并追加不可变覆盖快照。"""

    def __init__(self, *, database_url: str | None = None, pool: Any | None = None) -> None:
        self._database_url = database_url
        self._pool = pool

    async def list_candidates(
        self,
        *,
        task_id: str,
        agent_run_id: str,
        limit: int,
    ) -> list[H5CoverageAuthorityCandidate]:
        pool = self._pool or await get_postgres_pool(self._database_url)
        async with pool.acquire() as connection:
            rows = await connection.fetch(
                _LIST_CANDIDATES_SQL,
                task_id,
                agent_run_id,
                limit,
            )
        return [_candidate_from_row(row) for row in rows]

    async def promote(
        self,
        *,
        candidate: H5CoverageAuthorityCandidate,
        assessment: Mapping[str, object],
    ) -> bool:
        pool = self._pool or await get_postgres_pool(self._database_url)
        async with pool.acquire() as connection, connection.transaction():
            task = await connection.fetchrow(
                """
                SELECT finalization_epoch
                FROM qc_task
                WHERE id = $1::uuid
                  AND target_platform = 'h5'
                  AND status IN ('running', 'review')
                  AND finalization_state IN (
                      'open', 'exploration_draining', 'session_draining'
                  )
                  AND NOT EXISTS (
                      SELECT 1
                      FROM qc_v2_work_item AS active_work
                      WHERE active_work.task_id = qc_task.id
                        AND active_work.finalization_epoch = qc_task.finalization_epoch
                        AND active_work.work_type = 'exploration'
                        AND active_work.work_payload->>'surface_kind' = 'web'
                        AND active_work.status IN (
                            'pending', 'retry_wait', 'leased', 'running'
                        )
                  )
                FOR UPDATE
                """,
                candidate.task_id,
            )
            if task is None:
                return False
            epoch = int(task["finalization_epoch"])
            row = await connection.fetchrow(
                """
                SELECT
                    coverage_snapshot_id, revision, authority_source,
                    required_page_types, covered_page_types, missing_page_types,
                    coverage_scopes AS scopes, assessment_scope_ready,
                    frontier_terminal, completion_ready, schema_version, payload_hash
                FROM qc_h5_coverage_snapshot
                WHERE task_id = $1::uuid
                  AND agent_run_id = $2::uuid
                  AND finalization_epoch = $3
                ORDER BY revision DESC, created_at DESC
                LIMIT 1
                """,
                candidate.task_id,
                candidate.agent_run_id,
                epoch,
            )
            if row is None:
                return False
            previous = H5CoverageSnapshot.from_dict(_coverage_payload(row))
            frontier = await _latest_frontier(
                connection,
                task_id=candidate.task_id,
                agent_run_id=candidate.agent_run_id,
            )
            promoted = previous.promote_assessment(
                location=candidate.location,
                capture_id=candidate.page_capture_id,
                page_type=candidate.page_type,
                page_state=candidate.page_state,
                assessment=assessment,
                detection_confidence=candidate.detection_confidence,
                completion_allowed=_completion_allowed(frontier),
                include_page_types=(
                    frontier.sampling_policy_summary["include_page_types"]
                    if frontier is not None
                    else ()
                ),
            )
            if promoted is previous:
                return False
            snapshot_id = "h5-coverage:" + stable_protocol_hash(
                {
                    "task_id": candidate.task_id,
                    "agent_run_id": candidate.agent_run_id,
                    "finalization_epoch": epoch,
                    "revision": promoted.revision,
                }
            )
            inserted = await connection.fetchval(
                """
                INSERT INTO qc_h5_coverage_snapshot (
                    coverage_snapshot_id, task_id, agent_run_id, finalization_epoch,
                    revision, authority_source, required_page_types,
                    covered_page_types, missing_page_types, coverage_scopes,
                    assessment_scope_ready, frontier_terminal, completion_ready,
                    schema_version, payload_hash
                )
                VALUES (
                    $1, $2::uuid, $3::uuid, $4, $5, $6, $7::jsonb,
                    $8::jsonb, $9::jsonb, $10::jsonb, $11, $12, $13, $14, $15
                )
                ON CONFLICT (coverage_snapshot_id) DO NOTHING
                RETURNING coverage_snapshot_id
                """,
                snapshot_id,
                candidate.task_id,
                candidate.agent_run_id,
                epoch,
                promoted.revision,
                promoted.authority_source,
                _json(promoted.required_page_types),
                _json(promoted.covered_page_types),
                _json(promoted.missing_page_types),
                _json(promoted.scopes),
                promoted.assessment_scope_ready,
                promoted.frontier_terminal,
                promoted.completion_ready,
                promoted.schema_version,
                promoted.payload_hash,
            )
            return inserted is not None


async def _latest_frontier(
    connection: Any,
    *,
    task_id: str,
    agent_run_id: str,
) -> H5TaskFrontierSnapshot | None:
    payload = await connection.fetchval(
        """
        SELECT work_payload #> '{frontier,observation,metadata,h5_task_frontier}'
        FROM qc_v2_work_item
        WHERE task_id = $1::uuid
          AND work_type = 'exploration'
          AND work_payload #>> '{frontier,observation,metadata,agent_run_id}' = $2
        ORDER BY created_at DESC, work_id DESC
        LIMIT 1
        """,
        task_id,
        agent_run_id,
    )
    if payload is None:
        return None
    try:
        return H5TaskFrontierSnapshot.from_dict(_mapping(payload))
    except (TypeError, ValueError):
        return None


def _completion_allowed(frontier: H5TaskFrontierSnapshot | None) -> bool:
    return (
        frontier is not None
        and frontier.pending_count == 0
        and not any(entry.status.value == "selected" for entry in frontier.entries)
        and h5_frontier_completion_blockers_are_explainable(frontier)
    )


def _candidate_from_row(row: Mapping[str, Any]) -> H5CoverageAuthorityCandidate:
    detection = _mapping(row["detection_result"])
    assessment = row["assessment_payload"]
    return H5CoverageAuthorityCandidate(
        task_id=str(row["task_id"]),
        agent_run_id=str(row["agent_run_id"]),
        session_id=str(row["session_id"]),
        page_capture_id=str(row["page_capture_id"]),
        location=str(row["location"]),
        page_type=str(row["page_type"]),
        page_state=str(row["page_state"]),
        detection_confidence=float(row["detection_confidence"] or 0.0),
        detection_result=detection,
        persisted_adapter_result=(
            _mapping(assessment).get("adapter_result")
            if assessment is not None
            else None
        ),
    )


def _coverage_payload(row: Mapping[str, Any]) -> dict[str, Any]:
    payload = dict(row)
    for key in (
        "required_page_types",
        "covered_page_types",
        "missing_page_types",
        "scopes",
    ):
        value = payload.get(key)
        if isinstance(value, str):
            payload[key] = json.loads(value)
    return payload


def _mapping(value: object) -> dict[str, Any]:
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, Mapping):
        raise ValueError("H5 覆盖权威事实必须为对象")
    return dict(value)


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


_LIST_CANDIDATES_SQL = """
WITH latest_coverage AS (
    SELECT coverage.coverage_scopes
    FROM qc_h5_coverage_snapshot AS coverage
    INNER JOIN qc_task AS current_task
        ON current_task.id = coverage.task_id
       AND current_task.finalization_epoch = coverage.finalization_epoch
    WHERE coverage.task_id = $1::uuid
      AND coverage.agent_run_id = $2::uuid
    ORDER BY coverage.revision DESC, coverage.created_at DESC
    LIMIT 1
)
SELECT
    page_capture.task_id::text AS task_id,
    page_capture.agent_run_id::text AS agent_run_id,
    common_capture.surface_session_id AS session_id,
    page_capture.id::text AS page_capture_id,
    page_capture.url AS location,
    page_capture.page_type,
    COALESCE(page_capture.page_state, 'unknown') AS page_state,
    COALESCE(page_capture.detection_confidence, 0) AS detection_confidence,
    COALESCE(page_capture.metadata->'page_detection', '{}'::jsonb)
        || jsonb_build_object(
            'page_capture_id', page_capture.id::text,
            'page_url', page_capture.url,
            'page_type', page_capture.page_type,
            'page_state', COALESCE(page_capture.page_state, 'unknown'),
            'detection_status', page_capture.detection_status,
            'confidence', COALESCE(page_capture.detection_confidence, 0),
            'evidence_artifact_ids', COALESCE(artifacts.artifact_ids, '[]'::jsonb)
        ) AS detection_result,
    assessment.assessment_payload
FROM qc_page_capture AS page_capture
INNER JOIN qc_capture AS common_capture
    ON common_capture.id = page_capture.id
   AND common_capture.task_id = page_capture.task_id
   AND common_capture.agent_run_id = page_capture.agent_run_id
INNER JOIN qc_task AS task
    ON task.id = page_capture.task_id
LEFT JOIN LATERAL (
    SELECT jsonb_agg(artifact.id::text ORDER BY artifact.id::text) AS artifact_ids
    FROM qc_evidence_artifact AS artifact
    WHERE artifact.task_id = page_capture.task_id
      AND artifact.page_capture_id = page_capture.id
      AND artifact.lifecycle_status IN ('active', 'archived')
) AS artifacts ON TRUE
LEFT JOIN LATERAL (
    SELECT stored.assessment_payload
    FROM qc_h5_page_type_assessment AS stored
    WHERE stored.task_id = page_capture.task_id
      AND stored.agent_run_id = page_capture.agent_run_id
      AND stored.page_capture_id = page_capture.id
    ORDER BY stored.updated_at DESC, stored.assessment_record_id DESC
    LIMIT 1
) AS assessment ON TRUE
LEFT JOIN latest_coverage ON TRUE
WHERE page_capture.task_id = $1::uuid
  AND page_capture.agent_run_id = $2::uuid
  AND task.target_platform = 'h5'
  AND task.status IN ('running', 'review')
  AND task.finalization_state IN ('open', 'exploration_draining', 'session_draining')
  AND page_capture.capture_status = 'captured'
  AND page_capture.detection_status = 'recognized'
  AND page_capture.page_type IS NOT NULL
  AND page_capture.url IS NOT NULL
  AND common_capture.surface_kind = 'web'
  AND common_capture.capture_role IN ('after_action', 'probe_result_replay')
  AND COALESCE(artifacts.artifact_ids, '[]'::jsonb) <> '[]'::jsonb
  AND NOT EXISTS (
      SELECT 1
      FROM jsonb_array_elements(
          COALESCE(latest_coverage.coverage_scopes, '[]'::jsonb)
      ) AS scope
      WHERE scope->>'page_capture_id' = page_capture.id::text
  )
ORDER BY page_capture.captured_at, page_capture.id
LIMIT $3
"""


__all__ = ["PostgresH5CoverageAuthorityRepository"]
