"""多步补证不能被主探索插页，嵌套命令仍保留独立命令序号。"""

import asyncio
from contextlib import asynccontextmanager
from contextvars import Context
from datetime import UTC, datetime, timedelta

import pytest

from services.api.app.application.capture_coordinator import InProcessSessionCommandGate


def _gate():
    from services.api.app.application.h5_session_journey import H5SessionJourneyGate

    return H5SessionJourneyGate(delegate=InProcessSessionCommandGate())


async def test_main_cannot_enter_between_recapture_steps():
    gate = _gate()
    first, proceed, main = (asyncio.Event() for _ in range(3))
    sequences = []

    async def recapture():
        async with gate.hold_journey(task_id="task", session_id="h5:run"):
            async with gate.hold(task_id="task", session_id="h5:run") as permit:
                sequences.append(permit.command_sequence)
            first.set()
            await proceed.wait()
            async with gate.hold(task_id="task", session_id="h5:run") as permit:
                sequences.append(permit.command_sequence)

    async def explore():
        await first.wait()
        async with gate.hold(task_id="task", session_id="h5:run") as permit:
            sequences.append(permit.command_sequence)
            main.set()

    tasks = [asyncio.create_task(recapture()), asyncio.create_task(explore())]
    try:
        await first.wait()
        await asyncio.sleep(0.02)
        assert not main.is_set()
        proceed.set()
        await asyncio.wait_for(asyncio.gather(*tasks), 1)
        assert sequences == [1, 2, 3]
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def test_recovery_random_suffix_does_not_cause_identity_rejection():
    gate = _gate()
    async with gate.hold_journey(task_id="task", session_id="h5:run:recovery:ffff"):
        async with gate.hold(task_id="task", session_id="h5:run:recovery:0000") as permit:
            assert permit.command_sequence == 1


async def test_nested_recovery_never_cross_waits_another_root():
    gate = _gate()
    entered, finish = asyncio.Event(), asyncio.Event()

    async def other():
        async with gate.hold_journey(task_id="task", session_id="h5:a"):
            entered.set()
            await finish.wait()

    task = asyncio.create_task(other())
    await entered.wait()
    try:
        async with gate.hold_journey(task_id="task", session_id="h5:b"):
            with pytest.raises(RuntimeError, match="交叉等待"):
                async with gate.hold(task_id="task", session_id="h5:a"):
                    pytest.fail("不得取得另一根工作的会话")
    finally:
        finish.set()
        await task


async def test_child_stage_reenters_journey_without_reusing_command_permit():
    gate = _gate()
    async with gate.hold_journey(task_id="task", session_id="h5:run"):
        async def stage():
            async with gate.hold_journey(task_id="task", session_id="h5:run"):
                async with gate.hold(task_id="task", session_id="h5:run") as permit:
                    return permit.command_sequence
        assert await asyncio.wait_for(asyncio.create_task(stage()), 1) == 1
        assert await asyncio.wait_for(asyncio.create_task(stage()), 1) == 2


async def test_expired_wait_does_no_command_and_does_not_reset_deadline():
    from services.api.app.application.h5_session_journey import h5_session_journey

    gate = _gate()
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def owner():
        async with gate.hold_journey(task_id="task", session_id="h5:run"):
            entered.set()
            await release.wait()

    task = asyncio.create_task(owner())
    await entered.wait()
    try:
        deadline = datetime.now(UTC) + timedelta(milliseconds=20)
        with pytest.raises(TimeoutError):
            async with h5_session_journey(
                gate=gate, task_id="task", session_id="h5:run", deadline=deadline,
            ):
                calls.append("model-or-capture")
        assert calls == []
    finally:
        release.set()
        await task
    async with gate.hold(task_id="task", session_id="h5:run") as permit:
        assert permit.command_sequence == 1


async def test_escaped_child_scope_is_rejected_instead_of_bypassing_new_owner():
    gate = _gate()
    release = asyncio.Event()

    async def late():
        await release.wait()
        async with gate.hold(task_id="task", session_id="h5:run"):
            raise AssertionError("旧子协程不能取得命令")

    async with gate.hold_journey(task_id="task", session_id="h5:run"):
        task = asyncio.create_task(late())
    release.set()
    with pytest.raises(RuntimeError, match="失效"):
        await task


