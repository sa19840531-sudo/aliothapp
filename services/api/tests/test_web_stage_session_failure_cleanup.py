"""阶段异常退出须关闭当前浏览器；关闭证据与 Run 失败分别保留。"""

import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest

from services.api.app.agents.runtime.models import TerminationReason
from services.api.app.application.web_runtime_composition import (
    PostgresWebRuntimeProvider,
    ProductionWebV2RuntimeExecutor,
    WebV2Runtime,
)
from services.api.app.application.web_runtime_stage_effects import WebRuntimeStageEffects
from services.api.app.core.config import Settings
from services.api.app.domain.exploration.web_run_stage_checkpoint import WebRunStage
from services.api.app.domain.interaction import SurfaceSessionStatus
from services.api.app.infrastructure.browser.capture_runtime import WebCaptureRuntime
from services.api.app.infrastructure.db.web_run_stage_checkpoint_repository import (
    WebStageCheckpointConflict,
)
from services.api.tests.test_web_runtime_composition import _PolicyRepository, _web_record
from services.api.tests.test_web_runtime_stage_effects import Evidence, command, cursor


class SessionResources:
    """仅替代浏览器与持久化边界，保留生产关闭状态机。"""

    def __init__(self, *, close_error=False, active_command=False):
        self.status = SurfaceSessionStatus.ACTIVE
        self.browser_open = True
        self.close_error = close_error
        self.active_command = active_command
        self.row_version = 1
        self.events = []
        self.capture = WebCaptureRuntime(
            coordinator=SimpleNamespace(),
            session_command_gate=self,
            _browser=self,
            _executor=SimpleNamespace(),
            _evidence_buffer=SimpleNamespace(),
            surface_sessions=self,
        )

    async def get(self, *, session_id):
        assert session_id == "web:run-web"
        return SimpleNamespace(task_id="task-web", status=self.status, row_version=self.row_version)

    async def begin_draining(self, **kwargs):
        if self.active_command:
            raise RuntimeError("SESSION_COMMAND_STILL_LEASED")
        self.status = SurfaceSessionStatus.DRAINING
        return await self.get(session_id=kwargs["session_id"])

    async def seal_exploration_for_close(self, **kwargs):
        self.status = SurfaceSessionStatus.CLOSING
        return await self.get(session_id=kwargs["session_id"])

    async def close_session(self, session_id):
        assert session_id == "web:run-web"
        self.events.append("browser_close")
        if self.close_error:
            raise OSError("BROWSER_CLOSE_FAILED")
        self.browser_open = False

    async def mark_closed(self, **kwargs):
        assert not self.browser_open
        self.events.append("mark_closed")
        self.status = SurfaceSessionStatus.CLOSED

    async def mark_lost(self, **kwargs):
        self.events.append("mark_lost")
        self.status = SurfaceSessionStatus.LOST

    async def release_session(self, **kwargs):
        self.events.append("release")


class FailingStages:
    def __init__(self, boundary, error):
        self.boundary, self.error = boundary, error

    def fail_at(self, boundary):
        if self.boundary == boundary:
            raise self.error

    async def initialize(self, *_args, **_kwargs):
        self.fail_at("initialize")

    async def next_due(self, _run_id):
        self.fail_at("next_due")
        return None if self.boundary == "final_result" else SimpleNamespace(logical_page_key="home")

    async def advance_once(self, *_args):
        self.fail_at("advance_once")

    async def final_result(self, _run_id):
        self.fail_at("final_result")


class PreservedRuntime(WebV2Runtime):
    async def read_persisted_exploration(self, _command):
        return SimpleNamespace(page_timeline=("capture-before-failure",), page_type_keys=("home",))


async def run_failed_stage(
    resources, *, boundary="advance_once", mode="exploration_only", error=None
):
    runtime = PreservedRuntime(
        bootstrapper=SimpleNamespace(),
        capture_runtime=resources.capture,
        stage_orchestrator=FailingStages(
            boundary, error or RuntimeError("WEB_STAGE_HANDLER_FAILED")
        ),
    )
    return await ProductionWebV2RuntimeExecutor(worker_instance_id="worker").run(
        runtime, record=_web_record(execution_mode=mode), settings=Settings()
    )


