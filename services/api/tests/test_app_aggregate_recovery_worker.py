"""验证 Completion 恢复先交接快照和聚合，真实恢复失败仍进入超时判断。"""

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from services.api.app import worker as worker_module
from services.api.app.domain.task import TaskFinalizationSnapshot, TaskFinalizationState
from services.api.app.infrastructure.queue.redis_queue import AgentRunQueueStatus
from services.api.app.infrastructure.queue.state_store import InMemoryAgentRunStateStore
from services.api.tests.test_worker import (
    FakeTaskFinalizationService,
)
from services.api.tests.test_worker import (
    queue as queue,  # noqa: F401 - 复用真实 Redis 队列夹具
)


@pytest.mark.parametrize("failure", [None, "snapshot", "aggregate", "gate"])
async def test_recovery_handoffs_precede_stalled_check_and_failure_does_not_bypass_it(
    queue, failure,
):
    service = FakeTaskFinalizationService(gate_passed=False)
    record = await queue.enqueue(
        "APP 后段恢复", task_id="11111111-1111-1111-1111-111111111111",
        task_config={"workflow_version": "v2", "execution_mode": "full_inspection",
                     "_task_target": {"platform": "app"}},
    )
    record.status = AgentRunQueueStatus.COMPLETED
    record.finished_at = datetime.now(UTC)
    await queue.save(record)
    service.pending_task_ids.add(record.task_id)
    service.recoverable_run_ids[record.task_id] = record.run_id
    events = []

    async def get_snapshot(*, task_id):
        return TaskFinalizationSnapshot(
            task_id=task_id, state=TaskFinalizationState.SESSION_DRAINING,
            agent_run_id=record.run_id, finalization_epoch=4, row_version=7,
        )

    class Publisher:
        async def recover_if_unpublished(self, **kwargs):
            assert kwargs["finalization_epoch"] == 4
            events.append("snapshot")
            if failure == "snapshot":
                raise ValueError("Index 损坏")

    async def prepare(**kwargs):
        assert kwargs["finalization_epoch"] == 4
        events.append("aggregate")
        if failure == "aggregate":
            raise RuntimeError("聚合交接失败")

    async def terminate(_command, **_kwargs):
        events.append("stalled")
        return SimpleNamespace(state=TaskFinalizationState.SEALED)

    async def check_gate(_command):
        events.append("gate")
        if failure == "gate":
            raise ValueError("门禁事实损坏")
        return False

    service.get = get_snapshot
    service.completion_gate_preparer = SimpleNamespace(prepare_app_aggregate_for_recovery=prepare)
    service.terminate_stalled = terminate
    service.app_completion_ready_before_timeout = check_gate
    assert await worker_module._retry_pending_task_finalizations(
        queue, InMemoryAgentRunStateStore(), service,
        sealed_inspection_publisher=Publisher(),
    ) == 1
    assert events == ["snapshot", "aggregate", "gate", "stalled"]
