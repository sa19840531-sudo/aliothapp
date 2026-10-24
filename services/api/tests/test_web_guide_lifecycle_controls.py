"""真实控制器和 Runtime 退出前必须等待隔离引导浏览器与审计停止。"""

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from services.api.app.application.web_guide_dispatcher import WebGuideBranchDispatcher
from services.api.app.application.web_guide_page import WebGuideBranch, WebGuideOutcome
from services.api.app.application.web_runtime_composition import ProductionWebV2RuntimeExecutor
from services.api.app.application.worker_execution_control import (
    WorkerWorkflowCanceled,
    WorkerWorkflowDeadlineExceeded,
    run_with_worker_controls,
)
from services.api.app.core.config import Settings
from services.api.tests.test_web_runtime_composition import _web_record


@pytest.fixture(autouse=True)
def _isolate_environment(monkeypatch):
    """纯生命周期测试不继承线上登录或数据库配置。"""
    monkeypatch.delenv("ALIOTH_WEB_LOGIN_ENABLED", raising=False)
    monkeypatch.delenv("ALIOTH_DATABASE_URL", raising=False)


class _GuideLifecycle:
    def __init__(self):
        self.browser_started = asyncio.Event()
        self.browser_closed = asyncio.Event()
        self.audit_started = asyncio.Event()
        self.release_audit = asyncio.Event()
        self.release_audit.set()
        self.audit_completed = asyncio.Event()
        self.outcomes = []
        lifecycle = self

        class Observer:
            async def observe(self, candidate):
                lifecycle.browser_started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    lifecycle.browser_closed.set()

        class Ledger:
            async def claim(self, **kwargs):
                return "mock-guide-phase"

            async def complete(self, *, phase, outcome):
                assert lifecycle.browser_closed.is_set()
                lifecycle.audit_started.set()
                await lifecycle.release_audit.wait()
                lifecycle.outcomes.append(outcome)
                lifecycle.audit_completed.set()

        self.dispatcher = WebGuideBranchDispatcher(
            branch=WebGuideBranch(observer=Observer(), writer=object()),
            ledger=Ledger(),
            task_id="mock-task",
            agent_run_id="mock-run",
            session_id="web:mock-run",
            deadline=datetime.now(UTC) + timedelta(minutes=1),
        )

    async def schedule(self):
        await self.dispatcher.schedule(
            source_capture_id="mock-source",
            source_url="https://site.example/",
            snapshot={
                "elements": [{"tag": "a", "visible": True, "text": "官方 APP", "href": "/app"}]
            },
        )
        await asyncio.wait_for(self.browser_started.wait(), 1)

    def assert_stopped(self):
        assert self.browser_closed.is_set()
        assert self.audit_completed.is_set()
        assert len(self.outcomes) == 1
        assert self.outcomes[0].status == "skipped"
        assert self.outcomes[0].reason_code == "WEB_GUIDE_MAINLINE_FINISHED"
        assert not self.dispatcher._tasks


