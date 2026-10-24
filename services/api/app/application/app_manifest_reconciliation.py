"""App PENDING Evidence Manifest 的持久化 backlog 对账。"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from services.api.app.application.app_evidence_manifest import (
    AppEvidenceManifestFinalizer,
)
from services.api.app.application.app_metrics import (
    APP_MANIFEST_PENDING_AGE_SECONDS,
    APP_MANIFEST_WRITE_FAILURE_TOTAL,
    observe_app_metric_safely,
    record_app_metric_safely,
)
from services.api.app.domain.evidence import EvidenceManifestReference

logger = logging.getLogger(__name__)


class AppPendingManifestReader(Protocol):
    """从 PostgreSQL 查询最新超龄 PENDING Manifest。"""

    async def list_stale_pending(
        self,
        *,
        created_before: datetime,
        limit: int = 100,
    ) -> tuple[EvidenceManifestReference, ...]: ...


@dataclass(frozen=True, kw_only=True)
class AppManifestReconciliationSummary:
    """单轮 Manifest backlog 处理结果。"""

    finalized: int = 0
    failed: int = 0


class AppManifestBacklogReconciler:
    """让 Redis 丢唤醒或 Worker 中断只增加 Manifest 晋级延迟。"""

    def __init__(
        self,
        *,
        manifests: AppPendingManifestReader,
        finalizer: AppEvidenceManifestFinalizer,
        stale_after_seconds: int = 120,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if (
            isinstance(stale_after_seconds, bool)
            or not isinstance(stale_after_seconds, int)
            or stale_after_seconds < 1
        ):
            raise ValueError("stale_after_seconds 必须为正整数")
        self._manifests = manifests
        self._finalizer = finalizer
        self._stale_after = timedelta(seconds=stale_after_seconds)
        self._now = now or (lambda: datetime.now(UTC))

    async def reconcile_once(self, *, limit: int = 100) -> AppManifestReconciliationSummary:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise ValueError("limit 必须在 1 到 1000 之间")
        current = self._now()
        pending = await self._manifests.list_stale_pending(
            created_before=current - self._stale_after,
            limit=limit,
        )
        finalized = 0
        failed = 0
        for manifest in pending:
            age_seconds = max(0.0, (current - manifest.created_at).total_seconds())
            observe_app_metric_safely(
                APP_MANIFEST_PENDING_AGE_SECONDS,
                age_seconds,
                phase="backlog",
            )
            try:
                await self._finalizer.finalize(manifest)
                finalized += 1
            except Exception as exc:  # noqa: BLE001 - 单个坏对象不能阻断其他 Capture
                failed += 1
                record_app_metric_safely(
                    APP_MANIFEST_WRITE_FAILURE_TOTAL,
                    phase="backlog_finalize",
                    error_type=type(exc).__name__,
                )
                logger.exception(
                    "App PENDING Manifest backlog 晋级失败",
                    extra={
                        "task_id": manifest.task_id,
                        "capture_id": manifest.capture_id,
                        "manifest_id": manifest.manifest_id,
                    },
                )
        return AppManifestReconciliationSummary(finalized=finalized, failed=failed)
