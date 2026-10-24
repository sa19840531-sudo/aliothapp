"""Inspection Stream 唤醒的数据库权威消费处理器。"""

from __future__ import annotations

import json
import logging

from services.api.app.agents.runtime.inspection_work_item_consumer import (
    InspectionJudgeDispatcher,
    LiveInspectionPublisherPort,
)
from services.api.app.application.inspection_work_items import (
    InspectionWorkItemService,
)
from services.api.app.application.live_inspection_publication import (
    LiveInspectionPublicationBatchResult,
)
from services.api.app.application.outbox import INSPECTION_WORK_AVAILABLE_MESSAGE_TYPE
from services.api.app.infrastructure.queue.redis_stream import (
    RedisStreamWakeup,
    WakeupRoutingError,
)

logger = logging.getLogger(__name__)

EVIDENCE_SUPPLEMENT_ATTACHED_MESSAGE_TYPE = "alioth.v2.evidence-supplement.attached"
CAPTURE_COMMITTED_MESSAGE_TYPE = "alioth.v2.capture.committed"
EVIDENCE_BUNDLE_READY_MESSAGE_TYPE = "alioth.v2.evidence-bundle.ready"

INSPECTION_STREAM_MESSAGE_TYPES = frozenset(
    {
        INSPECTION_WORK_AVAILABLE_MESSAGE_TYPE,
        EVIDENCE_SUPPLEMENT_ATTACHED_MESSAGE_TYPE,
        CAPTURE_COMMITTED_MESSAGE_TYPE,
        EVIDENCE_BUNDLE_READY_MESSAGE_TYPE,
    }
)

_LIVE_PUBLISH_MESSAGE_TYPES = frozenset(
    {
        CAPTURE_COMMITTED_MESSAGE_TYPE,
        EVIDENCE_BUNDLE_READY_MESSAGE_TYPE,
    }
)


class CompositeLiveInspectionPublisher:
    """按平台门禁依次调用多个增量发布器，并汇总新建工作项数量。"""

    def __init__(self, *publishers: LiveInspectionPublisherPort) -> None:
        if not publishers:
            raise ValueError("增量发布组合至少需要一个发布器")
        self._publishers = publishers

    async def publish_pending(
        self,
        *,
        task_id: str,
        agent_run_id: str,
        limit: None = None,
    ) -> int:
        if limit is not None:
            raise ValueError("有界发布必须调用 publish_pending_batch")
        published = 0
        for publisher in self._publishers:
            published += await publisher.publish_pending(
                task_id=task_id,
                agent_run_id=agent_run_id,
            )
        return published

    async def publish_pending_batch(
        self,
        *,
        task_id: str,
        agent_run_id: str,
        limit: int,
        cursor: str | None,
    ) -> LiveInspectionPublicationBatchResult:
        """在所有平台发布器之间共享尝试预算，并用组合 cursor 续扫。"""

        if limit <= 0:
            raise ValueError("limit 必须大于 0")
        publisher_index, child_cursor = _decode_composite_cursor(cursor)
        attempted = 0
        created = 0
        while publisher_index < len(self._publishers) and attempted < limit:
            remaining = limit - attempted
            child_result = await self._publishers[
                publisher_index
            ].publish_pending_batch(
                task_id=task_id,
                agent_run_id=agent_run_id,
                limit=remaining,
                cursor=child_cursor,
            )
            child_result = LiveInspectionPublicationBatchResult(
                attempted_count=int(child_result.attempted_count),
                created_count=int(child_result.created_count),
                has_more=bool(child_result.has_more),
                next_cursor=child_result.next_cursor,
            )
            if child_result.attempted_count > remaining:
                raise RuntimeError("平台发布器尝试数量超过本轮剩余预算")
            attempted += child_result.attempted_count
            created += child_result.created_count
            if child_result.has_more:
                return LiveInspectionPublicationBatchResult(
                    attempted_count=attempted,
                    created_count=created,
                    has_more=True,
                    next_cursor=_encode_composite_cursor(
                        publisher_index,
                        child_result.next_cursor,
                    ),
                )
            publisher_index += 1
            child_cursor = None
        has_more = publisher_index < len(self._publishers)
        return LiveInspectionPublicationBatchResult(
            attempted_count=attempted,
            created_count=created,
            has_more=has_more,
            next_cursor=(
                _encode_composite_cursor(publisher_index, None) if has_more else None
            ),
        )


def _encode_composite_cursor(publisher_index: int, child_cursor: str | None) -> str:
    return json.dumps(
        {"publisher_index": publisher_index, "child_cursor": child_cursor},
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )


def _decode_composite_cursor(cursor: str | None) -> tuple[int, str | None]:
    if cursor is None:
        return 0, None
    try:
        payload = json.loads(cursor)
        publisher_index = int(payload["publisher_index"])
        child_cursor = payload.get("child_cursor")
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("publication cursor 格式无效") from exc
    if publisher_index < 0:
        raise ValueError("publication publisher_index 格式无效")
    if child_cursor is not None and not isinstance(child_cursor, str):
        raise ValueError("publication child cursor 格式无效")
    return publisher_index, child_cursor


class InspectionWakeupHandler:
    """确认 inspection 唤醒并提交 task/run 身份；Judge 完成不属于 ACK 语义。"""

    def __init__(
        self,
        *,
        service: InspectionWorkItemService,
        consumer: InspectionJudgeDispatcher,
    ) -> None:
        self._service = service
        self._dispatcher = consumer

    async def handle(self, wakeup: RedisStreamWakeup) -> bool:
        if wakeup.message_type not in INSPECTION_STREAM_MESSAGE_TYPES:
            raise WakeupRoutingError("Inspection Handler 不处理该消息类型")
        if wakeup.message_type == EVIDENCE_SUPPLEMENT_ATTACHED_MESSAGE_TYPE:
            reopened = await self._service.reopen_waiting_evidence(task_id=wakeup.task_id)
            if reopened:
                logger.info(
                    "task_id=%s 补采唤醒重开 %d 个 waiting_evidence 工作项。",
                    wakeup.task_id,
                    reopened,
                )
        consumer_run_id = await self._service.find_active_consumer_run(task_id=wakeup.task_id)
        if consumer_run_id is None:
            # 任务已没有运行中的消费 Run：claim 窗口关闭，收口门禁是权威兜底，唤醒可安全确认。
            return True
        accepted = await self._dispatcher.submit(
            task_id=wakeup.task_id,
            consumer_agent_run_id=consumer_run_id,
            prepare_publication=wakeup.message_type in _LIVE_PUBLISH_MESSAGE_TYPES,
        )
        return accepted
