"""H5 证据交接库存的 PostgreSQL 读取实现。"""

from __future__ import annotations

import json
from typing import Any

from services.api.app.application.h5_capture_readiness import h5_capture_is_inspection_ready
from services.api.app.application.h5_evidence_handoff import H5EvidenceCapture
from services.api.app.application.h5_evidence_inventory import H5EvidenceArtifactRef
from services.api.app.infrastructure.db.h5_completion_gate_sql import (
    H5_ALREADY_INSPECTED_LOGICAL_STATE_SQL,
)
from services.api.app.infrastructure.db.pool import get_postgres_pool


class PostgresH5EvidenceCaptureReader:
    """按任务和 Run 双重归属读取可判定 H5 页面证据。"""

    def __init__(self, *, database_url: str | None = None, pool: Any | None = None) -> None:
        self._database_url = database_url
        self._pool = pool

    async def list_judgeable_captures(
        self,
        *,
        task_id: str,
        agent_run_id: str,
    ) -> list[H5EvidenceCapture]:
        pool = self._pool or await get_postgres_pool(self._database_url)
        async with pool.acquire() as connection:
            rows = await connection.fetch(
                f"""
                SELECT
                    COALESCE(
                        common_capture.id::text,
                        action.action_execution_id
                    ) AS capture_id,
                    action.session_id,
                    surface_session.session_owner,
                    surface_session.status AS session_status,
                    task.finalization_state AS task_finalization_state,
                    surface_session.finalization_epoch AS session_finalization_epoch,
                    task.finalization_epoch AS task_finalization_epoch,
                    action.intent_payload->'parameters'->>'supplemental_evidence_source'
                        AS supplemental_evidence_source,
                    page_capture.page_type,
                    page_capture.page_state,
                    common_capture.metadata AS capture_metadata,
                    (SELECT coalesce(jsonb_agg(primary_frame.checksum),'[]'::jsonb)
                     FROM qc_evidence_artifact AS primary_frame
                     WHERE primary_frame.task_id=task.id
                       AND primary_frame.page_capture_id=common_capture.id
                       AND primary_frame.artifact_type='screenshot'
                       AND primary_frame.metadata->>'purpose'='h5_video_primary'
                       AND primary_frame.lifecycle_status IN ('active','archived'))
                    AS video_primary_checksums,
                    COALESCE(
                        page_capture.detection_status,
                        page_capture.metadata->'page_detection'->>'detection_status',
                        common_capture.metadata->'page_detection'->>'detection_status'
                    ) AS detection_status,
                    COALESCE(page_capture.capture_role, '') AS capture_role,
                    COALESCE(page_capture.url, '') AS url,
                    page_capture.inspection_status,
                    action.action_execution_id,
                    assessment.assessment_record_id AS page_type_assessment_id,
                    assessment.payload_hash AS assessment_hash,
                    assessment.logical_identity_fingerprint,
                    assessment.logical_state_fingerprint,
                    assessment.rule_matching_allowed,
                    CASE
                        WHEN {H5_ALREADY_INSPECTED_LOGICAL_STATE_SQL}
                        THEN TRUE
                        ELSE FALSE
                    END AS logical_state_previously_published,
                    before_manifest.manifest_id AS before_manifest_id,
                    before_manifest.manifest_hash AS before_manifest_hash,
                    before_manifest.quality_status AS before_manifest_quality_status,
                    after_manifest.manifest_id AS after_manifest_id,
                    after_manifest.manifest_hash AS after_manifest_hash,
                    after_manifest.quality_status AS after_manifest_quality_status,
                    before_capture.id::text AS parent_capture_id,
                    before_page_capture.url AS before_url,
                    array_remove(
                        array_agg(
                            DISTINCT after_evidence.artifact_type
                            ORDER BY after_evidence.artifact_type
                        ),
                        NULL
                    ) AS artifact_types,
                    array_remove(
                        array_agg(
                            DISTINCT before_evidence.artifact_type
                            ORDER BY before_evidence.artifact_type
                        ),
                        NULL
                    ) AS before_artifact_types
                    , jsonb_agg(
                        DISTINCT jsonb_build_object(
                            'artifact_id', after_evidence.id::text,
                            'task_id', after_evidence.task_id::text,
                            'agent_run_id', common_capture.agent_run_id::text,
                            'capture_id', after_evidence.page_capture_id::text,
                            'storage_path', after_evidence.storage_path,
                            'artifact_type', after_evidence.artifact_type,
                            'checksum', after_evidence.checksum,
                            'size_bytes', after_evidence.size_bytes,
                            'lifecycle_status', after_evidence.lifecycle_status
                        )
                    ) FILTER (WHERE after_evidence.id IS NOT NULL) AS artifact_refs
                    , jsonb_agg(
                        DISTINCT jsonb_build_object(
                            'artifact_id', before_evidence.id::text,
                            'task_id', before_evidence.task_id::text,
                            'agent_run_id', before_capture.agent_run_id::text,
                            'capture_id', before_evidence.page_capture_id::text,
                            'storage_path', before_evidence.storage_path,
                            'artifact_type', before_evidence.artifact_type,
                            'checksum', before_evidence.checksum,
                            'size_bytes', before_evidence.size_bytes,
                            'lifecycle_status', before_evidence.lifecycle_status
                        )
                    ) FILTER (WHERE before_evidence.id IS NOT NULL) AS before_artifact_refs
                FROM qc_action_execution AS action
                INNER JOIN qc_task AS task
                    ON task.id = action.task_id
                INNER JOIN qc_surface_session AS surface_session
                    ON surface_session.task_id = action.task_id
                   AND surface_session.session_id = action.session_id
                   AND surface_session.surface_kind = 'web'
                   AND surface_session.finalization_epoch = task.finalization_epoch
                   AND action.finalization_epoch = surface_session.finalization_epoch
                LEFT JOIN qc_capture AS common_capture
                    ON common_capture.id = action.result_capture_id
                   AND common_capture.task_id = action.task_id
                   AND common_capture.agent_run_id = $2::uuid
                   AND common_capture.surface_kind = 'web'
                   AND common_capture.surface_session_id = action.session_id
                   AND common_capture.capture_role = 'after_action'
                   AND common_capture.action_execution_id = action.action_execution_id
                   AND NOT EXISTS (
                       SELECT 1
                       FROM qc_page_capture AS common_capture_lifecycle
                       WHERE common_capture_lifecycle.id = common_capture.id
                         AND COALESCE(
                             common_capture_lifecycle.inspection_lifecycle_status,
                             common_capture.metadata->>'inspection_lifecycle_status',
                             ''
                         ) IN ('superseded', 'abandoned')
                   )
                   AND NOT EXISTS (
                       SELECT 1
                       FROM qc_surface_phase_run AS auth_run
                       LEFT JOIN qc_surface_phase_step AS auth_step
                         ON auth_step.phase_run_id = auth_run.phase_run_id
                        AND auth_step.task_id = auth_run.task_id
                        AND auth_step.session_id = auth_run.session_id
                       WHERE auth_run.task_id = common_capture.task_id
                         AND auth_run.session_id = common_capture.surface_session_id
                         AND auth_run.phase_key = 'h5_auth_bootstrap'
                         AND (
                               auth_run.source_capture_id = common_capture.id
                               OR (
                                   auth_step.result_capture_id = common_capture.id
                                   AND auth_step.step_key <> 'return_to_task_root'
                               )
                             )
                   )
                LEFT JOIN qc_page_capture AS page_capture
                    ON page_capture.id = common_capture.id
                LEFT JOIN LATERAL (
                    SELECT
                        stored.assessment_record_id,
                        stored.payload_hash,
                        stored.assessment_payload
                            ->'detection_result'
                            ->'logical_surface'
                            ->>'identity_fingerprint' AS logical_identity_fingerprint,
                        stored.assessment_payload
                            ->'detection_result'
                            ->'logical_surface'
                            ->>'state_fingerprint' AS logical_state_fingerprint,
                        COALESCE(
                            (
                                stored.assessment_payload
                                    ->'detection_result'
                                    ->>'rule_matching_allowed'
                            )::boolean,
                            FALSE
                        ) AS rule_matching_allowed
                    FROM qc_h5_page_type_assessment AS stored
                    WHERE stored.task_id = action.task_id
                      AND stored.agent_run_id = $2::uuid
                      AND stored.page_capture_id = common_capture.id
                      AND stored.detection_status = 'recognized'
                      AND stored.page_type = page_capture.page_type
                    ORDER BY stored.updated_at DESC, stored.assessment_record_id ASC
                    LIMIT 1
                ) AS assessment ON TRUE
                LEFT JOIN qc_capture AS before_capture
                    ON before_capture.id = action.source_capture_id
                   AND before_capture.task_id = action.task_id
                   AND before_capture.agent_run_id = $2::uuid
                   AND before_capture.surface_kind = 'web'
                   AND before_capture.surface_session_id = action.session_id
                   AND before_capture.capture_role = 'before_action'
                   AND before_capture.action_execution_id = action.action_execution_id
                   AND NOT EXISTS (
                       SELECT 1
                       FROM qc_page_capture AS before_capture_lifecycle
                       WHERE before_capture_lifecycle.id = before_capture.id
                         AND COALESCE(
                             before_capture_lifecycle.inspection_lifecycle_status,
                             before_capture.metadata->>'inspection_lifecycle_status',
                             ''
                         ) IN ('superseded', 'abandoned')
                   )
                LEFT JOIN qc_page_capture AS before_page_capture
                    ON before_page_capture.id = before_capture.id
                   AND before_page_capture.capture_role = 'before_action'
                LEFT JOIN qc_evidence_artifact AS after_evidence
                    ON after_evidence.page_capture_id = common_capture.id
                   AND after_evidence.task_id = action.task_id
                LEFT JOIN qc_evidence_artifact AS before_evidence
                    ON before_evidence.page_capture_id = before_capture.id
                   AND before_evidence.task_id = action.task_id
                LEFT JOIN LATERAL (
                    SELECT manifest_id, manifest_hash, quality_status
                    FROM qc_evidence_manifest AS manifest
                    WHERE manifest.task_id = action.task_id
                      AND manifest.capture_id = before_capture.id
                    ORDER BY manifest.manifest_version DESC
                    LIMIT 1
                ) AS before_manifest ON TRUE
                LEFT JOIN LATERAL (
                    SELECT manifest_id, manifest_hash, quality_status
                    FROM qc_evidence_manifest AS manifest
                    WHERE manifest.task_id = action.task_id
                      AND manifest.capture_id = common_capture.id
                    ORDER BY manifest.manifest_version DESC
                    LIMIT 1
                ) AS after_manifest ON TRUE
                WHERE action.task_id = $1::uuid
                  AND task.status = 'running'
                  AND task.finalization_state IN (
                      'open', 'exploration_draining', 'session_draining'
                  )
                  AND (
                        (
                            action.session_id = ('h5:' || $2::text)
                            AND surface_session.session_owner = 'h5-workflow'
                            AND surface_session.session_owner NOT LIKE 'h5-browser-recovery:%'
                        )
                        OR (
                            action.session_id ~ (
                                '^h5:' || $2::text || ':recovery:[0-9a-f]{{20}}$'
                            )
                            AND surface_session.session_owner = (
                                'h5-browser-recovery:' || $2::text
                            )
                        )
                        OR (
                            action.session_id = ('web-v2:' || $2::text)
                            AND surface_session.session_owner = 'web-workflow'
                        )
                        OR (
                            task.target_platform = 'web'
                            AND action.session_id = ('web:' || $2::text)
                            AND surface_session.session_owner = 'web-v2-workflow'
                        )
                  )
                  AND EXISTS (
                      SELECT 1 FROM qc_agent_run AS run
                      WHERE run.task_id = action.task_id
                        AND run.id = $2::uuid
                        AND (
                              task.finalization_agent_run_id = run.id
                              OR (
                                  task.finalization_agent_run_id IS NULL
                                  AND run.status IN ('pending', 'running')
                                  AND NOT EXISTS (
                                      SELECT 1
                                      FROM qc_agent_run AS competing_run
                                      WHERE competing_run.task_id = task.id
                                        AND competing_run.id <> run.id
                                        AND competing_run.status IN ('pending', 'running')
                                        AND NOT (
                                            task.target_platform = 'web'
                                            AND competing_run.agent_type = 'rule_judge'
                                            AND competing_run.input_context->>'source'
                                                = 'h5_inspection_publication'
                                            AND competing_run.input_context->>'finalization_epoch'
                                                = task.finalization_epoch::text
                                        )
                                  )
                              )
                        )
                  )
                GROUP BY
                    task.id,
                    page_capture.id,
                    action.action_execution_id,
                    action.session_id,
                    surface_session.session_owner,
                    surface_session.status,
                    task.finalization_state,
                    surface_session.finalization_epoch,
                    task.finalization_epoch,
                    task.target_platform,
                    action.command_sequence,
                    common_capture.id,
                    common_capture.created_at,
                    page_capture.page_type,
                    page_capture.page_state,
                    page_capture.page_seq,
                    page_capture.detection_status,
                    page_capture.metadata,
                    common_capture.metadata,
                    page_capture.capture_role,
                    page_capture.url,
                    page_capture.inspection_status,
                    action.action_execution_id,
                    assessment.assessment_record_id,
                    assessment.payload_hash,
                    assessment.logical_identity_fingerprint,
                    assessment.logical_state_fingerprint,
                    assessment.rule_matching_allowed,
                    before_manifest.manifest_id,
                    before_manifest.manifest_hash,
                    before_manifest.quality_status,
                    after_manifest.manifest_id,
                    after_manifest.manifest_hash,
                    after_manifest.quality_status,
                    before_capture.id,
                    before_page_capture.url
                ORDER BY action.command_sequence ASC, action.action_execution_id ASC
                """,
                task_id,
                agent_run_id,
            )
        return [
            H5EvidenceCapture(
                capture_id=str(row["capture_id"]),
                session_id=_required_row_text(row, "session_id"),
                session_owner=(
                    str(row.get("session_owner")).strip()
                    if row.get("session_owner") is not None
                    else None
                ),
                session_status=_optional_row_text(row, "session_status"),
                task_finalization_state=_optional_row_text(
                    row, "task_finalization_state"
                ),
                session_finalization_epoch=_optional_row_int(
                    row, "session_finalization_epoch"
                ),
                task_finalization_epoch=_optional_row_int(row, "task_finalization_epoch"),
                supplemental_evidence_source=_optional_row_text(
                    row, "supplemental_evidence_source"
                ),
                page_type=row["page_type"],
                page_state=row["page_state"],
                detection_status=row["detection_status"],
                capture_role=str(row["capture_role"]),
                url=str(row["url"] or ""),
                inspection_status=(
                    str(row["inspection_status"]).strip()
                    if row.get("inspection_status") is not None
                    else None
                ),
                action_execution_id=(
                    str(row["action_execution_id"])
                    if row["action_execution_id"] is not None
                    else None
                ),
                parent_capture_id=(
                    str(row["parent_capture_id"]) if row["parent_capture_id"] is not None else None
                ),
                before_url=str(row["before_url"] or ""),
                artifact_types=tuple(str(item) for item in (row["artifact_types"] or ())),
                before_artifact_types=tuple(
                    str(item) for item in (row["before_artifact_types"] or ())
                ),
                task_id=task_id,
                agent_run_id=agent_run_id,
                artifact_refs=_artifact_refs(row.get("artifact_refs")),
                before_artifact_refs=_artifact_refs(row.get("before_artifact_refs")),
                page_type_assessment_id=(
                    str(row.get("page_type_assessment_id")).strip()
                    if row.get("page_type_assessment_id") is not None
                    else None
                ),
                assessment_hash=(
                    str(row.get("assessment_hash")).strip()
                    if row.get("assessment_hash") is not None
                    else None
                ),
                before_manifest_id=_optional_row_text(row, "before_manifest_id"),
                before_manifest_hash=_optional_row_text(row, "before_manifest_hash"),
                before_manifest_quality_status=_optional_row_text(
                    row, "before_manifest_quality_status"
                ),
                after_manifest_id=_optional_row_text(row, "after_manifest_id"),
                after_manifest_hash=_optional_row_text(row, "after_manifest_hash"),
                after_manifest_quality_status=_optional_row_text(
                    row, "after_manifest_quality_status"
                ),
                logical_identity_fingerprint=_optional_row_text(
                    row, "logical_identity_fingerprint"
                ),
                logical_state_fingerprint=_optional_row_text(
                    row, "logical_state_fingerprint"
                ),
                logical_state_previously_published=bool(
                    row.get("logical_state_previously_published", False)
                ),
                rule_matching_allowed=bool(row.get("rule_matching_allowed", False)),
                inspection_ready=h5_capture_is_inspection_ready(
                    row.get("capture_metadata"), page_state=row["page_state"],
                    page_type=row["page_type"],
                    screenshot_checksums=[
                        item.checksum for item in _artifact_refs(row.get("artifact_refs"))
                        if item.artifact_type == "screenshot"
                    ],
                    primary_screenshot_checksums=(
                        json.loads(row["video_primary_checksums"])
                        if isinstance(row.get("video_primary_checksums"), str)
                        else row.get("video_primary_checksums", [])
                    ),
                ),
            )
            for row in rows
        ]


