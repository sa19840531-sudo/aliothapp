"""infrastructure/queue/redis_queue.py 的单元测试：使用 fakeredis 保证测试无需真实 Redis。"""

from datetime import UTC, datetime

import fakeredis
import pytest

from services.api.app.core.config import QueueSettings
from services.api.app.infrastructure.queue import redis_queue as redis_queue_module
from services.api.app.infrastructure.queue.redis_queue import (
    AgentRunQueueStatus,
    AgentRunRecord,
    RedisAgentRunQueue,
)


@pytest.fixture
def redis_client() -> fakeredis.aioredis.FakeRedis:
    return fakeredis.aioredis.FakeRedis(decode_responses=True)


@pytest.fixture
def queue(redis_client: fakeredis.aioredis.FakeRedis) -> RedisAgentRunQueue:
    return RedisAgentRunQueue(redis_client, ttl_seconds=3600)


async def test_enqueue_creates_queued_record_and_dequeue_returns_its_run_id(
    queue: RedisAgentRunQueue,
) -> None:
    record = await queue.enqueue("帮我质检这通电话")

    assert record.status == AgentRunQueueStatus.QUEUED
    assert record.message == "帮我质检这通电话"

    dequeued_run_id = await queue.dequeue(timeout=1)
    assert dequeued_run_id == record.run_id


async def test_enqueue_persists_owner_and_tenant_projection(
    queue: RedisAgentRunQueue,
) -> None:
    record = await queue.enqueue(
        "带归属的 Run",
        submitted_by_user_id="user-1",
        tenant_id="tenant-1",
    )

    persisted = await queue.get(record.run_id)

    assert persisted is not None
    assert persisted.submitted_by_user_id == "user-1"
    assert persisted.tenant_id == "tenant-1"


async def test_dequeue_times_out_on_empty_queue(queue: RedisAgentRunQueue) -> None:
    result = await queue.dequeue(timeout=0.1)
    assert result is None


async def test_ensure_enqueued_does_not_duplicate_existing_run(
    queue: RedisAgentRunQueue,
) -> None:
    record = await queue.enqueue("测试消息")

    assert await queue.ensure_enqueued(record.run_id) is True
    assert await queue.dequeue(timeout=0.1) == record.run_id
    assert await queue.dequeue(timeout=0.1) is None


async def test_queue_position_is_one_based_and_missing_safe(
    queue: RedisAgentRunQueue,
) -> None:
    first = await queue.enqueue("第一个")
    second = await queue.enqueue("第二个")

    assert await queue.queue_position(first.run_id) == 1
    assert await queue.queue_position(second.run_id) == 2
    assert await queue.queue_position("missing") is None


async def test_restore_queued_fills_missing_redis_projection_and_queue_entry(
    queue: RedisAgentRunQueue,
) -> None:
    """PostgreSQL 恢复出的 queued Run 缺少 Redis 投影时应补齐一次。"""
    record = AgentRunRecord(
        run_id="11111111-1111-1111-1111-111111111111",
        task_id="22222222-2222-2222-2222-222222222222",
        message="从 PostgreSQL 恢复",
        submitted_at=datetime(2026, 8, 13, 10, 0, tzinfo=UTC),
    )

    restored = await queue.restore_queued(record)

    assert restored is True
    persisted = await queue.get(record.run_id)
    assert persisted is not None
    assert persisted.status is AgentRunQueueStatus.QUEUED
    assert persisted.message == "从 PostgreSQL 恢复"
    assert await queue.queue_depth() == 1
    assert await queue.dequeue(timeout=0.1) == record.run_id


async def test_restore_queued_refreshes_existing_projection_without_duplicate_queue_entry(
    queue: RedisAgentRunQueue,
) -> None:
    record = AgentRunRecord(
        run_id="33333333-3333-3333-3333-333333333333",
        message="初始投影",
        submitted_at=datetime(2026, 8, 13, 10, 0, tzinfo=UTC),
    )
    assert await queue.restore_queued(record) is True

    refreshed = record.model_copy(update={"message": "恢复后的最新投影"})
    assert await queue.restore_queued(refreshed) is False

    persisted = await queue.get(record.run_id)
    assert persisted is not None
    assert persisted.message == "恢复后的最新投影"
    assert await queue.queue_depth() == 1
    assert await queue.dequeue(timeout=0.1) == record.run_id
    assert await queue.dequeue(timeout=0.1) is None


async def test_restore_queued_never_overwrites_running_projection_from_stale_scan(
    queue: RedisAgentRunQueue,
) -> None:
    running = AgentRunRecord(
        run_id="55555555-5555-5555-5555-555555555555",
        message="已由 Worker 执行",
        status=AgentRunQueueStatus.RUNNING,
        submitted_at=datetime(2026, 8, 13, 10, 0, tzinfo=UTC),
        started_at=datetime(2026, 8, 13, 10, 1, tzinfo=UTC),
        worker_instance_id="app-worker-active",
    )
    await queue.save(running)

    stale = running.model_copy(
        update={
            "message": "过期的 PostgreSQL 扫描快照",
            "status": AgentRunQueueStatus.QUEUED,
            "worker_instance_id": None,
        }
    )

    assert await queue.restore_queued(stale) is False
    persisted = await queue.get(running.run_id)
    assert persisted is not None
    assert persisted.status is AgentRunQueueStatus.RUNNING
    assert persisted.message == "已由 Worker 执行"
    assert persisted.worker_instance_id == "app-worker-active"
    assert await queue.queue_depth() == 0


