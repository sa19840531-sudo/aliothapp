"""新恢复会话必须先留真实初始状态，不能观察不存在的旧会话或重放动作。"""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from services.api.app.application.capture_coordinator import (
    CaptureCoordinationCommand,
    CaptureCoordinator,
)
from services.api.app.domain.interaction import ActionIntent
from services.api.tests.test_capture_coordinator import (
    RecordingActionExecutionRepository,
    RecordingCaptureWriter,
    RecordingEvidenceExitGate,
    RecordingInteractionExecutor,
    _recovery_intent,
)
from services.api.tests.test_h5_tiktok_popup_regression import saved_case


@pytest.mark.parametrize(
    "fault", ["task", "run", "session", "action", "phase", "parent", "directive"]
)
def test_fresh_surface_authorization_is_exactly_bound_to_original_navigation(fault):
    from services.api.app.application.capture_coordination import CapturePhase, CaptureWriteRequest
    from services.api.app.domain.interaction import SurfaceKind, build_action_execution_id

    intent = ActionIntent.from_dict(saved_case()["recovery_intent"])
    request = CaptureWriteRequest(
        task_id=intent.task_id,
        agent_run_id="f55480fb-6520-4850-ad2a-608bee18f424",
        session_id=intent.session_id,
        surface_kind=SurfaceKind.WEB,
        intent_id=intent.intent_id,
        phase=CapturePhase.BEFORE_ACTION,
        directive=intent.capture_profile.before,
        parent_capture_id=None,
        action_execution_id=build_action_execution_id(intent),
        session_command_sequence=1,
        finalization_epoch=0,
        deadline=intent.deadline,
        recovery_bootstrap_intent=intent,
    )
    mutations = {
        "task": {"task_id": "other"},
        "run": {"agent_run_id": "other"},
        "session": {"session_id": intent.parameters["recovery_source_session_id"]},
        "action": {"action_execution_id": "other"},
        "phase": {"phase": CapturePhase.AFTER_ACTION},
        "parent": {"parent_capture_id": "old"},
        "directive": {"directive": intent.capture_profile.after},
    }
    with pytest.raises(ValueError, match="授权"):
        replace(request, **mutations[fault])


async def test_saved_recovery_intent_prepares_only_new_target_not_lost_source():
    intent = ActionIntent.from_dict(saved_case()["recovery_intent"])
    intent = replace(intent, deadline=datetime.now(UTC) + timedelta(seconds=30))
    events = []

    class FreshSessionWriter(RecordingCaptureWriter):
        async def capture(self, request):
            if not self.requests:
                assert getattr(request, "recovery_bootstrap_intent", None) == intent
                assert request.parent_capture_id is None
                assert request.bootstrap_blank_surface is False
            return await super().capture(request)

    writer = FreshSessionWriter(events)
    executor = RecordingInteractionExecutor(events)
    result = await CaptureCoordinator(
        capture_writer=writer,
        interaction_executor=executor,
        action_execution_repository=RecordingActionExecutionRepository(events),
        evidence_exit_gate=RecordingEvidenceExitGate(),
    ).coordinate(
        CaptureCoordinationCommand(
            agent_run_id="f55480fb-6520-4850-ad2a-608bee18f424", intent=intent
        )
    )
    assert [event for event in events if not event.startswith("action:")] == [
        "capture:before_action",
        "execute",
        "capture:after_action",
    ]
    assert executor.requests[0].intent == intent
    assert result.before_capture.session_id == intent.session_id
    assert result.after_capture.parent_capture_id == result.before_capture.capture_id
    assert intent.source_capture_id == saved_case()["recovery_intent"]["source_capture_id"]
    assert getattr(writer.requests[1], "recovery_bootstrap_intent", None) is None


@pytest.mark.parametrize("mutation", ["purpose", "target_session", "source_session", "operation"])
async def test_untrusted_recovery_never_receives_fresh_surface_authorization(mutation):
    intent = _recovery_intent()
    changes = {
        "purpose": {"purpose": "普通导航"},
        "target_session": {"session_id": intent.parameters["recovery_source_session_id"]},
        "source_session": {
            "parameters": {**intent.parameters, "recovery_source_session_id": "h5:other"}
        },
        "operation": {"parameters": {**intent.parameters, "extra": True}},
    }
    intent = replace(intent, **changes[mutation])
    writer = RecordingCaptureWriter([])
    await CaptureCoordinator(
        capture_writer=writer,
        interaction_executor=RecordingInteractionExecutor([]),
        evidence_exit_gate=RecordingEvidenceExitGate(),
    ).coordinate(CaptureCoordinationCommand(agent_run_id="run-1", intent=intent))
    assert all(getattr(item, "recovery_bootstrap_intent", None) is None for item in writer.requests)


