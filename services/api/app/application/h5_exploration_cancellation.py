"""H5 探索的协作式取消端口与检查点。"""

from __future__ import annotations

from typing import Protocol


class H5CancellationPort(Protocol):
    """读取当前 H5 Run 的统一取消事实。"""

    async def is_cancel_requested(self, *, task_id: str, agent_run_id: str) -> bool: ...


class AgentRunCancellationSource(Protocol):
    """Redis Agent Run 队列提供的统一取消信号读取能力。"""

    async def is_cancel_requested(self, agent_run_id: str) -> bool: ...


class DurableAgentRunCancellationSource(Protocol):
    """PostgreSQL 长期状态存储提供的取消事实读取能力。"""

    async def is_cancel_requested(self, run_id: str) -> bool: ...


class RedisH5CancellationPort:
    """将既有 Redis Run 取消事实适配为 H5 领域端口。"""

    def __init__(
        self,
        source: AgentRunCancellationSource,
        *,
        durable_source: DurableAgentRunCancellationSource | None = None,
    ) -> None:
        self._source = source
        self._durable_source = durable_source

    async def is_cancel_requested(self, *, task_id: str, agent_run_id: str) -> bool:
        del task_id
        if await self._source.is_cancel_requested(agent_run_id):
            return True
        if self._durable_source is None:
            return False
        return await self._durable_source.is_cancel_requested(agent_run_id)


class H5ExplorationCanceled(RuntimeError):
    """取消在安全的 H5 副作用边界被确认。"""

    error_code = "WORKFLOW_CANCELED"
    retryable = False

    def __init__(self, *, stage: str) -> None:
        self.stage = stage
        super().__init__(f"H5 探索已在 {stage} 阶段取消")


class H5CancellationUnavailable(RuntimeError):
    """取消事实不可读取时拒绝继续 H5 副作用。"""

    error_code = "H5_CANCELLATION_SOURCE_UNAVAILABLE"
    retryable = True

    def __init__(self, *, stage: str) -> None:
        self.stage = stage
        super().__init__(f"H5 探索无法在 {stage} 阶段读取取消事实")


class H5CancellationCheckpoint:
    """在 H5 探索生命周期的安全边界确认取消。"""

    def __init__(self, port: H5CancellationPort | None = None) -> None:
        self._port = port

    async def raise_if_requested(
        self,
        *,
        task_id: str,
        agent_run_id: str,
        stage: str,
    ) -> None:
        if self._port is None:
            return
        try:
            requested = await self._port.is_cancel_requested(
                task_id=task_id,
                agent_run_id=agent_run_id,
            )
        except Exception as exc:  # noqa: BLE001 - 取消事实不可用时不能继续副作用
            raise H5CancellationUnavailable(stage=stage) from exc
        if requested:
            raise H5ExplorationCanceled(stage=stage)

    async def before_side_effect(
        self,
        *,
        work: object,
        agent_run_id: str,
        decision: object,
    ) -> None:
        """在模型产出决策后、任何页面副作用开始前确认取消。"""

        del decision
        task_id = getattr(work, "task_id", None)
        if not isinstance(task_id, str) or not task_id.strip():
            raise TypeError("H5 副作用检查缺少有效 task_id")
        await self.raise_if_requested(
            task_id=task_id,
            agent_run_id=agent_run_id,
            stage="after_decision_before_side_effect",
        )
