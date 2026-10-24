"""App sealed Index Publication 专用唤醒处理器。"""

from __future__ import annotations

from typing import Protocol

from services.api.app.application.app_inspection_publication import (
    AppInspectionPublicationClaimDisposition,
    AppInspectionPublicationRepository,
)
from services.api.app.domain.inspection.app_publication import (
    APP_INSPECTION_PUBLICATION_AVAILABLE_MESSAGE_TYPE,
)
from services.api.app.infrastructure.queue.redis_stream import (
    RedisStreamWakeup,
    WakeupRoutingError,
)


class AppCaptureInspectionPlanner(Protocol):
    """复用页面类型、锁定规则集与批次规划，但不直接写 Work。"""

    async def publish_for_capture(self, **kwargs: object) -> object: ...


class AppInspectionPublicationWakeupHandler:
    """整份 Index 全部规划并单事务完成后才允许 ACK。"""

    def __init__(
        self,
        *,
        repository: AppInspectionPublicationRepository,
        publisher: AppCaptureInspectionPlanner,
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
        if wakeup.message_type != APP_INSPECTION_PUBLICATION_AVAILABLE_MESSAGE_TYPE:
            raise WakeupRoutingError("App Publication Handler 不处理该消息类型")
        if wakeup.aggregate_type != "app_inspection_publication":
            raise WakeupRoutingError("App Publication 聚合类型不匹配")
        claim = await self._repository.claim(
            publication_id=wakeup.aggregate_id,
            task_id=wakeup.task_id,
            message_version=wakeup.aggregate_version,
            lease_owner=self._lease_owner,
            lease_seconds=self._lease_seconds,
        )
        if claim.disposition in {
            AppInspectionPublicationClaimDisposition.COMPLETED,
            AppInspectionPublicationClaimDisposition.STALE,
        }:
            return True
        if claim.disposition is AppInspectionPublicationClaimDisposition.BUSY:
            return False
        lease = claim.lease
        if lease is None:
            raise RuntimeError("App Publication claim 缺少租约")
        try:
            results = []
            for entry in lease.entries:
                results.append(
                    await self._publisher.publish_for_capture(
                        task_id=lease.task_id,
                        capture_id=entry.capture_id,
                        visual_page_type=entry.page_type,
                        page_state=entry.page_state,
                        agent_run_id=lease.source_agent_run_id,
                        snapshot_only=True,
                        prepare_only=True,
                    )
                )
            await self._repository.complete(lease=lease, results=tuple(results))
        except Exception as exc:
            await self._repository.retry(lease=lease, error=exc)
            raise
        return True
