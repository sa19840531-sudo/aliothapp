"""Evidence Manifest 与证据就绪事件的 PostgreSQL 仓储。"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from services.api.app.application.evidence_manifests import (
    CaptureEvidenceManifest,
    PersistedEvidenceManifest,
)
from services.api.app.domain.evidence import (
    EvidenceManifestReference,
    EvidenceQualityStatus,
    build_capture_committed,
    build_evidence_bundle_ready,
    build_evidence_manifest_reference,
)
from services.api.app.domain.protocol import build_protocol_envelope, stable_protocol_hash
from services.api.app.infrastructure.db.finalization_recovery_fence import (
    assert_finalization_recovery_fence,
)
from services.api.app.infrastructure.db.outbox_writer import insert_outbox_event
from services.api.app.infrastructure.db.pool import get_postgres_pool

CAPTURE_COMMITTED_MESSAGE = "alioth.v2.capture.committed"
EVIDENCE_BUNDLE_READY_MESSAGE = "alioth.v2.evidence-bundle.ready"


class EvidenceManifestPersistenceError(RuntimeError):
    """Manifest 的 Capture、Artifact 或 Packet 归属无法通过可信校验。"""


class PostgresEvidenceManifestRepository:
    """原子保存清单，并通过 Outbox 发布 Capture 与 Evidence 事件。"""

    def __init__(
        self,
        *,
        database_url: str | None = None,
        pool: Any | None = None,
        permitted_task_statuses: tuple[str, ...] = ("running",),
    ) -> None:
        if not permitted_task_statuses or any(
            not isinstance(item, str) or not item.strip() or item != item.strip()
            for item in permitted_task_statuses
        ):
            raise ValueError("permitted_task_statuses 必须为非空状态元组")
        self._database_url = database_url
        self._pool = pool
        self._permitted_task_statuses = tuple(dict.fromkeys(permitted_task_statuses))

    async def add(
        self,
        *,
        manifest: EvidenceManifestReference,
        recovery_lease_token: str | None = None,
        recovery_generation: int | None = None,
        recovery_lease_expires_at: datetime | None = None,
    ) -> PersistedEvidenceManifest:
        if not isinstance(manifest, EvidenceManifestReference):
            raise TypeError("manifest 必须为 EvidenceManifestReference")
        pool = await self._get_pool()
        async with pool.acquire() as connection, connection.transaction():
            if recovery_lease_token is not None:
                await assert_finalization_recovery_fence(
                    connection,
                    task_id=manifest.task_id,
                    recovery_lease_token=recovery_lease_token,
                    recovery_generation=recovery_generation,
                    recovery_lease_expires_at=recovery_lease_expires_at,
                )
            return await insert_manifest_on_connection(
                connection,
                manifest=manifest,
                permitted_task_statuses=self._permitted_task_statuses,
            )

    async def get(self, *, manifest_id: str) -> EvidenceManifestReference | None:
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            payload = await connection.fetchval(
                """
                SELECT manifest_payload
                FROM qc_evidence_manifest
                WHERE manifest_id = $1
                """,
                manifest_id,
            )
        return _parse_manifest(payload)

    async def get_latest_for_capture(
        self,
        *,
        task_id: str,
        capture_id: str,
        require_quality_pass: bool = False,
    ) -> EvidenceManifestReference | None:
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            payload = await connection.fetchval(
                """
                SELECT manifest_payload
                FROM qc_evidence_manifest
                WHERE task_id = $1::uuid
                  AND capture_id = $2::uuid
                  AND (NOT $3::boolean OR quality_status = 'pass')
                ORDER BY manifest_version DESC
                LIMIT 1
                """,
                task_id,
                capture_id,
                require_quality_pass,
            )
        return _parse_manifest(payload)

    async def list_latest_for_session(
        self,
        *,
        task_id: str,
        session_id: str,
        agent_run_id: str,
    ) -> tuple[CaptureEvidenceManifest, ...]:
        """读取目标 Run/Session 每个 Capture 的最新 Manifest 版本。"""
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            rows = await connection.fetch(
                """
                WITH latest_manifest AS (
                    SELECT DISTINCT ON (manifest.capture_id)
                        manifest.capture_id,
                        manifest.manifest_payload
                    FROM qc_evidence_manifest AS manifest
                    WHERE manifest.task_id = $1::uuid
                    ORDER BY manifest.capture_id, manifest.manifest_version DESC
                )
                SELECT
                    capture.id::text AS capture_id,
                    latest_manifest.manifest_payload,
                    capture.agent_run_id::text AS agent_run_id,
                    capture.capture_role,
                    capture.captured_at,
                    capture.metadata,
                    capture.action_execution_id
                FROM qc_capture AS capture
                LEFT JOIN latest_manifest
                    ON latest_manifest.capture_id = capture.id
                WHERE capture.task_id = $1::uuid
                  AND capture.surface_session_id = $2
                  AND capture.agent_run_id = $3::uuid
                  AND capture.capture_status = 'captured'
                ORDER BY capture.captured_at, capture.id
                """,
                task_id,
                session_id,
                agent_run_id,
            )
        return tuple(_parse_capture_manifest(row) for row in rows)

    async def list_stale_pending(
        self,
        *,
        created_before: datetime,
        limit: int = 100,
    ) -> tuple[EvidenceManifestReference, ...]:
        """只返回每个 App Capture 最新且已超龄的 PENDING Manifest。"""

        if not isinstance(created_before, datetime) or created_before.utcoffset() is None:
            raise ValueError("created_before 必须为带时区 datetime")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise ValueError("limit 必须在 1 到 1000 之间")
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            rows = await connection.fetch(
                """
                WITH latest_manifest AS (
                    SELECT DISTINCT ON (manifest.capture_id)
                        manifest.capture_id,
                        manifest.quality_status,
                        manifest.created_at,
                        manifest.manifest_payload
                    FROM qc_evidence_manifest AS manifest
                    ORDER BY manifest.capture_id, manifest.manifest_version DESC
                )
                SELECT latest_manifest.manifest_payload
                FROM latest_manifest
                INNER JOIN qc_capture AS capture
                  ON capture.id = latest_manifest.capture_id
                 AND capture.capture_status = 'captured'
                INNER JOIN qc_task AS task
                  ON task.id = capture.task_id
                 AND task.target_platform = 'app'
                WHERE latest_manifest.quality_status = 'pending'
                  AND latest_manifest.created_at <= $1
                ORDER BY latest_manifest.created_at, latest_manifest.capture_id
                LIMIT $2
                """,
                created_before,
                limit,
            )
        return tuple(
            manifest
            for row in rows
            if (manifest := _parse_manifest(row["manifest_payload"])) is not None
        )

    async def fail_latest_pending_for_agent_run(
        self,
        *,
        task_id: str,
        agent_run_id: str,
        quality_reason: str,
        created_at: datetime | None = None,
    ) -> int:
        """将目标 Run 下最新仍为 pending 的清单结转为 fail 终态版本。"""

        reason = str(quality_reason or "").strip()
        if not reason:
            raise ValueError("quality_reason 不能为空")
        occurred_at = created_at or datetime.now(UTC)
        pool = await self._get_pool()
        finalized = 0
        async with pool.acquire() as connection, connection.transaction():
            await connection.execute(
                "SELECT pg_advisory_xact_lock(hashtext($1))",
                f"task-finalization:{task_id}",
            )
            rows = await connection.fetch(
                """
                WITH latest_manifest AS (
                    SELECT DISTINCT ON (manifest.capture_id)
                        manifest.capture_id,
                        manifest.quality_status,
                        manifest.manifest_payload
                    FROM qc_evidence_manifest AS manifest
                    INNER JOIN qc_capture AS capture
                        ON capture.id = manifest.capture_id
                    WHERE manifest.task_id = $1::uuid
                      AND capture.agent_run_id = $2::uuid
                      AND capture.capture_status = 'captured'
                    ORDER BY manifest.capture_id, manifest.manifest_version DESC
                )
                SELECT manifest_payload
                FROM latest_manifest
                WHERE quality_status = 'pending'
                ORDER BY capture_id
                """,
                task_id,
                agent_run_id,
            )
            for row in rows:
                latest = _parse_manifest(row["manifest_payload"])
                if latest is None:
                    continue
                failed_manifest = build_evidence_manifest_reference(
                    task_id=latest.task_id,
                    session_id=latest.session_id,
                    capture_id=latest.capture_id,
                    manifest_version=latest.manifest_version + 1,
                    artifacts=latest.artifacts,
                    observation_packet_ids=latest.observation_packet_ids,
                    quality_status=EvidenceQualityStatus.FAIL,
                    quality_reason=reason,
                    created_at=occurred_at,
                )
                persisted = await insert_manifest_on_connection(
                    connection,
                    manifest=failed_manifest,
                    permitted_task_statuses=self._permitted_task_statuses,
                )
                if persisted.created:
                    finalized += 1
        return finalized

    async def _get_pool(self) -> Any:
        if self._pool is not None:
            return self._pool
        return await get_postgres_pool(self._database_url)


async def insert_manifest_on_connection(
    connection: Any,
    *,
    manifest: EvidenceManifestReference,
    permitted_task_statuses: tuple[str, ...] = ("running",),
) -> PersistedEvidenceManifest:
    """在调用方事务内原子写入清单行与 Outbox 事件。

    E09：PostgresPageCaptureRepository.save_capture 在同一事务里携带 pending
    清单调用本函数（放宽到 queued/running，与采集行自身的任务门槛一致）；
    独立 add() 保持只允许 running。
    """
    if not isinstance(manifest, EvidenceManifestReference):
        raise TypeError("manifest 必须为 EvidenceManifestReference")
    payload = manifest.to_dict()
    await connection.execute(
        "SELECT pg_advisory_xact_lock(hashtext($1))",
        f"task-finalization:{manifest.task_id}",
    )
    await _validate_manifest_sources(connection, manifest=manifest)
    row = await connection.fetchrow(
        """
        INSERT INTO qc_evidence_manifest AS existing (
            manifest_id, manifest_version, task_id, session_id,
            capture_id, protocol_version, manifest_hash, payload_hash,
            quality_status, quality_reason, manifest_payload, created_at
        )
        SELECT
            $1,
            $2,
            capture.task_id,
            $4,
            capture.id,
            $6,
            $7,
            $8,
            $9,
            $10,
            $11::jsonb,
            $12
        FROM qc_capture AS capture
        INNER JOIN qc_task AS task
            ON task.id = capture.task_id
           AND task.status = ANY($13::text[])
           AND task.finalization_state IN (
               'open', 'exploration_draining', 'session_draining'
           )
        WHERE capture.id = $5::uuid
          AND capture.task_id = $3::uuid
          AND capture.capture_status = 'captured'
        ON CONFLICT (capture_id, manifest_version) DO UPDATE
        SET manifest_id = existing.manifest_id
        WHERE existing.manifest_id = EXCLUDED.manifest_id
          AND existing.manifest_hash = EXCLUDED.manifest_hash
          AND existing.payload_hash = EXCLUDED.payload_hash
        RETURNING
            existing.manifest_id,
            (xmax = 0) AS created
        """,
        manifest.manifest_id,
        manifest.manifest_version,
        manifest.task_id,
        manifest.session_id,
        manifest.capture_id,
        manifest.protocol_version,
        manifest.manifest_hash,
        stable_protocol_hash(payload),
        manifest.quality_status.value,
        manifest.quality_reason,
        _json_dumps(payload),
        manifest.created_at,
        list(permitted_task_statuses),
    )
    if row is None or str(row["manifest_id"]) != manifest.manifest_id:
        raise EvidenceManifestPersistenceError(
            "Manifest 版本已被其他内容占用或 Capture 归属无效"
        )
    capture_event = build_capture_committed(
        manifest,
        occurred_at=manifest.created_at,
    )
    await insert_outbox_event(
        connection,
        aggregate_type="evidence_manifest",
        aggregate_id=manifest.manifest_id,
        envelope=build_protocol_envelope(
            message_type=CAPTURE_COMMITTED_MESSAGE,
            payload_protocol_version=capture_event.protocol_version,
            idempotency_key=capture_event.event_id,
            task_id=manifest.task_id,
            aggregate_type="evidence_manifest",
            aggregate_id=manifest.manifest_id,
            aggregate_version=manifest.manifest_version,
            occurred_at=capture_event.occurred_at,
            payload=capture_event.to_dict(),
            correlation_id=manifest.task_id,
        ),
    )
    if manifest.quality_status is EvidenceQualityStatus.PASS:
        ready_event = build_evidence_bundle_ready(
            manifest,
            occurred_at=manifest.created_at,
        )
        await insert_outbox_event(
            connection,
            aggregate_type="evidence_manifest",
            aggregate_id=manifest.manifest_id,
            envelope=build_protocol_envelope(
                message_type=EVIDENCE_BUNDLE_READY_MESSAGE,
                payload_protocol_version=ready_event.protocol_version,
                idempotency_key=ready_event.event_id,
                task_id=manifest.task_id,
                aggregate_type="evidence_manifest",
                aggregate_id=manifest.manifest_id,
                aggregate_version=manifest.manifest_version,
                occurred_at=ready_event.occurred_at,
                payload=ready_event.to_dict(),
                correlation_id=manifest.task_id,
                causation_id=capture_event.event_id,
            ),
        )
    return PersistedEvidenceManifest(
        manifest=manifest,
        created=bool(row["created"]),
    )


async def _validate_manifest_sources(
    connection: Any,
    *,
    manifest: EvidenceManifestReference,
) -> None:
    for artifact in manifest.artifacts:
        valid = await connection.fetchval(
            """
            SELECT EXISTS (
                SELECT 1
                FROM qc_evidence_artifact
                WHERE id = $1::uuid
                  AND task_id = $2::uuid
                  AND page_capture_id = $3::uuid
                  AND artifact_type = $4
                  AND storage_path = $5
                  AND checksum = $6
                  AND size_bytes = $7
            )
            """,
            artifact.artifact_id,
            manifest.task_id,
            manifest.capture_id,
            artifact.artifact_type,
            artifact.storage_path,
            artifact.checksum,
            artifact.size_bytes,
        )
        if not valid:
            raise EvidenceManifestPersistenceError(
                f"Artifact {artifact.artifact_id} 不属于目标 Capture 或校验和不一致"
            )
    for packet_id in manifest.observation_packet_ids:
        valid = await connection.fetchval(
            """
            SELECT EXISTS (
                SELECT 1
                FROM qc_observation_packet
                WHERE id = $1::uuid
                  AND task_id = $2::uuid
                  AND page_capture_id = $3::uuid
                  AND (
                    ($4 = 'pass' AND quality_gate_status = 'pass')
                    OR ($4 = 'pending' AND quality_gate_status IN ('pending', 'pass'))
                    OR $4 = 'fail'
                  )
            )
            """,
            packet_id,
            manifest.task_id,
            manifest.capture_id,
            manifest.quality_status.value,
        )
        if not valid:
            raise EvidenceManifestPersistenceError(
                f"Observation Packet {packet_id} 未通过质量门禁或归属不一致"
            )


def _parse_manifest(payload: Any) -> EvidenceManifestReference | None:
    if payload is None:
        return None
    return EvidenceManifestReference.from_dict(_as_mapping(payload, "manifest_payload"))


def _parse_capture_manifest(row: Mapping[str, Any]) -> CaptureEvidenceManifest:
    manifest = _parse_manifest(row["manifest_payload"])
    return CaptureEvidenceManifest(
        capture_id=str(row["capture_id"]),
        manifest=manifest,
        agent_run_id=str(row["agent_run_id"]),
        capture_role=str(row["capture_role"]),
        captured_at=row["captured_at"],
        metadata=_as_mapping(row["metadata"], "capture.metadata"),
        action_execution_id=(
            str(row["action_execution_id"])
            if row["action_execution_id"] is not None
            else None
        ),
    )


def _as_mapping(value: Any, field_name: str) -> Mapping[str, Any]:
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, Mapping):
        raise EvidenceManifestPersistenceError(f"{field_name} 必须为对象")
    return value


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
