"""纯探索 Web 孤儿不得借用 H5 清理或在持久租约有效时提前失败。"""

from datetime import UTC, datetime, timedelta

import fakeredis.aioredis
import pytest

from services.api.app import worker
from services.api.app.infrastructure.queue.redis_queue import (
    AgentRunQueueStatus,
    RedisAgentRunQueue,
)
from services.api.app.infrastructure.queue.state_store import InMemoryAgentRunStateStore


@pytest.fixture
def queue() -> RedisAgentRunQueue:
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    return RedisAgentRunQueue(client, ttl_seconds=3600)


@pytest.mark.parametrize("settled", [False, True])
async def test_web_exploration_orphan_uses_own_settlement_before_terminal(
    queue: RedisAgentRunQueue,
    monkeypatch,
    settled,
):
    record = await queue.enqueue(
        "Web 孤儿收束",
        task_id="11111111-1111-1111-1111-111111111111",
        task_config={
            "workflow_version": "v2",
            "web_runtime_version": "web-runtime.v2",
            "execution_mode": "exploration_only",
            "_task_target": {"platform": "web"},
        },
    )
    await queue.dequeue(timeout=0.1)
    record.status = AgentRunQueueStatus.RUNNING
    record.worker_instance_id = "dead-web-worker"
    record.worker_missing_since = datetime.now(UTC) - timedelta(
        seconds=worker.ORPHAN_RECONCILIATION_GRACE_SECONDS + 1
    )
    await queue.save(record)
    calls = []

    async def settle(*, task_id, run_id):
        calls.append((task_id, run_id))
        return settled

    async def forbid_h5(**kwargs):
        pytest.fail("Web 纯探索不得走 H5 孤儿清理")

    monkeypatch.setattr(worker, "_settle_web_exploration_orphan", settle, raising=False)
    monkeypatch.setattr(worker, "_fail_h5_orphaned_nonterminal_runtime", forbid_h5)
    store = InMemoryAgentRunStateStore()
    await worker._reconcile_orphaned_runs(queue, store)
    result = await queue.get(record.run_id)
    assert calls == [(record.task_id, record.run_id)]
    assert result.status is (AgentRunQueueStatus.FAILED if settled else AgentRunQueueStatus.RUNNING)
    assert bool(store.events) is settled


async def test_finished_web_orphan_projects_database_terminal_without_cleanup(
    queue: RedisAgentRunQueue,
    monkeypatch,
):
    record = await queue.enqueue(
        "Web 终态投影恢复",
        task_id="11111111-1111-1111-1111-111111111111",
        task_config={
            "workflow_version": "v2",
            "web_runtime_version": "web-runtime.v2",
            "execution_mode": "exploration_only",
            "_task_target": {"platform": "web"},
        },
    )
    await queue.dequeue(timeout=0.1)
    record.status = AgentRunQueueStatus.RUNNING
    record.worker_instance_id = "dead-web-worker"
    record.worker_missing_since = datetime.now(UTC) - timedelta(
        seconds=worker.ORPHAN_RECONCILIATION_GRACE_SECONDS + 1
    )
    await queue.save(record)
    store = InMemoryAgentRunStateStore()
    terminal = record.model_copy(
        update={
            "status": AgentRunQueueStatus.FAILED,
            "finished_at": datetime.now(UTC),
            "worker_instance_id": None,
            "worker_missing_since": None,
        }
    )
    await store.record_run_finished(terminal)

    async def forbidden(**kwargs):
        pytest.fail("已有权威终态只恢复投影，不再清理或等待旧租约")

    monkeypatch.setattr(worker, "_settle_web_exploration_orphan", forbidden)
    await worker._reconcile_orphaned_runs(queue, store)
    assert (await queue.get(record.run_id)).status is AgentRunQueueStatus.FAILED
