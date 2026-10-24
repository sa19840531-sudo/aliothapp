"""任务提交 Outbox 的 Redis 投影处理器。"""

from __future__ import annotations

from services.api.app.application.task_submission_delivery import (
    TaskSubmissionProjectionService,
)
from services.api.app.domain.task import TASK_SUBMISSION_COMMITTED_MESSAGE_TYPE
from services.api.app.infrastructure.queue.redis_stream import (
    RedisStreamWakeup,
    WakeupRoutingError,
)


class TaskSubmissionWakeupHandler:
    def __init__(self, *, service: TaskSubmissionProjectionService) -> None:
        self._service = service

    async def handle(self, wakeup: RedisStreamWakeup) -> bool:
        if wakeup.message_type != TASK_SUBMISSION_COMMITTED_MESSAGE_TYPE:
            raise WakeupRoutingError("任务提交 Handler 不处理该消息类型")
        if wakeup.aggregate_type != "task_submission" or wakeup.aggregate_version != 0:
            raise WakeupRoutingError("任务提交事实身份不匹配")
        return await self._service.project(
            submission_id=wakeup.aggregate_id,
            task_id=wakeup.task_id,
        )
