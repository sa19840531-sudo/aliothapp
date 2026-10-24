"""H5 Publication 专用唤醒处理器。"""

from __future__ import annotations

from typing import Protocol

from services.api.app.application.h5_inspection_publication import (
    H5InspectionPublicationClaimDisposition,
    H5InspectionPublicationRepository,
)
from services.api.app.domain.inspection import (
    H5_INSPECTION_PUBLICATION_AVAILABLE_MESSAGE_TYPE,
)
from services.api.app.infrastructure.queue.redis_stream import (
    RedisStreamWakeup,
    WakeupRoutingError,
)


class H5CaptureInspectionPublisher(Protocol):
    """复用共享页面规则匹配与 Work Item 发布能力。"""

    async def publish_for_capture(self, **kwargs: object) -> object: ...


class H5InspectionPublicationWakeupHandler:
    """按 Publication 租约发布共享质检工作；成功写回后才允许 ACK。"""

    def __init__(
        self,
        *,
        repository: H5InspectionPublicationRepository,
        publisher: H5CaptureInspectionPublisher,
        lease_owner: str,
        lease_seconds: int,
    ) -> None:
        if not str(lease_owner or "").strip():
            raise ValueError("lease_owner 不能为空")
        if lease_seconds <= 0:
            raise ValueError("lease_seconds 必须大于 0")
        self._repository = repository
        self._publisher = publisher
        self._lease_owner = lease_owner
        self._lease_seconds = lease_seconds

    async def handle(self, wakeup: RedisStreamWakeup) -> bool:
        if wakeup.message_type != H5_INSPECTION_PUBLICATION_AVAILABLE_MESSAGE_TYPE:
            raise WakeupRoutingError("H5 Publication Handler 不处理该消息类型")
        if wakeup.aggregate_type != "h5_inspection_publication":
            raise WakeupRoutingError("H5 Publication 聚合类型不匹配")
        claim = await self._repository.claim(
            publication_id=wakeup.aggregate_id,
            task_id=wakeup.task_id,
            message_version=wakeup.aggregate_version,
            lease_owner=self._lease_owner,
            lease_seconds=self._lease_seconds,
        )
        if claim.disposition in {
            H5InspectionPublicationClaimDisposition.COMPLETED,
            H5InspectionPublicationClaimDisposition.STALE,
        }:
            return True
        if claim.disposition is H5InspectionPublicationClaimDisposition.BUSY:
            return False
        lease = claim.lease
        if lease is None:
            raise RuntimeError("Publication claim 缺少租约")
        try:
            if lease.web_evidence_sequence_id is not None:
                await self._repository.process_web_sequence(lease=lease)
                return True
            result = await self._publisher.publish_for_capture(
                task_id=lease.task_id,
                capture_id=lease.page_capture_id,
                visual_page_type=lease.page_type,
                page_state=lease.page_state,
                agent_run_id=lease.source_agent_run_id,
                snapshot_only=True,
                prepare_only=True,
            )
            await self._repository.complete(lease=lease, result=result)
        except Exception as exc:
            await self._repository.retry(lease=lease, error=exc)
            raise
        return True
