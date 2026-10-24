"""按锁定业务平台选择 Agent Run 等待队列。"""

from __future__ import annotations

from collections.abc import Mapping

from services.api.app.domain.task import TaskTarget, TaskTargetPlatform
from services.api.app.infrastructure.queue.redis_queue import (
    AgentRunRecord,
    RedisAgentRunQueue,
)


class WebAgentRunQueueNotConfiguredError(RuntimeError):
    """Web Run 没有配置专属等待队列。"""


class AgentRunQueueRouter:
    """只根据持久化平台事实选队列，不根据 URL 或 Surface 推断。"""

    def __init__(
        self,
        *,
        default_queue: RedisAgentRunQueue,
        web_queue: RedisAgentRunQueue | None = None,
    ) -> None:
        self._default_queue = default_queue
        self._web_queue = web_queue

    def for_target(self, target: TaskTarget) -> RedisAgentRunQueue:
        if target.platform is TaskTargetPlatform.WEB:
            return self._require_web_queue()
        return self._default_queue

    def for_record(self, record: AgentRunRecord) -> RedisAgentRunQueue:
        target_payload = record.task_config.get("_task_target")
        if not isinstance(target_payload, Mapping):
            return self._default_queue
        if target_payload.get("platform") == TaskTargetPlatform.WEB.value:
            return self._require_web_queue()
        return self._default_queue

    def _require_web_queue(self) -> RedisAgentRunQueue:
        if self._web_queue is None:
            raise WebAgentRunQueueNotConfiguredError(
                "WEB_QUEUE_NOT_CONFIGURED: Web Agent Run 专属队列未配置"
            )
        return self._web_queue


__all__ = [
    "AgentRunQueueRouter",
    "WebAgentRunQueueNotConfiguredError",
]