async def test_restore_queued_replaces_only_the_expected_orphan_worker(
    queue: RedisAgentRunQueue,
) -> None:
    running = AgentRunRecord(
        run_id="66666666-6666-6666-6666-666666666666",
        message="旧 Worker",
        status=AgentRunQueueStatus.RUNNING,
        submitted_at=datetime(2026, 8, 13, 10, 0, tzinfo=UTC),
        worker_instance_id="dead-app-worker",
    )
    await queue.save(running)
    recovered = running.model_copy(
        update={
            "message": "PG 恢复快照",
            "status": AgentRunQueueStatus.QUEUED,
            "worker_instance_id": None,
        }
    )

    assert (
        await queue.restore_queued(
            recovered,
            expected_running_worker_instance_id="another-worker",
        )
        is False
    )
    assert (
        await queue.restore_queued(
            recovered,
            expected_running_worker_instance_id="dead-app-worker",
        )
        is True
    )
    persisted = await queue.get(running.run_id)
    assert persisted is not None
    assert persisted.status is AgentRunQueueStatus.QUEUED
    assert persisted.message == "PG 恢复快照"
    assert await queue.dequeue(timeout=0.1) == running.run_id


@pytest.mark.parametrize(
    "terminal_status",
    [
        AgentRunQueueStatus.COMPLETED,
        AgentRunQueueStatus.FAILED,
        AgentRunQueueStatus.CANCELED,
    ],
)
async def test_restore_queued_never_revives_terminal_redis_projection(
    queue: RedisAgentRunQueue,
    terminal_status: AgentRunQueueStatus,
) -> None:
    terminal = AgentRunRecord(
        run_id="44444444-4444-4444-4444-444444444444",
        message="终态投影",
        status=terminal_status,
        submitted_at=datetime(2026, 8, 13, 10, 0, tzinfo=UTC),
        finished_at=datetime(2026, 8, 13, 10, 1, tzinfo=UTC),
    )
    await queue.save(terminal)

    queued = terminal.model_copy(
        update={
            "message": "不应复活",
            "status": AgentRunQueueStatus.QUEUED,
            "finished_at": None,
        }
    )
    assert await queue.restore_queued(queued) is False

    persisted = await queue.get(terminal.run_id)
    assert persisted is not None
    assert persisted.status is terminal_status
    assert await queue.queue_depth() == 0


async def test_cancel_queued_removes_run_and_marks_terminal(
    queue: RedisAgentRunQueue,
) -> None:
    record = await queue.enqueue("测试消息")

    canceled = await queue.cancel_queued(record.run_id)

    assert canceled is not None
    assert canceled.status == AgentRunQueueStatus.CANCELED
    assert canceled.termination_reason == "canceled"
    assert canceled.finished_at is not None
    assert await queue.dequeue(timeout=0.1) is None
    persisted = await queue.get(record.run_id)
    assert persisted is not None
    assert persisted.status == AgentRunQueueStatus.CANCELED


async def test_get_returns_none_for_unknown_run_id(queue: RedisAgentRunQueue) -> None:
    assert await queue.get("does-not-exist") is None


async def test_save_round_trips_all_fields(queue: RedisAgentRunQueue) -> None:
    record = await queue.enqueue("测试消息")
    record.status = AgentRunQueueStatus.COMPLETED
    record.started_at = datetime.now(UTC)
    record.finished_at = datetime.now(UTC)
    record.termination_reason = "completed"
    record.final_text = "完成了"
    record.step_count = 2
    record.error_message = None
    record.error_category = None
    record.usage = {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}
    record.action_trace = [{"step": 1, "tool_name": "echo_tool", "status": "ok"}]
    record.working_memory = {
        "schema_version": 1,
        "current_page": {"page_capture_id": "capture-1"},
    }
    record.dispatch_attempt_count = 2
    record.worker_instance_id = "worker-1"
    record.worker_missing_since = datetime.now(UTC)

    await queue.save(record)
    loaded = await queue.get(record.run_id)

    assert loaded is not None
    assert loaded.status == AgentRunQueueStatus.COMPLETED
    assert loaded.final_text == "完成了"
    assert loaded.step_count == 2
    assert loaded.usage == {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}
    assert loaded.action_trace == [{"step": 1, "tool_name": "echo_tool", "status": "ok"}]
    assert loaded.working_memory["current_page"]["page_capture_id"] == "capture-1"
    assert loaded.dispatch_attempt_count == 2
    assert loaded.worker_instance_id == "worker-1"
    assert loaded.worker_missing_since == record.worker_missing_since


async def test_request_cancel_on_unknown_run_id_returns_false(queue: RedisAgentRunQueue) -> None:
    assert await queue.request_cancel("does-not-exist") is False


