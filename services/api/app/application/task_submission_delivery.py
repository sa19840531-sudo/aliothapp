"""任务提交事实到 Redis 短期队列投影的应用边界。"""

from __future__ import annotations

from typing import Protocol

from services.api.app.application.agent_run_queue_routing import AgentRunQueueRouter
from services.api.app.infrastructure.queue.redis_queue import AgentRunRecord, RedisAgentRunQueue


class TaskSubmissionDeliveryRepository(Protocol):
    async def load_pending_run(
        self,
        *,
        submission_id: str,
        task_id: str,
    ) -> AgentRunRecord | None: ...

    async def list_pending_runs(
        self,
        *,
        minimum_age_seconds: int,
        limit: int,
    ) -> tuple[AgentRunRecord, ...]: ...


class TaskSubmissionProjectionService:
    """Redis 只投影 PostgreSQL 已提交的同一 Run，不生成新身份。"""

    def __init__(
        self,
        *,
        repository: TaskSubmissionDeliveryRepository,
        queue: RedisAgentRunQueue,
        web_queue: RedisAgentRunQueue | None = None,
    ) -> None:
        self._repository = repository
        self._queue = queue
        self._queue_router = AgentRunQueueRouter(
            default_queue=queue,
            web_queue=web_queue,
        )

    async def project(self, *, submission_id: str, task_id: str) -> bool:
        record = await self._repository.load_pending_run(
            submission_id=submission_id,
            task_id=task_id,
        )
        if record is None:
            # Run 已开始或终结时，旧唤醒已完成使命，可以安全 ACK。
            return True
        await self._queue_router.for_record(record).restore_queued(record)
        return True

    async def reconcile(self, *, minimum_age_seconds: int = 10, limit: int = 100) -> int:
        if minimum_age_seconds <= 0 or limit <= 0:
            raise ValueError("任务提交对账参数必须大于 0")
        records = await self._repository.list_pending_runs(
            minimum_age_seconds=minimum_age_seconds,
            limit=limit,
        )
        restored = 0
        for record in records:
            restored += int(
                await self._queue_router.for_record(record).restore_queued(record)
            )
        return restored

    def _queue_for(self, record: AgentRunRecord) -> RedisAgentRunQueue:
        target = (record.task_config or {}).get("_task_target")
        is_web = isinstance(target, dict) and str(target.get("platform") or "").casefold() == "web"
        if not is_web:
            return self._queue
        if self._web_queue is None:
            raise RuntimeError("Web Run 投影缺少专用 Redis 队列")
        return self._web_queue
