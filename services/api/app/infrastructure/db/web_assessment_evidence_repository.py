"""Web 判定模型所需证据附件的 PostgreSQL 元数据读取器。"""

from __future__ import annotations

from typing import Any

from services.api.app.application.web_assessment_evidence import (
    StoredWebAssessmentArtifact,
)
from services.api.app.infrastructure.db.pool import get_postgres_pool


class PostgresWebAssessmentArtifactMetadataReader:
    """只读取已完成 Capture 下受信来源的指定附件闭集。"""

    def __init__(
        self,
        *,
        database_url: str | None = None,
        pool: Any | None = None,
    ) -> None:
        self._database_url = database_url
        self._pool = pool

    async def read_many(
        self,
        *,
        artifact_ids: tuple[str, ...],
    ) -> tuple[StoredWebAssessmentArtifact, ...]:
        if (
            not artifact_ids
            or len(set(artifact_ids)) != len(artifact_ids)
            or any(not isinstance(item, str) or not item.strip() for item in artifact_ids)
        ):
            raise ValueError("artifact_ids 必须为非空、不重复的文字元组")
        pool = self._pool or await get_postgres_pool(self._database_url)
        async with pool.acquire() as connection:
            rows = await connection.fetch(
                """
                SELECT artifact.id::text AS id,
                       artifact.task_id::text AS task_id,
                       artifact.page_capture_id::text AS page_capture_id,
                       capture.parent_page_capture_id::text AS parent_capture_id,
                       capture.capture_role,
                       NULLIF(capture.metadata->>'capture_purpose', '') AS capture_purpose,
                       supplemental_phase.phase_protocol_version AS supplemental_phase_protocol_version,
                       artifact.artifact_type,
                       artifact.storage_path,
                       artifact.content_type,
                       artifact.checksum,
                       artifact.size_bytes
                FROM qc_evidence_artifact AS artifact
                INNER JOIN qc_page_capture AS capture
                  ON capture.id = artifact.page_capture_id
                 AND capture.task_id = artifact.task_id
                LEFT JOIN LATERAL (
                    SELECT phase.phase_protocol_version
                    FROM qc_surface_phase_run AS phase
                    WHERE phase.task_id=artifact.task_id
                      AND phase.session_id=capture.surface_session_id
                      AND phase.source_capture_id=capture.parent_page_capture_id
                      AND phase.phase_key='web_full_page_supplemental'
                      AND phase.phase_protocol_version='web-full-page-supplemental.v1'
                      AND phase.status='completed'
                      AND phase.evidence_artifact_ids ? artifact.id::text
                    ORDER BY phase.finished_at DESC, phase.phase_run_id
                    LIMIT 1
                ) AS supplemental_phase ON true
                WHERE artifact.id = ANY($1::uuid[])
                  AND capture.capture_status = 'captured'
                  AND artifact.source IN ('baseline', 'rule_supplement')
                  AND artifact.artifact_type IN (
                      'screenshot', 'dom', 'visible_text',
                      'ui_snapshot', 'action_trace', 'har'
                  )
                """,
                list(artifact_ids),
            )
        by_id = {str(row["id"]): row for row in rows}
        if set(by_id) != set(artifact_ids) or len(by_id) != len(rows):
            raise RuntimeError("WEB_ASSESSMENT_ARTIFACT_METADATA_MISSING")
        return tuple(
            StoredWebAssessmentArtifact(
                artifact_id=artifact_id,
                artifact_type=str(by_id[artifact_id]["artifact_type"]),
                task_id=str(by_id[artifact_id]["task_id"]),
                capture_id=str(by_id[artifact_id]["page_capture_id"]),
                parent_capture_id=(
                    str(by_id[artifact_id]["parent_capture_id"])
                    if by_id[artifact_id].get("parent_capture_id") is not None
                    else None
                ),
                capture_role=str(by_id[artifact_id].get("capture_role") or ""),
                capture_purpose=(
                    str(by_id[artifact_id]["capture_purpose"])
                    if by_id[artifact_id].get("capture_purpose") is not None
                    else None
                ),
                supplemental_phase_protocol_version=(
                    str(by_id[artifact_id]["supplemental_phase_protocol_version"])
                    if by_id[artifact_id].get("supplemental_phase_protocol_version")
                    is not None
                    else None
                ),
                storage_path=str(by_id[artifact_id]["storage_path"]),
                content_type=str(by_id[artifact_id]["content_type"] or ""),
                checksum=str(by_id[artifact_id]["checksum"] or ""),
                size_bytes=int(by_id[artifact_id]["size_bytes"]),
            )
            for artifact_id in artifact_ids
        )


__all__ = ["PostgresWebAssessmentArtifactMetadataReader"]
