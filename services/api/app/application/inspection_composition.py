"""跨扫描平台的正式质检基础设施组合根。"""

from __future__ import annotations

from typing import Any

from services.api.app.application.app_evidence_manifest import (
    AppEvidenceManifestFinalizer,
)
from services.api.app.application.h5_live_inspection_publish import (
    build_default_h5_live_inspection_publish_service,
)
from services.api.app.application.web_live_inspection_publish import (
    WebLiveInspectionPublishService,
)
from services.api.app.application.web_manifest_finalizer import WebPageManifestFinalizer
from services.api.app.application.web_page_inspection_notification import (
    WebInspectionPublicationWakeup,
    WebPageInspectionNotification,
)
from services.api.app.infrastructure.db.h5_evidence_inventory_repository import (
    PostgresH5EvidenceInventoryRepository,
)
from services.api.app.infrastructure.db.h5_inspection_publication_repository import (
    PostgresH5InspectionPublicationRepository,
)
from services.api.app.infrastructure.db.web_capture_evidence_repository import (
    PostgresWebCaptureEvidenceRepository,
)
from services.api.app.infrastructure.db.web_evidence_manifest_repository import (
    PostgresWebEvidenceManifestRepository,
)
from services.api.app.infrastructure.db.web_live_inspection_repository import (
    PostgresWebLiveInspectionSourceRunReader,
)
from services.api.app.infrastructure.storage.factory import build_evidence_artifact_store


def build_default_web_page_inspection_notification(
    *,
    settings: Any,
    wakeup: WebInspectionPublicationWakeup | None = None,
) -> WebPageInspectionNotification:
    """组合 Web 端口与共享 Inventory、Publication、Manifest 基础设施。"""

    repository = PostgresWebCaptureEvidenceRepository()
    from services.api.app.infrastructure.db.web_capability_revision_repository import (
        PostgresWebCapabilityRevisionRepository,
    )
    from services.api.app.infrastructure.db.web_evidence_sequence_repository import (
        PostgresWebEvidenceSequenceRepository,
    )
    artifact_store = build_evidence_artifact_store(settings.evidence_storage)
    manifest_repository = PostgresWebEvidenceManifestRepository(
        permitted_task_statuses=("queued", "running", "review", "failed")
    )
    return WebPageInspectionNotification(
        sequence_repository=PostgresWebEvidenceSequenceRepository(),
        capability_revision_repository=PostgresWebCapabilityRevisionRepository(artifact_reader=artifact_store),
        reader=repository,
        inventory=PostgresH5EvidenceInventoryRepository(),
        publication_writer=PostgresH5InspectionPublicationRepository(
            max_attempts=settings.inspection_work_items.max_attempts,
            retry_delay_seconds=max(
                1,
                int(settings.inspection_work_items.retry_base_seconds),
            ),
        ),
        wakeup=wakeup,
        manifest_finalizer=WebPageManifestFinalizer(
            repository=manifest_repository,
            finalizer=AppEvidenceManifestFinalizer(
                repository=manifest_repository,
                artifact_store=artifact_store,
            ),
        ),
    )


def build_default_web_live_inspection_publish_service(
    *,
    settings: Any | None = None,
) -> WebLiveInspectionPublishService:
    """供 Judge Worker 的组合发布器补扫 Web Capture。"""

    if settings is None:
        from services.api.app.core.config import get_settings

        settings = get_settings()
    from services.api.app.infrastructure.db.web_sequence_pending_publication import (
        PostgresWebSequencePendingPublisher,
    )

    return WebLiveInspectionPublishService(
        source_run_reader=PostgresWebLiveInspectionSourceRunReader(),
        publisher=build_default_h5_live_inspection_publish_service(),
        sequence_publisher=PostgresWebSequencePendingPublisher(),
    )


__all__ = [
    "build_default_web_live_inspection_publish_service",
    "build_default_web_page_inspection_notification",
]
