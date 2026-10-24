"""Web 活会话质检发布的主 Run 身份适配。"""

from __future__ import annotations

from typing import Protocol

from services.api.app.application.live_inspection_publication import (
    LiveInspectionPublicationBatchResult,
)


class WebLiveInspectionSourceRunReader(Protocol):
    """读取当前 Web V2 Session 的主探索 Run。"""

    async def find_source_agent_run_id(self, *, task_id: str) -> str | None: ...


class WebPendingInspectionPublisher(Protocol):
    """按主探索 Run 补发待处理 Web Capture。"""

    async def publish_pending(
        self,
        *,
        task_id: str,
        agent_run_id: str,
        limit: None = None,
    ) -> int: ...

    async def publish_pending_batch(
        self,
        *,
        task_id: str,
        agent_run_id: str,
        limit: int,
        cursor: str | None,
    ) -> LiveInspectionPublicationBatchResult: ...


class WebLiveInspectionPublishService:
    """把通用质检消费 Run 映射回 Web Capture 所属的主探索 Run。"""

    def __init__(
        self,
        *,
        source_run_reader: WebLiveInspectionSourceRunReader,
        publisher: WebPendingInspectionPublisher,
        sequence_publisher=None,
    ) -> None:
        self._source_run_reader = source_run_reader
        self._publisher = publisher
        self._sequence_publisher = sequence_publisher

    async def publish_pending(
        self,
        *,
        task_id: str,
        agent_run_id: str,
        limit: None = None,
    ) -> int:
        if limit is not None:
            raise ValueError("有界发布必须调用 publish_pending_batch")
        task = str(task_id or "").strip()
        consumer_run_id = str(agent_run_id or "").strip()
        if not task or not consumer_run_id:
            raise ValueError("task_id 和 agent_run_id 不能为空")
        sequence_created = 0
        if self._sequence_publisher is not None:
            sequence_cursor = None
            while True:
                sequence_batch = await self._sequence_publisher.publish_pending_batch(task_id=task,limit=100,cursor=sequence_cursor)
                if sequence_batch is None:
                    break
                sequence_created += sequence_batch.created_count
                sequence_cursor = sequence_batch.next_cursor
        source_run_id = await self._source_run_reader.find_source_agent_run_id(
            task_id=task
        )
        if source_run_id is None:
            return sequence_created
        return sequence_created + await self._publisher.publish_pending(
            task_id=task,
            agent_run_id=source_run_id,
        )

    async def publish_pending_batch(
        self,
        *,
        task_id: str,
        agent_run_id: str,
        limit: int,
        cursor: str | None,
    ) -> LiveInspectionPublicationBatchResult:
        """把消费者 Run 映射到主 Run，并原样传递稳定批次进度。"""

        task = str(task_id or "").strip()
        consumer_run_id = str(agent_run_id or "").strip()
        if not task or not consumer_run_id:
            raise ValueError("task_id 和 agent_run_id 不能为空")
        if limit <= 0:
            raise ValueError("limit 必须大于 0")
        if self._sequence_publisher is not None:
            sequence_batch = await self._sequence_publisher.publish_pending_batch(task_id=task,limit=limit,cursor=cursor)
            if sequence_batch is not None:
                return sequence_batch
            if cursor and cursor.startswith('web-sequence:'):
                cursor = None
        source_run_id = await self._source_run_reader.find_source_agent_run_id(
            task_id=task
        )
        if source_run_id is None:
            return LiveInspectionPublicationBatchResult(0, 0, False)
        return await self._publisher.publish_pending_batch(
            task_id=task,
            agent_run_id=source_run_id,
            limit=limit,
            cursor=cursor,
        )


__all__ = [
    "WebLiveInspectionPublishService",
    "WebLiveInspectionSourceRunReader",
    "WebPendingInspectionPublisher",
]
