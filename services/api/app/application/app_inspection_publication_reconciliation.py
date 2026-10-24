"""恢复 App 封存证据与正式质检 Publication 之间的持久化交接。"""

from __future__ import annotations

import logging
from typing import Protocol

from services.api.app.application.app_inspection_publication import AppUnpublishedSealedIndex

logger = logging.getLogger(__name__)


class _UnpublishedIndexReader(Protocol):
    async def list_unpublished_sealed_indexes(
        self, *, limit: int = 100
    ) -> tuple[AppUnpublishedSealedIndex, ...]: ...


class _SealedIndexPublisher(Protocol):
    async def publish(
        self,
        *,
        task_id: str,
        agent_run_id: str,
        expected_finalization_epoch: int | None = None,
    ) -> object: ...


class AppInspectionPublicationReconciler:
    """复用正式封存校验和幂等发布，禁止重跑仍在运行的主 Run。"""

    def __init__(
        self,
        *,
        repository: _UnpublishedIndexReader,
        publisher: _SealedIndexPublisher,
    ) -> None:
        self._repository = repository
        self._publisher = publisher

    async def reconcile(self, *, limit: int = 100) -> int:
        candidates = await self._repository.list_unpublished_sealed_indexes(limit=limit)
        recovered = 0
        for candidate in candidates:
            try:
                await self._publisher.publish(
                    task_id=candidate.task_id,
                    agent_run_id=candidate.source_agent_run_id,
                    expected_finalization_epoch=candidate.finalization_epoch,
                )
            except Exception:  # noqa: BLE001 - 单任务归属变化或证据异常不能饿死其他任务
                logger.warning(
                    "task_id=%s run_id=%s 恢复 App sealed Index 质检交接失败",
                    candidate.task_id,
                    candidate.source_agent_run_id,
                    exc_info=True,
                )
                continue
            recovered += 1
        return recovered
