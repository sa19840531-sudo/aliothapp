"""复现取消查询已返回、连接 reset 超时；未知事实仍拒绝继续执行。"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from services.api.app import worker
from services.api.app.application.worker_execution_control import (
    WorkerWorkflowCanceled,
    run_with_worker_controls,
)
from services.api.app.infrastructure.queue.state_store import PostgresAgentRunStateStore


class Acquire:
    def __init__(self, value=False, query_error=None, acquire_error=None, release_error=None):
        self.connection = SimpleNamespace(fetchval=AsyncMock(
            return_value=value, side_effect=query_error,
        ))
        self.acquire_error = acquire_error
        self.release_error = release_error

    async def __aenter__(self):
        if self.acquire_error:
            raise self.acquire_error
        return self.connection

    async def __aexit__(self, *args):
        if self.release_error:
            raise self.release_error


@pytest.mark.parametrize("value", [False, True])
async def test_h5_release_timeout_preserves_completed_authoritative_read(value, caplog):
    acquire = Acquire(value=value, release_error=TimeoutError("connection reset timeout"))
    store = PostgresAgentRunStateStore(pool=SimpleNamespace(acquire=lambda: acquire))
    assert await store.is_h5_cancel_requested("run-h5") is value
    assert "connection" in caplog.text
    assert "lower(task.target_platform) = 'h5'" in acquire.connection.fetchval.call_args.args[0]


@pytest.mark.parametrize("stage", ["query", "acquire", "missing"])
async def test_h5_unknown_cancel_fact_is_not_treated_as_false(stage):
    acquire = Acquire(
        value=None if stage == "missing" else False,
        query_error=TimeoutError("query timeout") if stage == "query" else None,
        acquire_error=TimeoutError("acquire timeout") if stage == "acquire" else None,
        release_error=TimeoutError("reset timeout") if stage == "query" else None,
    )
    store = PostgresAgentRunStateStore(pool=SimpleNamespace(acquire=lambda: acquire))
    with pytest.raises((TimeoutError, RuntimeError)):
        await store.is_h5_cancel_requested("run-h5")


async def test_h5_cancelled_task_is_never_swallowed_after_completed_query():
    acquire = Acquire(release_error=asyncio.CancelledError())
    store = PostgresAgentRunStateStore(pool=SimpleNamespace(acquire=lambda: acquire))
    with pytest.raises(asyncio.CancelledError):
        await store.is_h5_cancel_requested("run-h5")


async def test_non_h5_cancel_read_keeps_original_failure_policy():
    acquire = Acquire(release_error=TimeoutError("reset timeout"))
    store = PostgresAgentRunStateStore(pool=SimpleNamespace(acquire=lambda: acquire))
    with pytest.raises(TimeoutError):
        await store.is_cancel_requested("run-web")


@pytest.mark.parametrize("h5", [False, True])
async def test_worker_selects_h5_fact_reader_only_for_h5(h5):
    durable = SimpleNamespace(
        is_cancel_requested=AsyncMock(return_value=False),
        is_h5_cancel_requested=AsyncMock(return_value=True),
    )
    queue = SimpleNamespace(is_cancel_requested=AsyncMock(return_value=False))
    assert await worker._is_cancel_requested_safely(
        queue, "run", durable_source=durable, h5=h5,
    ) is h5
    if h5:
        durable.is_cancel_requested.assert_not_awaited()
    else:
        durable.is_h5_cancel_requested.assert_not_awaited()


@pytest.mark.parametrize("value", [False, True])
async def test_completed_h5_cancel_read_controls_execution_even_when_reset_times_out(value):
    acquire = Acquire(value=value, release_error=TimeoutError("connection reset timeout"))
    store = PostgresAgentRunStateStore(pool=SimpleNamespace(acquire=lambda: acquire))
    operation = AsyncMock(return_value="completed")
    kwargs = dict(
        record=SimpleNamespace(loop_guard={}, started_at=None), default_duration_seconds=1,
        operation=operation, is_cancel_requested=lambda: store.is_h5_cancel_requested("run-h5"),
    )
    if value:
        with pytest.raises(WorkerWorkflowCanceled):
            await run_with_worker_controls(**kwargs)
        operation.assert_not_awaited()
    else:
        assert await run_with_worker_controls(**kwargs) == "completed"
        operation.assert_awaited_once()
