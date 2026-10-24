"""登记隔离登录账户附件；源首页只验证同 Run，不要求仍是当前帧。"""

from __future__ import annotations

import json
import re
from uuid import UUID

from services.api.app.infrastructure.db.pool import get_postgres_pool

_STEP = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_CHECKSUM = re.compile(r"^sha256:[0-9a-f]{64}$")


def _artifact_lock_key(artifact_id: str) -> int:
    """用附件 UUID 的高 64 位锁住登记与失败补偿的同一身份。"""

    return int.from_bytes(UUID(artifact_id).bytes[:8], "big", signed=True)


class WebLoginArtifactRegistrationError(RuntimeError):
    """登录附件未通过持久化身份门。"""


class PostgresWebLoginArtifactIndexRepository:
    def __init__(self, *, pool=None):
        self._pool = pool

    async def register(
        self, *, task_id: str, agent_run_id: str, main_session_id: str,
        isolated_session_id: str, source_capture_id: str, step: str, artifact,
    ) -> None:
        """同一任务、Run、Web 主会话及 epoch 下按附件身份幂等登记。"""

        try:
            for identifier in (
                task_id, agent_run_id, source_capture_id, artifact.artifact_id,
            ):
                UUID(identifier)
            if main_session_id != f"web:{agent_run_id}":
                raise ValueError("主会话不匹配")
            if isolated_session_id != f"{main_session_id}:login":
                raise ValueError("隔离会话不匹配")
            if not _STEP.fullmatch(step):
                raise ValueError("账户步骤无效")
            if artifact.artifact_type not in {"screenshot", "dom"}:
                raise ValueError("附件类型无效")
            expected_mime = (
                "image/png" if artifact.artifact_type == "screenshot"
                else "text/html; charset=utf-8"
            )
            if artifact.content_type != expected_mime:
                raise ValueError("附件内容类型无效")
            if not _CHECKSUM.fullmatch(artifact.checksum):
                raise ValueError("附件摘要无效")
            if type(artifact.size_bytes) is not int or artifact.size_bytes <= 0:
                raise ValueError("附件大小无效")
            if not isinstance(artifact.storage_path, str) or not artifact.storage_path:
                raise ValueError("附件路径无效")
            metadata = {
                **artifact.metadata,
                "source_capture_id": source_capture_id,
                "agent_run_id": agent_run_id,
                "main_session_id": main_session_id,
                "isolated_session_id": isolated_session_id,
                "step": step,
                "branch": "isolated_login",
            }
            encoded = json.dumps(metadata, ensure_ascii=False, sort_keys=True)
        except (AttributeError, TypeError, ValueError):
            raise WebLoginArtifactRegistrationError("WEB_LOGIN_ARTIFACT_IDENTITY_INVALID") from None

        pool = self._pool or await get_postgres_pool()
        try:
            async with pool.acquire() as connection, connection.transaction():
                await connection.execute(
                    "SELECT pg_advisory_xact_lock($1::bigint)",
                    _artifact_lock_key(artifact.artifact_id),
                )
                row = await connection.fetchrow(
                    """INSERT INTO qc_evidence_artifact (
                         id,task_id,page_capture_id,artifact_type,source,storage_path,
                         content_type,checksum,size_bytes,metadata)
                       SELECT $1::uuid,task.id,NULL::uuid,$6,'web_login_isolated',$7,
                              $8,$9,$10,$11::jsonb
                       FROM qc_task task
                       JOIN qc_agent_run run ON run.task_id=task.id AND run.id=$4::uuid
                       JOIN qc_capture source ON source.id=$3::uuid
                         AND source.task_id=task.id AND source.agent_run_id=run.id
                         AND source.surface_session_id=$5 AND source.surface_kind='web'
                         AND source.capture_status='captured'
                         AND source.metadata->>'finalization_epoch'=task.finalization_epoch::text
                       JOIN qc_page_capture page ON page.id=source.id
                         AND page.task_id=task.id AND page.agent_run_id=run.id
                         AND page.capture_status='captured'
                         AND page.metadata->>'finalization_epoch'=task.finalization_epoch::text
                       JOIN qc_surface_session session ON session.task_id=task.id
                         AND session.session_id=$5 AND session.session_owner='web-v2-workflow'
                         AND session.status IN ('active','draining')
                         AND session.finalization_epoch=task.finalization_epoch
                       WHERE task.id=$2::uuid AND lower(task.target_platform)='web'
                         AND task.status='running' AND run.status IN ('running','done')
                         AND task.finalization_state <> 'sealed'
                       ON CONFLICT (id) DO UPDATE SET id=EXCLUDED.id
                       WHERE qc_evidence_artifact.task_id=EXCLUDED.task_id
                         AND qc_evidence_artifact.page_capture_id IS NULL
                         AND qc_evidence_artifact.artifact_type=EXCLUDED.artifact_type
                         AND qc_evidence_artifact.source=EXCLUDED.source
                         AND qc_evidence_artifact.storage_path=EXCLUDED.storage_path
                         AND qc_evidence_artifact.content_type=EXCLUDED.content_type
                         AND qc_evidence_artifact.checksum=EXCLUDED.checksum
                         AND qc_evidence_artifact.size_bytes=EXCLUDED.size_bytes
                         AND qc_evidence_artifact.metadata=EXCLUDED.metadata
                       RETURNING id""",
                    artifact.artifact_id,
                    task_id,
                    source_capture_id,
                    agent_run_id,
                    main_session_id,
                    artifact.artifact_type,
                    artifact.storage_path,
                    artifact.content_type,
                    artifact.checksum,
                    artifact.size_bytes,
                    encoded,
                )
        except Exception:  # noqa: BLE001 - 不外发包含账户证据路径的 SQL 细节
            raise WebLoginArtifactRegistrationError("WEB_LOGIN_ARTIFACT_INDEX_REJECTED") from None
        if row is None:
            raise WebLoginArtifactRegistrationError("WEB_LOGIN_ARTIFACT_SOURCE_UNTRUSTED")

    async def confirm_registration(
        self, *, task_id: str, agent_run_id: str, main_session_id: str,
        isolated_session_id: str, source_capture_id: str, step: str, artifact,
    ) -> bool:
        """提交确认不明时只读核对完整身份，不在并发登记期间删除对象。"""

        pool = self._pool or await get_postgres_pool()
        async with pool.acquire() as connection, connection.transaction():
            await connection.execute(
                "SELECT pg_advisory_xact_lock($1::bigint)",
                _artifact_lock_key(artifact.artifact_id),
            )
            row = await connection.fetchrow(
                """SELECT task_id,source,page_capture_id,artifact_type,storage_path,
                          content_type,checksum,size_bytes,metadata
                   FROM qc_evidence_artifact WHERE id=$1::uuid""",
                artifact.artifact_id,
            )
        if row is None:
            return False
        metadata = row["metadata"]
        if isinstance(metadata, str):
            metadata = json.loads(metadata)
        expected_metadata = {
            **artifact.metadata,
            "source_capture_id": source_capture_id,
            "agent_run_id": agent_run_id,
            "main_session_id": main_session_id,
            "isolated_session_id": isolated_session_id,
            "step": step,
            "branch": "isolated_login",
        }
        return bool(
            str(row["task_id"]) == task_id
            and row["source"] == "web_login_isolated"
            and row["page_capture_id"] is None
            and row["artifact_type"] == artifact.artifact_type
            and row["storage_path"] == artifact.storage_path
            and row["content_type"] == artifact.content_type
            and row["checksum"] == artifact.checksum
            and row["size_bytes"] == artifact.size_bytes
            and metadata == expected_metadata
        )
