"""基于 Redis 的 Agent Run 任务队列：解耦"提交"与"执行"，支撑独立 Worker 进程消费。

Redis 负责短期分发、运行中查询、结果快照和取消信号，记录带 TTL 自然过期，不作为
长期审计存储。绑定业务任务的长期运行状态由 ``state_store.py`` 按配置写入
``qc_agent_run`` / ``qc_agent_action_trace``。

并发/一致性说明：``request_cancel`` 采取"先读记录判断是否可取消、再写取消标记"的
简单实现，未使用 Redis 事务/分布式锁；对于"取消一次尽力而为的后台任务"这一场景，
偶发的竞态（例如恰好在判断瞬间任务从 RUNNING 转为 COMPLETED）后果只是取消请求
被静默忽略，可接受，不值得为此引入额外复杂度。
"""

import json
import uuid
from datetime import UTC, datetime
from enum import StrEnum

import redis.asyncio as redis_asyncio
from pydantic import BaseModel, Field
from redis.backoff import ExponentialBackoff
from redis.exceptions import WatchError
from redis.retry import Retry

from services.api.app.core.config import QueueSettings
from services.api.app.domain.exploration.web_runtime_contract import (
    WEB_LEGACY_RUN_EXECUTION_NOT_SUPPORTED,
    is_legacy_web_execution,
)

DEFAULT_AGENT_RUN_QUEUE_KEY = "alioth:agent-runs:queue"
_RECORD_KEY_TEMPLATE = "alioth:agent-runs:{run_id}"
_CANCEL_KEY_TEMPLATE = "alioth:agent-runs:{run_id}:cancel"
_WORKER_HEARTBEAT_KEY_PREFIX = "alioth:agent-runs:worker-heartbeat:"

DEFAULT_WORKER_HEARTBEAT_TTL_SECONDS = 30
"""心跳键 TTL：Worker 进程每 ~10s 刷新一次；键过期即视为该实例离线。"""


class AgentRunQueueStatus(StrEnum):
    """Agent Run 任务在队列生命周期内的状态。"""

    QUEUED = "queued"
    """已入队，等待 Worker 取出执行。"""

    RUNNING = "running"
    """已被某个 Worker 取出，Agent Loop 正在执行。"""

    COMPLETED = "completed"
    """Agent Loop 明确以 completed 正常结束。"""

    FAILED = "failed"
    """Agent Loop 未完成目标，或 Worker 处理任务时出现异常。"""

    CANCELED = "canceled"
    """用户请求取消且 Agent Loop 确认以取消状态终止。"""


class AgentRunRecord(BaseModel):
    """一次 Agent Run 任务的完整状态快照，序列化为 JSON 存入 Redis。"""

    run_id: str
    task_id: str | None = None
    submitted_by_user_id: str | None = None
    # 由服务端登录态投影的租户；历史 Redis/PG 快照可能没有该字段，因此保持可选。
    tenant_id: str | None = None
    message: str
    task_config: dict = Field(default_factory=dict)
    page_sampling_policy: dict = Field(default_factory=dict)
    loop_guard: dict = Field(default_factory=dict)
    status: AgentRunQueueStatus = AgentRunQueueStatus.QUEUED
    submitted_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    termination_reason: str | None = None
    final_text: str | None = None
    step_count: int | None = None
    error_message: str | None = None
    error_category: str | None = None
    execution_outcome: str | None = None
    evidence_index_id: str | None = None
    exploration_status: str | None = None
    evidence_gap: list[str] = Field(default_factory=list)
    page_timeline: list[str] = Field(default_factory=list)
    web_exploration_summary: dict[str, object] | None = None
    usage: dict[str, object] | None = None
    action_trace: list[dict] = Field(default_factory=list)
    working_memory: dict = Field(default_factory=dict)
    final_report: dict | None = None
    dispatch_attempt_count: int = 0
    worker_instance_id: str | None = None
    worker_missing_since: datetime | None = None
    cancellation_cleanup_completed: bool = False
    cancellation_finalized: bool = False


def build_redis_client(settings: QueueSettings) -> redis_asyncio.Redis:
    """构建支持短暂断连自动恢复的异步 Redis 客户端。"""
    return redis_asyncio.from_url(
        settings.redis_url,
        decode_responses=True,
        health_check_interval=10,
        socket_keepalive=True,
        retry=Retry(
            ExponentialBackoff(cap=1.0, base=0.1),
            retries=3,
        ),
    )


