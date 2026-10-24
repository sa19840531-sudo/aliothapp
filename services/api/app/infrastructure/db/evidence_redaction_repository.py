"""平台局部脱敏计划仓储：可信来源解析、不可变冻结与创建审计原子落库。"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any
from uuid import UUID

from services.api.app.domain.evidence import EvidenceManifestReference
from services.api.app.domain.protocol import stable_protocol_hash
from services.api.app.infrastructure.db.pool import get_postgres_pool


class PostgresEvidenceRedactionRepository:
    def __init__(self, *, database_url: str | None = None, pool: Any | None = None) -> None:
        self._database_url = database_url
        self._pool = pool

    async def source(
        self,
        artifact_id: str,
        manifest_id: str | None = None,
        manifest_version: int | None = None,
    ) -> dict[str, Any] | None:
        """按精确版本读取来源；存在 Capture 时必须验证可信 Manifest 成员。"""
        if not _is_uuid(artifact_id):
            return None
        if manifest_version is not None and (
            isinstance(manifest_version, bool)
            or not isinstance(manifest_version, int)
            or manifest_version <= 0
        ):
            raise ValueError("Manifest 版本必须为正整数")
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                """SELECT artifact.id::text AS artifact_id,
                    artifact.task_id::text AS task_id, task.tenant_id::text AS tenant_id,
                    artifact.page_capture_id::text AS capture_id,
                    artifact.artifact_type,artifact.content_type,artifact.checksum,
                    artifact.size_bytes,artifact.storage_path,
                    capture.task_id::text AS capture_task_id,
                    capture.agent_run_id::text AS agent_run_id,
                    capture.surface_session_id AS session_id,
                    run.task_id::text AS run_task_id,
                    session.task_id::text AS session_task_id
                FROM qc_evidence_artifact AS artifact
                JOIN qc_task AS task ON task.id=artifact.task_id
                LEFT JOIN qc_capture AS capture ON capture.id=artifact.page_capture_id
                LEFT JOIN qc_agent_run AS run ON run.id=capture.agent_run_id
                LEFT JOIN qc_surface_session AS session
                  ON session.session_id=capture.surface_session_id
                WHERE artifact.id=$1::uuid AND task.deleted_at IS NULL
                  AND COALESCE(artifact.lifecycle_status,'active')
                      NOT IN ('pending_delete','deleted')""",
                artifact_id,
            )
            if row is None:
                return None
            row = dict(row)
            if row["capture_id"] is None:
                if manifest_id is not None or manifest_version is not None:
                    raise ValueError("无 Capture 的原件不能绑定 Manifest")
                manifest = None
            else:
                if (
                    row["capture_task_id"] != row["task_id"]
                    or row["session_task_id"] != row["task_id"]
                    or (row["agent_run_id"] is not None and row["run_task_id"] != row["task_id"])
                ):
                    raise ValueError("原件 Capture、Run 或 Session 归属不一致")
                stored = await connection.fetchrow(
                    """SELECT * FROM qc_evidence_manifest
                    WHERE capture_id=$1::uuid AND task_id=$2::uuid
                      AND ($3::text IS NULL OR manifest_id=$3)
                      AND ($4::int IS NULL OR manifest_version=$4)
                    ORDER BY manifest_version DESC LIMIT 1""",
                    row["capture_id"],
                    row["task_id"],
                    manifest_id,
                    manifest_version,
                )
                manifest = _validate_manifest(row, stored)
        source = {
            "artifact_id": row["artifact_id"],
            "task_id": row["task_id"],
            "agent_run_id": row["agent_run_id"],
            "session_id": row["session_id"],
            "capture_id": row["capture_id"],
            "manifest_id": manifest.manifest_id if manifest else None,
            "manifest_version": manifest.manifest_version if manifest else None,
            "manifest_hash": manifest.manifest_hash if manifest else None,
            "original_checksum": row["checksum"],
            "original_size_bytes": row["size_bytes"],
            "original_content_type": row["content_type"] or "application/octet-stream",
            "artifact_type": row["artifact_type"],
        }
        return {
            **source,
            "source_hash": stable_protocol_hash(source),
            "tenant_id": row["tenant_id"],
        }

    async def get(self, redaction_id: str) -> dict[str, Any] | None:
        if not _is_uuid(redaction_id):
            return None
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            payload = await connection.fetchval(
                "SELECT plan_payload FROM qc_evidence_redaction_plan WHERE id=$1::uuid",
                redaction_id,
            )
        return _mapping(payload) if payload is not None else None

    async def list_for_artifact(self, artifact_id: str) -> list[dict[str, Any]]:
        if not _is_uuid(artifact_id):
            return []
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            rows = await connection.fetch(
                "SELECT plan_payload FROM qc_evidence_redaction_plan "
                "WHERE artifact_id=$1::uuid ORDER BY created_at DESC,id",
                artifact_id,
            )
        return [_mapping(row["plan_payload"]) for row in rows]

    async def save(self, plan: dict[str, Any], audit: Any, audit_kwargs: dict[str, Any]) -> None:
        """计划和创建成功审计使用同一连接及事务，审计失败不遗留可交付计划。"""
        source = plan["source"]
        expected_audit = {
            "operation": "create",
            "result": "allowed",
            "artifact_id": source["artifact_id"],
            "task_id": source["task_id"],
            "actor_user_id": plan["reviewed_by"],
            "tenant_id": plan["tenant_id"],
            "redaction_id": plan["redaction_id"],
            "redaction_plan_hash": plan["plan_hash"],
            "source_hash": source["source_hash"],
            "original_checksum": source["original_checksum"],
            "derived_checksum": plan["derived_checksum"],
            "policy_version": plan["policy_version"],
        }
        if any(audit_kwargs.get(key) != value for key, value in expected_audit.items()):
            raise ValueError("创建审计与冻结脱敏计划不一致")
        created_at = datetime.fromisoformat(str(plan["created_at"]).replace("Z", "+00:00"))
        pool = await self._get_pool()
        async with pool.acquire() as connection, connection.transaction():
            await connection.execute(
                """INSERT INTO qc_evidence_redaction_plan (
                    id,artifact_id,task_id,tenant_id,plan_hash,plan_payload,created_by,created_at
                ) VALUES ($1::uuid,$2::uuid,$3::uuid,$4,$5,$6::jsonb,$7,$8)""",
                plan["redaction_id"],
                source["artifact_id"],
                source["task_id"],
                plan["tenant_id"],
                plan["plan_hash"],
                json.dumps(plan, ensure_ascii=False, allow_nan=False),
                plan["reviewed_by"],
                created_at,
            )
            await audit.record_redaction(connection=connection, **audit_kwargs)

    async def _get_pool(self) -> Any:
        return self._pool or await get_postgres_pool(self._database_url)


def _validate_manifest(row, stored) -> EvidenceManifestReference:
    if stored is None:
        raise ValueError("原件缺少所选版本的可信 Manifest")
    payload = _mapping(stored["manifest_payload"])
    manifest = EvidenceManifestReference.from_dict(payload)
    if any(
        [
            manifest.task_id != row["task_id"],
            manifest.capture_id != row["capture_id"],
            manifest.session_id != row["session_id"],
            manifest.manifest_id != stored["manifest_id"],
            manifest.manifest_version != stored["manifest_version"],
            manifest.manifest_hash != stored["manifest_hash"],
            manifest.session_id != stored["session_id"],
            stable_protocol_hash(payload) != stored["payload_hash"],
        ]
    ):
        raise ValueError("Manifest 冻结身份或内容哈希不一致")
    member = next(
        (item for item in manifest.artifacts if item.artifact_id == row["artifact_id"]), None
    )
    if member is None or any(
        [
            member.artifact_type != row["artifact_type"],
            member.checksum != row["checksum"],
            member.size_bytes != row["size_bytes"],
            member.storage_path != row["storage_path"],
            member.content_type != row["content_type"],
        ]
    ):
        raise ValueError("原件不属于所选 Manifest 或成员内容不一致")
    return manifest


def _mapping(payload):
    value = json.loads(payload) if isinstance(payload, str) else payload
    if not isinstance(value, dict):
        raise ValueError("冻结对象必须为 JSON 对象")
    return value


def _is_uuid(value):
    try:
        UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        return False
    return True