@pytest.mark.parametrize("boundary", ["initialize", "next_due", "advance_once", "final_result"])
async def test_stage_failure_closes_owned_browser_before_returning_failed_run(boundary):
    resources = SessionResources()
    result = await run_failed_stage(resources, boundary=boundary)
    assert not resources.browser_open
    assert resources.status is SurfaceSessionStatus.CLOSED
    assert resources.events == ["browser_close", "mark_closed", "release"]
    assert result.termination_reason is TerminationReason.ERROR
    assert result.error_message == "WEB_STAGE_HANDLER_FAILED"
    assert result.page_timeline == ("capture-before-failure",)


async def test_stage_failure_and_close_failure_preserve_both_failures_without_false_closed():
    resources = SessionResources(close_error=True)
    result = await run_failed_stage(resources)
    assert resources.status is SurfaceSessionStatus.LOST
    assert resources.events == ["browser_close", "mark_lost", "release"]
    assert result.error_message == "WEB_STAGE_HANDLER_FAILED"
    assert result.final_text == "Web V2 页面探索失败"
    assert result.evidence_gap == ("WEB_EVIDENCE_INCOMPLETE", "WEB_SESSION_CLOSE_FAILED")
    assert result.web_exploration_summary["session_close"] == {
        "status": "failed",
        "error_code": "WEB_SESSION_CLOSE_FAILED",
        "diagnostic_type": "OSError",
    }


async def test_failure_cleanup_cannot_close_session_with_live_command_lease():
    resources = SessionResources(active_command=True)
    result = await run_failed_stage(resources)
    assert resources.browser_open
    assert resources.status is SurfaceSessionStatus.ACTIVE
    assert resources.events == []
    assert result.error_message == "WEB_STAGE_HANDLER_FAILED"
    assert result.web_exploration_summary["session_close"]["status"] == "failed"


async def test_stage_cancellation_propagates_without_unfenced_close():
    resources = SessionResources(active_command=True)
    with pytest.raises(asyncio.CancelledError):
        await run_failed_stage(resources, error=asyncio.CancelledError())
    assert resources.browser_open
    assert resources.events == []


async def test_cancellation_during_failed_stage_summary_does_not_start_cleanup():
    resources = SessionResources()

    class CancelledSummaryRuntime(PreservedRuntime):
        async def read_persisted_exploration(self, _command):
            raise asyncio.CancelledError()

    runtime = CancelledSummaryRuntime(
        bootstrapper=SimpleNamespace(),
        capture_runtime=resources.capture,
        stage_orchestrator=FailingStages("advance_once", RuntimeError("WEB_STAGE_HANDLER_FAILED")),
    )
    with pytest.raises(asyncio.CancelledError):
        await ProductionWebV2RuntimeExecutor(worker_instance_id="worker").run(
            runtime, record=_web_record(), settings=Settings()
        )
    assert resources.browser_open
    assert resources.events == []


async def test_formal_stage_failure_leaves_closure_to_formal_lifecycle():
    resources = SessionResources()
    result = await run_failed_stage(resources, mode="full_inspection")
    assert result.error_message == "WEB_STAGE_HANDLER_FAILED"
    assert resources.browser_open
    assert resources.events == []


@pytest.mark.parametrize("conflict", [False, True], ids=["lease_lost", "checkpoint_conflict"])
@pytest.mark.parametrize("chain", [None, "cause", "context", "both"])
async def test_old_worker_cannot_close_reclaimed_stage_between_commands(conflict, chain):
    resources = SessionResources(active_command=False)
    ownership_error = (
        WebStageCheckpointConflict("阶段已被接管")
        if conflict
        else RuntimeError("WEB_STAGE_LEASE_LOST")
    )
    error = ownership_error
    if chain is not None:
        error = RuntimeError("WEB_STAGE_HANDLER_FAILED")
        if chain == "cause":
            error.__cause__ = ownership_error
        else:
            error.__context__ = ownership_error
            if chain == "both":
                error.__cause__ = RuntimeError("普通包装异常")

    class ReclaimedStages(FailingStages):
        async def advance_once(self, *_args):
            # 新 Worker 已接管阶段，两个命令之间没有 Session 命令租约。
            self.claimed_worker_id = "replacement-worker"
            self.claim_token = "replacement-token"
            resources.row_version = 2
            raise error

    stages = ReclaimedStages("advance_once", error)
    runtime = PreservedRuntime(
        bootstrapper=SimpleNamespace(),
        capture_runtime=resources.capture,
        stage_orchestrator=stages,
    )
    result = await ProductionWebV2RuntimeExecutor(worker_instance_id="old-worker").run(
        runtime, record=_web_record(), settings=Settings()
    )
    assert stages.claimed_worker_id == "replacement-worker"
    assert stages.claim_token == "replacement-token"
    assert resources.row_version == 2
    assert not resources.active_command
    assert resources.browser_open
    assert resources.status is SurfaceSessionStatus.ACTIVE
    assert resources.events == []
    assert result.termination_reason is TerminationReason.ERROR
    assert result.error_message == str(error)
    assert "session_close" not in result.web_exploration_summary


