"""真实命令门禁退出时的取消不能遗留已经明确登记的引导阶段。"""

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from services.api.app.application.surface_sessions import SessionCommandPermit
from services.api.app.application.web_guide_candidate import WebGuideCandidate
from services.api.app.application.web_guide_dispatcher import WebGuideBranchDispatcher
from services.api.app.application.worker_execution_control import (
    WorkerWorkflowCanceled,
    WorkerWorkflowDeadlineExceeded,
    run_with_worker_controls,
)
from services.api.app.domain.exploration.surface_phase import SurfacePhaseRunStatus
from services.api.app.domain.interaction import SurfaceKind, SurfaceSessionStatus
from services.api.app.infrastructure.db.session_command_gate import PostgresSessionCommandGate
from services.api.app.infrastructure.db.surface_session_repository import (
    SurfaceSessionPersistenceError,
)
from services.api.app.infrastructure.db.web_guide_phase_ledger import WebGuidePhaseLedger
from services.api.app.infrastructure.queue.redis_queue import AgentRunRecord


class _ClaimLifecycle:
    def __init__(self, *, preexisting=False, start_result_unknown=False, release_ok=True):
        self.release_started = asyncio.Event()
        self.release_gate = asyncio.Event()
        self.release_finished = asyncio.Event()
        self.audit_started = asyncio.Event()
        self.release_audit = asyncio.Event()
        self.release_audit.set()
        self.steps = []
        self.start_count = 0
        self.browser_attempts = []
        self.phase = SimpleNamespace(
            phase_run_id="mock-known-phase", row_version=0, status=SurfacePhaseRunStatus.RUNNING,
        )
        self.created = preexisting
        lifecycle = self

        class Sessions:
            async def get(self, *, session_id):
                return SimpleNamespace(
                    task_id="mock-task", surface_kind=SurfaceKind.WEB,
                    status=SurfaceSessionStatus.ACTIVE,
                )

            async def acquire_command(self, **kwargs):
                return SessionCommandPermit(
                    **{key: kwargs[key] for key in (
                        "task_id", "session_id", "surface_kind", "lease_owner", "lease_token",
                    )},
                    command_sequence=1, finalization_epoch=0,
                    lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
                )

            async def release_command(self, **kwargs):
                lifecycle.release_started.set()
                await lifecycle.release_gate.wait()
                lifecycle.release_finished.set()
                return release_ok

        class Phases:
            async def get_run(self, **kwargs):
                return lifecycle.phase if lifecycle.created else None

            async def start_run(self, **kwargs):
                lifecycle.start_count += 1
                lifecycle.created = True
                if start_result_unknown:
                    raise RuntimeError("登记结果与所有权未确认")
                return lifecycle.phase

            async def record_step(self, **kwargs):
                lifecycle.audit_started.set()
                await lifecycle.release_audit.wait()
                lifecycle.steps.append(kwargs)
                lifecycle.phase.row_version += 1

            async def finish_run(self, **kwargs):
                assert kwargs["expected_row_version"] == lifecycle.phase.row_version
                lifecycle.phase.status = kwargs["status"]

        class Branch:
            async def attempt(self, *args, **kwargs):
                lifecycle.browser_attempts.append(args)
                raise AssertionError("登记已取消后不能再打开浏览器")

        self.gate = PostgresSessionCommandGate(repository=Sessions(), lease_owner="mock-worker")
        self.ledger = WebGuidePhaseLedger(gate=self.gate, phases=Phases())
        self.dispatcher = WebGuideBranchDispatcher(
            branch=Branch(), ledger=self.ledger, task_id="mock-task", agent_run_id="mock-run",
            session_id="mock-session", deadline=datetime.now(UTC) + timedelta(minutes=1),
            claim_timeout_seconds=0.03,
        )
        self.candidate = WebGuideCandidate(
            candidate_id="mock-candidate", source_capture_id="mock-source",
            source_url="https://site.example/", destination_url="https://site.example/app",
            label="APP", kind="page_candidate",
        )

    async def schedule(self):
        await self.dispatcher.schedule(
            source_capture_id="mock-source", source_url="https://site.example/",
            snapshot={
                "elements": [{"tag": "a", "visible": True, "text": "官方 APP", "href": "/app"}]
            },
        )

    def assert_skipped(self, reason="WEB_GUIDE_MAINLINE_FINISHED"):
        assert self.release_finished.is_set()
        assert self.phase.status is SurfacePhaseRunStatus.COMPLETED
        assert len(self.steps) == 1
        assert self.steps[0]["decision_status"] == "skipped"
        assert self.steps[0]["reason_code"] == reason
        assert self.browser_attempts == []
        assert not self.dispatcher._tasks


