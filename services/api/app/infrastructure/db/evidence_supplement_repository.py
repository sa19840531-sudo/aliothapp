"""状态等价补采证据的 PostgreSQL 防伪挂接仓储。"""

from __future__ import annotations

from typing import Any

from services.api.app.application.evidence_supplements import (
    PersistedEvidenceSupplement,
)
from services.api.app.application.supplemental_evidence_policy import (
    SupplementalEvidenceOperation,
    SupplementalEvidencePolicy,
)
from services.api.app.domain.inspection import EvidenceSupplement
from services.api.app.domain.protocol import build_protocol_envelope, stable_protocol_hash
from services.api.app.infrastructure.db.outbox_writer import insert_outbox_event
from services.api.app.infrastructure.db.pool import get_postgres_pool

EVIDENCE_SUPPLEMENT_ATTACHED_MESSAGE = "alioth.v2.evidence-supplement.attached"


class EvidenceSupplementPersistenceError(RuntimeError):
    """Manifest、Probe Work、恢复证明或 Capture 血缘无法通过可信校验。"""


class PostgresEvidenceSupplementRepository:
    """只允许等价恢复后的成功 Probe Manifest 满足原 Evidence Request。"""

    def __init__(
        self,
        *,
        database_url: str | None = None,
        pool: Any | None = None,
        supplemental_evidence_policy: SupplementalEvidencePolicy | None = None,
    ) -> None:
        self._database_url = database_url
        self._pool = pool
        self._supplemental_evidence_policy = (
            supplemental_evidence_policy
            if supplemental_evidence_policy is not None
            else SupplementalEvidencePolicy()
        )

    async def attach(
        self,
        *,
        supplement: EvidenceSupplement,
    ) -> PersistedEvidenceSupplement:
        if not isinstance(supplement, EvidenceSupplement):
            raise TypeError("supplement 必须为 EvidenceSupplement")
        self._supplemental_evidence_policy.require_enabled(
            SupplementalEvidenceOperation.ATTACH_SUPPLEMENT
        )
        identity_payload = supplement.to_dict()
        identity_payload.pop("created_at")
        pool = await self._get_pool()
        async with pool.acquire() as connection, connection.transaction():
            row = await connection.fetchrow(
                """
                WITH RECURSIVE locked_request AS (
                    SELECT request.*
                    FROM qc_evidence_request AS request
                    WHERE request.request_id = $3
                      AND request.task_id = $2::uuid
                      AND (
                        (
                            request.status = 'running'
                            AND request.expires_at > now()
                        )
                        OR (
                            request.status = 'satisfied'
                            AND request.resolution_manifest_id = $4
                        )
                      )
                    FOR UPDATE
                ), capture_lineage AS (
                    SELECT capture.id, capture.parent_capture_id
                    FROM qc_evidence_manifest AS manifest
                    INNER JOIN qc_capture AS capture
                        ON capture.id = manifest.capture_id
                       AND capture.task_id = manifest.task_id
                    WHERE manifest.manifest_id = $4
                      AND manifest.task_id = $2::uuid
                    UNION
                    SELECT parent.id, parent.parent_capture_id
                    FROM capture_lineage AS child
                    INNER JOIN qc_capture AS parent
                        ON parent.id = child.parent_capture_id
                       AND parent.task_id = $2::uuid
                ), validated AS (
                    SELECT
                        request.request_id,
                        request.task_id,
                        manifest.manifest_id,
                        probe.work_id,
                        restoration.attempt_id,
                        restoration.equivalence_assessment_id,
                        restoration.source_grade
                    FROM locked_request AS request
                    INNER JOIN qc_restoration_attempt AS restoration
                        ON restoration.attempt_id = $6
                       AND restoration.request_id = request.request_id
                       AND restoration.task_id = request.task_id
                       AND restoration.status = 'equivalent'
                       AND restoration.equivalence_assessment_id = $7
                       AND restoration.source_grade = $8
                       AND restoration.assessment_payload->>'passed' = 'true'
                    INNER JOIN qc_v2_work_item AS probe
                        ON probe.work_id = $5
                       AND probe.work_type = 'probe'
                       AND probe.protocol_version = 'probe-work.v2'
                       AND probe.task_id = request.task_id
                       AND probe.evidence_request_id = request.request_id
                       AND probe.restoration_attempt_id = restoration.attempt_id
                       AND probe.equivalence_assessment_id
                            = restoration.equivalence_assessment_id
                       AND probe.source_grade = restoration.source_grade
                       AND probe.source_capture_id = restoration.restored_capture_id
                       AND probe.status = 'succeeded'
                       AND (
                            (
                                probe.work_payload->>'surface_kind' = 'web'
                                AND probe.created_at >= restoration.updated_at
                            )
                            OR (
                                probe.work_payload->>'surface_kind' IN ('android', 'ios')
                                AND probe.created_at >= restoration.finished_at
                            )
                       )
                    INNER JOIN qc_capture AS restored_capture
                        ON restored_capture.id = restoration.restored_capture_id
                       AND restored_capture.task_id = request.task_id
                       AND restored_capture.capture_status = 'captured'
                    INNER JOIN qc_evidence_manifest AS manifest
                        ON manifest.manifest_id = $4
                       AND manifest.task_id = request.task_id
                       AND manifest.session_id = probe.session_id
                       AND manifest.quality_status = 'pass'
                       AND manifest.capture_id <> restoration.restored_capture_id
                    INNER JOIN qc_capture AS result_capture
                        ON result_capture.id = manifest.capture_id
                       AND result_capture.task_id = request.task_id
                       AND result_capture.capture_status = 'captured'
                       AND result_capture.captured_at >= restored_capture.captured_at
                    WHERE EXISTS (
                        SELECT 1
                        FROM capture_lineage AS lineage
                        WHERE lineage.id = restoration.restored_capture_id
                    )
                      AND (
                        result_capture.metadata->>'capture_role'
                            = 'probe_result_replay'
                        OR (
                            probe.work_payload->>'surface_kind' IN ('android', 'ios')
                            AND result_capture.metadata->>'capture_role' = 'after_action'
                            AND EXISTS (
                                SELECT 1
                                FROM qc_action_execution AS probe_action
                                WHERE probe_action.task_id = request.task_id
                                  AND probe_action.session_id = probe.session_id
                                  AND probe_action.finalization_epoch
                                        = probe.finalization_epoch
                                  AND probe_action.status = 'completed'
                                  AND probe_action.result_capture_id = result_capture.id
                                  AND probe_action.intent_payload
                                        #>> '{parameters,exploration_work_id}'
                                        = probe.work_id
                            )
                        )
                      )
                      AND NOT EXISTS (
                        SELECT 1
                        FROM jsonb_array_elements_text(
                            request.request_payload->'required_artifact_types'
                        ) AS required(artifact_type)
                        WHERE NOT EXISTS (
                            SELECT 1
                            FROM jsonb_array_elements(
                                manifest.manifest_payload->'artifacts'
                            ) AS artifact(value)
                            WHERE artifact.value->>'artifact_type'
                                = required.artifact_type
                        )
                    )
                      AND EXISTS (
                        SELECT 1
                        FROM jsonb_array_elements(
                            manifest.manifest_payload->'artifacts'
                        ) AS artifact(value)
                        WHERE artifact.value->>'artifact_type' = 'action_trace'
                    )
                )
                INSERT INTO qc_evidence_supplement AS existing (
                    supplement_id, task_id, request_id, manifest_id,
                    probe_work_id, restoration_attempt_id,
                    equivalence_assessment_id, source_grade, origin,
                    protocol_version, payload_hash, created_at
                )
                SELECT
                    $1,
                    validated.task_id,
                    validated.request_id,
                    validated.manifest_id,
                    validated.work_id,
                    validated.attempt_id,
                    validated.equivalence_assessment_id,
                    validated.source_grade,
                    $9,
                    $10,
                    $11,
                    $12
                FROM validated
                ON CONFLICT (supplement_id) DO UPDATE
                SET supplement_id = existing.supplement_id
                WHERE existing.payload_hash = EXCLUDED.payload_hash
                  AND existing.request_id = EXCLUDED.request_id
                  AND existing.manifest_id = EXCLUDED.manifest_id
                RETURNING
                    existing.supplement_id,
                    existing.task_id::text AS task_id,
                    existing.request_id,
                    existing.manifest_id,
                    existing.probe_work_id,
                    existing.restoration_attempt_id,
                    existing.equivalence_assessment_id,
                    existing.source_grade,
                    existing.origin,
                    existing.protocol_version,
                    existing.created_at,
                    (xmax = 0) AS created
                """,
                supplement.supplement_id,
                supplement.task_id,
                supplement.request_id,
                supplement.manifest_id,
                supplement.probe_work_id,
                supplement.restoration_attempt_id,
                supplement.equivalence_assessment_id,
                supplement.source_grade.value,
                supplement.origin,
                supplement.protocol_version,
                stable_protocol_hash(identity_payload),
                supplement.created_at,
            )
            if row is None:
                raise EvidenceSupplementPersistenceError(
                    "补采证据缺少成功 Probe、等价恢复、必需产物或可信 Capture 血缘"
                )
            persisted = _parse_supplement(row)
            resolution_version = await connection.fetchval(
                """
                UPDATE qc_evidence_request AS request
                SET
                    status = 'satisfied',
                    resolution_manifest_id = $2,
                    resolution_reason = $3,
                    row_version = CASE
                        WHEN request.status = 'running'
                        THEN request.row_version + 1
                        ELSE request.row_version
                    END,
                    completed_at = COALESCE(request.completed_at, now()),
                    updated_at = now()
                WHERE request.request_id = $1
                  AND (
                    request.status = 'running'
                    OR (
                        request.status = 'satisfied'
                        AND request.resolution_manifest_id = $2
                    )
                  )
                  AND EXISTS (
                    SELECT 1
                    FROM qc_evidence_supplement AS attached
                    WHERE attached.supplement_id = $4
                      AND attached.request_id = request.request_id
                      AND attached.manifest_id = $2
                  )
                RETURNING request.row_version
                """,
                persisted.request_id,
                persisted.manifest_id,
                f"replay_probe:{persisted.supplement_id}",
                persisted.supplement_id,
            )
            if resolution_version is None:
                raise EvidenceSupplementPersistenceError(
                    "Evidence Request 已变化，补采证据不能原子收口"
                )
            payload = persisted.to_dict()
            envelope = build_protocol_envelope(
                message_type=EVIDENCE_SUPPLEMENT_ATTACHED_MESSAGE,
                payload_protocol_version=persisted.protocol_version,
                idempotency_key=persisted.supplement_id,
                task_id=persisted.task_id,
                aggregate_type="evidence_request",
                aggregate_id=persisted.request_id,
                aggregate_version=int(resolution_version),
                occurred_at=persisted.created_at,
                payload=payload,
                correlation_id=persisted.task_id,
                causation_id=persisted.probe_work_id,
            )
            await insert_outbox_event(
                connection,
                aggregate_type="evidence_request",
                aggregate_id=persisted.request_id,
                envelope=envelope,
            )
        return PersistedEvidenceSupplement(
            supplement=persisted,
            created=bool(row["created"]),
        )

    async def _get_pool(self) -> Any:
        if self._pool is not None:
            return self._pool
        return await get_postgres_pool(self._database_url)


def _parse_supplement(row: Any) -> EvidenceSupplement:
    return EvidenceSupplement.from_dict(
        {
            "protocol_version": str(row["protocol_version"]),
            "supplement_id": str(row["supplement_id"]),
            "task_id": str(row["task_id"]),
            "request_id": str(row["request_id"]),
            "manifest_id": str(row["manifest_id"]),
            "probe_work_id": str(row["probe_work_id"]),
            "restoration_attempt_id": str(row["restoration_attempt_id"]),
            "equivalence_assessment_id": str(row["equivalence_assessment_id"]),
            "source_grade": str(row["source_grade"]),
            "origin": str(row["origin"]),
            "created_at": row["created_at"].isoformat(),
        }
    )
