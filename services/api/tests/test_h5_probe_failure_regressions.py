"""H5 补采截止、租约围栏和关闭结算的回归。"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from services.api.app.application.app_exploration_lease import AppExplorationLeaseLostError
from services.api.app.application.evidence_requests import EvidenceRequestStatus
from services.api.app.application.exploration_work_queue import (
    ExplorerWorkPersistenceError,
    ExplorerWorkStatus,
)
from services.api.app.application.h5_probe_work import H5ProbeDrainService, H5ProbeWorkRunner
from services.api.app.application.supplemental_evidence_policy import (
    build_lab_supplemental_evidence_policy,
)
from services.api.tests.test_h5_probe_work import NOW, MemoryQueue, _work


def _runner(queue, executor, *, now=lambda: NOW):
    completion = SimpleNamespace(complete=AsyncMock())
    runner = H5ProbeWorkRunner(
        queue=queue,
        turn_executor=executor,
        request_consumption=SimpleNamespace(ensure_running=AsyncMock(
            return_value=SimpleNamespace(status=EvidenceRequestStatus.RUNNING),
        )),
        completion=completion,
        lease_owner="worker-1:h5-probe",
        policy=build_lab_supplemental_evidence_policy(),
        now=now,
    )
    return runner, completion


async def test_probe_timeout_cancels_and_joins_external_operation_before_terminal():
    work = replace(_work(), deadline=NOW + timedelta(milliseconds=30))
    stopped = asyncio.Event()

    async def execute(**kwargs):
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    class Queue(MemoryQueue):
        async def transition(self, *, command):
            if command.target_status is ExplorerWorkStatus.FAILED:
                assert stopped.is_set()
            return await super().transition(command=command)

    queue = Queue(work)
    runner, completion = _runner(queue, SimpleNamespace(execute=execute))
    outcome = await runner.run_one(task_id="task-1", agent_run_id="run-1")

    assert stopped.is_set()
    assert outcome.final_status is ExplorerWorkStatus.FAILED
    assert outcome.error_code == "H5_PROBE_DEADLINE_EXHAUSTED"
    completion.complete.assert_not_awaited()


async def test_probe_external_cancellation_joins_operation_without_writing_failure():
    started, stopped = asyncio.Event(), asyncio.Event()

    async def execute(**kwargs):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    queue = MemoryQueue(_work())
    runner, completion = _runner(queue, SimpleNamespace(execute=execute))
    running = asyncio.create_task(runner.run_one(task_id="task-1", agent_run_id="run-1"))
    await started.wait()
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    assert stopped.is_set()
    assert [item.target_status for item in queue.transitions] == [ExplorerWorkStatus.RUNNING]
    completion.complete.assert_not_awaited()


async def test_probe_expired_before_execution_does_not_touch_browser():
    executor = SimpleNamespace(execute=AsyncMock())
    runner, _ = _runner(MemoryQueue(_work()), executor, now=lambda: NOW + timedelta(minutes=6))
    result = await runner.run_one(task_id="task-1", agent_run_id="run-1")
    assert result.error_code == "H5_PROBE_DEADLINE_EXHAUSTED"
    executor.execute.assert_not_awaited()


async def test_probe_foreign_lease_is_not_converted_to_timeout():
    class Queue(MemoryQueue):
        async def renew(self, *, command):
            raise ExplorerWorkPersistenceError("租约已转移")

    executor = SimpleNamespace(execute=AsyncMock())
    runner, completion = _runner(Queue(_work()), executor)
    with pytest.raises(AppExplorationLeaseLostError):
        await runner.run_one(task_id="task-1", agent_run_id="run-1")
    executor.execute.assert_not_awaited()
    completion.complete.assert_not_awaited()


async def test_provider_timeout_is_not_mislabeled_as_probe_deadline():
    executor = SimpleNamespace(execute=AsyncMock(side_effect=TimeoutError("供应商超时")))
    runner, completion = _runner(MemoryQueue(_work()), executor)
    result = await runner.run_one(task_id="task-1", agent_run_id="run-1")
    assert result.error_code == "TimeoutError"
    assert result.final_status is ExplorerWorkStatus.FAILED
    completion.complete.assert_not_awaited()


async def test_wrapped_physical_budget_refusal_does_not_requeue_probe():
    from services.api.app.application.h5_llm_budget import H5LLMBudgetExhausted

    error = RuntimeError("SDK wrapper")
    error.__cause__ = H5LLMBudgetExhausted()
    queue = MemoryQueue(_work())
    runner, completion = _runner(queue, SimpleNamespace(execute=AsyncMock(side_effect=error)))
    result = await runner.run_one(task_id="task-1", agent_run_id="run-1")
    assert result.error_code == "h5_llm_call_budget_exhausted"
    assert result.final_status is ExplorerWorkStatus.FAILED
    completion.complete.assert_not_awaited()


@pytest.mark.parametrize("can_settle", [False, True])
async def test_deadline_lease_failure_never_bypasses_token_and_version_fencing(can_settle):
    class Queue(MemoryQueue):
        async def renew(self, *, command):
            self.renewals.append(command)
            raise ExplorerWorkPersistenceError("截止时间已过")

        async def transition(self, *, command):
            if command.target_status is ExplorerWorkStatus.FAILED and not can_settle:
                raise ExplorerWorkPersistenceError("租约已被回收")
            return await super().transition(command=command)

    queue = Queue(_work())
    executor = SimpleNamespace(execute=AsyncMock())
    runner, completion = _runner(queue, executor, now=lambda: NOW + timedelta(minutes=6))
    if can_settle:
        result = await runner.run_one(task_id="task-1", agent_run_id="run-1")
        assert result.final_status is ExplorerWorkStatus.FAILED
        assert result.error_code == "H5_PROBE_DEADLINE_EXHAUSTED"
        assert queue.transitions[-1].row_version == 2
        assert queue.transitions[-1].lease_token == queue.transitions[0].lease_token
        assert not queue.renewals
    else:
        with pytest.raises(AppExplorationLeaseLostError):
            await runner.run_one(task_id="task-1", agent_run_id="run-1")
    executor.execute.assert_not_awaited()
    completion.complete.assert_not_awaited()


@pytest.mark.parametrize("settled", [0, 1])
async def test_probe_lease_failure_requires_authoritative_terminal_settlement(settled):
    terminal = SimpleNamespace(settle_ready=AsyncMock(side_effect=[0, settled]))
    wakeup = SimpleNamespace(reopen_waiting_evidence=AsyncMock())
    service = H5ProbeDrainService(
        runner=SimpleNamespace(run_one=AsyncMock(side_effect=AppExplorationLeaseLostError())),
        completion=SimpleNamespace(recover_next=AsyncMock(return_value=None)),
        terminal_settlement=terminal,
        inspection_wakeup=wakeup,
        policy=build_lab_supplemental_evidence_policy(),
    )
    if settled:
        assert await service.drain_one(task_id="task-1", agent_run_id="run-1")
        wakeup.reopen_waiting_evidence.assert_awaited_once_with(task_id="task-1")
    else:
        with pytest.raises(AppExplorationLeaseLostError):
            await service.drain_one(task_id="task-1", agent_run_id="run-1")
        wakeup.reopen_waiting_evidence.assert_not_awaited()