class RedisAgentRunQueue:
    """封装 Agent Run 任务的入队/出队/状态读写/取消请求，基于 ``redis.asyncio`` 实现。"""

    def __init__(
        self,
        client: redis_asyncio.Redis,
        *,
        ttl_seconds: int,
        queue_key: str = DEFAULT_AGENT_RUN_QUEUE_KEY,
    ) -> None:
        if not isinstance(queue_key, str) or not queue_key.strip():
            raise ValueError("queue_key 不能为空")
        self._client = client
        self._ttl_seconds = ttl_seconds
        self._queue_key = queue_key.strip()

    async def enqueue(
        self,
        message: str,
        *,
        task_id: str | None = None,
        submitted_by_user_id: str | None = None,
        tenant_id: str | None = None,
        task_config: dict | None = None,
        page_sampling_policy: dict | None = None,
        loop_guard: dict | None = None,
    ) -> AgentRunRecord:
        """创建一条新的 Run 记录并推入队列，返回记录本身（含生成的 run_id）。"""
        record = AgentRunRecord(
            run_id=str(uuid.uuid4()),
            task_id=task_id,
            submitted_by_user_id=submitted_by_user_id,
            tenant_id=tenant_id,
            message=message,
            task_config=dict(task_config or {}),
            page_sampling_policy=dict(page_sampling_policy or {}),
            loop_guard=dict(loop_guard or {}),
            submitted_at=datetime.now(UTC),
        )
        await self.save(record)
        await self._client.rpush(self._queue_key, record.run_id)
        return record

    async def dequeue(self, *, timeout: float) -> str | None:
        """阻塞式取出一个待处理的 run_id；超时未取到返回 None。"""
        result = await self._client.blpop([self._queue_key], timeout=timeout)
        if result is None:
            return None
        _key, run_id = result
        return run_id

    async def claim_running(
        self,
        record: AgentRunRecord,
        *,
        worker_instance_id: str | None,
    ) -> bool:
        """以 Redis CAS 原子领取 queued 投影，抑制重复唤醒并发启动。"""

        record_key = _RECORD_KEY_TEMPLATE.format(run_id=record.run_id)
        for _attempt in range(5):
            async with self._client.pipeline(transaction=True) as pipeline:
                try:
                    await pipeline.watch(record_key)
                    existing_raw = await pipeline.get(record_key)
                    if existing_raw is None:
                        await pipeline.reset()
                        return False
                    existing = AgentRunRecord.model_validate_json(existing_raw)
                    if (
                        existing.status in {AgentRunQueueStatus.QUEUED, AgentRunQueueStatus.RUNNING}
                        and is_legacy_web_execution(existing.task_config)
                    ):
                        existing.status = AgentRunQueueStatus.FAILED
                        existing.finished_at = datetime.now(UTC)
                        existing.termination_reason = "error"
                        existing.error_message = WEB_LEGACY_RUN_EXECUTION_NOT_SUPPORTED
                        existing.error_category = "infrastructure"
                        existing.exploration_status = "failed"
                        existing.evidence_gap = [WEB_LEGACY_RUN_EXECUTION_NOT_SUPPORTED]
                        existing.worker_instance_id = None
                        pipeline.multi()
                        pipeline.set(record_key, existing.model_dump_json(), ex=self._ttl_seconds)
                        await pipeline.execute()
                        record.__dict__.update(existing.__dict__)
                        return False
                    if existing.status is not AgentRunQueueStatus.QUEUED:
                        await pipeline.reset()
                        return False
                    claimed = existing.model_copy(deep=True)
                    claimed.status = AgentRunQueueStatus.RUNNING
                    claimed.started_at = (
                        existing.started_at or record.started_at or datetime.now(UTC)
                    )
                    claimed.worker_instance_id = worker_instance_id
                    claimed.worker_missing_since = None
                    claimed.dispatch_attempt_count += 1
                    pipeline.multi()
                    pipeline.set(
                        record_key,
                        claimed.model_dump_json(),
                        ex=self._ttl_seconds,
                    )
                    await pipeline.execute()
                    record.__dict__.update(claimed.__dict__)
                    return True
                except WatchError:
                    continue
        raise RuntimeError("领取 Agent Run 时发生持续并发冲突")

    async def ensure_enqueued(self, run_id: str) -> bool:
        """幂等确认 queued Run 位于等待队列；不存在或非 queued 时返回 False。"""
        record = await self.get(run_id)
        if record is None or record.status != AgentRunQueueStatus.QUEUED:
            return False
        return await self._client.lpos(self._queue_key, run_id) is not None

    async def restore_queued(
        self,
        record: AgentRunRecord,
        *,
        expected_running_worker_instance_id: str | None = None,
    ) -> bool:
        """把 PostgreSQL 恢复出的同一 Run 幂等写回 Redis 队列。

        返回 True 表示本次补入了队列；已有 queued 投影只刷新记录且不重复 RPUSH。
        终态投影不得复活，running 投影也只有孤儿对账确认失联后才能显式替换。
        """

        if record.status is not AgentRunQueueStatus.QUEUED:
            raise ValueError("恢复入队只接受 queued AgentRunRecord")
        record_key = _RECORD_KEY_TEMPLATE.format(run_id=record.run_id)
        for _attempt in range(5):
            async with self._client.pipeline(transaction=True) as pipeline:
                try:
                    await pipeline.watch(record_key, self._queue_key)
                    existing_raw = await pipeline.get(record_key)
                    existing = (
                        AgentRunRecord.model_validate_json(existing_raw)
                        if existing_raw is not None
                        else None
                    )
                    if existing is not None and existing.status in {
                        AgentRunQueueStatus.COMPLETED,
                        AgentRunQueueStatus.FAILED,
                        AgentRunQueueStatus.CANCELED,
                    }:
                        await pipeline.reset()
                        return False
                    if existing is not None and existing.status is AgentRunQueueStatus.RUNNING:
                        # 孤儿对账只允许替换自己刚确认失联的旧 Worker；若另一个
                        # Worker 已在扫描后接管，WATCH 内看到的新 owner 必须保留。
                        if (
                            expected_running_worker_instance_id is None
                            or existing.worker_instance_id != expected_running_worker_instance_id
                        ):
                            await pipeline.reset()
                            return False
                    enqueued = await pipeline.lpos(self._queue_key, record.run_id) is not None
                    pipeline.multi()
                    pipeline.set(record_key, record.model_dump_json(), ex=self._ttl_seconds)
                    if not enqueued:
                        pipeline.rpush(self._queue_key, record.run_id)
                    await pipeline.execute()
                    return not enqueued
                except WatchError:
                    continue
        raise RuntimeError("恢复 App Run 到 Redis 队列时发生持续并发冲突")

    async def cancel_queued(self, run_id: str) -> AgentRunRecord | None:
        """从等待队列移除 queued Run，并立即写成取消终态。"""
        record = await self.get(run_id)
        if record is None or record.status != AgentRunQueueStatus.QUEUED:
            return None
        removed = await self._client.lrem(self._queue_key, 0, run_id)
        if removed == 0:
            return None
        record.status = AgentRunQueueStatus.CANCELED
        record.finished_at = datetime.now(UTC)
        record.termination_reason = "canceled"
        record.cancellation_cleanup_completed = True
        record.cancellation_finalized = False
        await self.save(record)
        return record

    async def get(self, run_id: str) -> AgentRunRecord | None:
        """按 run_id 查询当前记录；找不到（不存在或已过期）返回 None。"""
        raw = await self._client.get(_RECORD_KEY_TEMPLATE.format(run_id=run_id))
        if raw is None:
            return None
        return AgentRunRecord.model_validate_json(raw)

    async def save(self, record: AgentRunRecord) -> None:
        """写入/覆盖一条记录，带 TTL（进程/Redis 重启后旧记录自然过期，不做长期存储）。"""
        await self._client.set(
            _RECORD_KEY_TEMPLATE.format(run_id=record.run_id),
            record.model_dump_json(),
            ex=self._ttl_seconds,
        )

    async def requeue_after_dispatch_failure(
        self,
        record: AgentRunRecord,
        *,
        error_message: str,
        max_attempts: int = 3,
    ) -> bool:
        """Run 尚未真正启动时恢复出队记录；超过上限后写入失败终态。"""
        record.error_message = error_message
        record.error_category = "infrastructure"
        record.worker_instance_id = None
        record.worker_missing_since = None
        if record.dispatch_attempt_count >= max_attempts:
            record.status = AgentRunQueueStatus.FAILED
            record.finished_at = datetime.now(UTC)
            record.termination_reason = "error"
            await self.save(record)
            return False

        record.status = AgentRunQueueStatus.QUEUED
        record.started_at = None
        record.finished_at = None
        record.termination_reason = None
        await self.save(record)
        await self._client.rpush(self._queue_key, record.run_id)
        return True

    async def requeue_after_cancellation_check_unavailable(
        self,
        record: AgentRunRecord,
        *,
        error_message: str,
    ) -> None:
        """长期取消事实未知时 fail closed，且不消耗启动失败终态预算。"""

        record.status = AgentRunQueueStatus.QUEUED
        record.started_at = None
        record.finished_at = None
        record.termination_reason = None
        record.error_message = error_message
        record.error_category = "infrastructure"
        record.worker_instance_id = None
        record.worker_missing_since = None
        await self.save(record)
        await self._client.rpush(self._queue_key, record.run_id)

    async def list_records(self) -> list[AgentRunRecord]:
        """列出当前 Redis 中仍在 TTL 内的 Run 快照，不包含队列和心跳键。"""
        records: list[AgentRunRecord] = []
        prefix = _RECORD_KEY_TEMPLATE.split("{run_id}", 1)[0]
        async for raw_key in self._client.scan_iter(match=f"{prefix}*"):
            key = str(raw_key)
            if key == self._queue_key:
                continue
            run_id = key[len(prefix) :]
            try:
                uuid.UUID(run_id)
            except ValueError:
                continue
            record = await self.get(run_id)
            if record is not None:
                records.append(record)
        return records

    async def is_worker_instance_alive(self, instance_id: str | None) -> bool:
        """判断领取 Run 的 Worker 实例心跳是否仍存在。"""
        if not instance_id:
            return False
        return bool(await self._client.exists(f"{_WORKER_HEARTBEAT_KEY_PREFIX}{instance_id}"))

    async def cancel_orphaned_running(self, run_id: str) -> AgentRunRecord | None:
        """立即终结已无存活 Worker 持有的 running Run。"""
        record = await self.get(run_id)
        if record is None or record.status != AgentRunQueueStatus.RUNNING:
            return None
        if await self.is_worker_instance_alive(record.worker_instance_id):
            return None
        record.status = AgentRunQueueStatus.CANCELED
        record.finished_at = datetime.now(UTC)
        record.termination_reason = "canceled"
        record.worker_instance_id = None
        record.worker_missing_since = None
        record.cancellation_cleanup_completed = False
        record.cancellation_finalized = False
        await self.save(record)
        await self._client.delete(_CANCEL_KEY_TEMPLATE.format(run_id=run_id))
        return record

    async def request_cancel(self, run_id: str) -> bool:
        """请求取消一个 Run；找不到记录或已处于终态时返回 False（无法取消）。"""
        record = await self.get(run_id)
        if record is None:
            return False
        if record.status not in (AgentRunQueueStatus.QUEUED, AgentRunQueueStatus.RUNNING):
            return False
        await self._client.set(
            _CANCEL_KEY_TEMPLATE.format(run_id=run_id), "1", ex=self._ttl_seconds
        )
        return True

    async def is_cancel_requested(self, run_id: str) -> bool:
        """查询某个 run_id 是否已被请求取消，供 ``CancellationCheckMiddleware`` 的回调使用。"""
        return bool(await self._client.exists(_CANCEL_KEY_TEMPLATE.format(run_id=run_id)))

    async def record_worker_heartbeat(
        self,
        instance_id: str,
        *,
        roles: frozenset[str] | set[str] | tuple[str, ...] = (),
        ttl_seconds: int = DEFAULT_WORKER_HEARTBEAT_TTL_SECONDS,
    ) -> None:
        """写入/续期 Worker 心跳和角色；键过期即代表该实例离线。"""
        payload = {
            "heartbeat_at": datetime.now(UTC).isoformat(),
            "roles": sorted({str(role).strip() for role in roles if str(role).strip()}),
        }
        await self._client.set(
            f"{_WORKER_HEARTBEAT_KEY_PREFIX}{instance_id}",
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            ex=ttl_seconds,
        )

    async def read_worker_heartbeats(self) -> list[dict]:
        """读取当前存活的 Worker 心跳；兼容升级前仅保存时间戳的旧值。"""
        keys = [
            key async for key in self._client.scan_iter(match=f"{_WORKER_HEARTBEAT_KEY_PREFIX}*")
        ]
        if not keys:
            return []
        values = await self._client.mget(keys)
        heartbeats: list[dict] = []
        for key, value in zip(keys, values, strict=True):
            if value is None:
                continue
            try:
                decoded = json.loads(str(value))
            except json.JSONDecodeError:
                decoded = str(value)
            try:
                if isinstance(decoded, dict):
                    heartbeat_at = datetime.fromisoformat(str(decoded.get("heartbeat_at") or ""))
                    raw_roles = decoded.get("roles")
                    roles = (
                        sorted({str(role).strip() for role in raw_roles if str(role).strip()})
                        if isinstance(raw_roles, list)
                        else []
                    )
                else:
                    heartbeat_at = datetime.fromisoformat(str(decoded))
                    roles = []
            except ValueError:
                continue
            heartbeats.append(
                {
                    "instance_id": str(key)[len(_WORKER_HEARTBEAT_KEY_PREFIX) :],
                    "heartbeat_at": heartbeat_at,
                    "roles": roles,
                }
            )
        return heartbeats

    async def queue_depth(self) -> int:
        """返回等待被消费的 run 数量（不含已被取走正在执行的）。"""
        return int(await self._client.llen(self._queue_key))

    async def queue_position(self, run_id: str) -> int | None:
        """返回指定 Run 在当前队列中的一基位置；不在队列时返回空。"""

        position = await self._client.lpos(self._queue_key, run_id)
        return int(position) + 1 if position is not None else None