@pytest.mark.parametrize("stage", ["record_source_loss", "reopen"])
async def test_recovery_does_not_start_new_session_without_source_stop_proof(stage):
    from services.api.app.application.h5_exploration_cancellation import H5CancellationCheckpoint
    from services.api.app.application.h5_exploration_recovery import (
        H5FailureStage,
        H5RecoveryContext,
    )
    from services.api.app.application.h5_recapture_lease import H5PhysicalOperationNotDrained
    from services.api.app.application.h5_runtime_composition import _H5BrowserLossRuntime
    from services.api.app.domain.exploration.h5_frontier import H5TaskFrontierSnapshot
    from services.api.tests.test_h5_exploration_turn import _work

    work = _work()
    sessions = SimpleNamespace(
        get=AsyncMock(return_value=SimpleNamespace(status=SimpleNamespace(value="lost"))),
        register_active=AsyncMock(),
    )
    driver = SimpleNamespace(
        start_session=AsyncMock(return_value=SimpleNamespace(primary_surface_ref="fixture"))
    )
    runtime = _H5BrowserLossRuntime(
        capture_runtime=SimpleNamespace(
            surface_sessions=sessions,
            surface_driver=driver,
            close_browser_session=AsyncMock(
                side_effect=H5PhysicalOperationNotDrained("停止未确认")
            ),
        ),
        detector=SimpleNamespace(),
        workflow=SimpleNamespace(),
        cancellation=H5CancellationCheckpoint(SimpleNamespace()),
        now=lambda: datetime.now(UTC),
    )
    context = H5RecoveryContext(
        task_id=work.task_id,
        agent_run_id="run-h5",
        session_id=work.session_id,
        stage=H5FailureStage.ACTION,
        last_safe_location=work.frontier.observation.location,
        action_execution_id="unknown-action",
        frontier_snapshot=H5TaskFrontierSnapshot.create(max_pages=3),
        recovery_attempt_count=0,
        max_recovery_attempts=2,
        failed_work=work,
    )
    with pytest.raises(H5PhysicalOperationNotDrained):
        await getattr(runtime, stage)(context=context)
    driver.start_session.assert_not_awaited()
    sessions.register_active.assert_not_awaited()


async def test_recovery_coordinator_real_browser_navigates_once_and_retains_new_root(tmp_path):
    from services.api.app.application.interaction_runtime import (
        InMemoryActionEvidenceBuffer,
        ResolvedInteractionTarget,
    )
    from services.api.app.infrastructure.browser.capture_runtime import (
        BrowserCaptureWriter,
        BrowserInteractionExecutor,
    )
    from services.api.app.infrastructure.browser.page_capture import PlaywrightPageCaptureBrowser
    from services.api.tests.test_web_capture_runtime import RecordingPersistence, RecordingResolver

    intent = ActionIntent.from_dict(saved_case()["recovery_intent"])
    intent = replace(
        intent,
        deadline=datetime.now(UTC) + timedelta(seconds=60),
        semantic_target="https://fixture.invalid/",
        parameters={**intent.parameters, "destination_url": "https://fixture.invalid/"},
    )
    run_id = "f55480fb-6520-4850-ad2a-608bee18f424"
    browser = PlaywrightPageCaptureBrowser()
    buffer = InMemoryActionEvidenceBuffer()
    persistence = RecordingPersistence()
    delegate = BrowserCaptureWriter(
        browser=browser, persistence=persistence, evidence_buffer=buffer
    )
    navigations = []

    class Writer:
        async def capture(self, request):
            capture = await delegate.capture(request)
            if len(persistence.calls) == 1:
                page = browser._sessions[intent.session_id].page

                async def respond(route):
                    assert route.request.url == "https://fixture.invalid/"
                    navigations.append(route.request.url)
                    await route.fulfill(
                        content_type="text/html",
                        body=(
                            "<html><body><main><h1>恢复业务页</h1>"
                            "<p>已恢复的正文结构和真实页面内容，保留原Run和来源证据归属。</p>"
                            '<button type="button">我的</button></main></body></html>'
                        ),
                    )

                await page.context.route("**/*", respond)
            return capture

    try:
        coordinator = CaptureCoordinator(
            capture_writer=Writer(),
            interaction_executor=BrowserInteractionExecutor(
                browser=browser,
                target_resolver=RecordingResolver(
                    ResolvedInteractionTarget(destination_url="https://fixture.invalid/")
                ),
                evidence_buffer=buffer,
            ),
            action_execution_repository=RecordingActionExecutionRepository([]),
            evidence_exit_gate=RecordingEvidenceExitGate(),
        )
        result = await coordinator.coordinate(
            CaptureCoordinationCommand(agent_run_id=run_id, intent=intent)
        )
        assert navigations == ["https://fixture.invalid/"]
        assert result.before_capture.parent_capture_id is None
        assert result.after_capture.parent_capture_id == result.before_capture.capture_id
        before = persistence.calls[0]["result"]
        after = persistence.calls[-1]["result"]
        assert before.final_url == "about:blank"
        assert before.metrics["page_inspection_ready"] is False
        assert not any(item.artifact_type == "screenshot" for item in before.artifacts)
        assert after.final_url == "https://fixture.invalid/"
        assert after.metrics["page_inspection_ready"] is True
        assert 0 < after.metrics["dom_collection_audit"]["wire_bytes"] < 20000
        assert after.metrics["dom_collection_audit"]["truncated"] is False
        (tmp_path / "recovery-root.html").write_text(
            next(item.content for item in before.artifacts if item.artifact_type == "dom")
        )
        (tmp_path / "recovery-after.html").write_text(
            next(item.content for item in after.artifacts if item.artifact_type == "dom")
        )
    finally:
        await browser.close_session(intent.session_id)