async def test_cross_task_scope_and_release_are_rejected():
    gate = _gate()
    async with gate.hold_journey(task_id="task", session_id="h5:run"):
        with pytest.raises(ValueError):
            async with gate.hold(task_id="other", session_id="h5:run"):
                pass
        with pytest.raises(RuntimeError):
            await gate.release_session(task_id="task", session_id="h5:run")


async def test_distinct_sessions_and_non_h5_commands_are_not_globally_serialized():
    gate = _gate()
    async with gate.hold_journey(task_id="task", session_id="h5:run"):
        async def other():
            async with gate.hold(task_id="other", session_id="h5:other"):
                async with gate.hold(task_id="web", session_id="web:run"):
                    return True
        assert await asyncio.wait_for(asyncio.create_task(other(), context=Context()), 1)


async def test_remote_lease_loss_cancels_owned_body_and_keeps_rejection():
    from services.api.app.application.h5_session_journey import H5SessionJourneyGate

    stopped = asyncio.Event()

    @asynccontextmanager
    async def remote(scope):
        async def lose():
            await asyncio.sleep(0.02)
            scope.invalidate()
        monitor = asyncio.create_task(lose())
        try:
            yield
        finally:
            monitor.cancel()
            await asyncio.gather(monitor, return_exceptions=True)

    gate = H5SessionJourneyGate(delegate=InProcessSessionCommandGate(), remote_hold=remote)
    with pytest.raises(RuntimeError, match="失效"):
        async with gate.hold_journey(task_id="task", session_id="h5:run"):
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()
    assert stopped.is_set()


async def test_h5_work_cancellation_drains_child_before_journey_release():
    from services.api.app.application.app_exploration_lease import AppExplorationLeaseLostError
    from services.api.app.application.exploration_work_queue import RenewExplorerWorkLeaseCommand
    from services.api.app.application.h5_recapture_lease import H5BoundedLeaseGuard
    from services.api.tests.test_h5_probe_work import MemoryQueue, _work

    started, stopped = asyncio.Event(), asyncio.Event()
    guard = H5BoundedLeaseGuard(
        queue=MemoryQueue(_work()),
        command=RenewExplorerWorkLeaseCommand(
            work_id="work-1", lease_owner="worker", lease_token="token", lease_seconds=180,
        ), renew_interval_seconds=60, lost_error_type=AppExplorationLeaseLostError,
        lost_message="丢租", task_name="audit-child",
    )

    async def action():
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    running = asyncio.create_task(guard.run(action()))
    await started.wait()
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    assert stopped.is_set()


async def test_recapture_consumer_holds_source_across_plan_and_restoration():
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from services.api.app.domain.inspection import RestorationAttemptStatus, RestorationStrategy
    from services.api.app.domain.interaction import CaptureKind, CaptureRef, SurfaceKind
    from services.api.tests.test_h5_recapture_execution_limits import _consumer
    from services.api.tests.test_h5_targeted_recapture import NOW

    gate = _gate()
    entered, finish, competing = (asyncio.Event() for _ in range(3))

    async def restore(**kwargs):
        entered.set()
        await finish.wait()
        async with gate.hold(task_id="task-h5", session_id="h5:run-h5"):
            assert not competing.is_set()
        return SimpleNamespace(
            restoration_attempt=SimpleNamespace(status=RestorationAttemptStatus.EQUIVALENT),
            probe_work=object(),
        )

    runtime = SimpleNamespace(
        captures=SimpleNamespace(get_verified_capture=AsyncMock(return_value=SimpleNamespace(
            capture=CaptureRef(
                task_id="task-h5", session_id="h5:run-h5", capture_id="capture-target",
                surface_kind=SurfaceKind.WEB, capture_kind=CaptureKind.PAGE, role="after_action",
            ),
        ))),
        resume_equivalent_publication=AsyncMock(return_value=None),
        resolve_plan=AsyncMock(return_value=SimpleNamespace(plan=SimpleNamespace(
            normalization_step_ids=(), strategy=RestorationStrategy.ORIGINAL_SURFACE_PIN,
            deadline=NOW + timedelta(seconds=10),
        ))),
        restore_and_publish=restore,
    )
    consumer, requests, kwargs = _consumer(runtime, clock=lambda: NOW)
    consumer._session_command_gate = gate
    budget = kwargs["budget"]

    async def main():
        async with gate.hold(task_id="task-h5", session_id="h5:run-h5"):
            competing.set()

    owner = asyncio.create_task(consumer.consume_next(**kwargs))
    await entered.wait()
    other = asyncio.create_task(main())
    try:
        await asyncio.sleep(0.02)
        assert not competing.is_set()
        finish.set()
        result = await asyncio.wait_for(owner, 1)
        await asyncio.wait_for(other, 1)
        assert result.disposition.value == "probe_published"
        assert kwargs["budget"] is budget
        requests.release_lease.assert_not_awaited()
    finally:
        owner.cancel()
        other.cancel()
        await asyncio.gather(owner, other, return_exceptions=True)


