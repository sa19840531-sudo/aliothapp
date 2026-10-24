"""MinIO 页面证据对象存储实现。"""

import asyncio
import hashlib
from pathlib import PurePosixPath
from typing import Any

from minio import Minio
from minio.error import S3Error

from services.api.app.application.evidence_artifact_backfill import (
    EvidenceArtifactComputedIdentity,
)
from services.api.app.application.page_capture_persistence import PersistedEvidenceArtifact
from services.api.app.infrastructure.storage.local_evidence_store import serialize_artifact
from services.api.app.infrastructure.storage.minio_read import read_minio_object_with_retry


class MinioEvidenceArtifactStore:
    """将页面证据写入 MinIO Bucket，并返回数据库所需元数据。"""

    def __init__(
        self,
        *,
        endpoint: str,
        access_key: str,
        secret_key: str,
        bucket: str,
        secure: bool = False,
        client: Any | None = None,
    ) -> None:
        self._bucket = bucket
        self._client = client or Minio(
            endpoint,
            access_key=access_key,
            secret_key=secret_key,
            secure=secure,
        )

    async def check_ready(self) -> bool:
        """只读确认目标 Bucket 可访问；不得在 readiness 中创建或修改对象。"""
        return bool(await asyncio.to_thread(self._client.bucket_exists, self._bucket))

    async def store(
        self,
        *,
        task_id: str,
        page_capture_id: str,
        artifact_id: str,
        artifact_type: str,
        content: Any,
    ) -> PersistedEvidenceArtifact:
        return await asyncio.to_thread(
            self._store_sync,
            task_id,
            page_capture_id,
            artifact_id,
            artifact_type,
            content,
        )

    async def delete(self, storage_path: str) -> None:
        await asyncio.to_thread(self._client.remove_object, self._bucket, storage_path)

    async def read(self, storage_path: str) -> bytes:
        return await asyncio.to_thread(self._read_sync, storage_path)

    async def inspect_identity(self, storage_path: str) -> EvidenceArtifactComputedIdentity:
        """流式读取 MinIO 对象并确保连接归还连接池。"""
        return await asyncio.to_thread(self._inspect_identity_sync, storage_path)

    def _inspect_identity_sync(self, storage_path: str) -> EvidenceArtifactComputedIdentity:
        try:
            response = self._client.get_object(self._bucket, storage_path)
        except S3Error as exc:
            if exc.code in {"NoSuchKey", "NoSuchObject"}:
                raise FileNotFoundError(storage_path) from exc
            raise
        digest = hashlib.sha256()
        size_bytes = 0
        try:
            for chunk in response.stream(1024 * 1024):
                digest.update(chunk)
                size_bytes += len(chunk)
        finally:
            response.close()
            response.release_conn()
        return EvidenceArtifactComputedIdentity(
            checksum=f"sha256:{digest.hexdigest()}",
            size_bytes=size_bytes,
        )

    def _store_sync(
        self,
        task_id: str,
        page_capture_id: str,
        artifact_id: str,
        artifact_type: str,
        content: Any,
    ) -> PersistedEvidenceArtifact:
        data, extension, content_type, metadata = serialize_artifact(artifact_type, content)
        object_name = (
            PurePosixPath(task_id) / page_capture_id / f"{artifact_id}.{extension}"
        ).as_posix()
        checksum = hashlib.sha256(data).hexdigest()
        try:
            stat = self._client.stat_object(self._bucket, object_name)
        except (FileNotFoundError, KeyError):
            stat = None
        except S3Error as exc:
            if exc.code in {"NoSuchKey", "NoSuchObject"}:
                stat = None
            else:
                raise
        if stat is not None:
            existing_checksum = _metadata_value(stat.metadata, "sha256")
            if existing_checksum is None:
                existing = self._read_sync(object_name)
                existing_checksum = hashlib.sha256(existing).hexdigest()
                existing_size = len(existing)
            else:
                existing_size = int(stat.size)
            if existing_size != len(data) or existing_checksum != checksum:
                raise ValueError("证据对象不可覆盖：同一身份对应不同内容")
            return PersistedEvidenceArtifact(
                artifact_id=artifact_id,
                artifact_type=artifact_type,
                storage_path=object_name,
                content_type=content_type,
                checksum=f"sha256:{checksum}",
                size_bytes=existing_size,
                metadata={**metadata, "storage_backend": "minio", "bucket": self._bucket},
                created=False,
            )
        put_object = getattr(self._client, "_put_object", None)
        if not callable(put_object):
            raise RuntimeError("MinIO 客户端缺少条件写入能力，禁止降级为无条件覆盖")
        try:
            put_object(
                self._bucket,
                object_name,
                data,
                headers={
                    "Content-Type": content_type,
                    "If-None-Match": "*",
                    "X-Amz-Meta-Sha256": checksum,
                },
            )
        except S3Error as exc:
            if exc.code not in {
                "PreconditionFailed",
                "ConditionalRequestConflict",
                "ObjectAlreadyExists",
                "EntityAlreadyExists",
            }:
                raise
            existing = self._read_sync(object_name)
            existing_checksum = hashlib.sha256(existing).hexdigest()
            if len(existing) != len(data) or existing_checksum != checksum:
                raise ValueError("证据对象不可覆盖：同一身份对应不同内容") from exc
            return PersistedEvidenceArtifact(
                artifact_id=artifact_id,
                artifact_type=artifact_type,
                storage_path=object_name,
                content_type=content_type,
                checksum=f"sha256:{checksum}",
                size_bytes=len(existing),
                metadata={**metadata, "storage_backend": "minio", "bucket": self._bucket},
                created=False,
            )
        return PersistedEvidenceArtifact(
            artifact_id=artifact_id,
            artifact_type=artifact_type,
            storage_path=object_name,
            content_type=content_type,
            checksum=f"sha256:{checksum}",
            size_bytes=len(data),
            metadata={**metadata, "storage_backend": "minio", "bucket": self._bucket},
            created=True,
        )

    def _read_sync(self, storage_path: str) -> bytes:
        try:
            return read_minio_object_with_retry(self._client, self._bucket, storage_path)
        except S3Error as exc:
            if exc.code in {"NoSuchKey", "NoSuchObject"}:
                raise FileNotFoundError(storage_path) from exc
            raise


def _metadata_value(metadata: Any, name: str) -> str | None:
    if not isinstance(metadata, dict):
        return None
    wanted = name.casefold()
    for key, value in metadata.items():
        normalized = str(key).removeprefix("x-amz-meta-").casefold()
        if normalized == wanted:
            return str(value)
    return None
