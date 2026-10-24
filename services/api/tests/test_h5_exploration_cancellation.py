"""H5 探索取消检查点测试。"""

import pytest

from services.api.app.application.h5_exploration_cancellation import (
    H5CancellationCheckpoint,
    H5CancellationUnavailable,
    H5ExplorationCanceled,
    RedisH5CancellationPort,
)


class _CancellationSource:
    def __init__(self, requested: bool) -> None:
        self.requested = requested
        self.calls: list[tuple[str, str]] = []

    async def is_cancel_requested(self, *, task_id: str, agent_run_id: str) -> bool:
        self.calls.append((task_id, agent_run_id))
        return self.requested


@pytest.mark.asyncio
async def test_checkpoint_raises_typed_cancellation_before_side_effect() -> None:
    """若移除取消查询，副作用前的取消会被忽略。"""

    source = _CancellationSource(requested=True)
    checkpoint = H5CancellationCheckpoint(source)

    with pytest.raises(H5ExplorationCanceled) as captured:
        await checkpoint.raise_if_requested(
            task_id="task-1",
            agent_run_id="run-1",
            stage="before_action",
        )

    assert source.calls == [("task-1", "run-1")]
    assert captured.value.stage == "before_action"
    assert captured.value.error_code == "WORKFLOW_CANCELED"


@pytest.mark.asyncio
async def test_checkpoint_fails_closed_when_cancel_source_is_unavailable() -> None:
    """若取消源读取失败仍继续副作用，Redis 短暂故障会绕过用户已请求的取消。"""

    class UnavailableSource:
        async def is_cancel_requested(self, *, task_id: str, agent_run_id: str) -> bool:
            raise ConnectionError("redis unavailable")

    checkpoint = H5CancellationCheckpoint(UnavailableSource())

    with pytest.raises(H5CancellationUnavailable) as captured:
        await checkpoint.raise_if_requested(
            task_id="task-1",
            agent_run_id="run-1",
            stage="before_action",
        )

    assert captured.value.retryable is True


@pytest.mark.asyncio
async def test_redis_signal_short_circuits_durable_cancel_lookup() -> None:
    """Redis 已命中时不得再依赖 PostgreSQL 可用性。"""

    class RedisSource:
        async def is_cancel_requested(self, agent_run_id: str) -> bool:
            assert agent_run_id == "run-1"
            return True

    class UnavailableDurableSource:
        async def is_cancel_requested(self, run_id: str) -> bool:
            raise AssertionError(f"不应读取持久化来源：{run_id}")

    port = RedisH5CancellationPort(
        RedisSource(),
        durable_source=UnavailableDurableSource(),
    )

    assert await port.is_cancel_requested(task_id="task-1", agent_run_id="run-1") is True