async def test_summary_ownership_conflict_also_prevents_failure_cleanup():
    resources = SessionResources()

    class LostSummaryRuntime(PreservedRuntime):
        async def read_persisted_exploration(self, _command):
            raise WebStageCheckpointConflict("摘要读取前运行归属已变化")

    result = await ProductionWebV2RuntimeExecutor(worker_instance_id="old-worker").run(
        LostSummaryRuntime(
            bootstrapper=SimpleNamespace(),
            capture_runtime=resources.capture,
            stage_orchestrator=FailingStages(
                "advance_once", RuntimeError("WEB_STAGE_HANDLER_FAILED")
            ),
        ),
        record=_web_record(),
        settings=Settings(),
    )
    assert resources.browser_open
    assert resources.status is SurfaceSessionStatus.ACTIVE
    assert resources.events == []
    assert result.error_message == "WEB_STAGE_HANDLER_FAILED"


@pytest.mark.parametrize("mode", ["exploration_only", "full_inspection"])
async def test_provider_routes_stage_close_to_current_exploration_or_bound_formal_runtime(
    mode, monkeypatch, caplog,
):
    caplog.set_level("INFO", logger="services.api.app.application.web_runtime_stage_effects")
    # 这里只测试关闭器绑定，不能让部署环境的数据库配置触发真实恢复读取。
    monkeypatch.delenv("ALIOTH_DATABASE_URL", raising=False)
    resources = SessionResources()
    bindings = []

    class FormalCloser:
        def bind_live_runtime(self, *, session_id, runtime):
            bindings.append((session_id, runtime))

        async def close(self, *, task_id, session_id):
            raise AssertionError(
                f"阶段关闭不应提前调用正式收口器: {task_id}/{session_id}"
            )

    capture = SimpleNamespace(
        coordinator=SimpleNamespace(),
        surface_driver=SimpleNamespace(),
        surface_sessions=resources,
        web_desktop_capture_policy=SimpleNamespace(),
        trusted_ui_snapshot_reader=SimpleNamespace(),
        web_ad_landing_capture_port=SimpleNamespace(),
        web_full_page_capture_browser=SimpleNamespace(),
        web_page_capture_persistence=SimpleNamespace(),
        session_command_gate=resources,
        close_exploration_session=resources.capture.close_exploration_session,
    )
    record = _web_record(execution_mode=mode)
    record.task_config["_task_target"]["target_url"] = "http://91porna.com/"
    runtime = await PostgresWebRuntimeProvider(
        capture_runtime_builder=lambda **_kwargs: capture,
        assessment_agent=SimpleNamespace(),
        policy_repository=_PolicyRepository(),
        page_inspection_notification=SimpleNamespace(),
        formal_session_closer=FormalCloser(),
    ).build(record=record, settings=Settings())
    effects = WebRuntimeStageEffects(
        runtime=runtime,
        evidence=Evidence(),
        command=replace(command(), task_id="task-web", session_id="web:run-web"),
    )
    await effects.execute(cursor(WebRunStage.SESSION_CLOSE))
    if mode == "full_inspection":
        assert resources.browser_open
        assert resources.status is SurfaceSessionStatus.ACTIVE
        assert resources.events == []
        assert len(bindings) == 1
        assert any(
            getattr(record, "session_close_status", None)
            == "deferred_to_formal_reconciliation"
            for record in caplog.records
        )
    else:
        assert not resources.browser_open
        assert resources.status is SurfaceSessionStatus.CLOSED
        assert resources.events == ["browser_close", "mark_closed", "release"]
        assert len(bindings) == 0
