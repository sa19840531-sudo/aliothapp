"""验证真实消费循环的共享许可、不同队列公平性及冻结运行控制。"""

import asyncio
import os
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from urllib.parse import urlparse
from uuid import uuid4

import fakeredis
import pytest
import redis.asyncio as redis

from services.api.app import worker as worker_module
from services.api.app.application.worker_execution_control import (
    WorkerWorkflowCanceled,
    WorkerWorkflowDeadlineExceeded,
    run_with_worker_controls,
)
from services.api.app.core import config as config_module
from services.api.app.core.config import Settings, WorkerSettings
from services.api.app.infrastructure.queue.redis_queue import (
    AgentRunQueueStatus,
    AgentRunRecord,
    RedisAgentRunQueue,
)
from services.api.app.infrastructure.queue.state_store import InMemoryAgentRunStateStore


async def _wait_until(predicate, *, timeout=3.0):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.01)


@pytest.fixture
async def isolated_redis():
    url = os.getenv("ALIOTH_QUEUE_CAPACITY_TEST_REDIS_URL")
    if url:
        parsed = urlparse(url)
        if parsed.hostname not in {"127.0.0.1", "localhost"} or parsed.port in {None, 6379}:
            raise ValueError("容量测试只能使用明确指定的本机独立 Redis 端口")
        client = redis.from_url(url, decode_responses=True)
    else:
        client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    try:
        yield client
    finally:
        await client.aclose()


async def test_default_and_web_queue_share_bounded_fair_capacity_without_popping_waiters(
    isolated_redis, monkeypatch,
) -> None:
    prefix = f"test:capacity:{uuid4()}"
    queue = RedisAgentRunQueue(isolated_redis, ttl_seconds=60, queue_key=prefix + ":default")
    web_queue = RedisAgentRunQueue(isolated_redis, ttl_seconds=60, queue_key=prefix + ":web")
    records = []
    for platform in ["app", "h5"] + ["h5"] * 12 + ["web"] * 2:
        selected = web_queue if platform == "web" else queue
        records.append(await selected.enqueue(
            "容量回归", task_config={"_task_target": {"platform": platform}},
            loop_guard={"max_duration_seconds": 2 if len(records) == 1 else 20},
        ))
    started = []
    active = 0
    peak = 0
    outcomes = {}
    release = asyncio.Event()
    stop = asyncio.Event()

    async def process(selected, run_id, settings, **kwargs):
        nonlocal active, peak
        record = await selected.get(run_id)
        record.status = AgentRunQueueStatus.RUNNING
        record.started_at = datetime.now(UTC)
        await selected.save(record)
        started.append((run_id, record.task_config["_task_target"]["platform"]))
        active += 1
        peak = max(peak, active)
        try:
            await run_with_worker_controls(
                record=record, default_duration_seconds=20, operation=release.wait,
                is_cancel_requested=lambda: selected.is_cancel_requested(run_id),
                poll_interval_seconds=0.01,
            )
            outcomes[run_id] = "done"
        except WorkerWorkflowCanceled:
            outcomes[run_id] = "canceled"
        except WorkerWorkflowDeadlineExceeded:
            outcomes[run_id] = "timeout"
        finally:
            active -= 1

    monkeypatch.setattr(worker_module, "_process_one", process)
    settings = Settings(worker=WorkerSettings(concurrency=3, web_concurrency=2, max_active_runs=4))
    capacity = asyncio.BoundedSemaphore(4)
    tasks = [asyncio.create_task(worker_module._worker_loop(
        index, selected, settings, stop, InMemoryAgentRunStateStore(), run_capacity=capacity
    )) for index, selected in enumerate([queue] * 3 + [web_queue] * 2)]
    try:
        await _wait_until(lambda: len(started) >= 4)
        assert {platform for _, platform in started[:4]} == {"app", "h5", "web"}
        assert active == peak == 4
        assert await web_queue.queue_position(records[-1].run_id) == 1
        assert await queue.queue_depth() == 11
        await queue.request_cancel(records[0].run_id)
        await _wait_until(lambda: sum(platform == "web" for _, platform in started) == 2)
        assert outcomes[records[0].run_id] == "canceled"
        assert await queue.queue_depth() >= 10
        await _wait_until(lambda: outcomes.get(records[1].run_id) == "timeout", timeout=4)
        assert peak == 4
        stop.set()
        release.set()
        await asyncio.wait_for(asyncio.gather(*tasks), timeout=3)
        assert active == 0
        assert capacity._value == 4
    finally:
        stop.set()
        release.set()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await isolated_redis.delete(prefix + ":default", prefix + ":web")
        for record in records:
            await isolated_redis.delete(
                "alioth:agent-runs:" + record.run_id,
                "alioth:agent-runs:" + record.run_id + ":cancel",
            )