@pytest.mark.asyncio
@pytest.mark.parametrize("stop", ["cancel", "claim_timeout"])
async def test_real_ledger_waits_for_gate_exit_and_audits_committed_phase_before_stop(stop):
    lifecycle = _ClaimLifecycle()
    schedule = asyncio.create_task(lifecycle.schedule())
    await asyncio.wait_for(lifecycle.release_started.wait(), 1)
    assert lifecycle.created and lifecycle.phase.status is SurfacePhaseRunStatus.RUNNING
    if stop == "cancel":
        schedule.cancel()
    else:
        await asyncio.sleep(0.06)
    await asyncio.sleep(0)
    assert not schedule.done()
    assert lifecycle.steps == []
    lifecycle.release_gate.set()
    if stop == "cancel":
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(schedule, 1)
    else:
        await asyncio.wait_for(schedule, 1)
    await lifecycle.dispatcher.close()
    lifecycle.assert_skipped()


@pytest.mark.asyncio
async def test_repeated_cancel_during_known_claim_audit_does_not_interrupt_handoff():
    lifecycle = _ClaimLifecycle()
    lifecycle.release_audit.clear()
    schedule = asyncio.create_task(lifecycle.schedule())
    await asyncio.wait_for(lifecycle.release_started.wait(), 1)
    schedule.cancel()
    lifecycle.release_gate.set()
    await asyncio.wait_for(lifecycle.audit_started.wait(), 1)
    schedule.cancel()
    await asyncio.sleep(0)
    assert not schedule.done()
    schedule.cancel()
    await asyncio.sleep(0)
    assert not schedule.done()
    lifecycle.release_audit.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(schedule, 1)
    await lifecycle.dispatcher.close()
    lifecycle.assert_skipped()


@pytest.mark.asyncio
@pytest.mark.parametrize("stop", ["cancel", "deadline"])
async def test_worker_controls_wait_for_committed_claim_cleanup_before_releasing_operation(stop):
    lifecycle = _ClaimLifecycle()
    lifecycle.release_audit.clear()
    canceled = asyncio.Event()
    record = AgentRunRecord(
        run_id="mock-run", task_id="mock-task", message="登记交接测试",
        submitted_at=datetime.now(UTC),
        loop_guard={"max_duration_seconds": 0.06 if stop == "deadline" else 2},
    )

    async def operation():
        try:
            await lifecycle.schedule()
            await asyncio.Event().wait()
        finally:
            await lifecycle.dispatcher.close()

    async def cancel_reader():
        return canceled.is_set()

    execution = asyncio.create_task(run_with_worker_controls(
        record=record, default_duration_seconds=2, operation=operation,
        is_cancel_requested=cancel_reader, poll_interval_seconds=0.01,
    ))
    await asyncio.wait_for(lifecycle.release_started.wait(), 1)
    if stop == "cancel":
        canceled.set()
    await asyncio.sleep(0.08)
    assert not execution.done()
    lifecycle.release_gate.set()
    await asyncio.wait_for(lifecycle.audit_started.wait(), 1)
    assert not execution.done()
    lifecycle.release_audit.set()
    expected = WorkerWorkflowCanceled if stop == "cancel" else WorkerWorkflowDeadlineExceeded
    with pytest.raises(expected):
        await asyncio.wait_for(execution, 1)
    lifecycle.assert_skipped()


@pytest.mark.asyncio
async def test_canceled_idempotent_claim_does_not_complete_an_existing_phase():
    lifecycle = _ClaimLifecycle(preexisting=True)
    claim = asyncio.create_task(lifecycle.ledger.claim(
        candidate=lifecycle.candidate, task_id="mock-task", session_id="mock-session",
    ))
    await asyncio.wait_for(lifecycle.release_started.wait(), 1)
    claim.cancel()
    lifecycle.release_gate.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(claim, 1)
    assert lifecycle.start_count == 0
    assert lifecycle.steps == []
    assert lifecycle.phase.status is SurfacePhaseRunStatus.RUNNING


@pytest.mark.asyncio
async def test_unknown_start_result_does_not_guess_or_complete_a_phase():
    lifecycle = _ClaimLifecycle(start_result_unknown=True)
    lifecycle.release_gate.set()
    with pytest.raises(RuntimeError, match="所有权未确认"):
        await lifecycle.ledger.claim(
            candidate=lifecycle.candidate, task_id="mock-task", session_id="mock-session",
        )
    assert lifecycle.steps == []
    assert lifecycle.phase.status is SurfacePhaseRunStatus.RUNNING


@pytest.mark.asyncio
async def test_failed_gate_exit_audits_only_the_phase_created_by_this_claim():
    lifecycle = _ClaimLifecycle(release_ok=False)
    lifecycle.release_gate.set()
    with pytest.raises(SurfaceSessionPersistenceError, match="租约释放失败"):
        await lifecycle.ledger.claim(
            candidate=lifecycle.candidate, task_id="mock-task", session_id="mock-session",
        )
    lifecycle.assert_skipped("WEB_GUIDE_CLAIM_FAILED")