async def test_main_runner_acquires_journey_before_executor_and_releases_after_terminal():
    from types import SimpleNamespace

    from services.api.app.application.h5_exploration_turn import H5ExplorationTurnRunner
    from services.api.tests.test_h5_exploration_turn import _Queue, _terminal_turn, _work

    gate = _gate()
    work = _work()
    started = asyncio.Event()

    async def execute(**kwargs):
        started.set()
        async with gate.hold(task_id=work.task_id, session_id=work.session_id):
            return _terminal_turn(work)

    runner = H5ExplorationTurnRunner(
        queue=_Queue(work), turn_executor=SimpleNamespace(execute=execute),
        continuation=object(), lease_owner="journey-test", session_command_gate=gate,
    )
    async with gate.hold_journey(task_id=work.task_id, session_id=work.session_id):
        task = asyncio.create_task(
            runner.run_one(task_id=work.task_id, agent_run_id="run-h5"), context=Context(),
        )
        await asyncio.sleep(0.02)
        assert not started.is_set()
    result = await asyncio.wait_for(task, 1)
    assert result.final_status.value == "succeeded"
    await gate.release_session(task_id=work.task_id, session_id=work.session_id)


async def test_probe_runner_waits_before_planning_and_keeps_original_deadline():
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from services.api.app.application.evidence_requests import EvidenceRequestStatus
    from services.api.app.application.h5_probe_work import H5ProbeWorkRunner
    from services.api.app.application.supplemental_evidence_policy import (
        build_lab_supplemental_evidence_policy,
    )
    from services.api.tests.test_h5_probe_work import NOW, MemoryQueue, _turn, _work

    gate = _gate()
    work = _work()
    calls = []

    async def execute(**kwargs):
        calls.append(kwargs["work"].deadline)
        async with gate.hold(task_id=work.task_id, session_id=work.session_id):
            return _turn(work)

    runner = H5ProbeWorkRunner(
        queue=MemoryQueue(work), turn_executor=SimpleNamespace(execute=execute),
        request_consumption=SimpleNamespace(ensure_running=AsyncMock(
            return_value=SimpleNamespace(status=EvidenceRequestStatus.RUNNING))),
        completion=SimpleNamespace(complete=AsyncMock()), lease_owner="journey-test",
        policy=build_lab_supplemental_evidence_policy(), now=lambda: NOW,
        session_command_gate=gate,
    )
    async with gate.hold_journey(task_id=work.task_id, session_id=work.session_id):
        task = asyncio.create_task(
            runner.run_one(task_id=work.task_id, agent_run_id="run-1"), context=Context(),
        )
        await asyncio.sleep(0.02)
        assert calls == []
    result = await asyncio.wait_for(task, 1)
    assert result.final_status.value == "succeeded"
    assert calls == [work.deadline]