def _artifact_refs(values: Any) -> tuple[H5EvidenceArtifactRef, ...]:
    """将查询的制品元数据收敛为不可变对象读取身份。"""
    if isinstance(values, str):
        try:
            values = json.loads(values)
        except json.JSONDecodeError:
            return ()
    if isinstance(values, dict):
        values = (values,)
    result: list[H5EvidenceArtifactRef] = []
    for value in values or ():
        if not isinstance(value, dict):
            continue
        result.append(
            H5EvidenceArtifactRef(
                artifact_id=str(value.get("artifact_id") or ""),
                task_id=str(value.get("task_id") or ""),
                agent_run_id=str(value.get("agent_run_id") or ""),
                capture_id=str(value.get("capture_id") or ""),
                storage_path=str(value.get("storage_path") or ""),
                artifact_type=str(value.get("artifact_type") or ""),
                checksum=str(value.get("checksum") or ""),
                size_bytes=(
                    int(value["size_bytes"]) if value.get("size_bytes") is not None else -1
                ),
                lifecycle_status=(
                    str(value.get("lifecycle_status") or "active").strip().casefold()
                    or "active"
                ),
            )
        )
    return tuple(result)


def _required_row_text(row: Any, field_name: str) -> str:
    """拒绝用请求参数补造数据库身份。"""
    value = str(row.get(field_name) or "").strip()
    if not value:
        raise RuntimeError(f"H5 证据查询缺少 {field_name} 身份")
    return value


def _optional_row_text(row: Any, field_name: str) -> str | None:
    value = str(row.get(field_name) or "").strip()
    return value or None


def _optional_row_int(row: Any, field_name: str) -> int | None:
    value = row.get(field_name)
    return int(value) if value is not None else None