@pytest.mark.parametrize("reason", ["canceled", "expired", "hang"])
async def test_workflow_control_stops_before_execution_or_awaits_canceled_execution(reason):
    called = False
    cleaned = asyncio.Event()
    record = AgentRunRecord(
        run_id="controls", message="运行控制", submitted_at=datetime.now(UTC),
        started_at=datetime.now(UTC) - timedelta(seconds=1 if reason == "expired" else 0),
        loop_guard={"max_duration_seconds": 0.02},
    )

    async def operation():
        nonlocal called
        called = True
        try:
            await asyncio.Event().wait()
        finally:
            cleaned.set()

    async def canceled():
        return reason == "canceled"

    error = WorkerWorkflowCanceled if reason == "canceled" else WorkerWorkflowDeadlineExceeded
    with pytest.raises(error):
        await run_with_worker_controls(
            record=record, default_duration_seconds=20, operation=operation,
            is_cancel_requested=canceled, poll_interval_seconds=0.005,
        )
    assert called == (reason == "hang")
    assert cleaned.is_set() == (reason == "hang")


async def test_provider_timeout_is_not_rewritten_as_run_budget_timeout():
    record = AgentRunRecord(run_id="provider", message="提供者超时", submitted_at=datetime.now(UTC))

    async def operation():
        raise TimeoutError("提供者自己的超时")

    async def canceled():
        return False

    with pytest.raises(TimeoutError, match="提供者自己的超时") as error:
        await run_with_worker_controls(
            record=record, default_duration_seconds=20, operation=operation,
            is_cancel_requested=canceled,
        )
    assert not isinstance(error.value, WorkerWorkflowDeadlineExceeded)


async def test_initial_cancel_read_hang_is_inside_same_frozen_workflow_deadline():
    record = AgentRunRecord(
        run_id="cancel-read", message="取消读取阻塞", submitted_at=datetime.now(UTC),
        loop_guard={"max_duration_seconds": 0.02},
    )
    invoked = False

    async def operation():
        nonlocal invoked
        invoked = True

    async def hanging_cancel_read():
        await asyncio.Event().wait()
        return False

    async with asyncio.timeout(0.5):
        with pytest.raises(WorkerWorkflowDeadlineExceeded):
            await run_with_worker_controls(
                record=record, default_duration_seconds=20, operation=operation,
                is_cancel_requested=hanging_cancel_read,
            )
    assert invoked is False


async def test_h5_cancel_winning_at_failed_finalization_uses_real_terminal_wrapper(monkeypatch):
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    queue = RedisAgentRunQueue(client, ttl_seconds=60, queue_key="test:late-h5-cancel")
    record = await queue.enqueue(
        "失败收口阶段取消", task_id="task-late-cancel",
        task_config={"workflow_version": "v2", "execution_mode": "full_inspection",
                     "_task_target": {"platform": "h5"}},
    )
    record.status = AgentRunQueueStatus.FAILED
    state = InMemoryAgentRunStateStore()

    async def canceled(**_kwargs):
        record.status = AgentRunQueueStatus.CANCELED
        record.termination_reason = "canceled"
        return True

    async def no_effect(*_args, **_kwargs):
        return 0

    monkeypatch.setattr(worker_module, "_mark_h5_run_canceled_if_requested", canceled)
    monkeypatch.setattr(worker_module, "_cancel_h5_nonterminal_work", no_effect)
    monkeypatch.setattr(worker_module, "_reconcile_run_llm_usage_from_events", no_effect)
    try:
        assert await worker_module._finish_h5_completely_failed_run(
            queue=queue, record=record, settings=Settings(), state_store=state,
            progress_recorder=None, worker_instance_id=None,
            task_finalization_service=SimpleNamespace(terminate=no_effect), work_drained=True,
        ) is True
        persisted = await state.get_finished_record(run_id=record.run_id)
        assert persisted is not None and persisted.status is AgentRunQueueStatus.CANCELED
        assert persisted.final_report is None
    finally:
        await client.aclose()


@pytest.mark.parametrize("name", ["ALIOTH_WORKER_CONCURRENCY", "ALIOTH_WEB_WORKER_CONCURRENCY",
                                  "ALIOTH_WORKER_MAX_ACTIVE_RUNS"])
@pytest.mark.parametrize("value", ["0", "-1", "33"])
def test_invalid_worker_capacity_is_rejected_instead_of_silently_disabling_consumers(
    monkeypatch, name, value,
):
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError):
        config_module._build_worker_settings()