async def test_work_lease_loss_while_waiting_journey_stops_before_model_or_capture(monkeypatch):
    from types import SimpleNamespace

    from services.api.app.application import app_exploration_lease
    from services.api.app.application.app_exploration_lease import AppExplorationLeaseLostError
    from services.api.app.application.h5_recapture_lease import H5BoundedLeaseGuard

    gate = _gate()
    queued, fail = asyncio.Event(), asyncio.Event()
    calls = []
    renews = 0

    async def renew(**kwargs):
        nonlocal renews
        renews += 1
        if renews > 1:
            raise AppExplorationLeaseLostError("真实租约围栏变化")

    async def pulse(_):
        await fail.wait()

    monkeypatch.setattr(app_exploration_lease, "_sleep", pulse)

    async def waiting():
        guard = H5BoundedLeaseGuard(
            queue=SimpleNamespace(renew=renew), command=SimpleNamespace(lease_seconds=10),
            renew_interval_seconds=1, lost_error_type=AppExplorationLeaseLostError,
            lost_message="租约失效", task_name="journey-wait-test",
        )
        async with guard:
            queued.set()
            async with gate.hold_journey(task_id="task", session_id="h5:run"):
                calls.append("model-or-capture")

    async with gate.hold_journey(task_id="task", session_id="h5:run"):
        task = asyncio.create_task(waiting(), context=Context())
        await queued.wait()
        fail.set()
        with pytest.raises(AppExplorationLeaseLostError):
            await asyncio.wait_for(task, 1)
        assert calls == []
    assert renews == 2


async def test_main_first_renewal_hang_is_bounded_before_external_operation():
    from dataclasses import replace
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from services.api.app.application.h5_exploration_turn import H5ExplorationTurnRunner
    from services.api.tests.test_h5_exploration_turn import _Queue, _work

    stopped = asyncio.Event()
    work = _work()
    work = replace(work, budget=replace(work.budget, remaining_duration_ms=20))
    queue = _Queue(work)

    async def renew(**kwargs):
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    queue.renew = renew
    execute = AsyncMock()
    result = await asyncio.wait_for(H5ExplorationTurnRunner(
        queue=queue, turn_executor=SimpleNamespace(execute=execute), continuation=object(),
        lease_owner="journey-test", session_command_gate=_gate(),
    ).run_one(task_id=work.task_id, agent_run_id="run-h5"), 1)
    assert result.error_code == "H5_SESSION_JOURNEY_DEADLINE_EXHAUSTED"
    assert result.final_status.value == "failed"
    execute.assert_not_awaited()
    assert stopped.is_set()


@pytest.mark.parametrize("stage_mode", ["guard", "recapture", "recapture_without_renew"])
async def test_repeated_cancel_does_not_release_journey_before_child_cleanup(stage_mode):
    from types import SimpleNamespace

    from services.api.app.application.app_exploration_lease import AppExplorationLeaseLostError
    from services.api.app.application.h5_recapture_lease import (
        H5BoundedLeaseGuard,
        run_h5_recapture_stage,
    )

    gate = _gate()
    started, cleaning, finish, stopped, main = (asyncio.Event() for _ in range(5))

    async def operation():
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaning.set()
            await finish.wait()
            stopped.set()

    async def renew(**kwargs):
        return None

    async def owner():
        if stage_mode != "guard":
            async def stage():
                async with gate.hold_journey(task_id="task", session_id="h5:run"):
                    await operation()

            async def renew_request(**kwargs):
                return True

            await run_h5_recapture_stage(
                operation=stage, requests=(SimpleNamespace(renew_h5_lease=renew_request)
                    if stage_mode == "recapture" else object()), lease=object(),
                agent_run_id="run", lease_seconds=10,
                deadline=datetime.now(UTC) + timedelta(seconds=10),
                now=lambda: datetime.now(UTC),
            )
            return
        guard = H5BoundedLeaseGuard(
            queue=SimpleNamespace(renew=renew), command=SimpleNamespace(lease_seconds=10),
            renew_interval_seconds=1, lost_error_type=AppExplorationLeaseLostError,
            lost_message="租约失效", task_name="repeat-cancel-test",
        )
        async with guard, gate.hold_journey(task_id="task", session_id="h5:run"):
            await guard.run(operation())

    async def competing():
        async with gate.hold(task_id="task", session_id="h5:run"):
            assert stopped.is_set()
            main.set()

    task = asyncio.create_task(owner())
    await started.wait()
    task.cancel()
    await cleaning.wait()
    other = asyncio.create_task(competing())
    task.cancel()
    try:
        await asyncio.sleep(0.02)
        assert not task.done()
        assert not main.is_set()
        finish.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 1)
        await asyncio.wait_for(other, 1)
        assert main.is_set()
    finally:
        finish.set()
        task.cancel()
        other.cancel()
        await asyncio.gather(task, other, return_exceptions=True)
