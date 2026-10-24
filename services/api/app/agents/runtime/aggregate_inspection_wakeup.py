"""EvidenceInputSet 聚合质检工作专用唤醒处理器。"""

from __future__ import annotations

from services.api.app.application.aggregate_inspection_execution import (
    AggregateInspectionExecutor,
)
from services.api.app.domain.inspection import (
    AGGREGATE_INSPECTION_WORK_AVAILABLE_MESSAGE_TYPE,
)
from services.api.app.infrastructure.queue.redis_stream import (
    RedisStreamWakeup,
    WakeupRoutingError,
)


class AggregateInspectionWakeupHandler:
    """Redis 身份只用于定位；Executor 必须回 PostgreSQL 领取。"""

    def __init__(self, *, executor: AggregateInspectionExecutor) -> None:
        self._executor = executor

    async def handle(self, wakeup: RedisStreamWakeup) -> bool:
        if wakeup.message_type != AGGREGATE_INSPECTION_WORK_AVAILABLE_MESSAGE_TYPE:
            raise WakeupRoutingError("聚合质检 Handler 不处理该消息类型")
        if wakeup.aggregate_type != "aggregate_inspection_work_item":
            raise WakeupRoutingError("聚合质检 Work 类型不匹配")
        return await self._executor.execute_wakeup(
            work_item_id=wakeup.aggregate_id,
            task_id=wakeup.task_id,
            message_version=wakeup.aggregate_version,
        )
