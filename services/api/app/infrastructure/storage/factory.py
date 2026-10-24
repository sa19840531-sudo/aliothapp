"""页面证据存储后端工厂。"""

from services.api.app.application.page_capture_persistence import EvidenceArtifactStore
from services.api.app.core.config import EvidenceStorageSettings
from services.api.app.infrastructure.storage.local_evidence_store import (
    LocalEvidenceArtifactStore,
)
from services.api.app.infrastructure.storage.minio_evidence_store import (
    MinioEvidenceArtifactStore,
)


def build_evidence_artifact_store(
    settings: EvidenceStorageSettings,
) -> EvidenceArtifactStore:
    """根据配置构建证据存储后端。"""
    if settings.backend == "local":
        return LocalEvidenceArtifactStore(root=settings.local_root)

    required_values = {
        "ALIOTH_MINIO_ENDPOINT": settings.minio_endpoint,
        "ALIOTH_MINIO_ACCESS_KEY": settings.minio_access_key,
        "ALIOTH_MINIO_SECRET_KEY": settings.minio_secret_key,
    }
    missing = [name for name, value in required_values.items() if not value]
    if missing:
        raise ValueError(f"MinIO 证据存储缺少配置：{', '.join(missing)}")

    return MinioEvidenceArtifactStore(
        endpoint=settings.minio_endpoint,
        access_key=settings.minio_access_key,
        secret_key=settings.minio_secret_key,
        bucket=settings.minio_bucket,
        secure=settings.minio_secure,
    )