@pytest.mark.parametrize("status", [AgentRunQueueStatus.QUEUED, AgentRunQueueStatus.RUNNING])
async def test_request_cancel_succeeds_for_cancelable_statuses(
    queue: RedisAgentRunQueue, status: AgentRunQueueStatus
) -> None:
    record = await queue.enqueue("测试消息")
    record.status = status
    await queue.save(record)

    accepted = await queue.request_cancel(record.run_id)

    assert accepted is True
    assert await queue.is_cancel_requested(record.run_id) is True


@pytest.mark.parametrize(
    "status",
    [AgentRunQueueStatus.COMPLETED, AgentRunQueueStatus.FAILED, AgentRunQueueStatus.CANCELED],
)
async def test_request_cancel_rejected_for_terminal_statuses(
    queue: RedisAgentRunQueue, status: AgentRunQueueStatus
) -> None:
    record = await queue.enqueue("测试消息")
    record.status = status
    await queue.save(record)

    accepted = await queue.request_cancel(record.run_id)

    assert accepted is False
    assert await queue.is_cancel_requested(record.run_id) is False


async def test_is_cancel_requested_defaults_to_false(queue: RedisAgentRunQueue) -> None:
    record = await queue.enqueue("测试消息")
    assert await queue.is_cancel_requested(record.run_id) is False


async def test_dispatch_start_failure_requeues_consumed_run(
    queue: RedisAgentRunQueue,
) -> None:
    record = await queue.enqueue("测试消息")
    assert await queue.dequeue(timeout=0.1) == record.run_id
    record.status = AgentRunQueueStatus.RUNNING
    record.dispatch_attempt_count = 1
    record.worker_instance_id = "worker-1"

    requeued = await queue.requeue_after_dispatch_failure(
        record,
        error_message="数据库连接耗尽",
    )

    assert requeued is True
    assert await queue.dequeue(timeout=0.1) == record.run_id
    persisted = await queue.get(record.run_id)
    assert persisted is not None
    assert persisted.status == AgentRunQueueStatus.QUEUED
    assert persisted.worker_instance_id is None


async def test_orphaned_running_run_can_be_terminally_canceled(
    queue: RedisAgentRunQueue,
) -> None:
    record = await queue.enqueue("测试消息")
    assert await queue.dequeue(timeout=0.1) == record.run_id
    record.status = AgentRunQueueStatus.RUNNING
    record.worker_instance_id = "dead-worker"
    await queue.save(record)

    canceled = await queue.cancel_orphaned_running(record.run_id)

    assert canceled is not None
    assert canceled.status == AgentRunQueueStatus.CANCELED
    assert canceled.worker_instance_id is None


async def test_live_worker_running_run_is_not_terminally_canceled(
    queue: RedisAgentRunQueue,
) -> None:
    record = await queue.enqueue("测试消息")
    assert await queue.dequeue(timeout=0.1) == record.run_id
    record.status = AgentRunQueueStatus.RUNNING
    record.worker_instance_id = "worker-1"
    await queue.save(record)
    await queue.record_worker_heartbeat("worker-1")

    assert await queue.cancel_orphaned_running(record.run_id) is None


async def test_worker_heartbeat_reader_keeps_legacy_timestamp_compatible(
    queue: RedisAgentRunQueue,
    redis_client: fakeredis.aioredis.FakeRedis,
) -> None:
    await redis_client.set(
        "alioth:agent-runs:worker-heartbeat:legacy-worker",
        datetime.now(UTC).isoformat(),
    )

    heartbeats = await queue.read_worker_heartbeats()

    assert heartbeats[0]["instance_id"] == "legacy-worker"
    assert heartbeats[0]["roles"] == []
    assert heartbeats[0]["heartbeat_at"].tzinfo is not None


async def test_saved_record_has_ttl_applied(
    queue: RedisAgentRunQueue, redis_client: fakeredis.aioredis.FakeRedis
) -> None:
    record = await queue.enqueue("测试消息")
    ttl = await redis_client.ttl(f"alioth:agent-runs:{record.run_id}")
    assert 0 < ttl <= 3600


def test_agent_run_record_model_construction_defaults() -> None:
    record = AgentRunRecord(
        run_id="abc", message="hi", submitted_at=datetime(2026, 1, 1, tzinfo=UTC)
    )
    assert record.status == AgentRunQueueStatus.QUEUED
    assert record.action_trace == []
    assert record.working_memory == {}
    assert record.usage is None
    assert record.worker_missing_since is None


def test_build_redis_client_enables_connection_recovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict = {}
    expected_client = object()

    def fake_from_url(url: str, **kwargs):
        captured["url"] = url
        captured.update(kwargs)
        return expected_client

    monkeypatch.setattr(redis_queue_module.redis_asyncio, "from_url", fake_from_url)

    client = redis_queue_module.build_redis_client(QueueSettings())

    assert client is expected_client
    assert captured["decode_responses"] is True
    assert captured["health_check_interval"] == 10
    assert captured["socket_keepalive"] is True
    assert captured["retry"]._retries == 3