def _runtime(lifecycle, mode):
    close_calls = []

    class Stages:
        async def initialize(self, command, **kwargs):
            await lifecycle.schedule()

        async def next_due(self, run_id):
            if mode == "error":
                raise RuntimeError("mock-stage-failure")
            if mode == "wait":
                await asyncio.Event().wait()
            return None

        async def final_result(self, run_id):
            return SimpleNamespace(
                preparation=None,
                exploration=SimpleNamespace(
                    page_timeline=("mock-capture",), page_type_keys=("home",), failed_urls=()
                ),
            )

    async def close(**kwargs):
        close_calls.append(kwargs)

    return SimpleNamespace(
        stage_orchestrator=Stages(),
        web_guide_dispatcher=lifecycle.dispatcher,
        deadline=datetime.now(UTC) + timedelta(minutes=1),
        close=close,
    ), close_calls


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["normal", "error"])
async def test_runtime_normal_and_error_wait_for_owned_guide_cleanup(mode):
    lifecycle = _GuideLifecycle()
    runtime, close_calls = _runtime(lifecycle, mode)
    result = await ProductionWebV2RuntimeExecutor(worker_instance_id="mock-worker").run(
        runtime, record=_web_record(), settings=Settings()
    )
    lifecycle.assert_stopped()
    if mode == "normal":
        assert result.page_timeline == ("mock-capture",)
        # 正常 checkpoint 流程仍拥有主 Session 的关闭职责。
        assert close_calls == []
    else:
        assert result.error_message == "mock-stage-failure"
        assert len(close_calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("stop", ["cancel", "deadline", "parent_cancel"])
async def test_actual_worker_controls_wait_for_runtime_guide_before_exit(stop):
    lifecycle = _GuideLifecycle()
    runtime, close_calls = _runtime(lifecycle, "wait")
    record = _web_record()
    record.loop_guard = {"max_duration_seconds": 0.1 if stop == "deadline" else 5}
    cancel_requested = asyncio.Event()

    async def cancel_reader():
        return cancel_requested.is_set()

    task = asyncio.create_task(
        run_with_worker_controls(
            record=record,
            default_duration_seconds=5,
            operation=lambda: ProductionWebV2RuntimeExecutor(worker_instance_id="mock-worker").run(
                runtime, record=record, settings=Settings()
            ),
            is_cancel_requested=cancel_reader,
            poll_interval_seconds=0.01,
        )
    )
    await asyncio.wait_for(lifecycle.browser_started.wait(), 1)
    if stop == "cancel":
        cancel_requested.set()
        expected_error = WorkerWorkflowCanceled
    elif stop == "parent_cancel":
        task.cancel()
        expected_error = asyncio.CancelledError
    else:
        expected_error = WorkerWorkflowDeadlineExceeded
    with pytest.raises(expected_error):
        await asyncio.wait_for(task, 2)
    lifecycle.assert_stopped()
    # 取消不能绕过 checkpoint 所有权去关闭共享主 Session。
    assert close_calls == []


@pytest.mark.asyncio
async def test_close_does_not_cancel_already_cancelling_audit_again():
    lifecycle = _GuideLifecycle()
    lifecycle.release_audit.clear()
    await lifecycle.schedule()
    branch_task = next(iter(lifecycle.dispatcher._tasks))
    branch_task.cancel()
    await asyncio.wait_for(lifecycle.audit_started.wait(), 1)
    close = asyncio.create_task(lifecycle.dispatcher.close())
    await asyncio.sleep(0)
    assert not close.done()
    assert branch_task.cancelling() == 1
    lifecycle.release_audit.set()
    await asyncio.wait_for(close, 1)
    lifecycle.assert_stopped()


@pytest.mark.asyncio
async def test_repeated_parent_cancel_is_deferred_until_owned_audit_completes():
    lifecycle = _GuideLifecycle()
    lifecycle.release_audit.clear()
    await lifecycle.schedule()
    close = asyncio.create_task(lifecycle.dispatcher.close())
    await asyncio.wait_for(lifecycle.audit_started.wait(), 1)
    branch_task = next(iter(lifecycle.dispatcher._tasks))
    close.cancel()
    await asyncio.sleep(0)
    assert not close.done()
    close.cancel()
    await asyncio.sleep(0)
    assert not close.done()
    assert branch_task.cancelling() == 1
    lifecycle.release_audit.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(close, 1)
    lifecycle.assert_stopped()


@pytest.mark.asyncio
@pytest.mark.parametrize("delayed_claim", [False, True])
async def test_close_waits_for_registration_and_never_starts_closed_branch(delayed_claim):
    claim_started = asyncio.Event()
    release_claim = asyncio.Event()
    completed = []
    attempts = []

    class Ledger:
        async def claim(self, **kwargs):
            claim_started.set()
            if delayed_claim:
                await release_claim.wait()
            return "mock-phase"

        async def complete(self, *, phase, outcome):
            assert phase == "mock-phase"
            completed.append(outcome)

    class Branch:
        async def attempt(self, *args, **kwargs):
            attempts.append(args)
            raise AssertionError("关闭后不能再启动浏览器")

    dispatcher = WebGuideBranchDispatcher(
        branch=Branch(), ledger=Ledger(), task_id="mock-task", agent_run_id="mock-run",
        session_id="mock-session", deadline=datetime.now(UTC) + timedelta(minutes=1),
    )
    schedule = dispatcher.schedule(
        source_capture_id="mock-source", source_url="https://site.example/",
        snapshot={"elements": [{"tag": "a", "visible": True, "text": "官方 APP", "href": "/app"}]},
    )
    if delayed_claim:
        registration = asyncio.create_task(schedule)
        await asyncio.wait_for(claim_started.wait(), 1)
        close = asyncio.create_task(dispatcher.close())
        await asyncio.sleep(0)
        assert not close.done()
        release_claim.set()
        await asyncio.wait_for(asyncio.gather(registration, close), 1)
    else:
        await schedule
        # 尚未开始的支线也须写回已登记阶段，不能直接 cancel 丢失审计。
        await dispatcher.close()
    assert attempts == []
    assert len(completed) == 1 and completed[0].status == "skipped"
    assert not dispatcher._tasks


@pytest.mark.asyncio
async def test_close_preserves_terminal_audit_without_prior_cancel():
    lifecycle = _GuideLifecycle()
    lifecycle.release_audit.clear()

    class CompletedBranch:
        async def attempt(self, candidate, **kwargs):
            lifecycle.browser_started.set()
            lifecycle.browser_closed.set()
            return WebGuideOutcome(
                "uncertain", "WEB_GUIDE_LANDING_NEEDS_REVIEW",
                candidate.candidate_id, candidate.source_capture_id,
            )

    lifecycle.dispatcher._branch = CompletedBranch()
    await lifecycle.schedule()
    await asyncio.wait_for(lifecycle.audit_started.wait(), 1)
    branch_task = next(iter(lifecycle.dispatcher._tasks))
    close = asyncio.create_task(lifecycle.dispatcher.close())
    await asyncio.sleep(0)
    assert not close.done()
    assert branch_task.cancelling() == 0
    lifecycle.release_audit.set()
    await asyncio.wait_for(close, 1)
    assert lifecycle.audit_completed.is_set()
    assert lifecycle.outcomes[0].status == "uncertain"
    assert not lifecycle.dispatcher._tasks
