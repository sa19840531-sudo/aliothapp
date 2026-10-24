"""证据下载访问审计仓储：按次写入票据兑现的主体、证据与放行结果。"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

from services.api.app.infrastructure.db.pool import get_postgres_pool

SUBJECT_REVOKED = "subject_revoked"
OBJECT_ACCESS_REVOKED = "object_access_revoked"


class PostgresEvidenceDownloadAuditRepository:
    def __init__(self, *, database_url: str | None = None, pool: Any | None = None) -> None:
        self._database_url = database_url
        self._pool = pool

    async def record(
        self,
        *,
        artifact_id: str,
        actor_user_id: str,
        tenant_id: str,
        ticket_id: str,
        result: str,
        denied_reason: str | None = None,
    ) -> None:
        """写入一条不可变下载审计；表上有触发器拒绝后续 UPDATE/DELETE。"""
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            await connection.execute(
                """
                INSERT INTO qc_evidence_download_audit (
                    artifact_id,
                    actor_user_id,
                    tenant_id,
                    ticket_id,
                    result,
                    denied_reason,
                    operation
                )
                VALUES ($1, $2, $3, $4, $5, $6, 'download')
                """,
                artifact_id,
                actor_user_id,
                tenant_id,
                ticket_id,
                result,
                denied_reason,
            )

    async def record_export(
        self,
        *,
        request_id: str,
        task_id: str,
        execution_plan_id: str,
        snapshot_hash: str,
        mode: str,
        policy_version: str,
        actor_user_id: str,
        tenant_id: str,
        artifact_ids,
        materials=(),
        result: str,
        error_code: str | None = None,
        failed_artifact_id: str | None = None,
        archive_checksum: str | None = None,
    ) -> None:
        """整组审计复用原下载表；一个事务写齐全部成员，失败时禁止交付内容。"""
        by_id = {item["artifact_id"]: item for item in materials}
        rows = []
        for artifact_id in sorted(set(artifact_ids)) or [f"group:{task_id}"]:
            material = by_id.get(artifact_id, {})
            rows.append(
                (
                    artifact_id,
                    actor_user_id,
                    tenant_id,
                    request_id,
                    result,
                    error_code,
                    request_id,
                    task_id,
                    execution_plan_id,
                    snapshot_hash,
                    mode,
                    policy_version,
                    material.get("original_checksum"),
                    material.get("checksum"),
                    archive_checksum,
                    failed_artifact_id,
                    material.get("redaction_id"),
                    material.get("redaction_plan_hash") or material.get("plan_hash"),
                )
            )
        pool = await self._get_pool()
        async with pool.acquire() as connection, connection.transaction():
            await connection.executemany(
                """INSERT INTO qc_evidence_download_audit (
                    artifact_id,actor_user_id,tenant_id,ticket_id,result,denied_reason,
                    export_request_id,task_id,execution_plan_id,snapshot_hash,export_mode,
                    policy_version,original_checksum,derived_checksum,archive_checksum,
                    failed_artifact_id, redaction_id, redaction_plan_hash, operation
                ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,
                          $17::uuid,$18,'export')""",
                rows,
            )

    async def record_redaction(
        self,
        *,
        request_id: str,
        operation: str,
        artifact_id: str,
        actor_user_id: str,
        tenant_id: str,
        result: str,
        task_id: str | None = None,
        redaction_id: str | None = None,
        redaction_plan_hash: str | None = None,
        source_hash: str | None = None,
        original_checksum: str | None = None,
        derived_checksum: str | None = None,
        policy_version: str | None = None,
        error_code: str | None = None,
        connection: Any | None = None,
    ) -> None:
        """复用原审计链；创建调用可传入事务连接，保证计划与成功审计原子写入。"""
        if operation not in {"preview", "create", "read", "list"}:
            raise ValueError("局部脱敏审计操作不受支持")
        async with self._connection(connection) as active:
            await active.execute(
                """INSERT INTO qc_evidence_download_audit (
                    artifact_id,actor_user_id,tenant_id,ticket_id,result,denied_reason,
                    operation,task_id,redaction_id,redaction_plan_hash,source_hash,
                    original_checksum,derived_checksum,policy_version
                ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9::uuid,$10,$11,$12,$13,$14)""",
                artifact_id,
                actor_user_id,
                tenant_id,
                request_id,
                result,
                error_code,
                f"redaction_{operation}",
                task_id,
                redaction_id,
                redaction_plan_hash,
                source_hash,
                original_checksum,
                derived_checksum,
                policy_version,
            )

    @asynccontextmanager
    async def _connection(self, connection):
        if connection is not None:
            yield connection
            return
        pool = await self._get_pool()
        async with pool.acquire() as active:
            yield active

    async def _get_pool(self) -> Any:
        if self._pool is not None:
            return self._pool
        return await get_postgres_pool(self._database_url)
