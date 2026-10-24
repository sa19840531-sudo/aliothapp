"""历史运行必须由后台扫描完成持久化收口。"""

from datetime import UTC, datetime, timedelta

import fakeredis
import pytest

from services.api.app import worker
from services.api.app.core.config import Settings
from services.api.app.infrastructure.queue.redis_queue import RedisAgentRunQueue
from services.api.app.infrastructure.queue.state_store import InMemoryAgentRunStateStore


class TransientStore(InMemoryAgentRunStateStore):
    async def record_finished(self, record):
        if not getattr(self, "recovered", False):
            raise RuntimeError("数据库短断")
        await super().record_finished(record)


async def legacy_run(mode="exploration_only"):
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    queue = RedisAgentRunQueue(redis, ttl_seconds=60)
    record = await queue.enqueue(
        "历史探索", task_id="legacy-task",
        task_config={"workflow_version": "v2", "execution_mode": mode,
                     "web_inspection_notification_mode": "enforced",
                     "_task_target": {"platform": "web"}},
    )
    return redis, queue, record


@pytest.mark.asyncio
async def test_orphan_scan_retries_durable_failure_after_redis_legacy_terminal():
    _, queue, record = await legacy_run()
    state = TransientStore()
    await state.record_submitted(record)
    with pytest.raises(RuntimeError, match="数据库短断"):
        await worker._process_one(queue, record.run_id, settings=Settings(), state_store=state)
    assert (await queue.get(record.run_id)).status == "failed"
    state.recovered = True
    await worker._reconcile_orphaned_runs(queue, state)
    durable = await state.get_finished_record(run_id=record.run_id)
    assert durable is not None
    assert durable.error_message == "WEB_LEGACY_RUN_EXECUTION_NOT_SUPPORTED"
    finished_count = len(state.events)
    await worker._reconcile_orphaned_runs(queue, state)
    assert len(state.events) == finished_count


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["exploration_only", "full_inspection"])
@pytest.mark.parametrize("redis_expired", [False, True])
async def test_orphan_scan_terminalizes_predeployment_legacy_and_retries(
    mode, redis_expired, monkeypatch,
):
    async def forbidden_formal_recovery(*args, **kwargs):
        pytest.fail("历史 Run 不得调用正式巡检或浏览器恢复")

    monkeypatch.setattr(worker, "_reconcile_orphaned_web_formal_run", forbidden_formal_recovery)
    redis, queue, record = await legacy_run(mode)
    record.status = type(record.status).RUNNING
    record.started_at = datetime.now(UTC)
    record.worker_instance_id = "old-worker"
    record.worker_missing_since = datetime.now(UTC) - timedelta(
        seconds=worker.ORPHAN_RECONCILIATION_GRACE_SECONDS + 1,
    )
    await queue.save(record)
    state = TransientStore()
    await state.record_started(record)
    if redis_expired:
        await redis.flushdb()
    await worker._reconcile_orphaned_runs(queue, state)
    assert await state.get_finished_record(run_id=record.run_id) is None
    state.recovered = True
    await worker._reconcile_orphaned_runs(queue, state)
    durable = await state.get_finished_record(run_id=record.run_id)
    assert durable is not None
    assert durable.error_message == "WEB_LEGACY_RUN_EXECUTION_NOT_SUPPORTED"
    assert durable.evidence_gap == ["WEB_LEGACY_RUN_EXECUTION_NOT_SUPPORTED"]
    assert durable.dispatch_attempt_count == 0
